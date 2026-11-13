"""What a chart is, as data. Every chart is a Chart: a title, a subtitle and one or more Panels on a shared time axis.

A Panel draws one of:
  - lines on one axis, optionally with rain-style `bars` behind them on their own axis;
  - bars on their own (rain with no line to sit behind);
  - shares (the traffic-light share of each bar's time in good, poor and very poor).
A Line is a series of points, each optionally with the low and high it stands for (drawn as a band around the line), and
optionally the true records (lowest and highest reading, with when). Anything malformed raises here, when the chart is
built, not when it is drawn.

A chart also goes as JSON (to_json, from_json; its schema is schemas/chart.json): what a tool hands over, for whoever draws it."""

import math
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import date, datetime, tzinfo
from statistics import fmean

from .series import RAIN_WITH, WEATHER
from .timeutil import day_bounds, day_text


# Where a chart's data came from, written small in its bottom left corner ("Source: Ecowitt weather station"). Set by tools.Tools.call
# for the length of one tool call (each call runs in its own context, so calls made together don't mix), from the model's `source`
# argument or else the tool's own; a chart built in that call takes it.
CHART_SOURCE: ContextVar[str] = ContextVar("chart_source", default="")
SOURCE_CHARS = 60   # the longest source written (a URL beyond it is cut with an ellipsis)
ECOWITT_SOURCE, AIR_SOURCE = "Ecowitt weather station", "AirGradient sensor"


def source_text(given) -> str:
    """A source as it is written on a chart: one line, at most SOURCE_CHARS characters; "" for nothing usable."""
    text = " ".join(str(given or "").split())
    return text if len(text) <= SOURCE_CHARS else text[:SOURCE_CHARS - 1].rstrip() + "…"


NO_LOW = tuple(name for name, r in WEATHER.items() if not r.low)   # readings whose low is nothing every night or calm hour: only the high is tagged


@dataclass
class Line:
    label: str
    x: list[int]                                       # epoch seconds
    y: list[float]
    low: list[float] | None = None                     # each point's lowest and highest, drawn as a band
    high: list[float] | None = None
    smoothed: bool = False                             # the line is a light average of the readings, not the readings
    records: dict[str, tuple[int, float]] = field(default_factory=dict)   # "low" / "high": (epoch, the true reading)
    reading: str = ""                                  # which reading it is ("pm2_5", ...), for its colour; else its panel's
    indoor: bool = False                               # the indoor sensor's line: the reading's complementary colour
    right: bool = False                                # drawn against the panel's second axis, on the right (see panels.two_axes)
    on_line: bool = False                              # its peak is marked on the line itself, not on the top of its band (the band's high may be one glitch)

    def __post_init__(self):
        if len(self.x) != len(self.y) or len(self.x) < 2:
            raise ValueError(f"line {self.label!r}: needs at least two points, x and y the same length")
        if (self.low is None) != (self.high is None) or (self.low is not None and not len(self.low) == len(self.high) == len(self.x)):
            raise ValueError(f"line {self.label!r}: low and high go together and match the points")
        if not set(self.records) <= {"low", "high"}:
            raise ValueError(f"line {self.label!r}: records are 'low' and 'high'")

    def own_records(self) -> dict[str, tuple[int, float]]:
        """The line's highest and lowest point as drawn (the top and bottom of its band when it has one)."""
        hi, lo = self.high or self.y, self.low or self.y
        i, j = max(range(len(hi)), key=hi.__getitem__), min(range(len(lo)), key=lo.__getitem__)
        return {"high": (self.x[i], hi[i]), "low": (self.x[j], lo[j])}


@dataclass
class Bars:
    label: str
    unit: str
    x: list[int]                                       # each bar's start, epoch seconds
    y: list[float]
    width: int                                         # seconds
    per: str = ""                                      # what a bar covers: "hour", "6 hours", "day", "month", "year"
    values: bool = False                               # write each bar's number above it (a few bars of counts)

    def __post_init__(self):
        if len(self.x) != len(self.y):
            raise ValueError(f"bars {self.label!r}: x and y differ in length")


