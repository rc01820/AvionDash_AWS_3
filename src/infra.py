"""Calls to AWS services other than DynamoDB: SQS, S3, Lambda, API Gateway,
SNS, EventBridge, Step Functions, SSM, KMS and IAM.

The chaos engine uses the control-plane calls here (stage throttling, reserved
concurrency, event source mappings, table capacity, rule state, key state,
resource policies, role attachments) to break real AWS resources and put them
back. drift() reads every one of those settings back so the UI can show what is
currently different from the template. In local mode every call is simulated.
"""
import json
import logging
import os
import threading
import time
import uuid

import config
from store import SimulatedAwsError

log = logging.getLogger("avionpulse.infra")

_clients = {}
_clients_lock = threading.Lock()


def _client(name):
    if name not in _clients:
        with _clients_lock:
            if name not in _clients:
                import boto3
                from botocore.config import Config

                _clients[name] = boto3.client(
                    name,
                    config=Config(retries={"mode": "standard", "max_attempts": 3},
                                  connect_timeout=3, read_timeout=5),
                )
    return _clients[name]


# ── Local simulation state ────────────────────────────────────────────────
LOCAL_QUEUE = []          # messages waiting for the worker
LOCAL_DLQ = []
LOCAL_SIM = {             # pretend control-plane state
    "api_throttle": None,
    "worker_concurrency": None,
    "esm_enabled": True,
    "api_env": {},
    "table_capacity": (config.DEFAULT_RCU, config.DEFAULT_WCU),
    "sim_rule_enabled": True,
    "integration_broken": False,
    "worker_handler": "worker.handler",
    "worker_policy_attached": True,
    "kms_enabled": True,
    "queue_policy": "",
    "queue_retention": config.QUEUE_RETENTION_S,
    "recorder_policy": "",
    "sns_permission": True,
    "bus_target_broken": False,
    "flags": json.dumps(config.DEFAULT_FLAGS),
}
LOCAL_RECORDER = []       # keys "written" to the recorder bucket
_local_lock = threading.Lock()


# ── SQS ───────────────────────────────────────────────────────────────────
def send_event(message):
    body = json.dumps(message, default=str)
    if config.LOCAL:
        _local_guard_send()
        with _local_lock:
            LOCAL_QUEUE.append({"messageId": str(uuid.uuid4()), "body": body,
                                "receiveCount": 0, "sent": time.time()})
        return
    _client("sqs").send_message(QueueUrl=config.QUEUE_URL, MessageBody=body)


def queue_depths():
    if config.LOCAL:
        with _local_lock:
            oldest = min((m["sent"] for m in LOCAL_QUEUE), default=None)
            return {
                "queue_visible": len(LOCAL_QUEUE),
                "queue_in_flight": 0,
                "queue_oldest_age_s": int(time.time() - oldest) if oldest else 0,
                "dlq_visible": len(LOCAL_DLQ),
            }
    sqs = _client("sqs")
    attrs = ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"]
    q = sqs.get_queue_attributes(QueueUrl=config.QUEUE_URL, AttributeNames=attrs)["Attributes"]
    d = sqs.get_queue_attributes(QueueUrl=config.DLQ_URL, AttributeNames=attrs[:1])["Attributes"]
    return {
        "queue_visible": int(q.get("ApproximateNumberOfMessages", 0)),
        "queue_in_flight": int(q.get("ApproximateNumberOfMessagesNotVisible", 0)),
        "queue_oldest_age_s": None,  # CloudWatch-only metric (ApproximateAgeOfOldestMessage)
        "dlq_visible": int(d.get("ApproximateNumberOfMessages", 0)),
    }


# ── S3 ────────────────────────────────────────────────────────────────────
def get_s3_json(key):
    if config.LOCAL:
        path = os.path.join(config.LOCAL_WEB_ROOT, *key.split("/"))
        if not os.path.exists(path):
            raise SimulatedAwsError("NoSuchKey", f"The specified key does not exist: {key}")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    obj = _client("s3").get_object(Bucket=config.BUCKET, Key=key)
    return json.loads(obj["Body"].read())


