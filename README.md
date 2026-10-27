# AvionPulse v3 — Cloud Operations (AvionDash Suite)

> An AWS application built to break in realistic ways, so you can exercise Datadog against
> **AWS itself**, not just the app: serverless telemetry, AWS control-plane changes, security
> misconfigurations, event-driven integrations, and an on-demand **EC2 / ALB / RDS** tier with
> AWS Fault Injection Service experiments.

v3 builds on AvionDash AWS 2 (the serverless AvionPulse). Everything from v2 is still here: the
same pages, the same 23 faults and the same cost profile. On top of that, v3 adds:

* **26 AWS infrastructure faults** that change real AWS resources (capacity, rules, IAM, KMS,
  resource policies, integrations, security groups, NACLs, Auto Scaling, RDS) and put them back.
* **An integration tier**: DynamoDB Streams, an EventBridge custom bus, SNS, a Step Functions
  Express workflow, and an S3 flight-data recorder. Each one is a new thing to monitor and to break.
* **An on-demand Classic tier**: a second CloudFormation stack (VPC, ALB, EC2 Auto Scaling, RDS
  PostgreSQL) that you **launch from the Chaos page** and that **deletes itself** when its time is up.
* **An AWS Resources page** that reads every setting a fault can change and shows any drift from
  the template.
* **Break-glass tools**: a janitor on its own schedule, `scripts/restore.py`, and a Classic CLI.

---

## Architecture

```
                           ┌──────────────────── CORE STACK (always on, ≈ $1–1.50/month) ─────────────────────┐
 Browser ──► CloudFront ──►│ S3 (SPA)                                                                          │
   (RUM)          │ /api/* │ API Gateway HTTP API ─┬─ data integration ──► λ api ──► DynamoDB (+ Stream)       │
                  └───────►│                       └─ control integration ─┘   │      SQS (KMS) ──► λ worker   │
                           │ EventBridge ─► λ simulator ─► bus · SNS · Step Functions · S3 recorder ─► λ processor
                           │ EventBridge ─► λ janitor (fault TTLs + Classic auto-shutdown backstop)           │
                           │ FIS templates · Classic CFN role · Scheduler role  (free until used)              │
                           └───────────────────────────────┬───────────────────────────────────────────────────┘
                                           CreateStack /   │  DeleteStack (Chaos page, Scheduler, janitor)
                           ┌───────────────────────────────▼──── CLASSIC STACK (on demand, ≈ $0.07–0.11/hour) ──┐
       λ api /api/gates ──►│ ALB ──► EC2 Auto Scaling (gate service + Datadog Agent) ──► RDS PostgreSQL         │
                           └────────────────────────────────────────────────────────────────────────────────────┘
        Lambdas ──► Datadog Lambda Extension · EC2 ──► Datadog Agent + ddtrace · AWS ──► Datadog AWS integration
```

| Tier | AWS services | New in v3 |
|---|---|---|
| Web | CloudFront + S3 | — |
| App | API Gateway HTTP API (two integrations) + Lambda | separate control integration so login/chaos survive `api_integration_broken` |
| Data | DynamoDB (Streams on), SQS + DLQ (customer KMS key), SSM feature flags | KMS, Streams, SSM flags |
| Integration | EventBridge custom bus, SNS, Step Functions Express, S3 recorder, λ processor | all new |
| Scheduling | EventBridge rules (simulator, janitor), EventBridge Scheduler (Classic expiry) | janitor + Scheduler |
| Classic (on demand) | VPC, ALB, EC2 ASG (t4g.micro), RDS PostgreSQL (db.t4g.micro), AWS FIS | all new |

## Pages

| Page | What it shows | AWS path |
|---|---|---|
| Dashboard · Flights · Fleet · Airports · Ops Pipeline · NOTAMs | as in v2 | DynamoDB, SQS, S3 |
| **Integrations** | Five pipes and whether each is flowing: stream, bus, SNS, workflow, recorder | Streams, EventBridge, SNS, Step Functions, S3 |
| **Gates** (Classic) | Gate board served by EC2 from RDS | λ → ALB → EC2 → RDS (one distributed trace) |
| **AWS Resources** | Every chaos-controlled setting: template baseline vs. current value | Describe/Get APIs across 12 services |
| Architecture & Cost | Core + v3 diagrams, cost model including Classic hourly rates | `/api/system` |
| **Chaos Control** | 49 faults in 11 tiers, Classic tier launch/extend/teardown card | everything above |

Demo users: `admin / aviondash123` (chaos and Classic launch), `operator / operator123`,
`viewer / viewer123`. **Set `AdminPassword` when you deploy.** The admin user can start paid
EC2 and RDS resources.

## Quick start

### Run locally (no AWS account, no cost)

```bash
python3 scripts/local_server.py
```

(Windows: `python scripts\local_server.py`.) Open http://localhost:8080 and sign in as
`admin` / `aviondash123`.

