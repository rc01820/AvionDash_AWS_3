"""Flight simulator — scheduled by EventBridge (default every 5 minutes).

Each run:
  1. recomputes every flight from the lifecycle model and writes it to DynamoDB
     (the table's stream feeds the processor Lambda)
  2. emits business metrics (airborne flights, passengers, on-time %, fleet)
  3. drops a few ops events on the SQS queue for the worker
  4. feeds the integration channels: FlightStatusChanged events on the custom
     EventBridge bus, one crew alert on SNS, one turnaround workflow execution
     (Step Functions) and one flight-data-recorder snapshot in S3
  5. runs the scheduled side of async faults (cold-start waves, backlog flood,
     poison pills, crash). Expired faults are reverted by the janitor Lambda.
"""
import logging
import random
import time

import chaos
import config
import flightsim
import infra
import telemetry
from store import err_code
from seed_data import AIRCRAFT, AIRPORTS, OPS_EVENT_TEMPLATES
from store import get_store

telemetry.setup_logging()
log = logging.getLogger("avionpulse.simulator")


class SimulatorCrash(Exception):
    pass


def _ops_event(rng):
    sev, cat, tmpl = rng.choice(OPS_EVENT_TEMPLATES)
    tail = rng.choice(AIRCRAFT)[0]
    apt = rng.choice(AIRPORTS)[0]
    return {"type": "ops_event", "severity": sev, "category": cat, "tail_number": tail, "airport": apt,
            "message": tmpl.format(tail=tail, apt=apt), "enqueued_at": time.time(), "source": "simulator"}


def run(now=None):
    now = now or time.time()
    store = get_store()

    if chaos.active("cold_start_storm"):
        try:
            infra.recycle_api_function(2500)
            log.warning("[CHAOS] cold_start_storm: recycled API execution environments")
        except Exception as e:
            log.error("cold_start_storm recycle failed: %s", e)

    flights = flightsim.all_flights(now)
    previous = {f["SK"]: f.get("status") for f in store.query_pk("FLIGHT")}
    if chaos.active("simulator_crash"):
        store.put_many(flights[: len(flights) // 2])
        raise SimulatorCrash("KeyError: 'arrival_slot' — flight state writer crashed mid-batch (injected)")
    store.put_many(flights)
    if config.LOCAL:
        infra.local_stream(flights)

    # Business metrics are computed from what the data tier returns, so a
    # silent data bug shows up here even though nothing errors.
    pk = "FLIGHTS" if chaos.active("silent_failure") else "FLIGHT"
    stored = store.query_pk(pk)
    pax_mult = 847 if chaos.active("pax_corruption") else 1
    airborne = [f for f in stored if f.get("status") in ("en_route", "departed")]
    delayed = [f for f in stored if f.get("delay_minutes", 0) > 0]
    cancelled = [f for f in stored if f.get("status") == "cancelled"]
    total = len(stored)
    telemetry.metric("avionpulse.flights.airborne", len(airborne))
    telemetry.metric("avionpulse.flights.delayed", len(delayed))
    telemetry.metric("avionpulse.flights.cancelled", len(cancelled))
    telemetry.metric("avionpulse.flights.total", total)
    telemetry.metric("avionpulse.flights.on_time_pct",
                     round((total - len(delayed) - len(cancelled)) / max(total, 1) * 100, 1))
    telemetry.metric("avionpulse.pax.airborne", sum(int(f.get("pax", 0)) for f in airborne) * pax_mult)
    telemetry.metric("avionpulse.fleet.in_maintenance", sum(1 for a in AIRCRAFT if a[5] == "maintenance"))

    rng = random.Random()
    events = [_ops_event(rng) for _ in range(rng.randint(1, 3))]
    if chaos.active("queue_backlog"):
        events += [_ops_event(rng) for _ in range(20)]
    failures = {}

    def attempt(channel, fn, *args):
        try:
            fn(*args)
        except Exception as e:  # noqa: BLE001 — one broken channel must not starve the others
            failures[channel] = err_code(e)
            telemetry.metric("avionpulse.integration.errors", 1, [f"channel:{channel}", f"error:{err_code(e)}"])
            log.error("simulator: %s failed: %s", channel, e, exc_info=True)

    for e in events:
        attempt("sqs", infra.send_event, e)
    if chaos.active("sqs_poison_pill"):
        for _ in range(5):
            attempt("sqs", infra.send_event, {"type": "poison", "payload": "\x00\x7f{not-json", "source": "simulator"})

    changed = [{"flight": f["SK"], "from": previous.get(f["SK"]), "to": f.get("status"),
                "origin": f.get("origin_iata"), "destination": f.get("destination_iata")}
               for f in flights if previous.get(f["SK"]) != f.get("status")][:5]
    if not changed:  # quiet run: still send one event so the bus channel shows it's alive
        f = rng.choice(flights)
        changed = [{"flight": f["SK"], "from": f.get("status"), "to": f.get("status"), "heartbeat": True,
                    "origin": f.get("origin_iata"), "destination": f.get("destination_iata")}]
    attempt("bus", infra.put_bus_events, changed)
    crew = rng.choice(flights)
    attempt("sns", infra.publish_alert, f"Crew duty check {crew['SK']}",
            {"flight": crew["SK"], "tail_number": crew.get("tail_number"), "origin": crew.get("origin_iata"),
             "message": "Crew duty-time check due before next sector"})
    turn = rng.choice(flights)
    attempt("turnaround", infra.start_turnaround,
            {"flight": turn["SK"], "tail_number": turn.get("tail_number"), "origin": turn.get("origin_iata")})
    ts = time.strftime("%Y/%m/%d/%H%M%S", time.gmtime(now))
    attempt("recorder", infra.put_recorder_snapshot, f"recorder/{ts}.json",
            {"captured_at": now, "airborne": [{"flight": f["SK"], "lat": f.get("lat"), "lon": f.get("lon"),
                                                "altitude_ft": f.get("altitude_ft")} for f in airborne]})

    summary = {"flights_written": len(flights), "airborne": len(airborne), "events_enqueued": len(events),
               "status_changes": len(changed), "channel_failures": failures, "active_faults": chaos.active_count()}
    log.info("simulator run complete", extra=summary)
    return summary


def handler(event, context):
    return run()


if __name__ == "__main__" and config.LOCAL:
    print(run())
