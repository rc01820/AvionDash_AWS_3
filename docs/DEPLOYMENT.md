# AvionPulse v3 — Deploying to AWS

This guide covers everything from an empty AWS account to a running AvionPulse, then updating,
tearing down and troubleshooting it. You deploy **one** stack, the core. The Classic tier
(EC2 / ALB / RDS) is a second stack that the app creates and deletes on demand (section 6).

**Contents**

1. [Choose a deployment path](#1-choose-a-deployment-path)
2. [Before you start](#2-before-you-start) — account, permissions, region, tools, cost guard
3. [Optional: store the Datadog API key](#3-optional-store-the-datadog-api-key)
4. [Path A — AWS Console + CloudFormation](#4-path-a--aws-console--cloudformation-no-cli-needed-for-the-stack)
5. [Path B — AWS CLI + CloudFormation](#5-path-b--aws-cli--cloudformation-one-command)
6. [The Classic tier (on demand)](#6-the-classic-tier-on-demand)
7. [Parameter reference](#7-parameter-reference)
8. [What gets created](#8-what-gets-created)
9. [Verify the deployment](#9-verify-the-deployment)
10. [Day-2 operations](#10-day-2-operations) — code updates, reseeding, enabling Datadog, passwords
11. [Securing a shared deployment](#11-securing-a-shared-deployment)
12. [Tear down](#12-tear-down)
13. [Troubleshooting](#13-troubleshooting)
14. [Installation scenarios in detail](#14-installation-scenarios-in-detail) — local only, with/without
    Datadog, Classic tier, existing Datadog/CloudTrail setups, several copies, upgrading from v2, CI

---

## 1. Choose a deployment path

### 1.1 Which installation scenario are you in?

| # | Scenario | AWS account | Datadog | You get | Time | Cost |
|---|---|---|---|---|---|---|
| S1 | **Local only** | no | no | Every page, every fault, simulated Classic tier | 1 min | $0 |
| S2 | **Core, no Datadog** | yes | no | The real app breaking real AWS resources; AWS Resources page; CloudWatch only | 10 min | ≈ $1–1.50/mo |
| S3 | **Core + Datadog** (recommended) | yes | yes | Traces, logs, enhanced and custom metrics, AWS integration, CloudTrail | 30–45 min | + Datadog usage |
| S4 | **S3 + Classic tier on demand** | yes | yes | EC2 hosts, ALB, RDS, FIS experiments, distributed trace to EC2 | +15 min per launch | + $0.07–0.11/h |
| S5 | **Any of the above + RUM** | yes | yes | Browser sessions linked to backend traces | +5 min | Datadog RUM |
| S6 | **Account already monitored by Datadog / org CloudTrail** | yes | yes | Same as S3 without creating duplicates | 20 min | — |
| S7 | **Several copies** (per person, per region, per env) | yes | optional | Independent stacks side by side | 10 min each | per copy |
| S8 | **Upgrading from AvionDash AWS 2** | yes | optional | v3 alongside or instead of v2 | 15 min | — |
| S9 | **CI / your own pipeline** | yes | optional | Non-interactive deploys | — | — |

Step-by-step instructions for each are in [section 14](#14-installation-scenarios-in-detail).
The rest of this guide is the reference they point into.

### 1.2 Deployment paths for the core stack

Both paths create the same application. (v2's SAM path was dropped in v3: one template is easier
to keep correct now that the core stack also carries the Classic tier's roles and FIS templates.)

| | **A. Console + CloudFormation** | **B. CLI + CloudFormation** |
|---|---|---|
| Template | [`cloudformation/avionpulse.yaml`](../cloudformation/avionpulse.yaml) | same |
| Tools on your PC | Python 3.10+ (to build the zip) and a browser | AWS CLI v2 + Python |
| How code gets to Lambda | You upload one zip to S3 | Script builds and uploads the zip |
| How the template gets to CloudFormation | Console uploads it to its own S3 bucket (it's > 51 KB) | Script passes `--s3-bucket` (artifact bucket) |
| How the website gets to S3 | `SiteDeployment` custom resource (automatic) | same (automatic) |
| Stack delete empties the buckets | Yes (custom resources) | Yes |
| Best for | First-timers; seeing every parameter in the console | Repeatable deploys |

**Stack name: 24 characters or fewer.** The Classic tier's load balancer is named `<stack>-classic`,
and ALB names are capped at 32.

If you're unsure, use **Path B**: one command on Windows or macOS/Linux. Use **Path A** if you'd rather click through the AWS console.

---

## 2. Before you start

### 2.1 AWS account

* Use a **sandbox or personal account**, not production. The chaos engine changes Lambda,
  API Gateway and SQS settings on purpose. It only touches resources named after this stack,
  but a demo account keeps any surprises contained.
* Make sure you know which account you're in:
  ```bash
  aws sts get-caller-identity
  ```

### 2.2 Permissions for the person deploying

The easiest option is `AdministratorAccess` in a sandbox account. For least privilege, the
deploying identity needs create/update/delete rights on:

| Service | Why |
|---|---|
| CloudFormation | Create and update the stack |
| IAM | Create 8 roles, 4 managed policies and 1 instance profile (`CAPABILITY_IAM`) and pass them (`iam:PassRole`) |
| Lambda | Functions, event source mapping, permissions, event invoke config, and reading Datadog's public layers |
| API Gateway (v2) | HTTP API, routes, integration, stage |
| DynamoDB, SQS | Table and queues |
| S3 | Site bucket (+ the artifact bucket for paths A/B) |
| CloudFront | Distribution and Origin Access Control |
| CloudWatch Logs | Log groups with retention |
| EventBridge | Scheduled rules, a custom event bus and rules |
| KMS | One customer-managed key + alias (events queue) |
| SNS, Step Functions | Crew-alerts topic + subscription; Express state machine |
| AWS FIS | Four experiment templates (no cost until started) |
| SSM Parameter Store | Feature-flag parameter; plus the Datadog key if you store one |
| Budgets | Only if you set `BudgetEmail` |

### 2.3 Region

* **Recommended: `us-east-1`.** Every service used is available there, CloudFront metrics are
  published there, and Datadog's Lambda layers are published there.
* Any commercial region with Lambda arm64 (Graviton) and HTTP APIs works. The Datadog layers are
  published per region, so check the region is listed in Datadog's docs if you plan to enable it.
* The **artifact bucket (paths A/B) must be in the same region as the stack**. Lambda can only
  load code from a bucket in its own region.
* **For the Classic tier**, the region also needs at least two Availability Zones offering
  `t4g.micro` and `db.t4g.micro` (Graviton), plus AWS FIS. All major commercial regions qualify.

### 2.4 Tools

| Tool | Needed for | Install (Windows) | Check |
|---|---|---|---|
| Python 3.9+ | A, B (builds the zip), local mode, `classic.py`, `restore.py` | python.org installer, tick *Add to PATH* (macOS/Linux: usually preinstalled as `python3`) | `python --version` |
| AWS CLI v2 | B, teardown and `restore.py` | `msiexec /i https://awscli.amazonaws.com/AWSCLIV2.msi` | `aws --version` |

Then configure credentials, either with access keys:

```bash
aws configure
```

or with IAM Identity Center (SSO):

```bash
aws configure sso
```
```bash
aws sso login --profile <your-profile>
```

With SSO, set `AWS_PROFILE=<your-profile>` (PowerShell: `$env:AWS_PROFILE="<your-profile>"`)
before running the scripts. Set `AWS_REGION` the same way if you don't use `us-east-1`.

### 2.5 Try it locally first (optional, 1 minute)

```bash
python scripts/local_server.py
```

Open http://localhost:8080. It's the full UI, all 50 faults and a simulated Classic tier
against in-memory stand-ins for every AWS service, with no account and no cost. See
**[LOCAL_MODE.md](LOCAL_MODE.md)** for what's simulated and how each fault behaves locally.

### 2.6 Cost guard

The core stack costs about **$1.10–1.50 per month** while the free tier applies (the KMS key is
$1 of that) and about **$5 per month** without it. The Classic tier adds **$0.07–0.11 per hour**
only while launched (details in [COST.md](COST.md)). To be emailed if spend goes over a limit, pass
`BudgetEmail` (both paths). It creates a monthly AWS Budget that alerts at 50% actual and 100%
forecast of `MonthlyBudgetUsd` (default $10). AWS sends a confirmation email the first time.
**Recommended whenever you'll use the Classic tier.**

---

## 3. Optional: store the Datadog API key

Skip this section to deploy without Datadog. You can enable it later (section 10.3).

The template never takes the API key as a parameter. It reads it from an SSM **SecureString**
(standard tier, free). The path B script creates it for you as `/<stack>/datadog/api-key` when
you pass the key. For path A, create it yourself:

**Console:** *Systems Manager → Parameter Store → Create parameter*
* Name: `/avionpulse/datadog/api-key` (must match the `DatadogApiKeySsmName` parameter)
* Tier: **Standard**
* Type: **SecureString**, KMS key source *My current account*, key `alias/aws/ssm`
* Value: your Datadog API key (*Organization Settings → API Keys* in Datadog)

**CLI:**
```bash
aws ssm put-parameter --name /avionpulse/datadog/api-key --type SecureString --value <DD_API_KEY> --region us-east-1
```

**Layer versions.** Find the current version numbers of `Datadog-Python312-ARM` and
`Datadog-Extension-ARM` in Datadog's *Serverless → AWS Lambda → Python* installation docs. The
template defaults (107 / 80) are placeholders. A version that doesn't exist fails the deploy with
`Layer version ... does not exist`.

---

## 4. Path A — AWS Console + CloudFormation (no CLI needed for the stack)

### Step A1 — Build the code package

From the `AvionDash_AWS_3` folder:

```bash
python scripts/package.py
```

Output looks like:

```text
built dist/avionpulse-code-5d55fcffd3.zip (150 KB)
CodeKey=avionpulse/avionpulse-code-5d55fcffd3.zip
```

The zip holds the Lambda code at its root, the website under `web/`, and `classic_tier/` (the
Classic stack template and the EC2 gate service, which the Classic tier reads from this same zip). The hash in the name
changes whenever any file changes, which is how CloudFormation knows to roll out new code later.

### Step A2 — Create an artifact bucket and upload the zip

1. **S3 console → Create bucket**
   * **Bucket name:** globally unique, e.g. `avionpulse-artifacts-<your-account-id>-us-east-1`
   * **Region:** the region you'll deploy the stack to
   * **Block all public access:** leave **on**
   * Everything else default → **Create bucket**
2. Open the bucket → **Create folder** `avionpulse` → open it → **Upload** →
   add `dist/avionpulse-code-<hash>.zip` → **Upload**.
3. Note the bucket name and the key `avionpulse/avionpulse-code-<hash>.zip`. You'll need both in the next step.

### Step A3 — Create the stack

1. **CloudFormation console** (check the region selector top-right) → **Create stack → With new
   resources (standard)**.
2. **Prerequisite:** *Choose an existing template*. **Specify template:** *Upload a template file*
   → **Choose file** → `cloudformation/avionpulse.yaml` → **Next**. (The file is larger than
   51 KB; the console uploads it to its own `cf-templates-…` bucket automatically.)
3. **Stack name:** `avionpulse`. Use lowercase letters, digits and hyphens, **≤ 24 characters**
   (the Classic ALB is `<stack>-classic`, and ALB names are capped at 32). Resource names
   (`avionpulse-api`, `avionpulse-data`…) and the Datadog queries in the docs derive from it.
4. **Parameters** (grouped as on screen; full list in [section 7](#7-parameter-reference)):
   * **1. Code package** — `CodeBucket` = your artifact bucket; `CodeKey` = `avionpulse/avionpulse-code-<hash>.zip`
   * **2. Application** — set `AdminPassword` if anyone else can reach the site (section 11);
     leave the rest default.
   * **3. Datadog** — `EnableDatadog=true` only if you did section 3; set the two layer versions.
     Optionally the RUM id/token.
   * **4. Cost guards** — defaults are fine; optionally `BudgetEmail`.
   * **Next.**
5. **Configure stack options:**
   * **Tags:** add `app = avionpulse` (optional; every resource is tagged anyway).
   * **Stack failure options:** keep *Roll back all stack resources*.
   * **Next.**
6. **Review:** at the bottom tick **"I acknowledge that AWS CloudFormation might create IAM
   resources."** → **Submit**.

### Step A4 — Watch it build (about 6–10 minutes)

Open the **Events** tab and refresh. The order is roughly:

| Minute | What happens |
|---|---|
| 0–1 | Table (with stream), KMS key, queues, buckets, SSM flags, IAM roles and policies, log groups |
| 1–2 | Seven Lambda functions, HTTP API (2 integrations, 5 routes), stage, EventBridge rules and bus, SNS topic, Step Functions state machine, event source mappings |
| 1–2 | FIS experiment templates, Classic roles and instance profile |
| 1–2 | `SeedData` runs the seeder (~20 s: 70 items, paced for 5 WCU) |
| 2–7 | **CloudFront distribution** (the slow part) |
| 5–8 | `SiteDeployment` copies the website into S3 and invalidates CloudFront |
| ~8 | Stack status **CREATE_COMPLETE** |

### Step A5 — Open the site

**Outputs** tab → **SiteUrl** (`https://dxxxxxxxxxxxx.cloudfront.net`). The login page doesn't
list any users (deployed or local). Sign in with `admin` and either your `AdminPassword` or the demo
password `aviondash123` (all users: [README → Users and roles](../README.md#users-and-roles)).
**StatusPageUrl** is the public status page (no login). Then go to [section 9](#9-verify-the-deployment).

---

## 5. Path B — AWS CLI + CloudFormation (one command)

The script does path A for you: it creates the artifact bucket if missing, builds and uploads the
zip, stores the Datadog key, deploys `cloudformation/avionpulse.yaml` and prints the URL.

**PowerShell**
```powershell
./scripts/deploy-cfn.ps1
./scripts/deploy-cfn.ps1 -AdminPassword 'Choose-A-Long-One!' -BudgetEmail you@example.com
./scripts/deploy-cfn.ps1 -DatadogApiKey $env:DD_API_KEY -DatadogPythonLayerVersion <n> -DatadogExtensionLayerVersion <n>
```

**bash**
```bash
./scripts/deploy-cfn.sh
ADMIN_PASSWORD='Choose-A-Long-One!' BUDGET_EMAIL=you@example.com ./scripts/deploy-cfn.sh
DD_API_KEY=xxx DD_PY_LAYER=<n> DD_EXT_LAYER=<n> ./scripts/deploy-cfn.sh
```

| PowerShell parameter | bash variable | Default |
|---|---|---|
| `-StackName` | `STACK_NAME` | `avionpulse` |
| `-Region` | `AWS_REGION` | `us-east-1` |
| `-CodeBucket` | `CODE_BUCKET` | `<stack>-artifacts-<account>-<region>` (created if missing) |
| `-AppEnv` | `APP_ENV` | `demo` |
| `-AdminPassword` | `ADMIN_PASSWORD` | demo password |
| `-DatadogApiKey` | `DD_API_KEY` | none → Datadog off |
| `-DatadogSite` | `DD_SITE` | `datadoghq.com` |
| `-DatadogPythonLayerVersion` / `-DatadogExtensionLayerVersion` | `DD_PY_LAYER` / `DD_EXT_LAYER` | 107 / 80 (placeholders) |
| `-RumApplicationId` / `-RumClientToken` | `RUM_APPLICATION_ID` / `RUM_CLIENT_TOKEN` | none |
| `-BudgetEmail` / `-MonthlyBudgetUsd` | `BUDGET_EMAIL` / `MONTHLY_BUDGET_USD` | none / 10 |

Re-running the same command updates the stack in place. If nothing changed it exits cleanly
(`--no-fail-on-empty-changeset`). On failure it prints the first failed resources and their reasons.

**Doing it by hand** (what the script runs):

```bash
python scripts/package.py --bucket <artifact-bucket> --region us-east-1
```
```bash
aws cloudformation deploy --region us-east-1 --stack-name avionpulse --template-file cloudformation/avionpulse.yaml --s3-bucket <artifact-bucket> --s3-prefix avionpulse/templates --capabilities CAPABILITY_IAM --parameter-overrides CodeBucket=<artifact-bucket> CodeKey=avionpulse/avionpulse-code-<hash>.zip
```
```bash
aws cloudformation describe-stacks --stack-name avionpulse --query "Stacks[0].Outputs" --output table
```

---

## 6. The Classic tier (on demand)

The Classic tier is **not** in the core template. It's `cloudformation/classic.yaml`, shipped
inside the code zip, and the app's api function creates it as a separate stack named
`<stack>-classic`. The core stack only holds what that needs, and none of it costs anything:

* `ClassicCfnRole`: the CloudFormation service role that creates the VPC, ALB, ASG and RDS. The api
  function can pass this role but can't create EC2/RDS resources itself.
* `ClassicInstanceProfile`: SSM agent, code-package read, DB secret read, Datadog key read.
* `ClassicSchedulerRole`: lets a one-time EventBridge Scheduler job delete the stack at expiry.
* `FisRole` + four FIS experiment templates targeting the tag `avionpulse:classic=<stack>`.

**Launch:** Chaos Control → *Classic Tier* card → Minimal or Full → auto-shutdown 1–8 h → **Launch**.
Or from a terminal:

```bash
python scripts/classic.py up --url https://dxxxx.cloudfront.net --size minimal --hours 2 --wait
```

| | Minimal | Full |
|---|---|---|
| EC2 | 1× t4g.micro (ASG) | 2× t4g.micro across 2 AZs |
| RDS PostgreSQL | db.t4g.micro single-AZ | db.t4g.micro **Multi-AZ** |
| ALB | 1 | 1 |
| ≈ cost | $0.07 / hour | $0.11 / hour |
| Time to online | 12–15 min | 15–20 min |
| Extra faults | all Classic + CPU / latency / terminate FIS | + `fis_az_outage`, RDS **failover** |

**How it ends:**
1. **Teardown** on the card (or `classic.py down`). Deletion takes about 10 minutes.
2. At expiry, the **EventBridge Scheduler** job calls `cloudformation:DeleteStack`. This works even if the app is broken.
3. The **janitor** (every 2 min) deletes an expired stack the Scheduler missed and retries a failed delete.
4. Break glass: `python scripts/restore.py --classic-down`.

Only one Classic stack can exist at a time; **＋1 h** extends it up to 8 hours from launch. A
failed create is rolled back and deleted automatically (`OnFailure=DELETE`); the card shows the
CloudFormation events, including the failure reason.

**Regions:** the Classic tier needs two AZs, `t4g.micro` and `db.t4g.micro` (Graviton), and AWS FIS.
That's every major commercial region; check before using a newer one.

---

## 7. Parameter reference

Applies to `cloudformation/avionpulse.yaml` (the core stack). The Classic stack's parameters are
filled in by the app at launch.

| Parameter | Default | What it does / when to change it |
|---|---|---|
| `CodeBucket` | — (required) | S3 bucket in the stack's region holding the code zip |
| `CodeKey` | `avionpulse/avionpulse-code.zip` | Key of the zip. Use the hashed name `package.py` prints; a new key rolls out new code |
| `AppEnv` | `demo` | `env` tag on metrics, traces and logs. Lowercase, ≤ 20 chars |
| `AppVersion` | `3.0.0` | `version` tag (Datadog deployment tracking) |
| `AdminPassword` | blank | Replaces `aviondash123` for `admin`. NoEcho (masked in the console). Strongly recommended for shared deployments |
| `SeedVersion` | `1` | Change it (e.g. to `2`) to reseed the table on the next update. Resets flights and users; keeps the JWT secret |
| `EnableDatadog` | `false` | Attaches the Datadog layers and handler wrapper; Classic EC2 instances install the Datadog Agent |
| `DatadogSite` | `datadoghq.com` | Your Datadog site (EU: `datadoghq.eu`, etc.) |
| `DatadogApiKeySsmName` | `/avionpulse/datadog/api-key` | Where the extension reads the API key |
| `DatadogPythonLayerVersion` | 107 | `Datadog-Python312-ARM` version, check Datadog docs |
| `DatadogExtensionLayerVersion` | 80 | `Datadog-Extension-ARM` version, check Datadog docs |
| `RumApplicationId` / `RumClientToken` | blank | Turns on Datadog RUM in the browser |
| `ApiRateLimit` / `ApiBurstLimit` | 20 / 40 | HTTP API default-route throttle: a hard ceiling on spend |
| `FaultTtlMinutes` | 30 | Default auto-revert time for chaos faults (1–120) |
| `LogRetentionDays` | 3 | Retention for all 8 log groups |
| `SimulatorSchedule` | `rate(5 minutes)` | How often flights move and business metrics are emitted |
| `BudgetEmail` / `MonthlyBudgetUsd` | blank / 10 | Optional AWS Budget alert. Recommended if you'll use the Classic tier |

---

## 8. What gets created

`cloudformation/avionpulse.yaml` (core) creates 72 resources (73 with the optional Budget), all serverless or free:

| Tier | Resources |
|---|---|
| Web | S3 site bucket, bucket policy (CloudFront OAC only), Origin Access Control, CloudFront distribution (`/*` → S3, `/api/*` → API Gateway) |
| App | HTTP API with **two** Lambda integrations (data; control), 5 routes, `$default` stage with throttling and access logs; `api`, `worker`, `simulator`, `janitor`, `processor` Lambdas |
| Data | DynamoDB table (5/5, TTL, **Streams**), SQS queue (**KMS CMK**) + DLQ, KMS key + alias, SSM feature-flag parameter |
| Integration | EventBridge custom bus `<stack>-flight-ops` + rule, S3 **recorder** bucket (EventBridge notifications, 1-day expiry) + rule, SNS `<stack>-crew-alerts` + subscription, Step Functions Express `<stack>-turnaround`, Streams → processor mapping |
| Scheduling | `<stack>-simulator-tick` (5 min), `<stack>-janitor-tick` (2 min) |
| IAM | `AppRole`, `WorkerRole` + `WorkerDataPolicy`, `ChaosInfraPolicy`, `ClassicControlPolicy`, `SiteDeployerRole`, `StatesRole` |
| Classic support | `ClassicCfnRole`, `ClassicInstanceRole` + profile, `ClassicSchedulerRole`, `FisRole`, 4 FIS experiment templates |
| Ops | 8 log groups with retention, `seed` and `site-deployer` Lambdas with their custom resources (seed, publish site, empty recorder bucket), optional Budget |

`cloudformation/classic.yaml` (on demand) creates a VPC (2 public app subnets, 2 isolated DB
subnets with their own NACL), an internet gateway, 3 security groups, an internet-facing ALB +
target group + listener + header-checked forward rule, a launch template and ASG (Amazon Linux
2023 arm64), and an RDS PostgreSQL instance with an RDS-managed master password (Secrets Manager).
There's **no NAT gateway**.

---

## 9. Verify the deployment

Replace `<site>` with the `SiteUrl` output.

| # | Check | How | Expected |
|---|---|---|---|
| 1 | Stack | CloudFormation → stack status | `CREATE_COMPLETE` / `UPDATE_COMPLETE` |
| 2 | Edge + API | `curl https://<site>/api/health` | `{"status": "ok", "service": "avionpulse-api", ...}` |
| 3 | All tiers | `curl https://<site>/api/health/deep` | `dynamodb`, `sqs`, `s3` all `"ok"` |
| 4 | Data seeded | `curl https://<site>/api/dashboard/public-stats` | `flights.total` 25, `aircraft.total` 15, `airports.total` 25 |
| 5 | Web tier | Open `https://<site>` | Login page with radar animation and live stats |
| 6 | Auth | Sign in as `admin` | Dashboard with KPIs, flight map, weather |
| 7 | Simulator | Wait 5 min, refresh Dashboard | *Data freshness* shows "0–5 min old" |
| 8 | Async pipeline | *Ops Pipeline* → raise a request | 202 Accepted; the event appears in the table within ~10 s |
| 9 | S3 read path | *NOTAMs* page | 8 NOTAM cards |
| 10 | Chaos | *Chaos Control* → toggle **Latency Spike** on, browse, toggle off | Pages slow to 2–6 s, then recover; topbar shows active-fault badge |
| 11 | Infra chaos | Toggle **Worker Concurrency = 0** on | No error banner. `aws lambda get-function-concurrency --function-name avionpulse-worker` shows 0. Toggle off → reservation removed |
| 12 | Logs | CloudWatch → Log groups → `/aws/lambda/avionpulse-api` | JSON log lines |
| 13 | Janitor | CloudWatch → `/aws/lambda/avionpulse-janitor` | A `janitor run complete` line every 2 min |
| 14 | Integrations | *Integrations* page after 5–10 min | All five channels **FLOWING** |
| 15 | AWS Resources | *AWS Resources* page | Every row **✓ OK** (any ERROR row means a missing permission; see Troubleshooting) |
| 16 | v3 infra chaos | Toggle **Scheduler Rule Disabled** on, then check `aws events describe-rule --name avionpulse-simulator-tick` | `"State": "DISABLED"`; AWS Resources shows DRIFT; toggle off → `ENABLED`, OK |
| 17 | Classic tier (optional, costs ≈ $0.15) | Chaos → Classic card → Minimal, 1 h → Launch; wait for ONLINE; open *Gates*; **Tear down** | Gate board loads ("served by avionpulse-gates") within ~5 min of ONLINE; teardown returns the card to OFFLINE with the session cost |
| 18 | Status page | Open `https://<site>/status` **without signing in**; `curl -s https://<site>/api/status` | "All systems operational" (Gate services shows *Not deployed*); JSON with `"overall_status": "operational"` |
| 19 | Status page fault | Chaos → Edge → **Status Page 500** on → reload `/status` → off | HTTP **500** "status page is unavailable", then 200 again |
| 20 | Login page | Open `https://<site>/` signed out | No user names or passwords shown; *● System status* link present |
| 21 | Viewer restriction | Sign in as `viewer` / `viewer123`; then try `https://<site>/#chaos` | Sidebar shows only Operations pages; no fault badge; `#chaos` lands on Dashboard. `curl -H "X-Avion-Token: <viewer token>" https://<site>/api/chaos/status` → **403** |

If you enabled Datadog, continue with [DATADOG_SETUP.md](DATADOG_SETUP.md) section 8, then
section 10 for the status-page HTTP check and the Synthetic tests.

---

## 10. Day-2 operations

### 10.1 Deploy a code change

* **Path B:** re-run `deploy-cfn`. A changed file gives a new zip hash and a new `CodeKey`, so all
  functions update and the site is republished.
* **Path A:** `python scripts/package.py` → upload the new zip → stack → **Update** → *Use current
  template* → change `CodeKey` to the new name → Next → Next → tick the IAM box → **Submit**.
* A running Classic tier keeps the code it launched with. Tear it down and relaunch it to pick up gate-service changes.

### 10.2 Reseed the data

Change `SeedVersion` (e.g. `1` → `2`) and update the stack. Users, flights, aircraft and
airports are rewritten; processed events and the JWT secret are kept.

### 10.3 Turn Datadog on (or off) later

1. Create the SSM parameter (section 3) if it doesn't exist.
2. Update the stack with `EnableDatadog=true` and the two layer versions (Path A: *Update → Use
   current template*; Path B: re-run with the API key option).
3. Functions switch to the Datadog handler wrapper on the next invocation. Set it back to `false`
   to remove the layers.
4. The Classic tier reads `EnableDatadog` **at launch**: a Classic stack that's already running
   keeps whatever it started with. Tear it down and relaunch to get (or drop) the Datadog Agent.

### 10.4 Change the admin password

Update the stack with a new `AdminPassword`. The password is one of the seeder's properties, so
changing it reruns the seeder (which also rewrites flights and the other demo users, harmlessly).
To go back to the demo password, clear `AdminPassword` and bump `SeedVersion` in the same update.
Existing sessions stay valid until their 8-hour token expires.

### 10.5 After chaos experiments

Faults revert themselves after their TTL (the janitor checks every 2 minutes). To clean up
immediately: **Chaos Control → RESET ALL**. It restores every core infra fault, and the Classic
ones when that tier is online. If the site itself is unreachable, run the break-glass script with
your own credentials:

```bash
python scripts/restore.py --stack avionpulse --region us-east-1     # add --classic-down to delete Classic too
```

Messages left in the DLQ stay for 4 days; purge them in the SQS console if a monitor keeps alerting.
The **AWS Resources** page should show every row as OK afterwards.

> Infra faults change resources outside CloudFormation, which shows up as **drift**. Any stack
> update puts the template values back, and so does RESET ALL.

---

## 11. Securing a shared deployment

The site is public on a `*.cloudfront.net` URL and the demo passwords are published in this repo.
Anyone who signs in as `admin` can run chaos experiments, which change AWS resources (all scoped to
this stack, and all auto-reverting), **and can launch the Classic tier, which costs money** (capped
at 8 hours and one stack at a time). If anyone other than you might find the URL:

| Measure | Cost | How |
|---|---|---|
| **Set `AdminPassword`** | Free | Parameter (section 7). Do this at minimum |
| Tear down between demos | Free | Section 12. A redeploy takes ~7 minutes |
| Keep the API throttle | Free | `ApiRateLimit` / `ApiBurstLimit` cap abuse and cost |
| Budget alert | Free | `BudgetEmail` |
| Restrict by IP with AWS WAF | ~$5+/month | Attach a WAF web ACL with an IP set to the distribution (outside this template, to keep the default free) |

The `operator` and `viewer` demo users can't change anything in AWS (operators can only enqueue
maintenance requests). **Viewers can't even see the platform pages** (AWS Resources, Architecture &
Cost, Chaos Control): the links are removed and those API routes return 403. Use `viewer` for
anyone who only needs to watch operations, and for the Synthetic browser test
([DATADOG_SETUP.md §10.4](DATADOG_SETUP.md#104-synthetic-browser-test-as-the-viewer-user)).

The login page never prints credentials, deployed or in [local mode](LOCAL_MODE.md).
The demo passwords are still published in this repository, so changing `AdminPassword` is what
actually protects the admin functions.

The **public status page** (`/status`) shows component health only: no fault names, users,
resource names or account details.

---

## 12. Tear down

```powershell
./scripts/teardown.ps1                    # add -StackName / -Region if you changed them
```
```bash
./scripts/teardown.sh
```

The scripts delete the **Classic stack first** (if it exists, using the Classic CloudFormation
role) and its expiry schedule, then empty the site bucket, delete the core stack, wait for
completion (~5–15 minutes, mostly CloudFront) and delete the SSM API-key parameter
(`-KeepApiKey` / `KEEP_API_KEY=1` keeps it).

**Console:** delete `<stack>-classic` first if it exists (use the role ARN from the core stack's
`ClassicCfnRoleArn` output if asked), then the core stack. Custom resources empty the site and
recorder buckets. If you delete the core stack first, the Classic stack loses its service role.
Delete it by choosing a role with enough permissions (or your own credentials) in the console.

**Left behind on purpose** (pennies, delete them if you don't need them):
* The artifact bucket (`avionpulse-artifacts-…`), which also holds the uploaded template
* The SSM parameter, if you kept it
* The AWS Budget is part of the stack and is deleted with it

Check nothing is left with a tag search: *Resource Groups & Tag Editor → Tag Editor →* all
regions, tag `app = avionpulse`.

---

## 13. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Requires capabilities : [CAPABILITY_IAM]` | IAM box not ticked / flag missing | Tick the acknowledgement, or add `--capabilities CAPABILITY_IAM` |
| `Templates with a size greater than 51,200 bytes must be deployed via an S3 Bucket` | Deploying the core template inline | Use the deploy script (it passes `--s3-bucket`), or upload the file in the console |
| Classic launch: `ClassicExists` | A previous Classic stack still exists (maybe deleting) | Wait for it to finish deleting, or tear it down first |
| Classic stack `ROLLBACK`/deleted right after launch | A create failed (quota, AZ capacity, region without `t4g`/`db.t4g`) | The card lists the CloudFormation events with the reason; fix and launch again |
| Classic online but Gates page says *database initialising* | Instances are still installing packages or waiting for RDS | Give it 3–5 minutes; check `/var/log/avionpulse-userdata.log` via SSM Session Manager |
| `fis_*` toggle shows an error "no targets" | Classic offline, or instances not yet `running` | Wait for the tier to be online and the ASG healthy |
| `dynamodb_capacity_cut` shows `LimitExceededException` | AWS daily limit on capacity decreases | Use it again tomorrow; everything else is unaffected |
| `Error occurred while GetObject. S3 Error Code: NoSuchKey` / `PermanentRedirect` on a Lambda | Wrong `CodeKey`, or the artifact bucket is in another region | Check the key exactly (folder prefix included); bucket must be in the stack's region |
| `Layer version arn:...Datadog-... does not exist` | Placeholder layer versions | Look up current versions (section 3) and update |
| `SeedData` or `SiteDeployment` **CREATE_FAILED** | The custom resource reported an error | The *Status reason* includes the log stream name. Open `/aws/lambda/<stack>-seed` or `/aws/lambda/<stack>-site-deployer` |
| Stack sits on a custom resource for ~1 hour | The function couldn't start at all (wrong zip, wrong handler), so it never answered | Wait for the timeout or cancel the update; check the function's log group; rebuild the zip with `package.py` (don't zip by hand: modules must be at the zip root) |
| `already exists` for a function, table or queue | A resource with the same name exists (another stack, or a leftover) | Use a different stack name, or delete the leftover |
| Site shows **AccessDenied** XML | CloudFront still propagating, or the site files weren't published | Wait 5 min; check the bucket contains `index.html` |
| Site loads but every API call fails | CloudFront `/api/*` origin still deploying, or Lambda erroring | `curl <ApiEndpoint>/api/health` directly; check `/aws/lambda/<stack>-api` |
| HTTP **429** everywhere | `api_throttling` fault active, or real traffic above the stage limit | Chaos → turn it off / RESET ALL; raise `ApiRateLimit` |
| HTTP **503** on data pages | DynamoDB throttling (`dynamodb_throttle` fault or heavy load on 5 RCU) | Turn the fault off; it recovers in seconds |
| Chaos toggle shows `⚠ … AccessDenied` or `ResourceConflictException` | Missing permission (stack name mismatch) or a Lambda config update still in progress | Wait 10 s and retry; make sure you didn't rename functions outside the template |
| `DELETE_FAILED` on `SiteBucket` / `RecorderBucket` | Bucket not empty (objects added by hand) | Empty it in the S3 console, then delete the stack again |
| Login fails with the demo password | `AdminPassword` was set | Use your `AdminPassword`; or update the stack with it blank + new `SeedVersion` |
| Dashboard *Data freshness* keeps growing | Simulator not running (`simulator_crash` fault, or rule disabled) | Chaos → turn the fault off; EventBridge → Rules → check the rule is *Enabled* |

**Useful commands**

```bash
aws cloudformation describe-stack-events --stack-name avionpulse --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason]" --output table
```
```bash
aws logs tail /aws/lambda/avionpulse-api --since 15m --follow
```
```bash
aws lambda invoke --function-name avionpulse-simulator out.json
```

---

## 14. Installation scenarios in detail

Each scenario lists what to do, in order, and how you know it worked. They build on each other:
S3 assumes S2's steps, and S4 assumes S3's.

### S1 — Local only (no AWS account)

1. Install Python 3.9+.
2. `python scripts/local_server.py` (macOS/Linux: `python3`) from the `AvionDash_AWS_3` folder.
3. Open http://localhost:8080, sign in as `admin` / `aviondash123`.

**Done when:** the Dashboard shows 25 flights, and Chaos → Classic card → Launch reaches ONLINE in
about 45 s. Details and guided tours: [LOCAL_MODE.md](LOCAL_MODE.md).

### S2 — Core stack, no Datadog

Good for a first look at the real AWS behaviour, or when Datadog isn't ready yet.

1. Sandbox account, AWS CLI configured ([§2](#2-before-you-start)).
2. Deploy:
   ```bash
   ADMIN_PASSWORD='Choose-A-Long-One!' BUDGET_EMAIL=you@example.com ./scripts/deploy-cfn.sh
   ```
   ```powershell
   ./scripts/deploy-cfn.ps1 -AdminPassword 'Choose-A-Long-One!' -BudgetEmail you@example.com
   ```
3. Confirm the AWS Budgets email.
4. Run checks 1–16 of [§9](#9-verify-the-deployment).

**What you can observe without Datadog:** the app's own pages (AWS Resources drift, Integrations,
Ops Pipeline DLQ), CloudWatch metrics and logs in the AWS console, and CloudTrail *Event history*
(90 days, free) for the chaos API calls. Metrics are written as JSON log lines
(`metric_name`, `metric_value`), so nothing is lost if you add Datadog later.

**Done when:** every §9 check passes.

### S3 — Core stack + Datadog (recommended)

1. Datadog API key (*Organization Settings → API Keys*) and your Datadog site (e.g. `datadoghq.com`, `datadoghq.eu`).
2. Current layer versions of `Datadog-Python312-ARM` and `Datadog-Extension-ARM` ([§3](#3-optional-store-the-datadog-api-key)).
3. Deploy (stores the key in SSM, attaches the layers):
   ```bash
   DD_API_KEY=<key> DD_SITE=datadoghq.com DD_PY_LAYER=<n> DD_EXT_LAYER=<n> \
   ADMIN_PASSWORD='Choose-A-Long-One!' BUDGET_EMAIL=you@example.com ./scripts/deploy-cfn.sh
   ```
   ```powershell
   ./scripts/deploy-cfn.ps1 -DatadogApiKey $env:DD_API_KEY -DatadogSite datadoghq.com `
     -DatadogPythonLayerVersion <n> -DatadogExtensionLayerVersion <n> `
     -AdminPassword 'Choose-A-Long-One!' -BudgetEmail you@example.com
   ```
4. Connect the **Datadog AWS integration** and pick its namespaces
   ([DATADOG_SETUP.md §2](DATADOG_SETUP.md#2-aws-integration) and [§9.1](DATADOG_SETUP.md#91-aws-integration-scope)).
5. Send **CloudTrail** to Datadog ([DATADOG_SETUP.md §9.2](DATADOG_SETUP.md#92-cloudtrail--datadog-the-who-changed-what-half)).
6. Create the monitors ([DATADOG_SETUP.md §5–6](DATADOG_SETUP.md#5-creating-monitors--the-common-steps))
   and import the dashboard ([§7](DATADOG_SETUP.md#7-the-avionpulse-health-dashboard)).
7. Verify end to end ([DATADOG_SETUP.md §8](DATADOG_SETUP.md#8-verifying-everything-end-to-end)).

**Done when:** Serverless view lists the 5 instrumented functions (api, worker, simulator, janitor, processor) with traces, `avionpulse.flights.airborne` has
data, and toggling `kms_key_disabled` produces a `KMS.DisabledException` error in APM within ~2 min.

### S4 — Adding the Classic tier

Nothing to deploy up front: the core stack already holds the roles and FIS templates.

1. **Prerequisites** (once per account and region):
   * EC2 quota *Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances* ≥ 2 vCPUs
     (Full needs 4 vCPUs); new accounts sometimes start lower. Check *Service Quotas → EC2*.
   * RDS is available in the account (some brand-new accounts need a few hours after sign-up).
   * `EnableDatadog=true` on the core stack if you want hosts, processes, logs and EC2 traces in Datadog.
2. **Launch:** Chaos → Classic card → **Minimal** (or **Full** for Multi-AZ and the AZ-outage
   experiment) → auto-shutdown → **Launch**. Or:
   ```bash
   python scripts/classic.py up --url https://<site> --password '<AdminPassword>' --size minimal --hours 2 --wait
   ```
3. **Wait** for ONLINE (12–15 min Minimal, 15–20 min Full), then 3–5 more minutes for the
   instances to install packages and initialise the database.
4. **Verify:** *Gates* page loads; AWS Resources shows four Classic rows OK; in Datadog,
   Infrastructure → hosts `tier:classic` appear and APM shows `avionpulse-gates`.
5. **Use:** Classic and FIS faults ([FAULT_SCENARIOS.md](FAULT_SCENARIOS.md)); Classic monitors
   ([DATADOG_SETUP.md §6.12–6.13](DATADOG_SETUP.md#612-classic-tier)).
6. **End:** **Tear down** on the card, `classic.py down`, or let it expire. A Scheduler job deletes it on time.

### S5 — Adding RUM

1. Datadog → *Digital Experience → Add an Application → JavaScript*, name `avionpulse-web`.
2. Redeploy with `-RumApplicationId <id> -RumClientToken <pub…>` (bash: `RUM_APPLICATION_ID`, `RUM_CLIENT_TOKEN`).
3. The site deployer rewrites `config.js`, and browser sessions link to Lambda traces.

### S6 — The account is already monitored by Datadog / has an organization trail

* **AWS integration already connected:** don't add a second one. Add the v3 namespaces
  ([DATADOG_SETUP.md §9.1](DATADOG_SETUP.md#91-aws-integration-scope)). **Don't** add the
  `app:avionpulse` tag filter if other workloads in the account rely on the integration: it
  would stop their metrics. Filter AvionPulse in your queries instead.
* **Datadog Forwarder already installed:** add the AvionPulse trail's bucket as a trigger, or
  nothing at all if the org trail is already forwarded.
* **Organization CloudTrail exists:** don't create another trail. Check that its logs reach
  Datadog and that the chaos calls appear (`source:cloudtrail @userIdentity.arn:*AppRole*`).
* **Existing SSM key at a different path:** pass `DatadogApiKeySsmName=<your path>` (path A
  parameter; the path B script always uses `/<stack>/datadog/api-key`).
* **GuardDuty / Security Hub already on:** the runbooks in [AWS_RUNBOOKS.md](AWS_RUNBOOKS.md) work
  as written; skip their "enable" steps.

### S7 — Several copies side by side

Every resource name, IAM scope, FIS tag, schedule and SSM path derives from the **stack name**, so
copies don't collide as long as the stack names differ.

```bash
STACK_NAME=avp-alice APP_ENV=alice ./scripts/deploy-cfn.sh
```
```bash
STACK_NAME=avp-bob APP_ENV=bob AWS_REGION=eu-west-1 ./scripts/deploy-cfn.sh
```

* Use a distinct **`APP_ENV`** per copy. All copies report `service:avionpulse`, so Datadog tells
  them apart by `env:` (and `stack:` in `DD_TAGS`).
* Each copy gets its own artifact bucket, Classic stack (`<stack>-classic`) and budget.
* Monitors: scope by `env:<APP_ENV>` and the per-stack names (`functionname:avp-alice-worker`…).
* Stack names ≤ 24 characters.

### S8 — Upgrading from AvionDash AWS 2

v2 and v3 both default to the stack name `avionpulse`, so choose one of these:

| Option | How | When |
|---|---|---|
| **Side by side** (recommended) | Deploy v3 with `STACK_NAME=avionpulse3 APP_ENV=demo3`, then compare; tear down v2 when happy | You have Datadog monitors/dashboards for v2 you want to keep working meanwhile |
| **Replace** | `./scripts/teardown.sh` from the **v2** folder, then deploy v3 with the default name | You don't need v2 any more; your v2 monitors keep working because the names are the same |
| In-place stack update | **Not supported.** v2 stacks deployed with SAM can't be updated by the plain template, and the v3 template restructures IAM and integrations | — |

After the switch, add the v3 monitors in [DATADOG_SETUP.md §6.8 onwards](DATADOG_SETUP.md#68-aws-control-plane).
The v2 monitors (§6.1–6.6) still apply unchanged. One v2 behaviour changed: the janitor now runs
every 2 minutes in its own function (`<stack>-janitor`), not inside the simulator.

### S9 — CI or your own pipeline

The deploy script is a thin wrapper. The non-interactive equivalent:

```bash
python scripts/package.py --bucket "$ARTIFACTS" --region "$AWS_REGION" | tee pkg.txt
```
```bash
CODE_KEY=$(sed -n 's/^CodeKey=//p' pkg.txt)
```
```bash
aws cloudformation deploy --region "$AWS_REGION" --stack-name "$STACK" \
  --template-file cloudformation/avionpulse.yaml --s3-bucket "$ARTIFACTS" --s3-prefix "$STACK/templates" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset --tags app=avionpulse suite=aviondash \
  --parameter-overrides CodeBucket="$ARTIFACTS" CodeKey="$CODE_KEY" AppEnv="$APP_ENV" \
    AdminPassword="$ADMIN_PASSWORD" EnableDatadog=true DatadogApiKeySsmName="/$STACK/datadog/api-key" \
    DatadogPythonLayerVersion="$DD_PY_LAYER" DatadogExtensionLayerVersion="$DD_EXT_LAYER"
```

* The zip name is a content hash, so the same commit always produces the same `CodeKey`, and a
  no-change deploy is a no-op.
* Lint before deploying: `cfn-lint cloudformation/avionpulse.yaml cloudformation/classic.yaml`.
* Smoke test after deploying: `curl -fsS https://<site>/api/health/deep`.
* Make sure a pipeline never leaves a Classic stack running:
  `python scripts/restore.py --stack "$STACK" --classic-down` is safe to run at any time.

### Uninstall (any scenario)

`./scripts/teardown.sh` / `scripts\teardown.ps1` ([§12](#12-tear-down)). Then, in Datadog,
delete the AvionPulse monitors (filter `tag:app:avionpulse`) and the dashboard, and optionally
remove the Forwarder trigger and the v3 namespaces you added.