def calendar_bars(label: str, unit: str, figures: dict[date, float], by: str, tz: tzinfo) -> Bars:
    """A bar per calendar month or year (`by`) from {its first day: figure}, the figure written above it. A bar is as wide as
    the shortest month or year."""
    return Bars(label, unit, [day_bounds(d, tz)[0] for d in figures], [float(v) for v in figures.values()],
                28 * 86400 if by == "month" else 365 * 86400, by, values=True)


@dataclass
class Shares:
    label: str
    x: list[int]
    width: int
    good: list[float]                                  # percent of each bar's readings in each rating
    poor: list[float]
    very_poor: list[float]
    per: str = ""

    def __post_init__(self):
        if not len(self.x) == len(self.good) == len(self.poor) == len(self.very_poor):
            raise ValueError(f"shares {self.label!r}: columns differ in length")


@dataclass
class Panel:
    label: str
    unit: str = ""
    lines: list[Line] = field(default_factory=list)
    bars: Bars | None = None                           # behind the lines if there are any (rain), else the panel itself
    shares: Shares | None = None
    zones: tuple[float, float] | None = None           # good and poor limits, shaded behind a single rated reading
    reading: str = ""                                  # which reading this is ("humidity", "pressure" ...), for the rain rule
    flip_right: bool = False                           # the second axis runs top to bottom (a line on it falls as its reading rises)
    peaks: str = "pills"                               # how peaks are labelled: "pills" on the lines, "aside" in empty space, "none"

    def __post_init__(self):
        if not (self.lines or self.bars and self.bars.x or self.shares and self.shares.x):
            raise ValueError(f"panel {self.label!r}: nothing to draw")
        if self.shares and (self.lines or self.bars):
            raise ValueError(f"panel {self.label!r}: shares draw alone")
        if self.peaks not in ("pills", "aside", "none"):
            raise ValueError(f"panel {self.label!r}: peaks are 'pills', 'aside' or 'none'")
        if self.lines and all(line.right for line in self.lines):
            raise ValueError(f"panel {self.label!r}: a right-hand axis needs a line on the left too")

    @property
    def bars_only(self) -> bool:
        """Bars and no lines: rain on its own, or a count or figure per month or year."""
        return bool(self.bars) and not self.lines

    @property
    def xs(self) -> list[int]:
        """Every x the panel draws, bars to the end of the last one."""
        out = [t for line in self.lines for t in line.x]
        for b in (self.bars, self.shares):
            if b and b.x:
                out += [b.x[0], b.x[-1] + b.width]
        return out


@dataclass
class Compass:
    """A wind rose of the whole period, drawn beside a wind chart: 16 compass points (N first) x 3 speed steps."""
    rose: list[list[float]]
    speeds: bool                                       # the readings carried a wind speed, so the steps mean something
    speed_steps: tuple[float, float] = (10, 20)


@dataclass
class Chart:
    title: str
    subtitle: str
    panels: list[Panel]
    compass: Compass | None = None
    period: tuple[date, date] | None = None            # the first and last day it covers, for its caption
    source: str = ""                                   # where the data came from (CHART_SOURCE when not given): bottom left, small

    @property
    def single(self) -> bool:
        """One panel of lines: drawn big, with the room for a compass beside it."""
        return len(self.panels) == 1 and bool(self.panels[0].lines)

    @property
    def newest(self) -> int:
        """The time of the newest reading drawn (epoch seconds): a line's last point, or the start of the last bar."""
        return max([line.x[-1] for p in self.panels for line in p.lines] +
                   [b.x[-1] for p in self.panels for b in (p.bars, p.shares) if b and b.x])

    def __post_init__(self):
        if not self.panels:
            raise ValueError("a chart needs a panel")
        if self.compass and not self.single:
            raise ValueError("a compass goes beside a single line panel")
        self.source = source_text(self.source or CHART_SOURCE.get())
        for panel in self.panels:  # every line is labelled with its high and low, so each carries its records
            for line in panel.lines:
                line.records = line.records or line.own_records()
                if panel.reading in NO_LOW:
                    line.records.pop("low", None)

    def caption(self, today: date) -> str:
        """The chart's caption. One panel: one line, "Temperature, since Tue 7 Jul 2026" (a period that runs up to today),
        "Temperature, Wed 24 – Tue 30 Sep 2026" otherwise. Several panels: the title, then the period on a line of its own."""
        if not self.period:
            return self.title
        first, last = self.period
        if len(self.panels) != 1:
            return f"{self.title}\n{period_text(first, last)}"
        if first == last:
            return f"{self.title}, {'today' if first == today else day_text(first)}"
        return f"{self.title}, since {day_text(first)}" if last == today else f"{self.title}, {period_text(first, last)}"


