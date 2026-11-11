"""Show exactly what the bot sends to the model for a question, without calling the model.

Builds the real system prompt (the bot's persona, the server's instructions and the time) and the service's tools as the model is
offered them (lib/model_tools.py), reads the effort from the words, and runs the service's tools against the bot's file (no network, no
API keys: the weather station is its file alone, so a tool that needs more answers with its error). A recording stand-in plays the
model: its first reply is a tool call you choose with --call (default: the call for "hottest day where it also rained"), its second is a
short answer. Every request the bot would send is printed in full.

    python scripts/show_request.py "hottest day where it also rained"
    python scripts/show_request.py --file /path/ecobot.sqlite --call '{"name": ..., "arguments": {...}}' "..."
    python scripts/show_request.py --call '{"name": "readings", "arguments": {"reading": "outdoor.humidity", "period": "last 5 hours",
        "interval": "1hour"}}' "humidity each hour for the last 5 hours"     # the readings tool, run on the file
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

from _common import parser  # noqa: E402  (also puts the repo on sys.path)
from lib.config import data_dir, local_tz  # noqa: E402

from lib import intent, model_tools, prompt  # noqa: E402
from lib.llm import Agent  # noqa: E402
from lib.service import Service  # noqa: E402
from lib.service.context import Context  # noqa: E402
from lib.service.instructions import instructions  # noqa: E402
from lib.storage import FILE, Storage  # noqa: E402
from lib.tools import Turn  # noqa: E402


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


def default_call():
    return {"name": "days", "arguments": {"where": [{"field": "rain", "op": ">=", "value": 1}], "sort_by": "temp_max", "order": "desc",
                                          "limit": 3}}


def banner(text):
    print(f"\n{'=' * 8} {text} {'=' * 8}")


async def main():
    ap = parser(__doc__)
    ap.add_argument("question")
    ap.add_argument("--file", type=Path, default=data_dir() / FILE, help="the bot's file (default: ecobot.sqlite in DATA_DIR)")
    ap.add_argument("--call", help="JSON tool call the stand-in model makes first")
    args = ap.parse_args()

    tz = local_tz()
    now = datetime.now(tz)
    if not args.file.exists():
        sys.exit(f"{args.file}: not found")
    storage = Storage(args.file, tz)
    macs = sorted({s.device for s in storage.sensors.all() if s.kind != "air"})
    if not macs:
        storage.close()
        sys.exit(f"{args.file} has no station history")

    station = NS(name="Ecowitt", storage=storage, mac=macs[0], tz=tz, latitude=None)   # (the file alone)
    ctx = await Context.open(eco=station, tz=tz)
    tools = model_tools.tools(Service.of(ctx))
    text = args.question
    r = intent.read(text, now, {"Ecowitt"}, tz, ctx.readings())
    system = prompt.build(now, instructions(ctx))
    effort = r.effort
    call = json.loads(args.call) if args.call else default_call()

    client = Recorder([call, "(the model's answer goes here)"])
    messages = [{"role": "user", "content": text}]
    await Agent(client, "deepseek/deepseek-v4.1-flash-1017", tools).run(messages, system, effort, turn=Turn())

    banner("DECISIONS MADE IN CODE")
    print(f"reasoning effort: {effort}   answered in code (no model): "
          f"{'the report' if r.report else 'weather now' if r.weather_now else r.chart or 'no'}")
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
    storage.close()


if __name__ == "__main__":
    asyncio.run(main())