# ── API Gateway (HTTP API stage throttling) ───────────────────────────────
def set_api_throttle(api_id, rate, burst):
    if config.LOCAL:
        LOCAL_SIM["api_throttle"] = (rate, burst)
        log.warning("[LOCAL] apigatewayv2.update_stage throttle rate=%s burst=%s", rate, burst)
        return
    api_id = api_id or config.API_ID
    if not api_id:
        raise RuntimeError("API id unknown — toggle this fault from the web UI")
    _client("apigatewayv2").update_stage(
        ApiId=api_id,
        StageName="$default",
        DefaultRouteSettings={"ThrottlingRateLimit": rate, "ThrottlingBurstLimit": burst},
    )


# ── Lambda: reserved concurrency on the SQS worker ────────────────────────
def set_worker_concurrency_zero(enabled):
    if config.LOCAL:
        LOCAL_SIM["worker_concurrency"] = 0 if enabled else None
        log.warning("[LOCAL] lambda.%s_function_concurrency %s",
                    "put" if enabled else "delete", config.WORKER_FN)
        return
    lam = _client("lambda")
    if enabled:
        lam.put_function_concurrency(FunctionName=config.WORKER_FN, ReservedConcurrentExecutions=0)
    else:
        lam.delete_function_concurrency(FunctionName=config.WORKER_FN)


# ── Lambda: pause/resume the SQS -> worker event source mapping ───────────
def set_worker_trigger(enabled):
    if config.LOCAL:
        LOCAL_SIM["esm_enabled"] = enabled
        log.warning("[LOCAL] lambda.update_event_source_mapping Enabled=%s", enabled)
        return
    lam = _client("lambda")
    for m in lam.list_event_source_mappings(FunctionName=config.WORKER_FN)["EventSourceMappings"]:
        lam.update_event_source_mapping(UUID=m["UUID"], Enabled=enabled)


def worker_paused():
    """True when queue_backlog or worker_throttle should stop local draining."""
    return config.LOCAL and (not LOCAL_SIM["esm_enabled"] or LOCAL_SIM["worker_concurrency"] == 0
                             or not LOCAL_SIM["kms_enabled"])


# ── Lambda: recycle the API function's execution environments ─────────────
def recycle_api_function(slow_init_ms):
    """Touching an env var forces every warm container to be replaced, so the
    next requests all cold-start. slow_init_ms adds a sleep during init."""
    if config.LOCAL:
        env = LOCAL_SIM["api_env"]
        env["CHAOS_NONCE"] = uuid.uuid4().hex[:8]
        if slow_init_ms:
            env["CHAOS_SLOW_INIT_MS"] = str(slow_init_ms)
        else:
            env.pop("CHAOS_SLOW_INIT_MS", None)
        log.warning("[LOCAL] lambda.update_function_configuration %s env=%s", config.API_FN, env)
        return
    lam = _client("lambda")
    for attempt in range(3):
        cur = lam.get_function_configuration(FunctionName=config.API_FN)
        env = dict(cur.get("Environment", {}).get("Variables", {}))
        env["CHAOS_NONCE"] = uuid.uuid4().hex[:8]
        if slow_init_ms:
            env["CHAOS_SLOW_INIT_MS"] = str(slow_init_ms)
        else:
            env.pop("CHAOS_SLOW_INIT_MS", None)
        try:
            lam.update_function_configuration(FunctionName=config.API_FN,
                                              Environment={"Variables": env})
            return
        except lam.exceptions.ResourceConflictException:
            # A previous update is still rolling out.
            time.sleep(2 + attempt * 2)
    raise RuntimeError("Lambda configuration update still in progress — try again shortly")


