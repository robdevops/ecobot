"""The `relate` tool (docs/mcp-service.md 3.3): rain against one reading, the two read together at 30-minute resolution over whole days
to yesterday. For pressure, humidity or wind: does rain follow a change in it (lib/ecowitt/link.py, today's weather_link); for PM2.5 or
PM10: is the air cleaner after rain (lib/compose.py Composer.air_link, today's air_link). Their findings come back as they are, in the
service's shape; a chart, when asked, is the reading with rain as bars behind it, carrying the call that drew it (drawn_by, 3.7).

The station's side reads only the file. The air's asks the AirGradient cloud for the finished days the file lacks (up to 30, as today),
unless the request's metadata says `fetch` false; its `priority` changes nothing, as those requests have no queue to wait in.

Also here, for scan, which reads the air through the same Composer: the charts with their drawn_by (drawn_charts), the period a whole-day
call is replayed with (whole_day_words), the Composer of a request (composer_for: from what is held, when its metadata says `fetch`
false: HeldAir).
"""

import asyncio
import copy
import json
from datetime import date, datetime, timedelta

from ..airgradient.metrics import without_spikes
from ..compose import Composer
from ..ecowitt.link import link
from ..periods_text import Period
from ..specs import ECOWITT_SOURCE, Chart
from ..timeutil import local_date, local_epoch
from ..tools import Turn
from .common import Refusal, ServiceTool, answer, period_properties, read_period, sends_period, with_charts
from .context import Context
from .names import Catalogue, Reading
from .readings import first_day

STATION = {"pressure.relative": "pressure", "outdoor.humidity": "humidity", "wind.wind_speed": "wind"}   # -> weather_link's driver
AIR = {"air.pm2_5": "pm2_5", "air.pm10": "pm10"}                                                        # -> air_link's metric
DEFAULT_READING = "pressure.relative"
DEFAULT_PERIOD, AIR_PERIOD = "last 90 days", "last 60 days"
# The periods a chart of rain against a reading is worth redrawing over (3.7)
PERIODS = ({"period": "1m", "label": "1M"}, {"period": "3m", "label": "3M"}, {"period": "1y", "label": "1Y"})
RAIN_NAME = ("rainfall (rainfall.daily)", "rain (rainfall.rain)")   # weather_link's name for the rain in its notes -> the service's

DESCRIPTION = (
    "Rain against one reading, the two read together at 30-minute resolution over whole days (never averaged to days). For pressure, "
    "humidity or wind: does rain follow a change in it (how much of the rain fell after the reading had been falling, steady or "
    "rising over the 3 hours before, its level in rain and when dry, the biggest rain spells with the change before each). For PM2.5 or "
    "PM10: is the air cleaner after rain (the 6 hours after each rain against the 6 before, wet days against dry). Any \"X against "
    "rain\" is this tool. Examples: \"does rain follow a pressure drop?\", \"is there a correlation between pressure and rainfall?\" "
    "{reading: \"pressure\"}; \"does humidity go with rain over the past year?\" {reading: \"humidity\", period: \"past year\"}; "
    "\"does rain clear the air?\" {reading: \"pm2.5\"}; \"plot pressure against rainfall\" {reading: \"pressure\", chart: true}. "
    "Not for: each air reading against every weather reading (scan); a chart of readings over time with no question of rain "
    "(summary); rain totals, wet days or rain per month (days). "
    "Result: open with `verdict` (yes, no or weak) in your own words, then the evidence in `findings`, as `how_to_read` says; "
    "say the two go together or not, never that one causes the other. `note` when there was nothing to compare: say exactly that.")


def parameters() -> dict:
    return {
        "type": "object",
        "properties": {
            "reading": {"type": "string", "description": "The reading set against rain, a name or the word for it: pressure "
                                                         "(pressure.relative, the default), humidity (outdoor.humidity), wind (its "
                                                         "speed, wind.wind_speed), pm2.5 (air.pm2_5) or pm10 (air.pm10)."},
            **period_properties("the last 90 days; for pm2.5 and pm10, the last 60 days", whole_days=True),
            "chart": {"type": "boolean", "description": "true for a chart: the reading as a line, rain as bars behind it. Default "
                                                        "false."},
        },
    }


# ---------- shared with scan ----------
def drawn_charts(charts: list[Chart], ctx: Context, meta: dict, tool: str, arguments: dict, periods=PERIODS) -> dict:
    """The fields an answer with charts ends with (common.with_charts), each chart carrying the call that drew it: `drawn_by: {tool,
    arguments, periods}`, `arguments` the call as the tool read it (the readings' full names, every default filled in), `periods` those
    it is worth redrawing over, each replayed with only `period` changed."""
    fields = with_charts(charts, ctx.zone, meta)
    for chart in fields.get("charts", []):
        chart["drawn_by"] = {"tool": tool, "arguments": copy.deepcopy(arguments), "periods": [dict(p) for p in periods]}
    return fields


def whole_day_words(args: dict, period: Period) -> str:
    """The period of a whole-day call as words a replay reads the same: the words asked (or the default), or, for one made with
    `start` and `end`, "<first day> to <last day>"."""
    if str(args.get("start") or "").strip() and not sends_period(args):   # (`period` wins: common.read_period)
        return f"{period.first_day.isoformat()} to {period.last_day.isoformat()}"
    return period.asked


