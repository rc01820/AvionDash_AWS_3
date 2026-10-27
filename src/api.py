"""AvionPulse API — app tier. One Lambda behind an API Gateway HTTP API
(payload v2), reached through CloudFront at /api/*.

Handler: api.handler (wrapped by datadog_lambda.handler.handler when the
Datadog layer is enabled, see template.yaml).
"""
import base64
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone

import auth
import chaos
import classic
import config
import infra
import telemetry
from store import err_code, get_store

telemetry.setup_logging()
log = logging.getLogger("avionpulse.api")

# cold_start_storm sets this env var; the sleep lands in the Lambda init phase.
_slow_init = int(os.environ.get("CHAOS_SLOW_INIT_MS", "0") or 0)
if _slow_init and not config.LOCAL:
    log.warning("[CHAOS] cold_start_storm: sleeping %d ms during init", _slow_init)
    time.sleep(_slow_init / 1000)

_CONTAINER_STARTED = time.time()
_COLD = True
_INVOCATIONS = 0
_LEAK = []  # memory_leak fault keeps references here

MB = 1024 * 1024


class HttpError(Exception):
    def __init__(self, status, detail, code=None):
        super().__init__(detail)
        self.status, self.detail, self.code = status, detail, code


# ── Request plumbing ──────────────────────────────────────────────────────
class Request:
    def __init__(self, event, context):
        ctx = event.get("requestContext", {})
        self.method = ctx.get("http", {}).get("method", "GET").upper()
        self.path = event.get("rawPath", "/")
        self.query = event.get("queryStringParameters") or {}
        self.headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        self.api_id = ctx.get("apiId")
        self.request_id = ctx.get("requestId") or getattr(context, "aws_request_id", "local")
        self.context = context
        body = event.get("body") or ""
        if body and event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8", "replace")
        self.raw_body = body
        self.claims = None

    def json(self):
        ctype = self.headers.get("content-type", "")
        if not self.raw_body:
            return {}
        if "application/x-www-form-urlencoded" in ctype:
            return {k: v[0] for k, v in urllib.parse.parse_qs(self.raw_body).items()}
        try:
            data = json.loads(self.raw_body)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            raise HttpError(400, "Body must be JSON")

    def token(self):
        tok = self.headers.get("x-avion-token")
        if not tok:
            authz = self.headers.get("authorization", "")
            if authz.lower().startswith("bearer "):
                tok = authz[7:]
        return tok


def respond(status, body, extra_headers=None):
    headers = {
        "content-type": "application/json",
        "cache-control": "no-store",
        "access-control-allow-origin": "*",
        "access-control-allow-headers": "content-type,x-avion-token,authorization",
        "access-control-allow-methods": "GET,POST,OPTIONS",
    }
    headers.update(extra_headers or {})
    return {"statusCode": status, "headers": headers, "body": json.dumps(body, default=str)}


ROUTES = []


def route(method, pattern, role=None, data=False, exempt=False):
    """role: None=public, else minimum role. data: request touches the data
    tier (DynamoDB storm / poison-pill faults apply). exempt: chaos request
    faults skip it, so the chaos panel and login always work."""
    def deco(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn, role, data, exempt))
        return fn
    return deco