# ── Downstream dependency (downstream_timeout fault) ──────────────────────
def call_weather_provider():
    """10.255.255.1 is unroutable from a non-VPC Lambda: the connect hangs
    until the timeout fires, exactly like a dead third-party API."""
    import urllib.request

    urllib.request.urlopen("http://10.255.255.1:81/v1/metar", timeout=3)


# ══════════════════════════════════════════════════════════════════════════
#  v3 — AWS infrastructure faults
# ══════════════════════════════════════════════════════════════════════════
MISSING_ALIAS = ":chaos-missing-alias"   # a Lambda qualifier that does not exist
BAD_WORKER_HANDLER = "worker_v2.handler"  # a module that was never deployed
GOOD_WORKER_HANDLER = "worker.handler"
POISON_FLAGS = '{"board_page_size": 100,, "weather_enabled": tru'   # truncated, invalid JSON
LOCAL_COUNTERS = {"sns_failed": 0, "bus_failed": 0}


def _local_guard_send():
    if LOCAL_SIM["queue_policy"]:
        raise SimulatedAwsError("AccessDenied", f"User: {config.APP_ROLE_ARN} is not authorized to perform: "
                                "sqs:SendMessage on resource: avionpulse-events with an explicit deny")
    if not LOCAL_SIM["kms_enabled"]:
        raise SimulatedAwsError("KMS.DisabledException", f"{config.KMS_KEY_ID} is disabled.")


def _retry_conflict(fn):
    """Lambda rejects a config update while the previous one is still rolling out."""
    lam = _client("lambda")
    for attempt in range(3):
        try:
            return fn()
        except lam.exceptions.ResourceConflictException:
            time.sleep(2 + attempt * 2)
    raise RuntimeError("Lambda configuration update still in progress — try again shortly")


# ── DynamoDB: provisioned capacity ────────────────────────────────────────
def set_table_capacity(rcu, wcu):
    if config.LOCAL:
        LOCAL_SIM["table_capacity"] = (rcu, wcu)
        log.warning("[LOCAL] dynamodb.update_table %s RCU=%s WCU=%s", config.TABLE_NAME, rcu, wcu)
        return
    ddb = _client("dynamodb")
    cur = ddb.describe_table(TableName=config.TABLE_NAME)["Table"]["ProvisionedThroughput"]
    if (cur["ReadCapacityUnits"], cur["WriteCapacityUnits"]) == (rcu, wcu):
        return
    ddb.update_table(TableName=config.TABLE_NAME,
                     ProvisionedThroughput={"ReadCapacityUnits": rcu, "WriteCapacityUnits": wcu})


def local_capacity_throttled():
    """dynamodb_capacity_cut in local mode: 1/1 capacity throttles about a third of calls."""
    import random
    return LOCAL_SIM["table_capacity"][1] < config.DEFAULT_WCU and random.random() < 0.35


def table_capacity():
    if config.LOCAL:
        return LOCAL_SIM["table_capacity"]
    pt = _client("dynamodb").describe_table(TableName=config.TABLE_NAME)["Table"]["ProvisionedThroughput"]
    return pt["ReadCapacityUnits"], pt["WriteCapacityUnits"]


# ── EventBridge: the simulator's schedule rule ────────────────────────────
def set_sim_rule_enabled(enabled):
    if config.LOCAL:
        LOCAL_SIM["sim_rule_enabled"] = enabled
        log.warning("[LOCAL] events.%s_rule %s", "enable" if enabled else "disable", config.SIM_RULE)
        return
    ev = _client("events")
    (ev.enable_rule if enabled else ev.disable_rule)(Name=config.SIM_RULE)


def sim_rule_enabled():
    if config.LOCAL:
        return LOCAL_SIM["sim_rule_enabled"]
    return _client("events").describe_rule(Name=config.SIM_RULE)["State"] == "ENABLED"


# ── API Gateway: data-route integration points at a missing alias ─────────
def set_integration_broken(broken):
    if config.LOCAL:
        LOCAL_SIM["integration_broken"] = broken
        log.warning("[LOCAL] apigatewayv2.update_integration broken=%s", broken)
        return
    uri = config.fn_arn(config.API_FN) + (MISSING_ALIAS if broken else "")
    _client("apigatewayv2").update_integration(ApiId=config.API_ID, IntegrationId=config.API_INTEGRATION_ID,
                                               IntegrationUri=uri)


