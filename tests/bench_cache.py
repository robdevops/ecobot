"""A data folder shaped like a real station's, for scripts/bench_storage.py where the bot's own files aren't at hand: the storage redesign
(docs/storage-plan.md) is measured on it between the owner's runs of the benchmark on the real ones. Shaped like the owner's in October
2026: about 116, 391, 755 and 1457 days held at 5 minutes, 30 minutes, 4 hours and a day, 22 readings with the lows and highs Ecowitt
gives (2.4 million values), 184 days of AirGradient readings at 5 minutes and 4 days of the live readings' ranges, with ten days of
minutes (the owner's file has 9 hours: as many as the storage keeps, so the table shows whether they slow anything else). The station's
is ecobot.sqlite, written by the new storage as the migration writes it (`old=True` writes the old files instead, for the migration's
own check), the AirGradient's its old file until step 3; from the fake station's weather (tests/fakes.py), in about 30 seconds:

    python -m tests.bench_cache /tmp/bench-data
    python scripts/bench_storage.py --data /tmp/bench-data --tz Australia/Melbourne
"""

import math
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from lib.airgradient.readings import storage_rows
from lib.ecowitt.api import GROUPS, fix_units
from lib.ecowitt.fetch import spans
from lib.storage import Storage
from lib.storage.catalogue import CLOUD, KINDS
from lib.storage.write import horizon, pivot
from lib.timeutil import day_bounds, local_epoch, local_date, naive_local
from tests.fakes import (MAC, STEP, TZ, direction, gust, humidity, pressure, rain_day, temp, uv_index,
                         wind_speed)

