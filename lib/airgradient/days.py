"""Find, rank, count and total days by the air-quality sensor's readings, from the day rows alone (it never asks
AirGradient; storage.days works each day out as its readings are written, the sensor's glitches taken out). The same fields as a day's
highest (_max), lowest (_min) or mean (_avg) of each metric, e.g. pm2_5_max > 25 for the days particles passed 25 µg/m³. The counting,
ranking, grouping and bar charts are shared with the station's (lib/daytable.py); lib/service/days.py reads the day rows itself."""

import logging
from datetime import datetime, tzinfo

from ..daytable import analyse, prepare
from .metrics import ALL_METRICS, CHART_UNITS

log = logging.getLogger(__name__)

SUFFIX_KIND = ("max", "min", "avg")
FIELDS = {f"{m}_{kind}": CHART_UNITS[m] for m in ALL_METRICS for kind in SUFFIX_KIND}
ALWAYS_SHOWN = ("pm2_5_max",)   # every listed day shows it; days rank by it when nothing else is named

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