# ── Fault injection on the request path ───────────────────────────────────
def _inject_request_faults(req, data_route):
    if chaos.active("log_flood"):
        for i in range(50):
            log.warning("[FAULT][LOG_FLOOD] %d %s %s request_id=%s", i, req.method, req.path, req.request_id)

    if chaos.active("latency_spike"):
        d = random.uniform(2.0, 6.0)
        log.warning("[FAULT] latency_spike sleeping %.1fs on %s", d, req.path)
        time.sleep(d)

    if chaos.active("cpu_burn"):
        end = time.perf_counter() + 0.4
        x = 0.0
        while time.perf_counter() < end:
            x += random.random() ** 0.5

    if chaos.active("memory_leak"):
        if not (config.LOCAL and len(_LEAK) >= 8):
            _LEAK.append(b"\x01" * (8 * MB))
        log.warning("[FAULT] memory_leak holding %d MB in this container", len(_LEAK) * 8)

    if chaos.active("lambda_oom") and random.random() < 0.25:
        log.error("[FAULT] lambda_oom allocating until the runtime is killed")
        if config.LOCAL:
            raise HttpError(502, "Simulated Runtime.OutOfMemory (local mode)", "Runtime.OutOfMemory")
        hog = []
        for _ in range(128):
            hog.append(b"\x01" * (32 * MB))

    if chaos.active("lambda_timeout") and random.random() < 0.30:
        if config.LOCAL:
            time.sleep(2)
            raise HttpError(503, "Simulated 'Task timed out after 10.00 seconds' (local mode)", "Lambda.Timeout")
        remaining = req.context.get_remaining_time_in_millis() / 1000
        log.error("[FAULT] lambda_timeout sleeping %.1fs past the deadline", remaining + 1)
        time.sleep(remaining + 1)

    if chaos.active("random_500s") and random.random() < 0.35:
        raise chaos.UnhandledFault(f"FlightBoardRenderError: injected unhandled exception on {req.path}")

    if data_route:
        if chaos.active("dynamodb_throttle"):
            store = get_store()
            if config.LOCAL:
                if random.random() < 0.5:
                    from store import SimulatedAwsError
                    raise SimulatedAwsError("ProvisionedThroughputExceededException",
                                            "The level of configured provisioned throughput for the table was exceeded.")
            else:
                for _ in range(25):
                    store.scan_all(consistent=True)

        if chaos.active("sqs_poison_pill") and random.random() < 0.5:
            infra.send_event({"type": "poison", "payload": "\x00\x7f{not-json", "source": "api"})


# ── Data helpers ──────────────────────────────────────────────────────────
def _load_flights():
    store = get_store()
    if chaos.active("silent_failure"):
        # The "bad deploy": wrong partition key. Valid query, zero rows, no error.
        flights = store.query_pk("FLIGHTS")
    elif chaos.active("full_table_scan"):
        flights = [i for i in store.scan_all() if i.get("PK") == "FLIGHT"]
    else:
        flights = store.query_pk("FLIGHT")

    if chaos.active("n_plus_one"):
        for f in flights:
            ac = store.get("AIRCRAFT", f.get("tail_number", ""))
            ap = store.get("AIRPORT", f.get("origin_iata", ""))
            f["aircraft_status"] = (ac or {}).get("status")
            f["origin_city"] = (ap or {}).get("city")

    if chaos.active("pax_corruption"):
        for f in flights:
            f["pax"] = int(f.get("pax", 0)) * 847
    return flights


def _clean(items):
    return [{k: v for k, v in i.items() if k not in ("PK", "SK", "password_hash")} for i in items]


# ── Routes: health ────────────────────────────────────────────────────────
@route("GET", "/api/health")
def r_health(req):
    if chaos.active("health_check_fail"):
        raise HttpError(503, "Health check failing (fault injected)", "HealthCheckFault")
    return 200, {"status": "ok", "service": "avionpulse-api", "version": config.APP_VERSION,
                 "region": config.REGION, "env": config.APP_ENV}


@route("GET", "/api/health/deep")
def r_health_deep(req):
    checks, ok = {}, True
    for name, fn in (("dynamodb", lambda: get_store().get("CONFIG", "FAULTS")),
                     ("sqs", infra.queue_depths),
                     ("s3", lambda: infra.get_s3_json("data/notams.json"))):
        t = time.perf_counter()
        try:
            fn()
            checks[name] = {"status": "ok", "ms": round((time.perf_counter() - t) * 1000, 1)}
        except Exception as e:
            ok = False
            checks[name] = {"status": "error", "error": err_code(e)}
    if chaos.active("health_check_fail"):
        ok = False
    return (200 if ok else 503), {"status": "ok" if ok else "degraded", "checks": checks}


# ── Routes: auth ──────────────────────────────────────────────────────────
@route("POST", "/api/auth/token", exempt=True)
def r_login(req):
    body = req.json()
    username = str(body.get("username", "")).strip().lower()
    password = str(body.get("password", ""))
    user = get_store().get("USER", username) if username else None
    if not user or not auth.verify_password(password, user.get("password_hash", "")):
        log.warning("Failed login for %r", username)
        telemetry.metric("avionpulse.auth.login", 1, ["result:failed"])
        raise HttpError(401, "Incorrect username or password")
    get_store().update_attrs("USER", username, {"last_login": datetime.now(timezone.utc).isoformat()})
    telemetry.metric("avionpulse.auth.login", 1, ["result:ok", f"role:{user['role']}"])
    log.info("Login OK: %s", username)
    return 200, {"access_token": auth.create_token(username, user["role"]), "token_type": "bearer",
                 "username": username, "role": user["role"], "full_name": user.get("full_name")}


