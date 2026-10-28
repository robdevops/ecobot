"""Home Assistant's recorded states told as Ecowitt's history: the same buckets, fields and strings the cloud's /device/history answers with.

Home Assistant keeps a sensor's every change (a state is recorded only when it differs from the one before: the gateway reports about once a
minute, so a slow reading like the temperature changes a few times an hour). A cycle's bucket is told from the states in it and the one held as
it began:
  - the value is the mean over the time (rain rate, temperature ...), the highest (gusts) or the last (a rain counter, the compass point);
  - the 5-minute and longer cycles also give field_low and field_high, the lowest and highest state in the bucket (Ecowitt's own 5-minute
    cycle gives none, its coarser ones do), so a chart's shaded range and a day's true extremes are the readings', not an average's;
  - "1min" (Ecowitt has none) is the state held at the end of each minute.
Buckets are cut on the clock like the cloud's: 5 and 30 minutes and the minute on the epoch, the 4-hour and daily ones on UTC hours and days.
"""

from bisect import bisect_right
from dataclasses import dataclass

from .hass_map import ENTITIES, NO_RANGE, Entity, convert, written

SECONDS = {"1min": 60, "5min": 300, "30min": 1800, "4hour": 14400, "1day": 86400}


def rollup(states: list[tuple[float, float]], mode: str, start: float, end: float, width: int, until: float) -> dict[int, tuple[float, float, float]]:
    """{bucket start: (value, lowest, highest)} for each bucket of `width` seconds from the one holding `start` to the one holding `end`,
    told from `states` ([(when, value)], oldest first; a value holds until the next). A bucket the reading had no value in at all is
    left out, and one still going at `until` (now) is told only as far as it has gone."""
    out: dict[int, tuple[float, float, float]] = {}
    times = [t for t, _ in states]
    for b in range(int(start // width) * width, int(end // width) * width + 1, width):
        b_end = min(b + width, until)
        if b_end <= b:
            break
        i = bisect_right(times, b) - 1                      # the state held as the bucket began
        value = states[i][1] if i >= 0 else None
        at, area, covered = b, 0.0, 0.0
        low = high = value
        j = i + 1
        while j < len(states) and times[j] < b_end:
            t, v = states[j]
            if value is not None:
                area, covered = area + value * (t - at), covered + (t - at)
            at, value = t, v
            low, high = (v if low is None else min(low, v)), (v if high is None else max(high, v))
            j += 1
        if low is None:
            continue
        if value is not None and b_end > at:
            area, covered = area + value * (b_end - at), covered + (b_end - at)
        told = (area / covered if covered else value) if mode == "mean" else high if mode == "max" else value
        out[b] = (told, low, high)
    return out


@dataclass(frozen=True)
class Series:
    entity: Entity
    states: list[tuple[float, float]]   # in the bot's units


def history(series: list[Series], cycle: str, start: float, end: float, until: float) -> dict:
    """{group: {field: {"unit", "list": {epoch: "value"}}}} between two epochs for these series, in the shape of Ecowitt's history."""
    width = SECONDS[cycle]
    out: dict = {}
    for s in series:
        e = s.entity
        if not s.states:
            continue
        # a minute is the state it ends on, whatever the reading's usual way of being told
        found = rollup(s.states, "last" if cycle == "1min" else e.mode, start, end, width, until)
        if not found:
            continue
        fields = out.setdefault(e.group, {})
        fields[e.field] = {"unit": e.unit, "list": {str(b): written(v, e.decimals) for b, (v, _, _) in found.items()}}
        if cycle != "1min" and e.series not in NO_RANGE:
            for suffix, pick in (("_low", 1), ("_high", 2)):
                fields[e.field + suffix] = {"unit": e.unit, "list": {str(b): written(r[pick], e.decimals) for b, r in found.items()}}
    return out


def converted(states: list[tuple[float, float]], entity: Entity, unit: str | None) -> list[tuple[float, float]]:
    """Home Assistant's numbers for this entity in the bot's units."""
    return [(t, convert(v, entity.kind, unit, entity.scale)) for t, v in states]


def merged(into: dict, more: dict, instead: frozenset[int] = frozenset()) -> dict:
    """`into` with the readings of `more` added, in place: those of buckets it lacks, and those of the buckets in `instead`, which `more`
    tells in its place (a bucket `into` knows only part of); what `into` has of the rest is kept."""
    for group, fields in more.items():
        for name, obj in fields.items():
            have = into.setdefault(group, {}).setdefault(name, {"unit": obj.get("unit", ""), "list": {}})["list"]
            for ts, value in obj["list"].items():
                if int(ts) in instead:
                    have[ts] = value
                else:
                    have.setdefault(ts, value)
    return into


def present(data: dict, groups: list[str]) -> dict[str, set[int]]:
    """{group: the bucket starts it has any reading for}."""
    return {g: {int(ts) for obj in (data.get(g) or {}).values() if isinstance(obj, dict) for ts in obj.get("list", {})} for g in groups}


__all__ = ["ENTITIES", "SECONDS", "Series", "converted", "history", "merged", "present", "rollup"]