DAYS = {"5min": 116, "30min": 391, "4hour": 755, "1day": 1457}   # days held at each resolution
AIR_DAYS = 184
AIR_LOCATION = "153523"
C = "℃"
# group -> (field, its value at a time, the unit Ecowitt gives); no rainfall_piezo: the station has none
FIELDS = {
    "outdoor": [("temperature", temp, C), ("feels_like", lambda t: temp(t) - 1, C), ("app_temp", lambda t: temp(t) - 0.5, C),
                ("dew_point", lambda t: temp(t) - 5, C), ("vpd", lambda t: 0.06 + (temp(t) - 8) / 40, "inHg"), ("humidity", humidity, "%")],
    "indoor": [("temperature", lambda t: 19 + (temp(t) - 8) / 6, C), ("humidity", lambda t: 58 - (temp(t) - 8) / 3, "%")],
    "pressure": [("relative", pressure, "hPa"), ("absolute", lambda t: pressure(t) - 9, "hPa")],
    "wind": [("wind_speed", wind_speed, "km/h"), ("wind_gust", gust, "km/h"), ("wind_direction", direction, "º")],
    "rainfall": [("rain_rate", lambda t: 1.2 if rain_day(t) > rain_day(t - 300) else 0.0, "mm/hr"), ("daily", rain_day, "mm"),
                 ("event", rain_day, "mm"), ("1_hour", lambda t: min(rain_day(t), 1.0), "mm"),
                 ("weekly", lambda t: rain_day(t) + t // 86400 % 7, "mm"), ("monthly", lambda t: rain_day(t) + t // 86400 % 30, "mm"),
                 ("yearly", lambda t: rain_day(t) + t // 86400 % 365, "mm")],
    "solar_and_uvi": [("solar", lambda t: uv_index(t) * 80, "W/m²"), ("uvi", uv_index, "")],
}


DECIMALS = {"humidity": 0, "uvi": 0, "wind_direction": 0}   # as the owner's station writes them ("94", "0", "131"); the rest with one


def history(cycle: str, start: int, end: int) -> dict:
    """What Ecowitt would answer for [start, end] at this cycle: each reading, and its low and high from the 30-minute readings up."""
    step = STEP[cycle]
    first = start - start % step if cycle == "1day" else start   # (daily readings are UTC days)
    out: dict = {}
    for group, fields in FIELDS.items():
        for name, fn, unit in fields:
            places = DECIMALS.get(name, 1)
            for ts in range(first, end + 1, step):
                samples = [fn(t) for t in range(ts, ts + step, 300 if step <= 1800 else 3600)]
                out.setdefault(group, {}).setdefault(name, {"unit": unit, "list": {}})["list"][str(ts)] = f"{samples[0]:.{places}f}"
                if cycle != "5min":
                    for suffix, value in (("_low", min(samples)), ("_high", max(samples))):
                        out[group].setdefault(name + suffix, {"unit": unit, "list": {}})["list"][str(ts)] = f"{value:.{places}f}"
    return fix_units(out)


def requests(days: dict[str, int], now: float):
    """(cycle, start, end) of each request the archive makes to hold each resolution for its days."""
    today = naive_local(int(now), TZ).date()
    for cycle, n in days.items():
        first = datetime.combine(today - timedelta(days=n), datetime.min.time())
        if cycle == "1day":   # daily readings are UTC days: start on one, so the days held don't lose an hour to a clock change in between
            first = naive_local(local_epoch(first, TZ) // 86400 * 86400, TZ)
        for a, b in spans(cycle, first, naive_local(int(now), TZ)):
            yield cycle, local_epoch(a, TZ), local_epoch(b, TZ)




def live_ranges(days: float, now: float) -> dict:
    """The lowest and highest the bot saw in each 5 minutes (from Home Assistant's readings) of the outdoor readings and the pressure:
    {series: {5-minute mark: (readings, lowest, highest)}}."""
    found: dict = {}
    for group in ("outdoor", "pressure"):
        for name, fn, _ in FIELDS[group]:
            if group == "pressure" and name != "relative":
                continue
            for mark in range(int(now - now % 300 - days * 86400), int(now), 300):
                values = [fn(mark + s) for s in (-120, -60, 0, 60, 120)]
                found.setdefault(f"{group}.{name}", {})[mark] = (5, min(values) - 0.2, max(values) + 0.2)
    return found


def minutes(hours: float, now: float) -> dict:
    """Home Assistant's minutes of the last `hours`, in the shape the history is told in."""
    first = int(now - now % 60 - hours * 3600)
    return {group: {name: {"unit": unit, "list": {str(t): f"{fn(t):.1f}" for t in range(first, int(now), 60)}} for name, fn, unit in fields}
            for group, fields in FIELDS.items()}


def station(storage: Storage, days: dict[str, int], live_days: float, minute_hours: float, now: float):
    """The station's readings into ecobot.sqlite: each resolution held for its days, the live readings' bands and the minutes, written in
    one transaction (the rows as they are, the 30-minute bands rolled up and every day worked out once)."""
    with storage.writer.transaction() as w:
        for cycle, s, e in requests(days, now):
            data, limit = history(cycle, s, e), horizon(cycle, now)
            for group in GROUPS:   # (a group the station lacks is covered too, as a fetch covers it: nothing is missing there)
                sensor = w.ecowitt_sensor(MAC, group)
                rows, extra, units = pivot(group, data.get(group, {}), s, e)
                for row in rows:
                    row["final"] = int(row["ts"] <= limit)
                w.fields(sensor, cycle, units)
                w.put(sensor, cycle, rows, CLOUD, exact=True)
                w.extra(sensor, cycle, extra)
                w.cover(sensor, cycle, s, min(e, limit))
        w.put_ranges(MAC, live_ranges(live_days, now))
        w.put_minutes(MAC, minutes(minute_hours, now), now)
        for sensor in w.all_sensors():
            if sensor.kind in KINDS:
                w.roll_up(sensor, 0, 2 ** 40)


def air_days(days: int, now: float) -> tuple[dict, list[dict]]:
    """({finished day: its 5-minute readings}, today's so far) of the AirGradient, a glitch now and then."""
    today = local_date(int(now), TZ)
    done, rest = {}, []
    for n in range(days, -1, -1):
        day = today - timedelta(days=n)
        lo, hi = (local_epoch(datetime.combine(d, datetime.min.time()), TZ) for d in (day, day + timedelta(days=1)))
        rows = []
        for ts in range(lo, min(hi, int(now) - 600), 300):
            hour = naive_local(ts, TZ).hour
            rows.append({"ts": ts, "pm2_5": 4 + hour % 12 + (40.0 if ts // 300 % 997 == 0 else 0.0) + 2 * math.sin(ts / 5000),
                         "pm10": 7 + hour % 12, "pm1": 2 + hour % 6, "co2": 420 + 3 * hour, "voc_index": 100 + hour, "nox_index": 1,
                         "air_temperature": temp(ts - ts % 300), "air_humidity": humidity(ts - ts % 300)})
        if day < today:
            done[day] = rows
        else:
            rest = rows
    return done, rest


def air_spreads(now: float, spreads_days: int = 10) -> dict:
    """The ranges Home Assistant gave for the last days."""
    marks = range(int(now - now % 300 - spreads_days * 86400), int(now) - 600, 300)
    return {name: {ts: (1.0, 30.0) for ts in marks} for name in ("pm2_5", "pm10", "pm1", "co2")}


def air(storage: Storage, days: int, now: float):
    """The AirGradient's readings into ecobot.sqlite: each finished day held, today's so far, and Home Assistant's ranges."""
    done, today = air_days(days, now)
    with storage.writer.transaction() as w:
        sensor = w.air_sensor(AIR_LOCATION)
        for day, rows in done.items():
            w.put(sensor, "5min", storage_rows(rows, True), CLOUD, exact=True)
            w.cover(sensor, "5min", *day_bounds(day, TZ, True))
        w.put(sensor, "5min", storage_rows(today, False), CLOUD, exact=True)
        w.widen(sensor, "5min", air_spreads(now))




def build(folder: Path, days: dict[str, int] = DAYS, air_days: int = AIR_DAYS, live_days: float = 4, minute_hours: float = 240,
          now: float | None = None):
    """The file the benchmark reads, ecobot.sqlite in `folder`: the station's readings and the AirGradient's."""
    now = time.time() if now is None else now
    folder.mkdir(parents=True, exist_ok=True)
    storage = Storage(folder / "ecobot.sqlite", TZ, bulk=True)
    station(storage, days, live_days, minute_hours, now)
    air(storage, air_days, now)
    storage.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    started = time.monotonic()
    build(Path(sys.argv[1]))
    print(f"Built in {time.monotonic() - started:.0f} s: " + ", ".join(
        f"{p.name} {p.stat().st_size / 1e6:.1f} MB" for p in sorted(Path(sys.argv[1]).glob("*.sqlite"))))
