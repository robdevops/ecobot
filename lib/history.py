"""The `readings` tool: one reading's stored values at a fixed interval, each with its time, read straight from the bot's file (lib/storage).

It answers "humidity each hour for the last 5 hours", "PM2.5 every 30 minutes this morning" and other requests for a table of values.
weather_history gives a period's highs, lows, averages and charts; this gives the readings themselves.

Every interval is offered for every reading.
- An interval a sensor stores is read as it is: Ecowitt's 5min, 30min and 4hour, Home Assistant's and the air sensor's minutes, and the
  AirGradient cloud's older hourly averages.
- Any other interval is worked out from the finer readings the sensor has, never from coarser ones: an hourly reading is not split into
  half hours.
- An interval is a block of local time (an hour from :00, a day from midnight), except 4 hours: those are the station's own 4-hour records,
  which Ecowitt cuts on UTC hours (in Melbourne they start at 3am, 7am ... in summer, 2am, 6am ... in winter), so a 4-hour row is one of them.
- A stored reading counts only in the interval that holds all of the time it covers: one that would straddle two is left out.
- A day comes from the day rows (lib/storage/days.py), so its figures are those weather_days and air_days give.

The tool reads only the file and never asks a device or a cloud: what was never fetched, or what the sensor missed while it was off, comes
back as missing rows.
"""

import logging
import time as clock
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, tzinfo
from typing import NamedTuple

from .airgradient.metrics import PARTICLES, SPIKE_NEIGHBOUR, STORED as AIR_STORED, without_spikes
from .ecowitt.api import GROUPS, unit_text
from .lines import summary_by
from .rain import MAX_GAP, rain_slots
from .series import AIR, AIR_EXTRA, WEATHER
from .storage.catalogue import BY_NAME, KINDS, OTHER, RAIN_COUNTERS, Column, column_of, kind_of, text
from .storage.read import FINE, Reader
from .storage.schema import q
from .timeutil import clock_text, day_text, local_date, local_epoch, parse_local, to_local, when
from .tools import Tool, Turn, json_result

log = logging.getLogger(__name__)

INTERVALS = {"1min": 60, "5min": 300, "30min": 1800, "1hour": 3600, "4hour": 14400, "1day": 86400}
STORED = ("1min", "5min", "30min", "1hour", "4hour")   # the resolutions held as readings (Ecowitt's 1day records are read as day rows)
UTC_CUT = 14400   # Ecowitt's 4-hour records start on UTC hours (catalogue: "cut on UTC hours"): the 4hour interval is cut where they are
RAIN_FROM = ("1min", "5min", "30min")   # the running daily total is close enough together for rain_slots (MAX_GAP) only up to here
ALIASES = {"1m": "1min", "minute": "1min", "5m": "5min", "30m": "30min", "half hour": "30min", "half-hour": "30min",
           "1h": "1hour", "hour": "1hour", "hourly": "1hour", "60min": "1hour", "4h": "4hour", "day": "1day", "daily": "1day", "1d": "1day"}
MAX_ROWS = 300
DAY = 86400
RAIN = "rain"   # the reading offered in place of the station's rain counters: the mm that fell in each interval
AIR_SENSOR = "air"
DEFAULT_UNITS = {"rain_rate": "mm/h", "absolute": "hPa", "wind_direction": "°", "app_temp": "°C"}   # beside series.WEATHER's
MINUTES_NOTE = "1-minute readings are kept for 10 days, and only where Home Assistant or the sensor itself was read every minute."


class Metric(NamedTuple):
    sensor: str      # the Ecowitt group, or "air"
    reading: str     # the name offered: a catalogue column, or "rain"
    kind: str        # the storage table
    column: Column   # the column read (rain: the running daily total)
    unit: str

    @property
    def name(self) -> str:
        return f"{self.sensor}.{self.reading}"

    @property
    def rain(self) -> bool:
        return self.reading == RAIN

    @property
    def air(self) -> bool:
        return self.sensor == AIR_SENSOR

    @property
    def how(self) -> str:
        """How an interval's readings are told as one: "rain" (summed), "max" (the gusts), "last" (a compass point), "mean"."""
        return RAIN if self.rain else self.column.mode


def _default_unit(column: str) -> str:
    return next((r.unit for r in WEATHER.values() if r.field == column), DEFAULT_UNITS.get(column, ""))


