"""The `scan` tool (docs/ecobot-mcp.md 3.3): each air reading against every weather reading, read together at 30-minute resolution
over whole days to yesterday, for the relationships that stand out (lib/compose.py Composer.air_scan, today's air_scan, over
lib/analysis/scan.py). Its verdict and findings come back as they are, in the service's shape; a chart, when asked, is the strongest pair
over time, carrying the call that drew it (drawn_by, 3.7).

At most SCAN_MAX_DAYS days are scanned, as today: a longer period is cut to its latest days, and the result's `period` and
`note_period` say so. The air sensor's missing finished days are fetched (up to 30) unless the request's metadata says `fetch` false
(relate.composer_for); its `priority` changes nothing.
"""

import json
from dataclasses import replace
from datetime import timedelta

from ..compose import SCAN_AIR, SCAN_MAX_DAYS
from ..periods_text import Period
from ..specs import period_text
from ..timeutil import day_bounds, to_local
from ..tools import Turn
from .common import Refusal, ServiceTool, answer, period_properties, read_period
from .context import Context
from .names import Catalogue
from .readings import first_day
from .relate import composer_for, drawn_charts, flag, whole_day_words

SCANNED = {f"air.{m}": m for m in SCAN_AIR}   # the air readings scanned -> air_scan's metric
ALL = "all"
DEFAULT_PERIOD = "last 90 days"

DESCRIPTION = (
    "Which weather goes with air quality: each air reading (PM2.5, PM10, CO2, VOC, NOx) against every weather reading (temperature, "
    "humidity, dew point, pressure and its 3-hour change, wind speed, gusts, wind direction, rain), read together at 30-minute "
    "resolution over whole days, allowing for the daily cycle and for chance. It reports only the relationships that stand out, "
    "strongest first, with a plain verdict. Examples: \"what affects air quality?\" {}; \"is there a correlation between CO2 and the "
    "other readings?\" {reading: \"co2\"}; \"what goes with PM2.5 over the past year? show me\" {reading: \"pm2.5\", period: \"past "
    "year\", chart: true}. "
    f"At most {SCAN_MAX_DAYS} days: a longer period is cut to its latest {SCAN_MAX_DAYS}, and the result says so. "
    "Not for: air quality against rain or precipitation, whether rain clears the air, or rain against any one reading (relate); a chart of readings over time (summary); the air "
    "now (current). "
    "Result: open with `verdict`, then the relationships in `findings`, strongest first, as `how_to_read` says: they go together, "
    "they don't cause each other. With `chart`, the strongest pair over time (none when nothing stands out).")


def parameters() -> dict:
    return {
        "type": "object",
        "properties": {
            "reading": {"type": "string", "description": "The air reading scanned against the weather, a name or the word for it: "
                                                         "pm2.5 (air.pm2_5), pm10 (air.pm10), co2 (air.co2), voc (air.voc_index), nox "
                                                         "(air.nox_index); or all of them, the default."},
            **period_properties(f"the last 90 days; at most {SCAN_MAX_DAYS} days", whole_days=True),
            "chart": {"type": "boolean", "description": "true for a chart of the strongest relationship found, the two readings over "
                                                        "time. Default false."},
        },
    }


def scan_reading(catalogue: Catalogue, words) -> str:
    """"all", or the full name of the one air reading the words name. Raises UnknownReading, or Refusal for words that name no air
    reading scanned, or several."""
    if isinstance(words, (list, tuple)) and len(words) == 1:
        words = words[0]
    if words is None or str(words).strip().lower() in ("", ALL, "every", "everything", "all of them"):
        return ALL
    names = [r.name for r in catalogue.resolve(words, default=ALL)]
    scanned = [n for n in names if n in SCANNED]
    if scanned and set(SCANNED) <= set(names):   # the air sensor named ("air", "airgradient"): all its readings
        return ALL
    if len(scanned) == 1:
        return scanned[0]
    readings = [ALL, *(n for n in SCANNED if n in catalogue)]
    if not scanned:
        raise Refusal(f"{words!r} ({', '.join(names)}) isn't an air reading this tool scans: send one of {', '.join(readings)}. For rain "
                      "against a weather reading use relate.", readings=readings)
    raise Refusal(f"{words!r} names {len(scanned)} air readings: send one, or all.", readings=readings)


def latest_days(period: Period, tz) -> tuple[Period, str | None]:
    """The period cut to its latest SCAN_MAX_DAYS days, and the note that says so (None when it wasn't longer)."""
    first, last = period.first_day, period.last_day
    if (last - first).days < SCAN_MAX_DAYS:
        return period, None
    first = last - timedelta(days=SCAN_MAX_DAYS - 1)
    span = period_text(first, last)
    cut = replace(period, start=to_local(day_bounds(first, tz)[0], tz), text=f"its last {SCAN_MAX_DAYS} days: {span}")
    return cut, (f"Only the last {SCAN_MAX_DAYS} days can be scanned in one call: this covers {span}, not all of {period.text}.")


def tools(ctx: Context) -> list[ServiceTool]:
    async def handle(args: dict, meta: dict) -> str:
        reading = scan_reading(ctx.readings(), args.get("reading"))
        composer = composer_for(ctx, meta, "Scanning the air against the weather")
        air = ctx.readings().get(reading) or ctx.readings().get("air.pm2_5")
        now = ctx.now()
        period = read_period(args, tz=ctx.zone, now=now, default=DEFAULT_PERIOD, whole_days=True,
                             first_held=await first_day(ctx, [air]) if air else None)
        period, note_period = latest_days(period, ctx.zone)
        chart = flag(args.get("chart"))
        turn = Turn()
        out = json.loads(await composer.air_scan({"metric": SCANNED.get(reading, ALL), "start_date": period.first_day.isoformat(),
                                                  "end_date": period.last_day.isoformat(), "chart": chart}, turn))
        if "error" in out:   # (not expected: the reading and the period were checked here)
            raise Refusal(out["error"])
        fields = {"reading": reading, **{k: out[k] for k in ("verdict", "how_to_read", "findings") if k in out},
                  **{k: v for k, v in out.items() if k not in ("period", "chart", "verdict", "how_to_read", "findings")},
                  **({"note_period": note_period} if note_period else {})}
        drawn = {"reading": reading, "period": whole_day_words(args, period), "chart": True}
        return answer(period=period, as_of=now, **fields, **(drawn_charts(turn.charts, ctx, meta, "scan", drawn) if chart else {}))

    return [ServiceTool("scan", DESCRIPTION, parameters(), handle, open_world=True)]


__all__ = ["DESCRIPTION", "SCANNED", "latest_days", "parameters", "scan_reading", "tools"]
