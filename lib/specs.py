"""What a chart is, as data. Every chart is a Chart: a title, a subtitle and one or more Panels on a shared time axis.

A Panel draws one of:
  - lines on one axis, optionally with rain-style `bars` behind them on their own axis;
  - bars on their own (rain with no line to sit behind);
  - shares (the traffic-light share of each bar's time in good, poor and very poor).
A Line is a series of points, each optionally with the low and high it stands for (drawn as a band around the line), and
optionally the true records (lowest and highest reading, with when). Anything malformed raises here, when the chart is
built, not when it is drawn."""

from dataclasses import dataclass, field
from datetime import date

from .series import RAIN_WITH


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

    def __post_init__(self):
        if len(self.x) != len(self.y) or len(self.x) < 2:
            raise ValueError(f"line {self.label!r}: needs at least two points, x and y the same length")
        if (self.low is None) != (self.high is None) or (self.low is not None and not len(self.low) == len(self.high) == len(self.x)):
            raise ValueError(f"line {self.label!r}: low and high go together and match the points")
        if not set(self.records) <= {"low", "high"}:
            raise ValueError(f"line {self.label!r}: records are 'low' and 'high'")


@dataclass
class Bars:
    label: str
    unit: str
    x: list[int]                                       # each bar's start, epoch seconds
    y: list[float]
    width: int                                         # seconds
    per: str = ""                                      # what a bar covers: "hour", "6 hours", "day"

    def __post_init__(self):
        if len(self.x) != len(self.y):
            raise ValueError(f"bars {self.label!r}: x and y differ in length")


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
    aside: bool = False                                # several lines: peaks labelled in empty space, not as pills on the lines

    def __post_init__(self):
        if not (self.lines or self.bars or self.shares):
            raise ValueError(f"panel {self.label!r}: nothing to draw")
        if self.shares and (self.lines or self.bars):
            raise ValueError(f"panel {self.label!r}: shares draw alone")

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

    def __post_init__(self):
        if not self.panels:
            raise ValueError("a chart needs a panel")
        if self.compass and not (len(self.panels) == 1 and self.panels[0].lines):
            raise ValueError("a compass goes beside a single line panel")


def period_text(a: date, b: date) -> str:
    """'Tue 30 Sep 2026', 'Wed 24 – Tue 30 Sep 2026', 'Thu 27 Aug – Tue 30 Sep 2026'; the year goes on both dates when it differs."""
    if a == b:
        return f"{a:%a} {a.day} {a:%b %Y}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{a:%a} {a.day} – {b:%a} {b.day} {b:%b %Y}"
    if a.year == b.year:
        return f"{a:%a} {a.day} {a:%b} – {b:%a} {b.day} {b:%b %Y}"
    return f"{a:%a} {a.day} {a:%b %Y} – {b:%a} {b.day} {b:%b %Y}"


def rain_behind(panels: list[Panel]) -> list[Panel]:
    """A rain panel goes behind a line panel (the first bars-only panel is drawn there, on its own right-hand axis), so the
    rain lines up with the reading: the one series.RAIN_WITH names first, else the first line. With no line panel it
    stays a panel of its own."""
    lines = [p for p in panels if p.lines and not p.bars]
    line_panel = next((p for name in RAIN_WITH for p in lines if p.reading == name), lines[0] if lines else None)
    bars_panel = next((p for p in panels if p.bars and not p.lines), None)
    if not (line_panel and bars_panel):
        return panels
    line_panel.bars = bars_panel.bars
    return [p for p in panels if p is not bars_panel]


def stack(panels: list[Panel], first: date, last: date) -> Chart:
    """A chart of these panels for the days first to last, rain behind the first line; what a bar covers goes into the
    subtitle."""
    per = next((b.per for p in panels for b in (p.bars, p.shares) if b and b.per), "")
    return Chart(", ".join(p.label for p in panels), period_text(first, last) + (f"  ·  per {per}" if per else ""),
                 rain_behind(panels))
