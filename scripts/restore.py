"""Break-glass: put every AWS resource a chaos fault can change back to its
template baseline, using the AWS CLI with YOUR credentials — no app needed.

Use it when a fault has broken the site so badly that the Chaos page can't be
reached (or you just want to be sure nothing is left drifted). The janitor
Lambda does the same for expired faults every 2 minutes; this is for "now".

    python scripts/restore.py                       # stack "avionpulse", $AWS_REGION or us-east-1
    python scripts/restore.py --stack demo --region eu-west-1
    python scripts/restore.py --classic-down        # also delete the Classic stack

Standard library only; every step is independent and reports OK / FAILED.
"""
import argparse
import json
import os
import subprocess
import sys

ARGS = None


def aws(*args, query=None, text=True):
    cmd = ["aws", "--region", ARGS.region, *args]
    if query:
        cmd += ["--query", query]
    cmd += ["--output", "text" if text else "json"]
    out = subprocess.run(cmd, capture_output=True, text=True, shell=(os.name == "nt"))
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else f"exit {out.returncode}")
    return out.stdout.strip() if text else json.loads(out.stdout or "null")


def step(name, fn):
    try:
        note = fn()
        print(f"  OK      {name}" + (f" — {note}" if note else ""))
    except Exception as e:  # noqa: BLE001 — keep going, report everything
        print(f"  FAILED  {name} — {e}")


