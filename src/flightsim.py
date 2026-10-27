"""Deterministic flight lifecycle model.

Each flight repeats a cycle: scheduled -> boarding -> departed -> en_route ->
landed. Where a flight is in its cycle depends only on the wall clock, so the
simulator Lambda can recompute every flight from scratch on each run with no
state of its own. Delays, cancellations and load factor are drawn per cycle
from a seeded RNG, so they change from one rotation to the next.
"""
import math
import random
import zlib
from datetime import datetime, timezone

from seed_data import AIRCRAFT, AIRPORTS, CANCEL_REASONS, DELAY_REASONS, FLIGHTS

CRUISE_KTS = 470
SCHEDULED_MIN, BOARDING_MIN, DEPARTED_MIN, LANDED_MIN = 60, 20, 10, 40

AIRPORT_BY_IATA = {a[0]: a for a in AIRPORTS}
AIRCRAFT_BY_TAIL = {a[0]: a for a in AIRCRAFT}


def _rng(*parts):
    return random.Random(zlib.crc32("|".join(map(str, parts)).encode()))


def haversine_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.asin(math.sqrt(a))


def great_circle_point(lat1, lon1, lat2, lon2, frac):
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = 2 * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2
                                + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))
    if d == 0:
        return lat1, lon1
    a = math.sin((1 - frac) * d) / math.sin(d)
    b = math.sin(frac * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def flight_state(index, fdef, now):
    number, origin, dest, tail, cruise_alt = fdef
    o, d = AIRPORT_BY_IATA[origin], AIRPORT_BY_IATA[dest]
    ac = AIRCRAFT_BY_TAIL[tail]
    dist = haversine_nm(o[5], o[6], d[5], d[6])
    block = int(dist / CRUISE_KTS * 60) + 20                     # airborne minutes
    period = SCHEDULED_MIN + BOARDING_MIN + DEPARTED_MIN + block + LANDED_MIN
    offset = zlib.crc32(number.encode()) % period               # spread flights out

    t_min = now / 60 + offset
    cycle = int(t_min // period)
    phase = t_min - cycle * period
    cycle_start = (cycle * period - offset) * 60
    dep_epoch = cycle_start + (SCHEDULED_MIN + BOARDING_MIN) * 60
    arr_epoch = dep_epoch + (DEPARTED_MIN + block) * 60

    r = _rng(number, cycle)
    cancelled = r.random() < 0.04
    delay = r.choice([15, 25, 35, 45, 60, 90, 120]) if r.random() < 0.18 else 0
    load = r.uniform(0.62, 0.97)
    gate = f"{r.choice('ABCDEFGH')}{r.randint(1, 40):02d}"

    status, lat, lon, alt, spd, fuel, progress, notes = "scheduled", None, None, None, None, None, 0.0, None
    t_air = SCHEDULED_MIN + BOARDING_MIN + DEPARTED_MIN
    if cancelled:
        status, notes = "cancelled", r.choice(CANCEL_REASONS)
    elif phase < SCHEDULED_MIN + BOARDING_MIN:
        if delay and phase >= 40:
            status, notes = "delayed", r.choice(DELAY_REASONS)
        elif phase >= SCHEDULED_MIN:
            status = "boarding"
    elif phase < t_air:
        status = "departed"
        climb = (phase - SCHEDULED_MIN - BOARDING_MIN) / DEPARTED_MIN
        lat, lon = great_circle_point(o[5], o[6], d[5], d[6], 0.01)
        alt, spd, fuel, progress = int(4000 + climb * 14000), int(250 + climb * 70), 98.0, 1.0
    elif phase < t_air + block:
        status = "en_route"
        frac = (phase - t_air) / block
        lat, lon = great_circle_point(o[5], o[6], d[5], d[6], frac)
        if frac < 0.05:
            alt = int(18000 + (cruise_alt - 18000) * frac / 0.05)
        elif frac > 0.92:
            alt = int(cruise_alt * (1 - frac) / 0.08)
        else:
            alt = cruise_alt
        spd = int(CRUISE_KTS + r.randint(-25, 25))
        fuel = round(98 - frac * 82, 1)
        progress = round(frac * 100, 1)
    else:
        status, lat, lon, progress = "landed", d[5], d[6], 100.0

    return {
        "PK": "FLIGHT",
        "SK": number,
        "id": index + 1,
        "flight_number": number,
        "origin_iata": origin,
        "destination_iata": dest,
        "tail_number": tail,
        "aircraft_model": ac[1],
        "status": status,
        "departure_time": _iso(dep_epoch),
        "arrival_time": None if cancelled else _iso(arr_epoch + delay * 60),
        "gate": gate,
        "altitude_ft": alt,
        "speed_kts": spd,
        "lat": round(lat, 4) if lat is not None else None,
        "lon": round(lon, 4) if lon is not None else None,
        "fuel_remaining_pct": fuel,
        "delay_minutes": 0 if cancelled else delay,
        "pax": 0 if cancelled else int(ac[3] * load),
        "distance_nm": int(dist),
        "progress_pct": progress,
        "notes": notes,
        "updated_at": _iso(now),
        "updated_epoch": int(now),
    }


def all_flights(now):
    return [flight_state(i, f, now) for i, f in enumerate(FLIGHTS)]
