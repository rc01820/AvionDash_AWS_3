# AvionPulse v3 — How It Works

This guide explains how the pieces fit together: what runs where, how a request flows, how the
chaos engine changes real AWS resources and puts them back, how the Classic tier is launched and
destroyed, and how everything reaches Datadog. Read it before changing the code. It's also the best
way to explain the app to someone else.

**Contents**

1. [The big picture](#1-the-big-picture)
2. [Request path](#2-request-path)
3. [Data model](#3-data-model)
4. [Background work](#4-background-work)
5. [The integration tier](#5-the-integration-tier)
6. [The chaos engine](#6-the-chaos-engine)
7. [Drift read-back (AWS Resources page)](#7-drift-read-back-aws-resources-page)
8. [The Classic tier lifecycle](#8-the-classic-tier-lifecycle)
9. [Security model](#9-security-model)
10. [Telemetry: how signals reach Datadog](#10-telemetry-how-signals-reach-datadog)
11. [Local mode](#11-local-mode)
12. [Adding a new fault](#12-adding-a-new-fault)
13. [File map](#13-file-map)

---

## 1. The big picture

AvionPulse is a fictional airline operations centre. It runs as **two CloudFormation stacks**:

| | Core stack (`<stack>`) | Classic stack (`<stack>-classic`) |
|---|---|---|
| Template | `cloudformation/avionpulse.yaml` | `cloudformation/classic.yaml` |
| Created by | You (deploy script or console) | The app's `api` Lambda, from the Chaos page |
| Lifetime | Always on | 1–8 hours, then deleted automatically |
| Compute | 7 Lambda functions | EC2 Auto Scaling group (1–2 × t4g.micro) |
| Data | DynamoDB, SQS, S3, SSM | RDS PostgreSQL |
| Cost | ≈ $1–1.50/month | ≈ $0.07–0.11/hour while it exists |

```
                    ┌───────────────────────────── CORE ─────────────────────────────────┐
 Browser ─► CloudFront ─► S3 (SPA)                                                      │
                │ /api/*                                                                │
                ▼                                                                       │
          API Gateway HTTP API ── data integration ────► λ api ──► DynamoDB ──Stream──► λ processor
                │                └─ control integration ─┘    │       ▲                  ▲  ▲  ▲  ▲
                │                                             ├─► SQS (KMS) ─► λ worker ─┘  │  │  │  │
                │                                             └─► CloudFormation (Classic)  │  │  │  │
          EventBridge ─(5 min)─► λ simulator ─► DynamoDB, SQS, bus ──────────────────────────┘  │  │  │
                                              ─► SNS crew-alerts ──────────────────────────────┘  │  │
                                              ─► Step Functions turnaround ───────────────────────┘  │
                                              ─► S3 recorder ─► EventBridge (default bus) ───────────┘
          EventBridge ─(2 min)─► λ janitor ─► reverts expired faults, expires the Classic stack
                    └────────────────────────────────────────────────────────────────────┘
                    ┌───────────────────────────── CLASSIC ──────────────────────────────┐
 λ api /api/gates ─►│ ALB (header secret) ─► EC2 ASG: gate service + Datadog Agent ─► RDS │
                    └────────────────────────────────────────────────────────────────────┘
```

---

## 2. Request path

1. **CloudFront** serves the single-page app from S3 and forwards `/api/*` **and `/status`** to API
   Gateway (caching disabled, all viewer headers forwarded except Host). `/status` is the public
   status page, rendered as HTML by the `api` function.
2. **API Gateway HTTP API** has **two Lambda integrations** pointing at the same `api` function:

   | Route | Integration | Throttle | Why separate |
   |---|---|---|---|
   | `ANY /api/{proxy+}` (data) | `ApiIntegration` | `ApiRateLimit`/`ApiBurstLimit` (20/40) | `api_throttling` and `api_integration_broken` target this one |
   | `ANY /api/auth/{proxy+}` | `ControlIntegration` | 5 / 10 | Login always works |
   | `ANY /api/chaos/{proxy+}` | `ControlIntegration` | 5 / 10 | You can always switch a fault off |
   | `ANY /api/classic/{proxy+}` | `ControlIntegration` | 5 / 10 | You can always tear the Classic tier down |
   | `ANY /api/aws/{proxy+}` | `ControlIntegration` | 2 / 4 | Drift panel (many Describe calls) |
   | `GET /status` | `ApiIntegration` | default | Public status page. On the data integration on purpose, so `api_integration_broken` takes it down like a real outage would |

3. **`api.handler`** (`src/api.py`) is a small router:
   * `Request` wraps the API Gateway v2 event (method, path, query, headers, body).
   * Routes are registered with `@route(method, regex, role=…, data=…, exempt=…)`.
     * `role`: minimum role from the JWT (`viewer` < `operator` < `admin`).
     * `data=True`: the route touches the data tier, so the `dynamodb_throttle` and `sqs_poison_pill` request faults apply.
     * `exempt=True`: request-path faults are skipped (auth, chaos, classic, drift).
   * `_inject_request_faults()` runs the active request-path faults (sleep, burn CPU, allocate, raise, …).
   * `_map_exception()` turns AWS error codes into HTTP responses:

     | Error | HTTP | `error_code` |
     |---|---|---|
     | `ProvisionedThroughputExceededException`, `ThrottlingException` | 503 | as AWS reports it |
     | `KMS.*`, `KMSDisabledException` | 503 | as AWS reports it |
     | `AccessDenied`, `AccessDeniedException` | 500 | as AWS reports it |
     | `NoSuchKey` | 502 | `NoSuchKey` |
     | `infra.ConfigError` (bad SSM flags) | 500 | `ConfigParseError` |
     | URL timeout | 504 | `UpstreamTimeout` |
     | `classic.ClassicError` | its own status | e.g. `ClassicOffline`, `ALB503`, `GateServiceTimeout` |
     | `chaos.UnhandledFault` (`random_500s`) | not caught: Lambda records an Error and API Gateway returns a bare 500 | — |

   * Every response carries `x-avion-duration-ms`, `x-avion-cold-start` and `x-avion-request-id`.
     The top bar shows them.

**Routes**

| Method & path | Role | What it does |
|---|---|---|
| `GET /status` (HTML), `GET /api/status` (JSON) | public | Status page: component health, 15 s cache; `status_page_error` → 500 (`src/status.py`) |
| `GET /api/health`, `/api/health/deep` | public | Liveness; deep checks DynamoDB, SQS and S3 individually |
| `POST /api/auth/token`, `GET /api/auth/me` | public / viewer | PBKDF2 login → HS256 JWT (8 h) |
| `GET /api/dashboard/public-stats`, `/summary` | public / viewer | KPIs, busiest origins, data freshness |
| `GET /api/flights[/{number}]`, `/aircraft`, `/airports` | viewer | Registry and flight board |
| `GET`/`POST /api/events` | viewer / operator | Processed events / enqueue a maintenance request (202) |
| `GET /api/notams`, `/weather` | viewer | S3 read, synthetic METAR |
| `GET /api/events/queue` | viewer | Queue and DLQ depth for the Ops Pipeline page |
| `GET /api/system` | operator | Container info, queues (Architecture page) |
| `GET /api/integrations` | viewer | Last-seen counters of the five integration channels |
| `GET /api/gates` | viewer | Proxy to the Classic gate service through the ALB |
| `GET /api/aws/drift` | operator | Baseline vs. current for every chaos-controlled setting |
| `GET /api/classic/status`; `POST /api/classic/launch`, `/extend`, `/teardown` | operator; admin | Classic stack lifecycle |
| `GET /api/chaos/status`, `/catalog`; `POST /api/chaos/{name}/toggle`, `/reset-all` | operator; admin | Chaos engine |

---

## 3. Data model

One DynamoDB table, `<stack>-data`, provisioned at **5 RCU / 5 WCU** (inside the always-free
allowance, and small enough that throttling faults are real). Single-table design:

| PK | SK | Item | Written by |
|---|---|---|---|
| `AIRPORT` | IATA code | Airport directory (25) | seed |
| `AIRCRAFT` | tail number | Fleet (15, tails `N-AVP…`) | seed |
| `FLIGHT` | flight number | Live state of 25 flights | simulator, every run |
| `USER` | username | Demo users, PBKDF2 hashes | seed |
| `EVENT` | `<iso ts>#<id>` | Processed ops / maintenance events, 7-day TTL | worker |
| `CONFIG` | `FAULTS` | Chaos state: `{faults: {name: {until, by, experiment_id?}}, api_id}` | chaos engine |
| `CONFIG` | `SECRET` | JWT signing secret | seed (once) |
| `CONFIG` | `CLASSIC` | Classic record: size, launched_at/by, expires_at, shared secret, last session | classic |
| `INTEG` | `stream` / `bus` / `sns` / `turnaround` / `recorder` | `count`, `last_seen`, `last_detail` per channel | processor |

The table's **stream** (`KEYS_ONLY`) feeds the processor. The event source mapping filters on
`PK = FLIGHT`, so the processor's own `INTEG` writes never loop back.

Other state:

* **SQS** `<stack>-events` (encrypted with the customer-managed KMS key, 60 s data-key reuse) →
  worker; redrive to `<stack>-events-dlq` after 3 receives.
* **S3 site bucket**: the SPA and `data/notams.json`.
* **S3 recorder bucket**: `recorder/YYYY/MM/DD/HHMMSS.json` snapshots, 1-day expiry.
* **SSM** `/<stack>/config/flags`: JSON feature flags (`board_page_size`, `weather_enabled`,
  `maintenance_intake`), read with a 15 s cache.

---

## 4. Background work

| Function | Trigger | Does |
|---|---|---|
| `simulator` | EventBridge rule `<stack>-simulator-tick`, every 5 min (`SimulatorSchedule`) | Recomputes all 25 flights (deterministic lifecycle model in `flightsim.py`) and writes them (paced to 5 WCU); emits business metrics; enqueues 1–3 ops events; feeds all integration channels (§5); runs the scheduled side of async faults (cold-start recycling, backlog flood, poison pills, crash) |
| `worker` | SQS event source mapping, batch 10, 5 s window, partial batch failures | Validates each message and writes it as an `EVENT` item; a poison message fails alone and is redriven |
| `janitor` | EventBridge rule `<stack>-janitor-tick`, every 2 min | `chaos.janitor()` reverts expired faults; `classic.janitor()` tears down an expired Classic stack and retries a failed delete |
| `processor` | DynamoDB Stream, SNS, two EventBridge rules, Step Functions tasks | §5 |
| `seed` | CloudFormation custom resource | Seeds the table (re-runs when `SeedVersion` changes) |
| `site-deployer` | Two custom resources | Publishes `web/` to S3 + writes `config.js` + invalidates CloudFront; on delete empties the site and recorder buckets |

**Why the janitor has its own rule:** `eventbridge_rule_disabled` disables the simulator's rule.
If the janitor still ran inside the simulator (as in v2), that fault could never expire.

The simulator handles each integration channel separately (`attempt()` in `simulator.py`). A
broken channel logs `simulator: <channel> failed`, increments `avionpulse.integration.errors`
and lets the others run.

---

## 5. The integration tier

Five pipes, each fed once per simulator run and consumed by `processor.handler`, which updates
one `INTEG/<channel>` item. The **Integrations** page shows each pipe as FLOWING or STALE, based
on how long ago it last delivered.

| Channel | Producer → transport → consumer | Breaks with |
|---|---|---|
| `stream` | simulator writes FLIGHT items → DynamoDB Stream → ESM (filter `PK=FLIGHT`, retries 10 000, max age 1 h) → processor | `stream_processor_poison` (processor raises; iterator age climbs) |
| `bus` | simulator `PutEvents` (FlightStatusChanged, ≤ 5 per run, a heartbeat when nothing changed) → custom bus `<stack>-flight-ops` → rule `<stack>-flight-status` (2 retries, 5 min max age) → processor | `eb_target_broken` |
| `sns` | simulator `Publish` (one crew-duty alert) → topic `<stack>-crew-alerts` → Lambda subscription → processor | `sns_delivery_failure` |
| `turnaround` | simulator `StartExecution` → Express state machine `<stack>-turnaround`: AssignGate → Refuel → CrewCheck (5 s timeout) → Boarding → Complete; each Task invokes the processor with `{"task": …}` | `stepfn_task_failure` |
| `recorder` | simulator `PutObject` → recorder bucket → S3 "Object Created" on the default event bus → rule → processor | `s3_bucket_policy_deny` |

The processor recognises its input by shape: `task` key (Step Functions), `eventSource: aws:dynamodb`
(stream), `EventSource: aws:sns` (SNS), `detail-type` (EventBridge).

---

## 6. The chaos engine

`src/chaos.py` holds the **catalog** (50 entries) and the state logic. `src/infra.py` and
`src/classic.py` hold the AWS calls.

**Catalog entry fields:** `label`, `tier`, `severity`, `aws`, `icon`, `description`,
`datadog_signal`, plus:

* `infra: True` → the fault changes an AWS resource through `_apply_infra()`.
* `classic: True` → it needs the Classic tier online.
* `full_only: True` → it needs the Full size.

**State.** One item, `CONFIG/FAULTS`. Every Lambda reads it through `chaos.active(name)` with a
3 s cache; the Classic EC2 service reads it with a 5 s cache. Each active fault stores its expiry
(`until`), who switched it on (`by`), and for FIS faults the `experiment_id`.

**Toggle** (`chaos.toggle`):

1. Validate the name and clamp the TTL to 1–120 min.
2. Classic faults: `classic.fault_blocker()` refuses (HTTP 409 `ClassicOffline`) if the tier
   isn't online, or a Full-only fault is on Minimal.
3. Expand composites (`cascading_failure`, `security_incident`, `control_plane_storm`) into their members.
4. For each member with `infra: True`, call `_apply_infra(name, enabled, …)`. If AWS refuses
   (e.g. `LimitExceededException`), the error is added to `notes` and shown on the Chaos page
   instead of pretending it worked.
5. Save the state, log `[CHAOS] … ON/OFF`, and post a Datadog event.

**Infra dispatch** (`_apply_infra`): each fault maps to a pair of infra functions, one to break
and one to restore:

| Fault | Break | Restore |
|---|---|---|
| `api_throttling` | `UpdateStage` 1 rps / burst 2 | `UpdateStage` template values |
| `worker_throttle` | `PutFunctionConcurrency 0` | `DeleteFunctionConcurrency` |
| `queue_backlog` | `UpdateEventSourceMapping Enabled=false` | `Enabled=true`, unless `sqs_retention_cut` still needs it off |
| `cold_start_storm` | `UpdateFunctionConfiguration` env nonce + slow init | env without slow init |
| `dynamodb_capacity_cut` | `UpdateTable` 1/1 | `UpdateTable` 5/5 (skipped if already there) |
| `eventbridge_rule_disabled` | `DisableRule` | `EnableRule` |
| `api_integration_broken` | `UpdateIntegration` URI → `…:chaos-missing-alias` | URI → function ARN |
| `lambda_bad_deploy` | worker `DD_LAMBDA_HANDLER` (or `Handler`) → `worker_v2.handler` | `worker.handler` |
| `ssm_config_poisoned` | `PutParameter` truncated JSON | `PutParameter` defaults |
| `sqs_retention_cut` | retention 60 s + trigger off | 86400 s + trigger on (unless `queue_backlog`) |
| `iam_policy_detached` | `DetachRolePolicy` | `AttachRolePolicy` |
| `kms_key_disabled` | `DisableKey` | `EnableKey` |
| `sqs_send_denied` | `SetQueueAttributes Policy=<deny>` | `Policy=""` |
| `s3_bucket_policy_deny` | `PutBucketPolicy <deny>` | `DeleteBucketPolicy` |
| `eb_target_broken` | `PutTargets` ARN → missing alias | `PutTargets` real ARN |
| `sns_delivery_failure` | `RemovePermission` (statement found with `GetPolicy`) | `AddPermission` |
| Classic / FIS | see §8 | |

Request-path faults (no `infra` flag) are just `if chaos.active(...)` checks in the code: in
`api.py`, `simulator.py`, `processor.py` and, for `classic_db_slow_query`, the EC2 gate service.

**Expiry.** The janitor reverts any fault past its `until` (infra changes included).
**RESET ALL** reverts *every* infra fault, marked active or not, which is cheap and idempotent.
It also covers Classic faults while that tier is online.

**Break glass.** `scripts/restore.py` does the same restores with the AWS CLI and your own
credentials, for when the app can't be reached.

---

## 7. Drift read-back (AWS Resources page)

`infra.DRIFT_CHECKS` lists, for each infra fault, a resource, a setting, the template baseline and
a *reader* that asks AWS for the current value: `GetStage`, `GetIntegration`,
`GetFunctionConfiguration`, `GetFunctionConcurrency`, `ListEventSourceMappings`, `DescribeTable`,
`DescribeRule`, `ListAttachedRolePolicies`, `DescribeKey`, `GetQueueAttributes`, `GetBucketPolicy`,
`GetPolicy`, `ListTargetsByRule` and `GetParameter`. `classic.drift_rows()` adds four rows while the
Classic tier is online. Each row is `ok`, `drift` or `error`. A reader that fails doesn't hide the
others.

Because it reads AWS directly, the page also catches drift that **didn't** come from the app: a
change someone made by hand in the console shows up the same way.

---

## 8. The Classic tier lifecycle

### Launch (`POST /api/classic/launch`)

```
admin ─► λ api ─► classic.launch(size, hours)
           ├─ validate: size, 1–8 h, no Classic stack exists
           ├─ generate a 32-hex shared secret
           ├─ cloudformation:CreateStack <stack>-classic
           │     TemplateBody = classic_tier/classic.yaml (from the code zip)
           │     RoleARN      = ClassicCfnRole        ← CloudFormation creates the resources, not the Lambda
           │     OnFailure    = DELETE, Timeout 40 min, tags app/tier/avionpulse:classic/avionpulse:expires
           ├─ scheduler:CreateSchedule <stack>-classic-expiry
           │     at(<expiry>) → aws-sdk:cloudformation:deleteStack {StackName, RoleARN}
           │     ActionAfterCompletion = DELETE
           ├─ save CONFIG/CLASSIC
           └─ metric avionpulse.classic.launch + Datadog event
```

The api function returns at once (HTTP 202). The Chaos page polls `/api/classic/status` every
5 s while the tier is launching. That endpoint runs `DescribeStacks` + `DescribeStackEvents`, with
a 4 s cache, and the card shows progress (created resources / ~30) and the last 6 events.

### What CloudFormation builds (`classic.yaml`)

| Resource | Detail |
|---|---|
| VPC `10.42.0.0/16` | 2 public app subnets (AZ a/b), 2 isolated DB subnets with their own route table and **network ACL** |
| Security groups | ALB (80 from anywhere) → instances (8080 from the ALB only, as a separate ingress resource) → RDS (5432 from instances) |
| ALB `<stack>-classic` | Listener 80: default **403**; rule priority 10 forwards only when `X-AvionPulse-Key` equals the shared secret |
| Launch template + ASG | Amazon Linux 2023 arm64, t4g.micro, IMDSv2, instance profile from the core stack; 1 (Minimal) or 2 (Full) instances; ELB health checks; group metrics on |
| RDS | PostgreSQL 17, db.t4g.micro, 20 GB gp3, Multi-AZ on Full, RDS-managed master password (Secrets Manager), no backups, deletion protection off |

**Instance boot (user data):** installs Python 3.11 and `psycopg[binary]`, `boto3`, `ddtrace`;
downloads `classic_tier/gate_service.py` from the code zip in S3 and the RDS CA bundle. With
Datadog enabled, it installs the **Datadog Agent** (API key from SSM, logs and processes on,
tags `app:avionpulse, tier:classic`). It then writes a systemd unit running the service under
`ddtrace-run`.

**Gate service** (`src/classic_tier/gate_service.py`):

* `GET /health` answers without touching the database, so network faults between the app and the DB keep
  the ALB "healthy". That's the point of `classic_nacl_db_block`.
* `GET /gates` opens a TLS connection to RDS (verified against the RDS CA, 3 s connect timeout),
  reassigns one random gate and returns all 24.
* It reads the `classic_db_slow_query` switch from DynamoDB and continues the Lambda's trace from
  the `x-datadog-*` headers.

**Request path:** browser → CloudFront → API Gateway → `api` Lambda (`/api/gates`) → HTTP with the
shared-secret header and trace headers → ALB → EC2 → RDS. The api Lambda waits up to 8 s.

### Classic faults

| Fault | Call | Revert |
|---|---|---|
| `classic_sg_lockdown` | `RevokeSecurityGroupIngress` (ALB → 8080) | `AuthorizeSecurityGroupIngress` |
| `classic_nacl_db_block` | `CreateNetworkAclEntry` #10 deny TCP 5432 inbound | `DeleteNetworkAclEntry` #10 |
| `classic_asg_zero` | `UpdateAutoScalingGroup` min/desired 0 | min/desired = 1 or 2 |
| `classic_alb_maintenance` | `ModifyRule` → fixed 503 | `ModifyRule` → forward |
| `classic_rds_reboot` | `RebootDBInstance` (`ForceFailover` on Full) | none (one-shot) |
| `fis_*` | `fis:StartExperiment` on a core-stack template; experiment id saved | `fis:StopExperiment` (ignored if already finished) |

The FIS templates live in the **core** stack and target resources by tag
(`avionpulse:classic=<stack>`, plus `avionpulse:az=a` for the AZ subnets). They cost nothing
until started, and they always find whatever the current Classic stack contains.

### Extend, teardown, expiry

* **Extend** (`+1 h`): new expiry = min(launch + 8 h, max(current expiry, now) + 1 h); `UpdateSchedule`.
* **Teardown**: stops running FIS experiments → `DeleteStack` (with `ClassicCfnRole`) → `DeleteSchedule`
  → drops Classic faults from the state → records "last session" (size, hours, cost, reason) → event.
  A second teardown while deleting is a no-op.
* **Expiry**: the Scheduler job deletes the stack. Every 2 minutes the janitor also checks: if the
  stack is past its expiry it calls teardown; if it's `DELETE_FAILED` it retries; if it's gone it
  clears the record.

### Failure handling

| Situation | What happens |
|---|---|
| Create fails (quota, AZ capacity…) | `OnFailure=DELETE` removes it; the card shows the failure events; launch again |
| Delete fails (ENIs still detaching) | Card shows FAILED + **Retry teardown**; the janitor retries every 2 min |
| App broken by a fault | The Scheduler job still deletes the stack on time |
| Everything broken | `python scripts/restore.py --classic-down` |

---

## 9. Security model

| Principal | Can do | Can't do |
|---|---|---|
| **`AppRole`** (api, simulator, janitor, processor, seed) | Read/write its table; send to its queue; use and **disable/enable** its KMS key; change *named* resources of its own stack (stage, integration, worker config/concurrency, simulator rule, bus rule targets, recorder bucket policy, flags parameter, processor permissions); attach/detach **only** `WorkerDataPolicy` on `WorkerRole` (`iam:PolicyARN` condition); create/delete **only** the stack `<stack>-classic` and pass **only** `ClassicCfnRole` / `ClassicSchedulerRole`; break Classic resources **only** with tag `avionpulse:classic=<stack>`; start/stop FIS experiments | Create EC2, ALB or RDS; touch other stacks, tables, queues or keys; read `<stack>-data-restricted-ledger` (deliberately, for `iam_access_denied`) |
| **`WorkerRole`** | Poll its queue, decrypt with its key (inline); write the table (`WorkerDataPolicy`, detachable) | Anything else |
| **`ClassicCfnRole`** (assumed only by CloudFormation) | `ec2:*`, `elasticloadbalancing:*`, `autoscaling:*`, `rds:*` in this region; pass only the Classic instance role; RDS-managed secrets `rds!*`; create service-linked roles for ASG / ELB / RDS | IAM roles or policies (the instance role is pre-created in the core stack) |
| **`ClassicInstanceRole`** | SSM agent; read the code zip; `GetItem` the chaos state; read `rds!*` secrets; read the Datadog key | Write anything |
| **`ClassicSchedulerRole`** | `DeleteStack` on `<stack>-classic`; pass `ClassicCfnRole` | — |
| **`FisRole`** | AWS-managed FIS EC2 / network / RDS / SSM access policies | — |
| **`StatesRole`** | Invoke the processor | — |

**App users.** PBKDF2-hashed passwords, HS256 JWTs signed with a per-deployment secret. Roles
are ordered `viewer` < `operator` < `admin`:

| Role | Operations pages | Platform pages (AWS Resources, Architecture & Cost, Chaos Control) | Writes |
|---|---|---|---|
| `viewer` | yes | **no**: removed from the page, redirected to Dashboard, API returns 403 | none |
| `operator` | yes | yes, read only | maintenance requests |
| `admin` | yes | yes | chaos faults, Classic tier lifecycle |

Enforcement is on both sides: `@route(..., role="operator")` on the platform endpoints, and
`app.js` removes the platform links and fault badge for viewers. The login page shows no
credentials, deployed or local; the demo users are documented in the README. **Set `AdminPassword`.** The public status page is the
only page that needs no login, and it publishes component health only.

**Network.** The ALB is public, but the listener only forwards requests that carry the shared secret
(generated at launch, held in DynamoDB). Instances accept traffic only from the ALB. RDS is not
public and only accepts traffic from the instances. TLS to RDS is verified against the RDS CA.

---

## 10. Telemetry: how signals reach Datadog

| Signal | Path | Latency |
|---|---|---|
| Lambda traces, logs, enhanced metrics | Datadog Lambda layer + Extension (`DD_*` env vars, handler wrapped by `datadog_lambda.handler.handler`) | ~1 min |
| Custom metrics `avionpulse.*` | `datadog_lambda.metric.lambda_metric` → Extension (distribution metrics) | ~1 min |
| AWS service metrics (`aws.apigateway.*`, `aws.dynamodb.*`, `aws.sqs.*`, `aws.events.*`, `aws.sns.*`, `aws.states.*`, `aws.applicationelb.*`, `aws.rds.*`, `aws.ec2.*`, `aws.autoscaling.*`) | Datadog AWS integration polling CloudWatch | 5–15 min |
| CloudTrail (who changed what) | Trail → S3 → Forwarder / Firehose → Datadog Logs → Cloud SIEM | 5–15 min |
| Chaos and Classic events | `telemetry.event()` → Datadog Events API (`source:avionpulse-chaos`) | seconds |
| Classic hosts, processes, logs | Datadog Agent on EC2 | ~1 min |
| Classic traces | `ddtrace-run` (psycopg auto-instrumented), context continued from the Lambda | ~1 min |
| RUM (optional) | Browser SDK, linked to Lambda traces via `allowedTracingUrls` | real time |

Without the Datadog layer, metrics are written as structured log lines (`metric_name`,
`metric_value`, `metric_tags`), so nothing is lost; it can be rebuilt with log-based metrics.

Full setup and per-fault monitors: [DATADOG_SETUP.md](DATADOG_SETUP.md).

---

## 11. Local mode

`scripts/local_server.py` runs all of the above in one process with in-memory stand-ins for
every AWS service. See [LOCAL_MODE.md](LOCAL_MODE.md).

---

## 12. Adding a new fault

1. **Catalog**: add an entry to `CATALOG` in `src/chaos.py` (pick a `tier`; set `infra: True` if it
   changes AWS, `classic: True` if it needs the Classic tier).
2. **Behaviour**:
   * Request-path fault: add an `if chaos.active("<name>"):` where it should bite.
   * Infra fault: write `set_<thing>(broken)` in `src/infra.py` (or `classic.apply_fault`) with a
     `config.LOCAL` branch that updates `LOCAL_SIM`, and add a line to `_apply_infra`.
3. **Drift**: add a row to `infra.DRIFT_CHECKS` (or `classic.drift_rows`) with a reader.
4. **Permissions**: add the minimum IAM actions to `ChaosInfraPolicy` or `ClassicControlPolicy`,
   scoped by name, ARN or tag.
5. **Break glass**: add the restore step to `scripts/restore.py`.
6. **Local**: make `local_server.py` or the fake store react to the new `LOCAL_SIM` field, so the
   symptom shows locally.
7. **Docs**: a row in [FAULT_SCENARIOS.md](FAULT_SCENARIOS.md), and a monitor in
   [DATADOG_SETUP.md §6](DATADOG_SETUP.md#6-one-monitor-per-fault).
8. **Test**: run it locally (on → symptom → AWS Resources DRIFT → off → OK), lint the template
   (`cfn-lint`), deploy, and check the monitor fires.

The Chaos page needs no change. It builds itself from `/api/chaos/catalog`.

---

## 13. File map

| Path | Role |
|---|---|
| `cloudformation/avionpulse.yaml` | Core stack (72 resources, 73 with the optional Budget) |
| `cloudformation/classic.yaml` | Classic stack (31 resources), also packaged as `classic_tier/classic.yaml` |
| `src/api.py` | API router, request faults, error mapping, all routes |
| `src/chaos.py` | Fault catalog, state, toggle/reset/janitor logic, composites |
| `src/infra.py` | AWS calls for serverless infra faults, integration producers, flags, drift, local stand-ins |
| `src/classic.py` | Classic stack lifecycle, schedule, Classic/FIS faults, gate proxy, drift rows, local simulation |
| `src/janitor.py` | Scheduled janitor Lambda |
| `src/simulator.py` · `src/flightsim.py` | Flight model, business metrics, integration feeds |
| `src/worker.py` | SQS consumer |
| `src/processor.py` | Integration consumer (Streams, SNS, EventBridge, Step Functions) |
| `src/store.py` | DynamoDB access + `FakeStore` |
| `src/telemetry.py` | Logging, custom metrics, Datadog events |
| `src/status.py` | Public status page: component checks, 15 s cache, HTML + JSON rendering |
| `src/auth.py` | Passwords and JWTs |
| `src/seed.py` · `src/seed_data.py` | Seed data (custom resource) |
| `src/site_deployer.py` · `src/cfn.py` | Publishes the SPA; empties buckets on delete; custom-resource plumbing |
| `src/classic_tier/gate_service.py` | EC2 gate service |
| `web/` | SPA: `index.html`, `static/js/app.js`, `static/css/app.css`, `data/notams.json` |
| `scripts/` | `deploy-cfn`, `teardown`, `package.py`, `local_server.py`, `loadgen.py`, `classic.py`, `restore.py` |
| `docs/` | This guide, LOCAL_MODE, DEPLOYMENT, DATADOG_SETUP, FAULT_SCENARIOS, AWS_RUNBOOKS, COST, `datadog/` dashboard JSON |
