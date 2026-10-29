"""What a history answer reads from the storage, once what it needs is held (fetched first where the storage lacked it): in one read on
the reader's thread, the period's days (period.py) and the series it has, what its chart draws (the readings themselves for a line short
enough to draw them, else the line bucketed by the database; a line of days is drawn from the period's days), the wind's readings for
counting its direction and the rain counter for its bars.

A reading is a lines.Sample (its value, its band where it has one, the seconds it covers), written as the station writes it (the
catalogue's decimals: a 5-minute mean of the bot's live readings is drawn as the station would have sent it).
"""

from dataclasses import dataclass, field

from ..lines import Plotted, Sample
from ..storage.catalogue import BY_NAME, KINDS, field_of
from ..storage.read import Reader
from ..storage.schema import some_value
from .api import cycle_seconds
from .period import Period, period_on

type Store = dict[str, dict]   # "group.field" -> {"unit": str, "pts": {epoch: Sample}}


@dataclass
class Held:
    """A history answer's reads."""
    store: Store                                                # the period's readings at the resolutions planned, of what needs them
    period: dict[str, Period]                                   # its days, and each reading's records over them
    units: dict[str, str] = field(default_factory=dict)         # "group.field" -> its unit: each series the period has
    lines: dict[str, Plotted] = field(default_factory=dict)     # the lines the database bucketed
    wind: Store = field(default_factory=dict)                   # the wind's 5-minute readings of the days its direction is counted from
    rain: dict[int, float] = field(default_factory=dict)        # the rain counter's settled 30-minute readings ...
    rain_daily: dict[int, float] = field(default_factory=dict)  # ... and its daily records (a day before and after the period too)


def readings_on(r: Reader, mac: str, groups: list[str], cycle: str, start: int, end: int, into: Store, columns: list[str] | None = None,
                units: dict[str, dict[str, str]] | None = None):
    """The groups' readings (these columns of them, else all) at this resolution from start to end (the newest, not settled yet, too) into
    `into`, a reading's first unit kept ({group: {field: unit}}, else read here): one query per group."""
    seconds = cycle_seconds(cycle)
    for group in groups:
        sensor = r.sensor(mac, group)
        if sensor is None or sensor.kind not in KINDS:
            continue
        got = r.rows(sensor.kind, [sensor.id], cycle, start, end, columns).get(sensor.id)
        if got is None:
            continue
        known = units[group] if units is not None else r.fields(sensor.id, cycle)
        for column, values in got.values.items():
            c, name = BY_NAME[sensor.kind][column], field_of(group, column)
            written = lambda v: None if v is None else round(v, c.decimals) + 0.0   # (as text() writes it: no "-0.0")
            pts = {t: Sample(t, written(v), written(lo), written(hi), seconds)
                   for t, v, lo, hi in zip(got.ts, values, got.lo[column], got.hi[column]) if v is not None}
            if pts:
                into.setdefault(f"{group}.{name}", {"unit": known.get(name, ""), "pts": {}})["pts"].update(pts)


def first_minute_on(r: Reader, mac: str, groups: list[str], start: int, end: int) -> int | None:
    """When the first of the groups' minutes (Home Assistant's) from start to end is, None for none: one query per kind."""
    sensors = [s for g in groups if (s := r.sensor(mac, g)) is not None and s.kind in KINDS]
    found = [r.db.execute(f"SELECT MIN(ts) FROM {kind} WHERE sensor IN ({', '.join('?' * len(ids))}) AND res = '1min' AND ts BETWEEN ? AND ? "
                          f"AND {some_value([c.name for c in KINDS[kind]])}", (*ids, start, end)).fetchone()[0]
             for kind in dict.fromkeys(s.kind for s in sensors) for ids in [[s.id for s in sensors if s.kind == kind]]]
    return min((t for t in found if t is not None), default=None)


def counts_on(r: Reader, mac: str, groups: list[str], start: int, end: int) -> dict[str, int]:
    """{resolution: the readings held of the groups from start to end}, for a question that found none."""
    out = {}
    for res in ("5min", "30min", "1day"):
        out[res] = 0
        for group in groups:
            if (s := r.sensor(mac, group)) is not None and s.kind in KINDS:
                out[res] += r.db.execute(f"SELECT COUNT(*) FROM {s.kind} WHERE sensor = ? AND res = ? AND ts BETWEEN ? AND ? AND "
                                         f"{some_value([c.name for c in KINDS[s.kind]])}", (s.id, res, start, end)).fetchone()[0]
    return out


