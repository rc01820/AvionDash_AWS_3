"""AvionPulse gate-assignment service — the Classic tier's app.

Runs on the EC2 instances of the on-demand Classic stack (Amazon Linux 2023,
systemd unit written by the launch template's user data), behind the ALB, and
keeps gate assignments in RDS PostgreSQL (psycopg 3). Launched under
ddtrace-run when Datadog is enabled: ddtrace instruments psycopg, and this
module extracts the trace context the api Lambda sends, so the request span
and its postgres spans join the Lambda's trace.

    GET /health   ALB health check — deliberately does NOT touch the database
    GET /gates    gate board; reassigns one gate per call so there are writes

The classic_db_slow_query chaos switch is read from the same DynamoDB item the
Lambdas use (CONFIG / FAULTS), cached for 5 seconds.
"""
import json
import logging
import os
import random
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
import psycopg

try:  # present when launched under ddtrace-run (EnableDatadog=true)
    from ddtrace import tracer
    from ddtrace.propagation.http import HTTPPropagator
except ImportError:
    tracer = None

PORT = int(os.environ.get("PORT", "8080"))
DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.environ.get("DB_NAME", "gates")
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
TABLE_NAME = os.environ["TABLE_NAME"]
REGION = os.environ.get("AWS_REGION", "us-east-1")
RDS_CA = os.environ.get("RDS_CA_BUNDLE", "/opt/avionpulse/rds-ca.pem")  # downloaded by user data
GATES = [f"{t}{n}" for t in "ABC" for n in range(1, 9)]


class JsonFormatter(logging.Formatter):
    def format(self, record):
        doc = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
               "logger": record.name, "message": record.getMessage(), "service": "avionpulse-gates"}
        for k in ("dd.trace_id", "dd.span_id", "dd.service", "dd.env", "dd.version"):
            if hasattr(record, k):
                doc[k] = getattr(record, k)
        if record.exc_info:
            doc["error.stack"] = self.formatException(record.exc_info)
        return json.dumps(doc)


os.makedirs("/var/log/avionpulse", exist_ok=True)
_handlers = [logging.StreamHandler(), logging.FileHandler("/var/log/avionpulse/gates.log")]
for h in _handlers:
    h.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=_handlers)
log = logging.getLogger("avionpulse.gates")

ddb = boto3.client("dynamodb", region_name=REGION)
secrets = boto3.client("secretsmanager", region_name=REGION)
_creds = {}
_faults = {"at": 0.0, "slow": False}
STATE = {"db_ready": False, "instance_id": "unknown", "az": "unknown"}


def _imds(path):
    tok = urllib.request.urlopen(urllib.request.Request(
        "http://169.254.169.254/latest/api/token", method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"}), timeout=2).read().decode()
    return urllib.request.urlopen(urllib.request.Request(
        f"http://169.254.169.254/latest/meta-data/{path}",
        headers={"X-aws-ec2-metadata-token": tok}), timeout=2).read().decode()


def _connect():
    if not _creds:
        _creds.update(json.loads(secrets.get_secret_value(SecretId=DB_SECRET_ARN)["SecretString"]))
    return psycopg.connect(host=DB_HOST, dbname=DB_NAME, user=_creds["username"], password=_creds["password"],
                           connect_timeout=3, sslmode="verify-full", sslrootcert=RDS_CA, autocommit=True)


def _slow_queries():
    now = time.time()
    if now - _faults["at"] > 5:
        try:
            item = ddb.get_item(TableName=TABLE_NAME, Key={"PK": {"S": "CONFIG"}, "SK": {"S": "FAULTS"}})
            f = item.get("Item", {}).get("faults", {}).get("M", {}).get("classic_db_slow_query", {}).get("M", {})
            _faults["slow"] = float(f.get("until", {}).get("N", 0)) > now
        except Exception as e:  # noqa: BLE001 — chaos switch is best effort
            log.warning("fault state read failed: %s", e)
        _faults["at"] = now
    return _faults["slow"]


def init_db():
    """Create and seed the gates table; retries until RDS is reachable."""
    while True:
        try:
            with _connect() as con:
                con.execute("""CREATE TABLE IF NOT EXISTS gates (
                                 gate TEXT PRIMARY KEY, terminal TEXT NOT NULL, status TEXT NOT NULL,
                                 flight TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
                if con.execute("SELECT count(*) FROM gates").fetchone()[0] == 0:
                    for g in GATES:
                        occupied = random.random() < 0.5
                        con.execute("INSERT INTO gates (gate, terminal, status, flight) VALUES (%s, %s, %s, %s)",
                                    (g, g[0], "occupied" if occupied else "open",
                                     f"AP{random.randint(100, 999)}" if occupied else None))
            STATE["db_ready"] = True
            log.info("database ready at %s", DB_HOST)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("database not ready yet: %s", e)
            time.sleep(10)


def gate_board():
    t = time.perf_counter()
    with _connect() as con:
        if _slow_queries():
            log.warning("[FAULT] classic_db_slow_query adding pg_sleep(2.5)")
            con.execute("SELECT pg_sleep(2.5)")
        occupied = random.random() < 0.55
        con.execute("UPDATE gates SET status = %s, flight = %s, updated_at = now() WHERE gate = %s",
                    ("occupied" if occupied else "open", f"AP{random.randint(100, 999)}" if occupied else None,
                     random.choice(GATES)))
        rows = con.execute("SELECT gate, terminal, status, flight, updated_at FROM gates ORDER BY gate").fetchall()
    return {"gates": [{"gate": r[0], "terminal": r[1], "status": r[2], "flight": r[3],
                       "updated_at": r[4].isoformat()} for r in rows],
            "instance_id": STATE["instance_id"], "az": STATE["az"],
            "db_ms": round((time.perf_counter() - t) * 1000, 1), "served_by": "avionpulse-gates"}


class Handler(BaseHTTPRequestHandler):
    server_version = "AvionPulseGates/3.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok", "instance_id": STATE["instance_id"]})
        if self.path.startswith("/gates"):
            if not STATE["db_ready"]:
                return self._send(503, {"detail": "database initialising"})
            if tracer is None:
                return self._gates()
            # Continue the trace the api Lambda started (it sends x-datadog-* headers).
            tracer.context_provider.activate(HTTPPropagator.extract(dict(self.headers)))
            with tracer.trace("http.request", service="avionpulse-gates", resource="GET /gates",
                              span_type="web") as span:
                span.set_tag("http.method", "GET")
                span.set_tag("http.url", self.path)
                status = self._gates()
                span.set_tag("http.status_code", str(status))
                if status >= 500:
                    span.error = 1
            return None
        self._send(404, {"detail": "not found"})

    def _gates(self):
        try:
            self._send(200, gate_board())
            return 200
        except Exception as e:  # noqa: BLE001
            log.error("gate board failed: %s", e, exc_info=True)
            self._send(500, {"detail": f"database error: {type(e).__name__}: {e}"[:300]})
            return 500


def main():
    try:
        STATE["instance_id"] = _imds("instance-id")
        STATE["az"] = _imds("placement/availability-zone")
    except Exception as e:  # noqa: BLE001
        log.warning("instance metadata unavailable: %s", e)
    threading.Thread(target=init_db, daemon=True).start()
    log.info("gate service listening on :%d", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
