"""A question that is only a chart: the model sets chart_answers_it, the chart's caption is written in code and the model is asked
nothing more (docs/mcp-service.md 3.7). A real Agent over a scripted model and stand-in tools, through the bot's own model call."""

import json
import logging
from datetime import datetime
from types import SimpleNamespace as NS

from lib import intent, llm
from lib.bot import Bot
from lib.specs import Chart, Line, Panel
from lib.tools import Tool, Tools, Turn
from tests.fakes import TZ
from tests.test_agent import FakeLLM

NOW = datetime(2026, 9, 29, 14, 5)   # a Tuesday
ONLY_A_CHART = "plot pressure since Sunday"
PERIOD = "Sun 27 Sep 2026 - Tue 29 Sep 2026"
PRESSURE = {"period": PERIOD, "series": {"pressure.relative": {
    "unit": "hPa", "low": "1004.2", "low_when": "at 4am", "low_date": "Mon 28 Sep 2026",
    "high": "1021.7", "high_when": "at 10pm", "high_date": "Sun 27 Sep 2026"}}, "chart": "(the chart hint)"}
CAPTION = ("Sun 27 Sep – Tue 29 Sep 2026\n"
           "Pressure: low 1004.2 hPa, Mon 28 Sep at 4am · high 1021.7 hPa, Sun 27 Sep at 10pm")
ASKED = {"start_date": "2026-09-27 00:00:00", "end_date": "2026-09-29 14:05:00", "groups": "pressure", "chart": True,
         "chart_field": "pressure"}
EMPTY = {"type": "object", "properties": {}}


def chart(title: str = "Pressure") -> Chart:
    return Chart(title, "", [Panel(title, "", [Line(title, [1, 2], [1.0, 2.0])])])


def stand_in(name: str = "weather_history", result: dict | str = PRESSURE, draws: bool = True):
    """The tool as a stand-in: it draws its chart (when `draws`) and answers `result`. Returns (the tool, the arguments it was given)."""
    seen = []

    async def handler(args, turn=None):
        seen.append(args)
        if isinstance(result, Exception):
            raise result
        if draws:
            turn.charts.append(chart())
        return result if isinstance(result, str) else json.dumps(result)
    return Tool(name, "d", EMPTY, handler), seen


async def ask(alert_state, script, tools, text=ONLY_A_CHART, turn=None):
    """`text` through the bot's model call with a scripted model: (reply, the model's requests, the turn, the conversation)."""
    client = FakeLLM(script)
    bot = Bot(NS(tz=TZ), llm.Agent(client, "m", Tools(tools)), [], alert_state)
    read = intent.read(text, NOW)
    turn = turn or Turn.of(read, text)
    working = [{"role": "user", "content": text}]
    reply = await bot._ask_model(working, "sys", read, turn, None)
    return reply, client.requests, turn, working


def test_the_tools_whose_chart_can_be_the_answer_offer_the_flag_and_say_when_to_set_it():
    from lib.airgradient import days as air_days, source as air
    from lib.ecowitt import days, source as station
    for parameters in (station.HISTORY_PARAMS, air.PARAMETERS, air.asked()[1], days.PARAMETERS, air_days.PARAMETERS):
        flag = parameters["properties"]["chart_answers_it"]
        assert flag["type"] == "boolean" and "nothing else to answer" in flag["description"] and "is a storm coming?" in flag["description"]


async def test_a_chart_that_answers_the_question_is_the_reply_written_in_code_and_the_model_is_asked_once(alert_state, caplog):
    tool, seen = stand_in()
    assert intent.read(ONLY_A_CHART, NOW).fast is None                      # the model takes it, not a shortcut
    with caplog.at_level(logging.INFO, logger="lib.llm"):
        reply, requests, turn, working = await ask(alert_state, [[("weather_history", {**ASKED, "chart_answers_it": True})],
                                                                 "the model's own caption"], [tool])
    assert reply == CAPTION and len(requests) == 1 and len(turn.charts) == 1 and len(seen) == 1
    assert working[-1] == {"role": "assistant", "content": CAPTION}       # the conversation keeps it as the answer
    assert any("ended on a chart that answers the question (weather_history): no LLM 2" in r.getMessage() for r in caplog.records)


