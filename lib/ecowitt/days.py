"""Find, rank and count days by what the station read, from the storage alone (never asks Ecowitt).

Questions like "the hottest day it also rained" or "how many days over 35" compare readings on the
same day across a long period, which the history tool's summaries can't answer. Each day's figures are
its day row (lib/storage/days.py), worked out from the best readings held for it:
  - 5-minute or 30-minute readings, where they hold the whole day: exact local days (midnight to midnight);
  - otherwise the daily buckets, which run 10am to 10am local time, so those days are approximate
    (a morning shower can land on the day before). They are flagged in the answer.
The exact range grows by itself as the archive keeps more sub-daily data.
"""

import logging
import operator
import re
from datetime import date, datetime, tzinfo

from ..daytable import analyse, grouped_as_asked, lists_days, parameters, prepare
from ..series import WEATHER
from ..storage import Storage
from ..storage.catalogue import column_of
from ..storage.read import Reader
from ..timeutil import day_text, now_local
from ..tools import json_result
from .api import daily_run
from .calendar import PublicHolidays

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
FIELD_UNITS = {n: f[3] for n, f in FIELDS.items()}
ALWAYS_SHOWN = ("temp_max", "temp_min", "rain")   # every listed day shows them; days rank by the first when nothing else is named

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


PARTS = {"high": "hi", "low": "lo", "mean": "mean"}   # a figure's kind -> where a day row (storage.read.Days) keeps it
COARSER = {"5min": 0, "30min": 1, "1day": 2}


def day_values_on(r: Reader, mac: str, shown: list[str], days) -> tuple[dict[str, dict[date, float]], dict[date, str]]:
    """({reading: {day: its figure}}, {day: where it came from}) for these days, from the day rows: a day is "5min" or "30min" where every
    sensor it is asked of has its figures from those (the coarser of them), else "daily" (Ecowitt's daily records: approximate). One
    query per kind of sensor, on the reader's thread."""
    days = set(days)
    values: dict[str, dict[date, float]] = {n: {} for n in shown}
    if not days:
        return values, {}
    places, by_kind = {}, {}
    for name in shown:
        group, field, kind, _ = FIELDS[name]
        sensor, place = r.sensor(mac, group), column_of(group, field)
        if sensor is not None and place is not None:
            places[name] = (sensor, place[0], PARTS[kind])
            by_kind.setdefault(sensor.kind, ({}, set()))[0][sensor.id] = sensor
            by_kind[sensor.kind][1].add(place[0])
    rows = {}
    for kind, (sensors, columns) in by_kind.items():
        rows.update(r.days(kind, list(sensors), min(days), max(days), sorted(columns)))
    source: dict[date, str] = {}
    for name, (sensor, column, part) in places.items():
        got = rows.get(sensor.id)
        if got is None or column not in got.mean:
            continue
        for d, v, src in zip(got.day, getattr(got, part)[column], got.source):
            if v is not None and d in days:
                values[name][d] = v
                source[d] = max(source.get(d, src), src, key=COARSER.get)
    return values, {d: "daily" if s == "1day" else s for d, s in source.items()}


async def find_days(storage: Storage, mac: str, tz: tzinfo, args: dict, now: datetime, turn=None) -> dict:
    asked = prepare(args, FIELD_UNITS, ALWAYS_SHOWN, now.date())
    if isinstance(asked, str):
        return {"error": asked}
    only = args.get("only")
    if only not in (None, "public_holiday", "weekend"):
        return {"error": "only must be public_holiday or weekend"}
    calendar = PublicHolidays(tz)
    if only == "public_holiday" and not calendar.available:
        return {"error": calendar.problem()}
    kind = {None: lambda d: True, "weekend": lambda d: d.weekday() >= 5,
            "public_holiday": lambda d: calendar.name(d) is not None}[only]
    first, last, shown, where, sort_by = asked.first, asked.last, asked.shown, asked.where, asked.sort_by
    days = asked.days
    values, source = await storage.read(day_values_on, mac, shown, days)

    checked = [d for d in days if kind(d) and any(d in values[n] for n in shown)]
    got = analyse(values, asked, checked, tz, turn,
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
    if not lists_days(args):
        trace = []   # just the figures
    if trace:
        out["trace_rain_days"] = [row(d) for d in trace]
        out["note_trace"] = (f"These days rank higher but had less than {rain_cond['value']:g} mm of rain (a trace), so they "
                             "are not in \"days\". Mention the first as a short footnote.")
    if any(source.get(d) == "daily" for d in checked):
        out["exact_from"] = day_text(exact_days[0]) if exact_days else None
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


async def days_tool(storage: Storage, mac: str, tz: tzinfo, args: dict, turn=None) -> str:
    if args.get("only") and turn is not None and turn.text and not HOLIDAY_WORDS.search(turn.text):
        # the model added a holiday or weekend filter nobody asked for: it would count only those days (and, with holidays,
        # only the years the calendar knows), so it is dropped
        log.warning("Ignored only=%s: the question doesn't mention holidays or weekends (%s)", args["only"], turn.text[:60])
        args = {k: v for k, v in args.items() if k != "only"}
    args = grouped_as_asked(args, turn)
    now = now_local(tz)
    result = await find_days(storage, mac, tz, args, now, turn)   # (one read of the day rows; the rest is quick enough to stay here)
    return json_result(result)
