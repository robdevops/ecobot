"""One rule for turning readings into the line a chart draws, whichever device they came from.

A reading is a Sample (epoch, value, low, high, seconds): the value, the lowest and highest reading it stands for (None when
the source gives no range) and how many seconds of time it covers (300 for a 5-minute reading, 1800 for a 30-minute one,
86400 for one of Ecowitt's daily records). `build_line` picks how to draw a whole period of them:
  - up to about 500 points, the readings themselves (a slow 5-minute reading is lightly smoothed);
  - more than that, buckets of 30 minutes, an hour, 2 hours, 4 hours, and past that one point a day (the bucket's mean).
Every line keeps its range as a band at every width: the lowest and highest reading in each bucket, or a reading's own range
when the source gives one (Ecowitt's 30-minute lows and highs; wind: the average speed, shaded up to the gusts). The readings
themselves (up to about 500 points) have a band only where the source gave them a range."""

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from datetime import date, tzinfo
from itertools import accumulate
from statistics import fmean
from typing import NamedTuple

from .specs import Line
from .timeutil import SLOT, at_hour, local_date

DAY = 86400
SMOOTH_POINTS = 3                # a smoothed 5-minute line: each point is the mean of this many readings (15 minutes)
SHORT_DAYS = 31   # up to this long a weather line is drawn with no shaded range (see Plotted.unbanded)
POINT_BUDGET = 500                                  # about as many points as a chart can show legibly
WIDTHS = (300, SLOT, 3600, 7200, 14400, DAY)        # bucket sizes a chart line can be drawn at: 5 min ... a day
WIDTH_NAMES = {300: "5-minute", 1800: "30-minute", 3600: "hourly", 7200: "2-hour", 14400: "4-hour", 86400: "daily"}


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


def bucket_width(span_seconds: float, source_seconds: float, budget: int = POINT_BUDGET) -> int:
    """The finest bucket (never finer than the readings themselves) that keeps a line of this length within the
    point budget; a day is the widest."""
    return next((w for w in WIDTHS if w >= source_seconds and span_seconds / w <= budget), WIDTHS[-1])


def _summarise(points, key) -> dict:
    """{bucket: (mean, lowest low, highest high)} from (ts, value, low, high, fine) readings. The mean takes one value
    per 30-minute slot, so a stretch held as 5-minute readings (fine) counts no more than the same stretch held as
    30-minute ones: a slot's fine readings win where it has them."""
    slots: dict = {}
    extremes: dict = {}
    for ts, value, low, high, fine in points:
        k = key(ts)
        slots.setdefault((k, ts // SLOT), {}).setdefault(fine, []).append(value)
        lo, hi = extremes.get(k, (low, high))
        extremes[k] = (min(lo, low), max(hi, high))
    means: dict = {}
    for (k, _), got in slots.items():
        values = got.get(True) or got[False]
        means.setdefault(k, []).append(fmean(values))
    return {k: (fmean(v), *extremes[k]) for k, v in means.items()}


def daily_summary(points, tz: tzinfo) -> dict[date, tuple[float, float, float]]:
    """{local day: (mean, lowest low, highest high)}; see _summarise."""
    return _summarise(points, lambda ts: local_date(ts, tz))


def bucket_summary(points, width: int) -> dict[int, tuple[float, float, float]]:
    """{bucket start epoch: (mean, lowest low, highest high)} for buckets of `width` seconds; see _summarise."""
    return _summarise(points, lambda ts: ts // width * width)


def bucketed(points, width: int) -> tuple[list[int], list[float], list[float], list[float]]:
    """(x, mean, low, high) lists for a chart line in epoch-aligned buckets of this width (under a day), each drawn at its
    middle. A line of days is _daily's."""
    buckets = bucket_summary(points, width)
    keys = sorted(buckets)
    rows = [buckets[k] for k in keys]
    return [k + width // 2 for k in keys], [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]


def rolling_mean(values: dict[int, float], half_window: int) -> dict[int, float]:
    """Each reading replaced by the mean of the readings within half_window seconds either side of it (itself included):
    a light smoothing of the drawn line. Timestamps are unchanged, nothing is interpolated, and a gap only means fewer
    readings in the window. A reading that is not a finite number is left out."""
    ts = sorted(t for t, v in values.items() if math.isfinite(v))
    prefix = [0.0, *accumulate(values[t] for t in ts)]
    out: dict[int, float] = {}
    for t in ts:
        lo, hi = bisect_left(ts, t - half_window), bisect_right(ts, t + half_window)
        out[t] = (prefix[hi] - prefix[lo]) / (hi - lo)
    return out


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
        xs, mean, low, high = bucketed([(r.t, r.value, *r.bounds(), r.seconds < SLOT) for r in fine], max(width, SLOT))
        return Plotted(xs, mean, max(width, SLOT), False, low, high) if len(xs) >= 2 else None
    line = _as_they_are([r for r in fine if r.seconds == source])
    if line and smooth and source == 300:  # slow readings come in 0.1-degree steps: a light average of the real readings
        smoothed = rolling_mean(dict(zip(line.x, line.y)), SMOOTH_POINTS * source // 2)
        if len(smoothed) == len(line.x):  # every reading was a finite number
            smoothed[line.x[-1]] = line.y[-1]  # the end dot is the latest reading
            line.y, line.smoothed = [smoothed[t] for t in line.x], True
            if line.low is not None:   # the smoothed line stays inside its band
                line.low, line.high = [min(lo, y) for lo, y in zip(line.low, line.y)], [max(hi, y) for hi, y in zip(line.high, line.y)]
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
    points = {at_hour(d, hour, tz): v for d, v in days.items()}
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
