# AvionPulse v3 — Local Mode (no AWS account)

Local mode runs the whole application on your own computer, **with no AWS account, no credentials
and no cost**. Every AWS service is replaced by an in-memory stand-in, so you can click through every
page, switch on all 50 chaos faults, and launch and tear down the Classic tier, all in a few seconds.

Use it to:

* review the UI, the fault catalog and the Classic tier launch flow before deploying anything;
* rehearse a demo storyline;
* develop and test changes to the code (it runs the real handlers, not a mock-up).

What it can't do is show you **Datadog**. Nothing leaves your machine, so there are no traces,
AWS metrics, CloudTrail events or hosts. For that you need a real deployment
([DEPLOYMENT.md](DEPLOYMENT.md)).

**Contents**

1. [Requirements](#1-requirements)
2. [Start it](#2-start-it)
3. [What's running](#3-whats-running)
4. [How AWS is replaced](#4-how-aws-is-replaced)
5. [How each fault behaves locally](#5-how-each-fault-behaves-locally)
6. [The Classic tier in local mode](#6-the-classic-tier-in-local-mode)
7. [Reading the console output](#7-reading-the-console-output)
8. [Guided tours](#8-guided-tours)
9. [Generating traffic](#9-generating-traffic)
10. [Limitations: local vs. AWS](#10-limitations-local-vs-aws)
11. [Troubleshooting](#11-troubleshooting)
12. [For developers: how local mode is wired](#12-for-developers-how-local-mode-is-wired)

---

## 1. Requirements

| Need | Notes |
|---|---|
| Python **3.9 or newer** | `python3 --version` (macOS/Linux) or `python --version` (Windows) |
| A web browser | Any modern browser |
| Nothing else | Standard library only: no `pip install`, no AWS CLI, no Docker, no database |

It works the same on macOS, Linux and Windows.

---

## 2. Start it

From the `AvionDash_AWS_3` folder:

```bash
python3 scripts/local_server.py
```

On Windows:

```powershell
python scripts\local_server.py
```

You should see:

```text
AvionPulse local -> http://localhost:8080  (admin / aviondash123)
```

Open **http://localhost:8080** and sign in with one of these demo users. The login page doesn't
list them, in local mode or on a deployed site; they're documented here and in the
[README](../README.md#users-and-roles).

| User | Password | Sees | Can do |
|---|---|---|---|
| `admin` | `aviondash123` | Operations + Platform pages | Everything: chaos faults, Classic tier launch/teardown |
| `operator` | `operator123` | Operations + Platform pages (read only) | Raise maintenance requests |
| `viewer` | `viewer123` | **Operations pages only** (no AWS Resources, Architecture or Chaos) | Read only |

The **public status page** is at **http://localhost:8080/status** (no login; JSON at
`/api/status`). The *Status Page 500* fault makes both return HTTP 500 locally too.

**Options**

| Option | Default | Meaning |
|---|---|---|
| `--port N` | `8080` | Port to listen on (use another if 8080 is taken) |
| `--sim-interval S` | `60` | Seconds between simulator runs (AWS runs it every 5 minutes; lower it to see things change faster) |

```bash
python3 scripts/local_server.py --port 8093 --sim-interval 15
```

The server listens on **127.0.0.1 only**, so other machines on your network can't reach it.

**Stop it** with **Ctrl+C**. All state lives in memory and is lost when the server stops. The next
start reseeds fresh data, with no faults on and the Classic tier offline.

---

## 3. What's running

`local_server.py` plays the part of CloudFront, API Gateway, EventBridge and the SQS trigger:

| In AWS | Locally | Timing |
|---|---|---|
| CloudFront + S3 serving the SPA | The local HTTP server serves `web/` | — |
| API Gateway → `api` Lambda | `/api/*` requests are turned into API Gateway v2 events and passed to the real `api.handler` | per request |
| API Gateway stage throttle | A token bucket (unlimited until `api_throttling` sets 1 rps / burst 2) | per request |
| EventBridge → `simulator` Lambda | Background thread running `simulator.run()` | every `--sim-interval` s (default 60) |
| EventBridge → `janitor` Lambda | Background thread running `janitor.run()` | every 20 s |
| SQS event source mapping → `worker` | Background thread draining the fake queue in batches of 10 (DLQ after 3 receives) | every 2 s |
| CloudFormation `SeedData` custom resource | `seed.seed_all()` on start-up | once |

On start-up it seeds the in-memory table (airports, aircraft, users, flights), runs the simulator
once so the dashboard has data immediately, then starts the three background threads.

---

## 4. How AWS is replaced

Everything AWS-related goes through two modules, `store.py` (DynamoDB) and `infra.py` (everything
else), plus `classic.py` for the Classic stack. Each one checks `config.LOCAL`, which is set by the
environment variable `AVIONPULSE_LOCAL=1`. `local_server.py` sets that variable for you.

| AWS service | Local stand-in | Where |
|---|---|---|
| DynamoDB table | `FakeStore`, a thread-safe dict keyed by (PK, SK) | `store.py` |
| DynamoDB Streams → `processor` | After each simulator write, the FLIGHT items are passed to `processor.handler` as a stream batch | `infra.local_stream` |
| SQS events queue + DLQ | Two Python lists (`LOCAL_QUEUE`, `LOCAL_DLQ`) | `infra.py` |
| S3 NOTAM object | Read from `web/data/notams.json` on disk | `infra.get_s3_json` |
| S3 flight-data recorder | Keys appended to a list, then an "Object Created" event to `processor` | `infra.put_recorder_snapshot` |
| SNS crew alerts | Direct call to `processor.handler` with an SNS-shaped record | `infra.publish_alert` |
| EventBridge custom bus | Direct call to `processor.handler` per event | `infra.put_bus_events` |
| Step Functions Express workflow | A thread runs the three processor tasks in order and enforces the 5 s CrewCheck timeout | `infra._local_turnaround` |
| SSM feature-flag parameter | A string in `LOCAL_SIM["flags"]` | `infra.get_flags` |
| Control-plane state (stage throttle, concurrency, trigger, capacity, rule state, integration, handler, IAM attachment, KMS key state, queue policy and retention, bucket policy, SNS permission, bus target) | Fields in the `LOCAL_SIM` dict. The app and the local server read them back and behave as AWS would | `infra.LOCAL_SIM` |
| CloudFormation Classic stack, EC2, ALB, RDS, FIS, Scheduler | A fake stack whose status and events advance with time, plus a simulated gate service | `classic.py` (`LOCAL`, `LOCAL_SIM`) |
| Datadog metrics | One log line per metric: `metric <name>=<value> <tags>` | `telemetry.metric` |
| Datadog events | One log line: `[EVENT] <title> — <text>` | `telemetry.event` |

Everything above these stand-ins is the real code: the routes, auth, request-path fault injection,
the chaos engine, the janitor, the simulator, the worker, the processor, and the error mapping
that turns AWS error codes into HTTP responses.

---

## 5. How each fault behaves locally

Most faults look the same locally as in AWS (the same HTTP status, the same error code in the UI,
and the same drift on the AWS Resources page). The **Local behaviour** column says where it differs.

### v2 faults

| Fault | Local behaviour |
|---|---|
| `api_throttling` | Same: the token bucket drops to 1 rps / burst 2 → HTTP 429 |
| `latency_spike`, `cpu_burn`, `random_500s`, `health_check_fail`, `full_table_scan`, `n_plus_one`, `silent_failure`, `pax_corruption`, `log_flood`, `sqs_poison_pill` | Same as AWS (pure code paths) |
| `lambda_timeout` | Sleeps 2 s, then returns 503 `Lambda.Timeout` (no real 10 s kill) |
| `lambda_oom` | Returns 502 `Runtime.OutOfMemory` instead of really allocating 4 GB |
| `memory_leak` | Real allocation, capped at 64 MB so your machine is safe |
| `cold_start_storm` | The env-var change is recorded and shows on AWS Resources; there are no real cold starts |
| `worker_throttle`, `queue_backlog` | Worker thread pauses; queue depth on the Ops Pipeline page climbs |
| `dynamodb_throttle` | ~50% of data requests fail with `ProvisionedThroughputExceededException` → 503 |
| `iam_access_denied` | Simulated `AccessDeniedException` with a realistic ARN message |
| `s3_missing_object` | Real "missing file": `web/data/notams-v2.json` doesn't exist → 502 |
| `simulator_crash` | Simulator thread writes half the flights then raises; data goes stale |
| `downstream_timeout` | Real network timeout: the call to `10.255.255.1` hangs 3 s → 504 |

### v3 faults (serverless)

| Fault | What you see locally |
|---|---|
| `dynamodb_capacity_cut` | ~35% of data reads and writes fail with `ProvisionedThroughputExceededException` (chaos switches are exempt). AWS Resources: `1 / 1` |
| `eventbridge_rule_disabled` | The simulator thread skips its runs; flights freeze; Dashboard *Data freshness* climbs |
| `api_integration_broken` | Every data route returns **500** `{"message": "Internal Server Error"}` (the API Gateway response). Login, Chaos, AWS Resources and the Classic card still work |
| `lambda_bad_deploy` | The worker fails every batch with `Runtime.ImportModuleError` (console); messages go to the DLQ after 3 receives |
| `ssm_config_poisoned` | Dashboard and Flights return **500** `ConfigParseError` within 15 s |
| `sqs_retention_cut` | Worker paused **and** messages older than 60 s vanish. The queue depth stays low while events are lost |
| `iam_policy_detached` | Worker fails every message with `AccessDeniedException … dynamodb:PutItem`; the DLQ fills |
| `kms_key_disabled` | Raising a maintenance request returns **503** `KMS.DisabledException`; the worker stops receiving (immediately; AWS takes up to 60 s) |
| `sqs_send_denied` | Raising a maintenance request returns **500** `AccessDenied … explicit deny` |
| `s3_bucket_policy_deny` | Recorder uploads fail; the **Recorder** channel on Integrations goes STALE |
| `eb_target_broken` | Bus events aren't delivered (counted in `local_counters.bus_failed`); the **FlightStatusChanged** channel goes STALE |
| `sns_delivery_failure` | Crew alerts aren't delivered (`local_counters.sns_failed`); **Crew alerts** goes STALE |
| `stepfn_task_failure` | Turnarounds fail (raise or `States.Timeout` after 5 s); a "last turnaround failure" banner appears on Integrations; **Aircraft turnaround** goes STALE |
| `stream_processor_poison` | Each stream batch is rejected (console error); **Flight changes** goes STALE |
| `security_incident`, `control_plane_storm` | All members at once, as above |

"STALE" appears once a channel hasn't received anything for longer than its expected interval
(400–900 s). With `--sim-interval 15` you'll see it within a few minutes.

---

## 6. The Classic tier in local mode

The Classic card on the Chaos page works exactly as in AWS: choose a size and an auto-shutdown
time, launch, extend, tear down. Only the timings are shorter.

| Step | In AWS | Locally |
|---|---|---|
| Launch → online | 12–20 min | **45 s**, with a progress bar and fake CloudFormation events (VPC, subnets, NACL, security groups, RDS, ALB, ASG…) |
| Teardown → offline | ~10 min | **15 s** |
| Auto-shutdown at expiry | EventBridge Scheduler job (+ janitor backstop) | The janitor thread (every 20 s) tears it down once the expiry passes |
| Cost shown on the card | Real estimate | Same formula (≈ $0.074 / $0.107 per hour), nothing billed |

The **Gates** page then shows a simulated gate board ("served by avionpulse-gates (simulated)").

| Classic fault | Gates page locally |
|---|---|
| `classic_sg_lockdown` | Error: `ALB answered 504: no healthy targets` |
| `classic_asg_zero` | Error: `ALB answered 503: no healthy targets` |
| `classic_alb_maintenance` | Error: `ALB answered 503: Gate service under maintenance` |
| `classic_nacl_db_block` | Waits 3 s, then `ALB answered 500: database connection timed out` |
| `classic_rds_reboot` | Database errors for 60 s, then recovers by itself |
| `classic_db_slow_query` | Every load takes 2.5 s longer |
| `fis_network_latency` | Every load takes about 0.9 s longer |
| `fis_cpu_stress`, `fis_instance_terminate` | Recorded with a fake experiment id; no visible effect (no real hosts) |
| `fis_az_outage` | Only allowed at **Full** size, as in AWS; recorded, no visible effect |

Classic faults are greyed out until the tier is online. Tearing the tier down clears any Classic
faults still on. The AWS Resources page adds four Classic rows (security group rule, NACL rule,
ASG desired capacity, listener-rule action) while the tier is online.

---

## 7. Reading the console output

The terminal running `local_server.py` is your "CloudWatch". Useful prefixes:

| Prefix | Meaning | Example |
|---|---|---|
| `[LOCAL]` | An AWS call that would have been made | `[LOCAL] kms.disable_key local-key` |
| `[CHAOS]` | A fault switched on, off, expired or reset | `[CHAOS] kms_key_disabled ON by admin (ttl 30 min)` |
| `[FAULT]` | A request-path fault firing | `[FAULT] latency_spike sleeping 3.4s on /api/flights` |
| `[EVENT]` | What would be posted to the Datadog Events API | `[EVENT] AvionPulse chaos: kms_key_disabled ON — admin turned on …` |
| `[CLASSIC]` | Classic tier launch / extend / teardown | `[CLASSIC] launch avionpulse-classic size=full hours=2 by admin` |
| `metric` | A custom metric that would go to Datadog | `metric avionpulse.flights.airborne=7 env:local` |
| `Request failed` | A handled 5xx with its AWS error code | `Request failed GET /api/flights -> 500 ConfigParseError: …` |
| `[worker]` / `[simulator]` / `[janitor]` | Background thread problems | `[worker] Runtime.ImportModuleError: No module named 'worker_v2'` |

Every `/api/*` request is also logged with its status code.

---

## 8. Guided tours

Each takes 2–5 minutes. Start with `--sim-interval 15` and sign in as `admin`.

**A. Config drift you can see.** AWS Resources → everything OK. Chaos → turn on
`control_plane_storm`. Back to AWS Resources → four DRIFT rows. Open Dashboard (500
ConfigParseError) and Ops Pipeline (DLQ climbing). Chaos → **RESET ALL** → AWS Resources is all
OK again.

**B. The queue that looks healthy.** Ops Pipeline → raise a few maintenance requests → they appear
as processed. Turn on `sqs_retention_cut`. Raise more: they're accepted (202), but never appear,
and the queue depth stays near zero. The console shows nothing going to the DLQ: the messages just expire.

**C. Integrations going stale.** Integrations → all five FLOWING. Turn on `eb_target_broken` and
`stepfn_task_failure`. Within a few simulator runs, FlightStatusChanged and Aircraft turnaround
turn STALE and the turnaround-failure banner appears.

**D. Classic tier end to end.** Chaos → Classic card → **Full**, 1 hour → **Launch** → watch the
events → ONLINE. Open **Gates** (board loads). Back to Chaos → `classic_nacl_db_block` → Gates
times out after 3 s. Turn it off → Gates loads. **＋1 h**, then **Tear down** → OFFLINE, with
"Last session" and its cost on the card.

**E. The auto-revert.** Set the TTL picker to **5 min**, turn on `eventbridge_rule_disabled`, and
wait. The janitor reverts it (`[CHAOS] eventbridge_rule_disabled expired — auto-reverted`) and
the simulator resumes.

---

## 9. Generating traffic

`loadgen.py` works against the local server too:

```bash
python3 scripts/loadgen.py http://localhost:8080 --rps 2 --minutes 5
```

The CLI helper for the Classic tier also works locally:

```bash
python3 scripts/classic.py up --url http://localhost:8080 --size minimal --hours 1 --wait
```
```bash
python3 scripts/classic.py down --url http://localhost:8080
```

---

## 10. Limitations: local vs. AWS

| Area | Local mode | Real AWS |
|---|---|---|
| Datadog (APM, logs, metrics, CloudTrail, hosts, SIEM) | **None**; metrics and events are log lines | Everything in [DATADOG_SETUP.md](DATADOG_SETUP.md) |
| IAM | Never evaluated; a permission mistake in the template won't show | Real allow/deny decisions |
| Timing | Instant control-plane changes; 45 s Classic launch | Eventual consistency, KMS key cache (≤ 60 s), CloudWatch metrics 5–15 min late, 12–20 min Classic launch |
| Lambda platform | No cold starts, timeouts or OOM kills (simulated as responses) | Real `Task timed out`, `Runtime.OutOfMemory`, init durations |
| DynamoDB capacity | Throttling is simulated by probability | Real RCU/WCU accounting and burst credits |
| Classic tier | Simulated gate board; FIS CPU / terminate / AZ have no visible effect | Real EC2, ALB, RDS, FIS experiments, Datadog Agent hosts |
| Persistence | Lost on restart | DynamoDB, S3, CloudFormation |
| Multi-user | Single process on your machine | Shared site on CloudFront |

---

## 11. Troubleshooting

| Problem | Fix |
|---|---|
| `OSError: [Errno 48] Address already in use` | Another program (or a previous run) has the port. Use `--port 8094`, or stop the other process |
| `python3: command not found` (Windows) | Use `python` |
| Garbled characters in the Windows console | The server already switches the console to UTF-8; use Windows Terminal rather than the legacy console if it persists |
| Login fails after restarting the server | Your browser still holds a token from the previous run. Sign out, or reload the page |
| Page looks out of date after editing `web/` | Hard-refresh (Ctrl+Shift+R / Cmd+Shift+R); the server sends `no-cache`, but the browser may hold the old file |
| Dashboard shows errors right after start | A fault from your last session is not the cause (state is fresh). Check the console for a `Traceback` |
| Classic card says "no Classic tier support" | Not possible locally; if you see it, `AVIONPULSE_LOCAL` wasn't set. Start through `scripts/local_server.py` |

---

## 12. For developers: how local mode is wired

* `scripts/local_server.py` sets `AVIONPULSE_LOCAL=1` and `LOCAL_WEB_ROOT`, puts `src/` on the
  import path, then imports the real handlers.
* `config.LOCAL` is the only switch. Every AWS call site has an `if config.LOCAL:` branch next to
  the boto3 call, so the two paths sit side by side. Search the code for `config.LOCAL` to see them all.
* `infra.LOCAL_SIM` and `classic.LOCAL_SIM` hold the pretend AWS state; `infra.drift()` and
  `classic.drift_rows()` read the same fields, so the AWS Resources page works locally.
* **Adding a new fault?** Implement the AWS call in `infra.py` (or `classic.py`) with a local
  branch that updates `LOCAL_SIM`. Make the app or `local_server.py` react to that field, and add
  a `DRIFT_CHECKS` row. Then switch the fault on and off locally and confirm the AWS Resources row
  goes DRIFT → OK. See [HOW_IT_WORKS.md §12](HOW_IT_WORKS.md#12-adding-a-new-fault).
* Quick API check without a browser:

  ```bash
  B=http://localhost:8080/api
  ```
  ```bash
  T=$(curl -s -XPOST $B/auth/token -H 'content-type: application/json' -d '{"username":"admin","password":"aviondash123"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
  ```
  ```bash
  curl -s -XPOST $B/chaos/kms_key_disabled/toggle -H "X-Avion-Token: $T" -H 'content-type: application/json' -d '{"enabled":true,"ttl_minutes":5}'
  ```
  ```bash
  curl -s $B/aws/drift -H "X-Avion-Token: $T"
  ```
