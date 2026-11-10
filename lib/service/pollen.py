"""The `pollen` tool (docs/mcp-service.md 3.3): Melbourne's grass pollen and the thunderstorm asthma risk, from the pollen source
(lib/pollen, melbournepollen.com.au). No arguments.

The answer is today's pollen_asthma's, in the service's shape: the same `lines` (Pollen.lines), each level with its emoji and its
day in `grass` and `asthma`, `in_season`, `fetched_at`, `place` and `source`, and `text`, the bot's pollen section ("Pollen & asthma
(Melbourne, 6:00pm)" and a bullet per line, lib/report.py's report and lookup) written here so a client can send it as it is. Out of
season (the forecasts run October to December) it answers as pollen_asthma does, with its error. Request `_meta` `fetch: false`
answers from the page held, fetching nothing (the report's `cached`).
"""

import logging
from calendar import month_name

from ..pollen.parse import LEVELS
from ..pollen.source import OFF_SEASON, SEASON_MONTHS
from ..timeutil import day_text, to_local
from .common import Refusal, ServiceTool, answer, iso
from .forecast import clock_of, section

log = logging.getLogger(__name__)

HEADING = "Pollen & asthma"   # the bot's heading for it (lib/report.py SECTIONS)
NO_LEVELS = "No pollen levels are available right now."   # (pollen_asthma's own words)
SOURCE = ("melbournepollen.com.au (grass pollen from pollen-trap data; asthma risk from the Victorian Department of Health and BOM, "
          "1 Oct - 31 Dec)")   # (pollen_asthma's)
NO_ASTHMA = "The page had no thunderstorm asthma level for this district when it was last read."   # (pollen_asthma's asthma_note)
SEASON = f"{month_name[SEASON_MONTHS[0]]} to {month_name[SEASON_MONTHS[-1]]}"

DESCRIPTION = (
    "Melbourne's grass pollen level and the thunderstorm asthma risk for the station's district, each Low, Moderate, High or Extreme, "
    "from melbournepollen.com.au. For \"pollen today\", \"is it bad for hay fever\", \"thunderstorm asthma risk\": {}. Takes no "
    "arguments. The forecasts run only from October to December: out of season it says so, as an error. A report (\"report\", "
    "\"status\", \"sitrep\") is current, forecast {\"period\": \"today\"} and pollen, called together.\n"
    "Not for: air quality, PM2.5 and the like (the air sensor: current, summary); the weather forecast (forecast).\n"
    "Result: `text`, the whole answer for the person, ready to send; `lines`, emoji included: copy them as they are; `grass` and "
    "`asthma`, each level with its emoji (grass with `day`, the day its forecast is for; asthma with `updated`, when the site last "
    "updated it); `place`, `fetched_at`; `asthma_note` when the page had no asthma level.")
PARAMETERS = {"type": "object", "properties": {}}


def tools(ctx) -> list[ServiceTool]:
    """The `pollen` tool, when the pollen source is switched on."""
    if ctx.pollen is None:
        return []

    async def handle(args: dict, meta: dict) -> str:
        p = ctx.pollen
        if not p.in_season():
            raise Refusal(OFF_SEASON)
        if p.page is None and meta["fetch"]:   # (as pollen_asthma does: the page is fetched only when this season's isn't held)
            try:
                await p.warm(True)
            except Exception as e:
                log.warning("Pollen not readable: %s", e)
        lines = p.lines()
        if not lines:
            raise Refusal(NO_LEVELS)
        tz, now = clock_of(ctx, p.tz)
        cur = p.current()
        fetched = to_local(int(cur["fetched_at"]), tz) if cur["fetched_at"] else None
        levels = {}
        if g := cur["grass"]:   # (its day: the day the page's grass forecast is for)
            levels["grass"] = {"level": g["level"], "emoji": g["emoji"],
                               **({"day": g["date"].isoformat(), "day_text": day_text(g["date"])} if g["date"] else {})}
        if a := cur["asthma"]:
            levels["asthma"] = {"level": a["level"], "emoji": a["emoji"], **({"updated": a["updated"]} if a["updated"] else {})}
        return answer(period=None, as_of=fetched,
                      text=section(HEADING, lines, place=p.place, fetched_at=cur["fetched_at"], tz=tz, today=now.date()),
                      lines=lines, grass=levels.get("grass"), asthma=levels.get("asthma"),
                      **({} if "asthma" in levels else {"asthma_note": NO_ASTHMA}),
                      scale=list(LEVELS), in_season=True, season=SEASON,
                      **({"place": p.place} if p.place else {}), source=SOURCE,
                      **({"fetched_at": iso(fetched)} if fetched else {}))

    return [ServiceTool("pollen", DESCRIPTION, PARAMETERS, handle, open_world=True)]


__all__ = ["DESCRIPTION", "PARAMETERS", "tools"]
