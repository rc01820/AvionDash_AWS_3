"""AvionPulse chaos engine.

Fault state lives in a single DynamoDB item (CONFIG / FAULTS) so every Lambda
(api, worker, simulator) sees the same switches. Each Lambda caches it for a
few seconds to keep read capacity low.

Three kinds of fault:
  * request-path faults: code behaviour inside the Lambda (sleep, allocate, raise…)
  * infra faults (infra=True): the toggle calls an AWS control-plane API to
    really change a resource (stage throttle, reserved concurrency, table
    capacity, rule state, KMS key state, IAM attachment, resource policies…).
    Turning the fault off — or its TTL expiring — puts the resource back.
  * classic faults (classic=True): infra faults and AWS FIS experiments against
    the on-demand Classic tier (EC2 / ALB / RDS). They can only be switched on
    while that stack is online.

Every fault has a TTL. The janitor Lambda runs on its own EventBridge schedule
(not the simulator's, which a fault can disable) and reverts anything that has
expired, so a forgotten experiment can't keep burning money or alert noise.
"""
import logging
import time

import config
import infra
import telemetry
from store import err_code, get_store

log = logging.getLogger("avionpulse.chaos")

TIERS = {
    "edge": "EDGE TIER — CloudFront / API Gateway",
    "compute": "COMPUTE TIER — AWS Lambda",
    "data": "DATA TIER — DynamoDB / S3",
    "async": "ASYNC TIER — SQS / EventBridge",
    "business": "BUSINESS LOGIC — silent failures",
    "control": "AWS CONTROL PLANE — configuration drift",
    "security": "SECURITY & IAM — KMS / IAM / resource policies",
    "integration": "INTEGRATION — EventBridge bus / SNS / Step Functions / Streams",
    "classic": "CLASSIC TIER — EC2 / ALB / RDS (on demand)",
    "fis": "AWS FIS EXPERIMENTS — Classic tier",
    "composite": "COMPOSITE SCENARIOS",
}

