# AvionPulse v3 — Chaos Fault Scenarios

The Chaos Control page (`#chaos`, admin only) has **50 faults in 11 tiers** (47 individual faults + 3 composites).
v2's 23 faults are unchanged; v3 adds the **AWS control plane**, **Security & IAM**, **Integration**,
**Classic tier** and **AWS FIS** tiers. Fault state is a single DynamoDB item (`CONFIG / FAULTS`) shared by
every Lambda (and read by the Classic EC2 instances), cached for 3 seconds.

* **Request-path faults** change what the Lambda code does (sleep, allocate, raise, query differently).
* **Infra faults** (marked *MODIFIES AWS*) call an AWS control-plane API to really change a
  resource, and put it back when you switch them off.
* **Every fault has a TTL** (5–120 min, default 30). A dedicated **janitor Lambda** runs every
  2 minutes on its own EventBridge rule (no fault touches it) and reverts anything expired,
  including infra changes. **RESET ALL** reverts every infra fault whether or not it's marked active.
* `/api/chaos/*`, `/api/auth/*`, `/api/classic/*` and `/api/aws/*` are exempt from request-path
  faults and use their **own API Gateway integration and throttle**, so `api_integration_broken`
  can't lock you out of the switch that undoes it.
* Every toggle is logged (`[CHAOS] … ON/OFF`) and, when Datadog is enabled, posted to the Datadog
  **Events API** (`source:avionpulse-chaos`). Use it as a dashboard overlay so the cause sits next to the symptom.
* If everything is broken: `python scripts/restore.py --stack <name>` restores all core resources
  with the AWS CLI, no app needed.

## Edge tier — CloudFront / API Gateway

