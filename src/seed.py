"""Seeds the DynamoDB table. Runs as a CloudFormation custom resource on stack
create (change the SeedVersion parameter to reseed), or directly from
scripts/local_server.py in local mode.

Custom resource property AdminPassword (optional) replaces the published demo
password for the admin user — set it whenever the site is reachable by others.
"""
import logging
import time

import auth
import cfn
import flightsim
import infra
import telemetry
from seed_data import AIRCRAFT, AIRPORTS, USERS
from simulator import _ops_event
from store import get_store

telemetry.setup_logging()
log = logging.getLogger("avionpulse.seed")


def seed_all(admin_password=None):
    store = get_store()
    items = []
    for iata, icao, name, city, country, lat, lon, tz, elev, rwy in AIRPORTS:
        items.append({"PK": "AIRPORT", "SK": iata, "iata_code": iata, "icao_code": icao, "name": name,
                      "city": city, "country": country, "lat": lat, "lon": lon, "timezone": tz,
                      "elevation_ft": elev, "runways": rwy})
    for tail, model, mfr, cap, rng, status, engine, year, hours in AIRCRAFT:
        items.append({"PK": "AIRCRAFT", "SK": tail, "tail_number": tail, "model": model, "manufacturer": mfr,
                      "capacity": cap, "range_nm": rng, "status": status, "engine_type": engine,
                      "year_manufactured": year, "flight_hours": hours})
    for username, password, role, full_name in USERS:
        if username == "admin" and admin_password:
            password = admin_password
        items.append({"PK": "USER", "SK": username, "username": username, "role": role,
                      "full_name": full_name, "password_hash": auth.hash_password(password), "is_active": True})
    items.extend(flightsim.all_flights(time.time()))
    if not store.get("CONFIG", "SECRET"):
        items.append({"PK": "CONFIG", "SK": "SECRET", "value": auth.new_secret()})
    if not store.get("CONFIG", "FAULTS"):
        items.append({"PK": "CONFIG", "SK": "FAULTS", "faults": {}})

    store.put_many(items)
    import random
    rng = random.Random(7)
    for _ in range(6):
        infra.send_event(_ops_event(rng))
    log.info("Seeded %d items", len(items))
    return len(items)


def _seed(props):
    n = seed_all(admin_password=props.get("AdminPassword") or None)
    return f"seeded {n} items", {"Items": str(n)}


def handler(event, context):
    cfn.run(event, context, _seed, physical_id="avionpulse-seed")