CATALOG = {
    # ── Edge ─────────────────────────────────────────────────────────────
    "api_throttling": {
        "label": "API Gateway Throttling", "tier": "edge", "severity": "critical", "aws": "API Gateway",
        "infra": True, "icon": "🚦",
        "description": "Calls apigatewayv2:UpdateStage to drop the $default stage to 1 req/s (burst 2). "
                       "Browsers start receiving HTTP 429 Too Many Requests from API Gateway itself — the Lambda never runs.",
        "datadog_signal": "aws.apigateway.4xx spike while aws.lambda.invocations drops → composite monitor",
    },
    "latency_spike": {
        "label": "Latency Spike", "tier": "edge", "severity": "warning", "aws": "API Gateway + Lambda",
        "infra": False, "icon": "🐢",
        "description": "Adds a 2–6 s sleep to every API request. API Gateway IntegrationLatency and Lambda duration climb together.",
        "datadog_signal": "aws.apigateway.integration_latency p95 / trace.aws.lambda duration anomaly",
    },
    "random_500s": {
        "label": "Unhandled Exceptions", "tier": "edge", "severity": "warning", "aws": "Lambda",
        "infra": False, "icon": "💥",
        "description": "35% of requests raise an unhandled exception. Lambda counts an Error and API Gateway returns a bare 500.",
        "datadog_signal": "aws.lambda.errors + APM error rate → SLO burn-rate alert",
    },
    "health_check_fail": {
        "label": "Health Check Failure", "tier": "edge", "severity": "critical", "aws": "API Gateway",
        "infra": False, "icon": "🩺",
        "description": "/api/health returns 503. Everything else keeps working — only an external probe notices.",
        "datadog_signal": "Synthetics API test FAIL → downtime alert",
    },
    "status_page_error": {
        "label": "Status Page 500", "tier": "edge", "severity": "critical", "aws": "CloudFront + API Gateway + Lambda",
        "infra": False, "icon": "📟",
        "description": "The public status page (/status, and its JSON at /api/status) returns HTTP 500. The app itself keeps "
                       "working, so only probes that watch the status page notice: a Synthetics HTTP test and an Agent HTTP check.",
        "datadog_signal": "Synthetics API test on /status FAIL; Agent http_check http.can_connect CRITICAL / network.http.response_time gap",
    },
    # ── Compute ──────────────────────────────────────────────────────────
    "lambda_timeout": {
        "label": "Lambda Timeout", "tier": "compute", "severity": "critical", "aws": "Lambda",
        "infra": False, "icon": "⏱",
        "description": "30% of requests sleep past the 10 s function timeout. Lambda logs 'Task timed out' and API Gateway answers 503.",
        "datadog_signal": "aws.lambda.enhanced.timeouts → monitor; duration pinned at max",
    },
    "lambda_oom": {
        "label": "Out of Memory", "tier": "compute", "severity": "critical", "aws": "Lambda",
        "infra": False, "icon": "🧨",
        "description": "25% of requests allocate past the 256 MB memory size until the runtime is killed (Runtime.OutOfMemory).",
        "datadog_signal": "aws.lambda.enhanced.out_of_memory + max_memory_used at limit",
    },
    "memory_leak": {
        "label": "Warm-Container Memory Leak", "tier": "compute", "severity": "warning", "aws": "Lambda",
        "infra": False, "icon": "🧠",
        "description": "Each request keeps 8 MB alive in a module-level list. Memory grows per warm container until it OOMs and recycles.",
        "datadog_signal": "aws.lambda.enhanced.max_memory_used sawtooth → forecast monitor",
    },
    "cpu_burn": {
        "label": "CPU Burn", "tier": "compute", "severity": "warning", "aws": "Lambda",
        "infra": False, "icon": "🔥",
        "description": "Busy-waits 400 ms per request. At 256 MB the function gets a fraction of a vCPU, so billed duration jumps.",
        "datadog_signal": "aws.lambda.enhanced.billed_duration / estimated_cost increase",
    },
    "cold_start_storm": {
        "label": "Cold Start Storm", "tier": "compute", "severity": "warning", "aws": "Lambda",
        "infra": True, "icon": "🥶",
        "description": "lambda:UpdateFunctionConfiguration touches an env var (every warm container is replaced) and adds a "
                       "2.5 s sleep to init. Repeats on every simulator run while active.",
        "datadog_signal": "aws.lambda.enhanced.init_duration + cold_start:true spans in traces",
    },
    "worker_throttle": {
        "label": "Worker Concurrency = 0", "tier": "compute", "severity": "critical", "aws": "Lambda + SQS",
        "infra": True, "icon": "⛔",
        "description": "lambda:PutFunctionConcurrency sets the SQS worker's reserved concurrency to 0. Every poll is throttled; messages pile up.",
        "datadog_signal": "aws.lambda.throttles (worker) + aws.sqs.approximate_number_of_messages_visible",
    },
    # ── Data ─────────────────────────────────────────────────────────────
    "dynamodb_throttle": {
        "label": "DynamoDB Read Storm", "tier": "data", "severity": "critical", "aws": "DynamoDB",
        "infra": False, "icon": "🌪",
        "description": "Every data request fires 25 strongly-consistent full-table Scans against a 5 RCU table. "
                       "Burst capacity drains in seconds, then real ProvisionedThroughputExceededException errors surface as 503s.",
        "datadog_signal": "aws.dynamodb.read_throttle_events + consumed_read_capacity_units > provisioned",
    },
    "full_table_scan": {
        "label": "Scan Instead of Query", "tier": "data", "severity": "warning", "aws": "DynamoDB",
        "infra": False, "icon": "🔍",
        "description": "The flights endpoint uses Scan + client-side filter instead of a key Query. Same answer, many more RCUs.",
        "datadog_signal": "APM span aws.dynamodb Scan replaces Query; consumed RCU climbs",
    },
    "n_plus_one": {
        "label": "N+1 GetItem Calls", "tier": "data", "severity": "warning", "aws": "DynamoDB",
        "infra": False, "icon": "🔁",
        "description": "Flights list does a separate GetItem for each row's aircraft and origin airport — 50+ calls instead of 1.",
        "datadog_signal": "Trace waterfall with dozens of identical aws.dynamodb GetItem spans",
    },
    "iam_access_denied": {
        "label": "IAM Permission Denied", "tier": "data", "severity": "critical", "aws": "IAM + DynamoDB",
        "infra": False, "icon": "🔐",
        "description": "The dashboard reads a ledger table the Lambda role has no permission for — a real AccessDeniedException, like a policy someone tightened in a hurry.",
        "datadog_signal": "APM error type AccessDeniedException; log pattern 'is not authorized to perform'",
    },
    "s3_missing_object": {
        "label": "S3 Object Missing", "tier": "data", "severity": "warning", "aws": "S3",
        "infra": False, "icon": "🪣",
        "description": "NOTAM board reads data/notams-v2.json, which was never uploaded. S3 returns NoSuchKey (404) and the panel shows 502.",
        "datadog_signal": "aws.s3 GetObject span error NoSuchKey; aws.s3.4xx_errors (request metrics)",
    },
    # ── Async ────────────────────────────────────────────────────────────
    "sqs_poison_pill": {
        "label": "SQS Poison Messages", "tier": "async", "severity": "warning", "aws": "SQS",
        "infra": False, "icon": "☠",
        "description": "API and simulator enqueue malformed messages. The worker fails them, SQS redelivers 3 times, then moves them to the DLQ.",
        "datadog_signal": "aws.sqs.approximate_number_of_messages_visible on the DLQ > 0 → alert",
    },
    "queue_backlog": {
        "label": "Worker Trigger Paused", "tier": "async", "severity": "critical", "aws": "SQS + Lambda",
        "infra": True, "icon": "📥",
        "description": "lambda:UpdateEventSourceMapping disables the SQS trigger and the simulator floods 20 extra events per run. Queue age climbs steadily.",
        "datadog_signal": "aws.sqs.approximate_age_of_oldest_message threshold monitor",
    },
    "simulator_crash": {
        "label": "Scheduled Job Crash", "tier": "async", "severity": "critical", "aws": "EventBridge + Lambda",
        "infra": False, "icon": "🛑",
        "description": "The EventBridge-scheduled simulator crashes after writing half the flights. Async retries fire, data goes stale, business metrics stop.",
        "datadog_signal": "aws.lambda.errors (simulator) + 'no data' on avionpulse.flights.airborne",
    },
    "downstream_timeout": {
        "label": "Weather API Timeout", "tier": "async", "severity": "warning", "aws": "Lambda (egress)",
        "infra": False, "icon": "⛈",
        "description": "The weather panel calls a third-party METAR provider that never answers. Each call hangs 3 s and fails with 504.",
        "datadog_signal": "APM http.request span to 10.255.255.1 timing out; error tracking groups the timeout",
    },
    # ── Business ─────────────────────────────────────────────────────────
    "silent_failure": {
        "label": "Silent Partition-Key Bug", "tier": "business", "severity": "critical", "aws": "DynamoDB",
        "infra": False, "icon": "🤫",
        "description": "A 'deploy' changes the flights key to FLIGHTS. Queries succeed with zero rows: HTTP 200, no errors, no latency change — "
                       "but the board is empty and airborne count is 0.",
        "datadog_signal": "ONLY business metric avionpulse.flights.airborne == 0 or a Synthetics content assertion catches it",
    },
    "pax_corruption": {
        "label": "Passenger Count Corruption", "tier": "business", "severity": "warning", "aws": "Lambda",
        "infra": False, "icon": "🧮",
        "description": "Passenger counts are multiplied by 847 in responses and metrics. Infrastructure is green; the numbers are absurd.",
        "datadog_signal": "Anomaly monitor on avionpulse.pax.airborne",
    },
    "log_flood": {
        "label": "Log Flood", "tier": "business", "severity": "warning", "aws": "CloudWatch Logs",
        "infra": False, "icon": "📜",
        "description": "Emits 50 WARNING lines per request. Watch log ingestion — this is the one fault that can cost real money if left on.",
        "datadog_signal": "logs volume anomaly; aws.logs.incoming_bytes",
    },
    # ── AWS control plane ────────────────────────────────────────────────
    "dynamodb_capacity_cut": {
        "label": "Table Capacity Cut", "tier": "control", "severity": "critical", "aws": "DynamoDB",
        "infra": True, "icon": "📉",
        "description": "dynamodb:UpdateTable drops the table from 5/5 to 1 RCU / 1 WCU — someone 'right-sized' it. "
                       "The simulator's 25 paced writes now throttle and dashboard reads start failing. "
                       "(AWS limits capacity decreases per day; extra toggles may be refused.)",
        "datadog_signal": "aws.dynamodb.provisioned_write_capacity_units step down + write_throttle_events; "
                          "CloudTrail UpdateTable",
    },
    "eventbridge_rule_disabled": {
        "label": "Scheduler Rule Disabled", "tier": "control", "severity": "critical", "aws": "EventBridge",
        "infra": True, "icon": "⏸",
        "description": "events:DisableRule on the simulator's 5-minute schedule. Nothing errors — the job just stops running. "
                       "Flights freeze, business metrics stop. (The janitor has its own rule, so it still reverts.)",
        "datadog_signal": "'no data' on aws.lambda.invocations{functionname:*-simulator} and avionpulse.flights.airborne; "
                          "CloudTrail DisableRule",
    },
    "api_integration_broken": {
        "label": "API Integration Misrouted", "tier": "control", "severity": "critical", "aws": "API Gateway + Lambda",
        "infra": True, "icon": "🔀",
        "description": "apigatewayv2:UpdateIntegration points the data routes at a Lambda alias that doesn't exist. "
                       "API Gateway returns 500 on every data call while the api function's invocations drop to zero. "
                       "Login, chaos and Classic routes use a separate integration and keep working.",
        "datadog_signal": "aws.apigateway.5xx up while aws.lambda.invocations{functionname:*-api} falls; "
                          "access-log integration_error 'Function not found'",
    },
    "lambda_bad_deploy": {
        "label": "Bad Worker Deploy", "tier": "control", "severity": "critical", "aws": "Lambda",
        "infra": True, "icon": "📦",
        "description": "lambda:UpdateFunctionConfiguration sets the worker's handler to worker_v2.handler, a module that "
                       "was never shipped. Every SQS batch fails at init (Runtime.ImportModuleError) and messages drain to the DLQ.",
        "datadog_signal": "aws.lambda.errors{functionname:*-worker} at 100%, DLQ depth rising; "
                          "Change Tracking / CloudTrail UpdateFunctionConfiguration",
    },
    "ssm_config_poisoned": {
        "label": "Feature Flags Corrupted", "tier": "control", "severity": "warning", "aws": "SSM Parameter Store",
        "infra": True, "icon": "🧾",
        "description": "ssm:PutParameter writes truncated JSON to the feature-flag parameter. Within 15 s (flag cache) "
                       "the dashboard and flight board return 500 ConfigParseError.",
        "datadog_signal": "APM error type ConfigError on /api/flights; CloudTrail PutParameter just before the error spike",
    },
    "sqs_retention_cut": {
        "label": "Queue Retention 60 s", "tier": "control", "severity": "warning", "aws": "SQS + Lambda",
        "infra": True, "icon": "⌛",
        "description": "sqs:SetQueueAttributes sets retention to 60 s and pauses the worker trigger. Messages expire "
                       "unprocessed: queue depth stays LOW while events are silently lost.",
        "datadog_signal": "aws.sqs.number_of_messages_sent ≫ number_of_messages_deleted with flat depth; "
                          "avionpulse.events.processed drops to 0",
    },
    # ── Security & IAM ───────────────────────────────────────────────────
    "iam_policy_detached": {
        "label": "Worker Policy Detached", "tier": "security", "severity": "critical", "aws": "IAM + Lambda",
        "infra": True, "icon": "🪪",
        "description": "iam:DetachRolePolicy removes the worker role's data-access policy. The worker can still poll SQS "
                       "but every DynamoDB write is AccessDenied; batches fail and redrive to the DLQ.",
        "datadog_signal": "Error Tracking AccessDeniedException (worker); Cloud SIEM 'IAM policy detached from role'",
    },
    "kms_key_disabled": {
        "label": "KMS Key Disabled", "tier": "security", "severity": "critical", "aws": "KMS + SQS",
        "infra": True, "icon": "🗝",
        "description": "kms:DisableKey on the customer-managed key that encrypts the events queue. SendMessage fails with "
                       "KMS.DisabledException and the worker can't decrypt. SQS caches data keys for up to 60 s, so errors start within a minute.",
        "datadog_signal": "APM error KMS.DisabledException on sqs.SendMessage; Cloud SIEM 'KMS key disabled'; "
                          "aws.sqs.number_of_messages_sent drops",
    },
    "sqs_send_denied": {
        "label": "Queue Policy Deny", "tier": "security", "severity": "warning", "aws": "SQS",
        "infra": True, "icon": "🚷",
        "description": "sqs:SetQueueAttributes adds a resource policy that explicitly denies SendMessage to the app role. "
                       "Maintenance requests and simulator events fail with AccessDenied — IAM looks fine, the queue says no.",
        "datadog_signal": "APM error AccessDenied on sqs.SendMessage; CloudTrail SetQueueAttributes",
    },
    "s3_bucket_policy_deny": {
        "label": "Recorder Bucket Locked", "tier": "security", "severity": "warning", "aws": "S3",
        "infra": True, "icon": "🔒",
        "description": "s3:PutBucketPolicy on the flight-data recorder bucket denies PutObject to the app role. "
                       "The simulator's snapshot uploads fail; the recorder channel on the Integrations page goes stale.",
        "datadog_signal": "APM error AccessDenied on s3.PutObject; CloudTrail PutBucketPolicy (CSM misconfiguration rule)",
    },
    # ── Integration ──────────────────────────────────────────────────────
    "eb_target_broken": {
        "label": "Bus Target Broken", "tier": "integration", "severity": "warning", "aws": "EventBridge",
        "infra": True, "icon": "🎯",
        "description": "events:PutTargets repoints the FlightStatusChanged rule at a missing Lambda alias. "
                       "PutEvents still succeeds — delivery fails after 2 retries.",
        "datadog_signal": "aws.events.failed_invocations{rulename:*-flight-status} > 0 while aws.events.invocations drops",
    },
    "sns_delivery_failure": {
        "label": "SNS Delivery Failure", "tier": "integration", "severity": "warning", "aws": "SNS + Lambda",
        "infra": True, "icon": "📭",
        "description": "lambda:RemovePermission deletes the processor's resource policy statement for SNS. Publish still "
                       "succeeds; SNS can't invoke the subscriber. Crew alerts stop arriving (they're retried and delivered late after revert).",
        "datadog_signal": "aws.sns.number_of_notifications_failed > 0; processor invocations from SNS drop to 0",
    },
    "stepfn_task_failure": {
        "label": "Turnaround Workflow Failing", "tier": "integration", "severity": "warning", "aws": "Step Functions",
        "infra": False, "icon": "🧩",
        "description": "The crew-check task of the Express turnaround workflow fails: half the time it raises, half the time "
                       "it hangs past its 5 s TimeoutSeconds (States.Timeout).",
        "datadog_signal": "aws.states.executions_failed + executions_timed_out (express); processor errors",
    },
    "stream_processor_poison": {
        "label": "Stream Processor Stuck", "tier": "integration", "severity": "critical", "aws": "DynamoDB Streams + Lambda",
        "infra": False, "icon": "🌊",
        "description": "The DynamoDB stream consumer rejects every batch. Lambda retries the same shard position, so the "
                       "stream backs up instead of moving on — the classic 'iterator age' incident.",
        "datadog_signal": "aws.lambda.iterator_age{functionname:*-processor} climbing; processor error rate 100%",
    },
    # ── Classic tier (EC2 / ALB / RDS) ───────────────────────────────────
    "classic_sg_lockdown": {
        "label": "Security Group Lockdown", "tier": "classic", "severity": "critical", "aws": "EC2 + ALB",
        "infra": True, "classic": True, "icon": "🧱",
        "description": "ec2:RevokeSecurityGroupIngress removes the ALB → instance rule on port 8080. Health checks time "
                       "out, targets go unhealthy, the ALB answers 502/504.",
        "datadog_signal": "aws.applicationelb.un_healthy_host_count > 0, httpcode_elb_5xx; CloudTrail RevokeSecurityGroupIngress",
    },
    "classic_nacl_db_block": {
        "label": "NACL Blocks Database", "tier": "classic", "severity": "critical", "aws": "VPC + RDS",
        "infra": True, "classic": True, "icon": "🕸",
        "description": "ec2:CreateNetworkAclEntry denies TCP 5432 into the database subnets. Health checks stay green "
                       "(they don't touch the DB) but every gate request times out — healthy hosts, failing app.",
        "datadog_signal": "APM postgres connect timeouts on avionpulse-gates; aws.rds.database_connections drops; ALB target 5xx",
    },
    "classic_asg_zero": {
        "label": "ASG Scaled to Zero", "tier": "classic", "severity": "critical", "aws": "EC2 Auto Scaling",
        "infra": True, "classic": True, "icon": "📴",
        "description": "autoscaling:UpdateAutoScalingGroup sets min/desired to 0. Instances terminate, hosts disappear, the "
                       "ALB returns 503. Revert relaunches them (≈3 min to healthy).",
        "datadog_signal": "aws.applicationelb.healthy_host_count = 0; aws.autoscaling.group_in_service_instances = 0; host no-data",
    },
    "classic_alb_maintenance": {
        "label": "ALB Rule → Fixed 503", "tier": "classic", "severity": "warning", "aws": "ALB",
        "infra": True, "classic": True, "icon": "🚧",
        "description": "elasticloadbalancing:ModifyRule replaces the forward action with a fixed 503 'maintenance' "
                       "response. Instances are perfectly healthy — and receive no traffic.",
        "datadog_signal": "aws.applicationelb.httpcode_elb_5xx up, request_count to targets 0, healthy_host_count unchanged",
    },
    "classic_rds_reboot": {
        "label": "RDS Reboot / Failover", "tier": "classic", "severity": "warning", "aws": "RDS",
        "infra": True, "classic": True, "icon": "🔄",
        "description": "rds:RebootDBInstance (with ForceFailover on the Full size, which is Multi-AZ). One-shot: the "
                       "database is unavailable for ~1–3 minutes, then recovers on its own.",
        "datadog_signal": "RDS event 'DB instance restarted' / 'Multi-AZ failover'; connection errors spike then clear",
    },
    "classic_db_slow_query": {
        "label": "Slow Database Queries", "tier": "classic", "severity": "warning", "aws": "EC2 + RDS",
        "infra": False, "classic": True, "icon": "🐌",
        "description": "The gate service on EC2 adds pg_sleep(2.5) to every query. CPU and connections look normal; "
                       "latency triples end to end.",
        "datadog_signal": "APM postgres span duration on avionpulse-gates; aws.applicationelb.target_response_time p95",
    },
    # ── AWS FIS (Classic tier) ───────────────────────────────────────────
    "fis_cpu_stress": {
        "label": "FIS: CPU Stress", "tier": "fis", "severity": "warning", "aws": "FIS + SSM + EC2",
        "infra": True, "classic": True, "icon": "♨",
        "description": "Starts the FIS experiment that runs AWSFIS-Run-CPU-Stress on every gate-service instance for 5 min.",
        "datadog_signal": "system.cpu.user ~100% on tier:classic hosts; aws.ec2.cpuutilization; CPU credit balance falls",
    },
    "fis_network_latency": {
        "label": "FIS: Network Latency", "tier": "fis", "severity": "warning", "aws": "FIS + SSM + EC2",
        "infra": True, "classic": True, "icon": "📶",
        "description": "Starts the FIS experiment that adds 300 ms latency on the instances' network interface for 5 min (tc netem).",
        "datadog_signal": "ALB target_response_time jump; APM postgres spans +300 ms per round trip",
    },
    "fis_instance_terminate": {
        "label": "FIS: Terminate Instance", "tier": "fis", "severity": "critical", "aws": "FIS + EC2 Auto Scaling",
        "infra": True, "classic": True, "icon": "💀",
        "description": "Starts the FIS experiment that terminates one gate-service instance. Auto Scaling replaces it; "
                       "watch the ASG heal (Minimal size has one instance, so expect a short outage).",
        "datadog_signal": "EC2 state-change event; aws.autoscaling.group_in_service_instances dip; host replaced in infra list",
    },
    "fis_az_outage": {
        "label": "FIS: AZ Outage", "tier": "fis", "severity": "critical", "aws": "FIS + VPC + RDS",
        "infra": True, "classic": True, "full_only": True, "icon": "⚡",
        "description": "Full size only. Disrupts all network traffic in AZ-a's subnets for 5 min and forces an RDS Multi-AZ "
                       "failover — the closest you can get to an Availability Zone failure.",
        "datadog_signal": "healthy hosts halve, RDS failover event, cross-AZ traffic shift; composite monitor on tier:classic",
    },
    # ── Composite ────────────────────────────────────────────────────────
    "cascading_failure": {
        "label": "Cascading Failure", "tier": "composite", "severity": "critical", "aws": "All tiers",
        "infra": True, "icon": "🌋",
        "description": "Turns on latency_spike + dynamodb_throttle + random_500s + queue_backlog + log_flood together.",
        "datadog_signal": "Composite monitor across 4+ signals → P1",
    },
    "security_incident": {
        "label": "Security Incident", "tier": "composite", "severity": "critical", "aws": "IAM + KMS + SQS + S3",
        "infra": True, "icon": "🚨",
        "description": "Turns on iam_policy_detached + kms_key_disabled + sqs_send_denied + s3_bucket_policy_deny — "
                       "what a compromised admin (or a bad Terraform apply) looks like.",
        "datadog_signal": "Cloud SIEM signals correlated with the app's AccessDenied / KMS errors in one investigation",
    },
    "control_plane_storm": {
        "label": "Control-Plane Storm", "tier": "composite", "severity": "critical", "aws": "DynamoDB + EventBridge + Lambda + SSM",
        "infra": True, "icon": "🌀",
        "description": "Turns on dynamodb_capacity_cut + eventbridge_rule_disabled + lambda_bad_deploy + ssm_config_poisoned.",
        "datadog_signal": "Four CloudTrail changes in a minute, each followed by its own symptom — Change Tracking timeline",
    },
}

