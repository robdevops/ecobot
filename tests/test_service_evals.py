"""The service's ten tools as a client is offered them (tests/evals/service_cases.json holds the saved questions' example calls): the
tool list is the ten with their own descriptions and schemas, each description is short and each schema is JSON Schema, and every
example call, made concrete, is one the real tool answers rather than refuses.

The example calls are written for evalbase.NOW (Tue 29 Sep 2026 14:05). The real tools are called with the clock fixed at that time
(at_eval_now) over the fake station built under it, so the check is of the arguments being taken (names, words, forms, start and end),
not of the figures, and holds on any real date."""

import json
from datetime import datetime

import pytest

from lib.service.context import Context
from tests import evalbase, evalworld
from tests.fakes import TZ

EXPECT = evalbase.load_service_cases()


def test_the_tool_list_is_the_ten_service_tools_with_their_own_descriptions_and_schemas():
    built = evalbase.service_tools()
    assert [t.name for t in built] == evalbase.service_tool_names()
    assert len(built) == 10 and len({t.name for t in built}) == 10


JSON_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def schema_problems(schema, where: str) -> list[str]:
    """What isn't JSON Schema as function-calling APIs read it: a type each, a description on each property, enums of the type's values,
    `required` naming properties, items for an array."""
    out = []
    kind = schema.get("type")
    if kind not in JSON_TYPES:
        out.append(f"{where}: type {kind!r}")
    if kind == "object":
        props = schema.get("properties")
        if not isinstance(props, dict):
            out.append(f"{where}: an object needs properties")
            props = {}
        out += [f"{where}: required {r!r} is no property" for r in schema.get("required", []) if r not in props]
        for name, prop in props.items():
            out += schema_problems(prop, f"{where}.{name}")
    if kind == "array":
        out += schema_problems(schema["items"], f"{where}[]") if isinstance(schema.get("items"), dict) else [f"{where}: no items"]
    if "enum" in schema:
        py = {"string": str, "integer": int, "number": (int, float)}.get(kind, object)
        if not schema["enum"] or not all(isinstance(v, py) for v in schema["enum"]) or len(set(schema["enum"])) != len(schema["enum"]):
            out.append(f"{where}: enum {schema['enum']!r}")
    if extra := set(schema) - {"type", "properties", "required", "items", "enum", "description", "additionalProperties"}:
        out.append(f"{where}: unknown keys {sorted(extra)}")
    return out


@pytest.mark.parametrize("name", evalbase.service_tool_names())
def test_each_description_is_short_and_each_schema_is_json_schema(name):
    (tool,) = [t for t in evalbase.service_tools() if t.name == name]
    assert 0 < len(tool.description) < 1500
    assert tool.parameters.get("type") == "object" and schema_problems(tool.parameters, name) == []
    json.dumps(tool.parameters)   # (serialisable as it is)
    for prop_name, prop in tool.parameters.get("properties", {}).items():
        if "properties" not in prop:   # (a condition's own fields are described by the array that holds them)
            assert prop.get("description"), f"{name}.{prop_name} has no description"


# ---------- every expected call is one the real tool answers ----------
# At the evals' NOW (Tue 29 Sep 2026 14:05, Melbourne), whatever the real date: the expectations are written for it ("this month" is
# September to the 28th, "June to August 2026" is in the fake's history). Every datetime.now(), date.today() and time.time() in lib/
# and tests/ reads it, and the fake station's archive is built under it.
EVAL_NOW = evalworld.EVAL_NOW


def at_eval_now(patch: pytest.MonkeyPatch):
    """evalworld.at_eval_now, this module included (whatever pytest names it)."""
    evalworld.at_eval_now(patch, __name__)


@pytest.fixture(scope="module")
def eval_archive(shared_archive):
    """The fake station's whole archive (its ecobot.sqlite) built at EVAL_NOW, the air's month held in it too (real_context), once for every
    worker and run (tests/evalworld.py's)."""
    return evalworld.shared_eval_archive(shared_archive)


def calls_for(tool: str) -> list[tuple[str, dict]]:
    return [(cid, args) for cid, expect in EXPECT.items() for name, args in evalbase.example_calls(expect) if name == tool]


async def real_context(tmp_path, archive) -> Context:
    return await evalworld.real_context(tmp_path, archive)


@pytest.mark.parametrize("tool", evalbase.service_tool_names())
async def test_every_expected_call_is_answered_not_refused(tool, tmp_path, eval_archive, monkeypatch):
    calls = calls_for(tool)
    if not calls:
        pytest.skip(f"no case expects {tool}")
    at_eval_now(monkeypatch)
    assert datetime.now(TZ).strftime("%a %d %b %Y %H:%M") == "Tue 29 Sep 2026 14:05"
    ctx = await real_context(tmp_path, eval_archive)
    try:
        (service_tool,) = [t for t in evalbase.service_tools(ctx) if t.name == tool]
        refused = {}
        for case_id, args in calls:
            out = json.loads(await service_tool.call(args))
            if "error" in out:
                refused[case_id] = (args, out["error"])
        assert refused == {}, json.dumps(refused, indent=1, ensure_ascii=False)
    finally:
        await evalworld.close(ctx)


