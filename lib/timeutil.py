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


def rolling_range(at: list[int], x: list[int], low: list[float], high: list[float],
                  half_seconds: int) -> tuple[list[float], list[float]]:
    """For each time in `at` (sorted), the lowest low and highest high among the readings (x sorted, with their
    lows and highs) within half_seconds either side of it: the ribbon drawn behind a line to show the variation
    around each point."""
    lo, hi = [], []
    a = b = 0
    for t in at:
        while a < len(x) - 1 and x[a] < t - half_seconds:
            a += 1
        while b < len(x) and x[b] <= t + half_seconds:
            b += 1
        lo.append(min(low[a:max(b, a + 1)]))
        hi.append(max(high[a:max(b, a + 1)]))
    return lo, hi


def day_bounds(day: date, tz: tzinfo, last_second: bool = False) -> tuple[int, int]:
    """Epoch range of one local day: [start, next midnight), or up to its last second if last_second."""
    start = datetime.combine(day, time()).replace(tzinfo=tz)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp()) - last_second