COMPOSITE = {
    "cascading_failure": ["latency_spike", "dynamodb_throttle", "random_500s", "queue_backlog", "log_flood"],
    "security_incident": ["iam_policy_detached", "kms_key_disabled", "sqs_send_denied", "s3_bucket_policy_deny"],
    "control_plane_storm": ["dynamodb_capacity_cut", "eventbridge_rule_disabled", "lambda_bad_deploy",
                            "ssm_config_poisoned"],
}

CACHE_TTL_S = 3.0
_cache = {"at": 0.0, "doc": None}


class UnhandledFault(Exception):
    """Raised on purpose and left uncaught so Lambda records an Error."""


# ── State ─────────────────────────────────────────────────────────────────
def _load(force=False):
    now = time.time()
    if force or _cache["doc"] is None or now - _cache["at"] > CACHE_TTL_S:
        try:
            doc = get_store().get("CONFIG", "FAULTS", consistent=force) or {}
        except Exception as e:  # never let chaos state reads take the app down
            log.warning("fault state read failed: %s", err_code(e))
            doc = _cache["doc"] or {}
        _cache.update(at=now, doc=doc)
    return _cache["doc"]


def _save(doc):
    doc = dict(doc, PK="CONFIG", SK="FAULTS", updated_at=int(time.time()))
    get_store().put(doc)
    _cache.update(at=time.time(), doc=doc)