async def test_without_the_flag_the_model_writes_the_answer_as_before(alert_state):
    tool, _ = stand_in()
    reply, requests, turn, _ = await ask(alert_state, [[("weather_history", ASKED)], "The model's answer."], [tool])
    assert reply == "The model's answer." and len(requests) == 2 and len(turn.charts) == 1
    reply, requests, _, _ = await ask(alert_state, [[("weather_history", {**ASKED, "chart_answers_it": False})], "Again."], [tool])
    assert reply == "Again." and len(requests) == 2


async def test_the_flag_with_no_chart_or_a_failed_call_leaves_the_answer_to_the_model(alert_state):
    flagged = [("weather_history", {**ASKED, "chart_answers_it": True})]
    for result, draws in (({k: v for k, v in PRESSURE.items() if k != "chart"}, False),   # no chart drawn
                          ({"error": "bad date"}, False),                                   # the tool said no
                          (RuntimeError("Ecowitt is down"), False),                         # the tool failed
                          ({"period": PERIOD, "series": {}, "chart": "(hint)"}, True)):     # a chart, but nothing to caption it with
        tool, _ = stand_in(result=result, draws=draws)
        reply, requests, _, _ = await ask(alert_state, [flagged, "The model's answer."], [tool])
        assert reply == "The model's answer." and len(requests) == 2, result


async def test_a_step_with_any_call_that_is_not_a_chart_answering_the_question_goes_back_to_the_model(alert_state):
    history, _ = stand_in()
    now, _ = stand_in("weather_now", result={"outdoor": {"temperature": "12 ℃"}}, draws=False)
    flagged = ("weather_history", {**ASKED, "chart_answers_it": True})
    for step in ([flagged, ("weather_history", ASKED | {"groups": "outdoor"})],    # one of the two calls didn't set it
                 [flagged, ("weather_now", {"chart_answers_it": True})]):        # a tool whose answer is not a chart
        reply, requests, _, _ = await ask(alert_state, [step, "The model's answer."], [history, now])
        assert reply == "The model's answer." and len(requests) == 2


async def test_several_charts_that_answer_the_question_are_captioned_in_the_order_called(alert_state):
    pressure, _ = stand_in()
    air = {"period": PERIOD, "pm2_5": {"unit": "µg/m³", "high": 34.2, "high_time": "Mon 28 Sep 2026 3:00pm", "high_rating": "🟠 Poor",
                                       "average": 8.1, "low": 1.0, "low_time": "x"}, "chart": "(hint)"}
    quality, _ = stand_in("air_quality", result=air)
    script = [[("weather_history", {**ASKED, "chart_answers_it": True}),
               ("air_quality", {"start_date": ASKED["start_date"], "end_date": ASKED["end_date"], "metrics": ["pm2_5"], "chart": True,
                                "chart_answers_it": True})], "unused"]
    reply, requests, turn, _ = await ask(alert_state, script, [pressure, quality], text="plot pressure and pm2.5 since Sunday")
    assert len(requests) == 1 and len(turn.charts) == 2
    assert reply == CAPTION + "\n\nSun 27 Sep – Tue 29 Sep 2026\n• PM2.5: peak 34.2 µg/m³ (Mon 28 Sep 3:00pm) · 🟠 Poor · average 8.1"


