"""The saved questions in service terms (tests/evals/service_cases.json): what a model given only the service's ten tools should call for
every case in cases.json, checked without a model. Every case has an expectation; every tool in it is one of the ten; every call it
describes, made concrete, is one the real tool answers rather than refuses; the tool list itself is one any model can read. (What a
real model does with the ten is `scripts/eval_prompts.py --live --service`; its plumbing is tested here with a scripted model.)

The expectations are written for evalkit.NOW (Tue 29 Sep 2026 14:05), and a period is matched by what it means then ({"means"}), sent
in words or as start and end. The real tools are called with the clock fixed at that time (at_eval_now) over the fake station built
under it, so that check is of the arguments being taken (names, words, forms, start and end), not of the figures, and holds on any
real date."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from lib.service.context import Context
from tests import evalkit, evalworld
from tests.fakes import TZ

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


def test_a_condition_list_is_matched_by_its_keys_and_numbers_not_its_spelling():
    check = evalkit.check_args
    want = {"where": {"conditions": [{"field": "uv_max", "op": [">=", ">"], "value": 9}]}}
    for same in ([{"field": "uv_max", "op": ">=", "value": 9}], [{"value": 9, "op": ">=", "field": "uv_max"}],
                 [{"field": "uv_max", "op": ">=", "value": 9.0}], [{"field": "uv_max", "op": "≥", "value": "9"}],
                 [{"field": " uv_max", "op": "=>", "value": 9, "note": "after value"}], {"field": "uv_max", "op": ">", "value": 9}):
        assert check({"where": same}, want) == [], same
    for wrong in ([{"field": "uv_avg", "op": ">=", "value": 9}], [{"field": "uv_max", "op": "<", "value": 9}],
                  [{"field": "uv_max", "op": ">=", "value": 10}], [{"field": "uv_max", "op": ">=", "value": "nine"}],
                  [{"field": "uv_max", "op": ">=", "value": True}], [{"field": "uv_max", "op": ">="}], [], None, "uv_max >= 9",
                  [{"field": "uv_max", "op": ">=", "value": 9}, {"field": "rain", "op": ">=", "value": 1}]):
        assert check({"where": wrong}, want), wrong
    two = {"where": {"conditions": [{"field": "temp_max", "op": [">", ">="], "value": 35}, {"field": "rain", "op": [">", ">="]}]}}
    assert check({"where": [{"op": ">", "value": 0.2, "field": "rain"}, {"field": "temp_max", "value": 35.0, "op": "≥"}]}, two) == []
    assert check({"where": [{"field": "temp_max", "op": ">", "value": 35}]}, two)          # (the rain condition missing)
    either = {"where": {"conditions": [{"field": ["pm2_5_max", "pm2_5_avg"], "op": [">"], "value": 25}]}}
    assert check({"where": [{"field": "pm2_5_avg", "op": ">", "value": 25}]}, either) == []
    assert evalkit.example_value(two["where"]) == [{"field": "temp_max", "op": ">", "value": 35}, {"field": "rain", "op": ">", "value": 1}]


def test_every_where_is_matched_as_conditions_so_a_correct_call_in_any_key_order_passes():
    wheres = {cid: w for cid, expect in EXPECT.items() for alternatives in expect.get("args", {}).values()
              for alt in (alternatives if isinstance(alternatives, list) else [alternatives]) if (w := alt.get("where"))}
    assert wheres and all(set(w) <= {"conditions", "e.g."} and "conditions" in w for w in wheres.values()), wheres
    for cid in ("uv-days-two-years", "uv-days-per-year", "days-over-limit-by-year", "air-days-per-year", "count-pm25-days-per-year",
                "days-two-readings-go-to-the-model", "hottest-day-that-rained", "rainy-days-in-september"):
        (name, args), *_ = evalkit.example_calls(EXPECT[cid])
        backwards = {**args, "where": [{"note": "x", **dict(reversed(list(c.items())))} for c in args["where"]]}
        assert evalkit.check_calls([(name, backwards)], EXPECT[cid]) == [], cid


def test_a_period_sent_as_start_and_end_is_matched_by_what_it_means_as_the_tool_reads_it():
    check = evalkit.check_calls
    # days reads whole days to yesterday: September from its first day is the month to the 28th, as "September" is
    assert check([("days", {"start": "2026-09-01"})], {"args": {"days": {"period": {"means": "September"}}}}) == []
    assert check([("days", {"start": "2026-09-01", "end": "2026-09-28"})], {"args": {"days": {"period": {"means": "this month"}}}}) == []
    assert check([("days", {"start": "2026-08-01"})], {"args": {"days": {"period": {"means": "September"}}}})
    # summary reads to the minute: one day is that day's start and end; a start alone runs to now
    assert check([("summary", {"start": "2026-06-11", "end": "2026-06-11"})], {"args": {"summary": {"period": {"means": "11 June"}}}}) == []
    assert check([("summary", {"start": "2026-06-11"})], {"args": {"summary": {"period": {"means": "11 June"}}}})
    assert check([("readings", {"start": "2026-09-29"})], {"args": {"readings": {"period": {"means": "today"}}}}) == []
    assert "no period sent" in check([("days", {})], {"args": {"days": {"period": {"means": "September"}}}})[0]
    assert check([("days", {})], {"args": {"days": {"period": {"means": "on record", "optional": True}}}}) == []
    for cid, call in (("most-rain-days-jja", ("days", {"sort_by": "rain", "start": "2026-06-01", "end": "2026-08-31"})),
                      ("rainy-days-in-september", ("days", {"where": [{"field": "rain", "op": ">=", "value": 1}], "start": "2026-09-01"})),
                      ("rain-on-a-named-day", ("days", {"start": "2026-09-05", "end": "2026-09-05"})),
                      ("weather-on-june-11", ("summary", {"start": "2026-06-11", "end": "2026-06-11"})),
                      ("temperature-every-30-minutes",
                       ("readings", {"reading": "temperature", "interval": "30min", "start": "2026-09-29"}))):
        assert check([call], EXPECT[cid]) == [], cid


def test_the_whole_day_tools_are_those_whose_period_is_described_as_whole_days():
    tools = {t.name: t for t in evalkit.service_tools()}
    assert evalkit.WHOLE_DAY_TOOLS == {n for n, t in tools.items() if "Whole days only" in t.parameters.get("properties", {})
                                       .get("period", {}).get("description", "")}


def test_a_hottest_day_of_a_period_may_be_found_by_days_as_well():
    for cid, period in (("think-hottest-week", "this week"), ("hottest-coldest-this-month", "this month")):
        assert evalkit.check_calls([("days", {"sort_by": "temp_max", "period": period})], EXPECT[cid]) == [], cid
        assert evalkit.check_calls([("days", {"period": period})], EXPECT[cid]) == [], cid


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
# At the evals' NOW (Tue 29 Sep 2026 14:05, Melbourne), whatever the real date: the expectations are written for it ("this month" is
# September to the 28th, "June to August 2026" is in the fake's history). Every datetime.now(), date.today() and time.time() in lib/
# and tests/ reads it, and the fake station's archive is built under it (the idea of tests/test_service_switch.py's freeze, kept here).
EVAL_NOW = evalworld.EVAL_NOW


def at_eval_now(patch: pytest.MonkeyPatch):
    """evalworld.at_eval_now, this module included (whatever pytest names it)."""
    evalworld.at_eval_now(patch, __name__)


@pytest.fixture(scope="module")
def eval_archive(tmp_path_factory):
    """The fake station's whole archive (its ecobot.sqlite) built at EVAL_NOW, once for the module."""
    path = tmp_path_factory.mktemp("eval-now")
    with pytest.MonkeyPatch.context() as patch:
        at_eval_now(patch)
        return asyncio.run(evalworld.build_archive(path))