def active(name):
    f = _load().get("faults", {}).get(name)
    return bool(f and f.get("until", 0) > time.time())


def status():
    now = time.time()
    out = {}
    for name in CATALOG:
        f = _load().get("faults", {}).get(name)
        on = bool(f and f.get("until", 0) > now)
        out[name] = {"enabled": on,
                     "until": int(f["until"]) if on else None,
                     "by": f.get("by") if on else None}
    return out


def active_count():
    return sum(1 for v in status().values() if v["enabled"])


# ── Infra side effects ────────────────────────────────────────────────────
class ClassicOffline(Exception):
    """A Classic-tier fault was switched on while that stack isn't online."""


def _apply_infra(name, enabled, api_id, entry=None):
    """Make (or undo) the AWS change behind an infra fault. May return extra
    fields to keep on the fault record (e.g. a FIS experiment id)."""
    if CATALOG[name].get("classic"):
        import classic
        return classic.apply_fault(name, enabled, entry or {})
    if name == "api_throttling":
        if enabled:
            infra.set_api_throttle(api_id, 1, 2)
        else:
            infra.set_api_throttle(api_id, config.DEFAULT_RATE, config.DEFAULT_BURST)
    elif name == "worker_throttle":
        infra.set_worker_concurrency_zero(enabled)
    elif name == "queue_backlog":
        if enabled or not active("sqs_retention_cut"):
            infra.set_worker_trigger(not enabled)
    elif name == "cold_start_storm":
        infra.recycle_api_function(2500 if enabled else None)
    elif name == "dynamodb_capacity_cut":
        if enabled:
            infra.set_table_capacity(1, 1)
        else:
            infra.set_table_capacity(config.DEFAULT_RCU, config.DEFAULT_WCU)
    elif name == "eventbridge_rule_disabled":
        infra.set_sim_rule_enabled(not enabled)
    elif name == "api_integration_broken":
        infra.set_integration_broken(enabled)
    elif name == "lambda_bad_deploy":
        infra.set_worker_bad_deploy(enabled)
    elif name == "ssm_config_poisoned":
        infra.set_flags_poisoned(enabled)
    elif name == "sqs_retention_cut":
        infra.set_queue_retention(60 if enabled else config.QUEUE_RETENTION_S)
        if enabled or not active("queue_backlog"):
            infra.set_worker_trigger(not enabled)
    elif name == "iam_policy_detached":
        infra.set_worker_policy_attached(not enabled)
    elif name == "kms_key_disabled":
        infra.set_kms_enabled(not enabled)
    elif name == "sqs_send_denied":
        infra.set_queue_send_denied(enabled)
    elif name == "s3_bucket_policy_deny":
        infra.set_recorder_denied(enabled)
    elif name == "eb_target_broken":
        infra.set_bus_target_broken(enabled)
    elif name == "sns_delivery_failure":
        infra.set_sns_permission(not enabled)
    return None


