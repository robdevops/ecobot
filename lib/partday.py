"""Part of each day (docs/ecobot-mcp.md 3.3): a sensor's figures over one stretch of local time on each day of a period, worked out as a
day row is from a whole day's readings (lib/storage/days.py), so the overnight low, the nights below 5°C or the 3pm temperature can be
found, ranked and counted as days are.

- **A stretch** is named (night 22:00–06:00, morning 06:00–12:00, afternoon 12:00–18:00, evening 18:00–24:00, daytime 06:00–18:00), two
  times on the half hour ("15:00-16:00", "22:30-06:00"), or a moment: one time on the hour or half hour ("15:00", or "15:00-15:05").
  Times are on the half hour because a 30-minute reading's own lowest and highest run to the end of its half hour: part of one would
  count as all of it.
- **One window a day**, [start, end) in local wall time, read as start to end - 1 (the SQL's BETWEEN takes in its end). "24:00" is the
  next local midnight, so a whole day is 23 or 25 hours on a clock-change day. A stretch that crosses midnight starts the evening before
  and belongs to the day it ends on (Monday 22:00 to Tuesday 06:00 is Tuesday's night). A time the clocks skip is the moment they jump,
  so a stretch the October change removes (02:00–03:00 on its Sunday) has no time and is left out; a time that happens twice is the
  first.
- **Which readings:** each day's window at the first of the day rows' own resolutions that holds all of it (the coverage): the station's
  5-minute, then 30-minute readings; the air sensor's 5-minute, then hourly ones; never the minutes. Held at neither (a day held only as
  Ecowitt's daily or 4-hour records, a night whose evening is before the 30-minute archive): left out. Held but with no readings (the
  station was off): missing. Nothing falls back to whatever readings there are, as a period's part days do (ecowitt/period.py).
- **A moment** is the 5-minute reading at that time; on a day held only at 30 minutes it is the half hour from it, on the air sensor's
  hourly days the hour that holds it, and the note says so.
- **The figures** are the day rows' own SQL over the windows (Reader.part_days, one query for them all): a stretch's mean (of its half
  hours'), lowest, highest and when those happened are worked out as a whole day's are, the air sensor's glitches taken out. Except the
  rain counters: a day's rain is the highest of its running total, which over a stretch would be the rain since midnight up to its end.
  Over a stretch each counter (they all count the one gauge) is the mm that fell in it, the running daily total's rise from the last
  reading before the stretch to its last one in it, a fall counted as the midnight reset (lib/rain.rain_slots, as the readings tool
  works it out).

The cost, over a year of 5-minute readings (tests/bench_cache.py): a night of every day takes about 0.2 s for the outdoor sensor, 0.25 s
for the rain gauge and 0.8 s for the air sensor with its glitch rule (30 nights under 0.07 s); a moment 0.05 s; the whole day 0.55, 0.6
and 2.5 s. The day rows take 0.005 s, so a caller wanting whole days reads those.
"""

import re
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo

from .rain import MAX_GAP, rain_slots
from .storage import Sensor
from .storage.catalogue import RAIN_COUNTERS
from .storage.coverage import merge
from .storage.read import Days, Reader
from .timeutil import local_epoch, naive_local

DAY = 24 * 60     # minutes
MOMENT = 5        # a moment is one 5-minute reading: "15:00" is "15:00-15:05"
NAMED = {"night": (22 * 60, 6 * 60), "morning": (6 * 60, 12 * 60), "afternoon": (12 * 60, 18 * 60), "evening": (18 * 60, DAY),
         "daytime": (6 * 60, 18 * 60)}
ALLOWED = ("night (22:00-06:00), morning (06:00-12:00), afternoon (12:00-18:00), evening (18:00-24:00), daytime (06:00-18:00), two times "
           "on the half hour ('15:00-16:00', '22:30-06:00'; '24:00' is the midnight that ends the day), or a moment on the hour or half "
           "hour ('15:00')")
TIMES = re.compile(r"(\d{1,2}):(\d\d)(?:\s*(?:-|–|—|to)\s*(\d{1,2}):(\d\d))?")
STATION = ("5min", "30min")                 # what a stretch is read from, finest first, as the day rows are
FROM = {"air": ("5min", "1hour")}           # ... where a sensor's day rows differ
SECONDS = {"5min": 300, "30min": 1800, "1hour": 3600}
COARSE = {"30min": ("30-minute averages", "the half hour from it"), "1hour": ("hourly averages", "the hour that holds it")}

