import json
from types import SimpleNamespace as NS

from lib import charts, llm
from lib.bot import split_message, strip_chart_talk
from lib.tools import Tool, Tools


class FakeLLM:
    """Answers from a script: each item is a text answer or a list of (tool, args) calls."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **kw):
        self.requests.append(kw)
        step = self.script.pop(0)
        said = ""
        if isinstance(step, tuple):   # (text, calls): words written alongside tool calls
            said, step = step
        calls = None if isinstance(step, str) else [
            NS(id=f"c{i}", function=NS(name=n, arguments=json.dumps(a))) for i, (n, a) in enumerate(step)]
        msg = NS(content=step if isinstance(step, str) else said, tool_calls=calls)
        return NS(choices=[NS(message=msg)], usage=None)


def tools():
    seen = []

    async def handler(args, turn=None):
        seen.append(args)
        return json.dumps({"ok": True})
    return Tools([Tool("weather_now", "d", {"type": "object", "properties": {}}, handler)]), seen


async def test_fast_path_calls_the_model_only_at_the_end():
    t, seen = tools()
    client = FakeLLM(["Sunny."])
    agent = llm.Agent(client, "m", t)
    msgs = [{"role": "user", "content": "highs today"}]
    reply = await agent.run(msgs, "sys", "none", first_call=("weather_now", {"groups": "outdoor"}))
    assert reply == "Sunny." and len(client.requests) == 1 and seen == [{"groups": "outdoor"}]
    assert client.requests[0]["tool_choice"] == "auto" and client.requests[0]["extra_body"] == {"reasoning": {"effort": "none"}}
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]


async def test_weather_questions_must_fetch_first_and_reasoning_is_passed_through():
    t, seen = tools()
    client = FakeLLM([[("weather_now", {})], "Rain unlikely."])
    agent = llm.Agent(client, "m", t)
    reply = await agent.run([{"role": "user", "content": "will it rain?"}], "sys", "medium", tool_choice="required")
    assert reply == "Rain unlikely." and [r["tool_choice"] for r in client.requests] == ["required", "auto"]
    assert all(r["tools"] == t.schemas for r in client.requests)          # every tool on every call: the cached prompt holds
    assert client.requests[0]["extra_body"]["reasoning"]["effort"] == "medium" and len(seen) == 1


async def test_chat_needs_no_tools_and_identical_calls_are_made_once():
    t, seen = tools()
    client = FakeLLM(["You're welcome!"])
    assert await llm.Agent(client, "m", t).run([{"role": "user", "content": "thanks"}], "s", "none",
                                               tool_choice="auto") == "You're welcome!"
    assert client.requests[0]["tool_choice"] == "auto"
    client = FakeLLM([[("weather_now", {}), ("weather_now", {})], "done"])
    await llm.Agent(client, "m", t).run([{"role": "user", "content": "x"}], "s", "none")
    assert len(seen) == 1
    t, seen = tools()                                    # the same arguments in another order are the same call
    client = FakeLLM([[("weather_now", {"groups": "outdoor", "chart": True}), ("weather_now", {"chart": True, "groups": "outdoor"})], "done"])
    msgs = [{"role": "user", "content": "x"}]
    await llm.Agent(client, "m", t).run(msgs, "s", "none")
    assert len(seen) == 1 and "You already made this exact call" in msgs[-2]["content"]


def test_history_keeps_questions_and_answers_only():
    msgs = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "", "tool_calls": [1]},
            {"role": "tool", "content": "raw"}, {"role": "assistant", "content": "a"}]
    kept = llm.strip_tool_turns(msgs)
    assert [m["role"] for m in kept] == ["user", "assistant"]
    long = [m for i in range(30) for m in ({"role": "user", "content": str(i)}, {"role": "assistant", "content": "a"})]
    trimmed = llm.trim_history(long, 10)
    assert trimmed[0]["role"] == "user" and len(trimmed) <= 10


def test_chart_captions_lose_chart_talk_and_long_text_splits():
    assert strip_chart_talk("Chart attached below.\nPeak 31°C on Fri") == "Peak 31°C on Fri"
    assert all(len(c) <= 100 for c in split_message("line\n" * 100, 100))


def test_charts_render_for_both_datasets():
    ts = [1_780_000_000 + i * 1800 for i in range(96)]
    from zoneinfo import ZoneInfo

    from lib.specs import Bars, Chart, Line, Panel, Shares
    line = lambda label, ys, **kw: Line(label, ts, ys, **kw)
    single = Chart("Temperature", "x", [Panel("Temperature", "°C", [
        line("Outdoor", [10 + i % 9 for i in range(96)], records={"high": (ts[8], 19), "low": (ts[0], 10)}),
        line("Indoor", [20 + (i % 3) / 2 for i in range(96)])])])
    rain = Bars("Rain", "mm", ts[::12], [0.4 * (i % 3) for i in range(8)], 6 * 3600, "6 hours")
    behind = Chart("Pressure, Rain", "y", [Panel("Pressure", "hPa", [line("Pressure", [1010 + i % 5 for i in range(96)])], bars=rain)])
    shares = Shares("PM2.5 rating", ts[::12], 6 * 3600, [100.0] * 8, [0.0] * 8, [0.0] * 8, "6 hours")
    rated = Chart("PM2.5 rating, Rain", "z", [Panel("PM2.5 rating", "%", shares=shares), Panel("Rain", "mm", bars=rain)])
    air = Chart("Air quality", "w", [
        Panel("CO₂, VOC index", "", [line("CO₂", [450 + i for i in range(96)]), line("VOC index", [100 + i % 20 for i in range(96)])]),
        Panel("PM1, PM2.5, PM10", "µg/m³", [line("PM1", [3 + i % 4 for i in range(96)]), line("PM2.5", [5 + i % 7 for i in range(96)]),
                                               line("PM10", [8 + i % 9 for i in range(96)])]),
        Panel("NOx index", "", [line("NOx index", [1 + i % 3 for i in range(96)])], zones=(20, 150))])
    for spec in (single, behind, rated, air):
        assert charts.render(spec, ZoneInfo("Australia/Melbourne"))[:8] == b"\x89PNG\r\n\x1a\n"


async def test_reset_forgets_only_that_chat(alert_state):
    from lib.bot import Bot
    sent = []

    async def reply_text(text, **kw):
        sent.append(text)
    bot = Bot(None, None, [], alert_state)
    bot.chats[(1, None)].history.append({"role": "user", "content": "hi"})
    bot.chats[(2, None)].history.append({"role": "user", "content": "other chat"})
    bot.chats[(1, 7)].history.append({"role": "user", "content": "topic 7"})
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text)
    update = NS(effective_message=msg, effective_chat=NS(type="private", title=None),
                effective_user=NS(username="rob", full_name="Rob"))
    await bot.on_reset(update, None)
    assert (1, None) not in bot.chats and bot.chats[(2, None)].history and bot.chats[(1, 7)].history
    assert sent == ["Conversation memory cleared."]
    topic = NS(chat_id=1, message_thread_id=7, is_topic_message=True, reply_text=reply_text)
    await bot.on_reset(NS(effective_message=topic, effective_chat=update.effective_chat,
                          effective_user=update.effective_user), None)
    assert (1, 7) not in bot.chats and bot.chats[(2, None)].history


def test_the_prompt_defines_a_rainy_day_and_how_to_phrase_the_count():
    from datetime import datetime
    from lib import prompt
    from lib.ecowitt import days
    text = prompt.build(datetime(2026, 9, 29, 14, 5), {"Ecowitt": "Ecowitt"})
    assert "rain >= 1" in text and "any rain" in text and "It rained on 507 of 1,454 days" in text
    assert "1 mm or more" in days.DESCRIPTION


def test_the_prompt_asks_for_a_footnote_about_hotter_trace_days():
    from datetime import datetime
    from lib import prompt
    text = prompt.build(datetime(2026, 9, 29, 14, 5), {"Ecowitt": "Ecowitt"})
    assert "trace_rain_days" in text and "footnote" in text


def test_the_day_tool_schema_agrees_with_the_prompt_about_a_rainy_day():
    from lib.ecowitt import days
    where = days.PARAMETERS["properties"]["where"]["description"]
    assert "rain >= 1" in where and "rain > 0" not in where


def test_the_day_tool_rules_are_short_separate_bullets():
    from datetime import datetime
    from lib import prompt
    text = prompt.build(datetime(2026, 9, 29, 14, 5), {"Ecowitt": "Ecowitt"})
    block = text[text.index("- For questions that rank, compare or count DAYS"):text.index("- To ask whether rain comes WITH")]
    bullets = [line for line in block.splitlines() if line.startswith("  - ")]
    assert len(bullets) == 9 and all(len(b) < 300 for b in bullets)
    for needle in ("sort_by", "rain >= 1", "It rained on 507 of 1,454 days", "trace_rain_days", "note_daily", "on record", "public_holiday", "known day", "RECORD"):
        assert sum(needle in b for b in bullets) == 1, needle   # each rule lives in exactly one bullet


def test_the_system_prompt_carries_the_period_hints_only_when_there_are_some():
    from datetime import datetime
    from lib import intent, prompt
    now = datetime(2026, 9, 29, 14, 5)
    with_hint = prompt.build(now, {"Ecowitt": "Ecowitt"}, intent.period_hints("average temp 3m", now))
    assert "THE PERSON'S WORDS NAME THESE PERIODS" in with_hint and '"3m" = last 3 months: 2026-07-01' in with_hint
    assert "THE PERSON'S WORDS NAME" not in prompt.build(now, {"Ecowitt": "Ecowitt"}, intent.period_hints("hello", now))


def test_the_prompt_says_what_the_bot_can_and_cannot_do_for_the_sources_it_has():
    from datetime import datetime
    from lib import prompt
    now = datetime(2026, 9, 29, 14, 5)
    both = prompt.build(now, {"Ecowitt": "Ecowitt weather station", "AirGradient": "AirGradient outdoor sensor"})
    assert "WHAT THIS BOT CAN AND CAN'T DO" in both and "PM2.5 (µg/m³)" in both and "weather_link" in both
    assert "Custom alerts" in both and "solar radiation and UV index" in both and "Not available: lightning" in both and "correct the call once" in both
    weather_only = prompt.build(now, {"Ecowitt": "Ecowitt weather station"})
    assert "Weather station:" in weather_only and "Air quality (outdoor AirGradient)" not in weather_only
    assert "Weather station:" not in prompt.build(now, {"AirGradient": "AirGradient outdoor sensor"})


async def test_a_question_about_the_bot_itself_gets_no_tools_and_the_hint():
    from datetime import datetime
    from lib import prompt
    t, seen = tools()
    client = FakeLLM(["I track temperature, humidity..."])
    reply = await llm.Agent(client, "m", t).run([{"role": "user", "content": "list our metrics"}], "sys", "none",
                                                tool_choice="none")
    assert reply.startswith("I track") and client.requests[0]["tool_choice"] == "none" and seen == []
    now = datetime(2026, 9, 29, 14, 5)
    assert "ABOUT THE BOT ITSELF" in prompt.build(now, {"Ecowitt": "Ecowitt"}, [], True) and "ABOUT THE BOT ITSELF" not in prompt.build(now, {"Ecowitt": "Ecowitt"})


def test_the_prompt_never_shows_a_temperature_emoji_to_copy_and_says_no_entry_means_no_emoji():
    from datetime import datetime
    from lib import prompt
    text = prompt.build(datetime(2026, 9, 29, 14, 5), {"Ecowitt": "Ecowitt weather station"})
    assert '"outdoor.temperature": "' not in text and "a reading with no entry gets NO emoji" in text


def chunk(content=None, calls=None, usage=None):
    delta = NS(content=content, tool_calls=calls)
    return NS(choices=[NS(delta=delta)] if (content is not None or calls) else [], usage=usage)


def piece(index, id=None, name=None, args=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=args))


class StreamingLLM:
    """Streams each scripted answer as chunks: a list of chunks per call."""

    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **kw):
        self.requests.append(kw)
        chunks = self.script.pop(0)

        async def gen():
            for c in chunks:
                yield c
        return gen()


async def test_streamed_answers_give_the_same_result_and_report_the_text_so_far():
    t, seen = tools()
    usage = NS(prompt_tokens=10, completion_tokens=5)
    client = StreamingLLM([
        [chunk(calls=[piece(0, "c0", "weather_now", '{"gr')]), chunk(calls=[piece(0, None, None, 'oups": "outdoor"}')])],
        [chunk("It is "), chunk("12 degrees."), chunk(usage=usage)]])
    texts = []
    msgs = [{"role": "user", "content": "how hot"}]
    reply = await llm.Agent(client, "m", t).run(msgs, "sys", "none", on_text=texts.append)
    assert reply == "It is 12 degrees." and seen == [{"groups": "outdoor"}]
    assert texts == ["It is ", "It is 12 degrees."]
    assert client.requests[0]["stream"] is True and client.requests[0]["stream_options"] == {"include_usage": True}
    assert msgs[1]["tool_calls"][0]["function"] == {"name": "weather_now", "arguments": '{"groups": "outdoor"}'}


def test_a_rain_chart_caption_is_only_the_least_and_most_rain():
    from datetime import datetime
    from lib import prompt
    now = datetime(2026, 9, 29, 14, 5)
    text = prompt.build(now, {"Ecowitt": "Ecowitt weather station"}, rain_caption=True)
    assert "THIS IS A RAIN CHART" in text and "rain_outlook" not in text
    assert "THIS IS A RAIN CHART" not in prompt.build(now, {"Ecowitt": "Ecowitt weather station"})


def test_questions_about_the_bot_are_answered_as_simple_bullet_points():
    from datetime import datetime
    from lib import prompt
    text = prompt.build(datetime(2026, 9, 29, 14, 5), {"Ecowitt": "Ecowitt weather station"}, about_bot=True)
    assert "simple bullet points" in text and "no sub-bullets" in text and "7 at most" in text and "No dates" in text


def test_the_capabilities_mention_the_new_sources_only_when_they_are_on():
    from lib import prompt
    base = prompt.capabilities(["Ecowitt"])
    assert "pollen and thunderstorm asthma" in base.split("Not available:")[1] and "forecasts (only a short read" in base
    both = prompt.capabilities(["Ecowitt", "Pollen", "Forecast"])
    assert "tool pollen_asthma" in both and "tool weather_forecast" in both and "thunderstorm asthma risk reaching High" in both
    assert "pollen" not in both.split("Not available:")[1] and "forecasts" not in both.split("Not available:")[1]


async def test_several_fast_calls_run_together_before_the_model():
    seen = []

    async def handler(args, turn=None):
        seen.append(args)
        return json.dumps({"ok": True})
    t = Tools([Tool("weather_now", "d", {"type": "object", "properties": {}}, handler),
               Tool("air_quality", "d", {"type": "object", "properties": {}}, handler)])
    client = FakeLLM(["The report."])
    msgs = [{"role": "user", "content": "report"}]
    reply = await llm.Agent(client, "m", t).run(msgs, "sys", "none", first_call=[("weather_now", {"groups": "outdoor"}), ("air_quality", {})])
    assert reply == "The report." and len(client.requests) == 1 and seen == [{"groups": "outdoor"}, {}]
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "tool", "assistant"]
    assert [tc["id"] for tc in msgs[1]["tool_calls"]] == ["fast_1", "fast_2"] and [m["tool_call_id"] for m in msgs[2:4]] == ["fast_1", "fast_2"]





def test_the_capabilities_mention_forecast_revision_alerts_only_with_the_forecast_source():
    from lib import prompt
    assert "revised" not in prompt.capabilities(["Ecowitt"])
    assert "a forecast it sent being revised" in prompt.capabilities(["Ecowitt", "Forecast"])


def test_the_capabilities_list_only_the_alerts_the_sources_send_with_their_own_thresholds():
    from lib import prompt
    from lib.alerts.weather import GUST_ALERT_KMH
    station, air = prompt.capabilities(["Ecowitt"]), prompt.capabilities(["AirGradient"])
    assert f"wind gusts over {GUST_ALERT_KMH} km/h" in station and "mask alerts" not in station
    assert "mask alerts" in air and "rain starting" not in air and "Alerts, sent" not in prompt.capabilities([])


def test_the_capabilities_say_pollen_only_runs_october_to_december():
    from lib import prompt
    assert "only run from October to December" in prompt.capabilities(["Ecowitt", "Pollen"])


def test_the_prompt_leaves_the_heads_up_to_the_bot_and_points_at_the_count_tool():
    from datetime import datetime
    from lib import prompt
    text = prompt.build(datetime(2026, 9, 29, 14, 5), {"Ecowitt": "Ecowitt weather station"})
    assert "the bot warns the chat by itself" in text and "\u26a0" not in text and "decline" not in text.lower()
    assert "uv_max >= 9" in text and "count_only" in text


async def test_the_chat_is_warned_once_before_the_model_is_sent_more_than_twice_a_500_point_querys_data():
    from lib.llm import CHARS_PER_TOKEN, WARN_TOKENS
    from lib.tools import Turn
    warned, asked_after = [], []

    async def on_heavy(tokens):
        warned.append(tokens)
        asked_after.append(len(client.requests))   # how many model calls had been made when the warning was sent

    async def big(args, turn=None):
        return "x" * (WARN_TOKENS * CHARS_PER_TOKEN // 2 + 100)       # a bit over half the threshold each
    t = Tools([Tool("weather_history", "d", {"type": "object", "properties": {}}, big)])
    client = FakeLLM([[("weather_history", {"n": 1})], [("weather_history", {"n": 2})], [("weather_history", {"n": 3})], "Done."])
    reply = await llm.Agent(client, "m", t).run([{"role": "user", "content": "q"}], "sys", "none", turn=Turn(), on_heavy=on_heavy)
    assert reply == "Done." and len(warned) == 1 and warned[0] > WARN_TOKENS
    assert asked_after == [2]            # after the second result pushed it over, before the model was sent them

    small = FakeLLM([[("weather_history", {"n": 1})], "Done."])
    async def tiny(args, turn=None):
        return "x" * 500
    warned.clear()
    t2 = Tools([Tool("weather_history", "d", {"type": "object", "properties": {}}, tiny)])
    assert await llm.Agent(small, "m", t2).run([{"role": "user", "content": "q"}], "sys", "none", turn=Turn(), on_heavy=on_heavy) == "Done."
    assert warned == []


def test_the_prompt_says_to_use_the_whole_record_without_a_period_and_holidays_only_when_asked():
    from datetime import datetime
    from lib import prompt
    text = prompt.build(datetime(2026, 9, 29, 14, 5), {"Ecowitt": "Ecowitt weather station"})
    assert "use the whole record" in text and "only when the person's words say holidays or weekends" in text


async def test_the_reasoning_effort_goes_to_openrouter_in_its_own_form():
    t, _ = tools()
    client = FakeLLM([[("weather_now", {})], "Done."])
    await llm.Agent(client, "m", t).run([{"role": "user", "content": "q"}], "sys", "low")
    assert [r["extra_body"] for r in client.requests] == [{"reasoning": {"effort": "low"}}] * 2 and not any("extra_headers" in r for r in client.requests)


def test_older_answers_are_cut_to_their_first_lines_but_the_last_answer_and_every_question_stay_whole():
    long = "Mon 3 - Sun 9 Aug:\n" + "\n".join(f"• Day {i}: 10 to 20 °C, a long line of detail" for i in range(30))
    history = [{"role": "user", "content": "q1 " + "x" * 500}, {"role": "assistant", "content": long},
               {"role": "user", "content": "q2"}, {"role": "assistant", "content": long},
               {"role": "user", "content": "q3"}, {"role": "assistant", "content": long}]
    out = llm.shorten_old(history)
    assert out[1]["content"].endswith("…") and len(out[1]["content"]) <= llm.OLD_ANSWER_CHARS + 2 and out[1]["content"].startswith("Mon 3 - Sun 9 Aug:")
    assert out[3]["content"].endswith("…") and out[5]["content"] == long            # the last answer is whole
    assert out[0]["content"].startswith("q1 xxx") and len(out[0]["content"]) > 500        # questions untouched
    short = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "It is 12 degrees."}, {"role": "user", "content": "q"},
             {"role": "assistant", "content": "ok"}]
    assert llm.shorten_old(short) == short                                               # short answers are never changed
    assert llm.shorten_old(out) == out                                                   # idempotent
    one_line = [{"role": "assistant", "content": "word " * 200}, {"role": "assistant", "content": "last"}]
    assert len(llm.shorten_old(one_line)[0]["content"]) <= llm.OLD_ANSWER_CHARS + 2


async def test_a_question_that_asks_for_the_air_sensors_own_readings_is_sent_the_tools_that_offer_them():
    from lib.tools import Turn

    async def handler(args, turn=None):
        return json.dumps({"ok": True})
    base = {"type": "object", "properties": {"series": {"enum": ["humidity"]}}}
    wider = {"type": "object", "properties": {"series": {"enum": ["humidity", "air_humidity"]}}}
    t = Tools([Tool("plot_chart", "d", base, handler, asked=lambda: ("d+", wider)), Tool("weather_now", "d", {"type": "object", "properties": {}}, handler)])
    plain, asked = FakeLLM(["a"]), FakeLLM(["b"])
    await llm.Agent(plain, "m", t).run([{"role": "user", "content": "x"}], "sys", "none", turn=Turn())
    await llm.Agent(asked, "m", t).run([{"role": "user", "content": "x"}], "sys", "none", turn=Turn(air_extra=True))
    assert plain.requests[0]["tools"] == t.schemas and "air_humidity" not in json.dumps(plain.requests[0]["tools"])
    names = [s["function"]["name"] for s in asked.requests[0]["tools"]]
    assert names == ["plot_chart", "weather_now"] and "air_humidity" in json.dumps(asked.requests[0]["tools"])


async def test_a_tool_that_fails_answers_the_model_with_the_error_and_only_a_bug_logs_its_traceback(caplog):
    import logging

    async def broken(args, turn=None):
        raise RuntimeError("Ecowitt is down")

    async def buggy(args, turn=None):
        return {}["missing"]
    t = Tools([Tool("weather_now", "d", {"type": "object", "properties": {}}, broken),
               Tool("weather_history", "d", {"type": "object", "properties": {}}, buggy)])
    with caplog.at_level(logging.ERROR, logger="lib.tools"):
        assert await t.call("weather_now", "{}") == "Error calling tool: Ecowitt is down"
        assert await t.call("weather_history", "{}") == "Error calling tool: 'missing'"
    down, bug = caplog.records
    assert not down.exc_info and bug.exc_info                        # a failure is a line; a bug in the code keeps its traceback
