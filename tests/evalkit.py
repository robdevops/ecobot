"""Saved questions with what should happen to them, so every fix we make for a real report stays fixed.

tests/evals/cases.json holds the questions (real ones from the logs) and the decisions the bot still makes in code (docs/mcp-service.md 4):
the reasoning effort from the words, and its few shortcuts (the report, the gauges, "weather now", a named reading's chart and a
period, each a fixed call to a service tool). pytest checks them (`deterministic`).
tests/evals/service_cases.json holds, for the same case ids, what a model offered the service's ten tools (lib/service) should call:
checked offline by tests/test_service_evals.py, and asked of a model by `python scripts/eval_prompts.py --live` three ways:
  - the bot's own request (`bot=True`, the default of --live): its system prompt (lib/prompt.py: the persona, the server's
    instructions, the date and time), the ten tools as the bot offers them, its reasoning effort from the words; a question the bot
    answers in code is not asked of the model, its fixed calls are scored instead;
  - `--service`: the ten tools alone with a neutral system line and the date (SERVICE_SYSTEM);
  - `--service --instructions`: that, with the server's instructions (lib/service/instructions.py) after the neutral line.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from lib import intent, prompt
from lib.bot import complete_reply
from lib.llm import Agent
from lib.tools import Tool, Tools, Turn
from tests.fakes import EVERY_SOURCE

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
    "q:command": "asking the bot to do something (alerts)", "q:chat": "thanks and small talk", "q:report": "status / report", "q:dashboard": "the station's gauges",
    "q:detail-level": "asking for a finer data resolution", "q:cross-source": "a question that needs both devices together",
    "q:scan": "which readings go with which (every pair)", "q:interval": "values at an interval (each hour, every 30 minutes)",
    "q:chart-only": "a question that is only a chart (chart_answers_it), and one that asks something beside it",
    "m:temperature": "temperature", "m:humidity": "humidity", "m:pressure": "pressure", "m:wind-speed": "wind speed and gusts",
    "m:wind-direction": "wind direction", "m:rain": "rain", "m:air-quality": "air quality",
    "c:line": "a line chart of one reading", "c:daily-band": "a long chart: daily mean with its range",
    "c:stack": "several readings stacked", "c:two-axes": "two readings on one panel with a scale each", "c:link": "pressure (or another reading) against rain",
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


def content(case: Case) -> str:
    """The user turn exactly as the bot builds it for a private chat."""
    return f'(replying to your earlier message: "{case.reply_to}") {case.ask}' if case.reply_to else case.ask


def _matches(actual, want, args: dict | None = None, tool: str | None = None) -> str | None:
    """None if `actual` satisfies `want`, else why not. want: a value, {"regex"}, {"absent"}, {"has"}, {"any_of"} (or "one_of"), or
    the service's {"reads"} / {"reads_any"} (_reads), {"means"} (_means: over the whole call, `args`, as `tool` reads a period) and
    {"conditions"} (_conditions). Any of them with "optional": true also takes no value at all."""
    if isinstance(want, dict):
        if "means" in want:   # (the period may be sent as start and end instead: an optional one is absent only if neither is)
            sent = {k: (args or {}).get(k) for k in ("period", "start", "end")} if args is not None else {"period": actual}
            if want.get("optional") and not any(v not in (None, "") for v in sent.values()):
                return None
            return _means(sent, want["means"], tool)
        if want.get("optional") and actual in (None, "", [], {}):
            return None
        if "reads" in want or "reads_any" in want:
            return _reads(actual, want)
        if "conditions" in want:
            return _conditions(actual, want["conditions"])
        if "one_of" in want:
            return None if actual in want["one_of"] else f"{actual!r} not one of {want['one_of']}"
        if "absent" in want:
            return None if actual in (None, "", [], {}) else f"expected no value, got {actual!r}"
        if "regex" in want:
            return None if actual is not None and re.search(want["regex"], str(actual)) else f"{actual!r} does not match {want['regex']}"
        if "has" in want:
            return None if isinstance(actual, list) and all(w in actual for w in want["has"]) else f"{actual!r} lacks {want['has']}"
        if "any_of" in want:
            return None if actual in want["any_of"] else f"{actual!r} not one of {want['any_of']}"
    return None if actual == want else f"expected {want!r}, got {actual!r}"


def check_args(args: dict, want: dict, tool: str | None = None) -> list[str]:
    """Failures of one call's arguments against one alternative of its expected arguments; `tool` is the tool called (a period is
    read as that tool reads it)."""
    return [f"{k}: {why}" for k, w in want.items() if (why := _matches(args.get(k), w, args, tool))]


def check_calls(calls: list[tuple[str, dict]], expect: dict) -> list[str]:
    """Failures of what the model called against the case's expectations. A tool's expected arguments may be a list of alternatives
    (the service's cases: `period` in words, or `start` and `end`): a call matching any one of them passes."""
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
        alternatives = want if isinstance(want, list) else [want]
        if made and not any(not check_args(a, w, tool) for a in made for w in alternatives):
            fails.append(f"{tool} arguments: " + " | or: ".join("; ".join(check_args(made[0], w, tool)) for w in alternatives))
    return fails


DECISIONS = {"effort", "report", "dashboard", "weather_now", "chart"}   # what cases.json's `expect` may say


def read(case: Case):
    """intent.read of the case's question, as the bot reads it at NOW with every source."""
    from zoneinfo import ZoneInfo
    from lib.service.names import CATALOGUE
    tz = ZoneInfo(NOW_TZ)
    return intent.read(case.ask, NOW.replace(tzinfo=tz), EVERY_SOURCE, tz, CATALOGUE)


def deterministic(case: Case) -> list[str]:
    """Failures of the decisions made in code (no model): the reasoning effort, and which shortcut answers it, if any (`chart`: the
    summary arguments of a named reading's chart and a period, or null: no chart shortcut)."""
    e, fails = case.expect, []
    if extra := set(e) - DECISIONS:
        fails.append(f"unknown keys {sorted(extra)}")
    r = read(case)
    if "effort" in e and r.effort != e["effort"]:
        fails.append(f"effort {r.effort}, expected {e['effort']}")
    for key in ("report", "dashboard", "weather_now"):
        if key in e and getattr(r, key) != e[key]:
            fails.append(f"{key} {getattr(r, key)}, expected {e[key]}")
    if "chart" in e:
        got = r.chart[1] if r.chart else None
        if got != e["chart"]:
            fails.append(f"chart shortcut {got}, expected {e['chart']}")
    return fails


def shortcut_calls(r) -> list[tuple[str, dict]] | None:
    """The service calls the bot makes itself for a question it answers in code (the report, the gauges, weather now, a chart), else
    None: the model takes it."""
    from lib import gauge_sheet
    if r.report:
        return intent.report_calls(EVERY_SOURCE)
    if r.dashboard:
        return [("current", gauge_sheet.call())]
    if r.weather_now:
        return [intent.WEATHER_NOW_CALL]
    return [r.chart] if r.chart else None


# Where the bot's own shortcuts differ from what a model is expected to call, on purpose (docs/mcp-service.md 4): the report's forecast
# is the next 3 days, as it always was; a chart the bot draws itself sends its caption, so it needs no chart_answers_it
SHORTCUT_LEAVES = {"forecast": ("period",), "summary": ("chart_answers_it",)}


def shortcut_expectation(expect: dict) -> dict:
    """The case's service expectation, as the bot's own shortcut calls are held to it (less SHORTCUT_LEAVES)."""
    args = {}
    for tool, want in expect.get("args", {}).items():
        drop = SHORTCUT_LEAVES.get(tool, ())
        args[tool] = [{k: v for k, v in w.items() if k not in drop} for w in want] if isinstance(want, list) else \
            {k: v for k, v in want.items() if k not in drop}
    return {**expect, "args": args}


def bot_system(now: datetime = NOW) -> str:
    """The bot's system prompt at `now` (the station's local time): its persona, the server's instructions, the date and time."""
    from zoneinfo import ZoneInfo
    return prompt.build(now.replace(tzinfo=ZoneInfo(NOW_TZ)), service_instructions())


async def run_live(case: Case, client, model: str, effort: str | None = None) -> tuple[list[tuple[str, dict]], str]:
    """The bot's own request (docs/mcp-service.md 4): a question it answers in code gives its fixed calls (no model); any other is asked
    of the model with the bot's system prompt, the ten tools as it offers them, its reasoning effort from the words, tool_choice auto,
    and the turn ended on a complete result as the bot ends it. Returns the calls made and the reply."""
    r = read(case)
    if (calls := shortcut_calls(r)) is not None:
        return list(calls), "(answered in code)"
    calls = []
    messages = [*case.history, {"role": "user", "content": content(case)}]
    reply = await Agent(client, model, make_service_tools(calls)).run(messages, bot_system(), effort or r.effort, tool_choice="auto",
                                                                      turn=Turn(), finish=lambda _, results: complete_reply(results))
    return calls, reply


# ---------- the service's ten tools, offered alone (docs/mcp-service.md 6, step 2; 7) ----------
# The same questions asked of a model given only the service's tool list: no prompt of ours (a neutral line, the date, time and zone),
# no hints, no fast path, nothing filled in from the words. tests/evals/service_cases.json says, for every case id in cases.json, which
# of the ten tools answers it and with which arguments, as a client that has never seen this repository would call them.
SERVICE_CASES = CASES.parent / "service_cases.json"
SERVICE_SYSTEM = "You answer questions about a home weather station and air sensor using the tools."
SERVICE_EFFORT = "low"   # one reasoning level for every case: there is no intent.read to choose one
NOW_TZ = "Australia/Melbourne"
EXPECT_KEYS = {"tools", "any_tools", "not_tools", "first_tool", "args", "note"}


def service_modules():
    from lib.service import about, current, days, forecast, outlook, pollen, readings, relate, scan, summary
    return (current, summary, readings, days, relate, scan, outlook, forecast, pollen, about)


def service_tool_names() -> list[str]:
    return ["current", "summary", "readings", "days", "relate", "scan", "outlook", "forecast", "pollen", "about"]


def eval_context():
    """The Context the service's evals build the tools over: stand-ins for every source (nothing behind them: the tools are built, never
    run), the catalogue of every reading the service knows, the station's zone and NOW."""
    from zoneinfo import ZoneInfo

    from lib.service.context import Context
    tz = ZoneInfo(NOW_TZ)
    return Context(eco=object(), air=object(), composer=object(), forecast=object(), pollen=object(), tz=tz,
                   clock=lambda: NOW.replace(tzinfo=tz).timestamp())


def service_tools(ctx=None) -> list:
    """The ten ServiceTools, each module's tools(ctx), over eval_context() when no ctx is given."""
    ctx = ctx if ctx is not None else eval_context()
    return [t for module in service_modules() for t in module.tools(ctx)]


def service_instructions() -> str:
    """The server's instructions (lib/service/instructions.py) over eval_context(), as a `--instructions` run adds them."""
    from lib.service.instructions import instructions
    return instructions(eval_context())


def make_service_tools(calls: list) -> Tools:
    """The ten tools as a model is offered them (name, description, parameters from lib/service), with handlers that record the call."""
    def recorder(name):
        async def handler(args, turn=None):
            calls.append((name, args))
            return CANNED
        return handler
    return Tools([Tool(t.name, t.description, t.parameters, recorder(t.name)) for t in service_tools()])


def service_system(now: datetime = NOW, instructions: str | None = None) -> str:
    """The whole system prompt of a service run: the neutral line, and the date, time and zone now; with `instructions` (the server's,
    service_instructions()), those after it, as a client that passes them on to its model would (docs/mcp-service.md 3.2, 7)."""
    system = f"{SERVICE_SYSTEM}\nNow: {now:%A} {now.day} {now:%B %Y}, {now:%H:%M} ({NOW_TZ} time)."
    return f"{system}\n\n{instructions}" if instructions else system


def load_service_cases(path: Path = SERVICE_CASES) -> dict[str, dict]:
    """{case id: its expectation in service terms ({"tools", "any_tools", "not_tools", "first_tool", "args", "note"})}."""
    return json.loads(path.read_text())


def _resolved(names) -> list[str] | str:
    """The service names these readings stand for (lib/service/names.py; ":rating" set aside), or why they can't be read."""
    from lib.service.names import CATALOGUE, UnknownReading
    words = [names] if isinstance(names, str) else names
    if not isinstance(words, list):
        return f"{names!r} is not a reading or a list of them"
    try:
        return [r.name for r in CATALOGUE.resolve([str(w).split(":", 1)[0] for w in words], default=None)]
    except UnknownReading as e:
        return f"{names!r}: {e.message}"


def _reads(actual, want: dict) -> str | None:
    """{"reads": [names]}: the readings sent stand for every one of these; {"reads_any": [names]}: for at least one; "not": [names]: for
    none of these. What was sent is read as the service reads it (names or words)."""
    if actual in (None, "", []):
        return "no readings sent"
    got = _resolved(actual)
    if isinstance(got, str):
        return got
    if need := [n for n in want.get("reads", []) if n not in got]:
        return f"{actual!r} ({', '.join(got)}) lacks {', '.join(need)}"
    if "reads_any" in want and not set(want["reads_any"]) & set(got):
        return f"{actual!r} ({', '.join(got)}) has none of {', '.join(want['reads_any'])}"
    if extra := [n for n in want.get("not", []) if n in got]:
        return f"{actual!r} ({', '.join(got)}) should not have {', '.join(extra)}"
    return None


FIRST_HELD = date(2022, 10, 1)   # where "on record" starts when a case's period is read (the station's record, as the cases have it)


WHOLE_DAY_TOOLS = {"days", "relate", "scan"}   # the tools that read a period as whole days to yesterday (read_period's whole_days)


def _period_span(period, start=None, end=None, whole_days: bool = False) -> tuple | str:
    """(start, end) of a `period` in words, or of `start` and `end`, as the service reads them at NOW in the station's zone
    (lib/periods_text.py; whole_days as a whole-day tool reads them), or why they can't be read."""
    from zoneinfo import ZoneInfo

    from lib.periods_text import PeriodError, read_period
    if period is not None and (not isinstance(period, str) or not period.strip()):
        return f"{period!r} is not a period in words"
    if period is None and start in (None, ""):
        return "no period sent (neither `period` nor `start`)"
    what = repr(period) if period is not None else f"start {start!r}" + (f", end {end!r}" if end else "")
    try:
        p = read_period(period, start, end, now=NOW.replace(tzinfo=ZoneInfo(NOW_TZ)), tz=ZoneInfo(NOW_TZ), default="today",
                        whole_days=whole_days, first_held=FIRST_HELD)
    except PeriodError as e:
        return f"{what} can't be read: {e.message.split('. ')[0]}"
    return p.start, p.end


def _means(sent, refs, tool: str | None = None) -> str | None:
    """{"means": words or [words]}: the period sent ({"period", "start", "end"} of the call; a bare value is a `period`) reads, at NOW,
    as the same span as (one of) these words (a period in words as words; start and end as `tool` reads them, whole days for
    WHOLE_DAY_TOOLS): "past week", "this week" and
    "7d" are the same period, "last week" is another; to days, start "2026-09-01" is "September" (to yesterday)."""
    sent = sent if isinstance(sent, dict) else {"period": sent}
    if sent.get("period") is not None and str(sent["period"]).strip() == "":
        sent = {**sent, "period": None}
    if sent.get("period") is not None:   # the period wins over start and end sent beside it, as the tools read it (common.read_period)
        sent = {"period": sent["period"]}
    # words are compared as words (whole days would make "last 28 days" September on the 29th); dates as the tool reads them
    whole = tool in WHOLE_DAY_TOOLS and sent.get("period") is None
    got = _period_span(sent.get("period"), sent.get("start"), sent.get("end"), whole)
    if isinstance(got, str):
        return got
    refs = refs if isinstance(refs, list) else [refs]
    if any(_period_span(r, whole_days=whole) == got for r in refs):
        return None
    what = repr(sent["period"]) if sent.get("period") is not None else f"start {sent.get('start')!r}, end {sent.get('end')!r}"
    return f"{what} ({got[0]:%a %d %b %Y %H:%M} to {got[1]:%a %d %b %Y %H:%M}) is not the period of {' or '.join(map(repr, refs))}"


def _op(op) -> str:
    """An op as lib/service/days.py reads it (OP_WORDS: "≥" and "=>" are ">=")."""
    from lib.service.days import OP_WORDS
    return OP_WORDS.get(str(op).strip(), str(op).strip())


def _number(value) -> float | None:
    """A condition's value as days.py reads it (a number, or a string of one), else None."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    return None if isinstance(value, bool) or not isinstance(value, int | float) else float(value)


def _condition_fails(c, want: dict) -> str | None:
    if not isinstance(c, dict):
        return f"{c!r} is not a condition"
    fields = want["field"] if isinstance(want["field"], list) else [want["field"]]
    ops = [_op(o) for o in (want["op"] if isinstance(want.get("op"), list) else [want["op"]] if "op" in want else [])]
    if str(c.get("field", "")).strip() not in fields:
        return f"field {c.get('field')!r} is not {' or '.join(fields)}"
    if ops and _op(c.get("op")) not in ops:
        return f"op {c.get('op')!r} is not {' or '.join(ops)}"
    if (n := _number(c.get("value"))) is None:
        return f"value {c.get('value')!r} is not a number"
    if "value" in want and n != float(want["value"]):
        return f"value {c.get('value')!r} is not {want['value']:g}"
    return None


def _conditions(actual, want: list[dict]) -> str | None:
    """{"conditions": [{"field": name or [names], "op": [ops], "value": number}]}: the `where` sent (a list of conditions, or one) is
    these conditions, each once, in any order, compared by their keys as days.py reads them: a value as a number (9, 9.0 and "9" are
    the same), an op by its meaning (">=", "≥", "=>"), any other key ignored. With no "op" any op will do, with no "value" any number."""
    sent = [actual] if isinstance(actual, dict) else actual
    if not isinstance(sent, list):
        return f"{actual!r} is not a list of conditions"
    left = list(sent)
    for w in want:
        hit = next((c for c in left if _condition_fails(c, w) is None), None)
        if hit is None:
            why = "; ".join(f"{c!r}: {_condition_fails(c, w)}" for c in left) or "none left"
            return f"no condition on {w['field']} as {w} ({why})"
        left.remove(hit)
    return f"conditions not asked for: {left!r}" if left else None


def _example_conditions(want: list[dict]) -> list[dict]:
    return [{"field": w["field"][0] if isinstance(w["field"], list) else w["field"],
             "op": (w["op"][0] if isinstance(w["op"], list) else w["op"]) if "op" in w else ">=", "value": w.get("value", 1)} for w in want]


ABSENT = object()   # (example_value: the argument is best left out)


def example_value(want):
    """A concrete value that satisfies `want` (_matches), to call the tool with: a matcher's own "e.g.", else the first of any_of /
    one_of, the names of reads / has, the conditions given; ABSENT where the argument is best left out."""
    if not isinstance(want, dict):
        return want
    if "e.g." in want:
        return want["e.g."]
    if "absent" in want:
        return ABSENT
    for key in ("one_of", "any_of"):
        if key in want:
            return ABSENT if want[key][0] is None else want[key][0]
    if "reads" in want or "reads_any" in want:
        return list(want.get("reads") or want["reads_any"][:1])
    if "means" in want:
        return want["means"][0] if isinstance(want["means"], list) else want["means"]
    if "conditions" in want:
        return _example_conditions(want["conditions"])
    if "has" in want:
        return list(want["has"])
    raise ValueError(f"no example for {want!r}: give it an \"e.g.\"")


def expected_tools(expect: dict) -> list[str]:
    """The tools an expectation would have called (tools, any_tools, first_tool, and those it gives arguments for), each once."""
    return list(dict.fromkeys([*expect.get("tools", []), *expect.get("any_tools", []),
                               *([expect["first_tool"]] if "first_tool" in expect else []), *expect.get("args", {})]))


def example_calls(expect: dict) -> list[tuple[str, dict]]:
    """Every call a service expectation describes, made concrete: (tool, arguments) for each tool and each alternative of its arguments
    (example_value); a tool expected with no arguments named is called with {}."""
    out = []
    for tool in expected_tools(expect):
        want = expect.get("args", {}).get(tool, {})
        for alternative in (want if isinstance(want, list) else [want]):
            out.append((tool, {k: v for k, w in alternative.items() if (v := example_value(w)) is not ABSENT}))
    return out


def service_problems(expect: dict | None) -> list[str]:
    """What is wrong with a case's service expectation itself (no model): missing, an unknown key or tool, an argument the tool doesn't
    take, an example that doesn't satisfy its own matcher."""
    if expect is None:
        return ["no service expectation (tests/evals/service_cases.json)"]
    fails = [f"unknown keys {sorted(extra)}"] if (extra := set(expect) - EXPECT_KEYS) else []
    tools = {t.name: t for t in service_tools()}
    fails += [f"unknown tool {n!r}" for n in dict.fromkeys([*expected_tools(expect), *expect.get("not_tools", [])]) if n not in tools]
    for tool, want in expect.get("args", {}).items():
        known = tools[tool].parameters.get("properties", {}) if tool in tools else {}
        for alternative in (want if isinstance(want, list) else [want]):
            fails += [f"{tool} takes no argument {k!r}" for k in alternative if tool in tools and k not in known]
            for k, w in alternative.items():
                try:
                    v = example_value(w)
                except ValueError as e:
                    fails.append(f"{tool}.{k}: {e}")
                    continue
                if v is not ABSENT and (why := _matches(v, w, {k: v}, tool)):
                    fails.append(f"{tool}.{k}: its example doesn't satisfy it ({why})")
    return fails


async def run_live_service(case: Case, client, model: str, effort: str | None = None,
                           instructions: str | None = None) -> tuple[list[tuple[str, dict]], str]:
    """Ask the model with the ten tools alone and the neutral system line (with `instructions` after it, when given); returns the tool
    calls it made and its final reply. It may answer with no tool at all (tool_choice auto): a plain client forces nothing."""
    calls: list[tuple[str, dict]] = []
    messages = [*case.history, {"role": "user", "content": content(case)}]
    reply = await Agent(client, model, make_service_tools(calls)).run(messages, service_system(instructions=instructions),
                                                                      effort or SERVICE_EFFORT, tool_choice="auto")
    return calls, reply


def calls_text(calls: list[tuple[str, dict]]) -> str:
    """The calls a model made, one line: `days {"sort_by": "rain"}; pollen {}`."""
    return "; ".join(f"{n} {json.dumps(a, ensure_ascii=False)}" for n, a in calls) or "nothing"


async def run_service(cases: list[Case], expectations: dict[str, dict], client=None, model: str = "", *, repeat: int = 1,
                      effort: str | None = None, instructions: str | None = None, bot: bool = False, out=print) -> int:
    """The service's evals: each case's expectation checked (service_problems) and, with a client, the model asked `repeat` times with
    the ten tools alone (and the server's `instructions`, when given) and its calls scored against it (check_calls), as `--live` scores
    cases.json. Prints a line per case (`out`), with the calls of a run that fails; returns how many cases fail."""
    failed = 0
    for case in cases:
        expect = expectations.get(case.id)
        problems = service_problems(expect)
        line = "ok" if not problems else "FAIL"
        if client and not problems:
            passes = 0
            for _ in range(repeat):
                if bot:
                    calls, _reply = await run_live(case, client, model, effort)
                    in_code = shortcut_calls(read(case)) is not None
                else:
                    calls, _reply, in_code = *await run_live_service(case, client, model, effort, instructions), False
                why = check_calls(calls, shortcut_expectation(expect) if in_code else expect)
                passes += not why
                problems += [*(f"  run: {w}" for w in why[:3]), f"  called: {calls_text(calls)}"] if why else []
            line = f"{passes}/{repeat}" + ("" if passes == repeat else " FAIL") + (" code" if bot and in_code else "")
        out(f"{line:>10}  {case.id}  ({case.ask})")
        for p in problems:
            out(f"            {p}")
        failed += bool(problems)
    what = "the bot's prompt and the ten tools" if bot else f"the service's ten tools alone{', with its instructions' if instructions else ''}"
    if client:
        out(f"\nmodel: {model}")
    out(f"\n{len(cases) - failed} of {len(cases)} cases pass ({what})")
    return failed
