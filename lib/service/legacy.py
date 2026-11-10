"""The bot's old tool calls read as the service's (docs/mcp-service.md 6, step 2): one of the twelve tools' calls, with what the bot's
words decided beside it (the Turn: the reading charted, the readings stacked, an average, every day, two axes), as the service tool
that answers it and that tool's arguments. The bot's own code paths (its shortcuts, lookups and keyboard charts, intent.read's
decisions) and the charts the model draws through the twelve go through here, and nowhere else, so step 3, when the model gets the
service's tools and the Turn goes, deletes this module whole.

  - translate(name, args, turn, now, tz): (tool, arguments) of a call, or None when it has no service equivalent here;
  - lookup(read): the call behind a plain lookup intent.read answers in code ("how hot is it", "co2", "pollen" ...);
  - report(sources): the calls the report is written from;
  - periods_of(tool, arguments): the periods a chart of that call is worth drawing over, as its drawn_by would list them.
"""

from datetime import datetime, timedelta, tzinfo

from ..ecowitt.query import chart_field, stack_names
from ..periods_text import PeriodError, read_period
from ..series import INDOOR_READINGS, WEATHER
from .days import MONTH_PERIODS
from .names import BARE
from .summary import DAILY_MEANS, OVER_TIME

FMT = "%Y-%m-%d %H:%M:%S"
FIELD_NAMES = {r.field: name for name, r in WEATHER.items()}   # a field a chart plots -> its reading's name ("relative" -> "pressure")
SIDES = ("outdoor", "indoor")
RELATED = ({"period": "1m", "label": "1M"}, {"period": "3m", "label": "3M"}, {"period": "1y", "label": "1Y"})


def station_readings(names: list[str], sides: list[str]) -> list[str]:
    """The service's names of lib/series.py's readings, each on the sides asked (temperature and humidity: outdoor and indoor unless
    one is named; dew point and feels like indoor's only when indoor alone is)."""
    out: list[str] = []
    for name in names:
        if name in ("temperature", "humidity"):
            out += [f"{side}.{name}" for side in sides]
        elif name in INDOOR_READINGS and sides == ["indoor"]:
            out.append(f"indoor.{name}")
        elif name in BARE:
            out += BARE[name]
    return list(dict.fromkeys(out))


def _sides(groups) -> list[str]:
    groups = [g.strip() for g in (groups.split(",") if isinstance(groups, str) else groups or [])]
    return [s for s in SIDES if s in groups] or list(SIDES)


def _when(text: str, fmt: str = FMT) -> datetime | None:
    try:
        return datetime.strptime(str(text).strip(), fmt)
    except ValueError:
        try:
            return datetime.fromisoformat(str(text).strip())
        except ValueError:
            return None