def integration_uri():
    if config.LOCAL:
        return config.fn_arn(config.API_FN) + (MISSING_ALIAS if LOCAL_SIM["integration_broken"] else "")
    return _client("apigatewayv2").get_integration(ApiId=config.API_ID,
                                                   IntegrationId=config.API_INTEGRATION_ID)["IntegrationUri"]


# ── Lambda: "bad deploy" of the worker (handler module missing) ───────────
def set_worker_bad_deploy(bad):
    target = BAD_WORKER_HANDLER if bad else GOOD_WORKER_HANDLER
    if config.LOCAL:
        LOCAL_SIM["worker_handler"] = target
        log.warning("[LOCAL] lambda.update_function_configuration %s handler=%s", config.WORKER_FN, target)
        return
    lam = _client("lambda")

    def update():
        cur = lam.get_function_configuration(FunctionName=config.WORKER_FN)
        env = dict(cur.get("Environment", {}).get("Variables", {}))
        if "DD_LAMBDA_HANDLER" in env:   # Datadog wrapper imports this module at init
            env["DD_LAMBDA_HANDLER"] = target
            lam.update_function_configuration(FunctionName=config.WORKER_FN, Environment={"Variables": env})
        else:
            lam.update_function_configuration(FunctionName=config.WORKER_FN, Handler=target)
    _retry_conflict(update)


def worker_handler():
    if config.LOCAL:
        return LOCAL_SIM["worker_handler"]
    cur = _client("lambda").get_function_configuration(FunctionName=config.WORKER_FN)
    return cur.get("Environment", {}).get("Variables", {}).get("DD_LAMBDA_HANDLER") or cur["Handler"]


# ── IAM: detach the worker's data-access policy ───────────────────────────
def set_worker_policy_attached(attached):
    if config.LOCAL:
        LOCAL_SIM["worker_policy_attached"] = attached
        log.warning("[LOCAL] iam.%s_role_policy %s", "attach" if attached else "detach", config.WORKER_ROLE)
        return
    iam = _client("iam")
    if attached:
        iam.attach_role_policy(RoleName=config.WORKER_ROLE, PolicyArn=config.WORKER_DATA_POLICY_ARN)
    else:
        try:
            iam.detach_role_policy(RoleName=config.WORKER_ROLE, PolicyArn=config.WORKER_DATA_POLICY_ARN)
        except iam.exceptions.NoSuchEntityException:
            pass


def worker_policy_attached():
    if config.LOCAL:
        return LOCAL_SIM["worker_policy_attached"]
    pols = _client("iam").list_attached_role_policies(RoleName=config.WORKER_ROLE)["AttachedPolicies"]
    return any(p["PolicyArn"] == config.WORKER_DATA_POLICY_ARN for p in pols)


# ── KMS: the key that encrypts the events queue ───────────────────────────
def set_kms_enabled(enabled):
    if config.LOCAL:
        LOCAL_SIM["kms_enabled"] = enabled
        log.warning("[LOCAL] kms.%s_key %s", "enable" if enabled else "disable", config.KMS_KEY_ID)
        return
    kms = _client("kms")
    (kms.enable_key if enabled else kms.disable_key)(KeyId=config.KMS_KEY_ID)


def kms_state():
    if config.LOCAL:
        return "Enabled" if LOCAL_SIM["kms_enabled"] else "Disabled"
    return _client("kms").describe_key(KeyId=config.KMS_KEY_ID)["KeyMetadata"]["KeyState"]


# ── SQS: queue policy and retention ───────────────────────────────────────
def _queue_arn():
    return _client("sqs").get_queue_attributes(QueueUrl=config.QUEUE_URL,
                                               AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]


