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


def test_the_prompt_is_the_persona_then_the_servers_instructions_then_the_time():
    """docs/ecobot-mcp.md 4: the bot adds only its persona (a friendly Telegram bot, plain text, bullets, the caption a photo allows)
    to the server's instructions; nothing about the tools, the periods or the data."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from lib import prompt
    from lib.service.instructions import INSTRUCTIONS
    now = datetime(2026, 11, 10, 14, 5, tzinfo=ZoneInfo("Australia/Melbourne"))
    text = prompt.build(now, INSTRUCTIONS)
    assert text.startswith("You are a friendly weather bot on Telegram") and INSTRUCTIONS in text
    assert text.rstrip().endswith("Now: Tuesday 10 November 2026, 14:05 (Australia/Melbourne time).")
    assert "900 characters" in text and "•" in text and "Plain text only" in text
    for gone in ("weather_history", "weather_now", "RIGHT NOW", "start_date", "chart_field", "TIME PERIODS", "Fahrenheit"):
        assert gone not in text, gone
