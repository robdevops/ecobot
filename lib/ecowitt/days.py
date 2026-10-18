"""Find, rank and count days by what the station read, from the cache alone (never asks Ecowitt).

Questions like "the hottest day it also rained" or "how many days over 35" compare readings on the
same day across a long period, which the history tool's summaries can't answer. Here each day is
worked out in code from the best data the cache holds for it:
  - 5-minute or 30-minute readings, where cached: exact local days (midnight to midnight);
  - otherwise the daily buckets, which run 10am to 10am local time, so those days are approximate
    (a morning shower can land on the day before). They are flagged in the answer.
The exact range grows by itself as the archive keeps more sub-daily data.
"""

import asyncio
import logging
import operator
import re
from datetime import date, datetime, timedelta, tzinfo
from statistics import fmean

from ..daytable import analyse, grouped_as_asked, parameters, validate
from ..series import WEATHER
from ..timeutil import day_bounds, day_text, local_date, now_local
from ..tools import json_result
from .api import FINE_CYCLES, daily_run
from .calendar import PublicHolidays
from .store import HistoryCache

log = logging.getLogger(__name__)

# friendly name -> (group, field, which extreme of the day, unit)
FIELDS = {
    "temp_max": ("outdoor", "temperature", "high", "°C"),
    "temp_min": ("outdoor", "temperature", "low", "°C"),
    "rain": ("rainfall", "daily", "high", "mm"),        # the day's total: the rain counter's highest reading
    "wind_gust": ("wind", "wind_gust", "high", "km/h"),
}
# Every other reading (series), then three that aren't series readings: a day's highest (_max), its mean (_avg: "average humidity
# per month") and, where its low says something (not 0 every night or calm hour), its lowest (_min): a day's peak UV, its driest
# air, its lowest pressure ...
_OTHERS = [("wind_speed" if name == "wind" else name, r.group, r.field, r.unit, r.low)
           for name, r in WEATHER.items() if name not in ("temperature", "rain")]
_OTHERS += [("rain_rate", "rainfall", "rain_rate", "mm/h", False),
            ("indoor_temp", "indoor", "temperature", "°C", True), ("indoor_humidity", "indoor", "humidity", "%", True)]
for _name, _group, _field, _unit, _low in _OTHERS:
    FIELDS[f"{_name}_max"] = (_group, _field, "high", _unit)
    FIELDS[f"{_name}_avg"] = (_group, _field, "mean", _unit)
    if _low:
        FIELDS[f"{_name}_min"] = (_group, _field, "low", _unit)
FIELDS["temp_avg"] = ("outdoor", "temperature", "mean", "°C")
AVERAGED = {n for n in FIELDS if n.endswith(("_max", "_min")) and n not in ("temp_max", "temp_min")}   # no range of their own at 30 minutes or daily
ALWAYS_SHOWN = ("temp_max", "temp_min", "rain")

PARAMETERS = parameters(list(FIELDS), "rain >= 1 and temp_max > 30", {
    "only": {"type": "string", "enum": ["public_holiday", "weekend"],
             "description": "ONLY when the person's words say holidays or weekends: look just at public holidays in the owner's local area "
                            "(the bot knows them: never pick holiday dates yourself) or just Saturdays and Sundays. Days are then counted "
                            "from those only, so never set it otherwise."}})
DESCRIPTION = ("Find, rank or count DAYS by the station's readings, checking every day in the period: for questions that "
               "compare readings on the same day or count days (\"the hottest day it also rained\", \"how many days over "
               "35°C\", \"how many days was UV 9 or more\", \"the wettest day\", \"the windiest cold day\"). Fields: temp_max / temp_min (outdoor °C), "
               "rain (mm total for the day; a rainy day is 1 mm or more, above 0 is only a trace), wind_gust (km/h, highest), and a day's "
               "highest (_max) or lowest (_min) of every other reading: humidity, pressure, dew_point, feels_like, vpd, "
               "indoor_temp, indoor_humidity (_max and _min); wind_speed, solar, uv, rain_rate (_max only), e.g. uv_max >= 9; and _avg, a day's mean (temp_avg, "
               "humidity_avg, pressure_avg ...). For \"how many\" use count_only; group_by month or year breaks it down and draws a bar chart. "
               "For a total, average, highest or lowest per month or year use stat and of (\"rain per month\": stat sum, of rain; \"hottest day each "
               "year\": stat max, of temp_max; \"average humidity per month\": stat avg, of humidity_avg). Returns the total of matching "
               "days and the top ones. For a record's value and the time it happened (hottest, coldest, fastest gust), use weather_history instead. For ONE known day, give start_date = end_date = that day and no conditions: it returns that day's figures. Can be limited to local public holidays or weekends (only). Works from cached history, so any period up to the whole record is fast.")


