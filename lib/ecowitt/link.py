"""weather_link: does rain come with a change in another reading (pressure by default)? Both series are read together
from the cached 30-minute data, so it is about WHEN the rain fell, not what a day's extremes were. Nothing here
calls Ecowitt: it works from what the archive holds.

Per 30-minute slot: the rain that fell (the rise in the day's rain total, allowing for its midnight reset) and how
the reading had changed over the previous 3 hours. From those: how much of the rain fell while the reading was
falling, steady or rising; the reading's average level in wet and dry slots; a correlation between the change
before and the rain after; and the biggest rain spells with the change that preceded each."""

import asyncio
import json
import logging
from datetime import date, datetime, time, tzinfo

from ..analysis.pairs import analyse
from ..captions import LINK_CHART_HINT, wants_chart
from ..lines import build_line, slot_readings
from ..rain import rain_bars, rain_slots
from ..panels import panel_for
from ..series import WEATHER
from ..specs import Chart, Line, stack
from ..timeutil import day_bounds, local_date, now_local, parse_period
from .store import HistoryCache

log = logging.getLogger(__name__)

DEFAULT_DAYS = 90
# driver -> (group, field, unit, the change over 3 hours that counts as falling or rising)
DRIVERS = {"pressure": ("pressure", "relative", "hPa", 1.0),
           "humidity": ("outdoor", "humidity", "%", 5.0),
           "wind": ("wind", "wind_speed", "km/h", 5.0)}

PARAMETERS = {
    "type": "object",
    "properties": {
        "start_date": {"type": "string", "description": f"First day, 'YYYY-MM-DD'. Default: the last {DEFAULT_DAYS} days."},
        "end_date": {"type": "string", "description": "Last day, 'YYYY-MM-DD' (up to yesterday). Default: yesterday."},
        "driver": {"type": "string", "enum": list(DRIVERS),
                   "description": "The reading to relate to rain. Default pressure (relative)."},
        "chart": {"type": "boolean", "description": "Set true for a chart: the reading as a line, rain as bars behind it."},
    },
}
DESCRIPTION = ("Does rain come WITH a change in pressure (or humidity or wind)? Reads both together at 30-minute "
               "resolution from the cache and reports how much rain fell while the reading was falling, steady or "
               "rising over the previous 3 hours, the reading's level in wet and dry periods, a correlation, and the "
               "biggest rain spells with the change before each. Use it for 'is there a correlation between pressure and "
               "rainfall', 'did the rain come with the pressure drop' and 'plot pressure against rainfall'. Up to the "
               "whole record; never averaged to days.")


def driver_series(driver: dict[int, float], tz: tzinfo, first: date, last: date, label: str,
                  lows: dict[int, float] | None = None, highs: dict[int, float] | None = None) -> Line | None:
    """The reading as a line (see lines.build_line); a day's mean has its range shaded, from Ecowitt's own 30-minute
    lows and highs where the cache holds them."""
    line = build_line(slot_readings(driver, lows, highs), tz, ((last - first).days + 1) * 86400)
    return line.spec(label) if line else None


def chart_spec(driver: dict[int, float], rain: dict[int, float], tz: tzinfo, first: date, last: date,
               name: str, lows: dict[int, float] | None = None, highs: dict[int, float] | None = None) -> Chart | None:
    """The reading with the rain behind it."""
    line = driver_series(driver, tz, first, last, WEATHER[name].label, lows, highs)
    if line is None:
        return None
    return stack([panel_for(name, [line]), panel_for("rain", bars=rain_bars(rain, tz, first, last))], first, last)


def link(cache: HistoryCache, mac: str, tz: tzinfo, args: dict, now: datetime) -> tuple[dict, Chart | None, date | None, date | None]:
    """(result for the model, chart spec or None, first day, last day)."""
    if isinstance(period := parse_period(args, now.date(), DEFAULT_DAYS), str):
        return {"error": period}, None, None, None
    first, last = period
    name = args.get("driver") or "pressure"
    if name not in DRIVERS:
        return {"error": f"driver must be one of {', '.join(DRIVERS)}"}, None, None, None
    group, field, unit, threshold = DRIVERS[name]
    lo, hi = day_bounds(first, tz)[0], day_bounds(last, tz, last_second=True)[1]
    driver, lows, highs = cache.slots(mac, "30min", group, [field, field + "_low", field + "_high"], lo, hi)
    (daily,) = cache.slots(mac, "30min", "rainfall", ["daily"], lo, hi)
    rain = rain_slots(daily)
    out = {"period": f"{first:%a} {first.day} {first:%b %Y} - {last:%a} {last.day} {last:%b %Y}", "driver": f"{name} ({unit})",
           "resolution": "30-minute readings (not averaged to days)",
           "how_to_read": ("by_change_before groups each 30-minute slot by how the reading had changed over the previous 3 "
                           f"hours: falling = down {threshold:g} {unit} or more, rising = up {threshold:g} or more. share_of_rain "
                           "is the share of all the rain that fell in that group and rain_vs_fair_share compares it with share_of_time. "
                           "Open with `verdict` (Yes / No / Weak) in your own words, then give the evidence from `findings`. The correlation (3-hour change against the next 3 hours' rain) is weak by "
                           "construction, so a small value is NOT evidence of no link: say 'no link' only if findings show it "
                           "too (every group near 1.0x, no lower level in rain).")}
    result = analyse(driver, rain, threshold, tz, unit)
    log.info("Link %s to %s (%s): %d %s readings, %d rain readings, %d slots in common", first, last, name, len(driver),
             field, len(daily), len(set(rain) & set(driver)))
    if not result:
        missing = [what for what, got in ((f"{name} ({group}.{field})", driver), ("rainfall (rainfall.daily)", daily)) if not got]
        out["note"] = ("No cached 30-minute readings of " + " or ".join(missing) + " for this period." if missing else
                       "The two series share no 30-minute readings in this period.") + " Say exactly that."
        return out, None, first, last
    out.update(result)
    out["days_with_data"] = len({local_date(t, tz) for t in rain})
    spec = chart_spec(driver, rain, tz, first, last, name, lows, highs)
    return out, spec, first, last


async def link_tool(cache: HistoryCache, mac: str, tz: tzinfo, args: dict, turn) -> str:
    out, spec, first, last = await asyncio.to_thread(link, cache, mac, tz, args, now_local(tz))
    if spec and wants_chart(args, turn, datetime.combine(first, time()), datetime.combine(last, time())):
        turn.charts.append(spec)
        out["chart"] = LINK_CHART_HINT
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))
