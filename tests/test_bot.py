"""Bot.respond: what the person is sent when things go wrong."""

import asyncio
from datetime import datetime
from types import SimpleNamespace as NS

from lib import intent
from lib.bot import Bot
from lib.tools import Turn
from tests.fakes import TZ, StandInTools


class RaisingAgent:
    async def run(self, *a, **k):
        raise RuntimeError("401 Bearer sk-or-SECRET-1234 quota exceeded for org acme")


class AnsweringAgent:
    async def run(self, messages, system, effort, *, first_call=None, turn=None, **k):
        self.first_call = first_call
        return "It is 12 degrees."


async def ask(alert_state, agent, text="how hot is it"):
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)

    async def send_chat_action(*a, **k):
        pass
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    context = NS(bot=NS(send_chat_action=send_chat_action, id=99))
    await Bot(NS(tz=TZ), agent, [], alert_state).respond(update, context, text)
    return replies


async def test_error_details_stay_in_the_log_not_the_chat(alert_state):
    replies = await ask(alert_state, RaisingAgent())
    assert len(replies) == 1 and "went wrong" in replies[0] and "try again" in replies[0].lower()
    assert "SECRET" not in replies[0] and "Bearer" not in replies[0] and "acme" not in replies[0]
    assert "RuntimeError" in replies[0]      # just the kind of problem


class NoModel:
    ready = False

    async def run(self, *a, **k):
        raise AssertionError("there is no model to ask")


async def test_without_a_model_a_question_for_it_is_told_so_and_what_works_still_answers(alert_state):
    replies = await ask(alert_state, NoModel(), "will it rain tomorrow, do you think?")
    assert len(replies) == 1 and "needs the AI model" in replies[0] and "/report" in replies[0] and "/air_quality" in replies[0]
    replies = await ask(alert_state, NoModel(), "what can you do")
    assert len(replies) == 1 and "needs the AI model" not in replies[0]               # answered in code, as before


async def test_a_failing_shortcut_falls_back_to_the_model(alert_state, monkeypatch):
    def boom(*a, **k):
        raise OverflowError("date value out of range")
    monkeypatch.setattr(intent, "fast_call", boom)
    agent = AnsweringAgent()
    assert await ask(alert_state, agent, "weather last 99999 years") == ["It is 12 degrees."]
    assert agent.first_call is None


def test_a_reply_carries_the_message_it_replies_to_in_private_chats_too(alert_state):
    from types import SimpleNamespace as NS
    bot = Bot(NS(tz=TZ), None, [], alert_state)
    mine = NS(text="Absolute pressure\n• Low: 992.9 hPa at 2:20pm on Sat 5 Sep", caption=None, from_user=NS(id=99, full_name="Bot"))
    theirs = NS(text="lovely day", caption=None, from_user=NS(id=7, full_name="Ann"))
    private = NS(chat=NS(type="private"), from_user=NS(full_name="Rob"), reply_to_message=mine)
    assert bot._content(private, "did it rain on the lowest day", 99) == (
        '(replying to your earlier message: "Absolute pressure\n• Low: 992.9 hPa at 2:20pm on Sat 5 Sep") did it rain on the lowest day')
    assert bot._content(NS(**{**private.__dict__, "reply_to_message": None}), "hello", 99) == "hello"
    group = NS(chat=NS(type="group"), from_user=NS(full_name="Rob"), reply_to_message=theirs)
    assert bot._content(group, "same here", 99) == 'Rob (replying to Ann: "lovely day"): same here'
    assert bot._content(NS(**{**group.__dict__, "reply_to_message": None}), "hi", 99) == "Rob: hi"


