"""On-demand Classic tier: a second CloudFormation stack (VPC, ALB, EC2 Auto
Scaling group, RDS PostgreSQL) that the Chaos page launches and tears down.

Launch: the api Lambda calls cloudformation:CreateStack with the template
shipped in the code package (classic_tier/classic.yaml) and a dedicated
CloudFormation service role, so the Lambda itself never holds EC2/RDS create
rights. It also creates a one-time EventBridge Scheduler job that deletes the
stack at its expiry time — the tier shuts down even if the app is broken by a
chaos fault or nobody has the page open. The janitor Lambda is a second
backstop.

State lives in CONFIG / CLASSIC: size, expiry, who launched it and the shared
secret the ALB requires in the X-AvionPulse-Key header.
"""
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import config
import telemetry
from store import err_code, get_store

log = logging.getLogger("avionpulse.classic")

HOURLY_USD = {"minimal": 0.074, "full": 0.107}
SIZES = {"minimal": {"instances": 1, "multi_az": False}, "full": {"instances": 2, "multi_az": True}}
ONLINE = ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE")
EXPECTED_RESOURCES = 30  # rough count, for the progress bar
SCHEDULE_NAME = f"{config.STACK}-classic-expiry"[:64]

_clients = {}
_lock = threading.Lock()


def _client(name):
    if name not in _clients:
        with _lock:
            if name not in _clients:
                import boto3
                from botocore.config import Config

                _clients[name] = boto3.client(name, config=Config(retries={"mode": "standard", "max_attempts": 3},
                                                                  connect_timeout=3, read_timeout=8))
    return _clients[name]


class ClassicError(Exception):
    def __init__(self, status, detail, code=None):
        super().__init__(detail)
        self.status, self.detail, self.code = status, detail, code


# ── Record (DynamoDB CONFIG / CLASSIC) ────────────────────────────────────
def _record():
    return get_store().get("CONFIG", "CLASSIC", consistent=True) or {}


def _save(rec):
    get_store().put(dict(rec, PK="CONFIG", SK="CLASSIC", updated_at=int(time.time())))


# ── Local simulation of the stack lifecycle ───────────────────────────────
LOCAL = {"phase": None, "t0": 0.0, "params": {}}
LOCAL_SIM = {"sg": True, "nacl_deny": False, "asg": 0, "alb_forward": True, "rds_down_until": 0.0, "fis": {}}
LOCAL_CREATE_S, LOCAL_DELETE_S = 45, 15
_LOCAL_STEPS = ["VPC", "InternetGateway", "AppSubnetA", "AppSubnetB", "DbSubnetA", "DbSubnetB", "DbNacl",
                "AlbSecurityGroup", "InstanceSecurityGroup", "DbSecurityGroup", "DbSubnetGroup", "Database",
                "LoadBalancer", "TargetGroup", "Listener", "ForwardRule", "LaunchTemplate", "GateAsg"]


def _local_stack():
    if not LOCAL["phase"]:
        return None
    el = time.time() - LOCAL["t0"]
    if LOCAL["phase"] == "create":
        done = int(len(_LOCAL_STEPS) * min(el / LOCAL_CREATE_S, 1))
        status = "CREATE_COMPLETE" if el >= LOCAL_CREATE_S else "CREATE_IN_PROGRESS"
        if status == "CREATE_COMPLETE" and not LOCAL_SIM["asg"]:
            LOCAL_SIM["asg"] = SIZES[LOCAL["params"]["Size"]]["instances"]
        events = [{"resource": r, "status": "CREATE_COMPLETE", "reason": ""} for r in _LOCAL_STEPS[:done]]
        if done < len(_LOCAL_STEPS):
            events.append({"resource": _LOCAL_STEPS[done], "status": "CREATE_IN_PROGRESS",
                           "reason": "Resource creation Initiated"})
    else:
        if el >= LOCAL_DELETE_S:
            LOCAL.update(phase=None)
            LOCAL_SIM.update(sg=True, nacl_deny=False, asg=0, alb_forward=True, rds_down_until=0.0, fis={})
            return None
        status = "DELETE_IN_PROGRESS"
        done = int(len(_LOCAL_STEPS) * el / LOCAL_DELETE_S)
        events = [{"resource": r, "status": "DELETE_COMPLETE", "reason": ""} for r in reversed(_LOCAL_STEPS[-done:])] if done else []
    size = LOCAL["params"].get("Size", "minimal")
    return {"StackStatus": status, "resources_done": done,
            "Outputs": {"AlbDns": "local-classic-alb", "Size": size,
                        "InstanceCount": str(SIZES[size]["instances"])},
            "events": list(reversed(events))[:8]}


