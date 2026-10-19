"""Local-time helpers shared by the sources: naive local datetimes, epochs and day boundaries."""

from datetime import date, datetime, time, timedelta, tzinfo, UTC


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


def at_hour(day: date, hour: int, tz: tzinfo) -> int:
    """The epoch of `hour` o'clock local time on `day`."""
    return local_epoch(datetime.combine(day, time(hour)), tz)


def day_bounds(day: date, tz: tzinfo, last_second: bool = False) -> tuple[int, int]:
    """Epoch range of one local day: [start, next midnight), or up to its last second if last_second."""
    start = datetime.combine(day, time()).replace(tzinfo=tz)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp()) - last_second


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


SLOT = 1800                                         # the 30-minute slots both devices are lined up on
MIN_DAY_SLOTS = 24                                  # a day counts for a daily comparison with at least 12 hours of readings