async def test_a_hung_question_is_given_up_on_so_the_next_one_in_the_chat_is_answered(alert_state, monkeypatch, caplog):
    import asyncio
    from lib import bot

    class Hanging:
        async def run(self, *a, **k):
            await asyncio.sleep(3600)

    monkeypatch.setattr(bot, "TURN_SECONDS", 0.3)
    monkeypatch.setattr(bot, "WATCHDOG_SECONDS", 0.1)
    with caplog.at_level("WARNING"):
        replies = await ask(alert_state, Hanging())
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
    async def run(self, messages, system, effort, *, on_text=None, **k):
        await asyncio.sleep(0.05)       # "thinking"
        on_text("It is")
        await asyncio.sleep(0.05)
        on_text("It is 12 degrees.")
        await asyncio.sleep(0.05)
        return "It is 12 degrees."


async def ask_private(alert_state, agent, bot, chat_type="private", text="how hot is it"):
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type=chat_type, title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await Bot(NS(tz=TZ), agent, [], alert_state).respond(update, NS(bot=bot), text)
    return replies


async def test_a_private_chat_shows_a_thinking_draft_that_streams_the_answer_then_sends_it(alert_state, monkeypatch):
    monkeypatch.setattr("lib.bot.DRAFT_MIN_GAP", 0.01)
    bot = DraftBot()
    replies = await ask_private(alert_state, StreamingAgent(), bot)
    assert replies == ["It is 12 degrees."]
    assert bot.drafts[0][1] is None                                    # empty: "Thinking..."
    assert [t for _, t in bot.drafts][-1] == "It is 12 degrees." and len({d for d, _ in bot.drafts}) == 1
    assert bot.typing >= 1                                             # "typing..." runs alongside the draft


async def test_an_idle_draft_is_refreshed_before_telegram_drops_it(alert_state, monkeypatch):
    monkeypatch.setattr("lib.bot.DRAFT_REFRESH_SECONDS", 0.02)

    class Slow:
        async def run(self, *a, on_text=None, **k):
            await asyncio.sleep(0.15)
            return "ok"
    bot = DraftBot()
    await ask_private(alert_state, Slow(), bot)
    assert len(bot.drafts) >= 4 and all(t is None for _, t in bot.drafts)


async def test_a_refused_draft_falls_back_to_typing_and_the_reply_still_arrives(alert_state):
    bot = DraftBot(fail=True)
    assert await ask_private(alert_state, StreamingAgent(), bot) == ["It is 12 degrees."]
    assert bot.drafts == [] and bot.typing >= 1


async def test_groups_never_get_drafts(alert_state):
    bot = DraftBot()
    assert await ask_private(alert_state, AnsweringAgent(), bot, chat_type="supergroup") == ["It is 12 degrees."]
    assert bot.drafts == []


async def test_a_generation_stopped_update_is_logged(alert_state, caplog):
    update = NS(effective_message=NS(api_kwargs={"message_generation_stopped": {"draft_id": 1}}, chat_id=1),
                effective_chat=NS(type="private", id=1, title=None), effective_user=NS(username="rob", full_name="Rob"))
    with caplog.at_level("INFO", logger="lib.bot"):
        await Bot(NS(tz=TZ), None, [], alert_state).on_other(update, None)
    assert "generation stopped" in caplog.text and "message_generation_stopped" in caplog.text


async def test_one_chart_and_a_short_answer_are_one_message_otherwise_the_answer_is_a_message_of_its_own():
    from lib import bot as botmod
    sent = []

    async def reply_photo(photo, caption=None, **kw):
        sent.append(("photo", caption))

    async def reply_text(body, **kw):
        sent.append(("text", body))

    async def reply_media_group(media, **kw):
        sent.append(("group", len(media)))
    msg = NS(reply_photo=reply_photo, reply_text=reply_text, reply_media_group=reply_media_group)
    await botmod.deliver(msg, "", [b"png"], link=("live chart", "https://x"), titles=["Humidity"])
    assert sent == [("photo", "Humidity")]                    # no text, no footer: just the title
    sent.clear()
    await botmod.deliver(msg, "Fri was the hottest day.", [b"png"], titles=["Temperature"])
    assert sent == [("photo", "Temperature\n\nFri was the hottest day.")]              # one message: the picture with its title and the answer
    sent.clear()
    await botmod.deliver(msg, "x" * 1100, [b"png"], titles=["Temperature"])
    assert [kind for kind, _ in sent] == ["text", "photo"] and sent[1][1] == "Temperature"   # too long for a caption: apart, as before
    sent.clear()
    await botmod.deliver(msg, "Both are up.", [b"a", b"b"], titles=["A", "B"])             # several charts: the answer first
    assert sent == [("text", "Both are up."), ("group", 2)]