def period_text(a: date, b: date) -> str:
    """'Tue 30 Sep 2026', 'Wed 24 – Tue 30 Sep 2026', 'Thu 27 Aug – Tue 30 Sep 2026'; the year goes on both dates when it differs."""
    if a == b:
        return day_text(a)
    if (a.year, a.month) == (b.year, b.month):
        return f"{a:%a} {a.day} – {day_text(b)}"
    return f"{day_text(a, year=a.year != b.year)} – {day_text(b)}"


def rain_behind(panels: list[Panel]) -> list[Panel]:
    """A rain panel goes behind a line panel (the first bars-only panel is drawn there, on its own right-hand axis), so the
    rain lines up with the reading: the one series.RAIN_WITH names first, else the first line. With no line panel it
    stays a panel of its own."""
    lines = [p for p in panels if p.lines and not p.bars]
    line_panel = next((p for name in RAIN_WITH for p in lines if p.reading == name), lines[0] if lines else None)
    bars_panel = next((p for p in panels if p.bars_only), None)
    if not (line_panel and bars_panel):
        return panels
    line_panel.bars = bars_panel.bars
    return [p for p in panels if p is not bars_panel]


def stack(panels: list[Panel], first: date, last: date) -> Chart:
    """A chart of these panels for the days first to last, rain behind the first line. The subtitle says what a bar covers and
    whether a range is shaded. Every producer of a stacked chart goes through here, so they all read alike."""
    per = next((f"{'rain' if b is p.bars else 'rating'} per {b.per}" for p in panels for b in (p.bars, p.shares) if b and b.per), "")
    shaded = any(s.low is not None for p in panels for s in p.lines)
    return Chart(", ".join(p.label for p in panels),
                 "  ·  ".join(filter(None, [period_text(first, last), per, "range shaded" if shaded else ""])), rain_behind(panels),
                 period=(first, last))


@dataclass
class Tile:
    """One gauge of the dashboard: a reading drawn as a thermometer, a dial or a rain beaker, with its extremes below."""
    title: str
    unit: str
    value: float | None                    # now; None (no reading now) draws it dimmed, at its floor
    kind: str                              # "thermometer", "dial" or "beaker"
    low: float                             # the scale's ends
    high: float
    colours: tuple[str, ...]               # the scale's bands, from the low end to the high one
    columns: tuple[str, ...] = ()          # what the table under the gauge lists: "Max", "Min" or "Total"
    rows: dict[str, tuple[float | None, ...]] = field(default_factory=dict)   # "Daily" / "Monthly" / "Yearly" -> a figure per column
    ticks: tuple[float, ...] = ()          # the scale's labels
    decimals: int = 1
    bounds: tuple[float, ...] = ()         # the bands' edges, when each band is an equal share of the scale (the air gauges' categories);
                                           # none: the scale is even, low to high
    band: str | None = None                # the name of the band the reading is in, shown under it


@dataclass
class Dashboard:
    """The gauges of the weather station and the air sensor, left to right and top to bottom, `per_row` to a row."""
    tiles: list[Tile]
    per_row: int = 3


