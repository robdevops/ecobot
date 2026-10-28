"""The air sensor's readings as Home Assistant saw them, for the lowest and highest of each 5 minutes (the store's spreads).

AirGradient's cloud keeps one average per 5 minutes and the sensor's own server only the reading of the moment, so the bot can shade a
chart's range only for the time it read the sensor itself. Home Assistant's AirGradient integration reads the sensor every minute and keeps
each change (10 days unless its recorder is set otherwise), so the ranges of the time before the bot was reading it, or while it was down,
are read back from it. Its entities are found by the names the integration gives them: sensor.<device>_voc_index, sensor.<device>_pm2_5 ...

A range is kept only where Home Assistant's average over the five minutes agrees with the reading the bot holds for them: Home Assistant
may show another variant of a reading (the sensor's raw PM2.5 where the bot keeps the corrected one), whose range would not fit the line.
A metric that disagrees in most of the five minutes compared is that, and none of its ranges are kept.
"""

import math

from ..ecowitt.hass_history import rollup
from ..ecowitt.hass_map import convert

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


def agrees(name: str, mean: float, value: float) -> bool:
    return abs(mean - value) <= max(ABS[name], REL * abs(value))


def checked(found: dict[str, dict[int, tuple[float, float, float]]], rows: list[dict]) -> tuple[dict[str, dict[int, tuple[float, float]]],
                                                                                             dict[str, tuple[int, int]]]:
    """(the ranges to keep, {metric: (five minutes compared, how many agreed)}): those of the buckets that hold a reading of the metric
    (a bucket without one has no line to shade) and whose mean agrees with it, of the metrics that agree in most of them. A bucket the
    bot recorded itself has its own range already: it only counts in the comparison."""
    readings = {r["ts"]: r for r in rows}
    keep, verdict = {}, {}
    for name, buckets in found.items():
        compared = agreed = 0
        mine = {}
        for b, (mean, lo, hi) in buckets.items():
            row = readings.get(b)
            if row is None or name not in row:
                continue
            compared += 1
            if agrees(name, mean, row[name]):
                agreed += 1
                if name not in row.get("_range", {}):
                    mine[b] = (lo, hi)
        verdict[name] = (compared, agreed)
        if compared < MIN_COMPARED or agreed >= MATCH * compared:
            keep[name] = mine
    return keep, verdict


__all__ = ["ENTITIES", "checked", "entity_ids", "find_prefix", "ranges"]
