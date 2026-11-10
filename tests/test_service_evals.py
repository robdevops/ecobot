"""The saved questions in service terms (tests/evals/service_cases.json): what a model given only the service's ten tools should call for
every case in cases.json, checked without a model. Every case has an expectation; every tool in it is one of the ten; every call it
describes, made concrete, is one the real tool answers rather than refuses; the tool list itself is one any model can read. (What a
real model does with the ten is `scripts/eval_prompts.py --live --service`; its plumbing is tested here with a scripted model.)

The expectations are written for evalkit.NOW (Tue 29 Sep 2026 14:05), and a period is matched by what it means then ({"means"}). The
real tools are called on the real clock over the fake station (its history runs to now), so that check is of the arguments being taken
(names, words, forms, start and end), not of the figures."""

import json
import os
import subprocess
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from lib.airgradient import AirGradient
from lib.compose import Composer
from lib.forecast import Forecast
from lib.service.context import Context
from tests import evalkit
from tests.fakes import TZ, air_transport, archived_station, config

CASES = evalkit.load_cases()
EXPECT = evalkit.load_service_cases()
ROOT = Path(__file__).resolve().parent.parent


# ---------- the expectations ----------
def test_every_case_has_a_service_expectation_and_no_other_id_does():
    ids = [c.id for c in CASES]
    assert [i for i in ids if i not in EXPECT] == [] and [i for i in EXPECT if i not in ids] == []
    assert list(EXPECT) == ids   # (kept in cases.json's order, so the two files read side by side)


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_each_expectation_names_only_the_ten_tools_and_their_own_arguments(case):
    expect = EXPECT[case.id]
    assert evalkit.service_problems(expect) == []
    assert expect.get("note"), "say why: the expectation is a reading of the question and of 3.3's routing rules"
    for name, args in evalkit.example_calls(expect):   # each concrete call passes the checker it was made from
        assert evalkit.check_calls([(name, args)], {"args": {name: expect.get("args", {}).get(name, {})}}) == [], (name, args)


def test_the_checker_reads_names_words_and_periods_as_the_service_does():
    check = evalkit.check_args
    assert check({"readings": ["temperature"]}, {"readings": {"reads": ["outdoor.temperature"]}}) == []
    assert check({"readings": "outdoor humidity"}, {"readings": {"reads": ["outdoor.humidity"], "not": ["outdoor.temperature"]}}) == []
    assert check({"readings": ["humidity", "temp"]}, {"readings": {"reads": ["outdoor.humidity"], "not": ["outdoor.temperature"]}})
    assert check({"readings": ["airgradient humidity"]}, {"readings": {"reads": ["air.humidity"]}}) == []
    assert check({"chart_readings": ["air.pm2_5:rating"]}, {"chart_readings": {"reads": ["air.pm2_5"]}}) == []
    assert "lacks" in check({"readings": ["pressure"]}, {"readings": {"reads": ["rainfall.rain"]}})[0]
    assert check({"readings": ["lightning"]}, {"readings": {"reads": ["rainfall.rain"]}})          # (not a reading: refused, so a failure)
    assert check({}, {"readings": {"reads": ["outdoor.temperature"], "optional": True}}) == []
    assert check({}, {"readings": {"reads": ["outdoor.temperature"]}}) == ["readings: no readings sent"]
    assert check({"readings": ["rain rate"]}, {"readings": {"reads_any": ["rainfall.rain", "rainfall.rain_rate"]}}) == []
    # periods: the same span at NOW (Tue 29 Sep 2026 14:05), whatever the words
    for same in ("this week", "past week", "7d", "last 7 days", "the last week", "1w"):
        assert check({"period": same}, {"period": {"means": "this week"}}) == [], same
    assert check({"period": "last week"}, {"period": {"means": "this week"}})   # (the previous Monday to Sunday)
    assert check({"period": "3 months"}, {"period": {"means": "3m"}}) == [] and check({"period": "3d"}, {"period": {"means": "3m"}})
    assert check({"period": "all time"}, {"period": {"means": "on record"}}) == []
    assert check({"period": "Tuesday"}, {"period": {"means": ["Tuesday", "today"]}}) == []
    assert "can't be read" in check({"period": "sometime"}, {"period": {"means": "today"}})[0]
    assert check({"x": 1}, {"x": {"one_of": [1, 2]}}) == [] and check({"x": 3}, {"x": {"one_of": [1, 2]}})


