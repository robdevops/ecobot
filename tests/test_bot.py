"""Bot.respond: what the person is sent when things go wrong."""

import asyncio
from types import SimpleNamespace as NS

from lib import intent
from lib.bot import NO_MODEL, Bot
from lib.tools import Turn
from tests.fakes import draws, TZ
from tests.served import unreached


class RaisingAgent:
    async def run(self, *a, **k):
        raise RuntimeError("401 Bearer sk-or-SECRET-1234 quota exceeded for org acme")


class AnsweringAgent:
    async def run(self, messages, system, effort, *, turn=None, **k):
        self.system = system
        return "It is 12 degrees."


def telegram(send_chat_action, **more):
    """The Telegram bot a context carries, as the tests fake it: it shows "typing...", takes drafts without keeping them, and is bot 99."""
    async def send_message_draft(*a, **k):
        pass
    return NS(send_chat_action=send_chat_action, send_message_draft=send_message_draft, id=99, **more)


async def ask(serve, alert_state, agent, text="how hot is it"):
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)

    async def send_chat_action(*a, **k):
        pass
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    context = NS(bot=telegram(send_chat_action))
    await Bot(NS(tz=TZ), agent, alert_state, service=await serve()).respond(update, context, text)
    return replies


async def test_error_details_stay_in_the_log_not_the_chat(serve, alert_state):
    replies = await ask(serve, alert_state, RaisingAgent())
    assert len(replies) == 1 and "went wrong" in replies[0] and "try again" in replies[0].lower()
    assert "SECRET" not in replies[0] and "Bearer" not in replies[0] and "acme" not in replies[0]
    assert "RuntimeError" in replies[0]      # just the kind of problem


class NoModel:
    ready = False

    async def run(self, *a, **k):
        raise AssertionError("there is no model to ask")


async def test_without_a_model_a_typed_question_gets_one_line_saying_so(serve, alert_state):
    """Every typed question is the model's (step 6c): without one, a typed chart request or "report" is told so too."""
    for text in ("will it rain tomorrow, do you think?", "what can you do", "rain chart 7d", "report"):
        replies = await ask(serve, alert_state, NoModel(), text)
        assert replies == [NO_MODEL], text
    assert "\n" not in NO_MODEL and "no AI model" in NO_MODEL and "buttons and the alerts still work" in NO_MODEL


async def test_a_typed_chart_request_goes_to_the_model(serve, alert_state):
    """"humidity 7d" was drawn in code until step 6c: anything typed is the model's now."""
    agent = AnsweringAgent()
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)

    async def send_chat_action(*a, **k):
        pass
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    weather = await serve(sources=["Ecowitt"])
    await Bot(NS(tz=TZ), agent, alert_state, service=weather).respond(update, NS(bot=telegram(send_chat_action)), "humidity 7d")
    assert replies == ["It is 12 degrees."]


async def test_the_model_is_sent_the_persona_the_servers_instructions_and_the_time(serve, alert_state):
    from lib.prompt import PERSONA
    agent = AnsweringAgent()
    assert await ask(serve, alert_state, agent, "is it too cold to run?") == ["It is 12 degrees."]
    assert agent.system.startswith(PERSONA.rstrip()) and "\n\nThe devices: " in agent.system   # (lib/service/instructions.py)
    assert agent.system.rstrip().splitlines()[-1].startswith("Now: ") and "RIGHT NOW" not in agent.system


def test_a_reply_carries_the_message_it_replies_to_in_private_chats_too(alert_state):
    from types import SimpleNamespace as NS
    bot = Bot(NS(tz=TZ), None, alert_state, service=unreached())
    mine = NS(text="Absolute pressure\n• Low: 992.9 hPa at 2:20pm on Sat 5 Sep", caption=None, from_user=NS(id=99, full_name="Bot"))
    theirs = NS(text="lovely day", caption=None, from_user=NS(id=7, full_name="Ann"))
    private = NS(chat=NS(type="private"), from_user=NS(full_name="Rob"), reply_to_message=mine)
    assert bot._content(private, "did it rain on the lowest day", 99) == (
        '(replying to your earlier message: "Absolute pressure\n• Low: 992.9 hPa at 2:20pm on Sat 5 Sep") did it rain on the lowest day')
    assert bot._content(NS(**{**private.__dict__, "reply_to_message": None}), "hello", 99) == "hello"
    group = NS(chat=NS(type="group"), from_user=NS(full_name="Rob"), reply_to_message=theirs)
    assert bot._content(group, "same here", 99) == 'Rob (replying to Ann: "lovely day"): same here'
    assert bot._content(NS(**{**group.__dict__, "reply_to_message": None}), "hi", 99) == "Rob: hi"