@route("GET", "/api/auth/me", role="viewer", exempt=True)
def r_me(req):
    user = get_store().get("USER", req.claims["sub"]) or {}
    return 200, {"username": req.claims["sub"], "role": req.claims["role"],
                 "full_name": user.get("full_name"), "last_login": user.get("last_login")}


# ── Routes: dashboard ─────────────────────────────────────────────────────
@route("GET", "/api/dashboard/public-stats", data=True)
def r_public_stats(req):
    store = get_store()
    flights = _load_flights()
    return 200, {
        "flights": {"total": len(flights),
                    "airborne": sum(1 for f in flights if f.get("status") in ("en_route", "departed")),
                    "delayed": sum(1 for f in flights if f.get("delay_minutes", 0) > 0)},
        "aircraft": {"total": len(store.query_pk("AIRCRAFT"))},
        "airports": {"total": len(store.query_pk("AIRPORT"))},
    }


@route("GET", "/api/dashboard/summary", role="viewer", data=True)
def r_summary(req):
    store = get_store()
    t = time.perf_counter()
    if chaos.active("iam_access_denied"):
        store.probe_restricted_table()  # raises AccessDeniedException
    infra.get_flags()  # ssm_config_poisoned surfaces here as ConfigError
    flights = _load_flights()
    aircraft = store.query_pk("AIRCRAFT")
    airports = store.query_pk("AIRPORT")
    ddb_ms = (time.perf_counter() - t) * 1000

    total = len(flights)
    by_status = {}
    for f in flights:
        by_status[f.get("status")] = by_status.get(f.get("status"), 0) + 1
    delayed = sum(1 for f in flights if f.get("delay_minutes", 0) > 0)
    cancelled = by_status.get("cancelled", 0)
    airborne = by_status.get("en_route", 0) + by_status.get("departed", 0)
    pax_air = sum(int(f.get("pax", 0)) for f in flights if f.get("status") in ("en_route", "departed"))
    origins = {}
    for f in flights:
        origins[f["origin_iata"]] = origins.get(f["origin_iata"], 0) + 1
    busiest = sorted(origins.items(), key=lambda kv: -kv[1])[:5]
    newest = max((f.get("updated_epoch", 0) for f in flights), default=0)
    staleness = int(time.time() - newest) if newest else None
    if staleness is not None:
        telemetry.metric("avionpulse.data.staleness_s", staleness)

    act_ac = sum(1 for a in aircraft if a.get("status") == "active")
    return 200, {
        "flights": {"total": total, "airborne": airborne, "en_route": by_status.get("en_route", 0),
                    "delayed": delayed, "cancelled": cancelled, "by_status": by_status,
                    "pax_airborne": pax_air,
                    "on_time_pct": round((total - delayed - cancelled) / max(total, 1) * 100, 1)},
        "aircraft": {"total": len(aircraft), "active": act_ac,
                     "maintenance": sum(1 for a in aircraft if a.get("status") == "maintenance"),
                     "utilization_pct": round(act_ac / max(len(aircraft), 1) * 100, 1)},
        "airports": {"total": len(airports)},
        "busiest_origins": [{"iata": k, "departures": v} for k, v in busiest],
        "data_age_s": staleness,
        "system_health": {
            "dynamodb_ms": round(ddb_ms, 1),
            "cold_start": _COLD_THIS_REQUEST,
            "container_invocations": _INVOCATIONS,
            "container_age_s": int(time.time() - _CONTAINER_STARTED),
            "active_faults": chaos.active_count(),
        },
    }


# ── Routes: operations data ───────────────────────────────────────────────
@route("GET", "/api/flights", role="viewer", data=True)
def r_flights(req):
    flags = infra.get_flags()
    flights = _load_flights()
    status = req.query.get("status")
    if status:
        flights = [f for f in flights if f.get("status") == status]
    flights.sort(key=lambda f: f.get("departure_time", ""), reverse=True)
    try:
        limit = max(1, min(int(req.query.get("limit", flags["board_page_size"])), 200))
    except ValueError:
        raise HttpError(400, "limit must be an integer")
    telemetry.metric("avionpulse.api.flights_returned", len(flights))
    return 200, _clean(flights[:limit])


