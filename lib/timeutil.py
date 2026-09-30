"""Local-time helpers shared by the sources: naive local datetimes, epochs and day boundaries."""

from datetime import date, datetime, time, timedelta, timezone, tzinfo


def now_local(tz: tzinfo) -> datetime:
    """The current local time, naive and whole seconds."""
    return datetime.now(tz).replace(tzinfo=None, microsecond=0)


def to_local(ts: int, tz: tzinfo) -> datetime:
    """An epoch as an aware datetime in tz."""
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(tz)


def local_date(ts: int, tz: tzinfo) -> date:
    return to_local(ts, tz).date()


def local_epoch(local: datetime, tz: tzinfo) -> int:
    """A naive local datetime as an epoch."""
    return int(local.replace(tzinfo=tz).timestamp())


def day_bounds(day: date, tz: tzinfo, last_second: bool = False) -> tuple[int, int]:
    """Epoch range of one local day: [start, next midnight), or up to its last second if last_second."""
    start = datetime.combine(day, time()).replace(tzinfo=tz)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp()) - last_second


def daily_summary(points, tz: tzinfo) -> dict[date, tuple[float, float, float]]:
    """{local day: (mean, lowest low, highest high)} from (ts, value, low, high, fine) readings. The mean takes one value
    per 30-minute slot, so a stretch held as 5-minute readings (fine) counts no more than the same stretch held as
    30-minute ones: a slot's fine readings win where it has them."""
    slots: dict = {}
    extremes: dict = {}
    for ts, value, low, high, fine in points:
        day = local_date(ts, tz)
        slots.setdefault((day, ts // 1800), {}).setdefault(fine, []).append(value)
        lo, hi = extremes.get(day, (low, high))
        extremes[day] = (min(lo, low), max(hi, high))
    means: dict = {}
    for (day, _), got in slots.items():
        values = got.get(True) or got[False]
        means.setdefault(day, []).append(sum(values) / len(values))
    return {day: (sum(v) / len(v), *extremes[day]) for day, v in means.items()}
