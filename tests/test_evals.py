"""The saved questions: the decisions made in code must hold for every case, and the checker itself must work.
(What the real model does with them is scripts/eval_prompts.py --live.)"""

import pytest

from lib import evalkit

CASES = evalkit.load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_the_decisions_made_in_code_hold(case):
    fails = evalkit.deterministic(case)
    if case.xfail:  # a known gap: the case says what should happen. When it starts passing, remove the xfail note.
        assert fails, f"{case.id} now passes: remove its xfail"
        pytest.xfail(case.xfail)
    assert fails == []


def test_case_ids_are_unique_and_every_case_expects_something():
    assert len({c.id for c in CASES}) == len(CASES) and all(c.expect for c in CASES)


def test_the_call_checker_reports_what_is_wrong():
    expect = {"tools": ["weather_days"], "first_tool": "weather_days",
              "args": {"weather_days": {"start_date": "2026-09-05", "where": {"absent": True}, "sort_by": {"any_of": ["temp_max"]}}}}
    good = [("weather_days", {"start_date": "2026-09-05", "sort_by": "temp_max"})]
    assert evalkit.check_calls(good, expect) == []
    bad = [("weather_history", {}), ("weather_days", {"start_date": "2026-09-01", "where": [{"field": "rain"}], "sort_by": "rain"})]
    fails = " | ".join(evalkit.check_calls(bad, expect))
    assert "first tool" in fails and "start_date" in fails and "where" in fails and "sort_by" in fails
    assert evalkit.check_calls([], {"tools": ["weather_link"]}) == ["did not call weather_link (called: nothing)"]
    assert evalkit.check_calls([("weather_now", {})], {"not_tools": ["weather_now"]}) == ["should not have called weather_now"]
    assert evalkit.check_args({"g": ["a", "b"]}, {"g": {"has": ["a"]}}) == [] and evalkit.check_args({"g": "x"}, {"g": {"regex": "^y"}})


def test_the_real_tool_definitions_are_what_the_live_run_offers():
    tools = evalkit.make_tools([])
    assert set(tools.by_name) == set(evalkit.tool_names())