def calls_for(tool: str) -> list[tuple[str, dict]]:
    return [(cid, args) for cid, expect in EXPECT.items() for name, args in evalkit.example_calls(expect) if name == tool]


async def real_context(tmp_path, archive) -> Context:
    return await evalworld.real_context(tmp_path, archive)


@pytest.mark.parametrize("tool", evalkit.service_tool_names())
async def test_every_expected_call_is_answered_not_refused(tool, tmp_path, eval_archive, monkeypatch):
    calls = calls_for(tool)
    if not calls:
        pytest.skip(f"no case expects {tool}")
    at_eval_now(monkeypatch)
    assert datetime.now(TZ).strftime("%a %d %b %Y %H:%M") == "Tue 29 Sep 2026 14:05"
    ctx = await real_context(tmp_path, eval_archive)
    try:
        (service_tool,) = [t for t in evalkit.service_tools(ctx) if t.name == tool]
        refused = {}
        for case_id, args in calls:
            out = json.loads(await service_tool.call(args))
            if "error" in out:
                refused[case_id] = (args, out["error"])
        assert refused == {}, json.dumps(refused, indent=1, ensure_ascii=False)
    finally:
        await evalworld.close(ctx)


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
    assert 'called: forecast {}' in text   # a run that fails shows what was called


