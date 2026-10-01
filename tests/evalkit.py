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

from lib import compose, intent, prompt
from lib.airgradient import source as air
from lib.ecowitt import days, link, station
from lib.forecast import source as forecast
from lib.pollen import source as pollen
from lib.llm import Agent
from lib.tools import Tool, Tools

CASES = Path(__file__).resolve().parent / "evals" / "cases.json"
NOW = datetime(2026, 9, 29, 14, 5)  # a Tuesday: dates in the cases are relative to this
CANNED = json.dumps({"note": "(evaluation run: no data is available; say so in one short sentence)"})


# Every kind of question, metric and chart the bot handles needs at least one saved case (see Case.covers). One case
# per kind is the aim: when a new kind is added, add it here and a case for it; a redundant case can go.
COVERAGE = {
    "q:current": "current conditions", "q:pollen": "pollen and thunderstorm asthma", "q:forecast": "will it rain / looking ahead", "q:summary": "a period's highs and lows",
    "q:average": "averages", "q:record": "records (highest, fastest, all time)", "q:rank-days": "ranking or listing days",
    "q:known-day": "a question about one named day", "q:describe-day": "describing a day or a hypothetical",
    "q:correlation": "does one reading go with another", "q:follow-up": "a follow-up that keeps the subject",
    "q:correction": "the person says the answer was wrong", "q:about-the-bot": "questions about the bot itself",
    "q:command": "asking the bot to do something (alerts)", "q:chat": "thanks and small talk", "q:report": "status / report",
    "q:detail-level": "asking for a finer data resolution", "q:cross-source": "a question that needs both devices together",
    "q:scan": "which readings go with which (every pair)",
    "m:temperature": "temperature", "m:humidity": "humidity", "m:pressure": "pressure", "m:wind-speed": "wind speed and gusts",
    "m:wind-direction": "wind direction", "m:rain": "rain", "m:air-quality": "air quality",
    "c:line": "a line chart of one reading", "c:daily-band": "a long chart: daily mean with its range",
    "c:stack": "several readings stacked", "c:link": "pressure (or another reading) against rain",
    "c:wind-compass": "wind speed with the compass", "c:rating-strip": "air-quality traffic-light ratings over time",
    "c:composed": "a chart the model composes from series and styles", "c:air-single": "one air-quality metric", "c:air-panels": "all air-quality metrics",
}


@dataclass
class Case:
    id: str
    ask: str
    expect: dict
    reply_to: str = ""               # the message being replied to, if any
    note: str = ""
    history: list = field(default_factory=list)   # earlier turns [{"role": "user"|"assistant", "content"}], for follow-ups
    xfail: str = ""                  # known gap: the case says what SHOULD happen; the reason it doesn't yet
    covers: list = field(default_factory=list)    # which kinds of question, metric and chart this case stands for (COVERAGE)


def load_cases(path: Path = CASES) -> list[Case]:
    return [Case(**c) for c in json.loads(path.read_text())]


def tool_names() -> list[str]:
    return ["weather_now", "weather_history", "weather_days", "weather_link", "air_quality", "plot_chart", "air_link", "air_scan",
            "pollen_asthma", "weather_forecast"]


def make_tools(calls: list) -> Tools:
    """The real tool definitions with stand-in handlers that record the call."""
    def recorder(name):
        async def handler(args, turn=None):
            calls.append((name, args))
            return CANNED
        return handler
    defs = [("weather_now", station.REALTIME_DESCRIPTION, station.REALTIME_PARAMS),
            ("weather_history", station.HISTORY_DESCRIPTION, station.HISTORY_PARAMS),
            ("weather_days", days.DESCRIPTION, days.PARAMETERS),
            ("weather_link", link.DESCRIPTION, link.PARAMETERS),
            ("air_quality", air.DESCRIPTION, air.PARAMETERS),
            ("plot_chart", compose.PLOT_DESCRIPTION, compose.PLOT_PARAMETERS),
            ("air_link", compose.LINK_DESCRIPTION, compose.LINK_PARAMETERS),
            ("air_scan", compose.SCAN_DESCRIPTION, compose.SCAN_PARAMETERS),
            ("pollen_asthma", pollen.DESCRIPTION, pollen.PARAMETERS),
            ("weather_forecast", forecast.DESCRIPTION, forecast.PARAMETERS)]
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
    if expect.get("any_tools") and not any(t in names for t in expect["any_tools"]):
        fails.append(f"called none of {expect['any_tools']} (called: {', '.join(names) or 'nothing'})")
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
    r = intent.read(text, NOW, pollen=True, forecast=True)
    if "effort" in e and r.effort != e["effort"]:
        fails.append(f"effort {r.effort}, expected {e['effort']}")
    if "fast" in e:
        fast = r.fast
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
        hints = " | ".join(r.hints)
        fails += [f"period hints lack {h!r} (got: {hints or 'none'})" for h in e["hints"] if h not in hints]
    for key, got in (("chart_fields", r.chart_fields), ("chart_field", r.chart_field), ("report", r.report),
                     ("about_the_bot", r.about_the_bot), ("needs_data", r.needs_data),
                     ("weather_now", r.weather_now), ("rain_caption", r.rain_caption)):
        if key in e and got != e[key]:
            fails.append(f"{key} {got}, expected {e[key]}")
    for name in e.get("tools", []) + e.get("any_tools", []) + e.get("not_tools", []) + ([e["first_tool"]] if "first_tool" in e else []):
        if name not in tool_names():
            fails.append(f"unknown tool {name!r} in the case")
    return fails


async def run_live(case: Case, client, model: str, effort: str | None = None) -> tuple[list[tuple[str, dict]], str]:
    """Ask the real model; returns the tool calls it made and its final reply."""
    calls: list[tuple[str, dict]] = []
    text = case.ask
    r = intent.read(text, NOW, pollen=True, forecast=True)
    system = prompt.build(NOW, ["Ecowitt weather station", "AirGradient outdoor air-quality sensor",
                                "Melbourne pollen forecast and thunderstorm asthma risk (melbournepollen.com.au)",
                                "Weather forecast for the owner's location (Bureau of Meteorology)"],
                          r.hints, r.about_the_bot, r.report, r.weather_now, r.rain_caption, bool(r.fast))
    fast = r.fast
    messages = [*case.history, {"role": "user", "content": content(case)}]
    reply = await Agent(client, model, make_tools(calls)).run(
        messages, system, effort or r.effort, first_call=([fast[:2], *r.more] if r.more else fast[:2]) if fast else None, require_tool=r.needs_data,
        no_tools=r.about_the_bot)
    if fast:
        calls[0:0] = [(fast[0], fast[1]), *r.more]  # the bot ran them itself, before the model
    return calls, reply