# ── Mutations ─────────────────────────────────────────────────────────────
def toggle(name, enabled, ttl_min=None, user="system", api_id=None):
    if name not in CATALOG:
        raise KeyError(name)
    ttl_min = max(1, min(int(ttl_min or config.FAULT_TTL_DEFAULT_MIN), config.FAULT_TTL_MAX_MIN))
    doc = dict(_load(force=True))
    faults = dict(doc.get("faults", {}))
    if api_id:
        doc["api_id"] = api_id
    api_id = doc.get("api_id")

    if enabled and CATALOG[name].get("classic"):
        import classic
        why = classic.fault_blocker(name)
        if why:
            raise ClassicOffline(why)

    members = COMPOSITE.get(name, []) + [name]
    notes = []
    for m in members:
        extra = None
        if CATALOG[m]["infra"] and m not in COMPOSITE:
            try:
                extra = _apply_infra(m, enabled, api_id, faults.get(m))
            except Exception as e:
                # Report it rather than fail: e.g. the account can't reserve concurrency.
                notes.append(f"{m}: {err_code(e)} — {e}")
                log.error("[CHAOS] infra change for %s failed: %s", m, e)
                if enabled:
                    continue
        if enabled:
            faults[m] = dict(extra or {}, until=time.time() + ttl_min * 60, by=user)
        else:
            faults.pop(m, None)
        log.warning("[CHAOS] %s %s by %s (ttl %s min)", m, "ON" if enabled else "OFF", user, ttl_min)

    doc["faults"] = faults
    _save(doc)
    telemetry.event(f"AvionPulse chaos: {name} {'ON' if enabled else 'OFF'}",
                    f"{user} turned {'on' if enabled else 'off'} {', '.join(members)}"
                    + (f" for {ttl_min} min" if enabled else "") + (f"\nNotes: {'; '.join(notes)}" if notes else ""),
                    [f"fault:{name}", f"chaos_tier:{CATALOG[name]['tier']}"], "warning" if enabled else "success")
    return {"fault": name, "enabled": enabled, "members": members, "ttl_min": ttl_min, "notes": notes}