def _settled(r: Reader, mac: str, group: str, column: str, res: str, start: int, end: int) -> dict[int, float]:
    """{epoch: value} of a reading's settled rows at this resolution."""
    sensor = r.sensor(mac, group)
    if sensor is None or sensor.kind not in KINDS:
        return {}
    got = r.rows(sensor.kind, [sensor.id], res, start, end, [column]).get(sensor.id)
    return {} if got is None else {t: v for t, v, final in zip(got.ts, got.values[column], got.final) if v is not None and final}


def buckets_on(r: Reader, mac: str, groups: list[str], res: tuple[str, ...], start: int, end: int, width: int) -> dict[str, Plotted]:
    """{"group.field": its line}: the groups' readings at these resolutions from start to end in buckets of `width` seconds, each drawn at
    its middle, as lines.bucketed draws them (the database works them out: Reader.buckets); a line of fewer than two is none. One query
    per kind."""
    sensors = {g: s for g in groups if (s := r.sensor(mac, g)) is not None and s.kind in KINDS}
    out = {}
    for kind in dict.fromkeys(s.kind for s in sensors.values()):
        got = r.buckets(kind, [s.id for s in sensors.values() if s.kind == kind], res, start, end, width)
        for group, sensor in sensors.items():
            if (b := got.get(sensor.id)) is None or sensor.kind != kind:
                continue
            for column, mean in b.mean.items():
                x = [t + width // 2 for t, v in zip(b.ts, mean) if v is not None]
                if len(x) >= 2:
                    keep = [v is not None for v in mean]
                    pick = lambda values: [v for v, k in zip(values, keep) if k]
                    out[f"{group}.{field_of(group, column)}"] = Plotted(x, pick(mean), width, False, pick(b.lo[column]), pick(b.hi[column]))
    return out


def history_on(r: Reader, mac: str, groups: list[str], plan: list[tuple[str, int, int]], start: int, end: int, reaches_now: bool,
               wind: list[tuple[int, int]], rain: bool, readings: bool = True, buckets: tuple[int, tuple[str, ...]] | None = None) -> Held:
    """A history answer's reads for [start, end] (epochs): the period's days and its series' units; the readings of the plan ((resolution,
    start, end) each, in its order) of every group (readings: a chart that draws them), else only the wind's direction and speed (to count
    the direction by) and (rain) the rain counter; the lines the database buckets ((width, resolutions)); the wind's 5-minute readings of
    these stretches; and (rain) the rain counter's settled readings."""
    units = {g: r.fields(s.id) for g in groups if (s := r.sensor(mac, g)) is not None}   # (a query per group, for every read below)
    store: Store = {}
    some = {"wind": ["wind_direction", "wind_speed"], "rainfall": ["daily"] if rain else []}
    for cycle, a, b in plan:
        if readings:
            readings_on(r, mac, groups, cycle, a, b, store, units=units)
        else:
            for group in (g for g in groups if some.get(g)):
                readings_on(r, mac, [group], cycle, a, b, store, some[group], units)
    held = Held(store, period_on(r, mac, groups, start, end, reaches_now, any(cycle == "1min" for cycle, _, _ in plan)))
    held.units = {key: units[key.split(".", 1)[0]].get(key.split(".", 1)[1], "") for key in held.period}
    if buckets:
        held.lines = buckets_on(r, mac, groups, buckets[1], start, end, buckets[0])
    for a, b in wind:
        readings_on(r, mac, ["wind"], "5min", a, b, held.wind, units=units)
    if rain:
        held.rain = _settled(r, mac, "rainfall", "daily", "30min", start, end)
        held.rain_daily = _settled(r, mac, "rainfall", "daily", "1day", start - 86400, end + 86400)
    return held


__all__ = ["Held", "Store", "buckets_on", "counts_on", "first_minute_on", "history_on", "readings_on"]
