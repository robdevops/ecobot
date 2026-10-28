"""A data folder shaped like a real station's, for scripts/bench_storage.py where the bot's own caches aren't at hand: the storage redesign
(docs/storage-plan.md) is measured on it between the owner's runs of the benchmark on the real ones. Shaped like the owner's in October
2026: about 116, 391, 755 and 1457 days held at 5 minutes, 30 minutes, 4 hours and a day, 22 readings with the lows and highs Ecowitt
gives (2.4 million values), 184 days of AirGradient readings at 5 minutes, 4 days of the live readings' ranges and the last 9 hours of
minutes. Written through the bot's own stores, from the fake station's weather (tests/fakes.py), in about 20 seconds:

    python -m tests.bench_cache /tmp/bench-data
    python scripts/bench_storage.py --data /tmp/bench-data --tz Australia/Melbourne
"""

import math
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from lib.airgradient.store import AirStore
from lib.ecowitt.api import CYCLES, GROUPS, UNITS, fix_units
from lib.ecowitt.fetch import spans
from lib.ecowitt.live import LiveRanges
from lib.ecowitt.minutes import MinuteStore
from lib.ecowitt.store import HistoryCache
from lib.timeutil import local_epoch, local_date, naive_local
from tests.fakes import MAC, STEP, TZ, direction, gust, humidity, pressure, rain_day, temp, uv_index, wind_speed

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
                 ("event", rain_day, "mm"), ("hourly", lambda t: min(rain_day(t), 1.0), "mm"),
                 ("weekly", lambda t: rain_day(t) + t // 86400 % 7, "mm"), ("monthly", lambda t: rain_day(t) + t // 86400 % 30, "mm"),
                 ("yearly", lambda t: rain_day(t) + t // 86400 % 365, "mm")],
    "solar_and_uvi": [("solar", lambda t: uv_index(t) * 80, "W/m²"), ("uvi", uv_index, "")],
}


def history(cycle: str, start: int, end: int) -> dict:
    """What Ecowitt would answer for [start, end] at this cycle: each reading, and its low and high from the 30-minute readings up."""
    step = STEP[cycle]
    first = start - start % step if cycle == "1day" else start   # (daily readings are UTC days)
    out: dict = {}
    for group, fields in FIELDS.items():
        for name, fn, unit in fields:
            for ts in range(first, end + 1, step):
                samples = [fn(t) for t in range(ts, ts + step, 300 if step <= 1800 else 3600)]
                out.setdefault(group, {}).setdefault(name, {"unit": unit, "list": {}})["list"][str(ts)] = f"{samples[0]:.1f}"
                if cycle != "5min":
                    for suffix, value in (("_low", min(samples)), ("_high", max(samples))):
                        out[group].setdefault(name + suffix, {"unit": unit, "list": {}})["list"][str(ts)] = f"{value:.1f}"
    return fix_units(out)


def station(path: Path, days: dict[str, int], now: float):
    """The history cache, each resolution held for its days, a request's span at a time as the archive stores them."""
    cache = HistoryCache(path, UNITS)
    today = naive_local(int(now), TZ).date()
    for cycle, n in days.items():
        first = datetime.combine(today - timedelta(days=n), datetime.min.time())
        for a, b in spans(cycle, first, naive_local(int(now), TZ)):
            s, e = local_epoch(a, TZ), local_epoch(b, TZ)
            cache.store(MAC, cycle, GROUPS, history(cycle, s, e), s, e)
    cache.close()


def live_ranges(path: Path, days: float, now: float):
    """The lowest and highest the bot saw in each 5 minutes (from Home Assistant's readings) of the outdoor readings and the pressure."""
    found: dict = {}
    for group in ("outdoor", "pressure"):
        for name, fn, _ in FIELDS[group]:
            if group == "pressure" and name != "relative":
                continue
            for mark in range(int(now - now % 300 - days * 86400), int(now), 300):
                values = [fn(mark + s) for s in (-120, -60, 0, 60, 120)]
                found.setdefault(f"{group}.{name}", {})[mark] = (5, min(values) - 0.2, max(values) + 0.2)
    live = LiveRanges(path)
    live.put(found)
    live.close()


def minutes(path: Path, hours: float, now: float):
    store = MinuteStore(path)
    first = int(now - now % 60 - hours * 3600)
    store.put({group: {name: {"unit": unit, "list": {str(t): f"{fn(t):.1f}" for t in range(first, int(now), 60)}} for name, fn, unit in fields}
               for group, fields in FIELDS.items()}, now)
    store.close()


def air(path: Path, days: int, now: float, spreads_days: int = 10):
    """The AirGradient's 5-minute readings for these days (each finished day marked fetched), a glitch now and then, and the ranges Home
    Assistant gave for the last days."""
    store = AirStore(path, AIR_LOCATION, TZ)
    today = local_date(int(now), TZ)
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
            store.save_day(day, rows)
        else:
            store.merge(rows)
    marks = range(int(now - now % 300 - spreads_days * 86400), int(now) - 600, 300)
    store.add_spreads({name: {ts: (1.0, 30.0) for ts in marks} for name in ("pm2_5", "pm10", "pm1", "co2")})
    store.close()


def build(folder: Path, days: dict[str, int] = DAYS, air_days: int = AIR_DAYS, live_days: float = 4, minute_hours: float = 9,
          now: float | None = None):
    """The four files the benchmark reads, in `folder`."""
    now = time.time() if now is None else now
    folder.mkdir(parents=True, exist_ok=True)
    station(folder / "ecowitt_cache.sqlite", days, now)
    live_ranges(folder / "ecowitt_live.sqlite", live_days, now)
    minutes(folder / "ecowitt_minutes.sqlite", minute_hours, now)
    air(folder / "airgradient_cache.sqlite", air_days, now)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    started = time.monotonic()
    build(Path(sys.argv[1]))
    print(f"Built in {time.monotonic() - started:.0f} s: " + ", ".join(
        f"{p.name} {p.stat().st_size / 1e6:.1f} MB" for p in sorted(Path(sys.argv[1]).glob("*.sqlite"))))