NOT_HELD = "not held finely enough"         # why a day is left out: held at neither resolution (daily or 4-hour records only)
CLOCK_CHANGE = "removed by the clock change"
NOT_OVER = "not over yet"


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Stretch:
    """A stretch of each day's local time, in minutes after midnight: an `end` of DAY is the next midnight, and one before the start
    crosses midnight (the stretch starts the evening before). One five minutes long is a moment."""
    start: int
    end: int
    name: str | None = None

    @property
    def moment(self) -> bool:
        return self.end == self.start + MOMENT

    @property
    def overnight(self) -> bool:
        return self.end < self.start

    @property
    def text(self) -> str:
        """The stretch as a result states it: "night (22:00–06:00)", "15:00–16:00", "15:00"."""
        times = _clock(self.start) if self.moment else f"{_clock(self.start)}–{_clock(self.end)}"
        return f"{self.name} ({times})" if self.name else times


def _minutes(hours: str, minutes: str) -> int | None:
    """"15:30" as minutes after midnight ("24:00" the next midnight); None for a time that is no time."""
    found = int(hours) * 60 + int(minutes)
    return found if int(minutes) < 60 and found <= DAY else None


def parse_stretch(text: str) -> Stretch:
    """The stretch these words name (ALLOWED says which); ValueError, saying what is allowed, for anything else."""
    words = " ".join(str(text).lower().split())
    if words in NAMED:
        return Stretch(*NAMED[words], words)
    if found := TIMES.fullmatch(words):
        start = _minutes(found[1], found[2])
        end = _minutes(found[3], found[4]) if found[3] else None if start is None else start + MOMENT
        end = DAY if end == 0 else end   # ("18:00-00:00" runs to midnight)
        if (start is not None and end is not None and start < DAY and start % 30 == 0 and end != start
                and (end % 30 == 0 or end == start + MOMENT)):
            return Stretch(start, end)
    raise ValueError(f"{str(text)!r} is not a part of the day I can read: use {ALLOWED}")


# ---------- each day's window ----------
def clock_time(day: date, minutes: int, tz: tzinfo) -> tuple[int, bool]:
    """(the epoch at which the clock on `day` first reads this time, in minutes after its midnight (DAY: the next midnight, as day_bounds
    has it), and whether it reads it at all): a time the clocks skip is the moment they jump, the first whose clock reads later."""
    local = datetime.combine(day, time()) + timedelta(minutes=minutes)
    t = local_epoch(local, tz)   # (the first, in the hour that happens twice)
    if naive_local(t, tz) == local:
        return t, True
    early = local_epoch(local.replace(fold=1), tz)   # (a skipped time told with the offset after the jump: a moment before it)
    while t - early > 1:
        middle = (early + t) // 2
        early, t = (early, middle) if naive_local(middle, tz) >= local else (middle, t)
    return t, False


def window(stretch: Stretch, day: date, tz: tzinfo, res: str = "5min") -> tuple[int, int] | None:
    """[start, end) (epochs) of the day's stretch read at this resolution, or None where the clock change removes it. A moment is the
    reading that holds it: its 5 minutes, half hour or hour."""
    if stretch.moment:
        t, real = clock_time(day, stretch.start, tz)
        t -= t % SECONDS[res]
        return (t, t + SECONDS[res]) if real else None
    start = clock_time(day - timedelta(days=1) if stretch.overnight else day, stretch.start, tz)[0]
    end = clock_time(day, stretch.end, tz)[0]
    return (start, end) if end > start else None


def over(stretch: Stretch, day: date, tz: tzinfo, now: float) -> bool:
    """Has the day's stretch ended by `now` (epoch)? A caller counts today once it has: asked at 7am, last night is over."""
    return clock_time(day, stretch.end, tz)[0] <= now


def _plan(r: Reader, kind: str, sensor: int, stretch: Stretch, day: date, tz: tzinfo, now: float) -> tuple[str, int, int] | str:
    """(the resolution the day's stretch is read at, its start, its end), or why the day is left out."""
    if window(stretch, day, tz) is None:
        return CLOCK_CHANGE
    for res in FROM.get(kind, STATION):
        start, end = window(stretch, day, tz, res)
        if end > now:
            return NOT_OVER
        if r.held(sensor, res, start, end - 1):
            return res, start, end
    return NOT_HELD