async def test_with_the_instructions_the_system_line_carries_the_servers_instructions_after_the_neutral_line():
    from lib.service.instructions import instructions
    model = ScriptedModel({"average temp 3m": [("summary", {"readings": ["temperature"], "period": "last 3 months", "average": True})]})
    lines = []
    told = evalkit.service_instructions()
    assert told == instructions(evalkit.eval_context()) and "AirGradient" in told and "Australia/Melbourne" in told
    assert await evalkit.run_service([case("3m-average")], EXPECT, model, "m", instructions=told, out=lines.append) == 0
    system = model.requests[0]["messages"][0]["content"]
    assert system == evalkit.service_system(instructions=told) == f"{evalkit.service_system()}\n\n{told}"
    assert system.startswith(evalkit.SERVICE_SYSTEM) and lines[-1].endswith("(the service's ten tools alone, with its instructions)")
    assert [t["function"]["name"] for t in model.requests[0]["tools"]] == evalkit.service_tool_names()   # (the same tools)


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
    run = script("--service", "--instructions", "--ids", "hello")   # (offline it checks the expectations, as without)
    assert run.returncode == 0 and "1 of 1 cases pass (the service's ten tools alone, with its instructions)" in run.stdout
    run = script("--instructions")
    assert run.returncode != 0 and "--instructions goes with --service" in run.stderr


def test_a_rolling_period_in_words_is_not_a_month_that_its_days_happen_to_cover():
    """A period sent in words is read as words: "last 28 days" is not "September", though on the 29th its whole days to yesterday are
    1 to 28 September; start and end are still read in whole days as days reads them."""
    from tests import evalkit as ek
    for words in ("last 28 days", "4w", "past 4 weeks"):
        assert ek._means({"period": words}, "September", "days") is not None
        assert ek._means({"period": words}, "this month", "days") is not None
    assert ek._means({"period": "September"}, "September", "days") is None
    assert ek._means({"start": "2026-09-01"}, "September", "days") is None


def test_a_bare_word_read_fairly_passes_and_a_chat_line_with_any_call_fails():
    """The calls the first live run made that answer the question as a client with only the tool list reads it (each case's note says
    why): "metrics" as the readings now, a weather report for today (Tuesday) as the report. A chat line ("only that it's cold", "ok
    now what?") is not a request: any call fails it, so the eval shows a model that fetches readings for small talk."""
    check = evalkit.check_calls
    assert check([("current", {})], EXPECT["metrics-bare"]) == [] and check([("about", {})], EXPECT["metrics-bare"]) == []
    report = [("current", {}), ("forecast", {"period": "today"}), ("pollen", {})]
    assert check(report, EXPECT["weather-report-for-a-day"]) == []
    for cid in ("only-that-its-cold", "now-what"):
        assert check([], EXPECT[cid]) == [], cid
        for call in (report, [("current", {"readings": ["temperature"]})], [("summary", {"period": "today"})]):
            assert check(call, EXPECT[cid]), (cid, call)
    assert check([("summary", {})], EXPECT["metrics-bare"])