async def test_a_report_is_written_in_code_from_the_fast_path_calls_and_the_model_is_not_asked(alert_state):
    import json

    class Tools(StandInTools):
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
    bot = Bot(NS(tz=TZ), agent, sources, alert_state)
    await bot.respond(update, NS(bot=NS(send_chat_action=send_chat_action, id=99)), "report")
    await bot.respond(update, NS(bot=NS(send_chat_action=send_chat_action, id=99)), "weather now")
    assert agent.tools.called == ["weather_now", "air_quality", "weather_now"]
    assert replies[0] == "Weather station\n• Outdoor: 12.3 °C, 💦 94 %\n\nAir quality\n• PM10: 3.0 µg/m³ 🟢 good"
    assert replies[1] == "• Outdoor: 12.3 °C, 💦 94 %"
    history = bot.chats[(1, None)].history
    assert history[-1] == {"role": "assistant", "content": replies[1]} and history[-4]["content"] == "report"


async def test_a_question_that_times_out_is_asked_again_one_reasoning_step_lower(alert_state, monkeypatch):
    from lib import bot as botmod
    monkeypatch.setattr(botmod, "TURN_SECONDS", 0.05)
    monkeypatch.setattr(botmod, "RETRY_SECONDS", 0.5)
    efforts = []

    class SlowThenQuick:
        async def run(self, messages, system, effort, **k):
            efforts.append(effort)
            messages.append({"role": "assistant", "content": "(unfinished)"})
            if effort == "medium":
                await asyncio.sleep(10)
            return f"answered at {effort}"
    bot = Bot(NS(tz=TZ), SlowThenQuick(), [], alert_state)
    working = [{"role": "user", "content": "q"}]
    read = intent.read("what do you think about the weather today?", datetime(2026, 10, 2, 12, 0))
    assert read.effort == "medium"
    assert await bot._ask_model(working, "sys", read, Turn(), None) == "answered at low"
    assert efforts == ["medium", "low"] and working == [{"role": "user", "content": "q"}, {"role": "assistant", "content": "(unfinished)"}]


async def test_a_question_with_no_lower_step_or_a_second_timeout_gives_up(alert_state, monkeypatch):
    import pytest
    from lib import bot as botmod
    monkeypatch.setattr(botmod, "TURN_SECONDS", 0.05)
    monkeypatch.setattr(botmod, "RETRY_SECONDS", 0.05)
    efforts = []

    class Hangs:
        async def run(self, messages, system, effort, **k):
            efforts.append(effort)
            await asyncio.sleep(10)
    bot = Bot(NS(tz=TZ), Hangs(), [], alert_state)
    for text, expected in (("hello there", ["none"]), ("what do you think about the weather today?", ["medium", "low"])):
        efforts.clear()
        with pytest.raises(TimeoutError):
            await bot._ask_model([], "sys", intent.read(text, datetime(2026, 10, 2, 12, 0)), Turn(), None)
        assert efforts == expected


async def test_a_plain_chart_request_is_drawn_and_captioned_in_code_and_the_model_is_not_asked(alert_state, monkeypatch):
    import json
    from lib import bot as botmod
    from lib.specs import Chart, Line, Panel

    class Tools(StandInTools):
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
    monkeypatch.setattr(botmod, "render_chart", lambda spec, tz, times=None: b"png")
    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt")]
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    await Bot(NS(tz=TZ), Agent(), sources, alert_state).respond(update, NS(bot=NS(send_chat_action=send_chat_action, id=99)), "Humidity chart 7d")
    name, args = Agent.tools.calls[0]
    assert name == "weather_history" and args["chart"] and args["groups"] == "outdoor,indoor"
    assert sent == ["Humidity"]    # the chart's title is its whole caption