async def test_a_day_tools_chart_is_captioned_with_the_days_answer(alert_state):
    counted = {"period": "Sat 01 Aug 2026 - Mon 28 Sep 2026", "days_checked": 59, "matching_days": 17, "condition": "rain ≥ 1 mm",
               "by_month": {"2026-08": 9, "2026-09": 8}, "chart": "(hint)"}
    days, _ = stand_in("weather_days", result=counted)
    script = [[("weather_days", {"start_date": "2026-08-01", "end_date": "2026-09-28", "where": [{"field": "rain", "op": ">=", "value": 1}],
                                 "count_only": True, "group_by": "month", "chart_answers_it": True})], "unused"]
    reply, requests, _, _ = await ask(alert_state, script, [days], text="chart the rainy days each month since August")
    assert reply == "17 of 59 days with rain ≥ 1 mm (Sat 01 Aug – Mon 28 Sep 2026)." and len(requests) == 1


async def test_the_caption_speaks_of_the_readings_the_chart_shows_when_only_the_model_named_them(alert_state):
    both = {"period": PERIOD, "chart": "(hint)", "series": {
        "outdoor.temperature": {"unit": "℃", "low": "8.1", "high": "24.3"}, "outdoor.humidity": {"unit": "%", "low": "41", "high": "93"}}}
    tool, _ = stand_in(result=both)
    asked = {**ASKED, "groups": "outdoor", "chart_field": "humidity", "chart_answers_it": True}
    reply, _, _, _ = await ask(alert_state, [[("weather_history", asked)], "unused"], [tool], turn=Turn(chart_asked=True))
    assert reply.splitlines()[1].startswith("Humidity: low 41 %") and "Temperature" not in reply
    air = {"period": PERIOD, "chart": "(hint)", "pm2_5": {"unit": "µg/m³", "high": 9, "high_time": "x", "average": 3},
           "co2": {"unit": "ppm", "high": 812, "high_time": "Mon 28 Sep 2026 7:00am", "average": 455}}
    tool, _ = stand_in("air_quality", result=air)
    flagged = {"start_date": ASKED["start_date"], "end_date": ASKED["end_date"], "chart": True, "chart_answers_it": True}
    reply, _, _, _ = await ask(alert_state, [[("air_quality", flagged)], "unused"], [tool], text="graph the air since Sunday",
                               turn=Turn(chart_asked=True, air_named=["co2"]))   # no metrics: the tool charts the reading talked about
    assert reply.splitlines()[1:] == ["• CO₂: peak 812 ppm (Mon 28 Sep 7:00am) · average 455"]


async def test_the_bots_own_first_call_never_ends_the_turn():
    tool, seen = stand_in()
    finished = []

    def finish(calls, results):
        finished.append(calls)
        return "ended in code"
    client = FakeLLM(["The model's answer."])
    reply = await llm.Agent(client, "m", Tools([tool])).run(
        [{"role": "user", "content": "q"}], "sys", "none", first_call=("weather_history", {**ASKED, "chart_answers_it": True}),
        turn=Turn(), finish=finish)
    assert reply == "The model's answer." and len(client.requests) == 1 and finished == [] and len(seen) == 1


async def test_the_whole_answer_goes_out_with_its_chart(alert_state, monkeypatch):
    from lib import bot as botmod
    from tests.test_bot import telegram
    tool, _ = stand_in()
    client = FakeLLM([[("weather_history", {**ASKED, "chart_answers_it": True})], "unused"])
    sent = []

    async def reply_photo(photo, caption=None, **kw):
        sent.append(caption)
        return NS(message_id=7)

    async def send_chat_action(*a, **k):
        pass
    monkeypatch.setattr(botmod, "render_chart", lambda spec, tz, times=None: b"png")
    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt")]
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo,
             chat=NS(type="supergroup", title="g"), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    bot = Bot(NS(tz=TZ), llm.Agent(client, "m", Tools([tool])), sources, alert_state)
    await bot.respond(update, NS(bot=telegram(send_chat_action)), ONLY_A_CHART)
    assert sent == ["Pressure\n\n" + CAPTION] and len(client.requests) == 1
    assert bot.chats[(1, None)].history[-1] == {"role": "assistant", "content": CAPTION}
