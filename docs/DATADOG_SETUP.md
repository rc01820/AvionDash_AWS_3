# AvionPulse v3 — Datadog Setup

This guide takes you from a plain AvionPulse deployment to a fully monitored one. That covers the
application **and the AWS infrastructure under it**.

1. [Lambda instrumentation](#1-lambda-instrumentation) — traces, logs, enhanced and custom metrics
2. [AWS integration](#2-aws-integration) — CloudWatch metrics for every AWS service AvionPulse uses
3. [RUM](#3-rum-optional) (optional)
4. [Reference: names, tags, metrics and log lines](#4-reference-names-tags-metrics-log-lines)
5. [Creating monitors — the common steps](#5-creating-monitors--the-common-steps)
6. [One monitor per fault](#6-one-monitor-per-fault) — **all 49 chaos faults**, with exact queries and test steps
   * 6.1–6.6 the v2 serverless faults · 6.7 before the v3 monitors · 6.8 AWS control plane ·
     6.9 Security & IAM · 6.10 Integration · 6.11 v3 composites · 6.12 Classic tier · 6.13 AWS FIS ·
     6.14 summary table
7. [The AvionPulse Health dashboard](#7-the-avionpulse-health-dashboard) — import JSON or build by hand
8. [Verifying everything end to end](#8-verifying-everything-end-to-end)
9. [v3: AWS infrastructure monitoring](#9-v3-aws-infrastructure-monitoring) — integration scope, CloudTrail,
   Cloud SIEM, chaos events, Classic tier Agent

AvionPulse runs without Datadog (and entirely without AWS in [local mode](LOCAL_MODE.md), which has
no Datadog signals at all). Everything below assumes you deployed **with** a Datadog API key.
New to the app? Read [HOW_IT_WORKS.md](HOW_IT_WORKS.md) §10 first for how each signal reaches Datadog.

---

## 1. Lambda instrumentation

```powershell
./scripts/deploy-cfn.ps1 -DatadogApiKey <DD_API_KEY> -DatadogSite datadoghq.com -DatadogPythonLayerVersion <n> -DatadogExtensionLayerVersion <n>
```
```bash
DD_API_KEY=<key> DD_SITE=datadoghq.com DD_PY_LAYER=<n> DD_EXT_LAYER=<n> ./scripts/deploy-cfn.sh
```

What the script and template do:

| Piece | How |
|---|---|
| API key | Stored as SSM **SecureString** `/<stack>/datadog/api-key` (free); the extension reads it via `DD_API_KEY_SSM_NAME`. The key is never in the template or env vars. The Classic EC2 instances read the same parameter for the Agent, and the api function reads it to post chaos events. |
| Layers | `Datadog-Python312-ARM` + `Datadog-Extension-ARM` from account `464622532012`. |
| Handler | Switched to `datadog_lambda.handler.handler`; the real handler is in `DD_LAMBDA_HANDLER`. |
| Tags | `DD_SERVICE=avionpulse`, `DD_ENV`, `DD_VERSION`, `DD_TAGS=app:avionpulse,suite:aviondash,stack:<stack>,tier:serverless` |
| Instrumented functions | `api`, `worker`, `simulator`, `janitor`, `processor`. (`seed` and `site-deployer` are deploy-time custom resources and run without the layer.) |
| Traces | `DD_TRACE_ENABLED`, `DD_COLD_START_TRACING`; botocore is auto-instrumented, so every DynamoDB / SQS / S3 / Lambda / API Gateway call is its own span. |
| Logs | `DD_SERVERLESS_LOGS_ENABLED` — the extension ships logs directly (no Forwarder Lambda). Lambda logs are JSON (`LoggingConfig: JSON`), with trace IDs injected. |
| Enhanced metrics | `DD_ENHANCED_METRICS` — near-real-time `aws.lambda.enhanced.*` (timeouts, OOM, init duration, cold starts, cost). |
| Custom metrics | `datadog_lambda.metric.lambda_metric` through the extension — no CloudWatch custom-metric charges. |

**Layer versions.** The defaults (`107` / `80`) are placeholders. Check the current versions
in Datadog's *Serverless → AWS Lambda → Python* install docs, then pass
`-DatadogPythonLayerVersion` / `-DatadogExtensionLayerVersion` (or `DD_PY_LAYER` / `DD_EXT_LAYER`).
A version that doesn't exist fails the deploy with a "layer version not found" error.

**Check it worked.** Open **Infrastructure → Serverless**. Within a few minutes of browsing the site
you should see `avionpulse-api`, `avionpulse-worker`, `avionpulse-simulator`, `avionpulse-janitor`
and `avionpulse-processor` with traces (`avionpulse-seed` and `avionpulse-site-deployer` also
appear once the AWS integration is connected, without traces). Click `avionpulse-api` → the
*Traces* tab should show invocations with DynamoDB child spans. `avionpulse-processor` traces
show four kinds of trigger: DynamoDB stream, SNS, EventBridge and Step Functions.

## 2. AWS integration

Lambda metrics come from the extension; API Gateway, DynamoDB, SQS, CloudFront and the
integration-level Lambda metrics (`aws.lambda.throttles`) need the Datadog AWS integration.

1. **Integrations → Amazon Web Services → Add AWS Account → Automatically using CloudFormation.**
   Pick the region you deployed to and launch the quick-create stack in the AWS console.
2. When the account shows as connected, open it and go to **Metric Collection**:
   * Untick every namespace except **Lambda, ApiGateway, DynamoDB, SQS, CloudFront, Events** — and
     for v3 also **SNS, States (Step Functions), ApplicationELB, EC2, AutoScaling, RDS** (see §9).
   * Under *Limit metric collection by tag*, add `app:avionpulse` (the template tags every
     resource with it).
   * This keeps the CloudWatch `GetMetricData` bill (paid to AWS, $0.01 per 1,000 metrics) to cents.
3. **Log Collection** — leave off. The Lambda Extension already ships Lambda logs; the API Gateway
   access-log group is optional.
4. CloudFront metrics only exist in **us-east-1** regardless of where you deployed.

> **Latency of integration metrics.** `aws.apigateway.*`, `aws.dynamodb.*`, `aws.sqs.*` and
> `aws.lambda.throttles` arrive by polling CloudWatch, typically **5–15 minutes late**.
> `aws.lambda.enhanced.*`, custom `avionpulse.*` metrics, traces and logs arrive within about a minute.
> The monitor windows below are sized for that.

## 3. RUM (optional)

1. **Digital Experience → Add an Application → JS**, name it `avionpulse-web`.
2. Copy the *Application ID* and *Client Token*, then redeploy:

```powershell
./scripts/deploy-cfn.ps1 -DatadogApiKey <key> -RumApplicationId <app-id> -RumClientToken <pub-token>
```

`config.js` is generated at deploy time. RUM uses `allowedTracingUrls: [<site>/api]`, so browser
sessions link to the Lambda traces (CloudFront forwards the `x-datadog-*` headers to API Gateway).

---

## 4. Reference: names, tags, metrics, log lines

### Resource names

All names derive from the stack name. With the default stack name `avionpulse`:

| Resource | Name / tag value | Used in queries as |
|---|---|---|
| API Lambda | `avionpulse-api` | `functionname:avionpulse-api` |
| Worker Lambda | `avionpulse-worker` | `functionname:avionpulse-worker` |
| Simulator Lambda | `avionpulse-simulator` | `functionname:avionpulse-simulator` |
| DynamoDB table | `avionpulse-data` | `tablename:avionpulse-data` |
| Events queue | `avionpulse-events` | `queuename:avionpulse-events` |
| Dead-letter queue | `avionpulse-events-dlq` | `queuename:avionpulse-events-dlq` |
| HTTP API | stack output `ApiId` | `apiid:<ApiId>` |
| Janitor Lambda | `avionpulse-janitor` | `functionname:avionpulse-janitor` |
| Processor Lambda | `avionpulse-processor` | `functionname:avionpulse-processor` |
| Simulator schedule rule | `avionpulse-simulator-tick` | `rulename:avionpulse-simulator-tick` |
| Event bus / rule | `avionpulse-flight-ops` / `avionpulse-flight-status` | `eventbusname:avionpulse-flight-ops`, `rulename:avionpulse-flight-status` |
| SNS topic | `avionpulse-crew-alerts` | `topicname:avionpulse-crew-alerts` |
| State machine | `avionpulse-turnaround` (Express) | `statemachinearn:*avionpulse-turnaround*` |
| KMS key | alias `alias/avionpulse-events` (stack output `EventsKeyId`) | CloudTrail `@requestParameters.keyId` |
| Feature flags | SSM `/avionpulse/config/flags` | CloudTrail `@requestParameters.name` |
| Recorder bucket | stack output `RecorderBucketName` | `bucketname:<name>` |
| Worker role / data policy | stack outputs `WorkerRoleName`, `WorkerDataPolicyArn` | CloudTrail `@requestParameters.roleName` |
| Classic ALB | `avionpulse-classic` (new id each launch) | `loadbalancer:app/avionpulse-classic/*` |
| Classic ASG | `avionpulse-classic-gates` | `autoscalinggroupname:avionpulse-classic-gates` |
| Classic EC2, ALB, RDS | resource tags | `tier:classic`, `avionpulse:classic:avionpulse` |
| Classic APM service | from `DD_SERVICE` on EC2 | `service:avionpulse-gates`; postgres spans `postgres.query` |
| Everything | resource tag | `app:avionpulse` |
| Service / env | from `DD_SERVICE` / `DD_ENV` | `service:avionpulse`, `env:demo` |

Get the API id with:

```bash
aws cloudformation describe-stacks --stack-name avionpulse --query "Stacks[0].Outputs[?OutputKey=='ApiId'].OutputValue" --output text
```

**Using a different stack name or env?** Replace `avionpulse-` and `env:demo` everywhere below.

### Custom metrics

`lambda_metric` submits everything as Datadog **distribution** metrics. Query gauges with `avg:`
and counts with `sum:…as_count()`. Every custom metric also carries `env:<AppEnv>`. Without the
layer, each metric is written to the logs as a JSON line (`metric_name`, `metric_value`,
`metric_tags`) instead.

| Metric | Read as | Tags | Emitted by |
|---|---|---|---|
| `avionpulse.flights.airborne` | gauge | env | simulator (every 5 min) |
| `avionpulse.flights.delayed` / `.cancelled` / `.total` | gauge | env | simulator |
| `avionpulse.flights.on_time_pct` | gauge | env | simulator |
| `avionpulse.pax.airborne` | gauge | env | simulator |
| `avionpulse.fleet.in_maintenance` | gauge | env | simulator |
| `avionpulse.api.requests` | count | route, method, status_class, cold_start | api |
| `avionpulse.api.latency_ms` | distribution | route, method, cold_start | api |
| `avionpulse.api.flights_returned` | gauge | env | api |
| `avionpulse.data.staleness_s` | gauge | env | api (dashboard summary) |
| `avionpulse.auth.login` | count | result, role | api |
| `avionpulse.events.submitted` / `.processed` / `.poison` | count | type, severity | api / worker |
| `avionpulse.queue.processing_lag_s` | distribution | type | worker |
| `avionpulse.chaos.toggle` | count | fault, enabled | api |
| `avionpulse.integration.received` | count | channel (`stream`, `bus`, `sns`, `turnaround`, `recorder`) | processor |
| `avionpulse.integration.errors` | count | channel, error | simulator (a producer call failed) |
| `avionpulse.classic.launch` | count | size | api |

**Enable percentiles** for `avionpulse.api.latency_ms`: *Metrics → Summary →
avionpulse.api.latency_ms → Advanced → Enable percentiles*. Needed for the p95 queries below.

### Log lines the monitors key on

These strings come straight from the code, so log monitors can match them exactly.

| Log text (search term) | Where | Meaning |
|---|---|---|
| `"Request failed"` | api | Any handled 5xx. Format: `Request failed GET /api/notams -> 502 NoSuchKey: …` |
| `ProvisionedThroughputExceededException` | api | DynamoDB throttled |
| `AccessDeniedException` / `"is not authorized to perform"` | api | IAM denial |
| `NoSuchKey` | api | S3 object missing |
| `UpstreamTimeout` | api | Third-party call timed out |
| `"unhandled exception escaping handler"` | api | Raised on purpose; also logged by the Lambda runtime as an `[ERROR]` |
| `"Task timed out"` | Lambda platform | Function hit its 10 s timeout |
| `"Poison message"` | worker | SQS message failed validation |
| `SimulatorCrash` | Lambda runtime | Scheduled simulator raised |
| `"Failed login for"` | api | Bad password |
| `[CHAOS]` | api, janitor | Fault switched on/off/expired (handy for event overlays) |
| `[EVENT]` | api, janitor | Same text that is posted to the Datadog Events API |
| `[CLASSIC]` | api, janitor | Classic tier launch / extend / teardown |
| `"simulator: <channel> failed"` | simulator | An integration producer failed (`sqs`, `bus`, `sns`, `turnaround`, `recorder`) |
| `ConfigParseError` | api | Feature-flag parameter is invalid JSON |
| `KMS.DisabledException` | api, simulator | Queue key disabled |
| `"explicit deny"` | api, simulator | A resource policy (queue or bucket) denied the call |
| `"Runtime.ImportModuleError"` / `"No module named 'worker_v2'"` | Lambda platform (worker) | Bad worker deploy |
| `"[FAULT] stream_processor_poison"` | processor | Stream batch rejected |
| `"[FAULT] stepfn_task_failure"` | processor | Crew-check task failing |
| `"gate board failed"` | EC2 gate service (`service:avionpulse-gates`) | Database error on the Classic tier |
| `ALB503` / `ALB504` / `ALB500` / `GateServiceTimeout` | api (`error_code`) | Classic tier unreachable or failing, as seen from Lambda |

### Span-based DynamoDB call metric (used by two monitors)

There's no CloudWatch metric that counts DynamoDB calls by operation per request, so generate one
from APM spans:

1. **APM → Setup & Configuration → Generate Metrics → New Metric**.
2. Open any `avionpulse-api` trace first and click a DynamoDB child span. Note the attribute that
   holds the operation (normally `aws.operation`, value `Query`, `Scan`, `GetItem`…) and the
   service shown for the span.
3. Fill in:
   * **Metric name:** `avionpulse.ddb.calls`
   * **Query:** `env:demo @aws.operation:*` and, if you want to be strict, the DynamoDB span
     service you noted (e.g. `service:aws.dynamodb`).
   * **Group by:** `@aws.operation`
   * **Aggregation:** count
4. Save. The metric fills in from new spans only (not retroactively).

---

## 5. Creating monitors — the common steps

Every monitor in section 6 follows these steps; only the query, thresholds and message change.

1. **Monitors → New Monitor**, then choose the type listed for the fault (*Metric*, *Logs*,
   *Anomaly*, *Forecast*, *Composite* or *Synthetic*).
2. **Define the metric / search.** Click **Edit** (the `</>` source toggle) next to the query and
   paste the query from section 6. Datadog fills in the form for you. For log monitors, paste the
   search into the search bar and set *count of logs*.
3. **Set alert conditions.** Enter the *Alert* and *Warning* thresholds given. Leave
   *Require a full window of data* **off** for integration metrics (they arrive late and patchy).
4. **Missing data.** Default is *Do not notify* unless the fault says otherwise.
5. **Notify your team.**
   * **Name:** use the name given, e.g. `[AvionPulse] DynamoDB read throttling`.
   * **Message:** start from the template below, then paste the fault-specific runbook line.
   * **Recipients:** `@your-email` or `@slack-channel`.
   * **Renotify:** every 30 min while alerting (optional).
6. **Tags:** add `app:avionpulse`, `service:avionpulse`, `env:demo`, and `fault:<fault_name>`.
   The dashboard's monitor-summary widget filters on `app:avionpulse`, and `fault:` lets you see
   which chaos switch each monitor proves.
7. **Priority:** P1 for *critical* faults, P3 for *warning* faults (matches the chaos page badges).
8. **Create.**

**Message template**

```text
{{#is_alert}}
AvionPulse: {{monitor_name}} is ALERTING ({{value}}).
{{/is_alert}}
{{#is_warning}}
AvionPulse: {{monitor_name}} is in WARNING ({{value}}).
{{/is_warning}}
{{#is_recovery}}
AvionPulse: {{monitor_name}} recovered.
{{/is_recovery}}

Runbook: <fault-specific line from section 6>
Chaos panel: https://<your-cloudfront-domain>/#chaos (check for an active fault first)
Dashboard: AvionPulse v3 — Cloud Health

@your-email
```

---

## 6. One monitor per fault

Each entry gives the monitor that detects the fault, the exact settings, and how to prove it
fires. **Test procedure for all of them:** sign in as `admin`, open **Chaos Control**, pick a TTL
of 15–30 minutes, switch the fault on, generate traffic (browse the pages, or run
`python scripts/loadgen.py https://<site> --rps 1 --minutes 10`), wait the time stated, confirm
the monitor alerts, then switch the fault off and confirm it recovers.

> Times to alert assume the windows below. Enhanced, custom and log signals take about 1–3 minutes;
> CloudWatch-polled signals take about 10–20 minutes.

### 6.1 Edge tier

#### `api_throttling` — API Gateway throttling
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] API Gateway throttling (429s)`
* **Query:**
  ```
  sum(last_10m):sum:aws.apigateway.4xx{apiid:<ApiId>}.as_count() > 20
  ```
* **Thresholds:** Alert `> 20`, Warning `> 5`
* **Make it smarter (optional composite):** create a second monitor
  `[AvionPulse] API Lambda invocations dropped`,
  `pct_change(avg(last_10m),last_1h):sum:aws.lambda.enhanced.invocations{functionname:avionpulse-api}.as_count() < -50`,
  then a **Composite** monitor `A && B`. "Users see errors, but Lambda runs *less*" points straight
  at the edge, not the code.
* **Runbook:** Check the HTTP API `$default` stage throttle. Template default is 20 rps / burst 40.
  Clear the fault or run RESET ALL.
* **Test:** fault on, run loadgen at `--rps 3`. Alerts in about 10–20 min (CloudWatch delay).

#### `latency_spike` — Latency spike
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] API latency p95 high`
* **Query:**
  ```
  avg(last_5m):p95:avionpulse.api.latency_ms{env:demo} > 2000
  ```
  (Needs percentiles enabled — section 4. Without them use
  `avg(last_5m):avg:aws.lambda.enhanced.duration{functionname:avionpulse-api} > 2`, in seconds.)
* **Thresholds:** Alert `> 2000` ms, Warning `> 1000` ms
* **Runbook:** Compare `aws.apigateway.integration_latency` with Lambda duration. If both are high,
  the time is in the function; open a slow trace and look for the long gap before the first span.
* **Test:** fault on, browse. Alerts in about 2–5 min.

#### `random_500s` — Unhandled exceptions
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] API Lambda error rate`
* **Query:**
  ```
  sum(last_10m):sum:aws.lambda.enhanced.errors{functionname:avionpulse-api}.as_count() / sum:aws.lambda.enhanced.invocations{functionname:avionpulse-api}.as_count() * 100 > 10
  ```
* **Thresholds:** Alert `> 10` (%), Warning `> 3`
* **Also useful:** **APM → Error Tracking** groups these as one issue
  (`UnhandledFault: FlightBoardRenderError…`). Turn on *Notify on new issues* for `service:avionpulse`.
* **Runbook:** Error Tracking → open the newest issue → the stack trace names the route.
* **Test:** fault on, refresh the Flights page about 20 times. Alerts in about 2–5 min.

#### `health_check_fail` — Health check failure
* **Type:** Synthetic Monitoring → **API test** (HTTP)
* **Steps:** *Digital Experience → Synthetic Monitoring → New Test → API Test → HTTP*.
  * **URL:** `GET https://<site>/api/health`
  * **Assertions:** `status code is 200`; `body contains "ok"`; `response time is less than 3000 ms`
  * **Locations:** 1–2 managed locations near your region (each extra location adds runs)
  * **Frequency:** every 5 minutes
  * **Alert:** *alert if test fails for 2 minutes from 1 of N locations*, retry once after 30 s
  * **Name:** `[AvionPulse] Health endpoint`, tags as in section 5
* **Runbook:** `/api/health` only checks the function; call `/api/health/deep` to see DynamoDB,
  SQS and S3 individually.
* **Test:** fault on. Alerts on the next run (≤ 5 min).

### 6.2 Compute tier

#### `lambda_timeout` — Lambda timeout
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Lambda timeouts`
* **Query:**
  ```
  sum(last_5m):sum:aws.lambda.enhanced.timeouts{service:avionpulse}.as_count() > 0
  ```
* **Thresholds:** Alert `> 0`
* **Multi-alert:** under *Group by* choose `functionname` so the alert names the function.
* **Runbook:** Logs → `service:avionpulse "Task timed out"`. Duration graph will be pinned at 10 s.
* **Test:** fault on, refresh Flights about 10 times (30% of requests time out). Alerts in about 2 min.

#### `lambda_oom` — Out of memory
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Lambda out of memory`
* **Query:**
  ```
  sum(last_5m):sum:aws.lambda.enhanced.out_of_memory{service:avionpulse}.as_count() > 0
  ```
* **Thresholds:** Alert `> 0`; group by `functionname`
* **Runbook:** Check `aws.lambda.enhanced.max_memory_used` against the 256 MB size. One-off spike
  means a single bad request; a steady climb means a leak (see next).
* **Test:** fault on, refresh Flights about 10 times (25% OOM). Alerts in about 2 min.

#### `memory_leak` — Warm-container memory leak
* **Type:** Forecast
* **Name:** `[AvionPulse] API memory forecast to hit limit`
* **Query:**
  ```
  max(next_1h):forecast(max:aws.lambda.enhanced.max_memory_used{functionname:avionpulse-api}, 'linear', 1) >= 250
  ```
* **Thresholds:** Alert `>= 250` (MB; memory size is 256), Warning `>= 200`
* **Simpler alternative (Metric → Threshold):**
  `max(last_10m):max:aws.lambda.enhanced.max_memory_used{functionname:avionpulse-api} > 180`
* **Runbook:** A sawtooth (climb, OOM, drop) is the signature of state kept between invocations.
  Look for module-level lists or caches.
* **Test:** fault on, run loadgen at `--rps 1` for 10 min. Warning in about 5 min, OOMs follow.

#### `cpu_burn` — CPU burn
* **Type:** Anomaly
* **Name:** `[AvionPulse] API billed duration anomaly`
* **Query:**
  ```
  avg(last_15m):anomalies(avg:aws.lambda.enhanced.billed_duration{functionname:avionpulse-api}, 'agile', 2, direction='above', alert_window='last_5m', interval=60, count_default_zero='true') >= 1
  ```
* **Thresholds:** Alert `>= 1` (100% of the alert window anomalous), Recovery `<= 0`
* **Needs history:** an anomaly monitor needs a few days of normal traffic to learn from. Until
  then use `avg(last_10m):avg:aws.lambda.enhanced.billed_duration{functionname:avionpulse-api} > 0.35`.
* **Runbook:** Check `aws.lambda.enhanced.estimated_cost`. Duration up without more DynamoDB
  time in traces means the code is burning CPU.
* **Test:** fault on, browse for 5 min. Threshold version alerts in about 3 min.

#### `cold_start_storm` — Cold start storm
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Cold start storm`
* **Query:**
  ```
  sum(last_10m):sum:aws.lambda.enhanced.invocations{functionname:avionpulse-api,cold_start:true}.as_count() > 10
  ```
* **Thresholds:** Alert `> 10`, Warning `> 5`
* **Companion monitor (init time):**
  `avg(last_15m):avg:aws.lambda.enhanced.init_duration{functionname:avionpulse-api} > 2`
  (seconds; the fault adds 2.5 s to init).
* **Runbook:** Serverless view → `avionpulse-api` → *Cold starts*. A spike right after a
  configuration change means a deploy or config update recycled the containers.
* **Test:** fault on (it recycles containers immediately and again every simulator run), open
  several pages in a few browser tabs. Alerts in about 2–5 min.

#### `worker_throttle` — Worker concurrency = 0
* **Type:** Metric → Threshold (two monitors, use both)
* **Name 1:** `[AvionPulse] Worker throttled`
  ```
  sum(last_15m):sum:aws.lambda.throttles{functionname:avionpulse-worker}.as_count() > 0
  ```
* **Name 2:** `[AvionPulse] Events queue backing up`
  ```
  max(last_15m):max:aws.sqs.approximate_number_of_messages_visible{queuename:avionpulse-events} > 20
  ```
  Warning `> 10`
* **Why two:** depending on how Lambda's SQS poller backs off, reserved concurrency 0 doesn't
  always produce many throttle datapoints. Queue depth always shows it.
* **Runbook:** `aws lambda get-function-concurrency --function-name avionpulse-worker`. A value of
  0 means someone throttled it. RESET ALL removes the reservation.
* **Test:** fault on; raise 5 maintenance requests on **Ops Pipeline** (as admin or operator).
  Queue monitor alerts in about 15–25 min.

### 6.3 Data tier

#### `dynamodb_throttle` — DynamoDB read storm
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] DynamoDB read throttling`
* **Query:**
  ```
  sum(last_10m):sum:aws.dynamodb.read_throttle_events{tablename:avionpulse-data}.as_count() > 0
  ```
* **Fast companion (Logs):** `[AvionPulse] DynamoDB throttled (logs)`
  * Search: `service:avionpulse ProvisionedThroughputExceededException`
  * Condition: count `> 5` over last 5 minutes
  This alerts in about a minute, before the CloudWatch metric lands.
* **Runbook:** Compare `aws.dynamodb.consumed_read_capacity_units` with
  `aws.dynamodb.provisioned_read_capacity_units`. Look for a Scan-heavy trace.
* **Test:** fault on, refresh Flights repeatedly for about 1 min. Burst credit drains, then 503s
  begin. Log monitor about 2 min; metric monitor about 10–20 min.

#### `full_table_scan` — Scan instead of Query
* **Type:** Metric → Threshold (uses the span-based metric from section 4)
* **Name:** `[AvionPulse] DynamoDB Scan in request path`
* **Query:**
  ```
  sum(last_10m):sum:avionpulse.ddb.calls{@aws.operation:scan}.as_count() > 5
  ```
  (Tag values in generated metrics are lowercased. Check the exact tag in Metrics Explorer.)
* **Without the span metric:** threshold on consumed capacity,
  `sum(last_15m):sum:aws.dynamodb.consumed_read_capacity_units{tablename:avionpulse-data}.as_count() > 3000`.
  Tune it to about 3× your normal level after a day of traffic.
* **Runbook:** Open a recent `GET /api/flights` trace. The DynamoDB child span should say
  `Query`, not `Scan`.
* **Test:** fault on, refresh Flights 10 times. Span metric monitor alerts in about 3 min.

#### `n_plus_one` — N+1 GetItem calls
* **Type:** Metric → Threshold (span-based metric)
* **Name:** `[AvionPulse] DynamoDB N+1 pattern`
* **Query:**
  ```
  sum(last_10m):sum:avionpulse.ddb.calls{@aws.operation:getitem}.as_count() / sum:aws.lambda.enhanced.invocations{functionname:avionpulse-api}.as_count() > 10
  ```
* **Thresholds:** Alert `> 10` GetItem calls per invocation (normal is about 0–1; the fault does about 50)
* **Runbook:** Trace flame graph → dozens of identical `GetItem` spans under one request. Replace
  them with one Query or BatchGetItem.
* **Test:** fault on, refresh Flights 5 times. Alerts in about 3 min.

#### `iam_access_denied` — IAM permission denied
* **Type:** Logs
* **Name:** `[AvionPulse] IAM AccessDenied`
* **Search:**
  ```
  service:avionpulse (AccessDeniedException OR "is not authorized to perform")
  ```
* **Condition:** count `> 0` over the last 5 minutes. Group by `@functionname` if present.
* **Message extra:** add `{{log.message}}` to the body. The log line contains the exact role and
  resource ARN.
* **Runbook:** The message names the action and the resource. Compare with the Lambda role. For
  AvionPulse the missing grant is on `<stack>-data-restricted-ledger` (deliberate).
* **Test:** fault on, open the Dashboard (the summary call fails with 500). Alerts in about 1–2 min.

#### `s3_missing_object` — S3 object missing
* **Type:** Logs
* **Name:** `[AvionPulse] S3 NoSuchKey`
* **Search:**
  ```
  service:avionpulse NoSuchKey
  ```
* **Condition:** count `> 0` over the last 5 minutes
* **Runbook:** The error names the key. `aws s3 ls s3://<bucket>/data/` to confirm what was uploaded;
  redeploying runs `s3 sync` again.
* **Test:** fault on, open **NOTAMs**. Alerts in about 1–2 min.

### 6.4 Async tier

#### `sqs_poison_pill` — Poison messages
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Messages in dead-letter queue`
* **Query:**
  ```
  max(last_15m):max:aws.sqs.approximate_number_of_messages_visible{queuename:avionpulse-events-dlq} > 0
  ```
* **Thresholds:** Alert `> 0`, Warning: none
* **Fast companion (Metric):** `sum(last_5m):sum:avionpulse.events.poison{env:demo}.as_count() > 3`
  catches the worker rejecting messages before they reach the DLQ.
* **Runbook:** Logs → `service:avionpulse "Poison message"`. Inspect a DLQ message with
  `aws sqs receive-message --queue-url <DeadLetterQueueUrl>`. After a fix, redrive it from the
  SQS console (*Start DLQ redrive*).
* **Recovery note:** the DLQ doesn't empty itself. Purge it in the SQS console after testing, or the
  monitor stays red for 4 days (retention).
* **Test:** fault on, browse for 2 min. Poison counter about 2 min; DLQ monitor about 15–25 min
  (3 receives × 60 s visibility, plus CloudWatch delay).

#### `queue_backlog` — Worker trigger paused
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Events queue age`
* **Query:**
  ```
  max(last_15m):max:aws.sqs.approximate_age_of_oldest_message{queuename:avionpulse-events} > 600
  ```
* **Thresholds:** Alert `> 600` s, Warning `> 300` s
* **Runbook:** `aws lambda list-event-source-mappings --function-name avionpulse-worker` and check
  `State`. `Disabled` means the trigger is paused. RESET ALL re-enables it.
* **Test:** fault on. The simulator adds about 20 messages every 5 min. Alerts in about 20–25 min.

#### `simulator_crash` — Scheduled job crash
Create **two** monitors. The second one is the interesting one.

* **Monitor 1 — Type:** Metric → Threshold. **Name:** `[AvionPulse] Simulator errors`
  ```
  sum(last_15m):sum:aws.lambda.enhanced.errors{functionname:avionpulse-simulator}.as_count() > 0
  ```
* **Monitor 2 — Type:** Metric → Threshold. **Name:** `[AvionPulse] Flight data is stale`
  ```
  max(last_15m):max:avionpulse.data.staleness_s{env:demo} > 900
  ```
  Warning `> 600`. In **Missing data**, also tick *Notify if data is missing for more than 20 minutes*
  on a third quick monitor over `avionpulse.flights.airborne`. When the simulator crashes, the
  business metrics simply stop.
* **Runbook:** Serverless → `avionpulse-simulator` → *Errors* tab. Error Tracking will group the
  `SimulatorCrash` issue.
* **Test:** fault on. Errors alert after the next 5-minute tick; staleness alerts after about 15 min
  (only while someone is viewing the Dashboard — the API emits staleness when the summary loads).

#### `downstream_timeout` — Weather API timeout
* **Type:** Logs
* **Name:** `[AvionPulse] Third-party dependency timing out`
* **Search:**
  ```
  service:avionpulse UpstreamTimeout
  ```
* **Condition:** count `> 2` over the last 5 minutes
* **APM alternative:** **Monitors → New → APM** → *APM Metrics* on service `avionpulse`, resource
  `GET /api/weather` (if resources are named by route in your traces), Error rate `> 50%`.
* **Runbook:** The trace shows an HTTP span to `10.255.255.1` lasting 3 s. In a real system: check
  the provider's status page and whether a circuit breaker should open.
* **Test:** fault on, open the Dashboard 3 times (the weather panel calls the provider). Alerts in about 2 min.

### 6.5 Business logic

#### `silent_failure` — Silent partition-key bug
This is the showcase fault: no error, no latency change, HTTP 200 everywhere.

* **Monitor 1 — Type:** Metric → Threshold. **Name:** `[AvionPulse] No airborne flights`
  ```
  max(last_15m):avg:avionpulse.flights.airborne{env:demo} < 1
  ```
  Alert `< 1`. Normal is about 10–17 airborne.
* **Monitor 2 — Type:** Metric → Threshold. **Name:** `[AvionPulse] Flights API returning empty`
  ```
  max(last_10m):max:avionpulse.api.flights_returned{env:demo} < 1
  ```
* **Monitor 3 — Type:** Synthetic API test. **Name:** `[AvionPulse] Public stats content`
  * `GET https://<site>/api/dashboard/public-stats`
  * Assertions: status `200` **and** JSON path `$.flights.total` *greater than* `0`
  * Every 5 min, 1 location
* **Runbook:** APM shows 0% errors; that's the point. Check what changed recently (deploys) and
  query the table directly: `aws dynamodb query --table-name avionpulse-data --key-condition-expression "PK = :p" --expression-attribute-values '{":p":{"S":"FLIGHT"}}' --select COUNT`.
* **Test:** fault on. Synthetic about 5 min; airborne monitor after the next simulator run (≤ 5 min).

#### `pax_corruption` — Passenger count corruption
* **Type:** Metric → Threshold (deterministic). Use an Anomaly monitor once you have history.
* **Name:** `[AvionPulse] Implausible passenger count`
* **Query:**
  ```
  max(last_15m):max:avionpulse.pax.airborne{env:demo} > 20000
  ```
  Even if all 25 flights were airborne and full, the maximum is 6,338 seats (normal is about
  2,500–4,000), so anything over 20,000 is impossible. Under the fault it is in the millions.
* **Anomaly version:**
  `avg(last_4h):anomalies(avg:avionpulse.pax.airborne{env:demo}, 'basic', 3, direction='above', alert_window='last_15m') >= 1`
* **Runbook:** Infrastructure is fine; this is a data-quality bug. Compare a flight's `pax` with
  its aircraft `capacity` on the Flights and Fleet pages.
* **Test:** fault on. Alerts after the next simulator run (≤ 5 min).

#### `log_flood` — Log flood
* **Type:** Logs (or Anomaly on log count)
* **Name:** `[AvionPulse] Log volume spike`
* **Search:**
  ```
  service:avionpulse status:warn
  ```
* **Condition:** count `> 1000` over the last 5 minutes (normal is well under 50)
* **Also:** **Logs → Configuration → Indexes** → add a daily quota or an exclusion filter for
  `"[FAULT][LOG_FLOOD]"` if you want to protect your Datadog log budget during demos.
* **Runbook:** Logs → Patterns view. One pattern with thousands of hits is the culprit.
* **Test:** fault on, browse about 20 pages. Alerts in about 2 min.

### 6.6 Composite

#### `cascading_failure` — Cascading failure
* **Type:** **Composite**
* **Prerequisite monitors** (from above): `API latency p95 high` (A), `DynamoDB throttled (logs)` (B),
  `API Lambda error rate` (C), `Events queue age` (D), `Log volume spike` (E)
* **Name:** `[AvionPulse] P1 — cascading failure`
* **Trigger condition:**
  ```
  (a && c) && (b || d || e)
  ```
  (letters are assigned when you pick the monitors in the composite form)
* **Priority:** P1. **Message:** say *"Multiple tiers failing — start with the data tier,
  then work outwards"*.
* **Why composite:** each underlying monitor still notifies on its own; set them to *no
  notification* (remove recipients) if you want only the composite to page.
* **Test:** switch on `cascading_failure` (it turns on all five members). A and C fire in minutes,
  then B/E; the composite fires once A, C and one of the others are red.

### 6.7 Before you set up the v3 monitors

The v3 faults change **AWS resources**, so most of their signals come from the AWS integration
(CloudWatch, 5–15 min late) and CloudTrail. Do these once:

1. **AWS integration namespaces:** add SNS, States, ApplicationELB, EC2, AutoScaling and RDS
   ([§9.1](#91-aws-integration-scope)). Without them, the queries below return *No data*.
2. **CloudTrail in Datadog Logs** ([§9.2](#92-cloudtrail--datadog-the-who-changed-what-half)).
   Several monitors and every runbook below use it to answer *who changed what*.
3. **Placeholders:** `avionpulse` is the stack name, and `demo` is `AppEnv`. `<ApiId>` is the
   `ApiId` stack output.
4. **Classic tier monitors** (§6.12–6.13): set *Missing data* to **"Do not notify"**, because the
   tier is meant to disappear. Scope them with `tier:classic` or the Classic names in §4.
5. **Event overlay:** on every monitor's graph and every dashboard timeseries, add
   `sources:avionpulse-chaos` so the moment a fault was switched on is drawn on the graph.
6. **Tags:** as in §5, plus `chaos_tier:<control|security|integration|classic|fis>`.

**One monitor that covers every infra fault: AWS config change by AvionPulse**
* **Type:** Logs
* **Name:** `[AvionPulse] AWS configuration changed by the app role`
* **Search:**
  ```
  source:cloudtrail @userIdentity.arn:*avionpulse-AppRole* -@evt.name:(Describe* OR Get* OR List* OR SendMessage OR Publish OR PutEvents OR StartExecution OR PutObject OR GenerateDataKey OR Decrypt)
  ```
* **Condition:** count `> 0` over 10 min, **group by `@evt.name`** (one alert per kind of change).
* **Message extra:** `{{@evt.name.name}} on {{log.attributes.requestParameters}}`
* **Why:** it fires for every chaos change *and* for any change that didn't come from the Chaos page.
  Set it to *notify no one* and use it as the "cause" side of the composites below, or as a
  low-priority FYI.

> **Find the role name.** CloudFormation generates the role name, e.g.
> `avionpulse-AppRole-AbC123xyz`. Copy it from IAM, or use the wildcard as shown.

### 6.8 AWS control plane

#### `dynamodb_capacity_cut` — Table capacity cut
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] DynamoDB capacity below baseline`
* **Query:**
  ```
  min(last_15m):min:aws.dynamodb.provisioned_write_capacity_units{tablename:avionpulse-data} < 5
  ```
* **Thresholds:** Alert `< 5`. (Add the same for `provisioned_read_capacity_units`, or put both
  in a multi-query monitor.)
* **Symptom monitor (optional):**
  `sum(last_15m):sum:aws.dynamodb.write_throttle_events{tablename:avionpulse-data}.as_count() > 0`.
  The v2 *DynamoDB read throttling* monitor also fires under dashboard traffic.
* **Runbook:** CloudTrail `@evt.name:UpdateTable` shows who changed it. Put it back with
  RESET ALL, or `aws dynamodb update-table --table-name avionpulse-data --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5`.
* **Test:** fault on. The capacity metric changes on the next CloudWatch poll (10–20 min). The
  throttles start after the next simulator run (≤ 5 min) and show up in the metric 5–15 min later.
  AWS limits decreases per day, so if the toggle reports `LimitExceededException`, test tomorrow.

#### `eventbridge_rule_disabled` — Scheduler rule disabled
* **Type:** Metric → Threshold, **with missing data**
* **Name:** `[AvionPulse] Simulator stopped running`
* **Query:**
  ```
  sum(last_20m):sum:aws.lambda.enhanced.invocations{functionname:avionpulse-simulator}.as_count() < 1
  ```
* **Missing data:** **Notify** if data is missing for more than **20 minutes**. This is the
  condition that actually fires: a disabled rule produces no invocations, so the metric simply stops.
* **Second signal:** the v2 *Flight data is stale* monitor (`avionpulse.data.staleness_s > 900`)
  fires while someone is viewing the Dashboard.
* **Runbook:** EventBridge → Rules → `avionpulse-simulator-tick` → *Enable*; CloudTrail
  `@evt.name:DisableRule` shows who did it.
* **Test:** fault on. Alerts 20–25 min later. Nothing errors along the way, which is the point of the scenario.

#### `api_integration_broken` — API integration misrouted
Create two monitors and a composite.
* **A — Type:** Metric. **Name:** `[AvionPulse] API Gateway 5xx`
  ```
  sum(last_10m):sum:aws.apigateway.5xx{apiid:<ApiId>}.as_count() > 10
  ```
* **B — Type:** Metric (change). **Name:** `[AvionPulse] API Lambda invocations dropped`
  ```
  pct_change(avg(last_10m),last_1h):sum:aws.lambda.enhanced.invocations{functionname:avionpulse-api}.as_count() < -50
  ```
  (It drops but doesn't reach zero: login, chaos and Classic routes use the other integration.)
* **Composite:** `[AvionPulse] API Gateway can't reach Lambda` = `a && b`.
* **Also catches it:** the v2 *Health endpoint* Synthetic test, because `/api/health` is a data route.
* **Optional log monitor** (if the API Gateway access-log group `/aws/apigateway/avionpulse-access`
  is forwarded): `@integration_error:*Function*not*found*` count `> 0`.
* **Runbook:** API Gateway → APIs → `avionpulse-http` → Integrations: the data integration's target
  ends in `:chaos-missing-alias`. Edit it back to the function ARN, or RESET ALL.
* **Test:** fault on, browse Flights. Alerts in 10–20 min (CloudWatch). The Synthetic alerts on its next run.

#### `lambda_bad_deploy` — Bad worker deploy
* **Type:** Logs (fast) + Metric (backup)
* **Name:** `[AvionPulse] Worker cannot load its handler`
* **Search:**
  ```
  functionname:avionpulse-worker ("Runtime.ImportModuleError" OR "No module named")
  ```
  Condition: count `> 0` over 5 min.
* **Backup metric:**
  ```
  sum(last_15m):sum:aws.lambda.errors{functionname:avionpulse-worker}.as_count() / sum:aws.lambda.invocations{functionname:avionpulse-worker}.as_count() * 100 > 50
  ```
  This uses the CloudWatch metrics rather than `enhanced.*`, because init failures happen before the
  handler wrapper can record them.
* **Downstream:** the v2 *Messages in DLQ* monitor fires about 3 receives later.
* **Runbook:** Lambda → `avionpulse-worker` → Configuration: `DD_LAMBDA_HANDLER` (or *Handler*) is
  `worker_v2.handler`; set it to `worker.handler`. CloudTrail
  `@evt.name:UpdateFunctionConfiguration20150331v2` shows the change.
* **Test:** fault on, raise a maintenance request. The log monitor alerts in ~2 min.

#### `ssm_config_poisoned` — Feature flags corrupted
* **Type:** Logs
* **Name:** `[AvionPulse] Feature-flag configuration invalid`
* **Search:**
  ```
  service:avionpulse ConfigParseError
  ```
  Condition: count `> 0` over 5 min.
* **Alternative (APM):** *APM monitor → Error rate* on `service:avionpulse`,
  `resource_name:"GET /api/flights"`, `> 20%`.
* **Runbook:** `aws ssm get-parameter --name /avionpulse/config/flags`: the value is not valid
  JSON. CloudTrail `@evt.name:PutParameter` shows who wrote it. Restore the default JSON or RESET ALL.
* **Test:** fault on, open Flights. Alerts in 1–3 min (15 s flag cache + log latency).

#### `sqs_retention_cut` — Queue retention 60 s
* **Type:** Metric → Threshold (two)
* **Name:** `[AvionPulse] Events sent but never processed`
  ```
  sum(last_30m):sum:aws.sqs.number_of_messages_sent{queuename:avionpulse-events}.as_count() - sum:aws.sqs.number_of_messages_deleted{queuename:avionpulse-events}.as_count() > 10
  ```
* **Business side:** `[AvionPulse] No events processed`
  ```
  sum(last_20m):sum:avionpulse.events.processed{env:demo}.as_count() < 1
  ```
  Missing data → **Notify** after 20 min.
* **Why two:** the v2 *Events queue age* monitor stays **quiet**, because messages expire after 60 s
  and never get old. Only sent-vs-deleted and the business count show the loss.
* **Runbook:** SQS → `avionpulse-events` → *Message retention period* = 60 s and the Lambda trigger
  disabled. Set retention back to 1 day (86400 s) and enable the trigger, or RESET ALL.
* **Test:** fault on, raise 3–5 maintenance requests over 10 min. Alerts in 20–30 min.

### 6.9 Security & IAM

If you have **Cloud SIEM**, enable its out-of-the-box AWS CloudTrail detection rules (*Security →
Detection Rules*, source `cloudtrail`). Several fire on these faults; search the rule list for
*KMS*, *IAM policy* and *resource policy*. The monitors below work without SIEM.

#### `iam_policy_detached` — Worker policy detached
* **Type:** Logs
* **Name:** `[AvionPulse] Worker AccessDenied`
* **Search:**
  ```
  functionname:avionpulse-worker AccessDeniedException
  ```
  Condition: count `> 0` over 5 min. (Scoping to the worker keeps it separate from v2's
  `iam_access_denied`, which happens in the api function.)
* **Cause monitor (optional):** Logs `source:cloudtrail @evt.name:DetachRolePolicy` count `> 0`.
* **Runbook:** IAM → Roles → the worker role → *Permissions*: `WorkerDataPolicy` is missing.
  Attach it again (stack output `WorkerDataPolicyArn`) or RESET ALL.
* **Test:** fault on, raise a maintenance request. Alerts in ~2 min (IAM changes take a few
  seconds to apply).

#### `kms_key_disabled` — KMS key disabled
* **Type:** Logs
* **Name:** `[AvionPulse] Queue encryption key unavailable`
* **Search:**
  ```
  service:avionpulse ("KMS.DisabledException" OR KMSDisabledException OR "KMS.KMSInvalidStateException")
  ```
  Condition: count `> 0` over 5 min.
* **Cause monitor:** Logs `source:cloudtrail @evt.name:(DisableKey OR ScheduleKeyDeletion)` count `> 0`.
  Treat it as a **security** alert: disabling a key is a classic ransomware and sabotage step.
* **Downstream:** the worker can't decrypt, so the queue builds; the v2 *Events queue age* monitor follows.
* **Runbook:** KMS → Customer managed keys → `alias/avionpulse-events` → *Key actions → Enable*, or RESET ALL.
* **Test:** fault on, raise a maintenance request **after about a minute** (SQS caches data keys
  for 60 s). Alerts in ~2–3 min.

#### `sqs_send_denied` — Queue policy deny
* **Type:** Logs
* **Name:** `[AvionPulse] SQS resource policy denies the app`
* **Search:**
  ```
  service:avionpulse "sqs:SendMessage" "explicit deny"
  ```
  Condition: count `> 0` over 5 min.
* **Cause monitor:** Logs `source:cloudtrail @evt.name:SetQueueAttributes` count `> 0`.
* **Runbook:** SQS → `avionpulse-events` → *Access policy*: a `Deny` statement for the app role.
  Delete the policy, or RESET ALL. IAM looks fine; the queue's own policy is refusing the call.
* **Test:** fault on, raise a maintenance request (SQS policy changes can take up to 60 s). Alerts in ~2 min.

#### `s3_bucket_policy_deny` — Recorder bucket locked
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Flight-data recorder uploads failing`
  ```
  sum(last_15m):sum:avionpulse.integration.errors{channel:recorder}.as_count() > 0
  ```
* **Alternative (Logs):** `service:avionpulse "simulator: recorder failed"`.
* **Cause monitor:** Logs `source:cloudtrail @evt.name:PutBucketPolicy`. CSM Misconfigurations
  also evaluates the bucket policy on its next scan.
* **Runbook:** S3 → recorder bucket (stack output `RecorderBucketName`) → *Permissions → Bucket
  policy*: delete it, or RESET ALL.
* **Test:** fault on. Alerts after the next simulator run (≤ 5 min).

### 6.10 Integration — EventBridge bus / SNS / Step Functions / Streams

**Generic "pipe stopped" monitor** (one per channel, or one multi-alert):
* **Type:** Metric → Threshold, multi alert by `channel`
* **Name:** `[AvionPulse] Integration channel {{channel.name}} stopped`
  ```
  sum(last_20m):sum:avionpulse.integration.received{env:demo} by {channel}.as_count() < 1
  ```
* **Missing data:** Notify after 20 min. This catches all four faults below from the consumer side.

#### `eb_target_broken` — Bus target broken
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] EventBridge failed invocations`
  ```
  sum(last_15m):sum:aws.events.failed_invocations{rulename:avionpulse-flight-status}.as_count() > 0
  ```
* **Runbook:** EventBridge → Event buses → `avionpulse-flight-ops` → rule `avionpulse-flight-status`
  → *Targets*: the ARN ends in `:chaos-missing-alias`. CloudTrail `@evt.name:PutTargets`.
* **Test:** fault on. Failures are counted after 2 retries; expect the alert in 15–25 min.

#### `sns_delivery_failure` — SNS delivery failure
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] SNS deliveries failing`
  ```
  sum(last_15m):sum:aws.sns.number_of_notifications_failed{topicname:avionpulse-crew-alerts}.as_count() > 0
  ```
* **Runbook:** Lambda → `avionpulse-processor` → *Configuration → Permissions → Resource-based
  policy*: the `sns.amazonaws.com` statement is gone. CloudTrail `@evt.name:RemovePermission*`.
  After the revert, SNS keeps retrying, so the missed alerts arrive late; expect a burst.
* **Test:** fault on. Alerts in 15–25 min.

#### `stepfn_task_failure` — Turnaround workflow failing
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Turnaround workflow failing`
  ```
  sum(last_15m):sum:aws.states.executions_failed{statemachinearn:*avionpulse-turnaround*}.as_count() + sum:aws.states.executions_timed_out{statemachinearn:*avionpulse-turnaround*}.as_count() > 0
  ```
  If your Datadog site doesn't accept the wildcard, use the full ARN
  (`arn:aws:states:<region>:<account>:statemachine:avionpulse-turnaround`, lowercased).
* **Also:** Error Tracking groups `CrewCheckFailed` from `avionpulse-processor`.
* **Runbook:** Step Functions Express executions aren't listed in the console by default; use the
  processor's logs (`"[FAULT] stepfn_task_failure"`) and the turnaround channel on the Integrations page.
* **Test:** fault on. Alerts in 15–20 min (the metric is polled).

#### `stream_processor_poison` — Stream processor stuck
* **Type:** Metric → Threshold (or **Forecast**)
* **Name:** `[AvionPulse] Stream iterator age high`
  ```
  max(last_10m):max:aws.lambda.iterator_age{functionname:avionpulse-processor} > 300000
  ```
  Iterator age is in **milliseconds**: 300000 = 5 min. Warning `> 120000`.
* **Faster signal:** Logs `functionname:avionpulse-processor "[FAULT] stream_processor_poison"`,
  or the processor's error rate `aws.lambda.enhanced.errors{functionname:avionpulse-processor}`.
* **Runbook:** the processor rejects every stream batch, and Lambda retries the same shard position
  until the records are an hour old. Fix the consumer (turn the fault off) and the backlog drains.
* **Test:** fault on. Errors in 1–2 min; iterator age crosses 5 min after about 10 min + CloudWatch delay.

### 6.11 v3 composites

#### `security_incident` — Security incident
* **Type:** Composite
* **Members:** `Worker AccessDenied` (a), `Queue encryption key unavailable` (b),
  `SQS resource policy denies the app` (c), `Flight-data recorder uploads failing` (d)
* **Name:** `[AvionPulse] P1 — security incident`
* **Trigger:** `(a || b) && (c || d)`
* **Message:** *"Several resource policies, keys or roles changed at once. Treat as a possible
  compromise: check CloudTrail for the principal before reverting."* With Cloud SIEM, link the investigation.
* **Test:** switch on `security_incident`. The composite fires within ~5 min of the slowest member.

#### `control_plane_storm` — Control-plane storm
* **Type:** Composite
* **Members:** `DynamoDB capacity below baseline` (a), `Simulator stopped running` (b),
  `Worker cannot load its handler` (c), `Feature-flag configuration invalid` (d)
* **Name:** `[AvionPulse] P1 — control-plane storm`
* **Trigger:** `(c || d) && (a || b)`
* **Message:** link to the saved CloudTrail view in §9.2 (*"what changed in the last 30 minutes"*).
* **Test:** switch on `control_plane_storm`. `c` and `d` fire in minutes; the composite fires when
  `a` or `b` catches up (10–25 min).

### 6.12 Classic tier

All monitors here: **Missing data → Do not notify**, scope `tier:classic` or the Classic names.
They only mean something while the tier is online.

#### `classic_sg_lockdown` — Security group lockdown
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Classic ALB unhealthy targets`
  ```
  max(last_5m):max:aws.applicationelb.un_healthy_host_count{loadbalancer:app/avionpulse-classic/*} > 0
  ```
* **Cause monitor:** Logs `source:cloudtrail @evt.name:RevokeSecurityGroupIngress`.
* **Runbook:** EC2 → Security groups → the instance group: the ALB → 8080 rule is missing. Re-add
  it or turn the fault off.
* **Test:** fault on, open Gates. Targets go unhealthy after 2 failed checks (30 s); the metric
  arrives 5–10 min later.

#### `classic_nacl_db_block` — NACL blocks database
* **Type:** APM → Error rate
* **Name:** `[AvionPulse] Gate service errors`
  ```
  sum(last_5m):( sum:trace.http.request.errors{service:avionpulse-gates,env:demo}.as_count() / sum:trace.http.request.hits{service:avionpulse-gates,env:demo}.as_count() ) * 100 > 20
  ```
* **Alternatives:** Logs `service:avionpulse-gates "gate board failed"`; or from Lambda's side,
  Logs `service:avionpulse (ALB500 OR GateServiceTimeout)`.
* **Make the point:** put the *Classic ALB unhealthy targets* monitor next to it. It stays **OK**,
  because health checks don't touch the database.
* **Runbook:** VPC → Network ACLs → the `…-classic-db` ACL → inbound rule **10** denies TCP 5432.
  Delete it or turn the fault off.
* **Test:** fault on, open Gates a few times. Alerts in ~2 min (APM).

#### `classic_asg_zero` — ASG scaled to zero
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Classic has no healthy hosts`
  ```
  max(last_5m):max:aws.applicationelb.healthy_host_count{loadbalancer:app/avionpulse-classic/*} < 1
  ```
* **Second query (same monitor or separate):**
  `min(last_5m):min:aws.autoscaling.group_in_service_instances{autoscalinggroupname:avionpulse-classic-gates} < 1`
* **Runbook:** EC2 → Auto Scaling groups → `avionpulse-classic-gates`: desired = 0. Set it back
  to 1 (Minimal) or 2 (Full), or turn the fault off. Healthy again in ~3 min.
* **Test:** fault on. Alerts in 5–10 min. In Infrastructure, the hosts stop reporting.

#### `classic_alb_maintenance` — ALB rule → fixed 503
* **Type:** Logs
* **Name:** `[AvionPulse] Classic ALB returning maintenance page`
* **Search:**
  ```
  service:avionpulse ALB503 "maintenance"
  ```
  Condition: count `> 0` over 5 min.
* **Metric view:** `aws.applicationelb.http_fixed_response_count` rises while `request_count`
  to targets falls and `healthy_host_count` stays normal. Fixed responses also include the
  default 403 that internet scanners hit, so don't alert on that metric alone.
* **Runbook:** EC2 → Load balancers → `avionpulse-classic` → Listener 80 → rule priority 10: the
  action is *Return fixed response 503*. Set it back to *Forward to* the target group, or turn the fault off.
* **Test:** fault on, open Gates. Alerts in ~2 min.

#### `classic_rds_reboot` — RDS reboot / failover
* **Type:** Logs (CloudTrail)
* **Name:** `[AvionPulse] Classic database rebooted`
* **Search:**
  ```
  source:cloudtrail @evt.name:RebootDBInstance
  ```
* **Symptom:** the *Gate service errors* monitor fires for 1–3 min, then recovers by itself.
* **Runbook:** RDS → the instance → *Logs & events*: "DB instance restarted" (or "Multi-AZ
  failover completed" on Full). Nothing to revert.
* **Test:** fault on (one-shot). CloudTrail alert in 5–15 min; the errors are visible in APM immediately.

#### `classic_db_slow_query` — Slow database queries
* **Type:** Metric → Threshold (APM trace metric)
* **Name:** `[AvionPulse] Classic database queries slow`
  ```
  percentile(last_5m):p95:trace.postgres.query{env:demo} > 2
  ```
  (seconds). Warning `> 1`. If your APM shows the postgres spans under a different operation name,
  open a trace and use the name shown.
* **End-to-end view:** `p95:trace.http.request{service:avionpulse-gates}` and
  `avg:aws.applicationelb.target_response_time.average{loadbalancer:app/avionpulse-classic/*}`.
* **Runbook:** the trace flame graph shows `SELECT pg_sleep(2.5)` before every query. Turn the fault off.
* **Test:** fault on, open Gates 5 times. Alerts in ~2–3 min.

### 6.13 AWS FIS experiments

**Experiment started (one monitor for all four):**
* **Type:** Logs. **Name:** `[AvionPulse] FIS experiment started`
* **Search:** `source:cloudtrail @evt.name:StartExperiment` (count `> 0`, group by `@requestParameters.experimentTemplateId`)
* Useful as an event overlay too.

#### `fis_cpu_stress` — CPU stress
* **Type:** Metric → Threshold, multi alert by `host`
* **Name:** `[AvionPulse] Classic host CPU saturated`
  ```
  avg(last_5m):avg:system.cpu.user{tier:classic} by {host} > 85
  ```
  (Needs the Agent, i.e. `EnableDatadog=true`. Without it use
  `avg:aws.ec2.cpuutilization{tier:classic} by {host}` with a 10-minute window.)
* **Runbook:** FIS → Experiments → running experiment → *Stop*, or turn the fault off. t4g burst
  credits fall during the test (`aws.ec2.cpucredit_balance`).
* **Test:** fault on. Alerts in ~5 min; the experiment ends by itself after ~5 min.

#### `fis_network_latency` — Network latency
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Classic response time high`
  ```
  avg(last_5m):avg:aws.applicationelb.target_response_time.average{loadbalancer:app/avionpulse-classic/*} > 0.5
  ```
  (seconds). APM shows each postgres span about +300 ms.
* **Test:** fault on, open Gates repeatedly. Alerts in 5–10 min.

#### `fis_instance_terminate` — Terminate instance
* **Type:** Metric → Threshold
* **Name:** `[AvionPulse] Classic ASG below desired`
  ```
  min(last_5m):min:aws.autoscaling.group_in_service_instances{autoscalinggroupname:avionpulse-classic-gates} < 1
  ```
  For Full size use `< 2` (warning) / `< 1` (alert).
* **Cause:** CloudTrail `@evt.name:TerminateInstances` by the FIS role.
* **Runbook:** nothing to do. Auto Scaling replaces the instance in ~3 min; watch the new host appear.
* **Test:** fault on. Alerts in 5–10 min; recovers by itself.

#### `fis_az_outage` — AZ outage (Full size only)
* **Type:** Composite
* **Members:** `Classic ASG below desired` (a, at `< 2`), `Classic database rebooted` (b), `Gate service errors` (c)
* **Name:** `[AvionPulse] P1 — Availability Zone disruption`
* **Trigger:** `(a || c) && b`
* **Runbook:** this is a rehearsal of an AWS AZ event. Confirm that the service recovers on its
  own (RDS fails over, the healthy instance in AZ-b keeps serving), and note the time to recovery.
  Compare with AWS Health events ([AWS_RUNBOOKS.md §1](AWS_RUNBOOKS.md#1-aws-health-events)).
* **Test:** launch Classic **Full**, fault on. The failover takes 1–2 min, and the blackout lasts 5 min.

### 6.14 Summary table (all 49 faults)

| Fault | Monitor | Type | Typical time to alert |
|---|---|---|---|
| api_throttling | API Gateway throttling (429s) | Metric (+composite) | 10–20 min |
| latency_spike | API latency p95 high | Metric | 2–5 min |
| random_500s | API Lambda error rate | Metric | 2–5 min |
| health_check_fail | Health endpoint | Synthetic | ≤ 5 min |
| lambda_timeout | Lambda timeouts | Metric | ~2 min |
| lambda_oom | Lambda out of memory | Metric | ~2 min |
| memory_leak | API memory forecast to hit limit | Forecast | ~5 min |
| cpu_burn | API billed duration anomaly | Anomaly / Metric | ~3 min |
| cold_start_storm | Cold start storm | Metric | 2–5 min |
| worker_throttle | Worker throttled + Events queue backing up | Metric ×2 | 15–25 min |
| dynamodb_throttle | DynamoDB read throttling (+ logs) | Metric + Logs | 2 / 15 min |
| full_table_scan | DynamoDB Scan in request path | Span metric | ~3 min |
| n_plus_one | DynamoDB N+1 pattern | Span metric | ~3 min |
| iam_access_denied | IAM AccessDenied | Logs | 1–2 min |
| s3_missing_object | S3 NoSuchKey | Logs | 1–2 min |
| sqs_poison_pill | Messages in DLQ (+ poison counter) | Metric | 2 / 20 min |
| queue_backlog | Events queue age | Metric | 20–25 min |
| simulator_crash | Simulator errors + Flight data is stale | Metric ×2 | 5 / 15 min |
| downstream_timeout | Third-party dependency timing out | Logs | ~2 min |
| silent_failure | No airborne flights + empty API + Synthetic | Metric ×2 + Synthetic | ≤ 5 min |
| pax_corruption | Implausible passenger count | Metric | ≤ 5 min |
| log_flood | Log volume spike | Logs | ~2 min |
| cascading_failure | P1 — cascading failure | Composite | 5–15 min |
| dynamodb_capacity_cut | DynamoDB capacity below baseline (+ write throttles) | Metric | 10–20 min |
| eventbridge_rule_disabled | Simulator stopped running | Metric (missing data) | 20–25 min |
| api_integration_broken | API Gateway can't reach Lambda (+ Health Synthetic) | Composite | ≤ 5 / 10–20 min |
| lambda_bad_deploy | Worker cannot load its handler | Logs (+ metric) | ~2 min |
| ssm_config_poisoned | Feature-flag configuration invalid | Logs | 1–3 min |
| sqs_retention_cut | Events sent but never processed + No events processed | Metric ×2 | 20–30 min |
| iam_policy_detached | Worker AccessDenied | Logs | ~2 min |
| kms_key_disabled | Queue encryption key unavailable (+ DisableKey) | Logs | 2–3 min |
| sqs_send_denied | SQS resource policy denies the app | Logs | ~2 min |
| s3_bucket_policy_deny | Flight-data recorder uploads failing | Metric | ≤ 5 min |
| eb_target_broken | EventBridge failed invocations | Metric | 15–25 min |
| sns_delivery_failure | SNS deliveries failing | Metric | 15–25 min |
| stepfn_task_failure | Turnaround workflow failing | Metric | 15–20 min |
| stream_processor_poison | Stream iterator age high | Metric / Forecast | 15–20 min |
| security_incident | P1 — security incident | Composite | ~5 min |
| control_plane_storm | P1 — control-plane storm | Composite | 10–25 min |
| classic_sg_lockdown | Classic ALB unhealthy targets | Metric | 5–10 min |
| classic_nacl_db_block | Gate service errors | APM | ~2 min |
| classic_asg_zero | Classic has no healthy hosts | Metric | 5–10 min |
| classic_alb_maintenance | Classic ALB returning maintenance page | Logs | ~2 min |
| classic_rds_reboot | Classic database rebooted (+ gate errors) | Logs (CloudTrail) | 5–15 min |
| classic_db_slow_query | Classic database queries slow | APM trace metric | 2–3 min |
| fis_cpu_stress | Classic host CPU saturated | Metric (Agent) | ~5 min |
| fis_network_latency | Classic response time high | Metric | 5–10 min |
| fis_instance_terminate | Classic ASG below desired | Metric | 5–10 min |
| fis_az_outage | P1 — Availability Zone disruption | Composite | 5–15 min |

---

## 7. The AvionPulse Health dashboard

One screen that answers "is AvionPulse healthy, and if not, which tier?". It's laid out top to
bottom in the same order as the architecture: business → edge → compute → data → async →
**AWS infrastructure → integrations → Classic tier** (v3) → alerts and logs.

### Option A — import the JSON (2 minutes)

1. Open [`docs/datadog/avionpulse-dashboard.json`](datadog/avionpulse-dashboard.json) and replace
   all 5 occurrences of `REPLACE_WITH_API_ID` with your `ApiId` stack output (find-and-replace;
   they're in the three API Gateway widgets).
2. **Dashboards → New Dashboard →** give it any name → **Create**.
3. Click the **gear icon (⚙) → Import dashboard JSON**, paste the file, **Yes, replace**.
4. Set the `env` template variable at the top to `demo`.
5. The dashboard is titled **AvionPulse v3 — Cloud Health** and includes the three v3 groups.
   *AWS infrastructure* needs CloudTrail in Datadog (§9.2) for its first widget. *Classic tier*
   shows no data until you launch that tier.
6. Using a stack name other than `avionpulse`? Also find-and-replace `avionpulse-` with `<stack>-`.

### Option B — build it by hand

**Create the dashboard**

1. **Dashboards → New Dashboard**, name `AvionPulse v3 — Cloud Health`, layout **Dashboard**
   (the grid layout, not a Screenboard).
2. **Template variable:** click **Add Variable** → name `env`, tag/attribute `env`, default `demo`.
   Custom `avionpulse.*` queries below use `{$env}`. AWS integration metrics use resource
   names instead, because they don't all carry `env`.
3. For each group below: **Add Widgets → Group**, give it the title, then drag the listed widgets
   *into* the group. For every widget: paste the query in the **</>** (source) editor, set the
   title, then **Save**.

**Group 1 — Business health** (what the airline cares about)

| Widget | Type | Query | Settings |
|---|---|---|---|
| Airborne now | Query Value | `avg:avionpulse.flights.airborne{$env}` | Aggregation *last*; conditional format: `< 1` red, `>= 1` green |
| Passengers airborne | Query Value | `avg:avionpulse.pax.airborne{$env}` | *last*; `> 20000` red (corruption) |
| On-time % | Query Value | `avg:avionpulse.flights.on_time_pct{$env}` | *last*; unit `%`; `< 60` yellow, `>= 60` green |
| Data age | Query Value | `max:avionpulse.data.staleness_s{$env}` | *last*; unit `s`; `> 900` red, `> 600` yellow, else green |
| Flights by state | Timeseries (bars) | `avg:avionpulse.flights.airborne{$env}`, `avg:avionpulse.flights.delayed{$env}`, `avg:avionpulse.flights.cancelled{$env}` | Three queries on one widget; aliases Airborne / Delayed / Cancelled |
| Chaos switches | Timeseries (bars) | `sum:avionpulse.chaos.toggle{$env} by {fault}.as_count()` | Shows when someone flipped a fault — great for correlating everything below |

**Group 2 — Edge (CloudFront / API Gateway)**

| Widget | Type | Query | Settings |
|---|---|---|---|
| API requests | Timeseries | `sum:aws.apigateway.count{apiid:<ApiId>}.as_count()` | Bars |
| 4xx vs 5xx | Timeseries | `sum:aws.apigateway.4xx{apiid:<ApiId>}.as_count()`, `sum:aws.apigateway.5xx{apiid:<ApiId>}.as_count()` | Bars; colour 4xx orange, 5xx red |
| Latency vs integration latency | Timeseries | `avg:aws.apigateway.latency{apiid:<ApiId>}`, `avg:aws.apigateway.integration_latency{apiid:<ApiId>}` | Lines, unit ms. Gap between them = time spent in API Gateway itself |
| App requests by status | Timeseries | `sum:avionpulse.api.requests{$env} by {status_class}.as_count()` | Stacked bars. Real time, unlike the CloudWatch widgets above |
| API latency p95 | Timeseries | `p95:avionpulse.api.latency_ms{$env}` | Line; add a marker `y = 2000` labelled "SLO" |
| CloudFront requests (optional) | Timeseries | `sum:aws.cloudfront.requests{app:avionpulse}.as_count()` | Only if the integration collects us-east-1 |

**Group 3 — Compute (Lambda)**

| Widget | Type | Query | Settings |
|---|---|---|---|
| Invocations by function | Timeseries | `sum:aws.lambda.enhanced.invocations{service:avionpulse} by {functionname}.as_count()` | Stacked bars |
| Errors by function | Timeseries | `sum:aws.lambda.enhanced.errors{service:avionpulse} by {functionname}.as_count()` | Bars, red palette |
| Timeouts & OOMs | Timeseries | `sum:aws.lambda.enhanced.timeouts{service:avionpulse}.as_count()`, `sum:aws.lambda.enhanced.out_of_memory{service:avionpulse}.as_count()` | Bars |
| Duration (avg / max) | Timeseries | `avg:aws.lambda.enhanced.duration{functionname:avionpulse-api}`, `max:aws.lambda.enhanced.duration{functionname:avionpulse-api}` | Lines, unit s; marker `y = 10` "timeout" |
| Cold starts | Timeseries | `sum:aws.lambda.enhanced.invocations{functionname:avionpulse-api,cold_start:true}.as_count()` | Bars, blue |
| Init duration | Timeseries | `avg:aws.lambda.enhanced.init_duration{functionname:avionpulse-api}` | Line, unit s |
| Max memory used | Timeseries | `max:aws.lambda.enhanced.max_memory_used{service:avionpulse} by {functionname}` | Lines, unit MB; marker `y = 256` "limit". Leak shows as a sawtooth |
| Throttles | Timeseries | `sum:aws.lambda.throttles{app:avionpulse} by {functionname}.as_count()` | Bars |
| Estimated Lambda cost | Query Value | `sum:aws.lambda.enhanced.estimated_cost{service:avionpulse}.as_count()` | Time frame *past 1 month* (pin it in widget settings); unit `$` |

**Group 4 — Data (DynamoDB / S3)**

| Widget | Type | Query | Settings |
|---|---|---|---|
| Read capacity: consumed vs provisioned | Timeseries | `sum:aws.dynamodb.consumed_read_capacity_units{tablename:avionpulse-data}.as_rate()`, `avg:aws.dynamodb.provisioned_read_capacity_units{tablename:avionpulse-data}` | Consumed as area, provisioned as a dashed line |
| Write capacity: consumed vs provisioned | Timeseries | `sum:aws.dynamodb.consumed_write_capacity_units{tablename:avionpulse-data}.as_rate()`, `avg:aws.dynamodb.provisioned_write_capacity_units{tablename:avionpulse-data}` | Same styling |
| Throttle events | Timeseries | `sum:aws.dynamodb.read_throttle_events{tablename:avionpulse-data}.as_count()`, `sum:aws.dynamodb.write_throttle_events{tablename:avionpulse-data}.as_count()` | Bars, red |
| DynamoDB calls by operation | Timeseries | `sum:avionpulse.ddb.calls{*} by {@aws.operation}.as_count()` | Stacked bars. Needs the span metric from section 4. Scan or GetItem spikes = `full_table_scan` / `n_plus_one` |
| Data-tier errors (logs) | Toplist | Logs source: `service:avionpulse "Request failed"`, count, group by `@error_code`… | If the attribute isn't parsed, use a *Log Stream* widget with the same search instead |

**Group 5 — Async (SQS / EventBridge)**

| Widget | Type | Query | Settings |
|---|---|---|---|
| Queue depth | Timeseries | `max:aws.sqs.approximate_number_of_messages_visible{queuename:avionpulse-events}` | Line |
| Oldest message age | Timeseries | `max:aws.sqs.approximate_age_of_oldest_message{queuename:avionpulse-events}` | Line, unit s; marker `y = 600` |
| DLQ depth | Query Value | `max:aws.sqs.approximate_number_of_messages_visible{queuename:avionpulse-events-dlq}` | *last*; `> 0` red, `= 0` green |
| Events processed vs poison | Timeseries | `sum:avionpulse.events.processed{$env}.as_count()`, `sum:avionpulse.events.poison{$env}.as_count()` | Bars, poison in red |
| Queue processing lag | Timeseries | `avg:avionpulse.queue.processing_lag_s{$env}`, `max:avionpulse.queue.processing_lag_s{$env}` | Lines, unit s |
| Simulator runs & errors | Timeseries | `sum:aws.lambda.enhanced.invocations{functionname:avionpulse-simulator}.as_count()`, `sum:aws.lambda.enhanced.errors{functionname:avionpulse-simulator}.as_count()` | Bars. Steady ~1 per 5 min is healthy |

**Group 6 — Alerts & logs**

| Widget | Type | Settings |
|---|---|---|
| AvionPulse monitors | Monitor Summary | Query `tag:app:avionpulse`; display *Counts and list*; hide zero counts |
| Error logs | Log Stream | Search `service:avionpulse status:error`; columns *date, functionname, message* |
| Chaos activity | Log Stream | Search `service:avionpulse "[CHAOS]"` — every fault toggle and auto-revert |
| Log volume by status | Timeseries | Data source *Logs*, search `service:avionpulse`, count, group by `status`; stacked bars |

**Group 7 — AWS infrastructure (v3)**

| Widget | Type | Query | Settings |
|---|---|---|---|
| Config changes by the app | Log Stream | `source:cloudtrail @userIdentity.arn:*avionpulse-AppRole* -@evt.name:(Describe* OR Get* OR List* OR SendMessage OR Publish OR PutEvents OR StartExecution OR PutObject OR GenerateDataKey OR Decrypt)` | Columns *date, @evt.name, @requestParameters*. Needs CloudTrail (§9.2) |
| Chaos events | Event Stream (or List → Events) | `source:avionpulse-chaos` | Every toggle, reset and Classic launch/teardown |
| Provisioned capacity | Timeseries | `avg:aws.dynamodb.provisioned_read_capacity_units{tablename:avionpulse-data}`, `avg:aws.dynamodb.provisioned_write_capacity_units{tablename:avionpulse-data}` | Lines; should be flat at 5 |
| Simulator invocations | Timeseries | `sum:aws.lambda.enhanced.invocations{functionname:avionpulse-simulator}.as_count()` | Bars; a gap = rule disabled |
| Worker errors vs invocations | Timeseries | `sum:aws.lambda.errors{functionname:avionpulse-worker}.as_count()`, `sum:aws.lambda.invocations{functionname:avionpulse-worker}.as_count()` | Bad deploy = errors equal invocations |
| Sent vs deleted | Timeseries | `sum:aws.sqs.number_of_messages_sent{queuename:avionpulse-events}.as_count()`, `sum:aws.sqs.number_of_messages_deleted{queuename:avionpulse-events}.as_count()` | A gap = messages lost (`sqs_retention_cut`) or stuck |
| Security errors | Timeseries (Logs) | `service:avionpulse (AccessDeniedException OR "explicit deny" OR "KMS.DisabledException")`, count by `functionname` | Stacked bars |

**Group 8 — Integrations (v3)**

| Widget | Type | Query | Settings |
|---|---|---|---|
| Received by channel | Timeseries | `sum:avionpulse.integration.received{$env} by {channel}.as_count()` | Stacked bars; a missing colour = a stopped pipe |
| Producer errors by channel | Timeseries | `sum:avionpulse.integration.errors{$env} by {channel}.as_count()` | Bars, red |
| EventBridge failed invocations | Timeseries | `sum:aws.events.failed_invocations{rulename:avionpulse-flight-status}.as_count()` | Bars |
| SNS failed deliveries | Timeseries | `sum:aws.sns.number_of_notifications_failed{topicname:avionpulse-crew-alerts}.as_count()` | Bars |
| Turnaround failures | Timeseries | `sum:aws.states.executions_failed{statemachinearn:*avionpulse-turnaround*}.as_count()`, `sum:aws.states.executions_timed_out{statemachinearn:*avionpulse-turnaround*}.as_count()` | Bars |
| Stream iterator age | Timeseries | `max:aws.lambda.iterator_age{functionname:avionpulse-processor}` | Line, unit ms; marker `y = 300000` |

**Group 9 — Classic tier (v3, on demand)**

Shows *No data* while the tier is offline, which is expected.

| Widget | Type | Query | Settings |
|---|---|---|---|
| Healthy / unhealthy hosts | Timeseries | `max:aws.applicationelb.healthy_host_count{loadbalancer:app/avionpulse-classic/*}`, `max:aws.applicationelb.un_healthy_host_count{loadbalancer:app/avionpulse-classic/*}` | Lines, green and red |
| ALB 5xx and fixed responses | Timeseries | `sum:aws.applicationelb.httpcode_elb_5xx{loadbalancer:app/avionpulse-classic/*}.as_count()`, `sum:aws.applicationelb.http_fixed_response_count{loadbalancer:app/avionpulse-classic/*}.as_count()` | Bars |
| Target response time | Timeseries | `avg:aws.applicationelb.target_response_time.average{loadbalancer:app/avionpulse-classic/*}` | Line, unit s |
| Host CPU | Timeseries | `avg:system.cpu.user{tier:classic} by {host}` | Lines (Agent); FIS CPU stress shows as ~100% |
| ASG in service | Timeseries | `min:aws.autoscaling.group_in_service_instances{autoscalinggroupname:avionpulse-classic-gates}` | Line |
| RDS connections & CPU | Timeseries | `avg:aws.rds.database_connections{tier:classic}`, `avg:aws.rds.cpuutilization{tier:classic}` | Two lines |
| Gate service p95 | Timeseries | `p95:trace.http.request{service:avionpulse-gates}`, `p95:trace.postgres.query{env:$env.value}` | Lines, unit s |
| Classic launches | Query Value | `sum:avionpulse.classic.launch{$env}.as_count()` | Time frame *past 1 month*; multiply by your average hours × hourly rate from [COST.md](COST.md) for a rough spend |

**Finishing touches**

* **Event overlay:** on any timeseries widget → *Event Overlays* → search `tags:app:avionpulse`
  (monitor alerts) and `sources:avionpulse-chaos` (fault toggles), so cause and alert show up as
  vertical bars on every graph.
* **Time frame:** save the dashboard on *Past 1 hour* for demos; during a chaos run *Past 15 minutes*
  with live mode on.
* **Note widget** at the top (optional): "Toggle faults at https://\<site\>/#chaos — every fault
  auto-reverts after its TTL."

---

## 8. Verifying everything end to end

1. Deploy with Datadog enabled; browse every page once as `admin`.
2. **Serverless view** shows four functions with traces → instrumentation OK.
3. **Metrics Explorer**: `avionpulse.flights.airborne` has points every 5 min → custom metrics OK.
4. **Logs**: `service:avionpulse` returns JSON logs with `dd.trace_id` → logs and trace linking OK.
5. Wait about 15 minutes, then check `aws.dynamodb.consumed_read_capacity_units{tablename:avionpulse-data}`
   and `aws.sqs.approximate_number_of_messages_visible{queuename:avionpulse-events}` have data
   → AWS integration OK.
6. Work through the summary table in 6.7 one fault at a time (fault on → alert → fault off →
   recovery). The dashboard's *Chaos switches* bar and *Chaos activity* log stream give you the
   exact time each fault started, to measure time-to-detect.
7. When done: **RESET ALL** on the chaos page, and purge the DLQ in the SQS console.

**v3 additions**

8. **Serverless view** also lists `avionpulse-janitor` (an invocation every 2 min) and
   `avionpulse-processor` (traces from four trigger types).
9. **Metrics Explorer:** `sum:avionpulse.integration.received{*} by {channel}.as_count()` shows
   all five channels → the integration tier is OK.
10. **AWS metrics:** after about 15 min, `aws.events.invocations{rulename:avionpulse-flight-status}`,
    `aws.sns.number_of_notifications_delivered{topicname:avionpulse-crew-alerts}` and
    `aws.states.executions_succeeded{…turnaround…}` have data → the v3 namespaces are OK.
11. **CloudTrail:** toggle any v3 infra fault on and off, then search
    `source:cloudtrail @userIdentity.arn:*avionpulse-AppRole*`. Both calls appear within ~15 min.
12. **Events:** Events explorer `source:avionpulse-chaos` lists the same two toggles within seconds.
13. **Classic tier** (optional, ≈ $0.15): launch Minimal for 1 h. Within ~5 min of ONLINE, a host
    tagged `tier:classic` appears in Infrastructure, `service:avionpulse-gates` appears in APM,
    and opening the Gates page produces **one trace** from `avionpulse` (Lambda) into
    `avionpulse-gates` and `postgres.query`. Turn on `classic_nacl_db_block` → *Gate service
    errors* alerts while *Classic ALB unhealthy targets* stays OK. Tear down: the hosts disappear,
    and Classic monitors stay quiet (missing data → do not notify).
14. Work through the v3 rows of the 6.14 table the same way: fault on → alert → fault off → recovery.

---

## 9. v3: AWS infrastructure monitoring

v3 breaks AWS itself, so Datadog needs to see three more things: **the AWS resources' own
metrics**, **who changed what** (CloudTrail), and **hosts** for the Classic tier.

### 9.1 AWS integration scope

In the AWS integration tile → your account:

| Setting | Value | Why |
|---|---|---|
| Metric namespaces | Lambda, ApiGateway, DynamoDB, SQS, CloudFront, Events, **SNS, States, ApplicationELB, EC2, AutoScaling, RDS** | Every v3 fault's metric comes from one of these |
| Limit by tag | `app:avionpulse` | Classic resources carry `app:avionpulse` and `tier:classic` |
| **Resource collection** | On | Resource Catalog + CSM Misconfigurations see the security groups, bucket policies, KMS key state |
| **Events** | AWS Health, CloudWatch alarms | [AWS_RUNBOOKS.md §1](AWS_RUNBOOKS.md#1-aws-health-events) |
| Auto Scaling group metrics | Nothing to do — the Classic template turns on ASG group metrics collection (free) | `aws.autoscaling.group_in_service_instances` for `classic_asg_zero` |

Expect `aws.events.*`, `aws.sns.*`, `aws.states.*`, `aws.applicationelb.*`, `aws.rds.*` with
the usual 5–15 minute CloudWatch polling delay.

### 9.2 CloudTrail → Datadog (the "who changed what" half)

Every v3 infra fault is an AWS API call, made by the app's role, and recorded by CloudTrail.

1. If the account has no trail, create one (the first copy of management events is free;
   S3 storage costs cents): CloudTrail console → *Create trail* → management events, read **and** write.
2. Send it to Datadog, either:
   * **Datadog Forwarder**: install from the AWS integration tile, then add the trail's S3 bucket
     as a trigger (*Log collection → Automatically*), **or**
   * **Amazon Data Firehose** via the AWS integration's log-collection setup.
3. Verify: Logs → `source:cloudtrail @userIdentity.arn:*AppRole*` should show the app's `Describe*` calls.

**Useful searches** (save them as views):

```text
source:cloudtrail @userIdentity.arn:*<stack>-AppRole* -@evt.name:(Describe* OR Get* OR List*)
source:cloudtrail @evt.name:(UpdateTable OR DisableRule OR EnableRule OR UpdateIntegration OR PutParameter)
source:cloudtrail @evt.name:(DetachRolePolicy OR AttachRolePolicy OR DisableKey OR EnableKey OR SetQueueAttributes OR PutBucketPolicy)
source:cloudtrail @evt.name:(CreateStack OR DeleteStack) @requestParameters.stackName:*-classic
```

**Cloud SIEM** (if licensed) has out-of-the-box rules that fire on several v3 faults: KMS key
disabled (`kms_key_disabled`), IAM policy changes (`iam_policy_detached`), security group changes
(`classic_sg_lockdown`), and resource policy changes (`sqs_send_denied`, `s3_bucket_policy_deny`).
Run `security_incident` to see them correlate in one investigation.

**Chaos events.** Independently of CloudTrail, every toggle and every Classic launch or teardown
is posted to the Datadog Events API (`source:avionpulse-chaos`, tags `fault:<name>`,
`chaos_tier:<tier>`, `tier:classic`). Add it as an **event overlay** on every timeseries widget:
`sources:avionpulse-chaos`.

### 9.3 Classic tier hosts

With `EnableDatadog=true`, each Classic EC2 instance installs the Datadog Agent (API key from the
same SSM parameter), enables logs and processes, and runs the gate service under `ddtrace-run` as
`service:avionpulse-gates`. Hosts appear tagged `tier:classic`, `app:avionpulse` about 3 minutes
after the stack is `CREATE_COMPLETE`, and disappear on teardown.

* **Avoid false alerts when the tier is offline:** scope Classic monitors to `tier:classic` and
  set *missing data* to **"Do not notify"** (or **"Resolve"**). The tier is meant to vanish.
* **Distributed trace:** `/api/gates` → `avionpulse` (Lambda) → HTTP → `avionpulse-gates` (EC2) →
  `postgres`. Find it in APM → Traces, `service:avionpulse-gates`.
* **RDS Database Monitoring** is optional and billed per DB host; not configured by default.

### 9.4 Monitors for the v3 faults

Moved into §6 with full settings and test steps: [6.7](#67-before-you-set-up-the-v3-monitors)
(prerequisites and the "config changed" monitor), [6.8](#68-aws-control-plane),
[6.9](#69-security--iam), [6.10](#610-integration--eventbridge-bus--sns--step-functions--streams),
[6.11](#611-v3-composites), [6.12](#612-classic-tier), [6.13](#613-aws-fis-experiments), and the
summary in [6.14](#614-summary-table-all-49-faults).

### 9.5 Dashboard additions

The v3 widget groups (AWS infrastructure, Integrations, Classic tier) are in
[§7](#7-the-avionpulse-health-dashboard): Groups 7–9 in Option B, and already included in the
importable JSON.
