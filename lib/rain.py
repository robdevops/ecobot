"""Rain from the station's running daily total, at 30-minute slots: how much fell in each, which slots make up a
spell, and the bars a chart draws. Shared by the analyses and the charts."""

from bisect import bisect_right
from datetime import date, datetime, time, timedelta, tzinfo

from .specs import Bars, calendar_bars
from .timeutil import SLOT, day_bounds, local_epoch, to_local

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


def bar_layout(tz: tzinfo, first: date, last: date) -> tuple[list[int], int, str]:
    """(each bar's start, seconds per bar, what a bar covers) for a bar chart of this period: hourly up to 4 days, 6-hourly up
    to a month, daily beyond. The bars start at the same local clock times every day (midnight, 6am ...) from midnight of the
    first day, so a daylight-saving change doesn't move them (a day of 23 or 25 hours has one bar the shorter or longer)."""
    width = next(w for limit, w in CHART_BARS if (last - first).days + 1 <= limit)
    starts = {local_epoch(datetime.combine(first + timedelta(days=n), time(hour)), tz)
              for n in range((last - first).days + 1) for hour in range(0, 24, width // 3600)}
    return sorted(starts), width, {3600: "hour", 6 * 3600: "6 hours", 86400: "day"}[width]


def bar_of(starts: list[int], t: int) -> int | None:
    """The start of the bar that holds epoch `t` (None before the first)."""
    i = bisect_right(starts, t) - 1
    return starts[i] if i >= 0 else None


def rain_bars(rain: dict[int, float], tz: tzinfo, first: date, last: date, until: int | None = None, by: str | None = None,
              since: int | None = None) -> Bars:
    """Rain summed into bars (see bar_layout), one for every stretch of the period, dry ones included (a bar of 0), up to `until`
    (epoch, default the end of the last day). The bars start at local midnight of the first day, or with `since` (epoch, the period's
    own start) at the bar that holds it: a rolling last 24 hours has no bars left of its data."""
    if by in ("month", "year"):   # a figure per month or year was asked for: one bar for each, whatever the length
        return _calendar_bars(rain, tz, first, last, by)
    starts, width, per = bar_layout(tz, first, last)
    end = min(day_bounds(last, tz)[1], until) if until else day_bounds(last, tz)[1]
    begin = bar_of(starts, since) if since is not None and since > starts[0] else starts[0]
    bars: dict[int, float] = dict.fromkeys((s for s in starts if begin <= s < end), 0.0)
    for t, mm in rain.items():
        if mm > 0 and begin <= t < end:   # rain before the period's start is not part of it
            bars[bar_of(starts, t)] += mm
    return Bars("Rain", "mm", list(bars), [round(v, 2) for v in bars.values()], width, per)


def _calendar_bars(rain: dict[int, float], tz: tzinfo, first: date, last: date, by: str) -> Bars:
    """Rain summed per calendar month or year over the period, each with its total above its bar (every month or year of the
    period has one, a dry one 0)."""
    start = (lambda d: d.replace(day=1)) if by == "month" else (lambda d: d.replace(month=1, day=1))
    totals = dict.fromkeys((start(first + timedelta(days=n)) for n in range((last - first).days + 1)), 0.0)
    for t, mm in rain.items():
        if mm > 0 and first <= (d := to_local(t, tz).date()) <= last:
            totals[start(d)] += mm
    return calendar_bars("Rain", "mm", {d: round(v, 1) for d, v in totals.items()}, by, tz)
