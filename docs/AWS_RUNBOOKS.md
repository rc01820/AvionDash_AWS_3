# AvionPulse v3 — Manual AWS Runbooks

The Chaos page breaks everything it can **safely undo with a role scoped to its own stack**. Some
AWS failure modes don't fit that: AWS can't be made to emit them, they are account-wide, or they
touch audit and security controls an app should never hold permission for. This page covers each
of them as a manual runbook, with the Datadog signal to watch for and the exact revert.

**Before you start:** use a sandbox account. Run each step yourself with your own credentials. Do the
revert even if nothing seemed to happen. Set `R=us-east-1` and `S=avionpulse` (your stack name) in
your shell.

| # | Scenario | Why it isn't a button | Cost / risk |
|---|---|---|---|
| 1 | [AWS Health events](#1-aws-health-events) | AWS can't be made to emit them | none |
| 2 | [CloudTrail logging stopped](#2-cloudtrail-logging-stopped) | Audit tampering — no app should hold `cloudtrail:StopLogging` | low, revert promptly |
| 3 | [New IAM access key / user](#3-new-iam-user-and-access-key) | Account-wide IAM writes | low, delete after |
| 4 | [GuardDuty findings](#4-guardduty-sample-findings) | Account-level service, billed after trial | trial, then ~$1+/mo |
| 5 | [Security group open to the world](#5-security-group-open-to-the-world) | Deliberately exposes a host | low, minutes only |
| 6 | [CloudFront origin failure](#6-cloudfront-origin-failure) | Would take the Chaos page down with it; 5–10 min deploys | none |
| 7 | [Lambda faults with AWS FIS](#7-lambda-faults-with-aws-fis) | Needs the FIS Lambda extension layer + config bucket | $0.10/action-min |
| 8 | [Account Lambda concurrency exhaustion](#8-account-lambda-concurrency-exhaustion) | Account-wide; would throttle every function you own | moderate |
| 9 | [Cost anomaly](#9-cost-anomaly) | Cost data arrives a day later | a few dollars |
| 10 | [Regional dependency failure](#10-regional-dependency-failure) | Can't be done for real | none |

Prerequisite for most of these: **CloudTrail in Datadog** (see [DATADOG_SETUP.md §9](DATADOG_SETUP.md#9-v3-aws-infrastructure-monitoring)).

---

## 1. AWS Health events

**What it is.** AWS publishes service issues and scheduled maintenance (EC2 retirements, RDS
maintenance windows) as AWS Health events. They are AWS's own announcements and can't be created.

**Set up the detection.**
1. Datadog → Integrations → AWS → your account → enable the **Health** service / AWS Health events.
2. Create an **Event monitor**: `source:amazon_health` (filter on `region:<R>` if you like) → notify.
3. Optional: put an *Event stream* widget for `source:amazon_health` on the AvionPulse dashboard.

**Practise without a real event.** Launch the Classic tier at **Full** size and run `fis_az_outage`.
FIS's own "AZ Availability: Power Interruption" scenario is what AWS recommends for rehearsing AZ
events. Pending maintenance on the Classic RDS instance also shows up as a Health or RDS event:

```bash
aws rds describe-pending-maintenance-actions --region $R
```

---

## 2. CloudTrail logging stopped

**Goal:** fire Cloud SIEM's "CloudTrail logging disabled / trail modified" detection.
**Never use your organization or production trail.** Create a throwaway trail:

```bash
B=avionpulse-trail-test-$(aws sts get-caller-identity --query Account --output text)
aws s3 mb s3://$B --region $R
aws s3api put-bucket-policy --bucket $B --policy "{\"Version\":\"2012-10-17\",\"Statement\":[
 {\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"cloudtrail.amazonaws.com\"},\"Action\":\"s3:GetBucketAcl\",\"Resource\":\"arn:aws:s3:::$B\"},
 {\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"cloudtrail.amazonaws.com\"},\"Action\":\"s3:PutObject\",\"Resource\":\"arn:aws:s3:::$B/*\",
  \"Condition\":{\"StringEquals\":{\"s3:x-amz-acl\":\"bucket-owner-full-control\"}}}]}"
aws cloudtrail create-trail --name avionpulse-test-trail --s3-bucket-name $B --region $R
aws cloudtrail start-logging --name avionpulse-test-trail --region $R
aws cloudtrail stop-logging  --name avionpulse-test-trail --region $R     # ← the event
```

**Datadog:** Cloud SIEM signal for `StopLogging`. Log search: `source:cloudtrail @evt.name:StopLogging`.
**Revert:**
```bash
aws cloudtrail delete-trail --name avionpulse-test-trail --region $R
aws s3 rb s3://$B --force
```

---

## 3. New IAM user and access key

**Goal:** Cloud SIEM "IAM user created" / "access key created" / "user with console access" rules.

```bash
aws iam create-user --user-name avionpulse-siem-test
aws iam create-access-key --user-name avionpulse-siem-test        # ← the event (keep no copy of the key)
aws iam attach-user-policy --user-name avionpulse-siem-test \
  --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess              # ← "policy attached to user"
```

**Datadog:** `source:cloudtrail @evt.name:(CreateUser OR CreateAccessKey OR AttachUserPolicy)`; Cloud SIEM signals.
**Revert (always):**
```bash
aws iam detach-user-policy --user-name avionpulse-siem-test --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess
for k in $(aws iam list-access-keys --user-name avionpulse-siem-test --query 'AccessKeyMetadata[].AccessKeyId' --output text); do
  aws iam delete-access-key --user-name avionpulse-siem-test --access-key-id $k; done
aws iam delete-user --user-name avionpulse-siem-test
```

Don't simulate **root** sign-in or console logins without MFA. If you want login-failure
detections, use Datadog's Cloud SIEM rule test feature with a sample log instead.

---

## 4. GuardDuty sample findings

GuardDuty has a 30-day free trial, then bills on usage (usually a few dollars a month for a quiet account).

```bash
aws guardduty create-detector --enable --region $R               # skip if already enabled
D=$(aws guardduty list-detectors --region $R --query 'DetectorIds[0]' --output text)
aws guardduty create-sample-findings --detector-id $D --region $R \
  --finding-types "UnauthorizedAccess:EC2/SSHBruteForce" "Recon:EC2/PortProbeUnprotectedPort"
```

**Datadog:** with the AWS integration's GuardDuty log collection (or an EventBridge rule
`source: aws.guardduty` → Datadog), findings land as `source:guardduty` logs and Cloud SIEM signals.
Sample findings are prefixed `[SAMPLE]`.
**Revert:** `aws guardduty archive-findings …` or, if you created the detector just for this,
`aws guardduty delete-detector --detector-id $D --region $R`.

---

## 5. Security group open to the world

**Needs the Classic tier online.** It exposes the gate instances for a few minutes, so keep it short.

```bash
SG=$(aws cloudformation describe-stacks --stack-name $S-classic --region $R \
  --query "Stacks[0].Outputs[?OutputKey=='InstanceSecurityGroupId'].OutputValue" --output text)
aws ec2 authorize-security-group-ingress --group-id $SG --protocol tcp --port 22 --cidr 0.0.0.0/0 --region $R
```

**Datadog:** CSM Misconfigurations "Security group allows unrestricted SSH" (next resource scan;
can take up to about an hour); CloudTrail `AuthorizeSecurityGroupIngress` immediately.
**Revert:**
```bash
aws ec2 revoke-security-group-ingress --group-id $SG --protocol tcp --port 22 --cidr 0.0.0.0/0 --region $R
```

The Classic teardown removes the group anyway.

---

## 6. CloudFront origin failure

Not a button because it breaks `/api/*`, and with it the Chaos page, and each change takes
5–10 minutes to deploy. Use the direct API endpoint (stack output `ApiEndpoint`) while it's broken.

1. CloudFront console → the AvionPulse distribution → **Origins** → `api` → Edit.
2. Change *Origin domain* to `nonexistent.execute-api.<R>.amazonaws.com` → Save. Wait for *Deployed*.
3. Load the site: the SPA loads (S3 origin) and every API call returns **502**.

**Datadog:** `aws.cloudfront.5xx_error_rate` (enable *additional metrics* on the distribution for
4xx/5xx by code: about $0.01/day); RUM resource errors; Synthetics browser test failing.
**Revert:** set the origin domain back to `<ApiId>.execute-api.<R>.amazonaws.com` (stack output
`ApiEndpoint` without `https://`), or redeploy with `scripts/deploy-cfn.sh`.

---

## 7. Lambda faults with AWS FIS

FIS can inject delay or errors **into Lambda invocations**. It needs the AWS FIS Lambda extension
layer, an S3 configuration location and two environment variables on the target function. The
built-in `latency_spike` and `random_500s` faults cover the same symptoms, so use this to
demonstrate FIS itself.

1. Find the FIS extension layer ARN for your region and architecture (arm64) in the AWS FIS
   documentation (*"AWS FIS Lambda extension"*).
2. On `<S>-worker` (pick a non-critical function): add the layer, then set
   `AWS_FIS_CONFIGURATION_LOCATION=arn:aws:s3:::<bucket>/fis-config/` and
   `AWS_LAMBDA_EXEC_WRAPPER=/opt/aws-fis/bootstrap`. Grant its role `s3:GetObject` and `s3:ListBucket` on that prefix.
3. Create an experiment template with action `aws:lambda:invocation-add-delay`
   (`startupDelayMilliseconds: 2000`, `invocationPercentage: 50`, `duration: PT5M`), target
   `aws:lambda:function` = the worker, and give the FIS role `s3:PutObject` / `s3:DeleteObject` on the prefix.
4. Start it from the FIS console.

**Datadog:** worker `aws.lambda.duration` p95 jump, `aws.lambda.enhanced.timeouts` if delay > timeout.
**Revert:** the experiment ends on its own. Remove the layer and env vars, or redeploy the stack, which puts the function back.

---

## 8. Account Lambda concurrency exhaustion

Real "account at its concurrency limit" throttling affects **every function in the account**, so
only do this in a sandbox account. Reserved concurrency can't exceed what is unreserved, which
is why the app's `worker_throttle` sets 0 instead.

**Monitor without causing it:**
* `aws.lambda.concurrent_executions` (account level) vs. your account quota (Service Quotas
  console, *AWS Lambda → Concurrent executions*). New accounts can have a quota as low as 10.
* `aws.lambda.unreserved_concurrent_executions`.
* Datadog monitor: `max:aws.lambda.concurrent_executions{*} > 0.8 * <quota>`.

**Causing it (sandbox only):** `python scripts/loadgen.py <url> --rps 20 --minutes 2` with
`latency_spike` on fills the concurrency of a low-quota account. Watch `aws.lambda.throttles`
across all functions. Stop loadgen to revert.

---

## 9. Cost anomaly

1. Launch the Classic tier at **Full** for 8 hours and leave `log_flood` on for an hour under
   `loadgen.py --rps 1` (≈ $1–2 total).
2. **Datadog Cloud Cost Management** (needs the CUR / Data Exports setup) shows the spike about
   24 h later, broken down by `tier:classic` and `app:avionpulse` tags (activate them as cost
   allocation tags in the Billing console first).
3. **AWS Cost Anomaly Detection** (free) can email you. Its alerts can also be routed into Datadog via SNS.

**Revert:** tear down the Classic tier; `log_flood` expires with its TTL.

---

## 10. Regional dependency failure

You can't fail an AWS region. The useful rehearsal is "a dependency's regional endpoint is unreachable":

* `downstream_timeout` (third-party API) and `classic_nacl_db_block` (database endpoint) are the in-app versions.
* With the Classic tier at **Full**, `fis_az_outage` is the largest real blast radius you can cause.
* For a tabletop exercise, create a Datadog **Synthetics** API test from several locations against
  `/api/health/deep`, then use a **Downtime** to show what on-call sees during a planned outage.