def day_figures(cache: HistoryCache, mac: str, cycle: str, names: list[str], days: set[date], tz: tzinfo, start: int,
                end: int) -> dict[str, dict[date, float]]:
    """{reading: {day: its figure}} for each of `days`, from the cached readings between start and end (epochs): the day's highest
    high, lowest low or mean. Each field is read once, however many readings use it (temp_max, temp_min and temp_avg are one),
    and folded into days as it is read, one at a time, so a long period of 5-minute readings is not held whole."""
    readers: dict[tuple[str, str], list[str]] = {}
    for name in names:
        readers.setdefault(FIELDS[name][:2], []).append(name)
    out: dict[str, dict[date, float]] = {}
    for (group, field), wanted in readers.items():
        value, high, low = cache.slots(mac, cycle, group, [field, f"{field}_high", f"{field}_low"], start, end)
        for name in wanted:
            kind = FIELDS[name][2]
            points = value if kind == "mean" else {**value, **(high if kind == "high" else low)}   # a slot's own high or low, if it has one
            figures: dict[date, list[float]] = {}
            for ts, v in points.items():
                if (day := local_date(ts, tz)) in days:
                    got = figures.setdefault(day, [])
                    if kind == "mean" or not got:
                        got.append(v)
                    elif v > got[0] if kind == "high" else v < got[0]:
                        got[0] = v
            out[name] = {d: fmean(v) if kind == "mean" else v[0] for d, v in figures.items()}
    return out


