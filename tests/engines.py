"""lib's engines driven directly, for their own tests: the station's HistoryQuery over the groups asked for (with those its chart
needs), the reading-against-rain link (lib/ecowitt/link.py), and the air sensor's history and reading now (AirGradient.history, latest).
The service's tools run these same engines (lib/service/summary.py, relate.py, current.py) and have tests of their own.

Until step 6c these drivers were part of tests/old_tools.py, the old tools' handlers kept as the oracle the service's words were ported
against; the oracle (the old words, the old day finders and readings handler) is deleted, and only the engines' drivers are kept here."""

import asyncio
from datetime import datetime, time

from lib.airgradient.metrics import EXTRA
from lib.captions import wants_chart
from lib.ecowitt.api import GROUPS
from lib.ecowitt.link import link
from lib.ecowitt.query import HistoryQuery, chart_field, stack_names
from lib.series import WEATHER, find
from lib.timeutil import now_local
from lib.tools import Turn, json_result

DEFAULT_GROUPS = "outdoor,indoor"
LINK_CHART_HINT = "(a chart of the reading as a line and rain as bars behind it)"


def groups_of(value, default: str = DEFAULT_GROUPS) -> list[str]:
    """'outdoor, indoor' or ['outdoor', 'indoor'] -> ['outdoor', 'indoor'] (a reading named instead of its group becomes its group)."""
    parts = value if isinstance(value, list) else str(value or default).split(",")
    named = (p.split(".")[0].strip().lower() for p in parts if p.strip())
    groups = (g if g in GROUPS else (r.group if (r := find(g)) else None) for g in named)
    return list(dict.fromkeys(g for g in groups if g)) or default.split(",")


async def history(eco, args: dict, turn: Turn | None = None) -> str:
    """HistoryQuery over the station's groups asked for (args["groups"]) and those its chart needs, fetching as a question does."""
    turn = turn or Turn()
    groups = groups_of(args.get("groups"))
    groups += [g for n in stack_names(args, turn) if (g := WEATHER[n].group) not in groups]
    if (r := find(chart_field(args, turn) or "")) and r.group not in groups:
        groups.append(r.group)
    return await HistoryQuery(eco.fetcher(groups, urgent=not turn.ahead), args, turn).run()


async def linked(eco, args: dict, turn: Turn | None = None) -> str:
    """link() over the station's 30-minute readings, its chart kept on the turn when asked for (or the period is 3 days or more)."""
    turn = turn or Turn()
    out, spec, first, last = await asyncio.to_thread(link, eco.storage.read_now, eco.mac, eco.tz, args, now_local(eco.tz))
    if spec and wants_chart(args, turn, datetime.combine(first, time()), datetime.combine(last, time())):
        turn.charts.append(spec)
        out["chart"] = LINK_CHART_HINT
    return json_result(out)


async def air_now(air, extras=()) -> dict:
    """The air sensor's latest reading (AirGradient.latest)."""
    return (await air.latest(extras)).reading


async def air_history(air, args: dict, turn: Turn | None = None) -> str:
    """AirGradient.history over the period asked (start_date, end_date) and its chart when asked; with neither, the reading now."""
    turn = turn or Turn()
    chart = wants_chart(args, turn)
    if args.get("start_date") or args.get("end_date") or chart:
        return json_result(await air.history(args.get("start_date"), args.get("end_date"), chart=chart, metrics=args.get("metrics"),
                                             turn=turn))
    return json_result(await air_now(air, [m for m in args.get("metrics") or [] if m in EXTRA]))