async def test_a_hung_question_is_given_up_on_so_the_next_one_in_the_chat_is_answered(serve, alert_state, monkeypatch, caplog):
    import asyncio
    from lib import bot

    class Hanging:
        async def run(self, *a, **k):
            await asyncio.sleep(3600)

    monkeypatch.setattr(bot, "TURN_SECONDS", 0.3)
    with caplog.at_level("WARNING"):
        replies = await ask(serve, alert_state, Hanging())
    assert len(replies) == 1 and "took too long" in replies[0]
    assert "[Telegram 1]: gave up after 0s on: how hot is it" in caplog.text


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


async def ask_private(serve, alert_state, agent, bot, chat_type="private", text="how hot is it"):
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)

    async def reply_photo(photo, caption=None, **kw):
        replies.append(caption.split("\n\n", 1)[-1])
        return NS(message_id=5)
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text, reply_photo=reply_photo,
             chat=NS(type=chat_type, title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await Bot(NS(tz=TZ), agent, alert_state, service=await serve()).respond(update, NS(bot=bot), text)
    return replies


async def test_a_private_chat_shows_a_thinking_draft_that_streams_the_answer_then_sends_it(serve, alert_state, monkeypatch):
    monkeypatch.setattr("lib.bot.DRAFT_MIN_GAP", 0.01)
    bot = DraftBot()
    replies = await ask_private(serve, alert_state, StreamingAgent(), bot)
    assert replies == ["It is 12 degrees."]
    assert bot.drafts[0][1] is None                                    # empty: "Thinking..."
    assert [t for _, t in bot.drafts][-1] == "It is 12 degrees." and len({d for d, _ in bot.drafts}) == 1
    assert bot.typing >= 1                                             # "typing..." runs alongside the draft


async def test_an_idle_draft_is_refreshed_before_telegram_drops_it(serve, alert_state, monkeypatch):
    monkeypatch.setattr("lib.bot.DRAFT_REFRESH_SECONDS", 0.02)

    class Slow:
        async def run(self, *a, on_text=None, **k):
            await asyncio.sleep(0.15)
            return "ok"
    bot = DraftBot()
    await ask_private(serve, alert_state, Slow(), bot)
    assert len(bot.drafts) >= 4 and all(t is None for _, t in bot.drafts)


async def test_a_refused_draft_falls_back_to_typing_and_the_reply_still_arrives(serve, alert_state):
    bot = DraftBot(fail=True)
    assert await ask_private(serve, alert_state, StreamingAgent(), bot) == ["It is 12 degrees."]
    assert bot.drafts == [] and bot.typing >= 1


async def test_groups_never_get_drafts(serve, alert_state):
    bot = DraftBot()
    assert await ask_private(serve, alert_state, AnsweringAgent(), bot, chat_type="supergroup") == ["It is 12 degrees."]
    assert bot.drafts == []


async def test_a_generation_stopped_update_is_logged(alert_state, caplog):
    update = NS(effective_message=NS(api_kwargs={"message_generation_stopped": {"draft_id": 1}}, chat_id=1),
                effective_chat=NS(type="private", id=1, title=None), effective_user=NS(username="rob", full_name="Rob"))
    with caplog.at_level("INFO", logger="lib.bot"):
        await Bot(NS(tz=TZ), None, alert_state, service=unreached()).on_other(update, None)
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
    bot = Bot(NS(tz=TZ), SlowThenQuick(), alert_state, service=unreached())
    working = [{"role": "user", "content": "q"}]
    effort = intent.effort("what do you think about the weather today?")
    assert effort == "medium"
    assert await bot._ask_model(working, "sys", effort, Turn(), None) == "answered at low"
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
    bot = Bot(NS(tz=TZ), Hangs(), alert_state, service=unreached())
    for text, expected in (("hello there", ["none"]), ("what do you think about the weather today?", ["medium", "low"])):
        efforts.clear()
        with pytest.raises(TimeoutError):
            await bot._ask_model([], "sys", intent.effort(text), Turn(), None)
        assert efforts == expected


async def test_a_chart_button_is_its_fixed_call_drawn_by_the_service_and_the_model_is_not_asked(serve, alert_state, monkeypatch):
    class Agent:
        async def run(self, *a, **k):
            raise AssertionError("the model must not be asked for a button's chart")
    sent = []

    async def reply_photo(photo, caption=None, **kw):
        sent.append(caption)
    draws(monkeypatch, lambda data, tz, times=None: b"png")
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    service = chart_service()
    weather = await serve(tools=service, sources=["Ecowitt"])
    await Bot(NS(tz=TZ), Agent(), alert_state, service=weather).press(update, NS(bot=telegram(send_chat_action)), "Humidity chart 1d")
    assert service.calls == [("summary", {"readings": ["humidity"], "period": "1d", "chart": True})]   # the button's fixed call
    assert sent == ["Humidity"]    # the chart's title is its whole caption


async def test_a_chart_reply_logs_how_long_each_step_took(serve, alert_state, tmp_path, monkeypatch, caplog):
    """One line per chart reply: the calculation (the service's call, its drawing included: the bot draws nothing), the picture's size
    and sending it."""
    import re
    from lib.ecowitt import Ecowitt, api
    from lib.service.context import Context
    from tests.fakes import config, ecowitt_transport
    monkeypatch.setattr(api, "MIN_GAP_SECONDS", 0)
    eco = Ecowitt(config(tmp_path), transport=ecowitt_transport()[0])
    await eco.start()

    class Agent:
        async def run(self, *a, **k):
            raise AssertionError("no model for a button's chart")
    sent = []

    async def reply_photo(photo, caption=None, **kw):
        sent.append(photo)
        return NS(message_id=7)
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    weather = await serve(Context(eco=eco, tz=TZ))   # (ecobot-mcp over the fake station)
    with caplog.at_level("DEBUG"):
        await Bot(NS(tz=TZ), Agent(), alert_state, service=weather).press(update, NS(bot=telegram(send_chat_action)), "Temperature chart 1d")
    await eco.close()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Chart ms:")]
    assert len(sent) == 1 and len(lines) == 1, lines
    kb = round(len(sent[0]) / 1024)
    assert re.fullmatch(rf"Chart ms: calc \d+, {kb} KB, send \d+", lines[0])


async def _ask_with(serve, alert_state, answer, text, monkeypatch=None):
    """Press the button whose sentence is `text` on a bot whose service tools all give `answer`; returns (replies, whether the model was
    asked)."""
    from tests.fakes import StandInService
    asked = []

    class Agent:
        async def run(self, messages, *a, **k):
            asked.append(True)
            return "from the model"
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    service = StandInService(**{name: answer for name in ("current", "summary", "days", "about", "pollen", "forecast")})
    weather = await serve(tools=service, sources=["Ecowitt", "AirGradient"])
    await Bot(NS(tz=TZ), Agent(), alert_state, service=weather).press(update, NS(bot=telegram(send_chat_action)), text)
    return replies, bool(asked)


async def test_a_button_chart_the_service_cant_draw_says_why_and_the_model_is_not_asked(serve, alert_state):
    """(Until step 6c a chart shortcut the service couldn't draw went to the model: a button has no model to fall back on.)"""
    replies, asked = await _ask_with(serve, alert_state, {"error": "station not answering"}, "Humidity chart 1d")
    assert not asked and replies == ["station not answering"]
    replies, asked = await _ask_with(serve, alert_state, {"text": "no chart"}, "Humidity chart 1d")
    assert not asked and replies == ["Sorry, I couldn't draw that chart. Please try again in a moment."]


async def test_typing_starts_before_the_model_is_asked_and_runs_alongside_the_draft(serve, alert_state):
    seen = {}

    class Agent:
        async def run(self, *a, **k):
            seen["typing_when_asked"], seen["drafts_when_asked"] = bot.typing, len(bot.drafts)
            await asyncio.sleep(0.05)
            return "ok"
    bot = DraftBot()
    assert await ask_private(serve, alert_state, Agent(), bot) == ["ok"]
    assert seen["typing_when_asked"] >= 1 and bot.drafts


async def _message_aged(serve, alert_state, seconds):
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
    bot = Bot(NS(tz=TZ), Agent(), alert_state, service=await serve())
    await bot.on_message(update, NS(bot=telegram(send_chat_action, username="b")))
    return replies


async def test_a_message_sent_while_the_bot_was_starting_is_answered(serve, alert_state, caplog):
    assert await _message_aged(serve, alert_state, 120) == ["ok"]


async def test_a_message_that_waited_hours_is_ignored_and_logged(serve, alert_state, caplog):
    with caplog.at_level("INFO", logger="lib.bot"):
        assert await _message_aged(serve, alert_state, 2 * 3600) == []
    assert "[Telegram 1]: @rob: ignored a message 120 minutes old: how hot is it" in caplog.text


async def test_a_request_and_its_reply_are_logged_under_the_chat_they_came_from(serve, alert_state, caplog):
    with caplog.at_level("INFO", logger="lib.bot"):
        assert await _message_aged(serve, alert_state, 5) == ["ok"]
    lines = [r.getMessage() for r in caplog.records if r.name == "lib.bot"]
    assert "[Telegram 1]: @rob: how hot is it" in lines
    assert any(line.startswith("[Telegram 1]: Replied in ") and line.endswith(", tools 0, charts 0, 2 chars") for line in lines)


async def test_a_heavy_question_gets_a_heads_up_in_the_chat_before_the_answer_and_the_answer_still_comes(serve, alert_state):
    class Agent:
        async def run(self, messages, system, effort, on_heavy=None, **k):
            await on_heavy(24000)
            return "The answer."
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    weather = await serve(sources=["Ecowitt"])
    await Bot(NS(tz=TZ), Agent(), alert_state, service=weather).respond(update, NS(bot=telegram(send_chat_action)), "is it too cold to run")
    assert replies == ["\u26a0\ufe0f That's pulling in about 24k tokens of data, so it will take a little longer. Working on it...", "The answer."]


async def test_the_model_chooses_its_own_tools_whatever_the_message(serve, alert_state):
    seen = []

    class Agent:
        async def run(self, messages, system, effort, **k):
            seen.append(k["tool_choice"])
            return "ok"
    weather = await serve(sources=["Ecowitt"])
    for text in ("will it rain tomorrow?", "thanks!"):
        replies = []

        async def reply_text(body, **kw):
            replies.append(body)
        msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
                 chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
        update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

        async def send_chat_action(*a, **k):
            pass
        await Bot(NS(tz=TZ), Agent(), alert_state, service=weather).respond(update, NS(bot=telegram(send_chat_action)), text)
    assert seen == ["auto", "auto"]


async def test_the_weather_and_temperature_buttons_charts_are_sent_with_just_their_title(serve, alert_state, monkeypatch):
    class Agent:
        async def run(self, *a, **k):
            raise AssertionError("no model for a chart")
    sent = []

    async def reply_photo(photo, caption=None, **kw):
        sent.append(caption)
    draws(monkeypatch, lambda data, tz, times=None: b"png")
    weather = await serve(tools=chart_service(title="t"), sources=["Ecowitt"])
    for text in ("Weather chart 1d", "Temperature chart 1d"):
        sent.clear()
        msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo,
                 chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
        update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

        async def send_chat_action(*a, **k):
            pass
        await Bot(NS(tz=TZ), Agent(), alert_state, service=weather).press(update, NS(bot=telegram(send_chat_action)), text)
        assert sent == ["t"], text


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


class ChartingAgent(StreamingAgent):
    """The model's first step drew a chart (a result with `charts` kept on the Turn, and its picture as the service drew it); then it
    streams its answer."""

    async def run(self, messages, system, effort, *, turn=None, **k):
        from lib.weather import ChartImage
        from tests.fakes import chart_data
        chart = chart_data("Humidity")
        turn.results.append(("summary", {"readings": ["humidity"]}, {"caption": "c", "charts": [chart]}))
        turn.pictures[len(turn.results) - 1] = [ChartImage(b"png", chart, False)]
        return await super().run(messages, system, effort, turn=turn, **k)


async def test_an_answer_that_comes_with_a_chart_is_not_streamed_into_the_draft(serve, alert_state, monkeypatch):
    monkeypatch.setattr("lib.bot.DRAFT_MIN_GAP", 0.01)
    bot = DraftBot()
    assert await ask_private(serve, alert_state, ChartingAgent(), bot, text="plot humidity for a week") == ["It is 12 degrees."]
    assert bot.drafts and all(t is None for _, t in bot.drafts)         # only "Thinking...": no text to be followed by a picture and a second message
    bot = DraftBot()
    await ask_private(serve, alert_state, StreamingAgent(), bot, text="how hot is it")
    assert [t for _, t in bot.drafts][-1] == "It is 12 degrees."         # a plain answer still streams


def chart_service(caption="Fri 25 Sep – Thu 1 Oct 2026\nHumidity: low 50 %, Fri 25 Sep at 5am · high 60 %, Sat 26 Sep at 3pm",
                  title="Humidity"):
    """A stand-in service whose summary draws a chart titled `title` and captions it."""
    from tests.fakes import StandInService, chart_data
    return StandInService(summary=lambda args, meta: {"caption": caption, "charts": [chart_data(title)]})


async def test_a_chart_kept_by_its_question_from_before_the_swap_is_out_of_date(serve, alert_state):
    """Before step 3 a chart the model drew through a call the service had no equivalent of was remembered by its question; its buttons
    are out of date now."""
    from tests.fakes import StandInService
    bot = Bot(NS(tz=TZ), None, alert_state, service=await serve(tools=StandInService()))
    bot.charted.remember(1, 7, "how warm was it")
    answers = []

    async def answer(text=None, **k):
        answers.append(text)
    query = NS(data="pd:7", message=NS(chat_id=1, message_id=7), answer=answer)
    await bot.on_period_button(NS(callback_query=query), None)
    assert answers == ["That chart is out of date, please ask again"]


async def test_the_models_charts_are_drawn_with_their_buttons_and_the_model_is_sent_their_titles_alone(serve, alert_state, monkeypatch):
    """The model's call drew a chart: the model sees its title and period (no points), never the picture; the bot sends the picture
    the service drew (asked for as an image), with the period buttons its drawn_by lists, and keeps the call behind them."""
    import json
    from lib import model_tools
    from lib.llm import Agent
    from tests.fakes import chart_data
    drawn_by = {"tool": "summary", "arguments": {"readings": ["humidity"], "period": "7d", "chart": True},
                "periods": [{"period": "1d", "label": "1D"}, {"period": "7d", "label": "7D"}]}
    chart = {**chart_data("Humidity"), "drawn_by": drawn_by}

    class Summary:
        name, description, parameters = "summary", "Summary.", {"type": "object", "properties": {}}

        async def call(self, args, meta):
            assert meta["chart_points"] == "drawn"
            return json.dumps({"caption": "c", "charts": [chart], "chart_note": "note"})
    drawn = []
    draws(monkeypatch, lambda c, tz: drawn.append(c) or b"png")   # (ecobot-mcp's drawing, in memory)
    weather = await serve(tools=[Summary()])
    seen, sent = [], []

    class Model:
        """A model that calls summary, then answers."""
        def __init__(self):
            self.chat = self

        async def create(self, **kw):
            seen.append(kw["messages"][-1])
            if len(seen) == 1:
                call = NS(id="1", function=NS(name="summary", arguments="{}"))
                return NS(choices=[NS(message=NS(content="", tool_calls=[call]))], usage=None)
            return NS(choices=[NS(message=NS(content="Humid all week.", tool_calls=None))], usage=None)

    client = NS(chat=NS(completions=Model()))

    async def reply_photo(photo, caption=None, reply_markup=None, **kw):
        sent.append((caption, reply_markup))
        return NS(message_id=9)
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo,
             chat=NS(type="group", title="g"), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    bot = Bot(NS(tz=TZ), Agent(client, "m", model_tools.tools(weather)), alert_state, service=weather)
    await bot.respond(update, NS(bot=telegram(send_chat_action)), "how humid was it this week?")
    told = json.loads(seen[-1]["content"])
    assert told["charts"] == [{"title": "Humidity", "period": chart["period"]}] and told["chart_note"] == model_tools.BOT_CHART_NOTE
    assert drawn == [chart] and "png" not in seen[-1]["content"]
    (caption, markup), = sent
    assert caption.startswith("Humidity") and "Humid all week." in caption
    assert [b.callback_data for b in markup.inline_keyboard[0]] == ["pd:1d", "pd:7d"]
    assert bot.charted.get(1, 9)["drawn_by"] == drawn_by


BOT_MODULES = ["ecobot", "lib.bot", "lib.weather", "lib.upstream", "lib.model_tools", "lib.llm", "lib.prompt", "lib.alerts.notify",
               "lib.alerts.menu", "lib.alerts.irrigation", "lib.irrigation", "lib.irrigation.menu", "lib.periods", "lib.templates",
               "lib.admins", "lib.buttons", "lib.config", "lib.intent", "lib.version", "lib.theme"]
NOT_THE_BOTS = ("PIL", "lib.charts", "lib.draw", "lib.gauges",                                       # drawing (step 6a)
                "lib.ecowitt", "lib.airgradient", "lib.forecast", "lib.pollen", "lib.storage")     # the sources and their file (6b)


def test_the_bot_imports_no_pillow_no_drawing_module_and_no_source_module(tmp_path):
    """The bot draws nothing and runs no source (docs/ecobot-mcp.md steps 6a and 6b): importing its modules, and building it as
    ecobot-mcp's client with its alert delivery and irrigation check, and reading an answer for the air link (reads_air), imports neither Pillow nor lib/charts.py, lib/draw.py or
    lib/gauges.py, nor any module of the sources (lib/ecowitt, lib/airgradient, lib/forecast, lib/pollen) or of the readings file
    (lib/storage); every picture it sends, and every reading, is ecobot-mcp's."""
    import os
    import subprocess
    import sys
    from lib.config import ROOT
    code = (f"import sys, importlib; [importlib.import_module(m) for m in {BOT_MODULES!r}]; "
            "from types import SimpleNamespace as NS; from zoneinfo import ZoneInfo; from lib.alerts.notify import AlertDelivery, "
            "AlertState, Notifier; from lib.alerts.irrigation import IrrigationMonitor; from lib.bot import Bot; from lib.llm import Agent; "
            "from lib import model_tools; from lib.weather import RemoteWeather, ServiceRain; "
            "w = RemoteWeather('http://127.0.0.1:9/mcp', 't'); "
            f"s = AlertState({str(tmp_path / 's.json')!r}); "
            "b = Bot(NS(tz=ZoneInfo('UTC')), Agent(None, 'm', model_tools.tools(w)), s, service=w); "
            "AlertDelivery(w.alerts, Notifier(NS(), s, b.alert_charts), s, ZoneInfo('UTC'), weather=w); "
            "IrrigationMonitor(None, s, None, ZoneInfo('UTC'), 18, ServiceRain(w), w.forecast); "
            "from lib.bot import reads_air; [reads_air(*c) for c in (('current', {'readings': ['pm2.5']}, {'sensors': {'air': {}}}), "
            "('days', {'sort_by': 'pm2_5_max'}, {}), ('summary', {}, {'series': {}}))]; "
            f"print(sorted(m for m in sys.modules if m.split('.')[0] == 'PIL' or m.startswith({NOT_THE_BOTS!r})))")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                          env={**os.environ, "DATA_DIR": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "[]"