def set_queue_send_denied(denied):
    if config.LOCAL:
        LOCAL_SIM["queue_policy"] = "deny-send" if denied else ""
        log.warning("[LOCAL] sqs.set_queue_attributes Policy=%s", LOCAL_SIM["queue_policy"] or "<none>")
        return
    policy = ""
    if denied:
        policy = json.dumps({"Version": "2012-10-17", "Statement": [{
            "Sid": "AvionPulseChaosDenySend", "Effect": "Deny",
            "Principal": {"AWS": config.APP_ROLE_ARN}, "Action": "sqs:SendMessage", "Resource": _queue_arn()}]})
    _client("sqs").set_queue_attributes(QueueUrl=config.QUEUE_URL, Attributes={"Policy": policy})


def set_queue_retention(seconds):
    if config.LOCAL:
        LOCAL_SIM["queue_retention"] = seconds
        log.warning("[LOCAL] sqs.set_queue_attributes MessageRetentionPeriod=%s", seconds)
        return
    _client("sqs").set_queue_attributes(QueueUrl=config.QUEUE_URL,
                                        Attributes={"MessageRetentionPeriod": str(seconds)})


def queue_settings():
    if config.LOCAL:
        return {"policy": LOCAL_SIM["queue_policy"], "retention": LOCAL_SIM["queue_retention"]}
    a = _client("sqs").get_queue_attributes(QueueUrl=config.QUEUE_URL,
                                            AttributeNames=["Policy", "MessageRetentionPeriod"])["Attributes"]
    return {"policy": a.get("Policy", ""), "retention": int(a.get("MessageRetentionPeriod", 0))}


# ── S3: flight data recorder bucket ───────────────────────────────────────
def set_recorder_denied(denied):
    if config.LOCAL:
        LOCAL_SIM["recorder_policy"] = "deny-put" if denied else ""
        log.warning("[LOCAL] s3.%s_bucket_policy %s", "put" if denied else "delete", config.RECORDER_BUCKET)
        return
    s3 = _client("s3")
    if denied:
        s3.put_bucket_policy(Bucket=config.RECORDER_BUCKET, Policy=json.dumps({
            "Version": "2012-10-17", "Statement": [{
                "Sid": "AvionPulseChaosDenyWrites", "Effect": "Deny",
                "Principal": {"AWS": config.APP_ROLE_ARN}, "Action": "s3:PutObject",
                "Resource": f"arn:aws:s3:::{config.RECORDER_BUCKET}/*"}]}))
    else:
        s3.delete_bucket_policy(Bucket=config.RECORDER_BUCKET)


def recorder_policy():
    if config.LOCAL:
        return LOCAL_SIM["recorder_policy"]
    s3 = _client("s3")
    try:
        return s3.get_bucket_policy(Bucket=config.RECORDER_BUCKET)["Policy"]
    except Exception as e:
        if getattr(e, "response", {}).get("Error", {}).get("Code") == "NoSuchBucketPolicy":
            return ""
        raise


def put_recorder_snapshot(key, doc):
    body = json.dumps(doc, default=str).encode()
    if config.LOCAL:
        if LOCAL_SIM["recorder_policy"]:
            raise SimulatedAwsError("AccessDenied", "Access Denied (explicit deny in bucket policy)")
        LOCAL_RECORDER.append(key)
        _local_deliver({"source": "aws.s3", "detail-type": "Object Created",
                        "detail": {"bucket": {"name": config.RECORDER_BUCKET},
                                   "object": {"key": key, "size": len(body)}}})
        return
    _client("s3").put_object(Bucket=config.RECORDER_BUCKET, Key=key, Body=body, ContentType="application/json")


# ── SNS: crew alerts → processor Lambda ───────────────────────────────────
def _sns_statements():
    lam = _client("lambda")
    try:
        pol = json.loads(lam.get_policy(FunctionName=config.PROCESSOR_FN)["Policy"])
    except lam.exceptions.ResourceNotFoundException:
        return []
    return [s for s in pol.get("Statement", [])
            if (s.get("Principal") or {}).get("Service") == "sns.amazonaws.com"]


