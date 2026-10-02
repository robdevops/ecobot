"""Rain from the station's running daily total, at 30-minute slots: how much fell in each, which slots make up a
spell, and the bars a chart draws. Shared by the analyses and the charts."""

from datetime import date, tzinfo

from .specs import Bars
from .timeutil import SLOT, day_bounds

SPELL_GAP = 6                  # slots: 3 dry hours end a rain spell
SPELL_MM = 1.0                 # a spell needs this much rain to count as an event
MAX_GAP = 3 * SLOT             # a longer hole in the readings is not counted as rain
CHART_BARS = ((4, 3600), (31, 6 * 3600), (10 ** 6, 86400))  # (up to this many days, seconds per bar)


def rain_slots(daily: dict[int, float]) -> dict[int, float]:
    """Rain per slot from the day's running rain total: the rise since the previous reading, or the whole total
    after the midnight reset. A long hole in the readings gives 0 rather than a guess."""
    out: dict[int, float] = {}
    prev_t = prev = None
    for t in sorted(daily):
        v = daily[t]
        if prev is not None and t - prev_t <= MAX_GAP:
            out[t] = round(v - prev if v >= prev else v, 3)
        prev_t, prev = t, v
    return out


def rain_spells(rain: dict[int, float], wet: list[int]) -> list[list[int]]:
    """The wet slots grouped into spells (3 dry hours end one); only spells of at least SPELL_MM count as events."""
    spells, current = [], []
    for t in wet:
        if current and t - current[-1] > SPELL_GAP * SLOT:
            spells.append(current)
            current = []
        current.append(t)
    if current:
        spells.append(current)
    return [s for s in spells if sum(rain[t] for t in s) >= SPELL_MM]


def bar_layout(tz: tzinfo, first: date, last: date) -> tuple[int, int, str]:
    """(origin epoch, seconds per bar, what a bar covers) for a bar chart of this period: hourly up to 4 days, 6-hourly
    up to a month, daily beyond. Bars start at local midnight of the first day."""
    width = next(w for limit, w in CHART_BARS if (last - first).days + 1 <= limit)
    return (day_bounds(first, tz)[0], width,
            {3600: "hour", 6 * 3600: "6 hours", 86400: "day"}[width])


def rain_bars(rain: dict[int, float], tz: tzinfo, first: date, last: date, until: int | None = None) -> Bars:
    """Rain summed into bars (see bar_layout), one for every stretch of the period, dry ones included (a bar of 0), up to `until`
    (epoch, default the end of the last day)."""
    origin, width, per = bar_layout(tz, first, last)
    end = min(day_bounds(last, tz)[1], until) if until else day_bounds(last, tz)[1]
    bars: dict[int, float] = dict.fromkeys(range(origin, end, width), 0.0)
    for t, mm in rain.items():
        if mm > 0:
            k = origin + (t - origin) // width * width
            bars[k] = bars.get(k, 0.0) + mm
    xs = sorted(bars)
    return Bars("Rain", "mm", xs, [round(bars[k], 2) for k in xs], width, per)