Local mode runs the **real application code** with in-memory stand-ins for every AWS service:
DynamoDB, SQS, S3, SNS, EventBridge, Step Functions, SSM, KMS, IAM, CloudFormation, EC2, ALB, RDS
and FIS. Nothing touches AWS and nothing is billed; you need only Python 3.9+.

* All 49 faults work. Most produce the same HTTP errors and AWS Resources drift as in AWS.
* The Classic tier "launches" in about 45 s (12–20 min in AWS) and serves a simulated gate board.
* Simulator, worker and janitor run as background threads; the terminal shows each AWS call that
  *would* have been made (`[LOCAL] kms.disable_key …`).
* **No Datadog signals.** Metrics and events are printed as log lines instead.
* State is in memory and resets when you stop the server (Ctrl+C).

Options: `--port 8094`, `--sim-interval 15` (simulator every 15 s instead of 60).
Full guide, per-fault local behaviour and guided tours: **[docs/LOCAL_MODE.md](docs/LOCAL_MODE.md)**.

### Deploy to AWS

**Full guide: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**, which starts with a table of
installation scenarios (local only, with or without Datadog, Classic tier, existing Datadog
setups, several copies, upgrading from v2, CI). You deploy only the core stack; the Classic tier
is launched later from the app.

```bash
ADMIN_PASSWORD='Choose-A-Long-One!' BUDGET_EMAIL=you@example.com ./scripts/deploy-cfn.sh
```
```powershell
./scripts/deploy-cfn.ps1 -AdminPassword 'Choose-A-Long-One!' -BudgetEmail you@example.com -DatadogApiKey $env:DD_API_KEY
```

The stack name must be **24 characters or fewer**, because the Classic ALB is named `<stack>-classic`.
The template is larger than CloudFormation's 51 KB inline limit, so the scripts upload it through
the artifact bucket. The console upload does the same automatically.

### Launch the Classic tier

1. Chaos Control → **Classic Tier** card → choose **Minimal** or **Full** and an auto-shutdown time (1–8 h).
2. **Launch**. Progress and CloudFormation events show live. It takes about 12–15 minutes, mostly RDS.
3. Use the **Gates** page and the **Classic** and **FIS** fault tiers. Use **＋1 h** to extend (8 h maximum).
4. **Tear down**, or let it expire. A one-time EventBridge Scheduler job deletes the stack at expiry
   even if the app is broken; the janitor is a second backstop.

From a terminal: `python scripts/classic.py up --url https://dxxxx.cloudfront.net --size full --hours 2`.

### Something is stuck?

```bash
python scripts/restore.py --stack avionpulse --region us-east-1 --classic-down
```

This puts every core resource back to its baseline with your own AWS CLI credentials and deletes
the Classic stack. It doesn't need the app.

### Tear down everything

```bash
./scripts/teardown.sh        # or scripts\teardown.ps1 — deletes the Classic stack first, then the core
```

## Cost

| | Free tier active | No free tier |
|---|---|---|
| **Core stack**, typical demo month | **≈ $1.10 – $1.50** (KMS key is $1) | **≈ $5** |
| **Classic tier**, only while launched | Minimal **≈ $0.07/h** · Full **≈ $0.11/h** | same |
| Classic tier, 40 demo hours/month | ≈ $3 – $4.50 | same |

Full breakdown: **[docs/COST.md](docs/COST.md)**.

## Chaos faults (49)

| Tier | Faults |
|---|---|
| Edge | `api_throttling`†, `latency_spike`, `random_500s`, `health_check_fail` |
| Compute | `lambda_timeout`, `lambda_oom`, `memory_leak`, `cpu_burn`, `cold_start_storm`†, `worker_throttle`† |
| Data | `dynamodb_throttle`, `full_table_scan`, `n_plus_one`, `iam_access_denied`, `s3_missing_object` |
| Async | `sqs_poison_pill`, `queue_backlog`†, `simulator_crash`, `downstream_timeout` |
| Business | `silent_failure`, `pax_corruption`, `log_flood` |
| **AWS control plane** | `dynamodb_capacity_cut`†, `eventbridge_rule_disabled`†, `api_integration_broken`†, `lambda_bad_deploy`†, `ssm_config_poisoned`†, `sqs_retention_cut`† |
| **Security & IAM** | `iam_policy_detached`†, `kms_key_disabled`†, `sqs_send_denied`†, `s3_bucket_policy_deny`† |
| **Integration** | `eb_target_broken`†, `sns_delivery_failure`†, `stepfn_task_failure`, `stream_processor_poison` |
| **Classic tier** | `classic_sg_lockdown`†, `classic_nacl_db_block`†, `classic_asg_zero`†, `classic_alb_maintenance`†, `classic_rds_reboot`†, `classic_db_slow_query` |
| **AWS FIS** | `fis_cpu_stress`†, `fis_network_latency`†, `fis_instance_terminate`†, `fis_az_outage`† (Full size only) |
| Composite | `cascading_failure`, `security_incident`, `control_plane_storm` |

† changes a real AWS resource and puts it back when switched off or when its TTL expires.