# ── Stack reads ───────────────────────────────────────────────────────────
_desc_cache = {"at": 0.0, "val": None}


def describe(force=False):
    """The Classic stack as {StackStatus, Outputs{}, events[], resources_done}, or None."""
    if config.LOCAL:
        return _local_stack()
    if not force and time.time() - _desc_cache["at"] < 4:
        return _desc_cache["val"]
    cfn = _client("cloudformation")
    try:
        st = cfn.describe_stacks(StackName=config.CLASSIC_STACK)["Stacks"][0]
    except Exception as e:  # noqa: BLE001
        if "does not exist" in str(e):
            _desc_cache.update(at=time.time(), val=None)
            return None
        raise
    if st["StackStatus"] == "DELETE_COMPLETE":
        val = None
    else:
        evs = cfn.describe_stack_events(StackName=config.CLASSIC_STACK)["StackEvents"]
        done = len({e["LogicalResourceId"] for e in evs
                    if e["ResourceStatus"] == "CREATE_COMPLETE" and e["LogicalResourceId"] != config.CLASSIC_STACK})
        val = {"StackStatus": st["StackStatus"], "resources_done": done,
               "Outputs": {o["OutputKey"]: o["OutputValue"] for o in st.get("Outputs", [])},
               "events": [{"resource": e["LogicalResourceId"], "status": e["ResourceStatus"],
                           "reason": e.get("ResourceStatusReason", ""),
                           "time": e["Timestamp"].isoformat()} for e in evs[:8]]}
    _desc_cache.update(at=time.time(), val=val)
    return val


def is_online():
    try:
        st = describe()
    except Exception:  # noqa: BLE001
        return False
    return bool(st and st["StackStatus"] in ONLINE)


def _outputs():
    st = describe()
    if not st or st["StackStatus"] not in ONLINE:
        raise ClassicError(409, "Classic tier is offline — launch it from the Chaos page", "ClassicOffline")
    return st["Outputs"]


def status():
    rec = _record()
    st = describe()
    now = time.time()
    if st is None:
        phase = "offline"
    else:
        s = st["StackStatus"]
        phase = ("online" if s in ONLINE else "launching" if s == "CREATE_IN_PROGRESS"
                 else "deleting" if s == "DELETE_IN_PROGRESS" else "failed")
    size = rec.get("size") or (st or {}).get("Outputs", {}).get("Size") or "minimal"
    launched = rec.get("launched_at")
    running_h = ((rec.get("ended_at") or now) - launched) / 3600 if launched else 0
    return {
        "phase": phase,
        "stack": config.CLASSIC_STACK,
        "stack_status": st["StackStatus"] if st else None,
        "size": size,
        "launched_at": launched,
        "launched_by": rec.get("launched_by"),
        "expires_at": rec.get("expires_at") if phase in ("launching", "online") else None,
        "max_expires_at": (launched + config.CLASSIC_MAX_HOURS * 3600)
        if launched and phase in ("launching", "online") else None,
        "hourly_usd": HOURLY_USD,
        "cost_so_far_usd": round(running_h * HOURLY_USD.get(size, 0.074), 2) if launched else 0,
        "progress_pct": min(99, int(100 * (st or {}).get("resources_done", 0)
                                / (len(_LOCAL_STEPS) if config.LOCAL else EXPECTED_RESOURCES)))
        if phase == "launching" else (100 if phase == "online" else 0),
        "events": (st or {}).get("events", []),
        "alb_dns": (st or {}).get("Outputs", {}).get("AlbDns") if phase == "online" else None,
        "last_session": rec.get("last_session"),
        "configured": config.LOCAL or bool(config.CLASSIC_CFN_ROLE_ARN),
    }


# ── Launch / extend / teardown ────────────────────────────────────────────
def _template_body():
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "classic_tier", "classic.yaml"),
                 os.path.join(here, "..", "cloudformation", "classic.yaml")):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return fh.read()
    raise ClassicError(500, "classic.yaml is missing from the code package", "TemplateMissing")