def find_days(cache: HistoryCache, mac: str, tz: tzinfo, args: dict, now: datetime, turn=None) -> dict:
    today = now.date()
    try:
        first = date.fromisoformat(str(args["start_date"])[:10])
        last = min(date.fromisoformat(str(args["end_date"])[:10]), today - timedelta(days=1))
    except (KeyError, ValueError) as e:
        return {"error": f"bad date ({e}); use 'YYYY-MM-DD'"}
    if first > last:
        return {"error": "start_date must be before end_date (today isn't final, so the latest day is yesterday)"}
    where = args.get("where") or []
    if problem := validate(args, {n: f[3] for n, f in FIELDS.items()}):
        return {"error": problem}
    sort_by = args.get("sort_by") or (where[0]["field"] if where else args.get("of") or "temp_max")
    args = {**args, "sort_by": sort_by}
    only = args.get("only")
    if only not in (None, "public_holiday", "weekend"):
        return {"error": "only must be public_holiday or weekend"}
    calendar = PublicHolidays(tz)
    if only == "public_holiday" and not calendar.available:
        return {"error": calendar.problem()}
    kind = {None: lambda d: True, "weekend": lambda d: d.weekday() >= 5,
            "public_holiday": lambda d: calendar.name(d) is not None}[only]
    shown = list(dict.fromkeys([*ALWAYS_SHOWN, sort_by, *(c["field"] for c in where), *([args["of"]] if args.get("of") else [])]))
    groups = sorted({FIELDS[n][0] for n in shown})

    # which cycle each day is answered from: exact sub-daily data where every needed group has it
    days = [first + timedelta(days=k) for k in range((last - first).days + 1)]
    windows = [(cycle, d) for cycle in FINE_CYCLES for d in days]
    source: dict[date, str] = {}
    for (cycle, d), gaps in zip(windows, cache.uncovered(mac, groups, [(cycle, *day_bounds(d, tz, True)) for cycle, d in windows])):
        if d not in source and not any(gaps.values()):
            source[d] = cycle
    rest = {d for d in days if d not in source}

    values: dict[str, dict[date, float]] = {n: {} for n in shown}
    for cycle in FINE_CYCLES:
        mine = {d for d, c in source.items() if c == cycle}
        if mine:
            for n, figures in day_figures(cache, mac, cycle, shown, mine, tz, day_bounds(min(mine), tz, True)[0],
                                          day_bounds(max(mine), tz, True)[1]).items():
                values[n].update(figures)
    if rest:  # daily buckets: labelled with the local date their 10am start falls on
        for n, figures in day_figures(cache, mac, "1day", shown, rest, tz, day_bounds(min(rest), tz, True)[0] - 86400,
                                      day_bounds(max(rest), tz, True)[1] + 86400).items():
            values[n].update(figures)
        for d in rest:
            if any(d in values[n] for n in shown):
                source[d] = "daily"

    checked = [d for d in days if kind(d) and any(d in values[n] for n in shown)]
    units = {n: FIELDS[n][3] for n in shown}
    label = day_text
    got = analyse(values, shown, units, args, checked, first, last, tz, turn,
                  lambda d: {**({"holiday": calendar.name(d)} if only == "public_holiday" else {}),
                             "source": "daily" if source.get(d) == "daily" else "exact"},
                  {"public_holiday": "public holidays only", "weekend": "weekends only"}.get(only, ""))
    out, matches, ranked, rank, holds, row = got.out, got.matches, got.ranked, got.rank, got.holds, got.row

    # Days that would have ranked higher had a trace of rain counted: the answer should mention them
    trace: list[date] = []
    rain_cond = next((c for c in where if c["field"] == "rain" and c["op"] in (">", ">=") and c["value"] > 0), None)
    if rain_cond:
        relaxed = [{"field": "rain", "op": ">", "value": 0} if c is rain_cond else c for c in where]
        matched = set(matches)
        better = operator.lt if args.get("order", "desc") == "asc" else operator.gt
        for d in rank(d for d in checked if holds(d, relaxed)):
            if d in matched:
                break  # from here on the real matches rank at least as high
            if not ranked or better(values[sort_by][d], values[sort_by][ranked[0]]):  # strictly ahead, not a tie
                trace.append(d)
            if len(trace) == 2:
                break

    exact_days = sorted(d for d in checked if source.get(d) != "daily")
    if args.get("count_only") or (args.get("stat") not in (None, "count") and args.get("group_by")):
        trace = []   # just the figures
    if trace:
        out["trace_rain_days"] = [row(d) for d in trace]
        out["note_trace"] = (f"These days rank higher but had less than {rain_cond['value']:g} mm of rain (a trace), so they "
                             "are not in \"days\". Mention the first as a short footnote.")
    if any(source.get(d) == "daily" for d in checked):
        out["exact_from"] = label(exact_days[0]) if exact_days else None
        out["note_daily"] = (f"Days marked \"daily\" come from daily readings that run {daily_run(tz, first, last)}, so their rain and dates "
                             "are approximate (a morning shower can be counted on the day before). Say so briefly if any "
                             "listed day is marked daily, or if the period reaches back before exact_from.")
    if AVERAGED & set(shown) and any(source.get(d) != "5min" for d in checked):
        out["note_averaged"] = ("For these readings, days from 30-minute or daily data use each interval's average, so a day's true peak "
                                "(or low) may be a little beyond what is listed: a count of days at or past a limit can fall slightly short "
                                "near the limit. Say so briefly.")
    if not checked:
        out["note"] = "No cached readings for this period yet (the bot may still be downloading history)."
    log.info("Days %s to %s: %d checked, %d match, %d exact", first, last, len(checked), len(matches), len(exact_days))
    return out


HOLIDAY_WORDS = re.compile(r"\b(holidays?|weekends?|saturdays?|sundays?)\b", re.I)


async def days_tool(cache: HistoryCache, mac: str, tz: tzinfo, args: dict, turn=None) -> str:
    if args.get("only") and turn is not None and turn.text and not HOLIDAY_WORDS.search(turn.text):
        # the model added a holiday or weekend filter nobody asked for: it would count only those days (and, with holidays,
        # only the years the calendar knows), so it is dropped
        log.warning("Ignored only=%s: the question doesn't mention holidays or weekends (%s)", args["only"], turn.text[:60])
        args = {k: v for k, v in args.items() if k != "only"}
    args = grouped_as_asked(args, turn)
    now = now_local(tz)
    result = await asyncio.to_thread(find_days, cache, mac, tz, args, now, turn)
    return json_result(result)
