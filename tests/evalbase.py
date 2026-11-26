"""What the service's tests share with the evals' frozen world (tests/evalworld.py): NOW (Tue 29 Sep 2026 14:05, Melbourne), the
service's ten tools over stand-in sources (service_tools), and the saved expectations (tests/evals/service_cases.json) with the
example calls they describe (example_calls: the tool descriptions' examples are checked against them). Imports no model client."""

import json
from datetime import datetime
from pathlib import Path

NOW = datetime(2026, 9, 29, 14, 5)  # a Tuesday: dates in the cases are relative to this
NOW_TZ = "Australia/Melbourne"
SERVICE_CASES = Path(__file__).resolve().parent / "evals" / "service_cases.json"


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


def load_service_cases(path: Path = SERVICE_CASES) -> dict[str, dict]:
    """{case id: its expectation in service terms ({"tools", "any_tools", "not_tools", "first_tool", "args", "note"})}."""
    return json.loads(path.read_text())


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