def station_metrics(sent: dict[str, dict[str, str]] | None = None) -> list[Metric]:
    """The weather station's readings: for each group the bot fetches (api.GROUPS), the catalogue's columns its sensor has sent
    (`sent`: {group: {field: unit}}, as the file's `fields` table has them). A group with nothing recorded yet offers every column of its
    kind. The rain counters are running totals, so they are offered as one reading, "rain": the mm that fell in each interval."""
    out = []
    for group in GROUPS:
        kind = kind_of(group)
        if kind == OTHER:
            continue
        units: dict[str, str] = {}
        for field, unit in (sent or {}).get(group, {}).items():
            if (place := column_of(group, field)) and place[1] == "value":
                units.setdefault(place[0], unit_text(unit) or "")
        for c in KINDS[kind]:
            if units and c.name not in units:
                continue
            if c.name in RAIN_COUNTERS:
                if c.name == "daily":
                    out.append(Metric(group, RAIN, kind, c, units.get(c.name) or "mm"))
                continue
            out.append(Metric(group, c.name, kind, c, units.get(c.name) or _default_unit(c.name)))
    return out


def air_metrics(extra: bool = False) -> list[Metric]:
    """The air sensor's readings, with its own temperature and humidity when `extra` (offered only when a question names them)."""
    names = [*AIR, *(AIR_EXTRA if extra else ())]
    return [Metric(AIR_SENSOR, n, AIR_SENSOR, BY_NAME[AIR_SENSOR][n], AIR_STORED[n][1]) for n in names]


def parameters(names: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "metric": {"type": "string", "enum": names,
                       "description": "The sensor and reading, e.g. outdoor.humidity, indoor.temperature, pressure.relative, "
                                      "wind.wind_gust, rainfall.rain (mm that fell in each interval), air.pm2_5. One per call: "
                                      "outdoor and indoor are two calls, made together."},
            "start": {"type": "string", "description": "Start, 'YYYY-MM-DD HH:MM' local time."},
            "end": {"type": "string", "description": "End, 'YYYY-MM-DD HH:MM' local time. Default: now."},
            "interval": {"type": "string", "enum": list(INTERVALS),
                         "description": "One row per interval, a block of local time (1hour runs from :00 to :00; 4hour follows the station's "
                                        "own 4-hour records, whose times say where they start). 1min goes back 10 days."},
        },
        "required": ["metric", "start", "interval"],
    }


DESCRIPTION = (
    "The stored readings of ONE metric at a fixed interval, a row per interval with its time: for values at set times or per interval "
    "(\"humidity each hour for the last 5 hours\", \"PM2.5 every 30 minutes this morning\", \"rain each hour today\", a table of "
    "values; for one moment such as 3pm, a few minutes either side at 5min). Each row: the interval's average with its lowest and highest (rain: the mm that fell in it; gusts: the "
    f"highest). At most {MAX_ROWS} rows: pick an interval that fits (5min for a few hours, 1hour for up to 12 days, 4hour for up to 7 "
    "weeks, 1day for up to 300 days). For a period's highs and lows, records, averages or a chart use weather_history or air_quality.")
PARAMETERS = parameters([m.name for m in [*station_metrics(), *air_metrics()]])   # the tool as offered before any file is read (the evals)


