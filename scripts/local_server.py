"""Run AvionPulse locally with no AWS account.

    python scripts/local_server.py            # http://localhost:8080

Serves web/ like CloudFront, routes /api/* to the real api.handler like API
Gateway, and runs the simulator, janitor and SQS worker on background threads.
DynamoDB, SQS, SNS, EventBridge, Step Functions and the AWS control plane are
replaced by in-memory fakes, so every page and every chaos toggle works — infra
faults are simulated instead of applied. The Classic tier "launches" in ~45 s.

    python scripts/local_server.py --port 8094 --sim-interval 15

Full guide: docs/LOCAL_MODE.md
"""
import argparse
import json
import mimetypes
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

for _stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("AVIONPULSE_LOCAL", "1")
os.environ.setdefault("LOCAL_WEB_ROOT", os.path.join(ROOT, "web"))
sys.path.insert(0, os.path.join(ROOT, "src"))

import api  # noqa: E402
import chaos  # noqa: E402
import infra  # noqa: E402
import janitor  # noqa: E402
import seed  # noqa: E402
import simulator  # noqa: E402
import worker  # noqa: E402

WEB = os.path.join(ROOT, "web")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")


class FakeContext:
    function_name = "avionpulse-api (local)"
    memory_limit_in_mb = 256
    log_stream_name = "local"

    def __init__(self):
        self.aws_request_id = str(uuid.uuid4())
        self._deadline = time.time() + 10

    def get_remaining_time_in_millis(self):
        return max(0, int((self._deadline - time.time()) * 1000))


class StageThrottle:
    """Token bucket standing in for the API Gateway stage throttle."""

    def __init__(self):
        self.tokens, self.last, self.lock = 40.0, time.time(), threading.Lock()

    def allow(self):
        rate, burst = infra.LOCAL_SIM["api_throttle"] or (1000, 1000)
        with self.lock:
            now = time.time()
            self.tokens = min(burst, self.tokens + (now - self.last) * rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


THROTTLE = StageThrottle()


class Handler(BaseHTTPRequestHandler):
    server_version = "AvionPulseLocal/1.0"

    def log_message(self, fmt, *args):
        if "/api/" in str(args[0] if args else ""):
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, status, body, headers=None, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        for k, v in (headers or {}).items():
            if k.lower() not in ("content-type", "content-length"):
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _api(self):
        # Control routes have their own throttle and their own integration in AWS.
        own_route = self.path.startswith(("/api/chaos/", "/api/auth/", "/api/classic/", "/api/aws/"))
        if not own_route and not THROTTLE.allow():
            return self._send(429, {"message": "Too Many Requests"})
        if not own_route and infra.LOCAL_SIM["integration_broken"]:  # api_integration_broken
            return self._send(500, {"message": "Internal Server Error"})
        parts = urlsplit(self.path)
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        event = {
            "version": "2.0",
            "rawPath": parts.path,
            "rawQueryString": parts.query,
            "queryStringParameters": dict(parse_qsl(parts.query)) or None,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
            "isBase64Encoded": False,
            "requestContext": {"apiId": "local", "requestId": str(uuid.uuid4()),
                               "http": {"method": self.command, "path": parts.path}},
        }
        try:
            resp = api.handler(event, FakeContext())
        except chaos.UnhandledFault:
            return self._send(500, {"message": "Internal Server Error"})
        headers = resp.get("headers") or {}
        self._send(resp["statusCode"], resp["body"].encode(), headers,
                   ctype=headers.get("content-type", "application/json"))

    def _static(self):
        path = urlsplit(self.path).path
        if path == "/":
            path = "/index.html"
        full = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not full.startswith(WEB) or not os.path.isfile(full):
            return self._send(404, b"Not found", ctype="text/plain")
        with open(full, "rb") as fh:
            data = fh.read()
        self._send(200, data, {"cache-control": "no-cache"},
                   mimetypes.guess_type(full)[0] or "application/octet-stream")

    def do_GET(self):
        # /status is rendered by the api function (CloudFront sends it to API Gateway in AWS).
        is_api = self.path.startswith("/api/") or urlsplit(self.path).path in ("/status", "/status/")
        self._api() if is_api else self._static()

    def do_POST(self):
        self._api() if self.path.startswith("/api/") else self._send(405, b"", ctype="text/plain")


def simulator_loop(interval):
    while True:
        time.sleep(interval)
        if not infra.LOCAL_SIM["sim_rule_enabled"]:  # eventbridge_rule_disabled
            continue
        try:
            simulator.run()
        except Exception as e:  # simulator_crash fault lands here
            print(f"[simulator] run failed: {e}", file=sys.stderr)


def janitor_loop():
    while True:
        time.sleep(20)
        try:
            janitor.run()
        except Exception as e:  # noqa: BLE001
            print(f"[janitor] run failed: {e}", file=sys.stderr)


def worker_loop():
    """Drain the fake queue like an SQS event source mapping (redrive after 3)."""
    while True:
        time.sleep(2)
        with infra._local_lock:  # sqs_retention_cut: messages older than retention vanish
            keep = time.time() - infra.LOCAL_SIM["queue_retention"]
            infra.LOCAL_QUEUE[:] = [m for m in infra.LOCAL_QUEUE if m["sent"] >= keep]
        if infra.worker_paused():
            continue
        with infra._local_lock:
            batch, infra.LOCAL_QUEUE[:] = infra.LOCAL_QUEUE[:10], infra.LOCAL_QUEUE[10:]
        if not batch:
            continue
        for m in batch:
            m["receiveCount"] += 1
        records = [{"messageId": m["messageId"], "body": m["body"],
                    "attributes": {"ApproximateReceiveCount": str(m["receiveCount"])}} for m in batch]
        if infra.LOCAL_SIM["worker_handler"] != infra.GOOD_WORKER_HANDLER:  # lambda_bad_deploy
            print(f"[worker] Runtime.ImportModuleError: No module named "
                  f"'{infra.LOCAL_SIM['worker_handler'].split('.')[0]}'", file=sys.stderr)
            failed = {m["messageId"] for m in batch}
        else:
            failed = {f["itemIdentifier"] for f in worker.handler({"Records": records}, None)["batchItemFailures"]}
        with infra._local_lock:
            for m in batch:
                if m["messageId"] in failed:
                    (infra.LOCAL_DLQ if m["receiveCount"] >= 3 else infra.LOCAL_QUEUE).append(m)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--sim-interval", type=int, default=60, help="seconds between simulator runs")
    args = p.parse_args()

    seed.seed_all()
    simulator.run()
    threading.Thread(target=simulator_loop, args=(args.sim_interval,), daemon=True).start()
    threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=janitor_loop, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"AvionPulse local -> http://localhost:{args.port}  (admin / aviondash123)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
