"""One rule for turning readings into the line a chart draws, whichever device they came from.

A reading is a Sample (epoch, value, low, high, seconds): the value, the lowest and highest reading it stands for (None when
the source gives no range) and how many seconds of time it covers (300 for a 5-minute reading, 1800 for a 30-minute one,
86400 for one of Ecowitt's daily records). `build_line` picks how to draw a whole period of them:
  - up to about 500 points, the readings themselves (a slow 5-minute reading is lightly smoothed);
  - more than that, buckets of 30 minutes, an hour, 2 hours, 4 hours, and past that one point a day (the bucket's mean).
Every line keeps its range as a band at every width: the lowest and highest reading in each bucket, or a reading's own range
when the source gives one (Ecowitt's 30-minute lows and highs; wind: the average speed, shaded up to the gusts). The readings
themselves (up to about 500 points) have a band only where the source gave them a range."""

from dataclasses import dataclass, replace
from datetime import date, datetime, time
from typing import NamedTuple

from .specs import Line
from .timeutil import SLOT, WIDTH_NAMES, bucket_width, bucketed, daily_summary, local_date, local_epoch, rolling_mean

DAY = 86400
SMOOTH_POINTS = 3                # a smoothed 5-minute line: each point is the mean of this many readings (15 minutes)
SHORT_DAYS = 31   # up to this long a weather line is drawn with no shaded range (see Plotted.unbanded)


class Sample(NamedTuple):
    t: int                  # epoch seconds
    value: float
    low: float | None       # the lowest and highest reading it stands for, None when the source gives no range
    high: float | None
    seconds: int            # the time it covers

    def bounds(self) -> tuple[float, float]:
        """(low, high), the value itself for a side the source gave no range."""
        return (self.value if self.low is None else self.low), (self.value if self.high is None else self.high)


@dataclass
class Plotted:
    x: list[int]
    y: list[float]
    width: int                   # seconds per point (the readings' own for a raw line)
    raw: bool                    # the readings themselves, not buckets
    low: list[float] | None = None
    high: list[float] | None = None
    smoothed: bool = False

    def unbanded(self, days: float, keep: bool = False) -> "Plotted":
        """The line without its shaded range when the period is SHORT_DAYS or less (a short period reads best as a clean line); `keep`
        leaves it (wind: the mean shaded up to the gusts). A new line: this one is left as it is."""
        return self if keep or days > SHORT_DAYS else replace(self, low=None, high=None)

    @property
    def name(self) -> str:
        """How the line was drawn, for a subtitle: "5-minute readings", "30-minute readings", "hourly averages" ..."""
        if self.raw and self.width <= SLOT:
            return f"{WIDTH_NAMES.get(self.width, f'{self.width // 60}-minute')} readings"
        return f"{WIDTH_NAMES.get(self.width, 'daily')} averages"

    def spec(self, label: str, records: dict | None = None, reading: str = "", indoor: bool = False) -> Line:
        """The line as a chart draws it."""
        return Line(label, self.x, self.y, self.low, self.high, self.smoothed, records or {}, reading, indoor)


def slot_readings(values: dict[int, float], lows: dict[int, float] | None = None, highs: dict[int, float] | None = None,
                  seconds: int = SLOT) -> list[Sample]:
    """Readings from {epoch: value} maps, all covering `seconds`; the range is what the maps hold (taken out to the value where it
    falls short of it), else None."""
    lows, highs = lows or {}, highs or {}
    return [Sample(t, v, None if t not in lows else min(lows[t], v), None if t not in highs else max(highs[t], v), seconds)
            for t, v in values.items()]


def build_line(readings: list[Sample], tz, span_seconds: float, *, smooth: bool = False,
               force_daily: bool = False, until: date | None = None) -> Plotted | None:
    """The line for these readings (Samples, or tuples in their order) over a period of span_seconds; None when there is too
    little to draw. force_daily: one point a day whatever the length (an average was asked for). until: with daily records in
    the mix, days from this one on are not averaged from sub-daily readings (the day is not over)."""
    rs = sorted(map(Sample._make, readings))
    fine = [r for r in rs if r.seconds < DAY]
    records = [r for r in rs if r.seconds >= DAY]
    if not fine:  # only daily records: as they are, each with its own range
        return _as_they_are(records) if len(records) >= 2 else None
    source = min(r.seconds for r in fine)
    width = DAY if records or force_daily else bucket_width(span_seconds, source)
    if width >= DAY:
        return _daily(fine, records, tz, until)
    if width > source or len({r.seconds for r in fine}) > 1:  # too many for the chart, or two resolutions side by side
        xs, mean, low, high = bucketed([(r.t, r.value, *r.bounds(), r.seconds < SLOT) for r in fine], tz, max(width, SLOT))
        return Plotted(xs, mean, max(width, SLOT), False, low, high) if len(xs) >= 2 else None
    line = _as_they_are([r for r in fine if r.seconds == source])
    if line and smooth and source == 300:  # slow readings come in 0.1-degree steps: a light average of the real readings
        smoothed = rolling_mean(dict(zip(line.x, line.y)), SMOOTH_POINTS * source // 2)
        if len(smoothed) == len(line.x):  # every reading was a finite number
            smoothed[line.x[-1]] = line.y[-1]  # the end dot is the latest reading
            line.y, line.smoothed = [smoothed[t] for t in line.x], True
    return line


def _as_they_are(rs: list[Sample]) -> Plotted | None:
    if len(rs) < 2:
        return None
    ranges = [r.bounds() for r in rs]
    ranged = sum(1 for r in rs if r.low is not None or r.high is not None) >= len(rs) // 2
    return Plotted([r.t for r in rs], [r.value for r in rs], rs[0].seconds, True,
                [lo for lo, _ in ranges] if ranged else None, [hi for _, hi in ranges] if ranged else None)


def _daily(fine: list[Sample], records: list[Sample], tz, until: date | None) -> Plotted | None:
    """One point a day. Days held as sub-daily readings get their own mean, lowest and highest over the local day; the
    older days come from the daily records, each with its own range."""
    keep = [r for r in fine if until is None or local_date(r.t, tz) < until]
    days = daily_summary(((r.t, r.value, *r.bounds(), r.seconds < SLOT) for r in keep), tz)
    hour = 10 if records else 12  # Ecowitt's daily records are 10am-10am windows: draw the exact days where they sit
    points = {local_epoch(datetime.combine(d, time(hour)), tz): v for d, v in days.items()}
    for r in records:
        if local_date(r.t, tz) not in days:
            points[r.t] = (r.value, *r.bounds()) if r.low is not None and r.high is not None else (r.value, None, None)
    xs = sorted(points)
    if len(xs) < 2:
        return None
    ranged = sum(1 for t in xs if points[t][1] is not None) >= len(xs) // 2
    return Plotted(xs, [points[t][0] for t in xs], DAY, False,
                [points[t][1] if points[t][1] is not None else points[t][0] for t in xs] if ranged else None,
                [points[t][2] if points[t][2] is not None else points[t][0] for t in xs] if ranged else None)