async def test_a_chart_made_in_code_starts_no_refresh_and_other_questions_do(alert_state, monkeypatch):
    """The chart's own reads go first in line; a refresh started beside them would only queue requests ahead of the next ones."""
    import json
    from lib import bot as botmod
    from lib.specs import Chart, Line, Panel
    pokes = []

    class Tools(StandInTools):
        async def call(self, name, raw, turn=None):
            turn.charts.append(Chart("t", "", [Panel("Temperature", "°C", [Line("Outdoor", [1, 2], [1.0, 2.0])])]))
            return json.dumps({"period": "Fri 25 Sep 2026 - Thu 01 Oct 2026", "series": {
                "outdoor.temperature": {"unit": "℃", "low": "1", "high": "2"}}})

    class Agent:
        tools = Tools()

        async def run(self, *a, **k):
            return "It is 12 degrees."

    async def reply(*a, **k):
        return NS(message_id=7)
    monkeypatch.setattr(botmod, "render_chart", lambda spec, tz, times=None: b"png")
    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: pokes.append(1), describe=lambda: "Ecowitt")]
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_photo=reply, reply_text=reply,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    bot, context = Bot(NS(tz=TZ), Agent(), sources, alert_state), NS(bot=NS(send_chat_action=send_chat_action, id=99))
    await bot.respond(update, context, "Temperature chart 7d")
    assert pokes == []
    await bot.respond(update, context, "what do you think about the weather today?")
    assert pokes == [1]


async def test_a_chart_reply_logs_how_long_each_step_took(alert_state, tmp_path, monkeypatch, caplog):
    """One line per chart reply: reading the data, the calculation, drawing, the PNG (and its size) and sending it."""
    import re
    from lib.ecowitt import Ecowitt, api
    from lib.tools import Tools
    from tests.fakes import config, ecowitt_transport
    monkeypatch.setattr(api, "MIN_GAP_SECONDS", 0)
    eco = Ecowitt(config(tmp_path), transport=ecowitt_transport()[0])
    await eco.start()

    class Agent:
        tools = Tools(eco.tools)

        async def run(self, *a, **k):
            raise AssertionError("no model for a plain chart")
    sent = []

    async def reply_photo(photo, caption=None, **kw):
        sent.append(photo)
        return NS(message_id=7)
    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt")]
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    with caplog.at_level("DEBUG"):
        await Bot(NS(tz=TZ), Agent(), sources, alert_state).respond(update, NS(bot=NS(send_chat_action=send_chat_action, id=99)),
                                                             "Temperature chart 1d")
    await eco.close()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Chart ms:")]
    assert len(sent) == 1 and len(lines) == 1, lines
    kb = round(len(sent[0]) / 1024)
    assert re.fullmatch(rf"Chart ms: data \d+, calc \d+, draw \d+, PNG \d+ \({kb} KB\), send \d+", lines[0])


async def _ask_with(alert_state, tool_result, text, monkeypatch=None):
    """Ask `text` of a bot whose one tool returns `tool_result`; returns (replies, whether the model was asked)."""
    asked = []

    class Tools(StandInTools):
        async def call(self, name, raw, turn=None):
            return tool_result

    class Agent:
        tools = Tools()

        async def run(self, messages, *a, **k):
            asked.append(True)
            return "from the model"
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)
    sources = [NS(name=n, wants=lambda t: True, poke=lambda: None, describe=lambda n=n: n) for n in ("Ecowitt", "AirGradient")]
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    await Bot(NS(tz=TZ), Agent(), sources, alert_state).respond(update, NS(bot=NS(send_chat_action=send_chat_action, id=99)), text)
    return replies, bool(asked)


async def test_a_plain_reading_question_is_answered_in_code_without_the_model(alert_state):
    import json
    result = json.dumps({"outdoor": {"temperature": "12.3 ℃", "feels_like": "11.0 ℃"}, "indoor": {"temperature": "18.3 ℃"}})
    replies, asked = await _ask_with(alert_state, result, "how hot is it")
    assert not asked and replies == ["• Outdoor: 12.3 °C, feels like 11.0 °C\n• Indoor: 18.3 °C"]