def test_a_period_sent_with_start_and_end_beside_it_is_scored_as_the_tools_read_it():
    """common.read_period: beside junk start and end, or dates that read as the same span, the period is read, so the checker scores
    the period (the live run's uv-days-two-years sent period with start 'count_only', end 'cancel'; most-rain-days-jja sent 'June to
    August 2026' with the matching start 2026-06-01, end 2026-08-31). Dates that read as another span are refused by the tools, so the
    checker fails them (use-five-minute-data sent days {period: "on record", start: "2025-01-01", end: "2026-09-28"})."""
    from tests import evalkit as ek
    assert ek._means({"period": "June to August", "start": "count_only", "end": "cancel"}, "June to August", "days") is None
    assert ek._means({"period": "June to August 2026", "start": "2026-06-01", "end": "2026-08-31"}, "June to August", "days") is None
    assert ek._means({"period": "this week", "start": "2026-06-01", "end": "2026-08-31"}, "June to August", "days") is not None
    disagree = ek._means({"period": "on record", "start": "2025-01-01", "end": "2026-09-28"}, "on record", "days")
    assert disagree and "refused" in disagree and "but `start`/`end` say" in disagree
    call = ("days", {"sort_by": "rain", "period": "June to August", "start": "count_only", "end": "cancel"})
    assert ek.check_calls([call], EXPECT["most-rain-days-jja"]) == []


def test_a_rating_with_rain_passes_in_chart_readings_or_in_readings_but_not_as_pm25_alone():
    """rating-against-rainfall: deepseek put "air.pm2_5:rating" in `readings` (10 Nov 2026); summary takes it there too (summarised,
    drawn as its rating), so the case takes either place, but not a rating dropped by chart_readings sent without it."""
    from tests import evalkit as ek
    want = EXPECT["rating-against-rainfall"]
    base = {"period": "last 30 days", "chart": True}
    assert ek.check_calls([("summary", {**base, "chart_readings": ["air.pm2_5:rating", "rainfall.rain"]})], want) == []
    assert ek.check_calls([("summary", {**base, "readings": ["air.pm2_5:rating", "rain"]})], want) == []
    assert ek.check_calls([("summary", {**base, "readings": ["air.pm2_5:rating", "rain"], "chart_readings": ["pm2.5", "rain"]})], want)
    assert ek.check_calls([("summary", {**base, "readings": ["pm2.5", "rain"]})], want)


def test_the_hottest_day_ranked_by_days_must_be_the_hottest():
    """think-hottest-week's days call ranks the hottest first: the coldest (order asc), a count or a statistic isn't the answer."""
    from tests import evalkit as ek
    want = ek.load_service_cases()["think-hottest-week"]["args"]["days"]

    def fails(args):
        return [k for k, w in want.items() if k != "period" and ek._matches(args.get(k), w)]
    assert fails({"sort_by": "temp_max", "period": "this week"}) == []
    assert fails({"sort_by": "temp_max", "order": "asc"}) == ["order"]
    assert fails({"sort_by": "temp_max", "count_only": True}) == ["count_only"]
    assert fails({"stat": "avg", "of": "temp_avg"}) == ["stat"]


# ---------- the bot's live run over the frozen world, with a scripted model ----------
# `--live` answers the bot's model from the real ten tools (evalworld.world), so a turn can take more than one round: it is scored over
# the whole turn. These run it with a model that reads what its calls gave, over the same world (the module's archive, at EVAL_NOW,
# copied fresh for each run).
class Reacting:
    """An OpenAI-style client whose steps are scripted: each a text (the reply), a list of (tool, args), or a function of what the last
    step's calls gave (each result as the model was sent it, parsed) that returns one. Past the script it says "Done."."""

    def __init__(self, *steps):
        self.steps, self.requests, self.seen = list(steps), [], []
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **kw):
        self.requests.append(kw)
        given = []
        for m in reversed(kw["messages"]):
            if m["role"] != "tool":
                break
            text = m["content"].split("\n", 1)[-1] if m["content"].startswith(evalkit.REPEATED) else m["content"]
            given.insert(0, json.loads(text))
        self.seen.append(given)
        step = self.steps.pop(0) if self.steps else "Done."
        step = step(given) if callable(step) else step
        calls = None if isinstance(step, str) else [NS(id=f"c{i}", function=NS(name=n, arguments=json.dumps(a)))
                                                    for i, (n, a) in enumerate(step)]
        return NS(choices=[NS(message=NS(content=step if isinstance(step, str) else "", tool_calls=calls))], usage=None)


@pytest.fixture
def eval_world(tmp_path, eval_archive, monkeypatch):
    """The frozen world (what evalworld.world gives a run: fresh tools over a new copy of the archive for each run), on the module's
    archive."""
    at_eval_now(monkeypatch)
    return evalworld.World(tmp_path, eval_archive)


