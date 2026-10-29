"""A question's period as days, from the day rows (lib/storage/days.py) rather than its readings: each local day the period holds whole as
its day row has it, and the part of a day at either end worked out from the finest readings held for that part, as a day row is. A history
answer's records, its rows by day or by month and its averages are folded from these. No record is looked for again in finer
readings: a day row's times already come from the finest readings its day has (the archive keeps every resolution Ecowitt has).
"""

from datetime import date, timedelta
from typing import NamedTuple

from ..storage.catalogue import BY_NAME, KINDS, Column, field_of, text
from ..storage.days import utc_days
from ..storage.read import DAY_PARTS, Days, Reader
from ..timeutil import day_bounds, local_date
from .api import MINUTE
from .extremes import Ext

FINE = (MINUTE, "5min")          # readings, not averages: a figure from them is a true one
PARTS_FROM = ("5min", "30min")   # what a part of a day is read from (the minutes first, where the question was read from them)


class Day(NamedTuple):
    """One reading's figures for a day of the period, or for the part of it the period holds."""
    day: date
    source: str           # the resolution they come from ("1day": Ecowitt's daily record, 10am to 10am)
    timing: str           # ... and the times of the lowest and highest, as describe_time words them ("4hour": "sometime between")
    mean: float | None
    lo: float | None
    lo_ts: int | None
    hi: float | None
    hi_ts: int | None


class Period(NamedTuple):
    """One reading's days in the period, oldest first, and its lowest and highest over them (None where it has none)."""
    column: Column
    days: list[Day]
    low: Ext | None
    high: Ext | None


def _picks(days: list[Day]) -> dict[str, Day]:
    """{"low", "high": the day} of the lowest and the highest (the first, where several days are as low or as high)."""
    out = {}
    if low := min((d for d in days if d.lo is not None), key=lambda d: d.lo, default=None):
        out["low"] = low
    if high := max((d for d in days if d.hi is not None), key=lambda d: d.hi, default=None):
        out["high"] = high
    return out


def _ext(column: Column, want: str, day: Day, exact: bool = True) -> Ext:
    value, ts = (day.lo, day.lo_ts) if want == "low" else (day.hi, day.hi_ts)
    return Ext(value, text(value, column.decimals, column.whole), ts, day.timing, exact)


def extremes(column: Column, days: list[Day]) -> dict[str, Ext]:
    """{"low", "high": Ext} of these days, as true figures (whether one is an average is period_on's to say, for the period's own)."""
    return {want: _ext(column, want, day) for want, day in _picks(days).items()}


def period_on(r: Reader, mac: str, groups: list[str], start: int, end: int, reaches_now: bool, minutes: bool) -> dict[str, Period]:
    """{"group.field": its Period} for [start, end] (epochs). A day is whole when the period holds all of it, or when it is the last and the
    period reaches now (today's row holds its readings so far). A part of a day is read at the finest resolution held for all of it (the
    minutes first, when the question was read from them), else the finest that has readings; a part of a day with nothing finer than
    Ecowitt's daily record takes the day's row. One query per kind for the days, one (or a few, where a part has none) for the parts."""
    tz = r.storage.tz
    first, last = local_date(start, tz), local_date(end, tz)
    whole, parts = [], {}
    for n in range((last - first).days + 1):
        d = first + timedelta(days=n)
        a, b = day_bounds(d, tz, True)
        if a < start or (b > end and not (d == last and reaches_now)):
            parts[d] = (max(a, start), min(b, end))
        else:
            whole.append(d)
    sensors = {g: s for g in groups if (s := r.sensor(mac, g)) is not None and s.kind in KINDS}
    out = {}
    for kind in dict.fromkeys(s.kind for s in sensors.values()):
        ids = [s.id for s in sensors.values() if s.kind == kind]
        rows = {s: _rows(got) for s, got in r.days(kind, ids, whole[0], whole[-1]).items()} if whole else {}
        found = _parts(r, kind, ids, parts, minutes)
        if lacking := sorted({d for d in parts for s in ids if d not in found.get(s, {})}):   # a part with no readings of its own
            for s, got in r.days(kind, ids, lacking[0], lacking[-1]).items():
                found.setdefault(s, {}).update({d: row for d, row in _rows(got).items()
                                                if d in parts and d not in found.get(s, {}) and row[0] == "1day"})
        for group, sensor in sensors.items():
            if sensor.kind != kind:
                continue
            days = {d: row for d, row in rows.get(sensor.id, {}).items() if d not in parts}
            days.update(found.get(sensor.id, {}))
            for column in BY_NAME[kind].values():
                held = [Day(d, source, timing, *figures[column.name]) for d, (source, timing, figures) in sorted(days.items())
                        if column.name in figures and any(v is not None for v in figures[column.name])]
                if held:
                    ext = {want: _ext(column, want, d, _exact(r, kind, sensor.id, column, want, d)) for want, d in _picks(held).items()}
                    out[f"{group}.{field_of(group, column.name)}"] = Period(column, held, ext.get("low"), ext.get("high"))
    return out


def _rows(days: Days) -> dict[date, tuple[str, str, dict[str, tuple]]]:
    """{day: (source, timing, {column: its figures})} of a sensor's Days."""
    return {d: (days.source[i], days.timing[i], {c: tuple(getattr(days, a)[c][i] for a in DAY_PARTS) for c in days.mean})
            for i, d in enumerate(days.day)}


def _parts(r: Reader, kind: str, sensors: list[int], parts: dict[date, tuple[int, int]], minutes: bool) -> dict[int, dict]:
    """{sensor: {day: row}} of the parts of days: each at the first of its resolutions that has readings for it, those held for all of it
    first."""
    pending = {}
    for sensor in sensors:
        for d, (a, b) in parts.items():
            held = [res for res in PARTS_FROM if r.held(sensor, res, a, b)]
            pending[sensor, d] = ([MINUTE] if minutes else []) + held + [res for res in PARTS_FROM if res not in held]
    out: dict[int, dict] = {}
    while pending:
        got = r.part_days(kind, [(s, d, *parts[d], todo[0]) for (s, d), todo in pending.items()])
        for s, days in got.items():
            out.setdefault(s, {}).update(_rows(days))
        pending = {(s, d): todo[1:] for (s, d), todo in pending.items() if d not in out.get(s, {}) and len(todo) > 1}
    return out


def _exact(r: Reader, kind: str, sensor: int, column: Column, want: str, day: Day) -> bool:
    """Is a day's lowest or highest a true one: from readings (5-minute ones, minutes), a counter's or a gust's highest (a coarse reading's
    is its own), or a coarse reading's own lowest or highest (Ecowitt's, or the band its finer readings gave it), not an average."""
    if day.source in FINE or column.mode == "last" or column.mode == "max" and want == "high":
        return True
    ts = day.lo_ts if want == "low" else day.hi_ts
    window = utc_days(day.day, r.storage.tz) if day.source == "1day" else (ts, ts)
    return window is not None and r.banded(kind, sensor, column.name, "lo" if want == "low" else "hi", day.source, *window)


__all__ = ["Day", "Period", "extremes", "period_on"]
