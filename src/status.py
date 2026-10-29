"""Public status page — GET /status (HTML) and GET /api/status (JSON).

No login required. Each component is checked with one cheap call and the
result is cached for 15 seconds per Lambda container, so an uptime probe
(Datadog Synthetics, an Agent HTTP check) can poll it often without eating
the table's 5 RCU.

Only component health is published. Fault names, users and resource names
stay private.

The status_page_error chaos fault makes both endpoints return HTTP 500, so
external probes have something real to catch.
"""
import html
import logging
import time
from datetime import datetime, timezone

import classic
import infra
from store import err_code, get_store

log = logging.getLogger("avionpulse.status")

CACHE_S = 15
_cache = {"at": 0.0, "doc": None}
ORDER = {"operational": 0, "not_deployed": 0, "provisioning": 1, "degraded": 1, "outage": 2}
LABEL = {"operational": "Operational", "degraded": "Degraded", "outage": "Outage",
         "provisioning": "Provisioning", "not_deployed": "Not deployed"}
OVERALL = {0: "All systems operational", 1: "Some systems degraded", 2: "Service outage"}


class StatusPageFault(Exception):
    """Raised on purpose by the status_page_error fault."""


def _component(name, fn):
    t = time.perf_counter()
    try:
        state, note = fn()
    except Exception as e:  # noqa: BLE001 — a failing dependency is an outage, not a page error
        log.warning("status check %s failed: %s", name, err_code(e))
        state, note = "outage", "check failed"
    return {"name": name, "status": state, "label": LABEL[state], "note": note,
            "ms": round((time.perf_counter() - t) * 1000, 1)}


def _flights():
    flights = get_store().query_pk("FLIGHT")
    newest = max((f.get("updated_epoch", 0) for f in flights), default=0)
    age = time.time() - newest if newest else None
    if age is None:
        return "outage", "no flight data"
    if age < 900:
        return "operational", f"updated {int(age // 60)} min ago"
    return ("degraded" if age < 1800 else "outage"), f"data is {int(age // 60)} min old"


def _pipeline():
    q = infra.queue_depths()
    if q.get("dlq_visible", 0) > 0:
        return "degraded", "some events need attention"
    return "operational", "processing normally"


def _notams():
    infra.get_s3_json("data/notams.json")
    return "operational", "NOTAM feed available"


def _integrations():
    items = get_store().query_pk("INTEG")
    now = time.time()
    fresh = sum(1 for i in items if i.get("SK") in ("stream", "bus", "sns", "turnaround", "recorder")
                and i.get("last_seen") and now - i["last_seen"] < 900)
    if fresh == 5:
        return "operational", "all 5 channels flowing"
    return ("degraded" if fresh else "outage"), f"{fresh} of 5 channels flowing"


def _gates():
    st = classic.describe()
    if st is None:
        return "not_deployed", "on-demand tier is offline"
    s = st["StackStatus"]
    if s in classic.ONLINE:
        return "operational", "online"
    if s.endswith("_IN_PROGRESS"):
        return "provisioning", s.replace("_", " ").lower()
    return "outage", "stack failed"


def compute(force=False):
    if not force and _cache["doc"] and time.time() - _cache["at"] < CACHE_S:
        return _cache["doc"]
    comps = [
        {"name": "Web & API", "status": "operational", "label": "Operational", "note": "responding", "ms": 0.0},
        _component("Flight data", _flights),
        _component("Ops pipeline", _pipeline),
        _component("NOTAM service", _notams),
        _component("Integrations", _integrations),
        _component("Gate services", _gates),
    ]
    worst = max(ORDER[c["status"]] for c in comps)
    doc = {"service": "AvionPulse", "overall": OVERALL[worst],
           "overall_status": ("operational", "degraded", "outage")[worst],
           "components": comps, "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
    _cache.update(at=time.time(), doc=doc)
    return doc


_CSS = """
:root{--bg:#070b13;--p:#0c1424;--b:rgba(0,198,255,.18);--t:#e8f4fd;--s:#8fadc8;--d:#4a6a85;
--ok:#00e676;--warn:#ffd600;--bad:#ff3d5a;--info:#82b1ff;--a:#00c6ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--t);font:15px/1.5 Inter,system-ui,-apple-system,Segoe UI,sans-serif;
padding:40px 16px;min-height:100vh}
main{max-width:760px;margin:0 auto}
h1{font:700 1.4rem/1.2 Orbitron,monospace,sans-serif;letter-spacing:.06em;color:var(--a)}
.sub{color:var(--s);font-size:.85rem;margin-top:4px}
.banner{margin:26px 0;padding:18px 20px;border-radius:8px;border:1px solid;font-weight:600;font-size:1.05rem}
.banner.operational{color:var(--ok);border-color:rgba(0,230,118,.4);background:rgba(0,230,118,.07)}
.banner.degraded{color:var(--warn);border-color:rgba(255,214,0,.4);background:rgba(255,214,0,.06)}
.banner.outage{color:var(--bad);border-color:rgba(255,61,90,.45);background:rgba(255,61,90,.08)}
ul{list-style:none;border:1px solid var(--b);border-radius:8px;background:var(--p)}
li{display:flex;justify-content:space-between;gap:12px;padding:14px 18px;border-top:1px solid var(--b)}
li:first-child{border-top:none}
.name{font-weight:600}.note{color:var(--d);font-size:.8rem}
.st{font:600 .78rem ui-monospace,Menlo,monospace;white-space:nowrap;align-self:center}
.st.operational{color:var(--ok)}.st.degraded,.st.provisioning{color:var(--warn)}
.st.outage{color:var(--bad)}.st.not_deployed{color:var(--info)}
footer{margin-top:22px;color:var(--d);font-size:.78rem;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
a{color:var(--a)}
"""


def render_html(doc):
    rows = "".join(
        f'<li><div><div class="name">{html.escape(c["name"])}</div>'
        f'<div class="note">{html.escape(c["note"])}</div></div>'
        f'<span class="st {c["status"]}">● {html.escape(c["label"])}</span></li>' for c in doc["components"])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta http-equiv="refresh" content="60"/><title>AvionPulse Status</title><style>{_CSS}</style></head>
<body><main>
<h1>AvionPulse Status</h1>
<div class="sub">Live health of the AvionPulse operations platform · refreshes every minute</div>
<div class="banner {doc['overall_status']}" id="overall">{html.escape(doc['overall'])}</div>
<ul id="components">{rows}</ul>
<footer><span>Last checked {html.escape(doc['checked_at'])} UTC · <a href="/api/status">JSON</a></span>
<span><a href="/">Sign in to the operations centre →</a></span></footer>
</main></body></html>"""


def render_error_html():
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>AvionPulse Status — error</title><style>{_CSS}</style></head>
<body><main><h1>AvionPulse Status</h1>
<div class="banner outage">500 — the status page is unavailable</div>
<div class="sub">We couldn't load system status. Please try again shortly.</div>
</main></body></html>"""
