"""air_days: find, rank, count and total days by the air-quality sensor's readings, from the stored readings alone (it never asks
AirGradient). The same fields as a day's highest (_max), lowest (_min) or mean (_avg) of each metric, e.g. pm2_5_max > 25 for the
days particles passed 25 µg/m³. The counting, ranking, grouping and bar charts are shared with weather_days (lib/daytable.py)."""

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, tzinfo

from ..daytable import analyse, parameters, validate
from ..timeutil import day_bounds, local_date, now_local
from .metrics import ALL_METRICS, CHART_UNITS

log = logging.getLogger(__name__)

HOURLY_MAX_READINGS = 30   # a day with this few readings holds hourly averages (24), not 5-minute ones (288)
SUFFIX_KIND = {"max": max, "min": min, "avg": lambda v: sum(v) / len(v)}
FIELDS = {f"{m}_{kind}": CHART_UNITS[m] for m in ALL_METRICS for kind in SUFFIX_KIND}

PARAMETERS = parameters(list(FIELDS), "pm2_5_max > 25 and co2_max > 800")
DESCRIPTION = ("Find, rank or count DAYS by the air-quality sensor's readings, from the stored history (any period, fast): for "
               "questions like \"how many days was PM2.5 over 25\", \"the worst air day each month\", \"days CO2 passed 800\". "
               "Fields: for each of pm1, pm2_5, pm10, co2, voc_index, nox_index a day's highest (_max), lowest (_min) or mean (_avg), "
               "e.g. pm2_5_max >= 25 (µg/m³; a rating's limits are 9 and 55.4 for PM2.5). For \"how many\" use count_only; group_by "
               "month or year breaks it down and draws a bar chart. For a highest, lowest, average or total per month or year use stat "
               "and of (\"worst PM2.5 each month\": stat max, of pm2_5_max; \"average CO2 per year\": stat avg, of co2_avg). Returns the "
               "total of matching days and the top ones.")


def daily_values(rows: list[dict], shown: list[str], tz: tzinfo) -> dict[str, dict[date, float]]:
    """{field: {local day: that day's highest, lowest or mean}} from the stored readings."""
    by_day: dict[date, list[dict]] = {}
    for r in rows:
        by_day.setdefault(local_date(r["ts"], tz), []).append(r)
    values: dict[str, dict[date, float]] = {n: {} for n in shown}
    for name in shown:
        metric, kind = name.rsplit("_", 1)
        for day, day_rows in by_day.items():
            numbers = [r[metric] for r in day_rows if metric in r]
            if numbers:
                values[name][day] = SUFFIX_KIND[kind](numbers)
    return values


def find_air_days(store, tz: tzinfo, args: dict, now: datetime, turn=None) -> dict:
    today = now.date()
    try:
        first = date.fromisoformat(str(args["start_date"])[:10])
        last = min(date.fromisoformat(str(args["end_date"])[:10]), today - timedelta(days=1))
    except (KeyError, ValueError) as e:
        return {"error": f"bad date ({e}); use 'YYYY-MM-DD'"}
    if first > last:
        return {"error": "start_date must be before end_date (today isn't final, so the latest day is yesterday)"}
    if problem := validate(args, FIELDS):
        return {"error": problem}
    where = args.get("where") or []
    sort_by = args.get("sort_by") or (where[0]["field"] if where else args.get("of") or "pm2_5_max")
    args = {**args, "sort_by": sort_by}
    shown = list(dict.fromkeys(["pm2_5_max", sort_by, *(c["field"] for c in where), *([args["of"]] if args.get("of") else [])]))
    days = [first + timedelta(days=k) for k in range((last - first).days + 1)]
    rows = store.load(day_bounds(first, tz)[0], day_bounds(last, tz)[1])
    counts = store.counts(days)
    values = daily_values(rows, shown, tz)
    checked = [d for d in days if any(d in values[n] for n in shown)]
    got = analyse(values, shown, {n: FIELDS[n] for n in shown}, args, checked, first, last, tz, turn)
    out = got.out
    hourly = [d for d in checked if (counts.get(d) or 0) <= HOURLY_MAX_READINGS]
    if hourly and any(n.endswith(("_max", "_min")) for n in shown):
        out["note_hourly"] = (f"{len(hourly)} of these days only exist as hourly averages, so their highs and lows are hourly "
                              "averages and a short spike is smoothed. Say so briefly if it matters.")
    missing = sum(1 for d in days if not counts.get(d))
    if missing:
        out["note_missing"] = f"{missing} day(s) in this period have no stored readings (not downloaded yet, or the sensor was off) and are not counted."
    if not checked:
        out["note"] = "No stored readings for this period yet (the bot may still be downloading history)."
    log.info("Air days %s to %s: %d checked, %d match", first, last, len(checked), len(got.matches))
    return out


async def air_days_tool(store, tz: tzinfo, args: dict, turn=None) -> str:
    result = await asyncio.to_thread(find_air_days, store, tz, args, now_local(tz), turn)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
