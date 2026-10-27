"""Launch, check or tear down the on-demand Classic tier from the command line.

Goes through the deployed app's API (same code path as the Chaos page button,
so the auto-shutdown schedule and fault bookkeeping are identical):

    python scripts/classic.py up     --url https://dxxxx.cloudfront.net --size minimal --hours 2
    python scripts/classic.py status --url https://dxxxx.cloudfront.net
    python scripts/classic.py down   --url https://dxxxx.cloudfront.net

The admin password comes from --password or $AVIONPULSE_ADMIN_PASSWORD
(default: the demo password). If the app itself is unreachable, use
    python scripts/restore.py --classic-down
which deletes the stack directly with the AWS CLI. Standard library only.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def call(base, path, token=None, body=None):
    req = urllib.request.Request(base.rstrip("/") + "/api" + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", **({"X-Avion-Token": token} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except ValueError:
            detail = e.reason
        sys.exit(f"HTTP {e.code}: {detail}")


def show(st):
    left = f", auto-shutdown in {int((st['expires_at'] - time.time()) / 60)} min" if st.get("expires_at") else ""
    print(f"Classic tier: {st['phase'].upper()} ({st.get('stack_status') or 'no stack'}) size={st['size']}"
          f"{left}, cost so far ${st['cost_so_far_usd']:.2f}")
    for e in st.get("events", [])[:5]:
        print(f"    {e['status']:<22} {e['resource']} {e.get('reason', '')}")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("action", choices=["up", "down", "status"])
    p.add_argument("--url", required=True, help="site URL (stack output SiteUrl)")
    p.add_argument("--password", default=os.environ.get("AVIONPULSE_ADMIN_PASSWORD", "aviondash123"))
    p.add_argument("--size", choices=["minimal", "full"], default="minimal")
    p.add_argument("--hours", type=int, default=2)
    p.add_argument("--wait", action="store_true", help="poll until the stack is online / gone")
    a = p.parse_args()

    tok = call(a.url, "/auth/token", body={"username": "admin", "password": a.password})["access_token"]
    if a.action == "up":
        st = call(a.url, "/classic/launch", tok, {"size": a.size, "hours": a.hours})
    elif a.action == "down":
        st = call(a.url, "/classic/teardown", tok, {})
    else:
        st = call(a.url, "/classic/status", tok)
    show(st)
    target = {"up": "online", "down": "offline"}.get(a.action)
    while a.wait and target and st["phase"] not in (target, "failed"):
        time.sleep(20)
        st = call(a.url, "/classic/status", tok)
        show(st)


if __name__ == "__main__":
    main()