def main():
    global ARGS
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--stack", default=os.environ.get("STACK_NAME", "avionpulse"))
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    p.add_argument("--classic-down", action="store_true", help="also delete the Classic stack if it exists")
    ARGS = a = p.parse_args()

    stack = aws("cloudformation", "describe-stacks", "--stack-name", a.stack, query="Stacks[0]", text=False)
    out = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    params = {x["ParameterKey"]: x.get("ParameterValue") for x in stack.get("Parameters", [])}
    acct = stack["StackId"].split(":")[4]
    fn = lambda n: f"arn:aws:lambda:{a.region}:{acct}:function:{a.stack}-{n}"  # noqa: E731
    worker, api, processor = f"{a.stack}-worker", f"{a.stack}-api", f"{a.stack}-processor"
    print(f"Restoring {a.stack} in {a.region}")

    def stage():
        aws("apigatewayv2", "update-stage", "--api-id", out["ApiId"], "--stage-name", "$default",
            "--default-route-settings",
            f"ThrottlingRateLimit={params.get('ApiRateLimit', '20')},ThrottlingBurstLimit={params.get('ApiBurstLimit', '40')}")
    step("API Gateway stage throttle", stage)
    step("API Gateway data integration", lambda: aws(
        "apigatewayv2", "update-integration", "--api-id", out["ApiId"],
        "--integration-id", out["ApiIntegrationId"], "--integration-uri", fn("api")) and None)
    step("worker reserved concurrency", lambda: aws("lambda", "delete-function-concurrency",
                                                     "--function-name", worker) and None)

    def env_fix(name, drop=(), handler=None):
        cfg = aws("lambda", "get-function-configuration", "--function-name", name, text=False)
        env = dict(cfg.get("Environment", {}).get("Variables", {}))
        changed = False
        for k in drop:
            changed |= env.pop(k, None) is not None
        args = []
        if handler:
            if "DD_LAMBDA_HANDLER" in env and env["DD_LAMBDA_HANDLER"] != handler:
                env["DD_LAMBDA_HANDLER"], changed = handler, True
            elif "DD_LAMBDA_HANDLER" not in env and cfg["Handler"] != handler:
                args = ["--handler", handler]
        if changed:
            args += ["--environment", json.dumps({"Variables": env})]
        if args:
            aws("lambda", "update-function-configuration", "--function-name", name, *args)
            return "updated"
        return "already baseline"
    step("api function slow-init env", lambda: env_fix(api, drop=("CHAOS_SLOW_INIT_MS",)))
    step("worker handler", lambda: env_fix(worker, handler="worker.handler"))

    def esm():
        for u in aws("lambda", "list-event-source-mappings", "--function-name", worker,
                     query="EventSourceMappings[].UUID", text=False) or []:
            aws("lambda", "update-event-source-mapping", "--uuid", u, "--enabled")
    step("SQS → worker trigger", esm)

    def table():
        pt = aws("dynamodb", "describe-table", "--table-name", out["TableName"],
                 query="Table.ProvisionedThroughput", text=False)
        if (pt["ReadCapacityUnits"], pt["WriteCapacityUnits"]) == (5, 5):
            return "already 5/5"
        aws("dynamodb", "update-table", "--table-name", out["TableName"],
            "--provisioned-throughput", "ReadCapacityUnits=5,WriteCapacityUnits=5")
    step("DynamoDB capacity 5/5", table)
    step("simulator schedule rule", lambda: aws("events", "enable-rule", "--name", f"{a.stack}-simulator-tick") and None)
    step("worker data policy", lambda: aws("iam", "attach-role-policy", "--role-name", out["WorkerRoleName"],
                                            "--policy-arn", out["WorkerDataPolicyArn"]) and None)
    step("KMS key", lambda: aws("kms", "enable-key", "--key-id", out["EventsKeyId"]) and None)
    step("SQS queue policy + retention", lambda: aws(
        "sqs", "set-queue-attributes", "--queue-url", out["QueueUrl"],
        "--attributes", json.dumps({"Policy": "", "MessageRetentionPeriod": "86400"})) and None)

    def recorder():
        try:
            aws("s3api", "delete-bucket-policy", "--bucket", out["RecorderBucketName"])
        except RuntimeError as e:
            if "NoSuchBucketPolicy" not in str(e):
                raise
    step("recorder bucket policy", recorder)

    def sns_perm():
        topic = f"arn:aws:sns:{a.region}:{acct}:{a.stack}-crew-alerts"
        try:
            pol = json.loads(aws("lambda", "get-policy", "--function-name", processor, query="Policy"))
            if any((s.get("Principal") or {}).get("Service") == "sns.amazonaws.com" for s in pol["Statement"]):
                return "already present"
        except RuntimeError:
            pass
        aws("lambda", "add-permission", "--function-name", processor, "--statement-id", "avionpulse-sns-crew-alerts",
            "--action", "lambda:InvokeFunction", "--principal", "sns.amazonaws.com", "--source-arn", topic)
    step("processor SNS permission", sns_perm)

    def bus():
        rule, bus_name = f"{a.stack}-flight-status", f"{a.stack}-flight-ops"
        targets = aws("events", "list-targets-by-rule", "--rule", rule, "--event-bus-name", bus_name,
                      query="Targets", text=False)
        for t in targets:
            t["Arn"] = fn("processor")
        aws("events", "put-targets", "--rule", rule, "--event-bus-name", bus_name, "--targets", json.dumps(targets))
    step("EventBridge bus target", bus)
    step("SSM feature flags", lambda: aws(
        "ssm", "put-parameter", "--name", f"/{a.stack}/config/flags", "--type", "String", "--overwrite",
        "--value", '{"board_page_size": 100, "weather_enabled": true, "maintenance_intake": true}') and None)
    step("chaos state (CONFIG/FAULTS)", lambda: aws(
        "dynamodb", "put-item", "--table-name", out["TableName"],
        "--item", json.dumps({"PK": {"S": "CONFIG"}, "SK": {"S": "FAULTS"}, "faults": {"M": {}}})) and None)

    if a.classic_down:
        def classic():
            name = f"{a.stack}-classic"
            try:
                aws("cloudformation", "describe-stacks", "--stack-name", name)
            except RuntimeError:
                return "no Classic stack"
            aws("cloudformation", "delete-stack", "--stack-name", name, "--role-arn", out["ClassicCfnRoleArn"])
            try:
                aws("scheduler", "delete-schedule", "--name", f"{name}-expiry")
            except RuntimeError:
                pass
            return "delete started (~10 min)"
        step("Classic stack", classic)
    print("Done. Classic-tier network/ASG/ALB faults are not touched here — tear the Classic stack down "
          "(--classic-down) or wait for the janitor.")


if __name__ == "__main__":
    sys.exit(main())
