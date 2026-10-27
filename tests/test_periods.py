import json
from datetime import datetime
from types import SimpleNamespace as NS

from lib import bot as botmod
from lib import intent, periods
from lib.bot import Bot
from lib.specs import Chart, Line, Panel
from tests.fakes import TZ, StandInTools

NOW = datetime(2026, 10, 2, 12, 0)


def labels(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def test_the_row_is_always_week_month_quarter_and_year():
    assert labels(periods.keyboard()) == [[("1D", "pd:1"), ("7D", "pd:7"), ("1M", "pd:30"), ("3M", "pd:90"), ("1Y", "pd:365")]]
    assert [t for t, _ in labels(periods.keyboard(30))[0]] == ["1D", "7D", "\u25cf 1M", "3M", "1Y"]          # the chart's own period is marked
    assert [t for t, _ in labels(periods.keyboard(61))[0]] == ["1D", "7D", "1M", "3M", "1Y"]
    assert [t for t, _ in labels(periods.keyboard(365))[0]][-1] == "\u25cf 1Y"
    assert periods.days_in("pd:1") == 1 and periods.days_in("pd:90") == 90 and periods.days_in("pd:365") == 365 and periods.days_in("pd:30000") is None and periods.days_in("al:on:x") is None


def test_a_question_keeps_its_words_and_swaps_its_period():
    for ask, expected in (("Temperature chart 30d", "Temperature chart 90d"), ("chart air quality last month", "chart air quality 90d"),
                          ("rain chart for the last week", "rain chart 90d"), ("weather chart", "weather chart 90d"),
                          ("plot humidity and rain 3m", "plot humidity and rain 90d")):
        assert intent.with_period(ask, 90, NOW) == expected
    assert intent.period_days("Temperature chart 30d", NOW) == 30 and intent.period_days("weather chart", NOW) == 1
    assert intent.period_days("chart 24h", NOW) == 1 and intent.period_days("chart 365d", NOW) == 365


def test_only_the_newest_questions_are_remembered():
    charted = periods.Charted()
    for i in range(periods.REMEMBERED + 5):
        charted.remember(1, i, f"q{i}")
    assert len(charted.store) == periods.REMEMBERED and charted.get(1, 0) is None and charted.get(1, periods.REMEMBERED + 4)


def test_the_questions_behind_charts_survive_a_restart(tmp_path):
    from lib.alerts import AlertState
    state = AlertState(tmp_path / "s.json")
    Bot(NS(tz=TZ), None, [], state).charted.remember(1, 101, "Temperature chart 7d")
    again = Bot(NS(tz=TZ), None, [], AlertState(tmp_path / "s.json"))
    assert again.charted.get(1, 101) == "Temperature chart 7d"


class Tools(StandInTools):
    def __init__(self):
        self.calls = []

    async def call(self, name, raw, turn=None):
        self.calls.append((name, json.loads(raw)))
        turn.charts.append(Chart("Temperature", "", [Panel("Temperature", "°C", [Line("Outdoor", [1, 2], [1.0, 2.0])])]))
        return json.dumps({"period": "Fri 25 Sep 2026 - Thu 01 Oct 2026", "series": {"outdoor.temperature": {"unit": "℃", "low": "1", "high": "2"}}})


def make_bot(alert_state, monkeypatch):
    class Agent:
        tools = Tools()

        async def run(self, *a, **k):
            raise AssertionError("no model for a plain chart")
    monkeypatch.setattr(botmod, "render_chart", lambda spec, tz, times=None: b"png")
    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt")]
    return Bot(NS(tz=TZ), Agent(), sources, alert_state)


async def test_closing_the_bot_cancels_its_uploads_still_running(alert_state, monkeypatch):
    import asyncio
    bot = make_bot(alert_state, monkeypatch)
    upload = asyncio.create_task(asyncio.Event().wait())
    bot.background.add(upload)
    await bot.close()
    assert upload.cancelled()


def chat_message(sent, edits=None):
    async def reply_photo(photo, caption=None, **kw):
        sent.append((caption, kw.get("reply_markup")))
        return NS(message_id=100 + len(sent))

    async def edit_media(media, reply_markup=None):
        edits.append((media.caption, reply_markup))
    return NS(chat_id=1, message_id=50, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo,
              edit_media=edit_media, chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)


async def send_chat_action(*a, **k):
    pass


CONTEXT = NS(bot=NS(send_chat_action=send_chat_action, id=99))


async def test_a_chart_is_sent_with_the_other_periods_under_it_and_its_question_is_remembered(alert_state, monkeypatch):
    bot, sent = make_bot(alert_state, monkeypatch), []
    msg = chat_message(sent)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await bot.respond(update, CONTEXT, "Temperature chart 30d")
    caption, markup = sent[0]
    assert caption == "Temperature" and labels(markup) == [[("1D", "pd:1"), ("7D", "pd:7"), ("\u25cf 1M", "pd:30"), ("3M", "pd:90"), ("1Y", "pd:365")]]
    assert bot.charted.get(1, 101) == "Temperature chart 30d"


async def test_pressing_a_period_redraws_the_chart_in_place_with_that_period_left_out(alert_state, monkeypatch):
    bot, edits = make_bot(alert_state, monkeypatch), []
    msg = chat_message([], edits)
    bot.charted.remember(1, 50, "Temperature chart 30d")
    answers = []

    async def answer(text=None):
        answers.append(text)
    query = NS(data="pd:90", message=msg, answer=answer)
    update = NS(callback_query=query, effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await bot.on_period_button(update, CONTEXT)
    name, args = bot.agent.tools.calls[0]
    assert name == "weather_history" and args["start_date"].startswith("2026-") and answers == ["Drawing 3M..."]
    caption, markup = edits[0]
    assert caption == "Temperature" and [t for t, _ in labels(markup)[0]] == ["1D", "7D", "1M", "\u25cf 3M", "1Y"]
    assert bot.charted.get(1, 50) == "Temperature chart 90d"


async def test_a_button_on_a_chart_from_before_a_restart_says_so(alert_state, monkeypatch):
    bot = make_bot(alert_state, monkeypatch)
    answers = []

    async def answer(text=None):
        answers.append(text)
    msg = chat_message([])
    await bot.on_period_button(NS(callback_query=NS(data="pd:7", message=msg, answer=answer)), CONTEXT)
    assert answers == ["That chart is out of date, please ask again"] and not bot.agent.tools.calls


async def test_a_reply_with_period_buttons_leaves_the_persistent_keyboard_for_the_next_reply():
    from lib.templates import keyboard
    sent = []

    class Replies:
        async def reply_photo(self, photo, **kw):
            sent.append(kw["reply_markup"])
            return NS(message_id=7)
    got = await botmod.deliver(Replies(), "", [b"p"], markup=keyboard(), titles=["T"], period_row=periods.keyboard())
    assert got.carried is False and labels(sent[0])[0][0][0] == "1D" and got.photo.message_id == 7


def test_a_chart_drawn_lately_comes_back_from_the_cache_until_it_is_too_old_or_pushed_out():
    clock = [0.0]
    cache = periods.ImageCache(lambda: clock[0])
    key = cache.key("Temperature chart 30d", 30, NOW)
    assert key == cache.key("temperature chart for the last month", 30, NOW) and cache.get(key) is None
    cache.put(key, b"png", "Temperature")
    assert cache.get(key) == (b"png", "Temperature")
    clock[0] = periods.ImageCache.CHART_TTL + 1
    assert cache.get(key) is None
    for i in range(periods.ImageCache.CHART_IMAGES + 3):
        cache.put(f"q{i}", b"x", "t")
    assert len(cache.items) == periods.ImageCache.CHART_IMAGES and "q0" not in cache.items


async def test_toggling_back_to_a_period_just_drawn_uses_the_cached_image_and_asks_nothing_again(alert_state, monkeypatch):
    bot, sent, edits = make_bot(alert_state, monkeypatch), [], []
    msg = chat_message(sent, edits)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await bot.respond(update, CONTEXT, "Temperature chart 7d")
    answers = []

    async def answer(text=None):
        answers.append(text)
    msg.message_id = 101
    for days in (30, 30, 7, 30, 7):
        await bot.on_period_button(NS(callback_query=NS(data=f"pd:{days}", message=msg, answer=answer), effective_message=msg,
                                      effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob")), CONTEXT)
    assert len(bot.agent.tools.calls) == 2                      # the first send and the first 30d: the rest came from the cache (a press on the shown 30d does nothing)
    assert [e[0] for e in edits] == ["Temperature"] * 4   # the repeated 30d press edited nothing and bot.charted[(1, 101)] == "Temperature chart 7d"


async def test_a_chart_once_sent_goes_again_as_telegrams_copy_not_a_new_upload(alert_state, monkeypatch):
    bot, uploads, edits = make_bot(alert_state, monkeypatch), [], []

    async def reply_photo(photo, caption=None, **kw):
        uploads.append(photo)
        return NS(message_id=101, photo=(NS(file_id="small"), NS(file_id="7d")))

    async def edit_media(media, reply_markup=None):
        edits.append(media.media if isinstance(media.media, str) else "upload")
        return NS(photo=(NS(file_id="small"), NS(file_id=f"edit {len(edits)}")))
    msg = NS(chat_id=1, message_id=101, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo, edit_media=edit_media,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await bot.respond(update, CONTEXT, "Temperature chart 7d")

    async def answer(text=None):
        pass
    for days in (30, 7, 30):
        await bot.on_period_button(NS(callback_query=NS(data=f"pd:{days}", message=msg, answer=answer), effective_message=msg,
                                      effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob")), CONTEXT)
    assert uploads == [b"png"] and edits == ["upload", "7d", "edit 1"]   # 30d drawn and uploaded once; then both by Telegram's copy


async def test_redrawing_a_chart_from_a_button_shows_no_typing_or_thinking(alert_state, monkeypatch):
    bot = make_bot(alert_state, monkeypatch)
    actions = []

    async def chat_action(*a, **k):
        actions.append(a)
    drafts = []

    async def draft(*a, **k):
        drafts.append(a)
    context = NS(bot=NS(send_chat_action=chat_action, send_message_draft=draft, id=99))
    msg = chat_message([], [])
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await bot.respond(update, context, "Temperature chart 30d", redraw=msg)
    assert actions == [] and drafts == []
    await bot.respond(update, context, "Temperature chart 7d")          # a question of its own still does
    assert actions


def pressed(bot, sent):
    """A private chat's message whose photos land in `sent` as (photo, caption)."""
    async def reply_photo(photo, caption=None, **kw):
        sent.append((photo, caption))
        return NS(message_id=100 + len(sent), photo=(NS(file_id="small"), NS(file_id=f"copy {len(sent)}")))
    msg = NS(chat_id=1, message_id=50, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    return NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))


async def test_the_button_charts_are_drawn_ahead_and_a_press_is_sent_one_at_once(alert_state, monkeypatch, caplog):
    bot, sent = make_bot(alert_state, monkeypatch), []
    with caplog.at_level("INFO"):
        await bot.predraw()
    calls = len(bot.agent.tools.calls)
    assert calls >= 1 and all(name == "weather_history" for name, _ in bot.agent.tools.calls)   # the station's buttons (no air sensor here)
    assert [r.getMessage() for r in caplog.records if r.getMessage().startswith("Tool ")] == []      # quiet: nobody asked
    assert any(r.getMessage().startswith("Button charts drawn ahead every 300s: ") for r in caplog.records)
    update = pressed(bot, sent)
    await bot.respond(update, CONTEXT, "Temperature chart 1d")
    assert len(bot.agent.tools.calls) == calls and sent == [(b"png", "Temperature")]                 # no new question: the one drawn ahead
    history = bot.chats[(1, None)].history
    assert history[-1]["role"] == "assistant" and history[-1]["content"]                             # its text is kept, as a drawn one's is
    await bot.respond(update, CONTEXT, "Temperature chart 1d")
    assert len(bot.agent.tools.calls) == calls and sent[-1] == ("copy 1", "Temperature")             # and once sent, by Telegram's copy
    caplog.clear()
    with caplog.at_level("INFO"):
        await bot.predraw()
    assert not any("drawn ahead" in r.getMessage() for r in caplog.records)                          # only the first round is logged


async def test_a_chart_drawn_ahead_is_drawn_again_once_it_is_stale_and_never_answers_other_words(alert_state, monkeypatch):
    clock = [0.0]
    bot, sent = make_bot(alert_state, monkeypatch), []
    bot.images = periods.ImageCache(lambda: clock[0])
    await bot.predraw()
    calls = len(bot.agent.tools.calls)
    await bot.respond(pressed(bot, sent), CONTEXT, "temperature chart today")     # kept under the 1d key, but since midnight: drawn
    assert len(bot.agent.tools.calls) == calls + 1
    await bot.predraw()
    calls = len(bot.agent.tools.calls)
    clock[0] = periods.ImageCache.FRESH + 1
    await bot.respond(pressed(bot, sent), CONTEXT, "Temperature chart 1d")        # too old to send as it is
    assert len(bot.agent.tools.calls) == calls + 1


async def test_charts_are_not_drawn_ahead_while_a_question_is_being_answered(alert_state, monkeypatch):
    import asyncio
    bot = make_bot(alert_state, monkeypatch)
    bot.answering = 1
    bot.idle.clear()
    drawing = asyncio.create_task(bot.predraw())
    await asyncio.sleep(0.05)
    assert bot.agent.tools.calls == []
    bot.answering = 0
    bot.idle.set()
    await drawing
    assert bot.agent.tools.calls
    await bot.respond(pressed(bot, []), CONTEXT, "Temperature chart 7d")
    assert bot.answering == 0 and bot.idle.is_set()                    # counted while it ran, and let go after



def preloading_bot(alert_state, monkeypatch, refuse=None, clock=None):
    """A bot with TELEGRAM_PRELOAD_CHAT set, and the (chat, photo, silent) of every upload to it. The periods are uploaded side by side,
    in no fixed order, so each period's picture is its question's words and its file id "file " and those words; context.refused.error
    is what the uploads raise (none: they go through)."""
    bot, uploads, refused = make_bot(alert_state, monkeypatch), [], NS(error=refuse)
    bot.cfg = NS(tz=TZ, telegram_preload_chat=-1001)
    if clock:
        bot.images = periods.ImageCache(lambda: clock[0])
    draw = bot._draw_ahead

    async def draw_ahead(text, now):
        key, _, title, caption = await draw(text, now)
        return key, text.encode(), title, caption
    bot._draw_ahead = draw_ahead

    async def send_photo(chat, photo, caption=None, disable_notification=False):
        if refused.error:
            raise refused.error
        uploads.append((chat, photo, disable_notification))
        return NS(photo=(NS(file_id="small"), NS(file_id=f"file {photo.decode()}")))
    return bot, uploads, NS(bot=NS(send_chat_action=send_chat_action, send_photo=send_photo, id=99), refused=refused)


def uploads_of(*days):
    """What the uploads to the preload chat are, in any order, when they are the Temperature charts of these periods."""
    return sorted((-1001, f"Temperature chart {d}d".encode(), True) for d in days)


async def settle(bot):
    import asyncio
    await asyncio.gather(*bot.background)


async def test_the_other_periods_of_a_chart_are_uploaded_ahead_and_a_press_edits_by_file_id(alert_state, monkeypatch):
    bot, uploads, context = preloading_bot(alert_state, monkeypatch)
    sent, edits = [], []

    async def edit_media(media, reply_markup=None):
        edits.append(media.media)
        return NS(photo=())
    msg = chat_message(sent, edits)
    msg.edit_media, msg.message_id = edit_media, 101
    await bot.respond(NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob")),
                      context, "Temperature chart 7d")
    await settle(bot)
    assert sorted(uploads) == uploads_of(1, 30, 90, 365)                                      # 1D, 1M, 3M, 1Y: not the 7D just sent
    calls = len(bot.agent.tools.calls)

    async def answer(text=None):
        pass
    await bot.on_period_button(NS(callback_query=NS(data="pd:90", message=msg, answer=answer), effective_message=msg,
                                  effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob")), context)
    assert len(bot.agent.tools.calls) == calls and edits[0] == "file Temperature chart 90d"   # Telegram's copy of the 3M, no new question or upload


async def test_nothing_is_uploaded_ahead_without_the_setting_and_a_refused_upload_stops_it_with_one_warning(alert_state, monkeypatch, caplog):
    bot, uploads, context = preloading_bot(alert_state, monkeypatch)
    bot.cfg = NS(tz=TZ)
    update = pressed(bot, [])
    await bot.respond(update, context, "Temperature chart 7d")
    await settle(bot)
    assert uploads == [] and not bot.background
    from telegram.error import BadRequest
    bot, uploads, context = preloading_bot(alert_state, monkeypatch, refuse=BadRequest("Chat not found"))
    with caplog.at_level("WARNING"):
        await bot.respond(pressed(bot, []), context, "Temperature chart 7d")
        await settle(bot)
        await bot.respond(pressed(bot, []), context, "Temperature chart 30d")
        await settle(bot)
    assert [r for r in caplog.records if "TELEGRAM_PRELOAD_CHAT" in r.getMessage()].__len__() == 1 and uploads == [] and bot.preload_off


async def test_a_flood_limit_or_a_timeout_skips_those_uploads_and_the_next_chart_uploads_again(alert_state, monkeypatch, caplog):
    import warnings
    from telegram.error import RetryAfter, TimedOut
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # python-telegram-bot 22 warns whenever it builds a RetryAfter
        flood = RetryAfter(30)
    bot, uploads, context = preloading_bot(alert_state, monkeypatch)
    for error, asked in ((flood, "Temperature chart 7d"), (TimedOut(), "Temperature chart 30d")):
        context.refused.error = error
        with caplog.at_level("WARNING"):
            await bot.respond(pressed(bot, []), context, asked)
            await settle(bot)
        assert uploads == [] and not bot.preload_off
    assert len([r for r in caplog.records if "TELEGRAM_PRELOAD_CHAT" in r.getMessage()]) == 2        # one a chart, though several uploads were skipped
    context.refused.error = None
    await bot.respond(pressed(bot, []), context, "Temperature chart 90d")
    await settle(bot)
    assert sorted(uploads) == uploads_of(1, 365)                        # the 7D and the 1M were sent as the questions' own charts


async def test_a_period_that_cannot_be_drawn_is_left_out_and_the_others_are_uploaded(alert_state, monkeypatch):
    bot, uploads, context = preloading_bot(alert_state, monkeypatch)
    draw = bot._draw_ahead

    async def draw_ahead(text, now):
        if text.endswith("90d"):
            raise ValueError("no readings")
        return await draw(text, now)
    bot._draw_ahead = draw_ahead
    await bot.respond(pressed(bot, []), context, "Temperature chart 7d")
    await settle(bot)
    assert sorted(uploads) == uploads_of(1, 30, 365)
    assert bot.images.has_copy(bot.images.key("Temperature chart 30d", 30, NOW)) and not bot.images.has_copy(bot.images.key("Temperature chart 90d", 90, NOW))


async def test_charts_are_uploaded_ahead_at_once_even_while_a_question_is_being_answered(alert_state, monkeypatch):
    import asyncio
    bot, uploads, context = preloading_bot(alert_state, monkeypatch)
    bot.answering = 1
    bot.idle.clear()
    bot.preload_soon(context.bot, "Temperature chart 7d")
    await asyncio.wait_for(settle(bot), 5)                  # it would wait for ever if it waited for the question
    assert sorted(uploads) == uploads_of(1, 30, 90, 365)


async def test_each_period_is_uploaded_as_soon_as_it_is_drawn_not_when_the_slowest_is(alert_state, monkeypatch):
    import asyncio
    bot, uploads, context = preloading_bot(alert_state, monkeypatch)
    slow, three, draw, send = asyncio.Event(), asyncio.Event(), bot._draw_ahead, context.bot.send_photo

    async def draw_ahead(text, now):
        if text.endswith("chart 1d"):
            await slow.wait()
        return await draw(text, now)

    async def send_photo(chat, photo, **kw):
        message = await send(chat, photo, **kw)
        if len(uploads) == 3:
            three.set()
        return message
    bot._draw_ahead, context.bot.send_photo = draw_ahead, send_photo
    bot.preload_soon(context.bot, "Temperature chart 7d")
    await asyncio.wait_for(three.wait(), 5)
    assert sorted(uploads) == uploads_of(30, 90, 365)                    # the 1D, first in line, is still being drawn
    slow.set()
    await asyncio.wait_for(settle(bot), 5)
    assert sorted(uploads) == uploads_of(1, 30, 90, 365)


async def test_a_chart_uploaded_ahead_is_used_for_30_minutes_and_then_drawn_afresh(alert_state, monkeypatch):
    clock = [0.0]
    bot, uploads, context = preloading_bot(alert_state, monkeypatch, clock=clock)
    await bot.respond(pressed(bot, []), context, "Temperature chart 7d")
    await settle(bot)
    key = bot.images.key("Temperature chart 30d", 30, NOW)
    clock[0] = 29 * 60
    assert bot.images.has_copy(key) and isinstance(bot.images.get(key)[0], str)
    clock[0] = 31 * 60
    assert not bot.images.has_copy(key) and bot.images.get(key) is None


async def test_a_redrawn_chart_carries_a_short_answer_in_its_caption_and_a_long_one_follows_as_a_message(alert_state, monkeypatch):
    bot, edits, replies = make_bot(alert_state, monkeypatch), [], []
    msg = chat_message([], edits)

    async def reply_text(text, **kw):
        replies.append(text)
    msg.reply_text = reply_text
    assert await bot._redraw(msg, "Last 7 days: 72% avg.", b"png", "Humidity", periods.keyboard(7), "q")
    assert edits[0][0] == "Humidity\n\nLast 7 days: 72% avg." and replies == []                  # one message, as when the chart was first sent
    assert await bot._redraw(msg, "x" * botmod.CAPTION_LIMIT, b"png", "Humidity", periods.keyboard(7), "q")
    assert edits[1][0] == "Humidity" and replies == ["x" * botmod.CAPTION_LIMIT]                  # too long for a caption: its own message


async def test_pressing_a_period_under_a_chart_the_model_drew_keeps_its_answer_in_the_caption(alert_state, monkeypatch):
    bot, edits, replies = make_bot(alert_state, monkeypatch), [], []

    class Agent:
        ready = True

        async def run(self, working, system, effort, turn=None, **kw):
            turn.charts.append(Chart("Humidity and Temperature", "", [Panel("Humidity", "%", [Line("Ecowitt", [1, 2], [1.0, 2.0])])]))
            return "Last 7 days (21-27 Oct):\nHumidity: Ecowitt 72.1% avg."
    bot.agent = Agent()
    msg = chat_message([], edits)

    async def reply_text(text, **kw):
        replies.append(text)
    msg.reply_text = reply_text
    bot.charted.remember(1, 50, "overlay ecowitt outdoor humidity with airgradient humidity, on another axis the temperatures")

    async def answer(text=None):
        pass
    update = NS(callback_query=NS(data="pd:7", message=msg, answer=answer), effective_message=msg, effective_chat=msg.chat,
                effective_user=NS(username="rob", full_name="Rob"))
    await bot.on_period_button(update, CONTEXT)
    assert replies == [] and edits and "Humidity: Ecowitt 72.1% avg." in edits[0][0]