| Fault | What happens | Datadog signal | Suggested monitor |
|---|---|---|---|
| `api_throttling` *(infra)* | `apigatewayv2:UpdateStage` sets the `$default` stage to 1 req/s, burst 2. API Gateway returns **429** before Lambda runs. | API Gateway 4xx up while Lambda invocations drop | `sum:aws.apigateway.4xx{apiid:<id>}.as_count() > 20` and a composite with `aws.lambda.invocations` falling |
| `latency_spike` | 2–6 s sleep on every request. | `aws.apigateway.integration_latency`, Lambda duration, APM p95 | `avg(last_5m):p95:trace.aws.lambda{service:avionpulse} > 2` |
| `random_500s` | 35% of requests raise an **unhandled** exception → Lambda Error, API Gateway bare 500. | `aws.lambda.errors`, APM error rate, Error Tracking issue | SLO burn-rate alert on `avionpulse` availability |
| `health_check_fail` | `/api/health` returns 503; everything else is fine. | Synthetics API test fails | Synthetics test on `/api/health` |
| `status_page_error` | The **public status page** (`/status`, no login) and its JSON (`/api/status`) return **500**. The app keeps working; only status-page probes notice. | Agent HTTP check `http.can_connect` CRITICAL; Synthetics API test fails; log `[FAULT] status_page_error` | HTTP check service-check monitor + Synthetic API test on `/status` ([DATADOG_SETUP.md §10](DATADOG_SETUP.md#10-status-page-http-check-and-synthetics)) |

## Compute tier — Lambda

| Fault | What happens | Datadog signal |
|---|---|---|
| `lambda_timeout` | 30% of requests sleep past the 10 s timeout → `Task timed out`, API Gateway 503. | `aws.lambda.enhanced.timeouts`, duration pinned at 10 s |
| `lambda_oom` | 25% of requests allocate 32 MB chunks until the 256 MB runtime is killed (`Runtime.OutOfMemory`). | `aws.lambda.enhanced.out_of_memory`, `max_memory_used` at limit |
| `memory_leak` | Each request keeps 8 MB alive in a module-level list. Memory climbs per warm container until it OOMs and recycles — a sawtooth. | `aws.lambda.enhanced.max_memory_used` (forecast monitor) |
| `cpu_burn` | 400 ms busy-wait per request. 256 MB Lambdas get a fraction of a vCPU, so it shows as duration. | `aws.lambda.enhanced.billed_duration`, `estimated_cost` |
| `cold_start_storm` *(infra)* | `lambda:UpdateFunctionConfiguration` changes an env var (every warm container is replaced) and sets a 2.5 s sleep in init. Repeats every simulator run while active. The topbar badge shows **COLD START**. | `aws.lambda.enhanced.init_duration`, `cold_start:true` spans |
| `worker_throttle` *(infra)* | `lambda:PutFunctionConcurrency` = 0 on the SQS worker. Polls are throttled, the queue grows. | `aws.lambda.throttles{functionname:avionpulse-worker}`, queue depth |

## Data tier — DynamoDB / S3

| Fault | What happens | Datadog signal |
|---|---|---|
| `dynamodb_throttle` | Every data request runs 25 strongly-consistent full-table Scans against the 5 RCU table. Burst credit drains, then **real** `ProvisionedThroughputExceededException` → 503. | `aws.dynamodb.read_throttle_events`, `consumed_read_capacity_units` > provisioned, APM error type |
| `full_table_scan` | Flights endpoint uses `Scan` + filter instead of `Query`. Same response, more RCUs. | APM span name changes from `Query` to `Scan`; RCU climbs |
| `n_plus_one` | A `GetItem` per flight for its aircraft and origin airport — 50+ DynamoDB spans per request. | Trace waterfall; span count per trace |
| `iam_access_denied` | Dashboard calls `DescribeTable` on `<stack>-data-restricted-ledger`, which the role has no rights to. **Real** `AccessDeniedException`. | APM error `AccessDeniedException`; log pattern `is not authorized to perform` |
| `s3_missing_object` | NOTAM board reads `data/notams-v2.json`, which was never uploaded. S3 `NoSuchKey` → 502. (The role has `s3:ListBucket`, so S3 answers 404, not 403.) | S3 span error `NoSuchKey`; NOTAM page shows the error card |

## Async tier — SQS / EventBridge

| Fault | What happens | Datadog signal |
|---|---|---|
| `sqs_poison_pill` | API (50% of data requests) and simulator (5 per run) enqueue malformed messages. The worker reports them as batch item failures; SQS redelivers 3× then moves them to the **DLQ**. | `aws.sqs.approximate_number_of_messages_visible{queuename:avionpulse-events-dlq} > 0`, log `Poison message` |
| `queue_backlog` *(infra)* | `lambda:UpdateEventSourceMapping Enabled=false` pauses the worker trigger, and the simulator floods 20 extra events per run. | `aws.sqs.approximate_age_of_oldest_message` climbing |
| `simulator_crash` | The scheduled simulator writes half the flights and raises. Lambda async retry fires once more; data goes stale; business metrics stop. Dashboard "Data freshness" turns red. | `aws.lambda.errors{functionname:avionpulse-simulator}`; **no data** on `avionpulse.flights.airborne` |
| `downstream_timeout` | Weather panel calls a "third-party METAR provider" at an unroutable address. Each call hangs 3 s → 504. | APM `http.request` span timing out; Error Tracking groups `URLError` |

## Business logic — silent failures

| Fault | What happens | Why it matters |
|---|---|---|
| `silent_failure` | The flights query uses partition key `FLIGHTS` instead of `FLIGHT`. Valid query, zero rows, **HTTP 200, no errors, normal latency**. The board is empty, airborne = 0. | Infrastructure and APM stay green. Only a business-metric monitor (`avionpulse.flights.airborne == 0`), `avionpulse.api.flights_returned`, or a Synthetics assertion on page content catches it. |
| `pax_corruption` | Passenger counts ×847 in responses and metrics. | Anomaly monitor on `avionpulse.pax.airborne`. Nothing technical is wrong. |
| `log_flood` | 50 WARNING lines per request. | Log volume anomaly. **The one fault that can cost real money if left on** (see [COST.md](COST.md)). |

## AWS control plane — configuration drift

These faults make the kind of change a person or a pipeline makes through the AWS API. Each one
leaves a **CloudTrail management event**. Pair the symptom monitor with a CloudTrail log search (or
Datadog Change Tracking) to answer "what changed?" The **AWS Resources** page shows the drift live.

| Fault | AWS change (and revert) | What breaks | Datadog signal |
|---|---|---|---|
| `dynamodb_capacity_cut` | `dynamodb:UpdateTable` 5/5 → 1/1 RCU/WCU | The simulator's paced writes throttle; reads start failing under normal traffic | `aws.dynamodb.provisioned_*_capacity_units` step down, `write_throttle_events`; CloudTrail `UpdateTable` |
| `eventbridge_rule_disabled` | `events:DisableRule` on `<stack>-simulator-tick` | Nothing errors. The job simply stops: data freezes, business metrics go silent | **no-data** monitor on `aws.lambda.invocations{functionname:<stack>-simulator}` / `avionpulse.flights.airborne`; CloudTrail `DisableRule` |
| `api_integration_broken` | `apigatewayv2:UpdateIntegration` → api function `:chaos-missing-alias` | Every data route returns 500 from API Gateway; the api function's invocations drop to 0 | `aws.apigateway.5xx` up while `aws.lambda.invocations` falls; access log `integration_error` "Function not found" |
| `lambda_bad_deploy` | Worker handler → `worker_v2.handler` (via `DD_LAMBDA_HANDLER` when Datadog wraps it) | `Runtime.ImportModuleError` on every SQS batch; messages go to the DLQ after 3 receives | `aws.lambda.errors{functionname:<stack>-worker}` 100%, DLQ depth; CloudTrail `UpdateFunctionConfiguration20150331v2` |
| `ssm_config_poisoned` | `ssm:PutParameter` truncated JSON into `/<stack>/config/flags` | Within 15 s, Dashboard and Flights return 500 `ConfigParseError` | APM error type `ConfigError`; CloudTrail `PutParameter` just before the error spike |
| `sqs_retention_cut` | Retention 86400 → 60 s **and** the worker trigger paused | Messages expire unprocessed. Queue depth stays low, so a depth monitor stays green | `aws.sqs.number_of_messages_sent` ≫ `number_of_messages_deleted`; `avionpulse.events.processed` → 0 |

## Security & IAM

| Fault | AWS change (and revert) | What breaks | Datadog signal |
|---|---|---|---|
| `iam_policy_detached` | `iam:DetachRolePolicy` on the worker role's data policy (the app may only detach *that* policy) | The worker still polls SQS, but every DynamoDB write is `AccessDeniedException` | Error Tracking (worker) `AccessDeniedException`; Cloud SIEM "IAM policy detached / modified"; DLQ grows |
| `kms_key_disabled` | `kms:DisableKey` on the queue's customer-managed key | `SendMessage` → `KMS.DisabledException` (maintenance requests 503); the worker can't decrypt. Takes up to ~60 s (data-key cache) | APM `KMS.DisabledException`; Cloud SIEM "KMS key disabled"; `aws.kms` CloudTrail `DisableKey` |
| `sqs_send_denied` | `sqs:SetQueueAttributes` adds a resource policy denying `SendMessage` to the app role | IAM allows it, the queue denies it. `AccessDenied` on every enqueue | APM `AccessDenied` on `sqs.SendMessage`; CloudTrail `SetQueueAttributes` |
| `s3_bucket_policy_deny` | `s3:PutBucketPolicy` on the recorder bucket denies `PutObject` to the app role | Recorder uploads fail; the Integrations page's recorder channel goes stale | APM `AccessDenied` on `s3.PutObject`; CloudTrail `PutBucketPolicy`; CSM resource-policy finding |

## Integration — EventBridge bus / SNS / Step Functions / Streams

| Fault | Mechanism | What breaks | Datadog signal |
|---|---|---|---|
| `eb_target_broken` | `events:PutTargets` repoints the `FlightStatusChanged` rule at a missing alias | `PutEvents` succeeds and delivery fails (2 retries, 5 min max age) | `aws.events.failed_invocations{rulename:<stack>-flight-status}` > 0 |
| `sns_delivery_failure` | `lambda:RemovePermission` deletes the processor's SNS invoke statement | `Publish` succeeds and SNS can't invoke the subscriber. Alerts are retried and arrive late after the revert | `aws.sns.number_of_notifications_failed`; processor's SNS invocations → 0 |
| `stepfn_task_failure` | Crew-check task raises (50%) or sleeps past its 5 s `TimeoutSeconds` (50%) | Turnaround executions fail; the Integrations "turnaround" channel goes stale | `aws.states.executions_failed`, `executions_timed_out` |
| `stream_processor_poison` | The processor rejects every DynamoDB stream batch | Lambda retries the same shard position; the stream backs up behind it | `aws.lambda.iterator_age{functionname:<stack>-processor}` climbing |

## Classic tier — EC2 / ALB / RDS (on demand)

Available only while the Classic stack is **online**. Launch it from the card at the top of the
Chaos page. Every Classic resource is tagged `avionpulse:classic=<stack>`, `tier:classic`.

| Fault | AWS change (and revert) | What breaks | Datadog signal |
|---|---|---|---|
| `classic_sg_lockdown` | `ec2:RevokeSecurityGroupIngress` (ALB → :8080) | Health checks time out, targets go unhealthy, ALB 502/504 | `aws.applicationelb.un_healthy_host_count`, `httpcode_elb_5xx`; CloudTrail `RevokeSecurityGroupIngress` |
| `classic_nacl_db_block` | `ec2:CreateNetworkAclEntry` rule 10 deny TCP 5432 into the DB subnets | Health checks stay **green** (they skip the DB) while every gate request times out | APM postgres connect errors on `avionpulse-gates`; `aws.rds.database_connections` drops; healthy hosts unchanged |
| `classic_asg_zero` | `autoscaling:UpdateAutoScalingGroup` min/desired → 0 | Instances terminate, ALB 503. Revert relaunches (~3 min) | `healthy_host_count` = 0, `aws.autoscaling.group_in_service_instances` = 0, host no-data |
| `classic_alb_maintenance` | `elasticloadbalancing:ModifyRule` forward → fixed 503 | Healthy instances receive no traffic | `httpcode_elb_5xx` up with `request_count` to targets = 0 |
| `classic_rds_reboot` | `rds:RebootDBInstance` (ForceFailover on Full) — one shot | DB unavailable 1–3 min, then recovers | RDS event (reboot / Multi-AZ failover); connection-error spike |
| `classic_db_slow_query` | Request-path: the EC2 service adds `pg_sleep(2.5)` | Latency triples; CPU and connections look normal | APM postgres span duration; `target_response_time` p95 |

## AWS FIS experiments (Classic tier)

Switching one on calls `fis:StartExperiment` on a template the core stack created. The templates
target resources by tag, so they cost nothing until used (FIS bills $0.10 per action-minute).
Switching it off calls `fis:StopExperiment`. Experiments end on their own after 5–6 minutes.

| Fault | FIS action | Datadog signal |
|---|---|---|
| `fis_cpu_stress` | `aws:ssm:send-command` AWSFIS-Run-CPU-Stress, all instances, 5 min | `system.cpu.user` ≈ 100% on `tier:classic` hosts; `aws.ec2.cpuutilization`; CPU credit balance falls |
| `fis_network_latency` | AWSFIS-Run-Network-Latency, +300 ms on `ens5`, 5 min | ALB `target_response_time` jump; every postgres round trip +300 ms in APM |
| `fis_instance_terminate` | `aws:ec2:terminate-instances`, one instance | EC2 state-change event; ASG replaces it; host swap in the Infrastructure list |
| `fis_az_outage` *(Full only)* | `aws:network:disrupt-connectivity` on AZ-a subnets 5 min + `aws:rds:reboot-db-instances` forceFailover | Healthy hosts halve, RDS fails over to AZ-b. The closest thing to an AZ failure you can cause |

## Composite

| Fault | Members |
|---|---|
| `cascading_failure` | `latency_spike` + `dynamodb_throttle` + `random_500s` + `queue_backlog` + `log_flood` |
| `security_incident` | `iam_policy_detached` + `kms_key_disabled` + `sqs_send_denied` + `s3_bucket_policy_deny` |
| `control_plane_storm` | `dynamodb_capacity_cut` + `eventbridge_rule_disabled` + `lambda_bad_deploy` + `ssm_config_poisoned` |

## Demo storylines

1. **"Green dashboards, broken app"** — turn on `silent_failure`. Show APM: 0% errors, normal
   latency. Show the Flights page: empty. Then show the business metric monitor firing.
2. **"Is it us or AWS?"** — turn on `api_throttling`. Users see errors, but Lambda invocations
   *drop*. API Gateway 4xx and a composite monitor tell the real story.
3. **"The queue nobody watches"** — turn on `sqs_poison_pill`. The UI works perfectly; the DLQ
   counter on the Ops Pipeline page climbs; the DLQ monitor fires.
4. **"Cold starts after a deploy"** — turn on `cold_start_storm` and refresh the dashboard. Every
   container is new; show `init_duration` and the cold-start spans in the trace flame graph.
5. **"The tightened policy"** — `iam_access_denied`. A real `AccessDeniedException` with the exact
   ARN in the error message; Error Tracking groups it as a new issue.
6. **"Who changed what?"** — turn on `control_plane_storm`. Four symptoms appear in four places.
   Open the AWS Resources page and CloudTrail in Datadog Logs (`@evt.name:(UpdateTable OR DisableRule
   OR UpdateFunctionConfiguration20150331v2 OR PutParameter)`). Every symptom has a change one minute before it.
7. **"It's not the code, it's the key"** — `kms_key_disabled`. App errors say `KMS.DisabledException`;
   Cloud SIEM raises the security signal; the fix is in KMS, not in the deploy pipeline.
8. **"The queue that looks healthy"** — `sqs_retention_cut`. Depth stays near zero, so a naive depth
   monitor stays green. Only sent-vs-deleted (or the business metric) shows the data loss.
9. **"Healthy hosts, broken app"** — launch Classic, then `classic_nacl_db_block`. ALB health is
   100%, the Gates page times out, and APM shows the postgres connect timeouts.
10. **"Lose an AZ"** — launch Classic **Full**, then `fis_az_outage`. Watch healthy hosts halve, RDS
    fail over and the service recover. Run it next to the Datadog AWS Health / RDS events stream.
11. **"Is the status page lying?"** — Status Page 500 on. The app works for signed-in users, and
    the browser test as `viewer` stays green, but the public status page is down. The Agent HTTP
    check alerts in about a minute and the Synthetic API test on its next run. Show which probe
    noticed first, and from where.
