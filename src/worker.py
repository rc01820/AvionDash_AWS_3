"""SQS worker — async tier. Consumes maintenance requests and ops events,
writes them to DynamoDB as EVENT items (7-day TTL).

Uses partial batch responses (ReportBatchItemFailures): a poison message
fails on its own, SQS redelivers it, and after 3 receives the redrive policy
moves it to the dead-letter queue.
"""
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import config
import infra
import telemetry
from store import SimulatedAwsError, get_store

telemetry.setup_logging()
log = logging.getLogger("avionpulse.worker")

EVENT_TTL_S = 7 * 24 * 3600


class PoisonMessage(Exception):
    pass


def _process(record):
    try:
        msg = json.loads(record["body"])
    except json.JSONDecodeError as e:
        raise PoisonMessage(f"body is not JSON: {e}")
    if msg.get("type") == "poison":
        raise PoisonMessage("payload failed schema validation (injected poison pill)")
    if msg.get("type") not in ("maintenance_request", "ops_event"):
        raise PoisonMessage(f"unknown message type {msg.get('type')!r}")

    if config.LOCAL and not infra.LOCAL_SIM["worker_policy_attached"]:
        raise SimulatedAwsError("AccessDeniedException",
                                f"User: arn:aws:sts::000000000000:assumed-role/{config.WORKER_ROLE}/avionpulse-worker "
                                f"is not authorized to perform: dynamodb:PutItem on resource: {config.TABLE_NAME} "
                                "because no identity-based policy allows the dynamodb:PutItem action")
    now = time.time()
    lag = max(0.0, now - float(msg.get("enqueued_at", now)))
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    get_store().put({
        "PK": "EVENT",
        "SK": f"{ts}#{uuid.uuid4().hex[:6]}",
        "type": msg["type"],
        "tail_number": msg.get("tail_number"),
        "airport": msg.get("airport"),
        "category": msg.get("category", "OPS"),
        "severity": msg.get("severity", "info"),
        "message": msg.get("message", ""),
        "submitted_by": msg.get("submitted_by", "simulator"),
        "source": msg.get("source", "simulator"),
        "processed_at": ts,
        "queue_lag_s": round(lag, 2),
        "ttl": int(now + EVENT_TTL_S),
    })
    telemetry.metric("avionpulse.queue.processing_lag_s", round(lag, 2), [f"type:{msg['type']}"])
    telemetry.metric("avionpulse.events.processed", 1,
                     [f"type:{msg['type']}", f"severity:{msg.get('severity', 'info')}"])


def handler(event, context):
    failures = []
    for record in event.get("Records", []):
        try:
            _process(record)
        except PoisonMessage as e:
            receives = record.get("attributes", {}).get("ApproximateReceiveCount", "1")
            log.error("Poison message %s (receive #%s): %s", record.get("messageId"), receives, e)
            telemetry.metric("avionpulse.events.poison", 1)
            failures.append({"itemIdentifier": record["messageId"]})
        except Exception as e:
            log.error("Failed to process %s: %s", record.get("messageId"), e, exc_info=True)
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
