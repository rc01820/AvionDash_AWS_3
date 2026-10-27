"""Steady synthetic traffic so dashboards and monitors have something to chew on.

    python scripts/loadgen.py https://dxxxx.cloudfront.net --rps 0.5 --minutes 30
    python scripts/loadgen.py http://localhost:8080 --rps 2 --minutes 5

Cost note: 0.5 req/s for a full month is ~1.3M requests — past the free
allowances for API Gateway and Lambda requests (roughly $1.60/month extra).
Run it during demos, not 24/7. Standard library only.
"""
import argparse
import json
import random
import time
import urllib.error
import urllib.request

PATHS = [
    ("/api/dashboard/summary", 4), ("/api/flights?limit=100", 4), ("/api/flights?status=en_route", 2),
    ("/api/aircraft", 2), ("/api/airports", 1), ("/api/events", 2), ("/api/notams", 1),
    ("/api/weather", 2), ("/api/health", 1), ("/api/flights/AVP101", 1),
]


def call(base, path, token=None, body=None):
    headers = {"content-type": "application/json", "user-agent": "avionpulse-loadgen"}
    if token:
        headers["x-avion-token"] = token
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body else None,
                                 headers=headers, method="POST" if body else "GET")
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=35) as r:
            return r.status, json.loads(r.read() or b"null"), time.perf_counter() - t
    except urllib.error.HTTPError as e:
        return e.code, None, time.perf_counter() - t
    except (urllib.error.URLError, TimeoutError):
        return 0, None, time.perf_counter() - t


def main():
    p = argparse.ArgumentParser()
    p.add_argument("base", help="site URL, e.g. https://dxxxx.cloudfront.net")
    p.add_argument("--rps", type=float, default=0.5)
    p.add_argument("--minutes", type=float, default=15)
    p.add_argument("--user", default="operator")
    p.add_argument("--password", default="operator123")
    a = p.parse_args()
    base = a.base.rstrip("/")

    status, data, _ = call(base, "/api/auth/token", body={"username": a.user, "password": a.password})
    if status != 200:
        raise SystemExit(f"login failed: HTTP {status}")
    token = data["access_token"]
    weighted = [path for path, w in PATHS for _ in range(w)]
    end, counts, n = time.time() + a.minutes * 60, {}, 0
    print(f"loadgen -> {base} at {a.rps} req/s for {a.minutes} min")
    while time.time() < end:
        if random.random() < 0.03:
            status, _, dt = call(base, "/api/events", token, {
                "tail_number": f"N-AVP{random.randint(1, 15):03d}", "severity": random.choice(["info", "warning"]),
                "category": "MX", "message": "Loadgen: routine line maintenance check"})
        else:
            status, _, dt = call(base, random.choice(weighted), token)
        counts[status] = counts.get(status, 0) + 1
        n += 1
        if n % 20 == 0:
            print(f"{n} requests  status={dict(sorted(counts.items()))}  last={dt * 1000:.0f} ms")
        time.sleep(max(0.0, 1 / a.rps - dt))
    print("done", counts)


if __name__ == "__main__":
    main()
