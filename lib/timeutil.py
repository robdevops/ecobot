"""Local-time helpers shared by the sources: naive local datetimes, epochs and day boundaries."""

import math
from bisect import bisect_left, bisect_right
from datetime import date, datetime, time, timedelta, tzinfo, UTC
from itertools import accumulate
from statistics import fmean


def now_local(tz: tzinfo) -> datetime:
    """The current local time, naive and whole seconds."""
    return datetime.now(tz).replace(tzinfo=None, microsecond=0)


def to_local(ts: int, tz: tzinfo) -> datetime:
    """An epoch as an aware datetime in tz."""
    return datetime.fromtimestamp(ts, UTC).astimezone(tz)


def day_text(day: date | datetime, year: bool = True) -> str:
    """"Sat 3 Oct 2026", or "Sat 3 Oct" without the year: how the bot writes a day, everywhere."""
    return f"{day:%a} {day.day} {day:%b}" + (f" {day:%Y}" if year else "")


def clock_text(t: datetime, whole_hours: bool = False) -> str:
    """"3:45pm", "12:05am": how the bot writes a time of day; whole_hours writes one on the hour as "10am"."""
    hour, half = t.hour % 12 or 12, "am" if t.hour < 12 else "pm"
    return f"{hour}{half}" if whole_hours and not t.minute else f"{hour}:{t:%M}{half}"


def when(ts: int, tz: tzinfo) -> str:
    """An epoch as "Sun 11 Oct 2026 11:45pm" in tz (the form the tools put in their "time" field)."""
    dt = to_local(ts, tz)
    return f"{day_text(dt)} {clock_text(dt)}"


def local_date(ts: int, tz: tzinfo) -> date:
    return to_local(ts, tz).date()


def naive_local(ts: float, tz: tzinfo) -> datetime:
    """An epoch as a naive local datetime. It keeps `fold`, so local_epoch gives the epoch back exactly, even in the hour that
    happens twice when the clocks go back."""
    return to_local(ts, tz).replace(tzinfo=None)


def parse_local(text, tz: tzinfo) -> datetime:
    """A date or time as sent ("2026-10-01", "2026-10-01 14:00", "2026-10-01T03:00:00Z") as a naive local time: one with an offset
    is moved into tz first. ValueError for anything else."""
    dt = datetime.fromisoformat(str(text))
    return dt.astimezone(tz).replace(tzinfo=None) if dt.tzinfo else dt


def seconds_until(hour: int, minute: int, tz: tzinfo, now: datetime | None = None) -> float:
    """Real seconds from `now` (naive local; default the present) to the next `hour`:`minute` local time. Two clock times of one zone
    are an hour more or less apart than they look across a daylight-saving change; this counts the real seconds."""
    now = now or now_local(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.replace(tzinfo=tz).timestamp() - now.replace(tzinfo=tz).timestamp()


def local_epoch(local: datetime, tz: tzinfo) -> int:
    """A naive local datetime as an epoch."""
    return int(local.replace(tzinfo=tz).timestamp())


def day_bounds(day: date, tz: tzinfo, last_second: bool = False) -> tuple[int, int]:
    """Epoch range of one local day: [start, next midnight), or up to its last second if last_second."""
    start = datetime.combine(day, time()).replace(tzinfo=tz)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp()) - last_second


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


def parse_period(args: dict, today: date, default_days: int) -> tuple[date, date] | str:
    """(first, last) day from start_date / end_date, or an error text. The last day is at most yesterday: today is still
    settling. A missing start is default_days before the end."""
    try:
        last = min(date.fromisoformat(str(args["end_date"])[:10]), today - timedelta(days=1)) if args.get("end_date") else today - timedelta(days=1)
        first = date.fromisoformat(str(args["start_date"])[:10]) if args.get("start_date") else last - timedelta(days=default_days - 1)
    except ValueError as e:
        return f"bad date ({e}); use 'YYYY-MM-DD'"
    return (first, last) if first <= last else "start_date must be before end_date (today isn't final, so the latest day is yesterday)"


def period_parameters(default_days: int) -> dict:
    """The start_date and end_date in a tool's parameters, for a period parse_period reads."""
    return {"start_date": {"type": "string", "description": f"First day, 'YYYY-MM-DD'. Default: {default_days} days before the end."},
            "end_date": {"type": "string", "description": "Last day, 'YYYY-MM-DD' (up to yesterday). Default: yesterday."}}


POINT_BUDGET = 500                                  # about as many points as a chart can show legibly
WARN_POINTS = 2 * POINT_BUDGET                      # a question that sends the model more than twice a 500-point query's data gets a heads-up
TOKENS_PER_POINT = 10                               # a dated low/high row is about 10 tokens as the model reads it
CHARS_PER_TOKEN = 4
WARN_TOKENS = WARN_POINTS * TOKENS_PER_POINT        # 10,000 tokens: about $0.0125 of input at Grok 4.3's $1.25 per million, and it rides on every later step
SLOT = 1800                                         # the 30-minute slots both devices are lined up on
MIN_DAY_SLOTS = 24                                  # a day counts for a daily comparison with at least 12 hours of readings
WIDTHS = (300, SLOT, 3600, 7200, 14400, 86400)      # bucket sizes a chart line can be drawn at: 5 min ... a day
WIDTH_NAMES = {300: "5-minute", 1800: "30-minute", 3600: "hourly", 7200: "2-hour", 14400: "4-hour", 86400: "daily"}


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


def bucketed(points, tz: tzinfo, width: int) -> tuple[list[int], list[float], list[float], list[float]]:
    """(x, mean, low, high) lists for a chart line in buckets of this width: local days (drawn at noon) for a day,
    otherwise epoch-aligned buckets (drawn at their middle)."""
    if width >= 86400:
        days = daily_summary(points, tz)
        keys = sorted(days)
        xs = [local_epoch(datetime.combine(d, time(12)), tz) for d in keys]
        rows = [days[d] for d in keys]
    else:
        buckets = bucket_summary(points, width)
        keys = sorted(buckets)
        xs = [k + width // 2 for k in keys]
        rows = [buckets[k] for k in keys]
    return xs, [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]
