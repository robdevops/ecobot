"""The air monitor's readings as Home Assistant saw them: the readings of the times the bot did not read the monitor itself, their
lowest and highest of each 5 minutes (the bands), its states at the end of each minute (the minutes), and its states now (the live
reading while the monitor does not answer).

AirGradient's cloud keeps one average per 5 minutes and the monitor's own server only the reading of the moment. Home Assistant's
AirGradient integration reads the monitor every minute and keeps each change (10 days unless its recorder is set otherwise), so it holds
the time before the bot was reading the monitor, or while it was down, which is read back from it. Its entities are found by the names
the integration gives them: sensor.<device>_voc_index, sensor.<device>_pm2_5 ...

A metric of Home Assistant's is taken only where its average over the five minutes agrees with the reading the bot holds for them (the
source ranked above it): Home Assistant may show another variant of a reading (the monitor's raw PM2.5 where the bot keeps the corrected
one), whose range would not fit the line and whose readings would put a step in it. A metric that disagrees in most of the five minutes
compared is that: none of its ranges are kept and it fills no gaps. The same rule judges the cloud's readings against the bot's (verdicts).
"""

import math
from datetime import UTC, datetime

from ..ecowitt.hass_history import rollup
from ..ecowitt.hass_map import convert
from ..ecowitt.homeassistant import parse_time
from .metrics import STORED

BUCKET = 300
# The bot's metric -> the end of its entity's name in Home Assistant (sensor.<device>_<suffix>) and what it measures, for its unit
ENTITIES = {"pm1": ("pm1", ""), "pm2_5": ("pm2_5", ""), "pm10": ("pm10", ""), "co2": ("carbon_dioxide", ""),
            "voc_index": ("voc_index", ""), "nox_index": ("nox_index", ""),
            "air_temperature": ("temperature", "temperature"), "air_humidity": ("humidity", "")}
ANCHORS = ("voc_index", "nox_index")   # the names that find the sensor (the other integrations' sensors are not called so)
# How far Home Assistant's average over five minutes may be from the bot's reading for them and still be the same reading: this much,
# or REL of the reading, whichever is more
ABS = {"pm1": 2.0, "pm2_5": 2.0, "pm10": 2.0, "co2": 20.0, "voc_index": 8.0, "nox_index": 1.0, "air_temperature": 0.5, "air_humidity": 3.0}
REL = 0.15
MATCH = 0.8          # the share of the five minutes compared that must agree for a metric's ranges to be kept ...
MIN_COMPARED = 12    # ... once this many were compared (fewer: each five minutes is judged on its own)


def find_prefix(states: list[dict], current: dict | None = None) -> tuple[str | None, int]:
    """(the start of the air sensor's entity names, "sensor.open_air_"; None when Home Assistant has none, how many sensors it has).
    With several, the one whose VOC (or NOx) index is now nearest the bot's own reading."""
    found: dict[str, dict] = {}
    for s in states:
        entity = str(s.get("entity_id", "")) if isinstance(s, dict) else ""
        for anchor in ANCHORS:
            if entity.startswith("sensor.") and entity.endswith("_" + anchor):
                found.setdefault(entity[: -len(anchor)], {})[anchor] = s.get("state")
    if not found:
        return None, 0

    def distance(prefix: str) -> float:
        for anchor in ANCHORS:
            try:
                return abs(float(found[prefix][anchor]) - float((current or {})[anchor]))
            except (KeyError, TypeError, ValueError):
                continue
        return math.inf
    return min(sorted(found), key=distance), len(found)


def entity_ids(prefix: str, states: list[dict]) -> dict[str, str]:
    """{entity id: the bot's metric} for the metrics Home Assistant has an entity for."""
    have = {s.get("entity_id") for s in states if isinstance(s, dict)}
    return {prefix + suffix: name for name, (suffix, _) in ENTITIES.items() if prefix + suffix in have}


def ranges(history: dict[str, list[tuple[float, float]]], ids: dict[str, str], units: dict[str, str], start: float, end: float,
           now: float) -> dict[str, dict[int, tuple[float, float, float]]]:
    """{metric: {5-minute bucket start: (mean, lowest, highest)}} of each bucket from the one holding `start` to the one holding `end`
    that Home Assistant had a value in all through (the last, still going at `now`, is left for the next time)."""
    out = {}
    for entity, states in history.items():
        name = ids[entity]
        kind = ENTITIES[name][1]
        converted = [(t, convert(v, kind, units.get(entity))) for t, v in states]
        found = rollup(converted, "mean", start, end, BUCKET, now)
        out[name] = {b: r for b, r in found.items() if b + BUCKET <= now}
    return out