async def test_a_lookup_that_cannot_be_written_falls_back_to_the_model(alert_state):
    import json
    replies, asked = await _ask_with(alert_state, json.dumps({"error": "station not answering"}), "how hot is it")
    assert asked and replies == ["from the model"]


async def test_about_the_bot_is_answered_in_code_without_the_model(alert_state):
    replies, asked = await _ask_with(alert_state, "{}", "what can you do")
    assert not asked and replies[0].startswith("• Temperature, humidity")


async def test_typing_starts_before_the_model_is_asked_and_runs_alongside_the_draft(alert_state):
    seen = {}

    class Agent:
        async def run(self, *a, **k):
            seen["typing_when_asked"], seen["drafts_when_asked"] = bot.typing, len(bot.drafts)
            await asyncio.sleep(0.05)
            return "ok"
    bot = DraftBot()
    assert await ask_private(alert_state, Agent(), bot) == ["ok"]
    assert seen["typing_when_asked"] >= 1 and bot.drafts


async def test_the_forecast_is_fetched_with_the_first_step_of_a_rain_ahead_question(alert_state):
    got = []

    class Agent:
        async def run(self, messages, system, effort, first_call=None, **k):
            got.append(first_call)
            return "Possibly."
    sources = [NS(name=n, wants=lambda t: True, poke=lambda: None, describe=lambda n=n: n) for n in ("Ecowitt", "Forecast")]
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    await Bot(NS(tz=TZ), Agent(), sources, alert_state).respond(update, NS(bot=NS(send_chat_action=send_chat_action, id=99)), "will it rain?")
    assert [name for name, _ in got[0]] == ["weather_now", "weather_history", "weather_forecast"] and replies == ["Possibly."]


async def _message_aged(alert_state, seconds):
    from datetime import datetime, timedelta, timezone
    answered = []

    class Agent:
        async def run(self, *a, **k):
            answered.append(True)
            return "ok"
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)

    async def send_chat_action(*a, **k):
        pass
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text, text="how hot is it",
             date=datetime.now(timezone.utc) - timedelta(seconds=seconds), chat=NS(type="private", title=None, id=1),
             from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    bot = Bot(NS(tz=TZ), Agent(), [], alert_state)
    await bot.on_message(update, NS(bot=NS(send_chat_action=send_chat_action, id=99, username="b")))
    return replies


async def test_a_message_sent_while_the_bot_was_starting_is_answered(alert_state, caplog):
    assert await _message_aged(alert_state, 120) == ["ok"]


async def test_a_message_that_waited_hours_is_ignored_and_logged(alert_state, caplog):
    with caplog.at_level("INFO", logger="lib.bot"):
        assert await _message_aged(alert_state, 2 * 3600) == []
    assert "Ignored a message 120 minutes old" in caplog.text


async def test_a_heavy_question_gets_a_heads_up_in_the_chat_before_the_answer_and_the_answer_still_comes(alert_state):
    class Agent:
        async def run(self, messages, system, effort, on_heavy=None, **k):
            await on_heavy(24000)
            return "The answer."
    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt")]
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    await Bot(NS(tz=TZ), Agent(), sources, alert_state).respond(update, NS(bot=NS(send_chat_action=send_chat_action, id=99)), "is it too cold to run")
    assert replies == ["\u26a0\ufe0f That's pulling in about 24k tokens of data, so it will take a little longer. Working on it...", "The answer."]


async def test_a_days_over_a_limit_question_is_counted_in_code_with_no_model(alert_state):
    import json
    result = json.dumps({"period": "Wed 5 Oct 2022 - Sat 3 Oct 2026", "days_checked": 1454, "matching_days": 137, "condition": "UV index ≥ 10"})
    replies, asked = await _ask_with(alert_state, result, "days over UVI 10 by year")
    assert not asked and replies == ["137 of 1,454 days with UV index ≥ 10 (Wed 5 Oct 2022 – Sat 3 Oct 2026)."]