@route("GET", "/api/flights/(?P<number>[A-Za-z0-9]+)", role="viewer", data=True)
def r_flight(req, number):
    item = get_store().get("FLIGHT", number.upper())
    if not item:
        raise HttpError(404, "Flight not found")
    return 200, _clean([item])[0]


@route("GET", "/api/aircraft", role="viewer", data=True)
def r_aircraft(req):
    items = get_store().query_pk("AIRCRAFT")
    status = req.query.get("status")
    if status:
        items = [a for a in items if a.get("status") == status]
    return 200, _clean(items)


@route("GET", "/api/airports", role="viewer", data=True)
def r_airports(req):
    return 200, _clean(get_store().query_pk("AIRPORT"))


@route("GET", "/api/events", role="viewer", data=True)
def r_events(req):
    return 200, _clean(get_store().query_pk("EVENT", limit=60, descending=True))


@route("POST", "/api/events", role="operator")
def r_event_submit(req):
    body = req.json()
    tail = str(body.get("tail_number", "")).upper().strip()
    message = str(body.get("message", "")).strip()[:280]
    severity = body.get("severity", "warning")
    category = body.get("category", "MX")
    if not tail or not message:
        raise HttpError(422, "tail_number and message are required")
    if severity not in ("info", "warning", "critical"):
        raise HttpError(422, "severity must be info, warning or critical")
    if not get_store().get("AIRCRAFT", tail):
        raise HttpError(404, f"Unknown aircraft {tail}")
    infra.send_event({"type": "maintenance_request", "tail_number": tail, "message": message,
                      "severity": severity, "category": str(category)[:8].upper(),
                      "submitted_by": req.claims["sub"], "enqueued_at": time.time(), "source": "api"})
    telemetry.metric("avionpulse.events.submitted", 1, [f"severity:{severity}"])
    return 202, {"status": "queued", "queue": "avionpulse-events"}


@route("GET", "/api/notams", role="viewer")
def r_notams(req):
    key = "data/notams-v2.json" if chaos.active("s3_missing_object") else "data/notams.json"
    return 200, {"source": f"s3://{config.BUCKET}/{key}", "notams": infra.get_s3_json(key)}