def interval_starts(start: int, end: int, width: int, tz: tzinfo) -> list[int]:
    """The starts of the intervals of `width` seconds that hold [start, end]: blocks of local time from midnight, so an hour runs from :00
    whatever the zone's offset, and a day from midnight; across a daylight-saving change one is an hour longer or shorter. Four hours are
    cut on UTC hours, as Ecowitt cuts its 4-hour records (UTC_CUT)."""
    if width == UTC_CUT:
        return list(range(start // width * width, end + 1, width))
    marks: set[int] = set()
    day = local_date(start, tz)
    while day <= local_date(end, tz):
        midnight = datetime.combine(day, time())
        marks.update(local_epoch(midnight + timedelta(seconds=k * width), tz) for k in range(DAY // width))
        day += timedelta(days=1)
    ordered = sorted(marks)
    first = max(bisect_right(ordered, start) - 1, 0)   # the interval holding start
    return [t for t in ordered[first:] if t <= end]


def row_count(start: int, end: int, interval: str, tz: tzinfo) -> int:
    """About how many rows [start, end] makes at this interval (a day is a local date), without listing them."""
    if interval == "1day":
        return (local_date(end, tz) - local_date(start, tz)).days + 1
    return (end - start) // INTERVALS[interval] + 1


def within_cap(start: int, end: int, interval: str, tz: tzinfo) -> bool:
    """Does [start, end] make at most MAX_ROWS rows at this interval? Shorter than a day, the count is exact only once the intervals are
    listed (the first starts before `start`, and a daylight-saving change makes one an hour shorter): they are, once the estimate is close."""
    n = row_count(start, end, interval, tz)
    if interval == "1day":
        return n <= MAX_ROWS
    return n <= MAX_ROWS + 1 and len(interval_starts(start, end, INTERVALS[interval], tz)) <= MAX_ROWS


def finest_fit(start: int, end: int, tz: tzinfo) -> str | None:
    """The finest interval that keeps [start, end] within MAX_ROWS rows (None: not even days)."""
    return next((i for i in INTERVALS if within_cap(start, end, i, tz)), None)


def number(value: float | None, column: Column) -> float | int | None:
    """A reading written as its source writes it ("12.3", "94"), as a JSON number."""
    if value is None:
        return None
    written = text(value, column.decimals, column.whole)
    return float(written) if "." in written else int(written)


# ---------- reading the file (on the reader's thread) ----------
def _points(r: Reader, m: Metric, sensor: int, start: int, end: int, width: int) -> dict[str, list[tuple]]:
    """{resolution: [(ts, value, low, high)]} of the stored readings at every resolution as fine as the interval or finer, from start to end.
    Air particles have their glitches taken out first (the air_quality tool's rule), and an air reading's lowest and highest are its value's
    (the air day rows' rule: a band's extremes are single samples the glitch rule never sees)."""
    particles = m.air and m.reading in PARTICLES
    columns = list(PARTICLES) if particles else [m.column.name]
    around = SPIKE_NEIGHBOUR if particles else 0   # a glitch is told by the readings either side of it, those just outside the window too
    out: dict[str, list[tuple]] = {}
    for res in (RAIN_FROM if m.rain else STORED):
        if INTERVALS[res] > width:
            continue
        got = r.rows(m.kind, [sensor], res, start - around, end + around, columns).get(sensor)
        if got is None or m.column.name not in got.values:
            continue
        if m.air:
            rows = [{"ts": t, **{c: v for c in columns if (v := got.values[c][i]) is not None}} for i, t in enumerate(got.ts)]
            if particles:
                rows = [row for row in without_spikes(rows) if start <= row["ts"] <= end]
            points = [(row["ts"], v, v, v) for row in rows if (v := row.get(m.column.name)) is not None]
        else:
            name = m.column.name
            points = [(t, v, v if lo is None else lo, v if hi is None else hi)
                      for t, v, lo, hi in zip(got.ts, got.values[name], got.lo[name], got.hi[name]) if v is not None]
        if points:
            out[res] = points
    return out


def _blocks(points: dict[str, list[tuple]], m: Metric, marks: list[int], start: int) -> dict[int, tuple]:
    """{interval start: (value, low, high)} of the intervals that have readings. A mean takes one value per 30-minute slot, the finer
    readings where a slot has them (lines.summary_by, as the charts do); gusts take the highest; a compass point the last; rain the mm
    that fell (the rise of the running daily total, its midnight reset included: lib/rain.rain_slots)."""
    key = lambda ts: marks[bisect_right(marks, ts) - 1]
    if m.how == RAIN:
        # One running total from every resolution: a coarser reading only where no finer one falls in the time it covers. Their times
        # mean different moments (a 5-minute reading's total is the one at its end), so a finer reading just after a coarser one can be
        # lower, which rain_slots would take for the midnight reset (and count the whole day's rain again).
        total: dict[int, float] = {}
        for res in sorted(points, key=INTERVALS.get):
            span, have = INTERVALS[res], sorted(total)
            for t, v, _, _ in points[res]:
                i = bisect_left(have, t)
                if i == len(have) or have[i] >= t + span:
                    total[t] = v
        out: dict[int, tuple] = {}
        for t, mm in rain_slots(total).items():
            if t >= start:
                got = out.get(key(t), (0.0, None, None))
                out[key(t)] = (got[0] + mm, None, None)
        return out
    inside = {res: [p for p in ps if p[0] >= marks[0] and key(p[0]) == key(p[0] + INTERVALS[res] - 1)] for res, ps in points.items()}
    every = sorted((p for ps in inside.values() for p in ps), key=lambda p: p[0])
    if m.how == "max":   # the highest of the readings' own highs (a 30-minute record's gust is its first; its high, the strongest)
        out = {}
        for t, _, lo, hi in every:
            k = key(t)
            got = out.get(k)
            out[k] = (max(hi, got[0]), min(lo, got[1]), max(hi, got[2])) if got else (hi, lo, hi)
        return out
    if m.how == "last":
        return {key(t): (v, None, None) for t, v, _, _ in every}   # (oldest first: the last one stays)
    return summary_by([(t, v, lo, hi, res in FINE) for res, ps in inside.items() for t, v, lo, hi in ps], key)


def _coarser(r: Reader, m: Metric, sensor: int, gaps: list[int], width: int, tz: tzinfo) -> tuple[str, int, int] | None:
    """(resolution, first, last) of the finest readings coarser than the interval that cover any of these empty intervals (their starts):
    where a time is held only that coarsely (the air sensor's older hourly history, Ecowitt's 30-minute and 4-hour ones, the day rows), the
    interval to ask for instead. None when nothing coarser covers them either."""
    for res in STORED:
        span = INTERVALS[res]
        if span <= width or (m.rain and res not in RAIN_FROM):
            continue
        found = [t for (t,) in r.db.execute(f"SELECT ts FROM {m.kind} WHERE sensor = ? AND res = ? AND ts BETWEEN ? AND ? AND "
                                            f"{q(m.column.name)} IS NOT NULL", (sensor, res, gaps[0] - span + 1, gaps[-1] + width - 1))]
        covering = [t for t in found if (i := bisect_right(gaps, t + span - 1)) and gaps[i - 1] + width > t]   # (a gap starting before it ends, and ending after it starts)
        if covering:
            return res, covering[0], covering[-1]
    if m.how != "last" and (days := r.days(m.kind, [sensor], local_date(gaps[0], tz), local_date(gaps[-1], tz), [m.column.name]).get(sensor)):
        figure = days.mean if m.how == "mean" else days.hi
        held = [d for d, v in zip(days.day, figure.get(m.column.name, [])) if v is not None]
        if held:
            return "1day", local_epoch(datetime.combine(held[0], time()), tz), local_epoch(datetime.combine(held[-1], time()), tz)
    return None


def read_intervals(r: Reader, m: Metric, sensor: int, interval: str, marks: list[int], end: int, now: int, tz: tzinfo) -> dict:
    """The rows of an interval shorter than a day (or of days of a compass point), and what to say about them."""
    width = INTERVALS[interval]
    first = marks[0] - (MAX_GAP if m.rain else 0)   # rain: the reading before the first interval, for its first rise
    points = _points(r, m, sensor, first, end, width)
    blocks = _blocks(points, m, marks, marks[0])
    ranged = m.how == "mean"
    rows = []
    for t in marks:
        value, low, high = blocks.get(t, (None, None, None))
        row = [when(t, tz), number(value, m.column)]
        rows.append(row + [number(low, m.column), number(high, m.column)] if ranged else row)
    out = {"columns": ["time", "value", "low", "high"] if ranged else ["time", "value"], "rows": rows,
           "from": [res for res in INTERVALS if res in points]}
    if missing := sum(1 for t in marks if t not in blocks):
        out["missing"] = missing
    gaps = [t for t in marks if t not in blocks and t + width <= now]   # (the interval still filling isn't a gap)
    if gaps and (coarse := _coarser(r, m, sensor, gaps, width, tz)):
        res, a, b = coarse
        held = f"{day_text(to_local(a, tz))} to {day_text(to_local(b, tz))}" if res == "1day" else f"{when(a, tz)} to {when(b, tz)}"
        out["note_coarser"] = (f"Some of the empty rows are held only as {res} readings ({held}): ask again with interval {res} for those "
                               "times.")
        out["fits"] = res
    elif gaps and interval == "1min":
        out["note_minutes"] = MINUTES_NOTE
    return out


def read_days(r: Reader, m: Metric, sensor: int, first: date, last: date, tz: tzinfo) -> dict:
    """The rows of days, from the day rows: the day's mean with its lowest and highest and when they happened (gusts: the highest and
    when; rain: the day's total)."""
    got = r.days(m.kind, [sensor], first, last, [m.column.name]).get(sensor)
    by_day = {} if got is None else {d: i for i, d in enumerate(got.day)}
    name, rows, sources = m.column.name, [], set()
    at = lambda ts: clock_text(to_local(ts, tz)) if ts is not None else None
    day = first
    while day <= last:
        i = by_day.get(day)
        figures = None if i is None else {p: getattr(got, p).get(name, [None] * len(got.day))[i] for p in ("mean", "lo", "lo_ts", "hi", "hi_ts")}
        has = figures is not None and (figures["mean"] if m.how == "mean" else figures["hi"]) is not None
        if has:
            sources.add(got.source[i])
        if m.how == "mean":
            rows.append([day_text(day), *((number(figures["mean"], m.column), number(figures["lo"], m.column), at(figures["lo_ts"]),
                                           number(figures["hi"], m.column), at(figures["hi_ts"])) if has else (None,) * 5)])
        elif m.how == "max":
            rows.append([day_text(day), *((number(figures["hi"], m.column), at(figures["hi_ts"])) if has else (None, None))])
        else:   # rain: the running daily total's highest is the day's total
            rows.append([day_text(day), number(figures["hi"], m.column) if has else None])
        day += timedelta(days=1)
    columns = {"mean": ["day", "value", "low", "low_time", "high", "high_time"], "max": ["day", "value", "time"]}.get(m.how, ["day", "value"])
    out = {"columns": columns, "rows": rows, "from": sorted(sources, key=lambda s: INTERVALS.get(s, DAY))}
    if missing := sum(1 for row in rows if row[1] is None):
        out["missing"] = missing
    if "1day" in sources:
        out["note_daily"] = ("Days from \"1day\" come from Ecowitt's daily records, which run 10am to 10am, so their figures (and rain "
                             "totals) are approximate and their times are 4-hour windows at best.")
    return out


VALUE_IS = {RAIN: "the mm of rain that fell in the interval", "max": "the highest in the interval",
            "last": "the reading at the end of the interval", "mean": "the interval's average; low and high are its lowest and highest"}
DAY_VALUE_IS = {RAIN: "the day's rain total, mm", "max": "the day's highest, and when",
                "mean": "the day's average, with its lowest and highest and when they happened"}


class History:
    """The `readings` tool over the weather station's and the air sensor's stored readings (either may be missing)."""

    def __init__(self, eco=None, air=None, sent: dict[str, dict[str, str]] | None = None, now: Callable[[], float] = clock.time):
        """eco, air: the sources (Ecowitt, AirGradient), started. sent: the station's fields as the file has them ({group: {field: unit}};
        History.open reads them). now: the clock (a test's)."""
        self.eco, self.air, self.now = eco, air, now
        self.storage = eco.storage if eco else air.store.storage
        self.tz = eco.tz if eco else air.tz
        station = station_metrics(sent) if eco else []
        self.metrics = {m.name: m for m in [*station, *(air_metrics(extra=True) if air else [])]}
        offered = [m.name for m in [*station, *(air_metrics() if air else [])]]
        asked = [m.name for m in [*station, *(air_metrics(extra=True) if air else [])]]
        self.tools = [Tool("readings", DESCRIPTION, parameters(offered), self.handle,
                           asked=(lambda: (DESCRIPTION, parameters(asked))) if air else None)]

    @classmethod
    async def open(cls, eco=None, air=None) -> "History":
        """The tool, with the readings the station has sent read from the file."""
        sent = await eco.storage.read(lambda r: {s.external: r.fields(s.id) for s in r.sensors(device=eco.mac) if s.kind != AIR_SENSOR}) if eco else None
        return cls(eco, air, sent)

    def find(self, name: str) -> Metric | None:
        """The metric a name means: its own name, else a reading's ("humidity", "uv", "pm2_5", "rain") on the sensor that has it. A sensor
        named (indoor, air) is kept: "air.humidity" is the air sensor's own, never the outdoor humidity; "indoor.vpd" is none."""
        name = name.strip().lower()
        if name in self.metrics:
            return self.metrics[name]
        prefix, _, bare = name.rpartition(".")
        if prefix == AIR_SENSOR and (m := self.metrics.get(f"{AIR_SENSOR}.{AIR_SENSOR}_{bare}")):
            return m
        reading = WEATHER.get(bare) or next((r for r in WEATHER.values() if r.field == bare), None)
        m = (reading and self.metrics.get(f"{reading.group}.{RAIN if reading.field == 'daily' else reading.field}")
             or self.metrics.get(f"{AIR_SENSOR}.{bare}") or self.metrics.get(f"outdoor.{bare}"))
        return None if m and prefix in ("indoor", AIR_SENSOR) and m.sensor != prefix else m

    def _sensor(self, m: Metric) -> int | None:
        if m.air:
            s = self.air.store.sensor() if self.air else None
        else:
            s = self.storage.sensors.find(self.eco.mac, m.sensor) if self.eco else None
        return s.id if s else None

    def _time(self, value, default: int) -> int:
        """A start or end as sent ("2026-11-09 01:00", with seconds, a "T", or an offset such as "Z", which is moved into local time)."""
        if value in (None, ""):
            return default
        text = str(value).strip()
        try:
            local = parse_local(text, self.tz)
        except ValueError:   # trailing words ("2026-11-09 01:00 AEDT"): the time alone
            local = parse_local(text.replace("T", " ")[:19], self.tz)
        return local_epoch(local, self.tz)

    async def handle(self, args: dict, turn: Turn | None = None) -> str:
        m = self.find(str(args.get("metric") or ""))
        if m is None:
            return json_result({"error": f"unknown metric {args.get('metric')!r}", "metrics": list(self.metrics)})
        interval = str(args.get("interval") or "").strip().lower()
        interval = ALIASES.get(interval, interval)
        if interval not in INTERVALS:
            return json_result({"error": f"unknown interval {args.get('interval')!r}", "intervals": list(INTERVALS)})
        now = int(self.now())
        try:
            start, end = self._time(args.get("start"), None), min(self._time(args.get("end"), now), now)
        except (TypeError, ValueError):
            return json_result({"error": "start and end are 'YYYY-MM-DD HH:MM', local time"})
        if start is None or start >= end:
            return json_result({"error": "start must be before end (and before now)"})
        by_day = interval == "1day" and m.how != "last"   # (a compass point has no day figures: the last reading of each day)
        n = row_count(start, end, interval, self.tz)
        too_many = n > (MAX_ROWS if by_day else MAX_ROWS + 1)   # (an interval's count is exact only once listed: the first starts before `start`)
        marks = [] if too_many or by_day else interval_starts(start, end, INTERVALS[interval], self.tz)
        if too_many or len(marks) > MAX_ROWS:
            fits = finest_fit(start, end, self.tz)
            return json_result({"error": "too many rows", "rows": max(n, len(marks)), "max_rows": MAX_ROWS, "fits": fits,
                                "note": f"Ask again with interval {fits}." if fits else
                                        "Too long even for days: split the period, or use weather_history or weather_days."})
        out = {"metric": m.name, "unit": m.unit, "interval": interval, "value_is": (DAY_VALUE_IS if by_day else VALUE_IS)[m.how]}
        sensor = self._sensor(m)
        if sensor is None:
            return json_result({**out, "rows": [], "note": "Nothing is stored for this sensor yet."})
        if by_day:
            first, last = local_date(start, self.tz), local_date(end, self.tz)
            out |= await self.storage.read(read_days, m, sensor, first, last, self.tz)
            partial = last == local_date(now, self.tz)
        else:
            out |= await self.storage.read(read_intervals, m, sensor, interval, marks, end, now, self.tz)
            partial = marks[-1] + INTERVALS[interval] > now
        if partial:
            out["note_last"] = "The last row is still filling: it runs up to now."
        if not any(row[1] is not None for row in out["rows"]):
            out["note"] = (f"No readings at this interval for this period: ask again with interval {out['fits']}." if out.get("fits") else
                           "No readings are stored for this period at this interval: say so; never fill the gap.")
        return json_result(out)


__all__ = ["DESCRIPTION", "History", "INTERVALS", "MAX_ROWS", "PARAMETERS", "air_metrics", "interval_starts", "parameters", "station_metrics"]
