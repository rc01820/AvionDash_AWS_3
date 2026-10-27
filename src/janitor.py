"""Janitor — its own EventBridge schedule (every 2 minutes).

v2 ran the janitor inside the simulator; v3 can disable the simulator's rule
(eventbridge_rule_disabled), so cleanup needs a schedule no fault touches:
  1. revert chaos faults whose TTL has expired
  2. tear down the Classic tier when it passes its expiry (backstop for the
     one-time Scheduler job) and retry a delete that failed
"""
import logging

import chaos
import classic
import telemetry

telemetry.setup_logging()
log = logging.getLogger("avionpulse.janitor")


def run():
    expired = chaos.janitor()
    classic_action = None
    try:
        classic_action = (classic.janitor() or {}).get("phase")
    except Exception as e:  # noqa: BLE001 — fault cleanup above must still count as done
        log.error("classic janitor failed: %s", e, exc_info=True)
    summary = {"faults_expired": expired, "active_faults": chaos.active_count(), "classic": classic_action}
    log.info("janitor run complete", extra=summary)
    return summary


def handler(event, context):
    return run()
