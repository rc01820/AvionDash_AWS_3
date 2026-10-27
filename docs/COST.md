# AvionPulse v3 — AWS Cost Breakdown

v3 is two stacks with very different cost profiles:

| Stack | When it runs | Typical cost |
|---|---|---|
| **Core** (serverless) | Always | **≈ $1.10–1.50/month** with the free tier (the KMS key is $1 of that); **≈ $5/month** worst case |
| **Classic** (EC2 / ALB / RDS) | Only while launched from the Chaos page | **≈ $0.07/h** Minimal · **≈ $0.11/h** Full |

The v2 sections below still describe the serverless base. "v3 additions" and "Classic tier" cover
what's new. Prices are us-east-1 list prices as of October 2026; check the
[AWS Pricing Calculator](https://calculator.aws/) for your region.

Prices are us-east-1 list prices (Oct 2026). Check the
[AWS Pricing Calculator](https://calculator.aws/) for your region.

## Assumed demo workload

| Driver | Volume / month | Where it comes from |
|---|---|---|
| API requests | ~100,000 | A browser tab polls ~5 endpoints every 30 s for a few hours a day, plus demos |
| Simulator runs | 8,640 | EventBridge every 5 minutes, 24/7 |
| SQS messages | ~15,000 | 1–3 ops events per simulator run + maintenance requests |
| Idle SQS polling | ~650,000 requests | Lambda's SQS trigger long-polls continuously (5 pollers × 20 s) |
| Lambda compute | ~8,000 GB-s | 256 MB arm64, ~150 ms average API invocation |
| Logs | ~0.2 GB | JSON logs, 3-day retention |
| Data stored | < 1 MB DynamoDB, ~1 MB S3 | 25 airports, 15 aircraft, 25 flights, users, events (7-day TTL) |

## Line items

| Service | Resource | Free allowance | Demo usage | Free tier on | No free tier |
|---|---|---|---|---|---|
| **CloudFront** | 1 distribution, PriceClass_100 | 1 TB + 10M requests/month (always free) | < 1 GB, ~150k req | $0.00 | $0.15 |
| **S3** | 1 bucket (~1 MB) | 5 GB + 20k GET (12-month)¹ | 1 MB, mostly CloudFront-cached | $0.00 | $0.01 |
| **API Gateway** | HTTP API (not REST — ~70% cheaper) | 1M calls/month (12-month)¹ | 100k × $1.00/M | $0.00 | $0.10 |
| **Lambda** | 4 functions, arm64, 256 MB | 1M requests + 400k GB-s (always free) | ~120k req, ~8k GB-s | $0.00 | $0.13 |
| **DynamoDB** | 1 table, provisioned 5 RCU / 5 WCU | 25 RCU + 25 WCU + 25 GB (always free) | < 1 MB | $0.00 | $2.85² |
| **SQS** | Standard queue + DLQ | 1M requests (always free) | ~665k | $0.00 | $0.27 |
| **EventBridge** | 1 scheduled rule | Scheduled rules are free | 8,640 runs | $0.00 | $0.00 |
| **CloudWatch Logs** | 5 log groups, 3-day retention | 5 GB ingest + 5 GB stored (always free) | ~0.2 GB | $0.00 | $0.10 |
| **SSM Parameter Store** | 1 SecureString (Datadog key) | Standard parameters are free | 1 | $0.00 | $0.00 |
| **KMS** | AWS-managed keys only | Free | — | $0.00 | $0.00 |
| **IAM, CloudFormation** | roles, managed policy, stack | Free | — | $0.00 | $0.00 |
| **AWS Budgets** (optional) | 1 cost budget | Budget tracking is free | — | $0.00 | $0.00 |
| | | | **Total** | **≈ $0.00–0.15** | **≈ $3.60** |

¹ AWS changed the free tier for accounts created on or after **15 July 2025**: they get
  credits (up to $200) and a 6-month free plan instead of the 12-month offers. The "always free"
  offers (Lambda, DynamoDB, SQS, CloudFront, CloudWatch Logs) still apply to everyone, and the
  12-month items here are worth cents either way.

² DynamoDB is the biggest line *only* if your account has already used its 25 RCU/25 WCU
  always-free capacity elsewhere: 5 WCU × $0.00065/h + 5 RCU × $0.00013/h ≈ $2.85/month. Provisioned
  mode is deliberate — it keeps the bill flat whatever the chaos engine does, and it is what makes
  the `dynamodb_throttle` fault produce *real* throttling.

## Why it's this cheap (design choices)

| Choice | Saving |
|---|---|
| No VPC, no NAT gateway, no load balancer, no RDS | Avoids the usual fixed demo costs (NAT ≈ $33/mo, ALB ≈ $16/mo, smallest RDS ≈ $13/mo) |
| HTTP API instead of REST API | $1.00 vs $3.50 per million requests |
| arm64 (Graviton) Lambdas at 256 MB | ~20% cheaper per GB-second than x86 |
| Standard-library-only Python | No build step, no dependency layer, short cold starts |
| Provisioned 5/5 DynamoDB | Inside the always-free capacity; can never spike |
| CloudFront in front of S3 *and* the API | One origin for the browser (no CORS), free TLS, 1 TB free egress |
| 3-day log retention, explicit log groups | Logs never accumulate; deleted with the stack |
| API stage throttle (20 rps / burst 40) | Hard ceiling on what a runaway client or load test can spend |
| Fault TTLs + simulator janitor | A forgotten chaos experiment reverts itself within 5 minutes of its TTL |
| EventBridge every 5 min (not every minute) | 8.6k runs/month instead of 43k |

## What can push the bill up

| Scenario | Rough extra cost | Mitigation |
|---|---|---|
| `log_flood` fault left on under load | 50 lines × ~200 B per request: 100k requests ≈ 1 GB ≈ $0.50 beyond the free 5 GB | TTL auto-revert; keep it short |
| `loadgen.py` at 0.5 req/s for a whole month | ~1.3M requests → ~$1.30 API Gateway + ~$0.06 Lambda requests | Run it only during demos |
| A dashboard tab left open 24/7 | ~430k requests/month (still inside free Lambda; ~$0.43 API Gateway without free tier) | Close the tab |
| `lambda_timeout` under load | 10 s × 0.25 GB = 2.5 GB-s per timed-out request (≈ 160k timeouts before leaving the free tier) | TTL; it only hits 30% of requests |
| Raising `SimulatorSchedule` to every minute | ~35k more invocations + ~3M more DynamoDB writes (still free) | Leave at 5 min |

## v3 additions to the core stack

| Service | Resource | Free allowance | Demo usage | Free tier on | No free tier |
|---|---|---|---|---|---|
| **KMS** | 1 customer-managed key (events queue) | none for CMKs | $1/key + ~60k requests (60 s data-key cache) | $1.00–1.20 | $1.20 |
| **EventBridge** | custom bus + 2 rules, janitor rule | scheduled rules free | ~45k custom events × $1/M | $0.00–0.05 | $0.05 |
| **SNS** | crew-alerts → Lambda | 1M publishes, Lambda deliveries free | ~9k | $0.00 | $0.00 |
| **Step Functions Express** | turnaround workflow | — | ~9k executions × ~1 s × 64 MB | $0.01 | $0.01 |
| **S3 recorder** | 1 small snapshot per run, 1-day expiry | 2k PUT (12-month) | ~9k PUT | $0.03 | $0.04 |
| **Lambda** | `janitor` (every 2 min) + `processor` (event-driven) | inside the always-free 1M requests | ~22k + ~40k req | $0.00 | $0.01 |
| **DynamoDB Streams** | read by Lambda | Lambda stream reads are free | — | $0.00 | $0.00 |
| **SSM** | feature-flag parameter | standard tier free | 1 | $0.00 | $0.00 |
| **IAM, FIS templates, Scheduler role** | Classic support | free until used | — | $0.00 | $0.00 |
| | | | **v3 core additions** | **≈ $1.05–1.30** | **≈ $1.30** |

## Classic tier (on demand)

Billed only while `<stack>-classic` exists. Per hour, us-east-1:

| Resource | Rate | Minimal | Full |
|---|---|---|---|
| Application Load Balancer | $0.0225/h + ~0.5–1 LCU × $0.008 | $0.029 | $0.029 |
| Public IPv4 on the ALB (one per AZ) | $0.005/h each | $0.010 | $0.010 |
| EC2 t4g.micro | $0.0084/h each | $0.008 | $0.017 |
| Public IPv4 per instance | $0.005/h each | $0.005 | $0.010 |
| EBS gp3 8 GB per instance | $0.08/GB-month | $0.001 | $0.002 |
| RDS db.t4g.micro PostgreSQL | $0.016/h single-AZ, $0.032/h Multi-AZ | $0.016 | $0.032 |
| RDS gp3 20 GB | $0.115/GB-month (×2 Multi-AZ) | $0.003 | $0.006 |
| Secrets Manager (RDS master password) | $0.40/month, prorated | ~$0.001 | ~$0.001 |
| **Total** | | **≈ $0.074/h** | **≈ $0.107/h** |

| Usage pattern | Minimal | Full |
|---|---|---|
| One 2-hour demo | ≈ $0.15 | ≈ $0.21 |
| 40 demo hours / month | ≈ $3.00 | ≈ $4.30 |
| Left running 24/7 (impossible here: 8 h maximum, auto-deleted) | ≈ $54/month | ≈ $78/month |

Plus **AWS FIS** at $0.10 per action-minute: CPU stress or latency (one action, 5–6 min) ≈ $0.60,
AZ outage (two actions, 5 min) ≈ $1.00, terminate instance ≈ $0.10.

**Free tier:** accounts created before 15 July 2025 and in their first 12 months get 750 h/month of
a micro ALB, EC2 (t2/t3.micro; check whether t4g.micro is covered for your account) and single-AZ RDS
micro, which covers most of Minimal. Newer accounts get credits instead.

**Why there's no NAT gateway:** instances live in public subnets with security groups that only
admit the ALB. That keeps about $33/month off the bill. The trade-off is one public IPv4 per
instance ($0.005/h).

**What stops it running away:** one stack at a time, an 8-hour cap from launch, a one-time
EventBridge Scheduler job that deletes the stack at expiry, the janitor backstop every 2 minutes,
and `scripts/restore.py --classic-down`. Set `BudgetEmail` (default budget $10/month) to get an
email if something slips through anyway.

## Datadog-side costs (not AWS, but worth knowing)

- **Datadog AWS integration (CloudWatch polling)** — the integration calls CloudWatch
  `GetMetricData`, which AWS bills at **$0.01 per 1,000 metrics requested**. Polling everything in
  a busy account can cost dollars a month. Limit the integration to the `Lambda`, `ApiGateway`,
  `DynamoDB`, `SQS`, `CloudFront` and `Events` namespaces and filter on the `app:avionpulse` tag.
- **Datadog Lambda Extension** — adds a little init time and duration per invocation, which shows
  up in Lambda GB-seconds (still well inside the free allowance at demo volume).
- **Datadog billing** — Serverless is billed per active function (7 functions here, 5 of them instrumented); custom
  metrics are billed by unique tag combination. Classic-tier EC2 instances with the Agent are
  **infrastructure hosts** (and APM hosts) while they exist. Datadog bills hosts on the
  high-water mark excluding the top 1% of hours, so a few hours a month may barely register;
  check your contract. RDS Database Monitoring is not enabled by default.
- **CloudWatch polling for v3 namespaces** — adding SNS, States, ApplicationELB, EC2,
  AutoScaling and RDS to the AWS integration adds a few cents a month in `GetMetricData`. AvionPulse keeps metric tags low-cardinality
  (route, status class, severity) for this reason.

## Turning it off

```bash
./scripts/teardown.sh        # or scripts\teardown.ps1
```

Deletes the Classic stack (if any), then the core stack, the bucket contents, the table, queues,
the KMS key (scheduled for deletion after the 7-day minimum waiting period; it costs nothing while
pending deletion), log groups and the SSM parameter. Nothing keeps billing afterwards.