# ---------- a chart as JSON ----------
# How a chart is handed over (schemas/chart.json): its title, subtitle, period, source and the time of its newest reading, then each
# panel with its series. A series is a "line" ("band" when each point carries the low and high shaded around it), rain-style "bars",
# or a "rating" (each bar's share of time in good, poor and very poor). Times are ISO local times with their UTC offset, so each gives
# its epoch back exactly, even in the hour that happens twice when the clocks go back.
CHART_VERSION = 1
COLUMNS = {"line": 2, "band": 4, "bars": 2, "rating": 4}   # a point of each kind: [time, value], [time, value, low, high],
                                                            # [time, value] and [time, good, poor, very poor] (percent)


def to_json(chart: Chart, tz: tzinfo, max_points: int | None = None) -> dict:
    """The chart as JSON, with everything the renderer reads, so from_json gives the same chart back. Times are local to `tz`.
    max_points: each line in at most that many buckets of time (for a model to read), each the mean with the bucket's lowest
    and highest as its band, so the true extremes stay; None keeps the points as drawn (the bot's own charts, already within
    lines.POINT_BUDGET)."""
    if max_points is not None and max_points < 2:
        raise ValueError("max_points: a line needs at least two points")
    when = lambda t: datetime.fromtimestamp(t, tz).isoformat()

    def line(s: Line) -> dict:
        s = _bucketed(s, max_points) if max_points else s
        band = s.low is not None
        return {"name": s.label, "kind": "band" if band else "line",
                "points": [[when(t), *v] for t, *v in zip(s.x, s.y, *((s.low, s.high) if band else ()))],
                "records": {k: [when(t), v] for k, (t, v) in s.records.items()},
                "reading": s.reading, "smoothed": s.smoothed, "indoor": s.indoor, "right": s.right, "on_line": s.on_line}

    def panel(p: Panel, compass: Compass | None) -> dict:
        series = [line(s) for s in p.lines]
        if b := p.bars:
            series.append({"name": b.label, "kind": "bars", "unit": b.unit, "width": b.width, "per": b.per, "values": b.values,
                           "points": [[when(t), v] for t, v in zip(b.x, b.y)]})
        if r := p.shares:
            series.append({"name": r.label, "kind": "rating", "width": r.width, "per": r.per,
                           "points": [[when(t), *v] for t, *v in zip(r.x, r.good, r.poor, r.very_poor)]})
        out = {"label": p.label, "unit": p.unit, "reading": p.reading, "series": series, "flip_right": p.flip_right, "peaks": p.peaks}
        if p.zones is not None:
            out["zones"] = list(p.zones)
        if compass:
            out["compass"] = {"rose": [list(row) for row in compass.rose], "speeds": compass.speeds,
                              "speed_steps": list(compass.speed_steps)}
        return out
    return {"version": CHART_VERSION, "title": chart.title, "subtitle": chart.subtitle,
            "period": [d.isoformat() for d in chart.period] if chart.period else None, "source": chart.source,
            "newest": when(chart.newest), "panels": [panel(p, chart.compass if i == 0 else None) for i, p in enumerate(chart.panels)]}


