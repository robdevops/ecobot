"""Turning Ecowitt readings into per-series points and finding their lowest and highest, with when each happened."""

from datetime import date, datetime, tzinfo, UTC
from typing import NamedTuple

from ..timeutil import local_date
from .api import CYCLE_SECONDS
from .fetch import series_in


class Ext(NamedTuple):
    value: float
    raw: str        # as Ecowitt gave it, so no rounding artefacts
    ts: int
    cycle: str
    exact: bool     # a real reading or a bucket's true low/high, not an average


def collect(store: dict, data: dict, cycle: str):
    """Fold an Ecowitt 'data' object into store["group.field"] = {unit, pts: {ts: rec}}.
    rec holds value / low / high as (float, raw string) and the source cycle."""
    for group, fname, fobj in series_in(data):
        base, kind = fname, "value"
        if fname.endswith("_low"):
            base, kind = fname[:-4], "low"
        elif fname.endswith("_high"):
            base, kind = fname[:-5], "high"
        series = store.setdefault(f"{group}.{base}", {"unit": fobj.get("unit", ""), "pts": {}})
        for ts, raw in fobj["list"].items():
            try:
                rec = series["pts"].setdefault(int(ts), {"cycle": cycle})
                rec[kind] = (float(raw), str(raw))
            except (TypeError, ValueError):
                continue


def better(want: str, a: float, b: float) -> bool:
    return a < b if want == "low" else a > b


def fold(into: dict, ts: int, rec: dict):
    """Update into["low"/"high"] with this point's low/high if more extreme."""
    for want in ("low", "high"):
        if want in rec:
            found = (*rec[want], True)
        elif "value" in rec:
            found = (*rec["value"], rec["cycle"] == "5min")  # 5-minute readings are real readings
        else:
            continue
        if want not in into or better(want, found[0], into[want].value):
            into[want] = Ext(found[0], found[1], ts, rec["cycle"], found[2])


def series_extremes(series: dict, lo_ts: int | None = None, hi_ts: int | None = None) -> dict[str, Ext]:
    """Overall low/high of a series (optionally only points with lo_ts <= ts <= hi_ts)."""
    out: dict = {}
    for ts, rec in series["pts"].items():
        if lo_ts is None or lo_ts <= ts <= hi_ts:
            fold(out, ts, rec)
    return out


def low_of(rec: dict) -> float:
    return rec["low"][0] if "low" in rec else rec["value"][0]


def high_of(rec: dict) -> float:
    return rec["high"][0] if "high" in rec else rec["value"][0]


def daily_readings(pts: dict, before: date | None = None, tz: tzinfo | None = None) -> list[tuple]:
    """(ts, value, low, high, fine) for the 5- and 30-minute readings (those before `before`), for daily_summary."""
    return [(t, r["value"][0], low_of(r), high_of(r), r["cycle"] == "5min") for t, r in pts.items()
            if r["cycle"] != "1day" and "value" in r and (before is None or local_date(t, tz) < before)]


def _clock(dt: datetime) -> str:
    """12-hour clock, e.g. '7:05am', '3:30pm', '10am'."""
    text = dt.strftime("%I:%M%p").lstrip("0").lower()
    return text.replace(":00", "") if dt.minute == 0 else text


def _day(dt: datetime) -> str:
    return f"{dt:%a} {dt.day} {dt:%b %Y}"


def describe_time(ts: int, cycle: str, tz: tzinfo) -> tuple[str, str, str]:
    """(raw local time, ready-made wording, date) for an extreme found in a point of this
    cycle. The wording already says "at" (5-minute readings), "around" (30-minute slots) or gives
    the window it happened in, so the model can copy it as is. The date is separate (so an emoji
    can go before it) and empty when the window spans two days, since then the wording has both."""
    start = datetime.fromtimestamp(ts, UTC).astimezone(tz)
    raw = start.strftime("%a %d %b %Y %H:%M")
    if cycle == "5min":
        return raw, f"at {_clock(start)}", _day(start)
    if cycle == "30min":
        return raw, f"around {_clock(start)}", _day(start)
    end = datetime.fromtimestamp(ts + CYCLE_SECONDS[cycle], UTC).astimezone(tz)
    if start.date() == end.date():
        return raw, f"sometime between {_clock(start)} and {_clock(end)}", _day(start)
    return raw, f"sometime between {_clock(start)} {_day(start)} and {_clock(end)} {_day(end)}", ""
