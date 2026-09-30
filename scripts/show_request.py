"""Show exactly what the bot sends to the model for a question, without calling the model.

Builds the real system prompt and tool definitions, runs the real reasoning/fast-path decisions, and
runs the real tools against your cache (no network, no API keys). A recording stand-in plays the
model: its first reply is a tool call you choose with --call (default: the call the model made for
"hottest day where it also rained"), its second is a short answer. Every request the bot would send
is printed in full.

    python scripts/show_request.py "hottest day where it also rained"
    python scripts/show_request.py --cache /path/ecowitt_cache.sqlite --call '{"name": ..., "arguments": {...}}' "..."
"""

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

from _common import ROOT, parser  # noqa: E402  (also puts the repo on sys.path)

from lib import intent, prompt  # noqa: E402
from lib.airgradient import source as air  # noqa: E402
from lib.ecowitt import days, station  # noqa: E402
from lib.ecowitt.api import UNITS  # noqa: E402
from lib.ecowitt.store import HistoryCache  # noqa: E402
from lib.llm import Agent  # noqa: E402
from lib.tools import Tool, Tools  # noqa: E402


class Recorder:
    """Plays the model: records each request, answers from a script."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, str):
            msg = NS(content=step, tool_calls=None)
        else:
            msg = NS(content="", tool_calls=[NS(id="call_1", function=NS(name=step["name"], arguments=json.dumps(step["arguments"])))])
        return NS(choices=[NS(message=msg)], usage=None)


def default_call(today):
    return {"name": "weather_days", "arguments": {
        "start_date": str(today - timedelta(days=1459)), "end_date": str(today - timedelta(days=1)),
        "where": [{"field": "rain", "op": ">=", "value": 1}], "sort_by": "temp_max", "order": "desc", "limit": 3}}


def banner(text):
    print(f"\n{'=' * 8} {text} {'=' * 8}")


async def main():
    ap = parser(__doc__)
    ap.add_argument("question")
    ap.add_argument("--cache", type=Path, default=ROOT / "ecowitt_cache.sqlite")
    ap.add_argument("--call", help="JSON tool call the stand-in model makes first")
    args = ap.parse_args()

    tz = ZoneInfo(os.getenv("TZ") or "Australia/Melbourne")
    now = datetime.now(tz)
    macs = [m for (m,) in sqlite3.connect(f"file:{args.cache}?mode=ro", uri=True).execute("SELECT DISTINCT mac FROM coverage")]
    if not macs:
        sys.exit(f"{args.cache} has no cached history")
    cache = HistoryCache(args.cache, UNITS)

    async def days_handler(a):
        return await days.days_tool(cache, macs[0], tz, a)

    async def unavailable(a):
        return "(not run by this script)"

    tools = Tools([Tool("weather_now", station.REALTIME_DESCRIPTION, station.REALTIME_PARAMS, unavailable),
                   Tool("weather_history", station.HISTORY_DESCRIPTION, station.HISTORY_PARAMS, unavailable),
                   Tool("weather_days", days.DESCRIPTION, days.PARAMETERS, days_handler),
                   Tool("air_quality", air.DESCRIPTION, air.PARAMETERS, unavailable)])
    text = args.question
    system = prompt.build(now, ["Ecowitt weather station", "AirGradient outdoor air-quality sensor"],
                          intent.period_hints(text, now.replace(tzinfo=None)))
    effort, fast = intent.reasoning_effort(text), intent.fast_call(text, now.replace(tzinfo=None), True, True)
    call = json.loads(args.call) if args.call else default_call(now.date())

    client = Recorder([call, "(the model's answer goes here)"])
    messages = [{"role": "user", "content": text}]
    await Agent(client, "grok-4.3", tools).run(messages, system, effort, first_call=fast[:2] if fast else None,
                                               require_tool=intent.needs_data(text))

    banner("DECISIONS MADE IN CODE")
    print(f"reasoning effort: {effort}   fast path: {fast[2] if fast else 'no (the model chooses the tool)'}   "
          f"must call a tool first: {intent.needs_data(text)}")
    first = client.requests[0]
    banner("REQUEST 1: settings")
    print(json.dumps({k: v for k, v in first.items() if k not in ("messages", "tools")}, indent=2))
    banner("REQUEST 1: system prompt")
    print(first["messages"][0]["content"])
    banner("REQUEST 1: user message")
    print(json.dumps(first["messages"][1:], indent=2, ensure_ascii=False))
    banner("REQUEST 1: tools offered")
    for t in first["tools"]:
        print(json.dumps(t, indent=2, ensure_ascii=False))
    for n, req in enumerate(client.requests[1:], 2):
        banner(f"REQUEST {n}: what is added (settings, system prompt and tools are the same as request 1)")
        print(json.dumps(req["messages"][1 + len(first["messages"][1:]):], indent=2, ensure_ascii=False))
    total = sum(len(json.dumps(r["messages"], ensure_ascii=False)) + len(json.dumps(r.get("tools", []))) for r in client.requests)
    banner("SIZE")
    for n, req in enumerate(client.requests, 1):
        chars = len(json.dumps(req["messages"], ensure_ascii=False)) + len(json.dumps(req.get("tools", [])))
        print(f"request {n}: {chars:,} characters (about {chars // 4:,} tokens)")
    print(f"total sent: about {total // 4:,} tokens")


if __name__ == "__main__":
    asyncio.run(main())