def minutes(history: dict[str, list[tuple[float, float]]], ids: dict[str, str], units: dict[str, str], start: float, end: float,
            now: float) -> list[dict]:
    """The state held at the end of each minute from `start` to `end` as readings ([{"ts", metric...}], oldest first), of the minutes
    that are over at `now`."""
    rows: dict[int, dict] = {}
    for entity, states in history.items():
        name = ids[entity]
        converted = [(t, convert(v, ENTITIES[name][1], units.get(entity))) for t, v in states]
        for minute, (value, _, _) in rollup(converted, "last", start, end, 60, now).items():
            if minute + 60 <= now:
                rows.setdefault(minute, {"ts": minute})[name] = value
    return [rows[t] for t in sorted(rows)]


def live(states: list[dict], ids: dict[str, str], now: float) -> dict | None:
    """The monitor's reading now from Home Assistant's states, in the cloud's shape (the fields metrics.STORED reads first, stamped with
    the newest state's time, else `now`); None when it has none of the metrics."""
    row, newest = {}, 0.0
    for s in states:
        if not isinstance(s, dict) or (name := ids.get(str(s.get("entity_id", "")))) is None:
            continue
        try:
            row[STORED[name][0][0]] = float(s.get("state"))
        except (TypeError, ValueError):
            continue
        if when := s.get("last_updated") or s.get("last_changed"):
            newest = max(newest, parse_time(when))
    if not row:
        return None
    return {**row, "timestamp": datetime.fromtimestamp(newest or now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}


def agrees(name: str, mean: float, value: float) -> bool:
    return abs(mean - value) <= max(ABS[name], REL * abs(value))


def verdicts(found: dict[str, dict[int, float]], rows: list[dict]) -> dict[str, tuple[int, int]]:
    """{metric: (five minutes compared, how many agreed)}: a source's five-minute values ({metric: {bucket start: value}}) against the
    readings the bot holds for the same five minutes (`rows`), the buckets that hold both."""
    readings = {r["ts"]: r for r in rows}
    out = {}
    for name, buckets in found.items():
        compared = agreed = 0
        for b, value in buckets.items():
            row = readings.get(b)
            if row is not None and name in row:
                compared += 1
                agreed += agrees(name, value, row[name])
        out[name] = (compared, agreed)
    return out


def same_reading(verdict: tuple[int, int]) -> bool:
    """Does a metric's verdict say the source gives the reading the bot keeps? It does while too few five minutes were compared to say."""
    compared, agreed = verdict
    return compared < MIN_COMPARED or agreed >= MATCH * compared


def checked(found: dict[str, dict[int, tuple[float, float, float]]], rows: list[dict]) -> tuple[dict[str, dict[int, tuple[float, float]]],
                                                                                             dict[str, tuple[int, int]]]:
    """(the ranges to keep, the verdicts): the ranges of the buckets that hold a reading of the metric (a bucket without one has no line
    to shade) and whose mean agrees with it, of the metrics that agree in most of them. A bucket the bot recorded itself has its own range
    already: it only counts in the comparison."""
    readings = {r["ts"]: r for r in rows}
    verdict = verdicts({name: {b: mean for b, (mean, _, _) in buckets.items()} for name, buckets in found.items()}, rows)
    keep = {}
    for name, buckets in found.items():
        if same_reading(verdict[name]):
            keep[name] = {b: (lo, hi) for b, (mean, lo, hi) in buckets.items()
                          if (row := readings.get(b)) is not None and name in row and agrees(name, mean, row[name]) and name not in row.get("_range", {})}
    return keep, verdict


def gaps(found: dict[str, dict[int, tuple[float, float, float]]], rows: list[dict], verdict: dict[str, tuple[int, int]]) -> list[dict]:
    """The readings to fill the bot's gaps with: each five minutes' mean, with its lowest and highest as the band, of the metrics the
    verdicts say are the readings the bot keeps, where the bot holds no reading of that metric."""
    readings = {r["ts"]: r for r in rows}
    out: dict[int, dict] = {}
    for name, buckets in found.items():
        if not same_reading(verdict.get(name, (0, 0))):
            continue
        for b, (mean, lo, hi) in buckets.items():
            if name not in readings.get(b, {}):
                row = out.setdefault(b, {"ts": b, "_range": {}})
                row[name], row["_range"][name] = mean, (lo, hi)
    return [out[b] for b in sorted(out)]


__all__ = ["ENTITIES", "checked", "entity_ids", "find_prefix", "gaps", "live", "minutes", "ranges", "same_reading", "verdicts"]
