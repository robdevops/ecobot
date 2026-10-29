"""air_days: find, rank, count and total days by the air-quality sensor's readings, from the day rows alone (it never asks
AirGradient; storage.days works each day out as its readings are written, the sensor's glitches taken out). The same fields as a day's
highest (_max), lowest (_min) or mean (_avg) of each metric, e.g. pm2_5_max > 25 for the days particles passed 25 µg/m³. The counting,
ranking, grouping and bar charts are shared with weather_days (lib/daytable.py)."""

import logging
from datetime import datetime, tzinfo

from ..daytable import analyse, grouped_as_asked, parameters, prepare
from ..timeutil import now_local
from ..tools import json_result
from .metrics import ALL_METRICS, CHART_UNITS

log = logging.getLogger(__name__)

SUFFIX_KIND = ("max", "min", "avg")
FIELDS = {f"{m}_{kind}": CHART_UNITS[m] for m in ALL_METRICS for kind in SUFFIX_KIND}
ALWAYS_SHOWN = ("pm2_5_max",)   # every listed day shows it; days rank by it when nothing else is named

PARAMETERS = parameters(list(FIELDS), "pm2_5_max > 25 and co2_max > 800")
DESCRIPTION = ("Find, rank or count DAYS by the air-quality sensor's readings, from the stored history (any period, fast): for "
               "questions like \"how many days was PM2.5 over 25\", \"the worst air day each month\", \"days CO2 passed 800\". "
               "Fields: for each of pm1, pm2_5, pm10, co2, voc_index, nox_index a day's highest (_max), lowest (_min) or mean (_avg), "
               "e.g. pm2_5_max >= 25 (µg/m³; a rating's limits are 9 and 55.4 for PM2.5). For \"how many\" use count_only; group_by "
               "month or year breaks it down and draws a bar chart. For a highest, lowest, average or total per month or year use stat "
               "and of (\"worst PM2.5 each month\": stat max, of pm2_5_max; \"average CO2 per year\": stat avg, of co2_avg). Returns the "
               "total of matching days and the top ones.")


async def find_air_days(store, tz: tzinfo, args: dict, now: datetime, turn=None) -> dict:
    """The answer, from one read of the day rows (readings.AirReadings.day_figures)."""
    asked = prepare(args, FIELDS, ALWAYS_SHOWN, now.date())
    if isinstance(asked, str):
        return {"error": asked}
    days, shown = asked.days, asked.shown
    figures = await store.day_figures(asked.first, asked.last, shown)
    values, counts = figures.values, figures.counts
    checked = [d for d in days if any(d in values[n] for n in shown)]
    got = analyse(values, asked, checked, tz, turn)
    out = got.out
    hourly = [d for d in checked if d in figures.hourly]
    if hourly and any(n.endswith(("_max", "_min")) for n in shown):
        out["note_hourly"] = (f"{len(hourly)} of these days only exist as hourly averages, so their highs and lows are hourly "
                              "averages and a short spike is smoothed. Say so briefly if it matters.")
    missing = sum(1 for d in days if not counts.get(d))
    if missing:
        out["note_missing"] = f"{missing} day(s) in this period have no stored readings (not downloaded yet, or the sensor was off) and are not counted."
    if not checked:
        out["note"] = "No stored readings for this period yet (the bot may still be downloading history)."
    log.info("Air days %s to %s: %d checked, %d match", asked.first, asked.last, len(checked), len(got.matches))
    return out


async def air_days_tool(store, tz: tzinfo, args: dict, turn=None) -> str:
    return json_result(await find_air_days(store, tz, grouped_as_asked(args, turn), now_local(tz), turn))