def period(start: str | None, end: str | None, words: list[str], now: datetime, tz: tzinfo, whole_days: bool = False) -> dict:
    """The period of an old call (its start_date and end_date, the station's clock) as the service takes it: the first of `words` (the
    period's name as the question gave it) that the service reads as the same span, else whole days as words ("2026-11-04 to 2026-11-10"), else the times."""
    first, last = _when(start) if start else None, _when(end) if end else None
    if first is None:
        return {}
    if whole_days:
        return {"period": f"{first:%Y-%m-%d} to {last:%Y-%m-%d}" if last else f"{first:%Y-%m-%d} to now"}
    now = now.replace(tzinfo=None, microsecond=0)
    until = min(last, now) if last else now
    for said in words:
        try:
            read = read_period(said, None, None, now=now, tz=tz, default=said)
        except PeriodError:
            continue
        if read.start.replace(tzinfo=None) == first and until <= read.end.replace(tzinfo=None) <= until + timedelta(seconds=1):
            return {"period": said}
    whole_end = last is None or last >= now or last.strftime("%H:%M:%S") == "23:59:59"
    if first.time() == datetime.min.time() and whole_end:   # whole days, as words the service can swap for another period
        return {"period": f"{first:%Y-%m-%d} to {last:%Y-%m-%d}" if last else f"{first:%Y-%m-%d} to now"}
    out = {"start": first.strftime("%Y-%m-%d") if first.time() == datetime.min.time() else first.strftime("%Y-%m-%d %H:%M")}
    if last is not None and last < now:
        out["end"] = last.strftime("%Y-%m-%d") if last.strftime("%H:%M:%S") == "23:59:59" else \
            (last + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M")
    return out


def _words(text: str, now: datetime) -> list[str]:
    """The names intent gives the periods the question names (and a chart's default), as candidates for period()."""
    from .. import intent   # (the bot's own reading of the words: lazy, as the service doesn't need it)
    names = [name for name, _, _ in intent.spans_in(text, now)] if text else []
    return [*names, *[n.replace("past ", "the last ") for n in names if n.startswith("past ")], "last 24 hours"]


def translate(name: str, args: dict, turn=None, *, now: datetime, tz: tzinfo) -> tuple[str, dict] | None:
    """(service tool, arguments) for one of the old tools' calls with its Turn (what the words decided; None: nothing), or None."""
    args = dict(args or {})
    text = getattr(turn, "text", "") or ""
    if name == "weather_history":
        return "summary", _history(args, turn, now, tz)
    if name == "air_quality":
        metrics = [m for m in (args.get("metrics") or getattr(turn, "air_named", None) or ["pm2_5"])]
        if not (args.get("start_date") or args.get("end_date")):
            return "current", {"readings": [f"air.{m}" for m in metrics] if args.get("metrics") else ["air"]}
        out = {"readings": [f"air.{m}" for m in metrics],
               **period(args.get("start_date"), args.get("end_date"), _words(text, now), now, tz)}
        if args.get("chart") is not None:
            out["chart"] = bool(args.get("chart"))
        return "summary", _flags(out, args)
    if name in ("weather_days", "air_days"):
        keep = ("where", "sort_by", "order", "count_only", "group_by", "stat", "of", "limit", "only", "chart_answers_it")
        out = {k: args[k] for k in keep if args.get(k) not in (None, "", [])}
        out.update(period(args.get("start_date"), args.get("end_date") or now.strftime("%Y-%m-%d"), [], now, tz, whole_days=True))
        return "days", out
    if name == "weather_now":
        return "current", {}
    if name == "pollen_asthma":
        return "pollen", {}
    if name == "weather_forecast":
        days = int(args.get("days") or 7)
        return "forecast", {"period": "the next 7 days" if days >= 7 else "today" if days == 1 else f"next {days} days"}
    return None


def _flags(out: dict, args: dict) -> dict:
    if args.get("chart_answers_it") is True:
        out["chart_answers_it"] = True
    return out


def _history(args: dict, turn, now: datetime, tz: tzinfo) -> dict:
    """weather_history as summary: the readings the chart draws (the words' reading, else the model's; several stacked; else the
    temperature), on the sides its groups ask for, over its period, with its chart, average, every day and two axes."""
    turn = turn or _NoTurn()
    sides = _sides(args.get("groups"))
    stacked = stack_names(args, turn)
    field = chart_field(args, turn)
    names = stacked or ([FIELD_NAMES.get(field, field)] if field else None) or \
        [n for n in (getattr(turn, "readings", None) or []) if n in WEATHER][:1] or ["temperature"]
    out = {"readings": station_readings(names, sides),
           **period(args.get("start_date"), args.get("end_date"), _words(turn.text or "", now), now, tz)}
    if args.get("chart") is not None or turn.chart_asked:
        out["chart"] = bool(args.get("chart")) or bool(turn.chart_asked)
    if args.get("average") or turn.average_asked:
        out["average"] = True
    if turn.per_day:
        out["every_day"] = True
    if turn.two_axes and len(names) == 2:
        out["layout"] = "two_axes"
    return _flags(out, args)


class _NoTurn:
    text, chart_asked, average_asked, per_day, two_axes, readings, chart_field, chart_fields, air_named = "", False, False, False, False, [], None, [], []


def lookup(read) -> tuple[str, dict] | None:
    """The call behind a plain lookup intent.read answers in code (read.lookup: "reading", "air", "days", "extremes", "pollen",
    "forecast", "about"), from what the words decided."""
    kind = read.lookup
    if kind == "about":
        return "about", {}
    if kind == "reading":
        sides = read.sides or list(SIDES)
        words = [f"{sides[0]} {n.replace('_', ' ')}" if n in INDOOR_READINGS and len(sides) == 1 else n.replace("_", " ")
                 for n in read.lookup_arg]
        return "current", {"readings": words}
    if kind == "air":
        return "current", {"readings": [f"air.{m}" for m in read.lookup_arg] or ["air"]}
    return None


def report(sources) -> list[tuple[str, dict]]:
    """The calls the report is written from, for these sources (by name): the readings now (both devices), the forecast's next 3 days
    (what the report has always shown) and the pollen."""
    calls = [("current", {})] if {"Ecowitt", "AirGradient"} & set(sources) else []
    calls += [("pollen", {})] if "Pollen" in sources else []
    calls += [("forecast", {"period": "next 3 days"})] if "Forecast" in sources else []
    return calls


def periods_of(tool: str, arguments: dict) -> list[dict]:
    """The periods a chart drawn by this call is worth drawing over, as the tool's own drawn_by lists them (summary, days, relate,
    scan): for a chart the model drew through the old tools, which has none of its own."""
    if tool == "summary":
        if "period" not in arguments:
            return []
        return [dict(p) for p in (DAILY_MEANS if arguments.get("average") else OVER_TIME)]
    if tool == "days":
        return [{"period": p, "label": label} for p, label in MONTH_PERIODS] if arguments.get("group_by") == "month" else []
    if tool in ("relate", "scan"):
        return [dict(p) for p in RELATED]
    return []


__all__ = ["lookup", "period", "periods_of", "report", "station_readings", "translate"]
