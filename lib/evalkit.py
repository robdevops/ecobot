"""Saved questions with the tool calls we expect, so every fix we make for a real report stays fixed.

tests/evals/cases.json holds the questions (real ones from the logs). Two kinds of check:
  - decisions made in code (fast path, period hints, reasoning effort, which readings a chart combines): run by pytest;
  - what the model does with them (which tool, which arguments): `python scripts/eval_prompts.py --live`, against the
    real model with recording stand-in tools (nothing is fetched).
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import intent, prompt
from .airgradient import source as air
from .ecowitt import days, link, station
from .llm import Agent
from .tools import Tool, Tools

CASES = Path(__file__).resolve().parent.parent / "tests" / "evals" / "cases.json"
NOW = datetime(2026, 9, 29, 14, 5)  # a Tuesday: dates in the cases are relative to this
CANNED = json.dumps({"note": "(evaluation run: no data is available; say so in one short sentence)"})


@dataclass
class Case:
    id: str
    ask: str
    expect: dict
    reply_to: str = ""               # the message being replied to, if any
    note: str = ""
    tools: list[str] = field(default_factory=list)


def load_cases(path: Path = CASES) -> list[Case]:
    return [Case(**c) for c in json.loads(path.read_text())]


def tool_names() -> list[str]:
    return ["weather_now", "weather_history", "weather_days", "weather_link", "air_quality"]


def make_tools(calls: list) -> Tools:
    """The real tool definitions with stand-in handlers that record the call."""
    def recorder(name):
        async def handler(args):
            calls.append((name, args))
            return CANNED
        return handler
    defs = [("weather_now", station.REALTIME_DESCRIPTION, station.REALTIME_PARAMS),
            ("weather_history", station.HISTORY_DESCRIPTION, station.HISTORY_PARAMS),
            ("weather_days", days.DESCRIPTION, days.PARAMETERS),
            ("weather_link", link.DESCRIPTION, link.PARAMETERS),
            ("air_quality", air.DESCRIPTION, air.PARAMETERS)]
    return Tools([Tool(n, d, p, recorder(n)) for n, d, p in defs])


def content(case: Case) -> str:
    """The user turn exactly as the bot builds it for a private chat."""
    return f'(replying to your earlier message: "{case.reply_to}") {case.ask}' if case.reply_to else case.ask


def _matches(actual, want) -> str | None:
    """None if `actual` satisfies `want`, else why not. want: a value, {"regex"}, {"absent"}, {"has"}, {"any_of"}."""
    if isinstance(want, dict):
        if "absent" in want:
            return None if actual in (None, "", [], {}) else f"expected no value, got {actual!r}"
        if "regex" in want:
            return None if actual is not None and re.search(want["regex"], str(actual)) else f"{actual!r} does not match {want['regex']}"
        if "has" in want:
            return None if isinstance(actual, list) and all(w in actual for w in want["has"]) else f"{actual!r} lacks {want['has']}"
        if "any_of" in want:
            return None if actual in want["any_of"] else f"{actual!r} not one of {want['any_of']}"
    return None if actual == want else f"expected {want!r}, got {actual!r}"


def check_args(args: dict, want: dict) -> list[str]:
    return [f"{k}: {why}" for k, w in want.items() if (why := _matches(args.get(k), w))]


def check_calls(calls: list[tuple[str, dict]], expect: dict) -> list[str]:
    """Failures of what the model called against the case's expectations."""
    fails = []
    names = [n for n, _ in calls]
    for tool in expect.get("tools", []):
        if tool not in names:
            fails.append(f"did not call {tool} (called: {', '.join(names) or 'nothing'})")
    for tool in expect.get("not_tools", []):
        if tool in names:
            fails.append(f"should not have called {tool}")
    if "first_tool" in expect and (names[:1] != [expect["first_tool"]]):
        fails.append(f"first tool was {names[:1]}, expected {expect['first_tool']}")
    for tool, want in expect.get("args", {}).items():
        made = [a for n, a in calls if n == tool]
        if made and not any(not check_args(a, want) for a in made):
            fails.append(f"{tool} arguments: " + "; ".join(check_args(made[0], want)))
    return fails


def deterministic(case: Case) -> list[str]:
    """Failures of the decisions made in code (no model): reasoning effort, fast path, hints, chart choices."""
    e, fails = case.expect, []
    text = case.ask
    if "effort" in e and intent.reasoning_effort(text) != e["effort"]:
        fails.append(f"effort {intent.reasoning_effort(text)}, expected {e['effort']}")
    if "fast" in e:
        fast = intent.fast_call(text, NOW, True, True)
        if e["fast"] is None:
            if fast:
                fails.append(f"took the fast path ({fast[0]}) but should go to the model")
        elif not fast:
            fails.append("did not take the fast path")
        else:
            if fast[0] != e["fast"].get("tool"):
                fails.append(f"fast path tool {fast[0]}, expected {e['fast'].get('tool')}")
            fails += [f"fast path {f}" for f in check_args(fast[1], e["fast"].get("args", {}))]
    if "hints" in e:
        hints = " | ".join(intent.period_hints(text, NOW))
        fails += [f"period hints lack {h!r} (got: {hints or 'none'})" for h in e["hints"] if h not in hints]
    if "chart_fields" in e and intent.chart_fields(text) != e["chart_fields"]:
        fails.append(f"chart_fields {intent.chart_fields(text)}, expected {e['chart_fields']}")
    if "chart_field" in e and intent.chart_field(text) != e["chart_field"]:
        fails.append(f"chart_field {intent.chart_field(text)}, expected {e['chart_field']}")
    for name in e.get("tools", []) + e.get("not_tools", []) + ([e["first_tool"]] if "first_tool" in e else []):
        if name not in tool_names():
            fails.append(f"unknown tool {name!r} in the case")
    return fails


async def run_live(case: Case, client, model: str, effort: str | None = None) -> tuple[list[tuple[str, dict]], str]:
    """Ask the real model; returns the tool calls it made and its final reply."""
    calls: list[tuple[str, dict]] = []
    text = case.ask
    system = prompt.build(NOW, ["Ecowitt weather station", "AirGradient outdoor air-quality sensor"], intent.period_hints(text, NOW))
    fast = intent.fast_call(text, NOW, True, True)
    messages = [{"role": "user", "content": content(case)}]
    reply = await Agent(client, model, make_tools(calls)).run(
        messages, system, effort or intent.reasoning_effort(text), first_call=fast[:2] if fast else None,
        require_tool=intent.needs_data(text))
    if fast:
        calls.insert(0, (fast[0], fast[1]))  # the bot ran it itself, before the model
    return calls, reply