async def test_rain_by_month_is_worked_out_in_code_with_no_model(alert_state):
    import json
    result = json.dumps({"period": "Fri 4 Oct 2024 - Sat 3 Oct 2026", "days_checked": 731, "matching_days": 731,
                         "stat": {"what": "Total rain", "of": "rain", "unit": "mm"}, "value": 1234.5})
    replies, asked = await _ask_with(alert_state, result, "rain by month for 2 years")
    assert not asked and replies == ["Total rain: 1234.5 mm over 731 days (Fri 4 Oct 2024 – Sat 3 Oct 2026)."]


async def test_the_model_is_asked_with_the_tool_choice_the_message_calls_for(alert_state):
    seen = []

    class Agent:
        async def run(self, messages, system, effort, **k):
            seen.append(k["tool_choice"])
            return "ok"
    for text in ("will it rain tomorrow?", "thanks!"):
        replies = []

        async def reply_text(body, **kw):
            replies.append(body)
        sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt")]
        msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
                 chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
        update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

        async def send_chat_action(*a, **k):
            pass
        await Bot(NS(tz=TZ), Agent(), sources, alert_state).respond(update, NS(bot=NS(send_chat_action=send_chat_action, id=99)), text)
    assert seen == ["required", "auto"]


async def test_weather_all_week_and_a_plain_chart_are_sent_with_just_their_title(alert_state, monkeypatch):
    import json
    from lib import bot as botmod
    from lib.specs import Chart, Line, Panel

    class Tools(StandInTools):
        async def call(self, name, raw, turn=None):
            turn.charts.append(Chart("t", "", [Panel("Temperature", "°C", [Line("Outdoor", [1, 2], [1.0, 2.0])])]))
            return json.dumps({"period": "Fri 25 Sep 2026 - Thu 01 Oct 2026", "series": {
                "outdoor.temperature": {"unit": "℃", "low": "1", "high": "2"}}})

    class Agent:
        tools = Tools()

        async def run(self, *a, **k):
            raise AssertionError("no model for a chart")
    sent = []

    async def reply_photo(photo, caption=None, **kw):
        sent.append(caption)
    monkeypatch.setattr(botmod, "render_chart", lambda spec, tz, times=None: b"png")
    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt")]
    for text, caption_wanted in (("ecowitt all week", True), ("temperature chart 7d", True)):
        sent.clear()
        msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo,
                 chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
        update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

        async def send_chat_action(*a, **k):
            pass
        await Bot(NS(tz=TZ), Agent(), sources, alert_state).respond(update, NS(bot=NS(send_chat_action=send_chat_action, id=99)), text)
        assert sent == ["t"], text


async def test_the_report_carries_a_refresh_button_that_redraws_it_in_place(alert_state):
    import json
    from lib import report_menu

    class Tools(StandInTools):
        reading = "12.3 ℃"

        async def call(self, name, raw, turn=None):
            return {"weather_now": json.dumps({"outdoor": {"temperature": self.reading}}),
                    "air_quality": json.dumps({"pm10": {"value": 3.0, "unit": "µg/m³", "rating": "🟢 good"}})}[name]

    class Agent:
        tools = Tools()

    class Query:
        data, edits, toast = report_menu.REFRESH, [], None

        async def answer(self, text=None, **kw):
            self.toast = text

        async def edit_message_text(self, text, reply_markup=None, **kw):
            self.edits.append((text, reply_markup))
            self.entities = kw.get("entities")
    sources = [NS(name=n, wants=lambda t: True, poke=lambda: None, describe=lambda n=n: n) for n in ("Ecowitt", "AirGradient")]
    sent = []

    async def reply_text(body, **kw):
        sent.append((body, kw.get("reply_markup"), kw.get("entities")))
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    context = NS(bot=NS(send_chat_action=send_chat_action, id=99))
    bot = Bot(NS(tz=TZ), Agent(), sources, alert_state)
    alert_state.add_chat(1, "Rob")
    alert_state.set_keyboard(1, bot.keyboard_version)   # its buttons are current, so a reply carries only its own
    await bot.respond(update, context, "report")
    await bot.respond(update, context, "weather now")
    assert [b.callback_data for b in sent[0][1].inline_keyboard[0]] == [report_menu.REFRESH] and sent[1][1] is None
    assert [e.type for e in sent[0][2]] == ["bold", "bold"] and sent[1][2] is None            # the report's headings are bold; "weather now" has none
    Tools.reading = "13.0 ℃"
    query = Query()
    await bot.on_report_button(NS(callback_query=query), context)
    assert query.toast is None and "13.0 °C" in query.edits[0][0] and query.edits[0][1].inline_keyboard[0][0].callback_data == report_menu.REFRESH
    assert [e.type for e in query.entities] == ["bold", "bold"]