@route("GET", "/api/weather", role="viewer")
def r_weather(req):
    if chaos.active("downstream_timeout"):
        log.warning("[FAULT] downstream_timeout calling unreachable METAR provider")
        infra.call_weather_provider()
    window = int(time.time() // 600)
    out = []
    for iata in ("JFK", "LHR", "DXB", "SIN", "LAX", "ORD", "FRA", "NRT"):
        r = random.Random(f"{iata}{window}")
        wind_dir, wind_kt = r.randrange(0, 360, 10), r.randint(3, 28)
        vis = r.choice([10, 10, 10, 8, 6, 3, 1])
        cat = "VFR" if vis >= 5 else ("MVFR" if vis >= 3 else "IFR")
        temp = r.randint(-5, 38)
        out.append({"iata": iata, "wind": f"{wind_dir:03d}@{wind_kt}kt", "visibility_sm": vis,
                    "temp_c": temp, "category": cat,
                    "metar": f"{iata} {wind_dir:03d}{wind_kt:02d}KT {vis}SM {temp:02d}/{temp - r.randint(2, 12):02d}"})
    return 200, {"provider": "avionpulse-synthetic-metar", "observations": out}


@route("GET", "/api/system", role="viewer")
def r_system(req):
    try:
        queues = infra.queue_depths()
    except Exception as e:
        queues = {"error": err_code(e)}
    ctx = req.context
    return 200, {
        "region": config.REGION,
        "function_name": getattr(ctx, "function_name", config.API_FN),
        "memory_mb": int(getattr(ctx, "memory_limit_in_mb", 256) or 256),
        "runtime": f"python{sys.version_info.major}.{sys.version_info.minor}",
        "version": config.APP_VERSION,
        "env": config.APP_ENV,
        "request_id": req.request_id,
        "cold_start": _COLD_THIS_REQUEST,
        "container_invocations": _INVOCATIONS,
        "container_age_s": int(time.time() - _CONTAINER_STARTED),
        "leak_mb": len(_LEAK) * 8,
        "slow_init_ms": _slow_init,
        "datadog_enabled": bool(os.environ.get("DD_LAMBDA_HANDLER")) and not config.LOCAL,
        "local_mode": config.LOCAL,
        "queues": queues,
        "active_faults": chaos.active_count(),
        "table": config.TABLE_NAME,
        "stack": config.STACK,
    }



# ── Routes: v3 integrations, AWS drift, Classic tier ──────────────────────
@route("GET", "/api/integrations", role="viewer")
def r_integrations(req):
    items = {i["SK"]: i for i in _clean_keep_sk(get_store().query_pk("INTEG"))}
    now = time.time()
    channels = []
    for key, label, aws, expect_s in (
            ("stream", "Flight changes", "DynamoDB Streams → λ processor", 400),
            ("bus", "FlightStatusChanged", "EventBridge custom bus → λ processor", 900),
            ("sns", "Crew alerts", "SNS topic → λ processor", 400),
            ("turnaround", "Aircraft turnaround", "Step Functions Express → λ tasks", 400),
            ("recorder", "Flight-data recorder", "S3 PutObject → EventBridge → λ processor", 400)):
        it = items.get(key, {})
        age = int(now - it["last_seen"]) if it.get("last_seen") else None
        channels.append({"key": key, "label": label, "aws": aws, "count": it.get("count", 0),
                         "last_seen": it.get("last_seen"), "age_s": age,
                         "healthy": age is not None and age < expect_s, "last_detail": it.get("last_detail")})
    fails = items.get("turnaround_failed")
    return 200, {"channels": channels, "turnaround_last_failure": fails.get("last_detail") if fails else None,
                 "local_counters": infra.LOCAL_COUNTERS if config.LOCAL else None}


def _clean_keep_sk(items):
    return [{k: v for k, v in i.items() if k != "PK"} for i in items]


@route("GET", "/api/aws/drift", role="viewer", exempt=True)
def r_drift(req):
    rows = infra.drift() + classic.drift_rows()
    return 200, {"rows": rows, "drifted": sum(1 for r in rows if r["status"] != "ok"),
                 "checked_at": int(time.time())}


@route("GET", "/api/classic/status", role="viewer", exempt=True)
def r_classic_status(req):
    return 200, classic.status()


@route("POST", "/api/classic/launch", role="admin", exempt=True)
def r_classic_launch(req):
    body = req.json()
    return 202, classic.launch(body.get("size", "minimal"), body.get("hours", 2), req.claims["sub"])


@route("POST", "/api/classic/extend", role="admin", exempt=True)
def r_classic_extend(req):
    return 200, classic.extend(req.json().get("hours", 1), req.claims["sub"])


@route("POST", "/api/classic/teardown", role="admin", exempt=True)
def r_classic_teardown(req):
    return 202, classic.teardown(req.claims["sub"])


@route("GET", "/api/gates", role="viewer")
def r_gates(req):
    return 200, classic.gates()

# ── Routes: chaos ─────────────────────────────────────────────────────────
@route("GET", "/api/chaos/status", role="viewer", exempt=True)
def r_chaos_status(req):
    st = chaos.status()
    return 200, {"faults": {k: v["enabled"] for k, v in st.items()}, "details": st,
                 "ttl_default_min": config.FAULT_TTL_DEFAULT_MIN}


@route("GET", "/api/chaos/catalog", role="viewer", exempt=True)
def r_chaos_catalog(req):
    return 200, {"faults": chaos.CATALOG, "tiers": chaos.TIERS}


@route("POST", "/api/chaos/(?P<name>[a-z0-9_]+)/toggle", role="admin", exempt=True)
def r_chaos_toggle(req, name):
    if name not in chaos.CATALOG:
        raise HttpError(404, f"Unknown fault: {name}")
    body = req.json()
    result = chaos.toggle(name, bool(body.get("enabled")), body.get("ttl_minutes"),
                          user=req.claims["sub"], api_id=req.api_id)
    telemetry.metric("avionpulse.chaos.toggle", 1, [f"fault:{name}", f"enabled:{bool(body.get('enabled'))}".lower()])
    return 200, result


@route("POST", "/api/chaos/reset-all", role="admin", exempt=True)
def r_chaos_reset(req):
    return 200, chaos.reset_all(user=req.claims["sub"], api_id=req.api_id)


# ── Dispatch ──────────────────────────────────────────────────────────────
_COLD_THIS_REQUEST = False


def _map_exception(e, req):
    code = err_code(e)
    if isinstance(e, infra.ConfigError):
        status, detail, code = 500, "Feature-flag configuration is invalid", "ConfigParseError"
    elif code.startswith("KMS.") or code in ("KMSDisabledException", "KMSInvalidStateException",
                                              "KMSAccessDeniedException"):
        status, detail = 503, "Queue encryption key is unavailable"
    elif code in ("ProvisionedThroughputExceededException", "ThrottlingException",
                "RequestLimitExceeded", "TooManyRequestsException"):
        status, detail = 503, "Data tier throttled — DynamoDB capacity exceeded"
    elif code in ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation"):
        status, detail = 500, "Lambda role is not authorized for this resource"
    elif code in ("NoSuchKey", "NoSuchBucket"):
        status, detail = 502, "Required S3 object is missing"
    elif isinstance(e, (TimeoutError, urllib.error.URLError)):
        status, detail, code = 504, "Upstream provider timed out", "UpstreamTimeout"
    else:
        status, detail = 500, "Internal error"
    log.error("Request failed %s %s -> %s %s: %s", req.method, req.path, status, code, e, exc_info=True)
    return status, {"detail": detail, "error_code": code, "request_id": req.request_id}


def _dispatch(req):
    if req.method == "OPTIONS":
        return 204, {}
    for method, rx, fn, role, data, exempt in ROUTES:
        m = rx.match(req.path.rstrip("/") or "/")
        if not m or method != req.method:
            continue
        try:
            if role:
                claims = auth.decode_token(req.token() or "")
                if not claims:
                    raise HttpError(401, "Invalid or expired token")
                if not auth.has_role(claims, role):
                    raise HttpError(403, f"{role} role required")
                req.claims = claims
            if not exempt and not req.path.startswith("/api/health"):
                _inject_request_faults(req, data)
            return fn(req, **m.groupdict())
        except (HttpError, classic.ClassicError) as e:
            if e.status >= 500:
                log.error("Request failed %s %s -> %s %s: %s", req.method, req.path, e.status, e.code, e.detail)
            return e.status, {"detail": e.detail, "error_code": e.code, "request_id": req.request_id}
        except chaos.ClassicOffline as e:
            return 409, {"detail": str(e), "error_code": "ClassicOffline", "request_id": req.request_id}
        except chaos.UnhandledFault:
            raise
        except Exception as e:  # noqa: BLE001 — every AWS error becomes a mapped 5xx
            return _map_exception(e, req)
    return 404, {"detail": f"No route for {req.method} {req.path}"}


def _route_tag(path):
    parts = [p for p in path.split("/") if p][:3]
    if len(parts) == 3 and parts[1] in ("flights", "chaos", "classic"):
        parts[2] = "{id}"
    return "/" + "/".join(parts)


def handler(event, context):
    global _COLD, _INVOCATIONS, _COLD_THIS_REQUEST
    _COLD_THIS_REQUEST, _COLD = _COLD, False
    _INVOCATIONS += 1
    t0 = time.perf_counter()
    req = Request(event, context)
    tags = [f"route:{_route_tag(req.path)}", f"method:{req.method}", f"cold_start:{str(_COLD_THIS_REQUEST).lower()}"]
    try:
        status, body = _dispatch(req)
    except chaos.UnhandledFault:
        telemetry.metric("avionpulse.api.requests", 1, tags + ["status_class:5xx"])
        log.error("[FAULT] unhandled exception escaping handler", exc_info=True)
        raise
    elapsed = (time.perf_counter() - t0) * 1000
    telemetry.metric("avionpulse.api.requests", 1, tags + [f"status_class:{status // 100}xx"])
    telemetry.metric("avionpulse.api.latency_ms", round(elapsed, 1), tags)
    return respond(status, body, {"x-avion-cold-start": str(_COLD_THIS_REQUEST).lower(),
                                  "x-avion-request-id": req.request_id,
                                  "x-avion-duration-ms": f"{elapsed:.1f}"})