# ---------- the figures ----------
@dataclass
class PartDays:
    """A sensor's figures over a stretch of each day of a period (stretch_figures)."""
    stretch: Stretch
    days: Days = field(default_factory=Days)   # the days worked out, oldest first, each as a day row has it: per reading its mean, lowest
                                               # and highest and when those happened, `n`, and in `source` the resolution read
    windows: dict[date, tuple[int, int]] = field(default_factory=dict)   # each day read: its window, [start, end) epochs
    left_out: dict[date, str] = field(default_factory=dict)              # the days not read, and why (NOT_HELD, CLOCK_CHANGE, NOT_OVER)
    missing: list[date] = field(default_factory=list)                    # the days held but with no readings (the station was off)

    @property
    def note(self) -> str | None:
        """What a moment was on the days held too coarsely for its 5-minute reading, else None."""
        coarse = Counter(res for res in self.days.source if res in COARSE)
        if not self.stretch.moment or not coarse:
            return None
        return f"{self.stretch.text} is the 5-minute reading at that time; " + "; ".join(
            f"on {n} day{'' if n == 1 else 's'} held only as {COARSE[res][0]}, {COARSE[res][1]}" for res, n in sorted(coarse.items())) + "."


def stretch_figures(r: Reader, kind: str, sensor: Sensor | int, first: date, last: date, stretch: Stretch | str,
                    columns: list[str] | None, tz: tzinfo, now: float) -> PartDays:
    """A sensor's figures over the stretch on each day from first to last (a night belongs to the day it ends on), these columns of its
    kind (None: all): one query for the figures, and for the rain counters a look at the running total around each stretch. On the
    reader's thread (Storage.read). A day whose stretch has not ended by `now` (epoch) is left out."""
    stretch = parse_stretch(stretch) if isinstance(stretch, str) else stretch
    sensor = sensor if isinstance(sensor, int) else sensor.id
    out, plans = PartDays(stretch), {}
    for n in range((last - first).days + 1):
        day = first + timedelta(days=n)
        plan = _plan(r, kind, sensor, stretch, day, tz, now)
        if isinstance(plan, str):
            out.left_out[day] = plan
        else:
            plans[day], out.windows[day] = plan, plan[1:]
    got = r.part_days(kind, [(sensor, day, start, end - 1, res) for day, (res, start, end) in plans.items()], columns).get(sensor)
    if got is not None:
        got.complete = [True] * len(got.day)   # (each held whole at the resolution it was read at)
        _rain(r, kind, sensor, got, plans)
        out.days = got
    read = set(out.days.day)
    out.missing = [day for day in plans if day not in read]
    return out


def _rain(r: Reader, kind: str, sensor: int, days: Days, plans: dict[date, tuple[str, int, int]]):
    """The rain counters' figures over each stretch: the mm that fell in it, from the running daily total whichever counter it is (in
    `hi`, where a day row has the day's total), and when the last of it fell (`hi_ts`; None when none did). A total has no mean or
    lowest."""
    counters = [c for c in days.hi if c in RAIN_COUNTERS]
    if not counters:
        return
    spans = merge([(start - MAX_GAP, end - 1) for _, start, end in plans.values()])   # (only what the stretches need: a night is a third)
    totals = {}
    for res in STATION:   # (both: the reading before a stretch may be at the other)
        ts, values = [], []
        for start, end in spans:
            if got := r.rows(kind, [sensor], res, start, end, ["daily"]).get(sensor):
                ts += got.ts
                values += got.values["daily"]
        totals[res] = ts, values
    for i, day in enumerate(days.day):
        mm, when = _fell(totals, *plans[day])
        for c in counters:
            days.mean[c][i] = days.lo[c][i] = days.lo_ts[c][i] = None
            days.hi[c][i], days.hi_ts[c][i] = mm, when


def _fell(totals: dict[str, tuple[list[int], list[float]]], res: str, start: int, end: int) -> tuple[float | None, int | None]:
    """(the mm that fell in [start, end), when the last of it fell) from the running daily totals read at `res`: its rise from the last
    reading before the stretch (at the other resolution where `res` has none close enough) to its last reading in it. None for a stretch
    with no totals."""
    ts, values = totals[res]
    i, j = bisect_left(ts, start), bisect_left(ts, end)
    if i == j:
        return None, None
    running = dict(zip(ts[i:j], values[i:j]))
    for other in (res, *(o for o in totals if o != res)):
        before, held = totals[other]
        k = bisect_left(before, start) - 1
        if k >= 0 and before[k] >= start - MAX_GAP:
            running[before[k]] = held[k]
            break
    fell = {t: mm for t, mm in rain_slots(running).items() if t >= start}
    return round(sum(fell.values()), 3), max((t for t, mm in fell.items() if mm > 0), default=None)


__all__ = ["CLOCK_CHANGE", "NAMED", "NOT_HELD", "NOT_OVER", "PartDays", "Stretch", "clock_time", "over", "parse_stretch",
           "stretch_figures", "window"]