def _at(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _schedule(expires_at, create):
    if config.LOCAL:
        log.warning("[LOCAL] scheduler.%s_schedule %s at(%s)", "create" if create else "update",
                    SCHEDULE_NAME, _at(expires_at))
        return
    sch = _client("scheduler")
    kwargs = dict(
        Name=SCHEDULE_NAME,
        ScheduleExpression=f"at({_at(expires_at)})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Description="AvionPulse Classic tier auto-shutdown",
        Target={"Arn": "arn:aws:scheduler:::aws-sdk:cloudformation:deleteStack",
                "RoleArn": config.SCHEDULER_ROLE_ARN,
                "Input": json.dumps({"StackName": config.CLASSIC_STACK, "RoleARN": config.CLASSIC_CFN_ROLE_ARN})},
    )
    if create:
        try:
            sch.create_schedule(**kwargs)
            return
        except sch.exceptions.ConflictException:
            pass  # a leftover from a previous session — overwrite it
    sch.update_schedule(**kwargs)


def _delete_schedule():
    if config.LOCAL:
        return
    sch = _client("scheduler")
    try:
        sch.delete_schedule(Name=SCHEDULE_NAME)
    except sch.exceptions.ResourceNotFoundException:
        pass


def launch(size, hours, user):
    if size not in SIZES:
        raise ClassicError(422, "size must be minimal or full")
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        raise ClassicError(422, "hours must be a whole number")
    if not 1 <= hours <= config.CLASSIC_MAX_HOURS:
        raise ClassicError(422, f"hours must be between 1 and {config.CLASSIC_MAX_HOURS}")
    if not config.LOCAL and not config.CLASSIC_CFN_ROLE_ARN:
        raise ClassicError(409, "Classic tier support is not deployed with this stack", "ClassicNotConfigured")
    if describe(force=True) is not None:
        raise ClassicError(409, "A Classic stack already exists — tear it down first", "ClassicExists")

    now = time.time()
    expires = now + hours * 3600
    secret = secrets.token_hex(16)
    params = {
        "CoreStack": config.STACK, "Size": size, "SharedSecret": secret,
        "CodeBucket": config.CODE_BUCKET, "CodeKey": config.CODE_KEY,
        "InstanceProfileArn": config.CLASSIC_INSTANCE_PROFILE_ARN, "TableName": config.TABLE_NAME,
        "AppEnv": config.APP_ENV, "AppVersion": config.APP_VERSION,
        "EnableDatadog": "true" if config.DATADOG_ENABLED else "false",
        "DatadogSite": config.DD_SITE, "DatadogApiKeySsmName": config.DD_API_KEY_SSM_NAME or "/none",
    }
    if config.LOCAL:
        LOCAL.update(phase="create", t0=now, params=params)
        LOCAL_SIM.update(sg=True, nacl_deny=False, asg=0, alb_forward=True, rds_down_until=0.0, fis={})
    else:
        _client("cloudformation").create_stack(
            StackName=config.CLASSIC_STACK,
            TemplateBody=_template_body(),
            Parameters=[{"ParameterKey": k, "ParameterValue": str(v)} for k, v in params.items()],
            RoleARN=config.CLASSIC_CFN_ROLE_ARN,
            OnFailure="DELETE",
            TimeoutInMinutes=40,
            Tags=[{"Key": "app", "Value": "avionpulse"}, {"Key": "tier", "Value": "classic"},
                  {"Key": "avionpulse:classic", "Value": config.STACK},
                  {"Key": "avionpulse:expires", "Value": _at(expires)}],
        )
        _desc_cache["at"] = 0
    _schedule(expires, create=True)
    _save({"size": size, "launched_at": now, "launched_by": user, "expires_at": expires,
           "shared_secret": secret, "last_session": _record().get("last_session")})
    telemetry.metric("avionpulse.classic.launch", 1, [f"size:{size}"])
    telemetry.event(f"AvionPulse Classic tier launching ({size})",
                    f"{user} launched {config.CLASSIC_STACK} for {hours} h (≈ ${HOURLY_USD[size] * hours:.2f})",
                    ["tier:classic", f"size:{size}"], "info")
    log.warning("[CLASSIC] launch %s size=%s hours=%s by %s", config.CLASSIC_STACK, size, hours, user)
    return status()


def extend(hours, user):
    rec = _record()
    st = describe(force=True)
    if not st or st["StackStatus"] in ("DELETE_IN_PROGRESS",):
        raise ClassicError(409, "Classic tier is not running", "ClassicOffline")
    cap = rec["launched_at"] + config.CLASSIC_MAX_HOURS * 3600
    new = min(cap, max(rec["expires_at"], time.time()) + int(hours) * 3600)
    if new <= rec["expires_at"]:
        raise ClassicError(409, f"Already at the {config.CLASSIC_MAX_HOURS} h maximum", "ClassicMaxLifetime")
    _schedule(new, create=False)
    rec["expires_at"] = new
    _save(rec)
    log.warning("[CLASSIC] extended to %s by %s", _at(new), user)
    return status()


def teardown(user, reason="manual"):
    import chaos

    st = describe(force=True)
    if st is None:
        raise ClassicError(409, "No Classic stack to tear down", "ClassicOffline")
    if st["StackStatus"] == "DELETE_IN_PROGRESS":
        return status()
    for name in chaos.CATALOG:
        if name.startswith("fis_") and chaos.active(name):
            try:
                apply_fault(name, False, (chaos._load().get("faults") or {}).get(name) or {})
            except Exception as e:  # noqa: BLE001
                log.warning("could not stop %s: %s", name, e)
    if config.LOCAL:
        LOCAL.update(phase="delete", t0=time.time())
    else:
        _client("cloudformation").delete_stack(StackName=config.CLASSIC_STACK, RoleARN=config.CLASSIC_CFN_ROLE_ARN)
        _desc_cache["at"] = 0
    _delete_schedule()
    chaos.drop_classic_faults(user)
    rec = _record()
    if rec.get("launched_at"):
        hrs = (time.time() - rec["launched_at"]) / 3600
        rec["last_session"] = {"size": rec.get("size"), "hours": round(hrs, 2),
                               "cost_usd": round(hrs * HOURLY_USD.get(rec.get("size"), 0.074), 2),
                               "ended_at": int(time.time()), "reason": reason}
    rec["ended_at"] = time.time()
    _save(rec)
    telemetry.event("AvionPulse Classic tier teardown", f"{config.CLASSIC_STACK} deleted ({reason}) by {user}",
                    ["tier:classic", f"reason:{reason}"], "success")
    log.warning("[CLASSIC] teardown %s (%s) by %s", config.CLASSIC_STACK, reason, user)
    return status()


def janitor():
    """Auto-shutdown backstop (the Scheduler job is the primary) and retry of a failed delete."""
    rec = _record()
    try:
        st = describe(force=True)
    except Exception as e:  # noqa: BLE001
        log.error("classic janitor: describe failed: %s", err_code(e))
        return None
    if st is None:
        if rec.get("launched_at") and not rec.get("ended_at"):
            import chaos
            chaos.drop_classic_faults("janitor")
            rec["ended_at"] = time.time()
            _save(rec)
        return None
    s = st["StackStatus"]
    if s == "DELETE_FAILED":
        log.warning("classic janitor: retrying failed delete")
        return teardown("janitor", "retry-delete")
    if s.endswith("_IN_PROGRESS"):
        return None
    if rec.get("expires_at") and rec["expires_at"] <= time.time():
        return teardown("janitor", "expired")
    return None


# ── Classic faults ────────────────────────────────────────────────────────
def fault_blocker(name):
    import chaos

    if not is_online():
        return "Classic tier is offline — launch it first"
    if chaos.CATALOG[name].get("full_only") and status()["size"] != "full":
        return "This experiment needs the Full size (Multi-AZ) Classic tier"
    return None


def _ignore(fn, *codes):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        if err_code(e) not in codes:
            raise


def apply_fault(name, enabled, entry):
    """AWS change behind a Classic fault. Returns extra fields for the fault record."""
    if name == "classic_db_slow_query":
        return None                    # request-path fault: the EC2 service reads the switch itself
    if config.LOCAL:
        return _apply_local(name, enabled, entry)
    out = _outputs() if (enabled or name not in ("fis_cpu_stress", "fis_network_latency",
                                                  "fis_instance_terminate", "fis_az_outage")) else {}
    if name == "classic_sg_lockdown":
        ec2 = _client("ec2")
        perm = [{"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080,
                 "UserIdGroupPairs": [{"GroupId": out["AlbSecurityGroupId"]}]}]
        if enabled:
            _ignore(lambda: ec2.revoke_security_group_ingress(GroupId=out["InstanceSecurityGroupId"],
                                                              IpPermissions=perm), "InvalidPermission.NotFound")
        else:
            _ignore(lambda: ec2.authorize_security_group_ingress(GroupId=out["InstanceSecurityGroupId"],
                                                                 IpPermissions=perm), "InvalidPermission.Duplicate")
    elif name == "classic_nacl_db_block":
        ec2 = _client("ec2")
        if enabled:
            _ignore(lambda: ec2.create_network_acl_entry(
                NetworkAclId=out["DbNetworkAclId"], RuleNumber=10, Protocol="6", RuleAction="deny", Egress=False,
                CidrBlock="0.0.0.0/0", PortRange={"From": 5432, "To": 5432}), "NetworkAclEntryAlreadyExists")
        else:
            _ignore(lambda: ec2.delete_network_acl_entry(NetworkAclId=out["DbNetworkAclId"], RuleNumber=10,
                                                         Egress=False), "InvalidNetworkAclEntry.NotFound")
    elif name == "classic_asg_zero":
        n = 0 if enabled else int(out["InstanceCount"])
        _client("autoscaling").update_auto_scaling_group(AutoScalingGroupName=out["AsgName"],
                                                         MinSize=n, DesiredCapacity=n)
    elif name == "classic_alb_maintenance":
        if enabled:
            actions = [{"Type": "fixed-response", "FixedResponseConfig": {
                "StatusCode": "503", "ContentType": "application/json",
                "MessageBody": '{"detail":"Gate service under maintenance"}'}}]
        else:
            actions = [{"Type": "forward", "TargetGroupArn": out["TargetGroupArn"]}]
        _client("elbv2").modify_rule(RuleArn=out["ForwardRuleArn"], Actions=actions)
    elif name == "classic_rds_reboot":
        if enabled:
            _client("rds").reboot_db_instance(DBInstanceIdentifier=out["DbInstanceId"],
                                              ForceFailover=out.get("Size") == "full")
    elif name in config.FIS_TEMPLATES:
        fis = _client("fis")
        if enabled:
            exp = fis.start_experiment(experimentTemplateId=config.FIS_TEMPLATES[name], clientToken=str(uuid.uuid4()),
                                       tags={"app": "avionpulse", "fault": name})["experiment"]
            return {"experiment_id": exp["id"]}
        if entry.get("experiment_id"):
            # Already finished experiments refuse to stop — that's fine.
            _ignore(lambda: fis.stop_experiment(id=entry["experiment_id"]), "ValidationException",
                    "ResourceNotFoundException", "ConflictException")
    return None


def _apply_local(name, enabled, entry):
    n = SIZES[LOCAL["params"].get("Size", "minimal")]["instances"]
    if name == "classic_sg_lockdown":
        LOCAL_SIM["sg"] = not enabled
    elif name == "classic_nacl_db_block":
        LOCAL_SIM["nacl_deny"] = enabled
    elif name == "classic_asg_zero":
        LOCAL_SIM["asg"] = 0 if enabled else n
    elif name == "classic_alb_maintenance":
        LOCAL_SIM["alb_forward"] = not enabled
    elif name == "classic_rds_reboot" and enabled:
        LOCAL_SIM["rds_down_until"] = time.time() + 60
    elif name in config.FIS_TEMPLATES:
        if enabled:
            LOCAL_SIM["fis"][name] = time.time() + 300
            return {"experiment_id": "EXP" + uuid.uuid4().hex[:12]}
        LOCAL_SIM["fis"].pop(name, None)
    log.warning("[LOCAL] classic fault %s %s", name, "ON" if enabled else "OFF")
    return None


# ── Drift rows for the Classic stack ──────────────────────────────────────
def drift_rows():
    if not is_online():
        return []
    rows = []
    if config.LOCAL:
        out = {"InstanceCount": str(SIZES[LOCAL["params"].get("Size", "minimal")]["instances"])}
        checks = [
            ("classic_sg_lockdown", "Instance security group", "ALB → :8080 ingress", "present",
             lambda: "present" if LOCAL_SIM["sg"] else "REVOKED"),
            ("classic_nacl_db_block", "DB subnets network ACL", "rule 10 (deny 5432)", "absent",
             lambda: "DENY 5432" if LOCAL_SIM["nacl_deny"] else "absent"),
            ("classic_asg_zero", "Auto Scaling group", "desired capacity", out["InstanceCount"],
             lambda: str(LOCAL_SIM["asg"])),
            ("classic_alb_maintenance", "ALB listener rule", "action", "forward",
             lambda: "forward" if LOCAL_SIM["alb_forward"] else "FIXED 503"),
        ]
    else:
        out = _outputs()
        ec2 = _client("ec2")

        def sg():
            g = ec2.describe_security_groups(GroupIds=[out["InstanceSecurityGroupId"]])["SecurityGroups"][0]
            ok = any(p.get("FromPort") == 8080 and any(u["GroupId"] == out["AlbSecurityGroupId"]
                                                       for u in p.get("UserIdGroupPairs", []))
                     for p in g["IpPermissions"])
            return "present" if ok else "REVOKED"

        def nacl():
            a = ec2.describe_network_acls(NetworkAclIds=[out["DbNetworkAclId"]])["NetworkAcls"][0]
            return "DENY 5432" if any(e["RuleNumber"] == 10 and not e["Egress"] for e in a["Entries"]) else "absent"

        def asg():
            g = _client("autoscaling").describe_auto_scaling_groups(AutoScalingGroupNames=[out["AsgName"]])
            return str(g["AutoScalingGroups"][0]["DesiredCapacity"])

        def rule():
            r = _client("elbv2").describe_rules(RuleArns=[out["ForwardRuleArn"]])["Rules"][0]
            return "forward" if r["Actions"][0]["Type"] == "forward" else "FIXED 503"

        checks = [
            ("classic_sg_lockdown", "Instance security group", "ALB → :8080 ingress", "present", sg),
            ("classic_nacl_db_block", "DB subnets network ACL", "rule 10 (deny 5432)", "absent", nacl),
            ("classic_asg_zero", "Auto Scaling group", "desired capacity", out["InstanceCount"], asg),
            ("classic_alb_maintenance", "ALB listener rule", "action", "forward", rule),
        ]
    for fault, resource, setting, base, reader in checks:
        try:
            cur = reader()
            st = "ok" if cur == base else "drift"
        except Exception as e:  # noqa: BLE001
            cur, st = f"error: {err_code(e)}", "error"
        rows.append({"fault": fault, "resource": resource, "setting": setting,
                     "baseline": base, "current": cur, "status": st})
    return rows


# ── Gate Assignment service (EC2 behind the ALB) ──────────────────────────
GATES = [f"{t}{n}" for t in "ABC" for n in range(1, 9)]


def gates():
    out = _outputs()
    if config.LOCAL:
        return _local_gates()
    headers = {"X-AvionPulse-Key": _record().get("shared_secret", "")}
    try:  # pass the trace context so the EC2 service's spans join this Lambda trace
        from ddtrace import tracer
        from ddtrace.propagation.http import HTTPPropagator

        span = tracer.current_span()
        if span:
            HTTPPropagator.inject(span.context, headers)
    except ImportError:
        pass
    req = urllib.request.Request(f"http://{out['AlbDns']}/gates", headers=headers)
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:  # noqa: BLE001
            detail = None
        raise ClassicError(502 if e.code >= 500 else e.code,
                           f"ALB answered {e.code}: {detail or e.reason}", f"ALB{e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise ClassicError(504, f"Gate service unreachable: {getattr(e, 'reason', e)}", "GateServiceTimeout")
    body["proxy_ms"] = round((time.perf_counter() - t) * 1000, 1)
    return body


def _local_gates():
    import chaos

    now = time.time()
    if not LOCAL_SIM["alb_forward"]:
        raise ClassicError(502, "ALB answered 503: Gate service under maintenance", "ALB503")
    if LOCAL_SIM["asg"] == 0 or not LOCAL_SIM["sg"]:
        code = 503 if LOCAL_SIM["asg"] == 0 else 504
        raise ClassicError(502, f"ALB answered {code}: no healthy targets", f"ALB{code}")
    if LOCAL_SIM["nacl_deny"] or LOCAL_SIM["rds_down_until"] > now:
        time.sleep(3)
        raise ClassicError(502, "ALB answered 500: database connection timed out (10.42.20.15:5432)", "ALB500")
    if chaos.active("classic_db_slow_query"):
        time.sleep(2.5)
    if "fis_network_latency" in LOCAL_SIM["fis"]:
        time.sleep(0.9)
    window = int(now // 120)
    rows = []
    for i, g in enumerate(GATES):
        r = __import__("random").Random(f"{g}{window}")
        occupied = r.random() < 0.55
        rows.append({"gate": g, "terminal": g[0], "status": "occupied" if occupied else "open",
                     "flight": f"AP{r.randint(100, 999)}" if occupied else None,
                     "updated_at": datetime.fromtimestamp(now - r.randint(0, 900), timezone.utc).isoformat()})
    return {"gates": rows, "instance_id": "i-local0001", "az": "local-1a", "db_ms": 4.2,
            "served_by": "avionpulse-gates (simulated)", "proxy_ms": 6.0}