def set_sns_permission(present):
    if config.LOCAL:
        LOCAL_SIM["sns_permission"] = present
        log.warning("[LOCAL] lambda.%s_permission sns -> %s", "add" if present else "remove", config.PROCESSOR_FN)
        return
    lam = _client("lambda")
    found = _sns_statements()
    if not present:
        for s in found:
            lam.remove_permission(FunctionName=config.PROCESSOR_FN, StatementId=s["Sid"])
    elif not found:
        lam.add_permission(FunctionName=config.PROCESSOR_FN, StatementId="avionpulse-sns-crew-alerts",
                           Action="lambda:InvokeFunction", Principal="sns.amazonaws.com",
                           SourceArn=config.TOPIC_ARN)


def sns_permission_present():
    if config.LOCAL:
        return LOCAL_SIM["sns_permission"]
    return bool(_sns_statements())


def publish_alert(subject, message):
    if config.LOCAL:
        if LOCAL_SIM["sns_permission"]:
            _local_deliver({"Records": [{"EventSource": "aws:sns",
                                         "Sns": {"Subject": subject, "Message": json.dumps(message)}}]})
        else:
            LOCAL_COUNTERS["sns_failed"] += 1
            log.error("[LOCAL] SNS delivery to %s failed: lambda:InvokeFunction permission missing", config.PROCESSOR_FN)
        return
    _client("sns").publish(TopicArn=config.TOPIC_ARN, Subject=subject[:100], Message=json.dumps(message, default=str))


# ── EventBridge custom bus: FlightStatusChanged → processor ───────────────
def set_bus_target_broken(broken):
    if config.LOCAL:
        LOCAL_SIM["bus_target_broken"] = broken
        log.warning("[LOCAL] events.put_targets %s broken=%s", config.BUS_RULE, broken)
        return
    ev = _client("events")
    targets = ev.list_targets_by_rule(Rule=config.BUS_RULE, EventBusName=config.BUS_NAME)["Targets"]
    for t in targets:
        t["Arn"] = config.fn_arn(config.PROCESSOR_FN) + (MISSING_ALIAS if broken else "")
    ev.put_targets(Rule=config.BUS_RULE, EventBusName=config.BUS_NAME, Targets=targets)


def bus_target_arn():
    if config.LOCAL:
        return config.fn_arn(config.PROCESSOR_FN) + (MISSING_ALIAS if LOCAL_SIM["bus_target_broken"] else "")
    targets = _client("events").list_targets_by_rule(Rule=config.BUS_RULE, EventBusName=config.BUS_NAME)["Targets"]
    return targets[0]["Arn"] if targets else "<no target>"


def put_bus_events(details):
    if not details:
        return
    if config.LOCAL:
        for d in details:
            if LOCAL_SIM["bus_target_broken"]:
                LOCAL_COUNTERS["bus_failed"] += 1
            else:
                _local_deliver({"source": "avionpulse.simulator", "detail-type": "FlightStatusChanged", "detail": d})
        return
    entries = [{"Source": "avionpulse.simulator", "DetailType": "FlightStatusChanged",
                "Detail": json.dumps(d, default=str), "EventBusName": config.BUS_NAME} for d in details]
    for i in range(0, len(entries), 10):
        _client("events").put_events(Entries=entries[i:i + 10])


# ── Step Functions: aircraft turnaround workflow ──────────────────────────
def start_turnaround(payload):
    if config.LOCAL:
        threading.Thread(target=_local_turnaround, args=(payload,), daemon=True).start()
        return
    _client("stepfunctions").start_execution(stateMachineArn=config.STATE_MACHINE_ARN,
                                             input=json.dumps(payload, default=str))


