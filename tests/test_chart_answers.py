"""A question that is only a chart: the model sets chart_answers_it, the service's result comes complete (its chart and its `reply`),
and the turn ends with that reply: the model is asked nothing more (docs/ecobot-mcp.md 3.7). A real Agent over a scripted model and
stand-in service tools (offered as the bot offers them, lib/model_tools.py), through the bot's own model call."""

import json
import logging
from datetime import datetime
from types import SimpleNamespace as NS

from lib import intent, llm, model_tools
from lib.bot import Bot
from lib.tools import Turn
from tests.fakes import TZ, chart_data
from tests.test_agent import FakeLLM

NOW = datetime(2026, 9, 29, 14, 5)   # a Tuesday
ONLY_A_CHART = "plot pressure since Sunday"
REPLY = "Sun 27 Sep – Tue 29 Sep 2026\nPressure: low 1004.2 hPa, Mon 28 Sep at 4am · high 1021.7 hPa, Sun 27 Sep at 10pm"
COMPLETE = {"period": {"text": "Sun 27 – Tue 29 Sep 2026"}, "caption": REPLY, "charts": [chart_data("Pressure")], "reply": REPLY,
            "complete": True}
ASKED = {"readings": ["pressure"], "period": "since Sunday", "chart": True, "chart_answers_it": True}


class StandIn:
    """A service tool that answers `result` (a dict, or an exception it raises) and keeps the arguments it was sent."""

    def __init__(self, name: str = "summary", result=COMPLETE):
        self.name, self.description, self.parameters, self.result, self.seen = name, "d", {"type": "object", "properties": {}}, result, []

    async def call(self, args, meta):
        self.seen.append(args)
        if isinstance(self.result, Exception):
            raise self.result
        return json.dumps(self.result, ensure_ascii=False)


async def ask(alert_state, script, tools, text=ONLY_A_CHART):
    """`text` through the bot's model call with a scripted model: (reply, the model's requests, the turn, the conversation)."""
    client = FakeLLM(script)
    bot = Bot(NS(tz=TZ), llm.Agent(client, "m", model_tools.tools(tools)), [], alert_state)
    read = intent.read(text, NOW)
    turn = Turn()
    working = [{"role": "user", "content": text}]
    reply = await bot._ask_model(working, "sys", read, turn, None)
    return reply, client.requests, turn, working


async def test_a_complete_result_is_the_reply_and_the_model_is_asked_once(alert_state, caplog):
    tool = StandIn()
    assert intent.read(ONLY_A_CHART, NOW).chart is None                       # the model takes it, not a shortcut
    with caplog.at_level(logging.INFO, logger="lib.llm"):
        reply, requests, turn, working = await ask(alert_state, [[("summary", ASKED)], "the model's own caption"], [tool])
    assert reply == REPLY and len(requests) == 1 and len(tool.seen) == 1
    assert turn.results[0][2]["charts"] == COMPLETE["charts"]                 # the whole result kept, for drawing
    assert working[-1] == {"role": "assistant", "content": REPLY}           # the conversation keeps it as the answer
    assert any("ended on a complete result (summary): no LLM 2" in r.getMessage() for r in caplog.records)


async def test_a_result_that_isnt_complete_leaves_the_answer_to_the_model(alert_state):
    for result in ({k: v for k, v in COMPLETE.items() if k not in ("reply", "complete")},   # no chart_answers_it, or a note to word
                   {**COMPLETE, "charts": []},                                              # complete, but no chart
                   {"error": "That period isn't one of the forms."},                         # refused
                   RuntimeError("Ecowitt is down")):                                         # failed
        reply, requests, _, _ = await ask(alert_state, [[("summary", ASKED)], "The model's answer."], [StandIn(result=result)])
        assert reply == "The model's answer." and len(requests) == 2, result


async def test_a_step_with_any_result_that_isnt_complete_goes_back_to_the_model(alert_state):
    complete, plain = StandIn(), StandIn("current", {"lines": {"station": ["• Outdoor: 12 °C"]}})
    reply, requests, _, _ = await ask(alert_state, [[("summary", ASKED), ("current", {})], "The model's answer."], [complete, plain])
    assert reply == "The model's answer." and len(requests) == 2


async def test_several_complete_results_are_the_reply_in_the_order_called(alert_state):
    other = {**COMPLETE, "reply": "PM2.5: peak 34.2 µg/m³"}
    days = StandIn("days", other)
    reply, requests, _, _ = await ask(alert_state, [[("summary", ASKED), ("days", {"group_by": "month", "chart_answers_it": True})], "x"],
                                      [StandIn(), days])
    assert reply == f"{REPLY}\n\nPM2.5: peak 34.2 µg/m³" and len(requests) == 1


async def test_the_model_is_sent_the_result_less_the_charts_data(alert_state):
    _, requests, _, _ = await ask(alert_state, [[("summary", {**ASKED, "chart_answers_it": False})], "Low on Monday."],
                                  [StandIn(result={k: v for k, v in COMPLETE.items() if k not in ("reply", "complete")})])
    told = json.loads(requests[1]["messages"][-1]["content"])
    assert told["charts"] == [{"title": "Pressure", "period": COMPLETE["charts"][0]["period"]}] and told["caption"] == REPLY