def test_alternatives_pass_when_any_one_does():
    expect = {"args": {"days": [{"period": {"means": "September"}}, {"start": "2026-09-01"}]}}
    assert evalkit.check_calls([("days", {"period": "this month"})], expect) == []
    assert evalkit.check_calls([("days", {"start": "2026-09-01"})], expect) == []
    fails = evalkit.check_calls([("days", {"period": "August"})], expect)
    assert len(fails) == 1 and "| or:" in fails[0]


# ---------- the tool list a model is offered ----------
def test_the_tool_list_is_the_ten_service_tools_with_their_own_descriptions_and_schemas():
    built = evalkit.service_tools()
    assert [t.name for t in built] == evalkit.service_tool_names()
    offered = evalkit.make_service_tools([])
    assert set(offered.by_name) == set(evalkit.service_tool_names())
    # (the two lists are never offered together; the one name they share is the service's own `readings`, not the bot's)
    assert set(offered.by_name) & set(evalkit.tool_names()) == {"readings"}
    assert offered.by_name["readings"].parameters != evalkit.make_tools([]).by_name["readings"].parameters
    for tool, schema in zip(built, offered.schemas):
        assert schema == {"type": "function", "function": {"name": tool.name, "description": tool.description,
                                                          "parameters": tool.parameters}}


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


@pytest.mark.parametrize("name", evalkit.service_tool_names())
def test_each_description_is_short_and_each_schema_is_json_schema(name):
    (tool,) = [t for t in evalkit.service_tools() if t.name == name]
    assert 0 < len(tool.description) < 1500
    assert tool.parameters.get("type") == "object" and schema_problems(tool.parameters, name) == []
    json.dumps(tool.parameters)   # (serialisable as it is)
    for prop_name, prop in tool.parameters.get("properties", {}).items():
        if "properties" not in prop:   # (a condition's own fields are described by the array that holds them)
            assert prop.get("description"), f"{name}.{prop_name} has no description"


# ---------- every expected call is one the real tool answers ----------
def calls_for(tool: str) -> list[tuple[str, dict]]:
    return [(cid, args) for cid, expect in EXPECT.items() for name, args in evalkit.example_calls(expect) if name == tool]


async def real_context(tmp_path, archived_cache) -> Context:
    """The ten tools' sources, faked: the archived station, an AirGradient on the same file, both together, Open-Meteo, the pollen page
    in season."""
    from tests.test_pollen import make as pollen_source
    from tests.test_service_forecast import open_meteo
    eco, _ = await archived_station(tmp_path, archived_cache)
    air = AirGradient(config(tmp_path), transport=air_transport(rows_per_day=24)[0], storage=eco.storage)
    await air.start()
    forecast = Forecast(config(tmp_path, forecast=True, forecast_lat=-37.8, forecast_lon=144.9, place="Melbourne"), None, open_meteo([]))
    noon = datetime.combine(datetime.now(TZ).date(), dtime(12, 0))
    forecast.now = lambda: noon
    pollen, _ = pollen_source(tmp_path)
    ctx = await Context.open(eco=eco, air=air, composer=Composer(eco, air), forecast=forecast, pollen=pollen)
    (summary,) = [t for t in evalkit.service_tools(ctx) if t.name == "summary"]
    held = json.loads(await summary.call({"readings": ["air"], "period": "last 30 days", "chart": False}))   # (the air's month, held)
    assert "error" not in held
    return ctx


@pytest.mark.parametrize("tool", evalkit.service_tool_names())
async def test_every_expected_call_is_answered_not_refused(tool, tmp_path, archived_cache):
    calls = calls_for(tool)
    if not calls:
        pytest.skip(f"no case expects {tool}")
    ctx = await real_context(tmp_path, archived_cache)
    try:
        (service_tool,) = [t for t in evalkit.service_tools(ctx) if t.name == tool]
        refused = {}
        for case_id, args in calls:
            out = json.loads(await service_tool.call(args))
            if "error" in out:
                refused[case_id] = (args, out["error"])
        assert refused == {}, json.dumps(refused, indent=1, ensure_ascii=False)
    finally:
        await ctx.pollen.close()
        await ctx.air.close()
        await ctx.eco.close()