async def test_a_button_press_too_old_to_answer_is_only_logged_and_other_errors_still_raise(caplog):
    import pytest
    from telegram.error import BadRequest

    from lib.bot import answer_query

    class Query:
        data = "pd:7"

        def __init__(self, error=None):
            self.error, self.told = error, []

        async def answer(self, *args, **kwargs):
            if self.error:
                raise self.error
            self.told.append((args, kwargs))
    ok = Query()
    assert await answer_query(ok, "Drawing 7D...", show_alert=False) and ok.told == [(("Drawing 7D...",), {"show_alert": False})]
    caplog.set_level("INFO")
    assert not await answer_query(Query(BadRequest("Query is too old and response timeout expired or query id is invalid")), "x")
    assert "too old to answer" in caplog.text and "pd:7" in caplog.text
    with pytest.raises(BadRequest):
        await answer_query(Query(BadRequest("Message is not modified")))


async def test_the_air_sensors_own_readings_stay_offered_in_a_chat_until_two_runs_go_by_without_the_model_using_them(alert_state):
    seen = []
    uses = iter([True, True, False, False, False])      # what the model does with the series when it has them: used, used, then not

    class Capturing:
        async def run(self, messages, system, effort, *, turn=None, **k):
            seen.append((turn.air_extra, "AIRGRADIENT'S OWN TEMPERATURE AND HUMIDITY" in system))
            if turn.air_extra and next(uses):
                turn.air_extra_used = True              # a tool served them
            return "ok"
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)

    async def send_chat_action(*a, **k):
        pass
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    context = NS(bot=NS(send_chat_action=send_chat_action, id=99))
    bot = Bot(NS(tz=TZ), Capturing(), [], alert_state)
    for said in ("what is the weather like", "overlay ecowitt humidity with airgradient humidity", "do that again but for temperature",
                 "and the wind?", "and the rain?", "and the pressure?", "and the uv?"):
        await bot.respond(update, context, said)
    assert seen == [(False, False),                       # nothing about the air sensor yet
                    (True, True), (True, True),           # named, used; the follow-up (no AirGradient in it) still offered, used again
                    (True, True), (True, True),           # two runs the model did not use them in ...
                    (False, False), (False, False)]       # ... and they are no longer offered, nor mentioned in the prompt


async def test_either_side_naming_the_air_sensors_readings_keeps_them_offered(alert_state):
    seen = []
    replies_by_question = {"what is the weather like": "It is mild.", "tell me about airgradient humidity": "The AirGradient humidity is 61%.",
                           "and tomorrow?": "Sunny.", "and the day after?": "Cloudy.", "what about the wind?": "Calm."}

    class Capturing:
        async def run(self, messages, system, effort, *, turn=None, **k):
            seen.append(turn.air_extra)
            return replies_by_question[messages[-1]["content"]]
    sent = []

    async def reply_text(body, **kw):
        sent.append(body)

    async def send_chat_action(*a, **k):
        pass
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    context = NS(bot=NS(send_chat_action=send_chat_action, id=99))
    bot = Bot(NS(tz=TZ), Capturing(), [], alert_state)
    for said in replies_by_question:
        await bot.respond(update, context, said)
    # the person's words named it (offered) and the reply did too (still offered for the next two runs); then they stop
    assert seen == [False, True, True, True, False]