def _bucketed(line: Line, most: int) -> Line:
    """The line in at most `most` buckets of equal time, each the mean of its points at their mean time, with the lowest low and
    highest high among them as its band (the true extremes stay). A line of no more points is left as it is."""
    if len(line.x) <= most:
        return line
    start, width = line.x[0], (line.x[-1] - line.x[0]) // most + 1   # so the last point falls in bucket `most - 1` at the latest
    buckets: dict = {}
    for i, t in enumerate(line.x):
        buckets.setdefault((t - start) // width, []).append(i)
    if len(buckets) < 2:
        return line
    lows, highs, groups = line.low or line.y, line.high or line.y, list(buckets.values())
    return replace(line, x=[round(fmean(line.x[i] for i in g)) for g in groups], y=[fmean(line.y[i] for i in g) for g in groups],
                   low=[min(lows[i] for i in g) for g in groups], high=[max(highs[i] for i in g) for g in groups])


def from_json(data: dict) -> Chart:
    """The Chart to_json wrote, built the way any chart is: anything malformed raises ValueError here."""
    if not isinstance(data, dict) or data.get("version") != CHART_VERSION:
        raise ValueError(f"not a chart of version {CHART_VERSION}")
    try:
        panels, compass, recordless = [], None, []
        for p in data["panels"]:
            built = [_series(s) for s in p["series"]]
            lines = [s for s in built if isinstance(s, Line)]
            bars = [s for s in built if isinstance(s, Bars)]
            shares = [s for s in built if isinstance(s, Shares)]
            if len(bars) > 1 or len(shares) > 1:
                raise ValueError(f"panel {p['label']!r}: one series of bars and one rating at most")
            recordless += [s for s in lines if not s.records]
            panels.append(Panel(p["label"], p["unit"], lines, bars[0] if bars else None, shares[0] if shares else None,
                                _pair(p["zones"]) if "zones" in p else None, p["reading"], p["flip_right"], p["peaks"]))
            if "compass" in p:
                compass = _compass(p["compass"])
        period = None if data["period"] is None else _pair(data["period"], date.fromisoformat)
        chart = Chart(data["title"], data["subtitle"], panels, compass, period)
        chart.source = source_text(data["source"])   # as written (not the CHART_SOURCE of whatever call this is)
    except (KeyError, TypeError, IndexError, AttributeError) as e:
        raise ValueError(f"not a chart: {type(e).__name__}: {e}") from None
    for line in recordless:   # drawn without records (a reading with no low, and no high given): a chart would give it its own
        line.records = {}
    return chart


def _series(s: dict) -> Line | Bars | Shares:
    """One series of a panel's JSON as what draws it."""
    kind, points = s["kind"], s["points"]
    if kind not in COLUMNS:
        raise ValueError(f"series {s['name']!r}: its kind is one of {', '.join(COLUMNS)}, not {kind!r}")
    if any(len(point) != COLUMNS[kind] for point in points):
        raise ValueError(f"series {s['name']!r}: a point of a {kind} is {COLUMNS[kind]} values")
    x = [_epoch(point[0]) for point in points]
    columns = [[_number(point[i]) for point in points] for i in range(1, COLUMNS[kind])]
    if kind == "bars":
        return Bars(s["name"], s["unit"], x, columns[0], s["width"], s["per"], s["values"])
    if kind == "rating":
        return Shares(s["name"], x, s["width"], *columns, s["per"])
    low, high = columns[1:] if kind == "band" else (None, None)
    records = {k: (_epoch(t), _number(v)) for k, (t, v) in s["records"].items()}
    return Line(s["name"], x, columns[0], low, high, s["smoothed"], records, s["reading"], s["indoor"], s["right"], s["on_line"])


def _compass(c: dict) -> Compass:
    if len(c["rose"]) != 16 or any(len(row) != 3 for row in c["rose"]):
        raise ValueError("a wind rose is 16 compass points of 3 speed steps")
    return Compass([[_number(v) for v in row] for row in c["rose"]], c["speeds"], _pair(c["speed_steps"]))


def _pair(values: list, read=None) -> tuple:
    """Two values (zones, a period, speed steps) as a tuple, each read by `read` (else checked to be a number)."""
    if len(values) != 2:
        raise ValueError(f"two values, not {values!r}")
    return tuple(map(read or _number, values))


def _epoch(text: str) -> int | float:
    """An ISO time with its UTC offset as epoch seconds (an int when whole, as the charts hold them)."""
    t = datetime.fromisoformat(text)
    if t.tzinfo is None:
        raise ValueError(f"a time needs its UTC offset: {text!r}")
    seconds = t.timestamp()
    return int(seconds) if seconds.is_integer() else seconds


def _number(v) -> float:
    """A value as given, checked to be a finite number (not a string, a bool or None)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"not a number: {v!r}")
    return v