def _local_turnaround(payload):
    import processor
    try:
        out = processor.handler(dict(payload, task="assign_gate"), None)
        t = time.time()
        out = processor.handler(dict(out, task="crew_check"), None)
        if time.time() - t > 5:  # the state machine's TimeoutSeconds on CrewCheck
            raise SimulatedAwsError("States.Timeout", "CrewCheck exceeded TimeoutSeconds=5")
        processor.handler(dict(out, task="complete"), None)
    except Exception as e:  # noqa: BLE001
        processor.record("turnaround_failed", {"flight": payload.get("flight"), "error": str(e)[:200]})
        log.error("[LOCAL] turnaround execution failed: %s", e)


def _local_deliver(event):
    import processor
    try:
        processor.handler(event, None)
    except Exception as e:  # noqa: BLE001
        log.error("[LOCAL] processor failed: %s", e)


def local_stream(items):
    """Local stand-in for the DynamoDB stream → processor event source mapping."""
    import processor
    records = [{"eventSource": "aws:dynamodb", "eventName": "MODIFY",
                "dynamodb": {"Keys": {"PK": {"S": i["PK"]}, "SK": {"S": i["SK"]}}}} for i in items]
    try:
        processor.handler({"Records": records}, None)
    except Exception as e:  # noqa: BLE001
        log.error("[LOCAL] stream batch failed (would be retried, iterator age climbs): %s", e)


# ── SSM Parameter Store: feature flags ────────────────────────────────────
class ConfigError(Exception):
    """Feature-flag parameter could not be parsed."""


_flags_cache = {"at": 0.0, "raw": None}


def get_flags():
    now = time.time()
    if _flags_cache["raw"] is None or now - _flags_cache["at"] > 15:
        if config.LOCAL:
            raw = LOCAL_SIM["flags"]
        else:
            raw = _client("ssm").get_parameter(Name=config.FLAGS_PARAM)["Parameter"]["Value"]
        _flags_cache.update(at=now, raw=raw)
    try:
        return dict(config.DEFAULT_FLAGS, **json.loads(_flags_cache["raw"]))
    except ValueError as e:
        raise ConfigError(f"{config.FLAGS_PARAM} is not valid JSON: {e}")


def set_flags_poisoned(poisoned):
    value = POISON_FLAGS if poisoned else json.dumps(config.DEFAULT_FLAGS)
    _flags_cache["raw"] = None
    if config.LOCAL:
        LOCAL_SIM["flags"] = value
        log.warning("[LOCAL] ssm.put_parameter %s", config.FLAGS_PARAM)
        return
    _client("ssm").put_parameter(Name=config.FLAGS_PARAM, Value=value, Type="String", Overwrite=True)


def flags_raw():
    if config.LOCAL:
        return LOCAL_SIM["flags"]
    return _client("ssm").get_parameter(Name=config.FLAGS_PARAM)["Parameter"]["Value"]


# ── Read-back of everything above (UI drift panel) ────────────────────────
def _stage_throttle():
    if config.LOCAL:
        t = LOCAL_SIM["api_throttle"]
        return f"{t[0]:g} rps / burst {t[1]}" if t else f"{config.DEFAULT_RATE:g} rps / burst {config.DEFAULT_BURST}"
    s = _client("apigatewayv2").get_stage(ApiId=config.API_ID, StageName="$default")["DefaultRouteSettings"]
    return f"{s.get('ThrottlingRateLimit', 0):g} rps / burst {s.get('ThrottlingBurstLimit', 0)}"


def _worker_concurrency():
    if config.LOCAL:
        c = LOCAL_SIM["worker_concurrency"]
    else:
        c = _client("lambda").get_function_concurrency(FunctionName=config.WORKER_FN).get("ReservedConcurrentExecutions")
    return "unreserved" if c is None else str(c)


def _worker_trigger():
    if config.LOCAL:
        return "Enabled" if LOCAL_SIM["esm_enabled"] else "Disabled"
    maps = _client("lambda").list_event_source_mappings(FunctionName=config.WORKER_FN)["EventSourceMappings"]
    return ", ".join(sorted({m["State"] for m in maps})) or "<none>"