async def test_the_air_sensors_own_readings_are_offered_to_a_follow_up_when_the_last_messages_named_them_whatever_the_count(alert_state):
    seen = []

    class Capturing:
        async def run(self, messages, system, effort, *, turn=None, **k):
            seen.append(turn.air_extra)
            return "ok"
    sent = []

    async def reply_text(body, **kw):
        sent.append(body)

    async def send_chat_action(*a, **k):
        pass
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    context = NS(bot=NS(send_chat_action=send_chat_action, id=99))
    for earlier in (
            [{"role": "user", "content": "plot airgradient humidity"}, {"role": "assistant", "content": "Done."}],     # the person named it
            [{"role": "user", "content": "plot humidity"}, {"role": "assistant", "content": "Here is AirGradient humidity over Ecowitt."}],   # the reply did
            [{"role": "user", "content": "plot humidity"}, {"role": "assistant", "content": "Done."}]):                # neither did
        bot = Bot(NS(tz=TZ), Capturing(), [], alert_state)
        bot.chats[bot._key(msg)].history = list(earlier)                   # a chat whose count has run out: only the messages are left
        await bot.respond(update, context, "do that again but for temperature")
    assert seen == [True, True, False]


async def test_every_guidance_topic_stays_on_in_a_chat_while_either_side_names_it_or_its_tools_are_used_then_goes_after_two_quiet_runs(alert_state):
    seen = []
    script = {"how many days was uv over 9": ("ok", "weather_days"),       # the person names days, and its tool is used
              "and for solar": ("ok", None), "thanks": ("ok", None), "right": ("ok", None),
              "tell me something": ("The pressure is steady and the wind was strong.", None),   # the reply names wind
              "and the pressure": ("ok", None), "and humidity": ("ok", None), "fine": ("ok", None), "good": ("ok", None)}

    class Capturing:
        async def run(self, messages, system, effort, *, turn=None, **k):
            seen.append((messages[-1]["content"], sorted(turn.topics & {"days", "wind"})))
            reply, tool = script[messages[-1]["content"]]
            if tool:
                messages.append({"role": "assistant", "content": None, "tool_calls": [{"function": {"name": tool}}]})
            return reply
    sent = []

    async def reply_text(body, **kw):
        sent.append(body)

    async def send_chat_action(*a, **k):
        pass
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    context = NS(bot=NS(send_chat_action=send_chat_action, id=99))
    bot = Bot(NS(tz=TZ), Capturing(), [], alert_state)
    for said in script:
        await bot.respond(update, context, said)
    got = dict(seen)
    assert got["how many days was uv over 9"] == ["days"]
    assert got["and for solar"] == ["days"] and got["thanks"] == ["days"]            # no word of it, but its tool was just used
    assert got["right"] == []                                                          # two quiet runs: gone
    assert got["and the pressure"] == ["wind"]                                         # the reply to the question before named the wind
    assert got["and humidity"] == ["wind"] and got["fine"] == [] and got["good"] == []   # and that goes too, after two quiet runs


async def test_an_answer_that_comes_with_a_chart_is_not_streamed_into_the_draft(alert_state, monkeypatch):
    monkeypatch.setattr("lib.bot.DRAFT_MIN_GAP", 0.01)
    bot = DraftBot()
    assert await ask_private(alert_state, StreamingAgent(), bot, text="plot humidity for a week") == ["It is 12 degrees."]
    assert bot.drafts and all(t is None for _, t in bot.drafts)         # only "Thinking...": no text to be followed by a picture and a second message
    bot = DraftBot()
    await ask_private(alert_state, StreamingAgent(), bot, text="how hot is it")
    assert [t for _, t in bot.drafts][-1] == "It is 12 degrees."         # a plain answer still streams