async def bot_run(case_id: str, model: Reacting, world) -> tuple[int, str]:
    lines = []
    failed = await evalkit.run_service([case(case_id)], EXPECT, model, "m", bot=True, world=world, out=lines.append)
    return failed, "\n".join(lines)


async def test_a_refused_call_then_a_corrected_one_passes_and_the_refusal_is_counted(eval_world):
    model = Reacting([("summary", {"readings": ["flux capacitor"], "period": "3m", "average": True})],
                     lambda given: [("summary", {"readings": ["temperature"], "period": "3m", "average": True})] if "error" in given[0]
                     else "gave up",
                     "Averaged 14.2 °C.")
    failed, text = await bot_run("3m-average", model, eval_world)
    assert failed == 0, text
    assert "1/1  3m-average" in text and '2 calls, 1 refused: "Averaged 14.2 °C."' in text
    assert "isn't a reading this service knows" in model.seen[1][0]["error"]   # (the real refusal, as the model was sent it)
    async with eval_world.fresh() as service:
        assert model.requests[0]["messages"][0]["content"] == evalkit.bot_system(ctx=service.ctx)


async def test_a_second_call_made_on_a_note_passes(eval_world):
    """rain-per-month: one figure for the year comes back with days' note_group_by; the model acts on it and asks again by month."""
    asked = {"stat": "sum", "of": "rain", "period": "this year"}
    model = Reacting([("days", asked)],
                     lambda given: [("days", {**asked, "group_by": "month"})] if "note_group_by" in given[0] else "One figure.",
                     lambda given: f"{len(given[0]['by_month'])} months.")
    failed, text = await bot_run("rain-per-month", model, eval_world)
    assert failed == 0, text
    assert '2 calls, 0 refused: "9 months."' in text   # (January to September: the real days, by month)
    without = Reacting([("days", asked)], lambda given: "One figure.")
    failed, text = await bot_run("rain-per-month", without, eval_world)
    assert failed == 1 and "group_by" in text and '1 call, 0 refused: "One figure."' in text


async def test_a_forbidden_tool_in_a_later_round_fails(eval_world):
    model = Reacting([("relate", {})], [("days", {"stat": "max", "of": "pressure_max", "group_by": "month"})], "Weak.")
    failed, text = await bot_run("pressure-rain-correlation", model, eval_world)
    assert failed == 1 and "0/1 FAIL  pressure-rain-correlation" in text and "should not have called days" in text
    assert "2 calls, 0 refused" in text


async def test_a_turn_of_more_than_eight_calls_has_run_away_and_fails(eval_world):
    many = [[("days", {"stat": "sum", "of": "rain", "group_by": "month", "period": f"last {n} days"})] for n in range(40, 49)]
    failed, text = await bot_run("rain-per-month", Reacting(*many, "Rain by month."), eval_world)
    assert failed == 1 and "ran away: 9 calls (more than 8)" in text and "9 calls, 0 refused" in text
    failed, text = await bot_run("rain-per-month", Reacting(*many[:8], "Rain by month."), eval_world)
    assert "ran away" not in text and '8 calls, 0 refused: "Rain by month."' in text


async def test_a_turn_is_capped_at_the_agents_steps():
    """However long the model keeps calling, the turn takes at most lib/llm.py's MAX_STEPS (10) steps of calls, then it is made to
    answer."""
    from lib import llm
    assert llm.MAX_STEPS == 10
    model = Reacting(*[[("about", {})]] * 30)
    turn = await evalkit.run_live(case("3m-average"), model, "m")
    assert len(model.requests) == llm.MAX_STEPS + 1 and len(turn.calls) == llm.MAX_STEPS
    assert model.requests[-1]["tool_choice"] == "none"


async def test_the_world_is_built_at_the_frozen_clock_and_its_tools_give_real_results_then_the_clock_is_put_back():
    import time
    async with evalworld.world() as world, world.fresh() as service:
        assert time.time() == EVAL_NOW
        out = json.loads(await service["days"].call({"stat": "sum", "of": "rain", "group_by": "month", "period": "this year"}))
        assert "error" not in out and out["period"]["text"] == "this year: Thu 1 Jan – Mon 28 Sep 2026"
        assert list(out["by_month"]) == [f"2026-{m:02d}" for m in range(1, 10)] and out["days_checked"]
        assert [t.name for t in service] == evalkit.service_tool_names()
    assert time.time() != EVAL_NOW


