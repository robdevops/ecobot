"""Bot.respond: what the person is sent when things go wrong."""

import asyncio
from datetime import datetime
from types import SimpleNamespace as NS

from lib import intent
from lib.bot import Bot
from lib.tools import Turn
from tests.fakes import TZ


class RaisingAgent:
    async def run(self, *a, **k):
        raise RuntimeError("401 Bearer xai-SECRET-1234 quota exceeded for org acme")


class AnsweringAgent:
    async def run(self, messages, system, effort, first_call=None, require_tool=True, no_tools=False, turn=None):
        self.first_call = first_call
        return "It is 12 degrees."


async def ask(agent, text="how hot is it"):
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)

    async def send_chat_action(*a, **k):
        pass
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    context = NS(bot=NS(send_chat_action=send_chat_action, id=99))
    await Bot(NS(tz=TZ), agent, [], None).respond(update, context, text)
    return replies


async def test_error_details_stay_in_the_log_not_the_chat():
    replies = await ask(RaisingAgent())
    assert len(replies) == 1 and "went wrong" in replies[0] and "try again" in replies[0].lower()
    assert "SECRET" not in replies[0] and "Bearer" not in replies[0] and "acme" not in replies[0]
    assert "RuntimeError" in replies[0]      # just the kind of problem


async def test_a_failing_shortcut_falls_back_to_the_model(monkeypatch):
    def boom(*a, **k):
        raise OverflowError("date value out of range")
    monkeypatch.setattr(intent, "fast_call", boom)
    agent = AnsweringAgent()
    assert await ask(agent, "weather last 99999 years") == ["It is 12 degrees."]
    assert agent.first_call is None


def test_a_reply_carries_the_message_it_replies_to_in_private_chats_too():
    from types import SimpleNamespace as NS
    bot = Bot(NS(tz=TZ), None, [], None)
    mine = NS(text="Absolute pressure\n• Low: 992.9 hPa at 2:20pm on Sat 5 Sep", caption=None, from_user=NS(id=99, full_name="Bot"))
    theirs = NS(text="lovely day", caption=None, from_user=NS(id=7, full_name="Ann"))
    private = NS(chat=NS(type="private"), from_user=NS(full_name="Rob"), reply_to_message=mine)
    assert bot._content(private, "did it rain on the lowest day", 99) == (
        '(replying to your earlier message: "Absolute pressure\n• Low: 992.9 hPa at 2:20pm on Sat 5 Sep") did it rain on the lowest day')
    assert bot._content(NS(**{**private.__dict__, "reply_to_message": None}), "hello", 99) == "hello"
    group = NS(chat=NS(type="group"), from_user=NS(full_name="Rob"), reply_to_message=theirs)
    assert bot._content(group, "same here", 99) == 'Rob (replying to Ann: "lovely day"): same here'
    assert bot._content(NS(**{**group.__dict__, "reply_to_message": None}), "hi", 99) == "Rob: hi"


async def test_a_hung_question_is_given_up_on_so_the_next_one_in_the_chat_is_answered(monkeypatch, caplog):
    import asyncio
    from lib import bot

    class Hanging:
        async def run(self, *a, **k):
            await asyncio.sleep(3600)

    monkeypatch.setattr(bot, "TURN_SECONDS", 0.3)
    monkeypatch.setattr(bot, "WATCHDOG_SECONDS", 0.1)
    with caplog.at_level("WARNING"):
        replies = await ask(Hanging())
    assert len(replies) == 1 and "took too long" in replies[0]
    assert "Still working" in caplog.text and "Gave up" in caplog.text


async def test_charts_are_drawn_one_at_a_time_from_any_thread():
    import asyncio
    from lib.charts import render
    from lib.specs import Chart, Line, Panel
    chart = Chart("t", "s", [Panel("P", "", [Line("L", [1_780_000_000 + i * 600 for i in range(30)], [float(i % 5) for i in range(30)])])])
    pngs = await asyncio.gather(*(asyncio.to_thread(render, chart, TZ) for _ in range(6)))
    assert all(p[:4] == b"\x89PNG" for p in pngs) and len({len(p) for p in pngs}) == 1


class DraftBot:
    """A Telegram bot that accepts drafts."""

    def __init__(self, fail=False):
        self.id, self.drafts, self.typing, self.fail = 99, [], 0, fail

    async def send_message_draft(self, chat_id, draft_id, text=None, message_thread_id=None):
        if self.fail:
            raise RuntimeError("drafts not allowed")
        self.drafts.append((draft_id, text))

    async def send_chat_action(self, *a, **k):
        self.typing += 1