# ---------- the live run, with a scripted model ----------
class ScriptedModel:
    """An OpenAI-style client whose answers are scripted per question: {question: [(tool, args), ...] or a text}. Each question gets its
    calls, then a one-line reply. Every request is kept."""

    def __init__(self, script: dict):
        self.script, self.requests = script, []
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **kw):
        self.requests.append(kw)
        asked = next(m["content"] for m in reversed(kw["messages"]) if m["role"] == "user")
        answered = kw["messages"][-1]["role"] == "tool"
        step = "Done." if answered else self.script.get(asked, "Nothing to look up.")
        calls = None if isinstance(step, str) else [NS(id=f"c{i}", function=NS(name=n, arguments=json.dumps(a)))
                                                    for i, (n, a) in enumerate(step)]
        msg = NS(content=step if isinstance(step, str) else "", tool_calls=calls)
        return NS(choices=[NS(message=msg)], usage=None)


def case(case_id: str):
    return next(c for c in CASES if c.id == case_id)


async def test_a_live_service_run_offers_the_ten_tools_alone_with_no_prompt_of_ours_and_scores_the_calls():
    picked = [case("3m-average"), case("will-it-rain"), case("hello"), case("pushback-cooler-this-week")]
    model = ScriptedModel({"average temp 3m": [("summary", {"readings": ["temperature"], "period": "last 3 months", "average": True})],
                           "will it rain later?": [("forecast", {})],   # wrong: outlook
                           "Wrong. It was cooler this week": [("summary", {"period": "past week"})]})
    lines = []
    failed = await evalkit.run_service(picked, EXPECT, model, "m", out=lines.append)
    assert failed == 1
    text = "\n".join(lines)
    assert "1/1  3m-average" in text and "0/1 FAIL  will-it-rain" in text and "did not call outlook (called: forecast)" in text
    assert "1/1  hello" in text and "1/1  pushback-cooler-this-week" in text
    assert text.endswith("3 of 4 cases pass (the service's ten tools alone)")
    first = model.requests[0]
    assert [t["function"]["name"] for t in first["tools"]] == evalkit.service_tool_names()
    assert first["tool_choice"] == "auto" and first["extra_body"] == {"reasoning": {"effort": evalkit.SERVICE_EFFORT}}
    system = first["messages"][0]
    assert system == {"role": "system", "content": evalkit.service_system()}
    assert system["content"] == ("You answer questions about a home weather station and air sensor using the tools.\n"
                                 "Now: Tuesday 29 September 2026, 14:05 (Australia/Melbourne time).")
    assert first["messages"][1:] == [{"role": "user", "content": "average temp 3m"}]
    follow_up = next(r for r in model.requests if r["messages"][-1].get("content") == "Wrong. It was cooler this week")
    assert [m["role"] for m in follow_up["messages"]] == ["system", "user", "assistant", "user"]   # (the case's history, as it was)


async def test_without_a_model_the_service_run_checks_only_the_expectations():
    lines = []
    assert await evalkit.run_service(CASES, EXPECT, out=lines.append) == 0
    assert lines[-1] == f"\n{len(CASES)} of {len(CASES)} cases pass (the service's ten tools alone)"
    assert await evalkit.run_service([case("hello")], {}, out=lines.append) == 1 and "no service expectation" in lines[-2]


def script(*args: str, key: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENROUTER_")}
    if key:
        env["OPENROUTER_API_KEY"] = key
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "eval_prompts.py"), *args], capture_output=True, text=True, env=env,
                          cwd=ROOT, timeout=120)


def test_the_script_runs_the_service_evals_offline_and_needs_a_key_to_ask_a_model():
    run = script("--service", "--ids", "3m-average,hello")
    assert run.returncode == 0 and "2 of 2 cases pass (the service's ten tools alone)" in run.stdout
    run = script("--live", "--service")
    assert run.returncode != 0 and "--live needs OPENROUTER_API_KEY" in run.stderr
