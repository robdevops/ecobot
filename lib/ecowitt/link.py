"""weather_link: does rain come with a change in another reading (pressure by default)? Both series are read together
from the 30-minute readings held, so it is about WHEN the rain fell, not what a day's extremes were. Nothing here
calls Ecowitt: it works from what the archive holds.

Per 30-minute slot: the rain that fell (the rise in the day's rain total, allowing for its midnight reset) and how
the reading had changed over the previous 3 hours. From those: how much of the rain fell while the reading was
falling, steady or rising; the reading's average level in wet and dry slots; a correlation between the change
before and the rain after; and the biggest rain spells with the change that preceded each."""

import asyncio
import logging
from datetime import date, datetime, time, tzinfo

from ..analysis.pairs import analyse
from ..captions import LINK_CHART_HINT, wants_chart
from ..lines import build_line, slot_readings
from ..rain import rain_bars, rain_slots
from ..panels import panel_for
from ..series import WEATHER
from ..specs import Chart, Line, stack
from ..storage import Storage
from ..storage.read import Reader
from ..timeutil import day_bounds, day_text, local_date, now_local, parse_period, period_parameters
from ..tools import json_result
from .reads import slot_values_on

log = logging.getLogger(__name__)

DEFAULT_DAYS = 90
DRIVERS = {"pressure": 1.0, "humidity": 5.0, "wind": 5.0}   # a reading (series) -> the change over 3 hours that counts as falling or rising

PARAMETERS = {
    "type": "object",
    "properties": {
        **period_parameters(DEFAULT_DAYS),
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


def reading_slots(r: Reader, mac: str, name: str, start: int, end: int) -> tuple[dict, dict, dict]:
    """(values, lows, highs) per 30-minute slot of a reading (series), as every chart of it shades them: the band is the row's (Ecowitt's
    own lows and highs, widened by its 5-minute readings and the live readings the bot and Home Assistant saw). A reading shaded up to
    another field (wind) has its band reach that field's highest (the gusts)."""
    reading = WEATHER[name]
    group, field = reading.group, reading.field
    values, lows, highs = slot_values_on(r, mac, group, field, "30min", start, end)
    if reading.band_field:
        gust, _, gust_high = slot_values_on(r, mac, group, reading.band_field, "30min", start, end)
        highs = {t: max(gust.get(t, 0.0), gust_high.get(t, 0.0)) for t in {*gust, *gust_high}}
    return values, lows, highs


def driver_series(driver: dict[int, float], tz: tzinfo, first: date, last: date, label: str,
                  lows: dict[int, float] | None = None, highs: dict[int, float] | None = None) -> Line | None:
    """The reading as a line (see lines.build_line); a day's mean has its range shaded, from the slots' lows and highs
    (reading_slots) where there are any."""
    line = build_line(slot_readings(driver, lows, highs), tz, ((last - first).days + 1) * 86400)
    return line.spec(label) if line else None


def chart_spec(driver: dict[int, float], rain: dict[int, float], tz: tzinfo, first: date, last: date,
               name: str, lows: dict[int, float] | None = None, highs: dict[int, float] | None = None) -> Chart | None:
    """The reading with the rain behind it."""
    line = driver_series(driver, tz, first, last, WEATHER[name].label, lows, highs)
    if line is None:
        return None
    return stack([panel_for(name, [line]), panel_for("rain", bars=rain_bars(rain, tz, first, last))], first, last)


def link_on(r: Reader, mac: str, name: str, lo: int, hi: int) -> tuple[dict, dict, dict, dict]:
    """(the reading's values, lows, highs, the rain counter's readings) per 30-minute slot from lo to hi: one read."""
    return *reading_slots(r, mac, name, lo, hi), slot_values_on(r, mac, "rainfall", "daily", "30min", lo, hi)[0]


def link(read, mac: str, tz: tzinfo, args: dict, now: datetime) -> tuple[dict, Chart | None, date | None, date | None]:
    """(result for the model, chart spec or None, first day, last day); `read` calls a reader function on the storage."""
    if isinstance(period := parse_period(args, now.date(), DEFAULT_DAYS), str):
        return {"error": period}, None, None, None
    first, last = period
    name = args.get("driver") or "pressure"
    if name not in DRIVERS:
        return {"error": f"driver must be one of {', '.join(DRIVERS)}"}, None, None, None
    reading, threshold = WEATHER[name], DRIVERS[name]
    group, field, unit = reading.group, reading.field, reading.unit
    lo, hi = day_bounds(first, tz)[0], day_bounds(last, tz, last_second=True)[1]
    driver, lows, highs, daily = read(link_on, mac, name, lo, hi)
    rain = rain_slots(daily)
    out = {"period": f"{day_text(first)} - {day_text(last)}", "driver": f"{name} ({unit})",
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


async def link_tool(storage: Storage, mac: str, tz: tzinfo, args: dict, turn) -> str:
    out, spec, first, last = await asyncio.to_thread(link, storage.read_now, mac, tz, args, now_local(tz))
    if spec and wants_chart(args, turn, datetime.combine(first, time()), datetime.combine(last, time())):
        turn.charts.append(spec)
        out["chart"] = LINK_CHART_HINT
    return json_result(out)