* **[docs/FAULT_SCENARIOS.md](docs/FAULT_SCENARIOS.md)**: what each fault does, the Datadog signal, and demo storylines.
* **[docs/AWS_RUNBOOKS.md](docs/AWS_RUNBOOKS.md)**: AWS scenarios the app deliberately can't trigger
  (AWS Health events, CloudTrail tampering, root sign-in, quotas, cost anomalies) as step-by-step manual runbooks.
* **[docs/DATADOG_SETUP.md](docs/DATADOG_SETUP.md)**: instrumentation, CloudTrail / Cloud SIEM, and per-fault monitors.

## Documentation

| Guide | Read it when you want to… |
|---|---|
| [docs/LOCAL_MODE.md](docs/LOCAL_MODE.md) | Try everything on your own machine with no AWS account; understand what's simulated |
| [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) | Understand the architecture, request path, chaos engine, Classic lifecycle, IAM model and telemetry; add a new fault |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Deploy to AWS: installation scenarios (S1–S9), console and CLI paths, parameters, verification, day-2, teardown, troubleshooting |
| [docs/DATADOG_SETUP.md](docs/DATADOG_SETUP.md) | Instrument, connect the AWS integration and CloudTrail, create a monitor for each of the 49 faults, import the dashboard |
| [docs/FAULT_SCENARIOS.md](docs/FAULT_SCENARIOS.md) | Know what each fault does and the signal it produces; run a demo storyline |
| [docs/AWS_RUNBOOKS.md](docs/AWS_RUNBOOKS.md) | Rehearse AWS failures the app deliberately can't trigger (Health events, CloudTrail, GuardDuty, quotas…) |
| [docs/COST.md](docs/COST.md) | See what the core and Classic tiers cost and what keeps them cheap |
| [docs/datadog/avionpulse-dashboard.json](docs/datadog/avionpulse-dashboard.json) | Import the *AvionPulse v3 — Cloud Health* dashboard |

## Repository layout

```
cloudformation/
  avionpulse.yaml      Core stack (always on): web, app, data, integration tiers + Classic/FIS support
  classic.yaml         Classic stack (on demand): VPC, ALB, EC2 ASG, RDS — launched by the api function
src/
  api.py               API Lambda: routing, auth, request-path faults, Classic + drift routes
  chaos.py             Fault catalog (49), shared state, infra dispatch, TTL janitor logic
  infra.py             AWS control-plane calls for every serverless infra fault + drift read-back
  classic.py           Classic stack lifecycle, auto-shutdown schedule, Classic/FIS faults, gate proxy
  janitor.py           Scheduled janitor Lambda (own rule — no fault can disable it)
  processor.py         Integration processor: Streams, SNS, EventBridge, Step Functions tasks
  simulator.py         Flight simulator, business metrics, feeds every integration channel
  worker.py            SQS consumer with partial batch failures
  classic_tier/
    gate_service.py    Gate service the Classic EC2 instances run (PostgreSQL, ddtrace)
  seed.py · site_deployer.py · cfn.py · store.py · flightsim.py · auth.py · telemetry.py
web/                   SPA (index.html, static/css, static/js), data/notams.json
scripts/
  deploy-cfn.sh/.ps1   Deploy the core stack
  teardown.sh/.ps1     Delete the Classic stack (if any), then the core
  classic.py           Launch / status / tear down the Classic tier from a terminal
  restore.py           Break-glass: restore every core resource with the AWS CLI
  package.py · local_server.py · loadgen.py
docs/                  LOCAL_MODE, HOW_IT_WORKS, DEPLOYMENT, DATADOG_SETUP, FAULT_SCENARIOS,
                       AWS_RUNBOOKS, COST, datadog/avionpulse-dashboard.json
```

## Safety rails

* **Every fault has a TTL.** The janitor runs every 2 minutes on its own EventBridge rule, so
  disabling the simulator's rule can't stop cleanup.
* **Least-privilege chaos.** The app role can only change resources of its own stack: named
  ARNs, `avionpulse:classic=<stack>` tags, and `iam:PolicyARN` conditions. It can't create EC2 or
  RDS itself. Only CloudFormation can, through a dedicated service role, and only for the stack
  named `<stack>-classic`.
* **No fault deletes data or resources** (except the Classic stack teardown you ask for).
* **Control routes survive.** Login, Chaos, Classic and AWS-Resources routes use their own API
  Gateway integration and throttle.
* **The Classic tier always ends.** One stack at a time, 8 h maximum, a Scheduler job at expiry,
  a janitor backstop, and `restore.py --classic-down`.

## Notes

* Infra faults create CloudFormation drift on purpose. RESET ALL, the janitor, `restore.py` or the
  next deploy puts everything back.
* `kms_key_disabled` takes up to about a minute to bite, because SQS caches data keys for 60 s.
* `dynamodb_capacity_cut`: AWS limits how often a table's capacity can be *decreased* each day.
  Past that limit, the toggle reports AWS's error instead of pretending it worked.
* FIS network latency targets interface `ens5` (Amazon Linux 2023 on Graviton).
* All data is fictional. Tail numbers use the `N-AVP` prefix.