class StreamingAgent:
    async def run(self, messages, system, effort, first_call=None, require_tool=True, no_tools=False, turn=None, on_text=None):
        await asyncio.sleep(0.05)       # "thinking"
        on_text("It is")
        await asyncio.sleep(0.05)
        on_text("It is 12 degrees.")
        await asyncio.sleep(0.05)
        return "It is 12 degrees."


async def ask_private(agent, bot, chat_type="private"):
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type=chat_type, title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await Bot(NS(tz=TZ), agent, [], None).respond(update, NS(bot=bot), "how hot is it")
    return replies


async def test_a_private_chat_shows_a_thinking_draft_that_streams_the_answer_then_sends_it(monkeypatch):
    monkeypatch.setattr("lib.bot.DRAFT_MIN_GAP", 0.01)
    bot = DraftBot()
    replies = await ask_private(StreamingAgent(), bot)
    assert replies == ["It is 12 degrees."]
    assert bot.drafts[0][1] is None                                    # empty: "Thinking..."
    assert [t for _, t in bot.drafts][-1] == "It is 12 degrees." and len({d for d, _ in bot.drafts}) == 1
    assert bot.typing == 0


async def test_an_idle_draft_is_refreshed_before_telegram_drops_it(monkeypatch):
    monkeypatch.setattr("lib.bot.DRAFT_REFRESH_SECONDS", 0.02)

    class Slow:
        async def run(self, *a, on_text=None, **k):
            await asyncio.sleep(0.15)
            return "ok"
    bot = DraftBot()
    await ask_private(Slow(), bot)
    assert len(bot.drafts) >= 4 and all(t is None for _, t in bot.drafts)


async def test_a_refused_draft_falls_back_to_typing_and_the_reply_still_arrives():
    bot = DraftBot(fail=True)
    assert await ask_private(StreamingAgent(), bot) == ["It is 12 degrees."]
    assert bot.drafts == [] and bot.typing >= 1


async def test_groups_never_get_drafts():
    bot = DraftBot()
    assert await ask_private(AnsweringAgent(), bot, chat_type="supergroup") == ["It is 12 degrees."]
    assert bot.drafts == []


async def test_a_generation_stopped_update_is_logged(caplog):
    update = NS(effective_message=NS(api_kwargs={"message_generation_stopped": {"draft_id": 1}}, chat_id=1),
                effective_chat=NS(type="private", id=1, title=None), effective_user=NS(username="rob", full_name="Rob"))
    with caplog.at_level("INFO", logger="lib.bot"):
        await Bot(NS(tz=TZ), None, [], None).on_other(update, None)
    assert "generation stopped" in caplog.text and "message_generation_stopped" in caplog.text


async def test_a_long_answer_is_cut_to_fit_one_chart_message_not_sent_as_text_then_picture():
    from lib import bot as botmod
    answer = "Fri 25 Sep - Thu 01 Oct 2026\n" + "\n".join(f"Reading {i}: 10.0 to 20.0 °C, a long description of it" for i in range(40))
    assert len(answer) > botmod.CAPTION_LIMIT
    sent = []

    async def reply_photo(photo, caption=None, caption_entities=None):
        sent.append(("photo", caption))

    async def reply_text(body, **kw):
        sent.append(("text", body))
    await botmod.deliver(NS(reply_photo=reply_photo, reply_text=reply_text), answer, [b"png"], link=("live chart", "https://x"))
    assert [kind for kind, _ in sent] == ["photo"]
    caption = sent[0][1]
    assert len(caption) <= botmod.CAPTION_LIMIT and caption.startswith("Fri 25 Sep") and "…" in caption and caption.endswith("live chart")
    assert botmod.fit_caption("short answer") == "short answer"