async def test_each_run_has_its_own_copy_of_the_world_so_a_case_scores_the_same_whatever_ran_before(eval_world):
    """A call can fill the station's file from the fakes (a day in 2023, fetched and stored, moves "on record" back to it): each run of
    each case gets fresh tools over a new copy of the archive, so the same case run first and again after another gives the same."""
    def holidays():
        return Reacting([("days", {"only": "public_holiday", "sort_by": "temp_max"})], lambda given: given[0]["period"]["text"])
    first = await bot_run("public-holidays", holidays(), eval_world)
    assert first[0] == 0 and '"on record: Sat 29 Mar 2025 – Mon 28 Sep 2026"' in first[1]
    old_day = {"readings": ["outdoor.temperature"], "start": "2023-11-10 11:00", "end": "2023-11-11 11:00"}
    assert (await bot_run("a-day-in-2023", Reacting([("summary", old_day)], "Mild."), eval_world))[0] == 0
    assert await bot_run("public-holidays", holidays(), eval_world) == first


async def test_a_matching_call_that_was_refused_meets_nothing_and_a_forbidden_one_still_fails(eval_world):
    """3m-average: the expected summary, with an argument it doesn't take, is refused; an answered call follows, then a give-up reply."""
    refused = ("summary", {"readings": ["temperature"], "period": "3m", "average": True, "bogus": 1})
    assert evalkit.check_calls([refused], EXPECT["3m-average"]) == []   # (its tool and arguments match: only its refusal tells)
    failed, text = await bot_run("3m-average", Reacting([refused], [("about", {})], "Sorry, I can't average that."), eval_world)
    assert failed == 1 and "did not call summary (called: about)" in text and "2 calls, 1 refused" in text
    failed, text = await bot_run("pressure-rain-correlation",
                                 Reacting([("relate", {})], [("days", {"stat": "max", "of": "pressure_max", "bogus": 1})],
                                          [("about", {})], "Weak."), eval_world)
    assert failed == 1 and "should not have called days" in text and "3 calls, 1 refused" in text


async def test_a_turn_whose_reply_is_empty_or_the_fallback_or_that_ends_on_a_refusal_fails(eval_world):
    from lib.llm import FALLBACK
    asked = ("summary", {"readings": ["temperature"], "period": "3m", "average": True})
    failed, text = await bot_run("3m-average", Reacting([asked], ""), eval_world)   # (the Agent replies FALLBACK)
    assert failed == 1 and "the reply is the Agent's fallback" in text and f'1 call, 0 refused: "{FALLBACK}"' in text
    bad = ("summary", {**asked[1], "bogus": 1})
    failed, text = await bot_run("3m-average", Reacting([asked], [bad], "Averaged 14.2 °C."), eval_world)
    assert failed == 1 and "the turn ended on a refused call (summary" in text and '2 calls, 1 refused: "Averaged 14.2 °C."' in text
    failed, text = await bot_run("3m-average", Reacting([bad], [asked], "Averaged 14.2 °C."), eval_world)
    assert failed == 0, text
    assert evalkit.check_turn(evalkit.LiveTurn([asked], " ", [False]), EXPECT["3m-average"]) == ["the reply is empty"]
    assert evalkit.check_turn(evalkit.LiveTurn([asked], "Averaged.", [False]), EXPECT["3m-average"]) == []


async def test_the_tools_alone_answer_no_data_to_every_call_and_score_every_call_of_the_turn():
    """--service: every call is answered CANNED and the model may call again (the Agent's loop): a call in its second round counts,
    and the docs say so (not "the first round")."""
    import ast
    model = Reacting([("forecast", {})], lambda given: [("outlook", {})] if given == [json.loads(evalkit.CANNED)] else "?", "No data.")
    lines = []
    assert await evalkit.run_service([case("will-it-rain")], EXPECT, model, "m", out=lines.append) == 0, lines
    assert len(model.requests) == 3
    script_doc = ast.get_docstring(ast.parse((ROOT / "scripts" / "eval_prompts.py").read_text()))
    for text in (script_doc, evalkit.run_service.__doc__, evalkit.__doc__, (ROOT / "docs" / "mcp-service.md").read_text()):
        assert "first round" not in text and "(one round)" not in text