def _slow_init():
    if config.LOCAL:
        return LOCAL_SIM["api_env"].get("CHAOS_SLOW_INIT_MS", "unset")
    env = _client("lambda").get_function_configuration(FunctionName=config.API_FN).get("Environment", {})
    return env.get("Variables", {}).get("CHAOS_SLOW_INIT_MS", "unset")


def _flags_state():
    try:
        json.loads(flags_raw())
        return "valid JSON"
    except ValueError:
        return "INVALID JSON"


DRIFT_CHECKS = [
    # fault, resource, setting, baseline, reader
    ("api_throttling", "API Gateway stage $default", "default route throttle",
     lambda: f"{config.DEFAULT_RATE:g} rps / burst {config.DEFAULT_BURST}", _stage_throttle),
    ("api_integration_broken", "API Gateway data integration", "target",
     lambda: "api function", lambda: "MISSING ALIAS" if integration_uri().endswith(MISSING_ALIAS) else "api function"),
    ("cold_start_storm", f"Lambda {config.API_FN}", "CHAOS_SLOW_INIT_MS", lambda: "unset", _slow_init),
    ("worker_throttle", f"Lambda {config.WORKER_FN}", "reserved concurrency", lambda: "unreserved", _worker_concurrency),
    ("lambda_bad_deploy", f"Lambda {config.WORKER_FN}", "handler", lambda: GOOD_WORKER_HANDLER, worker_handler),
    ("queue_backlog", "SQS → worker trigger", "event source mapping", lambda: "Enabled", _worker_trigger),
    ("dynamodb_capacity_cut", f"DynamoDB {config.TABLE_NAME}", "provisioned RCU / WCU",
     lambda: f"{config.DEFAULT_RCU} / {config.DEFAULT_WCU}", lambda: "%s / %s" % table_capacity()),
    ("eventbridge_rule_disabled", f"EventBridge rule {config.SIM_RULE}", "state",
     lambda: "ENABLED", lambda: "ENABLED" if sim_rule_enabled() else "DISABLED"),
    ("iam_policy_detached", f"IAM role {config.WORKER_ROLE}", "data policy attached",
     lambda: "attached", lambda: "attached" if worker_policy_attached() else "DETACHED"),
    ("kms_key_disabled", "KMS key (events queue)", "key state", lambda: "Enabled", kms_state),
    ("sqs_send_denied", "SQS events queue", "queue policy",
     lambda: "none", lambda: "DENY SendMessage" if queue_settings()["policy"] else "none"),
    ("sqs_retention_cut", "SQS events queue", "message retention",
     lambda: f"{config.QUEUE_RETENTION_S} s", lambda: f"{queue_settings()['retention']} s"),
    ("s3_bucket_policy_deny", "S3 flight-data recorder", "bucket policy",
     lambda: "none", lambda: "DENY PutObject" if recorder_policy() else "none"),
    ("sns_delivery_failure", f"Lambda {config.PROCESSOR_FN}", "SNS invoke permission",
     lambda: "present", lambda: "present" if sns_permission_present() else "MISSING"),
    ("eb_target_broken", f"EventBridge rule {config.BUS_RULE}", "target",
     lambda: "processor", lambda: "MISSING ALIAS" if bus_target_arn().endswith(MISSING_ALIAS) else "processor"),
    ("ssm_config_poisoned", f"SSM {config.FLAGS_PARAM}", "value", lambda: "valid JSON", _flags_state),
]


def drift():
    from store import err_code
    rows = []
    for fault, resource, setting, baseline, reader in DRIFT_CHECKS:
        base = baseline()
        try:
            cur = str(reader())
            status = "ok" if cur == base else "drift"
        except Exception as e:  # noqa: BLE001 — one unreadable resource must not hide the rest
            cur, status = f"error: {err_code(e)}", "error"
        rows.append({"fault": fault, "resource": resource, "setting": setting,
                     "baseline": base, "current": cur, "status": status})
    return rows