def flag(value) -> bool:
    """A boolean argument as sent (true, "true")."""
    return value is True or str(value).strip().lower() == "true"


class HeldAir:
    """The air sensor as Composer reads it (`rows`), answering from what the file holds and fetching nothing: for a request whose
    metadata says `fetch` false. The readings are AirGradient.rows' (each stored reading, the particles' glitches taken out); a day
    not held is left out and counted, as one beyond the inline fetch is."""

    def __init__(self, air):
        self.air, self.tz = air, air.tz

    async def rows(self, t0: datetime, t1: datetime) -> tuple[list[dict], int, list[date]]:
        store = self.air.store
        days = [t0.date() + timedelta(days=k) for k in range((t1.date() - t0.date()).days + 1)]
        lo, hi = local_epoch(t0, self.tz), local_epoch(t1, self.tz)
        held = await store.held(days)
        rows = [r for r in without_spikes(await store.load(lo, hi + 1)) if lo <= r["ts"] <= hi]
        have = {local_date(r["ts"], self.tz) for r in rows}
        hourly = sum(1 for h in held.values() if h.n and h.hourly)
        return rows, hourly, [d for d in days if held[d].n is None and d not in have]


def composer_for(ctx: Context, meta: dict, what: str) -> Composer:
    """The Composer that reads the air for this request: the context's, or, when the request's metadata says `fetch` false, one over
    what the file holds (HeldAir). Refusal when there is none: `what` needs both devices."""
    if ctx.composer is None:
        raise Refusal(f"{what} needs both the weather station and the air sensor, and this service has "
                      f"{'only the weather station' if ctx.eco else 'only the air sensor' if ctx.air else 'neither'}.")
    return ctx.composer if meta["fetch"] else Composer(ctx.composer.eco, HeldAir(ctx.composer.air))


# ---------- the reading ----------
def relate_reading(catalogue: Catalogue, words) -> tuple[Reading, list[Reading]]:
    """(the reading set against rain, the others the words name that could be). Raises UnknownReading, or Refusal when the words name
    none that can be."""
    found = catalogue.resolve(words, default=DEFAULT_READING)
    usable = [r for r in found if r.name in STATION or r.name in AIR]
    if not usable:
        can = [n for n in (*STATION, *AIR) if n in catalogue]
        raise Refusal(f"{words!r} ({', '.join(r.name for r in found)}) can't be set against rain: this tool relates rain to one of "
                      f"{', '.join(can)}. For rain itself (totals, wet days) use days; for a chart of readings over time, summary.",
                      readings=can)
    return usable[0], usable[1:]


# ---------- the tool ----------
def tools(ctx: Context) -> list[ServiceTool]:
    async def handle(args: dict, meta: dict) -> str:
        reading, others = relate_reading(ctx.readings(), args.get("reading"))
        air = reading.name in AIR
        if not air and ctx.eco is None:
            raise Refusal(f"{reading.name} is the weather station's, and this service has no weather station.")
        composer = composer_for(ctx, meta, "Setting PM2.5 or PM10 against rain") if air else None
        rain = ctx.readings().get("rainfall.rain")
        now = ctx.now()
        period = read_period(args, tz=ctx.zone, now=now, default=AIR_PERIOD if air else DEFAULT_PERIOD, whole_days=True,
                             first_held=await first_day(ctx, [reading, *([rain] if rain else [])]))
        chart = flag(args.get("chart"))
        dates = {"start_date": period.first_day.isoformat(), "end_date": period.last_day.isoformat()}
        if air:
            turn = Turn()
            out = json.loads(await composer.air_link({"metric": AIR[reading.name], **dates, "chart": chart}, turn))
            charts = turn.charts
        else:
            out, spec, _, _ = await asyncio.to_thread(link, ctx.storage.read_now, ctx.eco.mac, ctx.zone,
                                                      {"driver": STATION[reading.name], **dates}, now)
            charts = [spec] if spec else []
            for c in charts:
                c.source = ECOWITT_SOURCE
        if "error" in out:   # (not expected: the reading and the period were checked here)
            raise Refusal(out["error"])
        if "note" in out:
            out["note"] = out["note"].replace(*RAIN_NAME)
        fields = {"reading": reading.name, "label": reading.label, "unit": reading.unit,
                  **{k: out[k] for k in ("verdict", "how_to_read", "findings") if k in out},
                  **{k: v for k, v in out.items() if k not in ("period", "driver", "metric", "chart", "verdict", "how_to_read", "findings")}}
        if others:
            fields["note_others"] = (f"Set {reading.name} against rain only: one reading a call. For "
                                     f"{', '.join(r.name for r in others)}, call again, once for each, in the same round.")
        drawn = {"reading": reading.name, "period": whole_day_words(args, period), "chart": True}
        return answer(period=period, as_of=now, **fields, **(drawn_charts(charts, ctx, meta, "relate", drawn) if chart else {}))

    return [ServiceTool("relate", DESCRIPTION, parameters(), handle, open_world=ctx.composer is not None)]


__all__ = ["AIR", "DESCRIPTION", "HeldAir", "PERIODS", "STATION", "composer_for", "drawn_charts", "flag", "parameters", "relate_reading",
           "tools", "whole_day_words"]