async def test_a_report_is_written_in_code_from_the_fast_path_calls_and_the_model_is_not_asked():
    import json

    class Tools:
        def __init__(self):
            self.called = []

        async def call(self, name, raw, turn=None):
            self.called.append(name)
            return {"weather_now": json.dumps({"outdoor": {"temperature": "12.3 ℃", "humidity": "94 %"}, "emoji": {"outdoor.humidity": "💦"}}),
                    "air_quality": json.dumps({"pm10": {"value": 3.0, "unit": "µg/m³", "rating": "🟢 good"}})}[name]

    class Agent:
        tools = Tools()

        async def run(self, *a, **k):
            raise AssertionError("the model must not be asked for a report")
    sources = [NS(name=n, wants=lambda t: True, poke=lambda: None, describe=lambda n=n: n) for n in ("Ecowitt", "AirGradient")]
    agent, replies = Agent(), []

    async def reply_text(body, **kw):
        replies.append(body)
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    bot = Bot(NS(tz=TZ), agent, sources, None)
    await bot.respond(update, NS(bot=NS(send_chat_action=send_chat_action, id=99)), "report")
    await bot.respond(update, NS(bot=NS(send_chat_action=send_chat_action, id=99)), "weather now")
    assert agent.tools.called == ["weather_now", "air_quality", "weather_now"]
    assert replies[0] == "Weather station\n• Outdoor: 12.3 °C, 💦 94 %\n\nAir quality\n• PM10: 3.0 µg/m³ 🟢 good"
    assert replies[1] == "• Outdoor: 12.3 °C, 💦 94 %"
    history = bot.chats[(1, None)].history
    assert history[-1] == {"role": "assistant", "content": replies[1]} and history[-4]["content"] == "report"


async def test_a_question_that_times_out_is_asked_again_one_reasoning_step_lower(monkeypatch):
    from lib import bot as botmod
    monkeypatch.setattr(botmod, "TURN_SECONDS", 0.05)
    monkeypatch.setattr(botmod, "RETRY_SECONDS", 0.5)
    efforts = []

    class SlowThenQuick:
        async def run(self, messages, system, effort, first_call=None, require_tool=True, no_tools=False, turn=None, on_text=None):
            efforts.append(effort)
            messages.append({"role": "assistant", "content": "(unfinished)"})
            if effort == "medium":
                await asyncio.sleep(10)
            return f"answered at {effort}"
    bot = Bot(NS(tz=TZ), SlowThenQuick(), [], None)
    working = [{"role": "user", "content": "q"}]
    read = intent.read("what do you think about the weather today?", datetime(2026, 10, 2, 12, 0))
    assert read.effort == "medium"
    assert await bot._ask_model(working, "sys", read, Turn(), None) == "answered at low"
    assert efforts == ["medium", "low"] and working == [{"role": "user", "content": "q"}, {"role": "assistant", "content": "(unfinished)"}]


async def test_a_question_with_no_lower_step_or_a_second_timeout_gives_up(monkeypatch):
    import pytest
    from lib import bot as botmod
    monkeypatch.setattr(botmod, "TURN_SECONDS", 0.05)
    monkeypatch.setattr(botmod, "RETRY_SECONDS", 0.05)
    efforts = []

    class Hangs:
        async def run(self, messages, system, effort, **k):
            efforts.append(effort)
            await asyncio.sleep(10)
    bot = Bot(NS(tz=TZ), Hangs(), [], None)
    for text, expected in (("hello there", ["none"]), ("what do you think about the weather today?", ["medium", "low"])):
        efforts.clear()
        with pytest.raises(asyncio.TimeoutError):
            await bot._ask_model([], "sys", intent.read(text, datetime(2026, 10, 2, 12, 0)), Turn(), None)
        assert efforts == expected


async def test_a_plain_chart_request_is_drawn_and_captioned_in_code_and_the_model_is_not_asked(monkeypatch):
    import json
    from lib import bot as botmod
    from lib.specs import Chart, Line, Panel

    class Tools:
        calls = []

        async def call(self, name, raw, turn=None):
            self.calls.append((name, json.loads(raw)))
            turn.charts.append(Chart("Humidity", "", [Panel("Humidity", "%", [Line("Outdoor", [1, 2], [50.0, 60.0])])]))
            return json.dumps({"period": "Fri 25 Sep 2026 - Thu 01 Oct 2026", "series": {
                "outdoor.humidity": {"unit": "%", "low": "50", "high": "60", "low_when": "at 5am", "low_date": "Fri 25 Sep 2026",
                                     "high_when": "at 3pm", "high_date": "Sat 26 Sep 2026"}}})

    class Agent:
        tools = Tools()

        async def run(self, *a, **k):
            raise AssertionError("the model must not be asked for a plain chart")
    sent = []

    async def reply_photo(photo, caption=None, **kw):
        sent.append(caption)
    monkeypatch.setattr(botmod, "render_chart", lambda spec, tz: b"png")
    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt")]
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    await Bot(NS(tz=TZ), Agent(), sources, None).respond(update, NS(bot=NS(send_chat_action=send_chat_action, id=99)), "Humidity chart 7d")
    name, args = Agent.tools.calls[0]
    assert name == "weather_history" and args["chart"] and args["groups"] == "outdoor,indoor"
    assert sent and "Humidity: low 50 %, Fri 25 Sep at 5am · high 60 %, Sat 26 Sep at 3pm" in sent[0]
