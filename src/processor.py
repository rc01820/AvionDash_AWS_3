"""Integration processor — one Lambda behind four AWS event sources:

  * DynamoDB Streams     FLIGHT item changes (filtered at the event source mapping)
  * SNS                  crew-alerts topic
  * EventBridge          FlightStatusChanged on the flight-ops bus, and
                         S3 "Object Created" for the flight-data recorder bucket
  * Step Functions       tasks of the aircraft-turnaround Express workflow

Each source updates one INTEG/<channel> item (last seen, count, last detail) so
the Integrations page can show which pipes are flowing. The writes are tiny and
one per invocation, which keeps the 5 WCU table comfortable.
"""
import json
import logging
import random
import time

import chaos
import telemetry
from store import get_store

telemetry.setup_logging()
log = logging.getLogger("avionpulse.processor")

GATES = [f"{t}{n}" for t in "ABC" for n in range(1, 9)]


class CrewCheckFailed(Exception):
    """Raised by the turnaround workflow's crew-check task under stepfn_task_failure."""


class StreamBatchRejected(Exception):
    """Raised for every DynamoDB stream batch under stream_processor_poison."""


def record(channel, detail, n=1):
    get_store().incr("INTEG", channel, "count", n, {"last_seen": int(time.time()), "last_detail": detail})
    telemetry.metric("avionpulse.integration.received", n, [f"channel:{channel}"])


def _stream(records):
    if chaos.active("stream_processor_poison"):
        log.error("[FAULT] stream_processor_poison rejecting batch of %d records", len(records))
        raise StreamBatchRejected(f"schema v2 decoder cannot read {len(records)} FLIGHT stream records (injected)")
    flights = sorted({r["dynamodb"]["Keys"]["SK"]["S"] for r in records})
    record("stream", {"records": len(records), "flights": flights[:6]}, len(records))


def _task(event):
    task = event.get("task")
    flight = event.get("flight", "?")
    if task == "assign_gate":
        return dict(event, gate=random.Random(flight + str(int(time.time() // 3600))).choice(GATES))
    if task == "crew_check":
        if chaos.active("stepfn_task_failure"):
            if random.random() < 0.5:
                log.error("[FAULT] stepfn_task_failure crew roster lookup failed for %s", flight)
                raise CrewCheckFailed(f"Crew roster service returned no duty record for {flight} (injected)")
            log.error("[FAULT] stepfn_task_failure crew check hanging past the 5 s task timeout")
            time.sleep(8)
        return dict(event, crew_ok=True)
    if task == "complete":
        record("turnaround", {"flight": flight, "gate": event.get("gate")})
        return dict(event, status="turned")
    raise ValueError(f"unknown task {task!r}")


def handler(event, context):
    if "task" in event:
        return _task(event)
    recs = event.get("Records") or []
    if recs and recs[0].get("eventSource") == "aws:dynamodb":
        return _stream(recs)
    if recs and recs[0].get("EventSource") == "aws:sns":
        for r in recs:
            msg = r["Sns"].get("Message", "{}")
            try:
                msg = json.loads(msg)
            except ValueError:
                pass
            record("sns", {"subject": r["Sns"].get("Subject"), "message": msg})
        return {"ok": True}
    dtype = event.get("detail-type")
    if dtype == "FlightStatusChanged":
        record("bus", event.get("detail", {}))
        return {"ok": True}
    if dtype == "Object Created":
        obj = event.get("detail", {}).get("object", {})
        record("recorder", {"key": obj.get("key"), "size": obj.get("size")})
        return {"ok": True}
    log.warning("processor: unrecognised event %s", json.dumps(event, default=str)[:300])
    return {"ok": False}
