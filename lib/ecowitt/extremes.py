"""A record (a lowest or highest reading) and when it happened, in words."""

from datetime import datetime, tzinfo, UTC
from typing import NamedTuple

from ..timeutil import clock_text, day_text
from .api import MINUTE, cycle_seconds


class Ext(NamedTuple):
    value: float
    raw: str        # as the station writes it (the catalogue's decimals), so no rounding artefacts
    ts: int
    cycle: str
    exact: bool     # a real reading or a bucket's true low/high, not an average


def low_of(rec: dict) -> float:
    return rec["low"][0] if "low" in rec else rec["value"][0]


def high_of(rec: dict) -> float:
    return rec["high"][0] if "high" in rec else rec["value"][0]


_clock = lambda dt: clock_text(dt, whole_hours=True)   # "7:05am", "3:30pm", "10am"


def describe_time(ts: int, cycle: str, tz: tzinfo) -> tuple[str, str, str]:
    """(raw local time, ready-made wording, date) for an extreme found in a point of this
    cycle. The wording already says "at" (5-minute readings), "around" (30-minute slots) or gives
    the window it happened in, so the model can copy it as is. The date is separate (so an emoji
    can go before it) and empty when the window spans two days, since then the wording has both."""
    start = datetime.fromtimestamp(ts, UTC).astimezone(tz)
    raw = f"{day_text(start)} {clock_text(start)}"
    if cycle in ("5min", MINUTE):
        return raw, f"at {_clock(start)}", day_text(start)
    if cycle == "30min":
        return raw, f"around {_clock(start)}", day_text(start)
    end = datetime.fromtimestamp(ts + cycle_seconds(cycle), UTC).astimezone(tz)
    if start.date() == end.date():
        return raw, f"sometime between {_clock(start)} and {_clock(end)}", day_text(start)
    return raw, f"sometime between {_clock(start)} {day_text(start)} and {_clock(end)} {day_text(end)}", ""
