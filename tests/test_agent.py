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
        calls = None if isinstance(step, str) else [
            NS(id=f"c{i}", function=NS(name=n, arguments=json.dumps(a))) for i, (n, a) in enumerate(step)]
        msg = NS(content=step if isinstance(step, str) else "", tool_calls=calls)
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
    assert client.requests[0]["tool_choice"] == "auto" and client.requests[0]["extra_body"] == {"reasoning_effort": "none"}
    assert [m["role"] for m in msgs] == ["user", "assistant", "tool", "assistant"]


async def test_weather_questions_must_fetch_first_and_reasoning_is_passed_through():
    t, seen = tools()
    client = FakeLLM([[("weather_now", {})], "Rain unlikely."])
    agent = llm.Agent(client, "m", t)
    reply = await agent.run([{"role": "user", "content": "will it rain?"}], "sys", "medium", require_tool=True)
    assert reply == "Rain unlikely." and client.requests[0]["tool_choice"] == "required"
    assert client.requests[0]["extra_body"]["reasoning_effort"] == "medium" and len(seen) == 1


async def test_chat_needs_no_tools_and_identical_calls_are_made_once():
    t, seen = tools()
    client = FakeLLM(["You're welcome!"])
    assert await llm.Agent(client, "m", t).run([{"role": "user", "content": "thanks"}], "s", "none",
                                               require_tool=False) == "You're welcome!"
    assert client.requests[0]["tool_choice"] == "auto"
    client = FakeLLM([[("weather_now", {}), ("weather_now", {})], "done"])
    await llm.Agent(client, "m", t).run([{"role": "user", "content": "x"}], "s", "none")
    assert len(seen) == 1


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
    line = {"kind": "line", "title": "Temperature", "subtitle": "x", "unit": "°C", "series": [
        {"label": "Outdoor", "x": ts, "y": [10 + i % 9 for i in range(96)], "records": {"high": [ts[8], 19], "low": [ts[0], 10]}},
        {"label": "Indoor", "x": ts, "y": [20 + (i % 3) / 2 for i in range(96)]}]}
    air = {"kind": "panels", "title": "Air quality", "subtitle": "y", "panels": [
        {"label": "PM2.5", "unit": "µg/m³", "zones": [9, 55.4], "x": ts, "y": [5 + i % 7 for i in range(96)]},
        {"label": "CO₂", "unit": "ppm", "zones": [799, 1499], "x": ts, "y": [450 + i for i in range(96)]}]}
    from zoneinfo import ZoneInfo
    for spec in (line, air):
        assert charts.render(spec, ZoneInfo("Australia/Melbourne"))[:8] == b"\x89PNG\r\n\x1a\n"


async def test_reset_forgets_only_that_chat():
    from lib.bot import Bot
    sent = []

    async def reply_text(text, **kw):
        sent.append(text)
    bot = Bot(None, None, [], None)
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
    text = prompt.build(datetime(2026, 9, 29, 14, 5), ["Ecowitt"])
    assert "rain >= 1" in text and "any rain" in text and "It rained on 507 of 1,454 days" in text
    assert "1 mm or more" in days.DESCRIPTION


def test_the_prompt_asks_for_a_footnote_about_hotter_trace_days():
    from datetime import datetime
    from lib import prompt
    text = prompt.build(datetime(2026, 9, 29, 14, 5), ["Ecowitt"])
    assert "trace_rain_days" in text and "footnote" in text


def test_the_day_tool_schema_agrees_with_the_prompt_about_a_rainy_day():
    from lib.ecowitt import days
    where = days.PARAMETERS["properties"]["where"]["description"]
    assert "rain >= 1" in where and "rain > 0" not in where


def test_the_day_tool_rules_are_short_separate_bullets():
    from datetime import datetime
    from lib import prompt
    text = prompt.build(datetime(2026, 9, 29, 14, 5), ["Ecowitt"])
    block = text[text.index("- For questions that rank, compare or count DAYS"):text.index("- Use weather_now")]
    bullets = [line for line in block.splitlines() if line.startswith("  - ")]
    assert len(bullets) == 9 and all(len(b) < 300 for b in bullets)
    for needle in ("sort_by", "rain >= 1", "It rained on 507 of 1,454 days", "trace_rain_days", "note_daily", "on record", "public_holiday", "known day", "RECORD"):
        assert sum(needle in b for b in bullets) == 1, needle   # each rule lives in exactly one bullet


def test_the_system_prompt_carries_the_period_hints_only_when_there_are_some():
    from datetime import datetime
    from lib import intent, prompt
    now = datetime(2026, 9, 29, 14, 5)
    with_hint = prompt.build(now, ["Ecowitt"], intent.period_hints("average temp 3m", now))
    assert "THE PERSON'S WORDS NAME THESE PERIODS" in with_hint and '"3m" = last 3 months: 2026-07-01' in with_hint
    assert "THE PERSON'S WORDS NAME" not in prompt.build(now, ["Ecowitt"], intent.period_hints("hello", now))


def test_the_prompt_says_what_the_bot_can_and_cannot_do_for_the_sources_it_has():
    from datetime import datetime
    from lib import prompt
    now = datetime(2026, 9, 29, 14, 5)
    both = prompt.build(now, ["Ecowitt weather station", "AirGradient outdoor sensor"])
    assert "WHAT THIS BOT CAN AND CAN'T DO" in both and "PM2.5 (µg/m³)" in both and "weather_link" in both
    assert "Custom alerts" in both and "solar and UV" in both and "correct the call once" in both
    weather_only = prompt.build(now, ["Ecowitt weather station"])
    assert "Weather station:" in weather_only and "Air quality (outdoor AirGradient)" not in weather_only
    assert "Weather station:" not in prompt.build(now, ["AirGradient outdoor sensor"])


async def test_a_question_about_the_bot_itself_gets_no_tools_and_the_hint():
    from datetime import datetime
    from lib import prompt
    t, seen = tools()
    client = FakeLLM(["I track temperature, humidity..."])
    reply = await llm.Agent(client, "m", t).run([{"role": "user", "content": "list our metrics"}], "sys", "none",
                                                require_tool=False, no_tools=True)
    assert reply.startswith("I track") and client.requests[0]["tool_choice"] == "none" and seen == []
    now = datetime(2026, 9, 29, 14, 5)
    assert "ABOUT THE BOT ITSELF" in prompt.build(now, ["Ecowitt"], [], True) and "ABOUT THE BOT ITSELF" not in prompt.build(now, ["Ecowitt"])


def test_the_full_report_instructions_are_added_only_for_a_report_request():
    from datetime import datetime
    from lib import prompt
    now = datetime(2026, 9, 29, 14, 5)
    assert "FULL CURRENT REPORT" in prompt.build(now, ["Ecowitt"], report=True) and "air_quality (no dates)" in prompt.build(now, ["Ecowitt"], report=True)
    assert "FULL CURRENT REPORT" not in prompt.build(now, ["Ecowitt"])
