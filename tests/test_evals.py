"""The saved questions: the decision made in code (the reasoning effort) must hold for every case, and the checker itself must work.
(What the real model does with them is scripts/eval_prompts.py --live.)"""

import pytest

from tests import evalkit

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


def test_every_kind_of_question_metric_and_chart_has_a_saved_case():
    covered = {tag for c in CASES for tag in c.covers}
    assert covered <= set(evalkit.COVERAGE), f"unknown tags: {covered - set(evalkit.COVERAGE)}"
    assert not set(evalkit.COVERAGE) - covered, f"no case for: {sorted(set(evalkit.COVERAGE) - covered)}"


def test_the_call_checker_reports_what_is_wrong():
    expect = {"tools": ["days"], "first_tool": "days",
              "args": {"days": {"period": "this month", "where": {"absent": True}, "sort_by": {"any_of": ["temp_max"]}}}}
    good = [("days", {"period": "this month", "sort_by": "temp_max"})]
    assert evalkit.check_calls(good, expect) == []
    bad = [("summary", {}), ("days", {"period": "last month", "where": [{"field": "rain"}], "sort_by": "rain"})]
    fails = " | ".join(evalkit.check_calls(bad, expect))
    assert "first tool" in fails and "period" in fails and "where" in fails and "sort_by" in fails
    assert evalkit.check_calls([], {"tools": ["relate"]}) == ["did not call relate (called: nothing)"]
    assert evalkit.check_calls([("current", {})], {"not_tools": ["current"]}) == ["should not have called current"]
    assert evalkit.check_args({"g": ["a", "b"]}, {"g": {"has": ["a"]}}) == [] and evalkit.check_args({"g": "x"}, {"g": {"regex": "^y"}})


async def test_the_bots_live_run_is_its_own_request_and_every_question_goes_to_the_model():
    """--live asks the model as the bot does (its prompt: the persona, the server's instructions, the time; the ten tools; its effort);
    the questions it once answered in code (the report, a named reading's chart) are asked of the model too (step 6c)."""
    from tests.test_agent import FakeLLM
    from tests.test_service_evals import case
    client = FakeLLM([[("summary", {"readings": ["temperature"], "period": "last 3 months", "average": True})], "Done."])
    turn = await evalkit.run_live(case("3m-average"), client, "m")
    assert turn.calls == [("summary", {"readings": ["temperature"], "period": "last 3 months", "average": True})]
    assert turn.reply == "Done." and turn.refused == 0
    first = client.requests[0]
    assert first["messages"][0] == {"role": "system", "content": evalkit.bot_system()}
    assert first["messages"][0]["content"].startswith("You are a friendly weather bot on Telegram") and first["tool_choice"] == "auto"
    assert [t["function"]["name"] for t in first["tools"]] == evalkit.service_tool_names()
    assert first["extra_body"]["reasoning"]["effort"] == evalkit.effort(case("3m-average"))
    for case_id, calls in (("report", [("current", {}), ("pollen", {}), ("forecast", {"period": "today"})]),
                           ("button-rain-chart", [("summary", {"readings": ["rain"], "period": "7d", "chart": True})])):
        client = FakeLLM([calls, "Done."])
        turn = await evalkit.run_live(case(case_id), client, "m")
        assert turn.calls == calls and turn.reply == "Done." and len(client.requests) == 2, case_id
        assert client.requests[0]["messages"][-1] == {"role": "user", "content": case(case_id).ask}