def reset_all(user="system", api_id=None):
    doc = dict(_load(force=True))
    if api_id:
        doc["api_id"] = api_id
    notes = []
    import classic
    classic_up = classic.is_online()
    faults = doc.get("faults", {})
    for name, meta in CATALOG.items():
        if meta["infra"] and name not in COMPOSITE:
            if meta.get("classic") and not classic_up:
                continue
            # Always revert infra faults, even if state says off — cheap and idempotent.
            try:
                _apply_infra(name, False, doc.get("api_id"), faults.get(name))
            except Exception as e:
                notes.append(f"{name}: {err_code(e)}")
    doc["faults"] = {}
    _save(doc)
    log.warning("[CHAOS] all faults reset by %s", user)
    telemetry.event("AvionPulse chaos: reset all", f"{user} cleared every fault", ["fault:all"], "success")
    return {"message": "All faults cleared", "notes": notes}


def janitor():
    """Revert expired faults (called by the scheduled simulator)."""
    doc = dict(_load(force=True))
    faults = dict(doc.get("faults", {}))
    now = time.time()
    expired = [n for n, f in faults.items() if f.get("until", 0) <= now]
    for n in expired:
        if CATALOG.get(n, {}).get("infra") and n not in COMPOSITE:
            try:
                _apply_infra(n, False, doc.get("api_id"), faults.get(n))
            except Exception as e:
                log.error("[CHAOS] janitor could not revert %s: %s", n, e)
                continue
        faults.pop(n, None)
        log.warning("[CHAOS] %s expired — auto-reverted", n)
    if expired:
        doc["faults"] = faults
        _save(doc)
    return expired


def drop_classic_faults(user="system"):
    """The Classic stack went away: forget its faults (nothing left to revert)."""
    doc = dict(_load(force=True))
    faults = {k: v for k, v in doc.get("faults", {}).items() if not CATALOG.get(k, {}).get("classic")}
    if len(faults) != len(doc.get("faults", {})):
        doc["faults"] = faults
        _save(doc)
        log.warning("[CHAOS] classic-tier faults cleared (%s)", user)
