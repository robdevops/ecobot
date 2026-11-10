import asyncio
import json
from datetime import datetime
from types import SimpleNamespace as NS

from lib import bot as botmod
from lib import intent, periods
from lib.bot import Bot
from lib.service import Service
from lib.service.summary import OVER_TIME
from lib.specs import Chart, Line, Panel, to_json
from tests.fakes import TZ

NOW = datetime(2026, 10, 2, 12, 0)
SORRY = "Sorry, I couldn't draw that chart, please ask again"
ROW = [("1D", "pd:1d"), ("7D", "pd:7d"), ("1M", "pd:1m"), ("3M", "pd:3m"), ("1Y", "pd:1y")]   # summary's periods, as its drawn_by lists them


def labels(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def shaded(markup):
    """The texts of the shaded buttons (the blue ones)."""
    return [b.text for row in markup.inline_keyboard for b in row if b.style == "primary"]


def test_a_charts_buttons_are_its_drawn_bys_periods_labelled_as_the_service_labels_them_its_own_shaded():
    drawn_by = {"tool": "summary", "arguments": {"readings": ["outdoor.temperature"], "period": "last 30 days", "chart": True},
                "periods": list(OVER_TIME)}
    own = periods.own_period(drawn_by, NOW, TZ)
    assert own == "1m" and labels(periods.periods_keyboard(drawn_by, own)) == [ROW] and shaded(periods.periods_keyboard(drawn_by, own)) == ["1M"]
    assert periods.periods_keyboard({**drawn_by, "periods": []}) is None                               # none listed: no buttons
    months = {"tool": "days", "arguments": {"group_by": "month", "period": "2026-01-01 to 2026-10-01"},
              "periods": [{"period": "3m", "label": "3M"}, {"period": "1y", "label": "1Y"}, {"period": "on record", "label": "All"}]}
    assert labels(periods.periods_keyboard(months)) == [[("3M", "pd:3m"), ("1Y", "pd:1y"), ("All", "pd:on record")]]   # no 1D for a count per month
    assert periods.period_in("pd:on record") == "on record" and periods.with_period(months, "1y") == {"group_by": "month", "period": "1y"}
    assert periods.call_key("summary", {"b": 1, "a": [2]}) == periods.call_key("summary", {"a": [2], "b": 1}) == 'summary {"a":[2],"b":1}'


def test_only_the_newest_charts_are_remembered():
    charted = periods.Charted()
    for i in range(periods.REMEMBERED + 5):
        charted.remember(1, i, f"q{i}")
    assert len(charted.store) == periods.REMEMBERED and charted.get(1, 0) is None and charted.get(1, periods.REMEMBERED + 4)


def test_the_calls_behind_charts_survive_a_restart(tmp_path):
    from lib.alerts.notify import AlertState
    state = AlertState(tmp_path / "s.json")
    record = {"drawn_by": {"tool": "summary", "arguments": {"period": "7d"}, "periods": list(OVER_TIME)}, "words": False}
    Bot(NS(tz=TZ), None, [], state).charted.remember(1, 101, record)
    again = Bot(NS(tz=TZ), None, [], AlertState(tmp_path / "s.json"))
    assert again.charted.get(1, 101) == record


class Summary:
    """The service's summary as the stand-in draws it: a temperature chart of the period asked (its picture is the period's words, so a
    test can tell which period it is), with its drawn_by; `refuse` the periods it refuses, `slow` those it waits on."""
    name = "summary"

    def __init__(self):
        self.calls, self.refuse, self.slow = [], set(), {}

    async def call(self, args, meta=None):
        self.calls.append(dict(args))
        period = args.get("period") or "last 24 hours"
        if period in self.slow:
            await self.slow[period].wait()
        if period in self.refuse:
            return json.dumps({"error": f"Nothing is held for {period}."})
        chart = Chart("Temperature", "", [Panel("Temperature", "°C", [Line("Outdoor", [1, 2], [1.0, 2.0])])])
        if args.get("start"):   # set times, as the real summary keeps them: no periods to offer
            arguments, listed = dict(args), []
        else:
            arguments, listed = {**{k: v for k, v in args.items() if k not in ("start", "end")}, "period": period}, [dict(p) for p in OVER_TIME]
        data = {**to_json(chart, TZ), "drawn_by": {"tool": "summary", "arguments": arguments, "periods": listed}}
        return json.dumps({"caption": "Fri 25 Sep – Thu 1 Oct 2026\nTemperature: low 1 °C · high 2 °C", "charts": [data]})


def make_bot(alert_state, monkeypatch):
    class Agent:
        async def run(self, *a, **k):
            raise AssertionError("no model for a plain chart")
    monkeypatch.setattr(botmod, "render_data", lambda data, tz, times=None: str(data["drawn_by"]["arguments"].get("period")
                                                                                      or data["drawn_by"]["arguments"]["start"]).encode())
    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt")]
    return Bot(NS(tz=TZ), Agent(), sources, alert_state, service=Service([Summary()]))


def calls(bot) -> list[dict]:
    """The calls the bot made of the service's summary."""
    return bot.service["summary"].calls


async def test_closing_the_bot_cancels_its_uploads_still_running(alert_state, monkeypatch):
    bot = make_bot(alert_state, monkeypatch)
    upload = asyncio.create_task(asyncio.Event().wait())
    bot.tasks.running.add(upload)
    await bot.tasks.close()
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


CONTEXT = NS(bot=NS(send_chat_action=send_chat_action, send_message_draft=send_chat_action, id=99))


def press(msg, data, answer):
    return NS(callback_query=NS(data=data, message=msg, answer=answer), effective_message=msg, effective_chat=msg.chat,
              effective_user=NS(username="rob", full_name="Rob"))


async def test_a_chart_is_sent_with_its_periods_under_it_and_its_call_is_remembered(alert_state, monkeypatch):
    bot, sent = make_bot(alert_state, monkeypatch), []
    msg = chat_message(sent)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await bot.respond(update, CONTEXT, "Temperature chart 30d")
    caption, markup = sent[0]
    assert caption == "Temperature" and labels(markup) == [ROW] and shaded(markup) == ["1M"]
    assert calls(bot) == [{"readings": ["temperature"], "period": "30d", "chart": True}]   # the words as they are
    record = bot.charted.get(1, 101)
    assert record["drawn_by"]["arguments"]["period"] == "30d" and record["words"] is False


async def test_pressing_a_period_replays_the_charts_call_with_that_period_in_place(alert_state, monkeypatch):
    bot, edits = make_bot(alert_state, monkeypatch), []
    msg = chat_message([], edits)
    drawn_by = {"tool": "summary", "arguments": {"readings": ["outdoor.temperature"], "period": "last 30 days", "chart": True},
                "periods": [dict(p) for p in OVER_TIME]}
    bot.charted.remember(1, 50, {"drawn_by": drawn_by, "words": False})
    answers = []

    async def answer(text=None, **k):
        answers.append(text)
    await bot.on_period_button(press(msg, "pd:3m", answer), CONTEXT)
    assert calls(bot) == [{"readings": ["outdoor.temperature"], "period": "3m", "chart": True}] and answers == ["Drawing 3M..."]
    caption, markup = edits[0]
    assert caption == "Temperature" and labels(markup) == [ROW] and shaded(markup) == ["3M"]
    assert bot.charted.get(1, 50)["drawn_by"]["arguments"]["period"] == "3m"


async def test_a_replay_the_service_refuses_shows_its_refusal_on_the_button(alert_state, monkeypatch):
    bot, edits = make_bot(alert_state, monkeypatch), []
    bot.service["summary"].refuse.add("1y")
    msg = chat_message([], edits)
    bot.charted.remember(1, 50, {"drawn_by": {"tool": "summary", "arguments": {"period": "7d"}, "periods": [dict(p) for p in OVER_TIME]}})
    answers = []

    async def answer(text=None, **k):
        answers.append(text)
    await bot.on_period_button(press(msg, "pd:1y", answer), CONTEXT)
    assert answers == ["Nothing is held for 1y."] and edits == []
    await bot.on_period_button(press(msg, "pd:2y", answer), CONTEXT)                    # a period the chart doesn't list
    assert answers[-1] == "That chart is out of date, please ask again"


async def test_a_replayed_chart_that_cannot_be_drawn_or_sent_says_sorry_on_the_button_and_raises_nothing(alert_state, monkeypatch):
    from telegram.error import NetworkError
    bot, edits = make_bot(alert_state, monkeypatch), []
    msg = chat_message([], edits)
    record = {"drawn_by": {"tool": "summary", "arguments": {"period": "7d"}, "periods": [dict(p) for p in OVER_TIME]}, "words": False}
    bot.charted.remember(1, 50, record)
    answers = []

    async def answer(text=None, **k):   # Telegram takes one answer per press
        if answers:
            raise BadRequest("Query is too old and response timeout expired or query id is invalid")
        answers.append(text)

    def broken(data, tz, times=None):
        raise ValueError("no panels")
    monkeypatch.setattr(botmod, "render_data", broken)
    await bot.on_period_button(press(msg, "pd:3m", answer), CONTEXT)   # a chart that fails to render
    assert answers == [SORRY] and edits == []

    async def offline(media, reply_markup=None):
        raise NetworkError("connection reset")
    monkeypatch.setattr(botmod, "render_data", lambda data, tz, times=None: b"png")
    said = []

    async def reply_text(text, **k):
        said.append(text)
    msg.edit_media, msg.reply_text, answers[:] = offline, reply_text, []
    await bot.on_period_button(press(msg, "pd:1y", answer), CONTEXT)   # Telegram failing other than refusing the edit, after "Drawing"
    assert answers == ["Drawing 1Y..."] and said == [SORRY]            # (the press is answered once: the sorry goes in the chat)


async def test_a_button_on_a_chart_from_before_a_restart_says_so(alert_state, monkeypatch):
    bot = make_bot(alert_state, monkeypatch)
    answers = []

    async def answer(text=None):
        answers.append(text)
    msg = chat_message([])
    await bot.on_period_button(press(msg, "pd:7d", answer), CONTEXT)
    assert answers == ["That chart is out of date, please ask again"] and not calls(bot)


async def test_a_reply_with_period_buttons_leaves_the_persistent_keyboard_for_the_next_reply():
    from lib.templates import keyboard
    sent = []

    class Replies:
        async def reply_photo(self, photo, **kw):
            sent.append(kw["reply_markup"])
            return NS(message_id=7)
    row = periods.periods_keyboard({"tool": "summary", "arguments": {"period": "7d"}, "periods": [dict(p) for p in OVER_TIME]})
    got = await botmod.deliver(Replies(), "", [b"p"], markup=keyboard(), titles=["T"], period_row=row)
    assert got.carried is False and labels(sent[0])[0][0][0] == "1D" and got.photo.message_id == 7


def test_a_chart_drawn_lately_comes_back_from_the_cache_until_it_is_too_old_or_pushed_out():
    clock = [0.0]
    cache = periods.ImageCache(lambda: clock[0])
    key = periods.call_key("summary", {"readings": ["outdoor.temperature"], "period": "last 30 days", "chart": True})
    assert cache.get(key) is None
    cache.put(key, b"png", "Temperature", drawn_by={"tool": "summary", "arguments": {"period": "1m"}, "periods": []})
    assert cache.get(key) == (b"png", "Temperature") == cache.get('summary {"period":"1m"}')   # under the call drawn_by states too
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
    for period in ("1m", "1m", "7d", "1m", "7d"):
        await bot.on_period_button(press(msg, f"pd:{period}", answer), CONTEXT)
    assert len(calls(bot)) == 2                     # the first send and the first 1M: the rest came from the cache (a press on the shown 1M does nothing)
    assert [e[0] for e in edits] == ["Temperature"] * 4   # the repeated 1M press edited nothing


async def test_two_wordings_of_one_chart_share_one_picture(alert_state, monkeypatch):
    bot, sent = make_bot(alert_state, monkeypatch), []
    update = pressed(bot, sent)
    await bot.respond(update, CONTEXT, "Temperature chart 7d")
    await bot.respond(update, CONTEXT, "temperature chart 7d?")
    assert len(calls(bot)) == 1 and sent[1][0] == "copy 1"                               # the same call: drawn once, sent as Telegram's copy


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
    for period in ("1m", "7d", "1m"):
        await bot.on_period_button(press(msg, f"pd:{period}", answer), CONTEXT)
    assert uploads == [b"7d"] and edits == ["upload", "7d", "edit 1"]   # 1M drawn and uploaded once; then both by Telegram's copy


def pressed(bot, sent):
    """A private chat's message whose photos land in `sent` as (photo, caption)."""
    async def reply_photo(photo, caption=None, **kw):
        sent.append((photo, caption))
        return NS(message_id=100 + len(sent), photo=(NS(file_id="small"), NS(file_id=f"copy {len(sent)}")))
    msg = NS(chat_id=1, message_id=50, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    return NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))


async def test_a_chart_just_drawn_is_sent_again_at_once_and_then_as_telegrams_copy(alert_state, monkeypatch):
    bot, sent = make_bot(alert_state, monkeypatch), []
    update = pressed(bot, sent)
    await bot.respond(update, CONTEXT, "Temperature chart 1d")
    asked = len(calls(bot))
    assert asked == 1 and sent == [(b"1d", "Temperature")]
    await bot.respond(update, CONTEXT, "Temperature chart 1d")
    assert len(calls(bot)) == asked and sent[-1] == ("copy 1", "Temperature")             # drawn lately: no new call, Telegram's copy
    history = bot.chats[(1, None)].history
    assert history[-1]["role"] == "assistant" and history[-1]["content"]                 # its text is kept, as a drawn one's is


async def test_a_chart_is_drawn_again_once_it_is_stale_and_never_answers_another_call(alert_state, monkeypatch):
    clock = [0.0]
    bot, sent = make_bot(alert_state, monkeypatch), []
    bot.images = periods.ImageCache(lambda: clock[0])
    await bot.respond(pressed(bot, sent), CONTEXT, "Temperature chart 1d")
    asked = len(calls(bot))
    await bot.respond(pressed(bot, sent), CONTEXT, "temperature chart today")     # since midnight: another call, drawn
    assert len(calls(bot)) == asked + 1
    asked = len(calls(bot))
    clock[0] = periods.ImageCache.FRESH + 1
    await bot.respond(pressed(bot, sent), CONTEXT, "Temperature chart 1d")        # too old to send as it is
    assert len(calls(bot)) == asked + 1


def preloading_bot(alert_state, monkeypatch, refuse=None, clock=None):
    """A bot with TELEGRAM_PRELOAD_CHAT set, and the (chat, photo, silent) of every upload to it. The periods are uploaded side by side,
    in no fixed order, so each period's picture is its period's words and its file id "file " and those words; context.refused.error
    is what the uploads raise (none: they go through)."""
    bot, uploads, refused = make_bot(alert_state, monkeypatch), [], NS(error=refuse)
    bot.cfg = NS(tz=TZ, telegram_preload_chat=-1001)
    if clock:
        bot.images = periods.ImageCache(lambda: clock[0])
        bot.alert_charts.images = bot.images

    async def send_photo(chat, photo, caption=None, disable_notification=False):
        if refused.error:
            raise refused.error
        uploads.append((chat, photo, disable_notification))
        return NS(photo=(NS(file_id="small"), NS(file_id=f"file {photo.decode()}")))
    return bot, uploads, NS(bot=NS(send_chat_action=send_chat_action, send_message_draft=send_chat_action, send_photo=send_photo, id=99), refused=refused)


def uploads_of(*periods_):
    """What the uploads to the preload chat are, in any order, when they are the charts of these periods."""
    return sorted((-1001, p.encode(), True) for p in periods_)


async def settle(bot):
    await asyncio.gather(*bot.tasks.running)


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
    assert sorted(uploads) == uploads_of("1d", "1m", "3m", "1y")                          # 1D, 1M, 3M, 1Y: not the 7D just sent
    assert all(c["period"] in ("7d", "1d", "1m", "3m", "1y") for c in calls(bot))
    asked = len(calls(bot))

    async def answer(text=None):
        pass
    await bot.on_period_button(press(msg, "pd:3m", answer), context)
    assert len(calls(bot)) == asked and edits[0] == "file 3m"                              # Telegram's copy of the 3M, no new call or upload


async def test_nothing_is_uploaded_ahead_without_the_setting_and_a_refused_upload_stops_it_with_one_warning(alert_state, monkeypatch, caplog):
    bot, uploads, context = preloading_bot(alert_state, monkeypatch)
    bot.cfg = NS(tz=TZ)
    update = pressed(bot, [])
    await bot.respond(update, context, "Temperature chart 7d")
    await settle(bot)
    assert uploads == [] and not bot.tasks.running
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
    assert sorted(uploads) == uploads_of("1d", "3m", "1y")              # the 7D and the 1M were sent as the questions' own charts (3M is 91 days)


async def test_a_period_that_cannot_be_drawn_is_left_out_and_the_others_are_uploaded(alert_state, monkeypatch):
    bot, uploads, context = preloading_bot(alert_state, monkeypatch)
    bot.service["summary"].refuse.add("3m")
    await bot.respond(pressed(bot, []), context, "Temperature chart 7d")
    await settle(bot)
    assert sorted(uploads) == uploads_of("1d", "1m", "1y")
    drawn_by = bot.images.held(periods.call_key("summary", {"readings": ["temperature"], "period": "7d", "chart": True})).drawn_by
    key = lambda p: periods.call_key("summary", periods.with_period(drawn_by, p))
    assert bot.images.has_copy(key("1m")) and not bot.images.has_copy(key("3m"))


async def test_each_period_is_uploaded_as_soon_as_it_is_drawn_not_when_the_slowest_is(alert_state, monkeypatch):
    bot, uploads, context = preloading_bot(alert_state, monkeypatch)
    slow, three, send = asyncio.Event(), asyncio.Event(), context.bot.send_photo
    bot.service["summary"].slow["1d"] = slow

    async def send_photo(chat, photo, **kw):
        message = await send(chat, photo, **kw)
        if len(uploads) == 3:
            three.set()
        return message
    context.bot.send_photo = send_photo
    bot.preload_soon(context.bot, {"tool": "summary", "arguments": {"readings": ["outdoor.temperature"], "period": "7d", "chart": True},
                                   "periods": [dict(p) for p in OVER_TIME]})
    await asyncio.wait_for(three.wait(), 5)
    assert sorted(uploads) == uploads_of("1m", "3m", "1y")                # the 1D, first in line, is still being drawn
    slow.set()
    await asyncio.wait_for(settle(bot), 5)
    assert sorted(uploads) == uploads_of("1d", "1m", "3m", "1y")


async def test_a_chart_uploaded_ahead_is_used_for_30_minutes_and_then_drawn_afresh(alert_state, monkeypatch):
    clock = [0.0]
    bot, uploads, context = preloading_bot(alert_state, monkeypatch, clock=clock)
    await bot.respond(pressed(bot, []), context, "Temperature chart 7d")
    await settle(bot)
    key = periods.call_key("summary", {"readings": ["temperature"], "period": "1m", "chart": True})
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
    row = periods.periods_keyboard({"tool": "summary", "arguments": {"period": "7d"}, "periods": [dict(p) for p in OVER_TIME]})
    assert await bot._redraw(msg, "Last 7 days: 72% avg.", b"png", "Humidity", row, {})
    assert edits[0][0] == "Humidity\n\nLast 7 days: 72% avg." and replies == []                  # one message, as when the chart was first sent
    assert await bot._redraw(msg, "x" * botmod.CAPTION_LIMIT, b"png", "Humidity", row, {})
    assert edits[1][0] == "Humidity" and replies == ["x" * botmod.CAPTION_LIMIT]                  # too long for a caption: its own message


async def test_the_keyboards_charts_are_drawn_ahead_and_a_press_is_answered_from_them(alert_state, monkeypatch):
    clock = [0.0]
    bot, uploads, context = preloading_bot(alert_state, monkeypatch, clock=clock)
    bot.cfg = NS(tz=TZ)                                                                     # no preload chat: drawn, not uploaded
    await bot.draw_buttons_ahead(context.bot)
    from lib.timeutil import now_local
    now = now_local(TZ)
    assert uploads == [] and bot.images.ready(periods.call_key(*bot._sentence_call("Temperature chart 1d", now)))
    assert bot._sentence_call("Air quality all metrics 1d", now) is None                     # no air sensor here: nothing to draw
    asked, sent = len(calls(bot)), []
    await bot.respond(pressed(bot, sent), context, "Temperature chart 1d")
    assert len(calls(bot)) == asked and sent[0][0] == b"1d"                       # the one drawn ahead: no new call
    clock[0] += 400                                                                          # too old to answer with: the press draws afresh
    await bot.respond(pressed(bot, sent), context, "Temperature chart 1d")
    assert len(calls(bot)) == asked + 1
    asked = len(calls(bot))
    await bot.draw_buttons_ahead(context.bot)
    drawn = [c["readings"] for c in calls(bot)[asked:]]
    assert ["humidity"] in drawn and ["temperature"] not in drawn   # just drawn: left alone


async def test_the_keyboards_charts_are_drawn_ahead_as_background_requests(alert_state, monkeypatch):
    bot, _, context = preloading_bot(alert_state, monkeypatch)
    metas = []
    call = bot.service["summary"].call

    async def counting(args, meta=None):
        metas.append(meta)
        return await call(args, meta)
    bot.service["summary"].call = counting
    await bot.draw_buttons_ahead(context.bot)
    assert metas and all(m["priority"] == "background" and m["chart_points"] == "drawn" for m in metas)


async def test_the_keyboards_charts_are_uploaded_ahead_only_while_the_bot_is_in_use(alert_state, monkeypatch):
    import time
    from lib.bot import USED_SECONDS
    clock = [0.0]
    bot, uploads, context = preloading_bot(alert_state, monkeypatch, clock=clock)
    await bot.draw_buttons_ahead(context.bot)
    assert uploads == [] and not bot.used_lately                                             # nobody has used the bot: drawn, not uploaded
    bot.noticed()
    clock[0] += 120
    await bot.draw_buttons_ahead(context.bot)
    assert (-1001, b"1d", True) in uploads
    sent = []
    await bot.respond(pressed(bot, sent), context, "Temperature chart 1d")
    assert sent[0][0] == "file 1d"                                                # a press sends Telegram's copy
    await settle(bot)                                                                        # (the press's other periods, uploaded ahead)
    uploads.clear()
    clock[0] += 120
    bot.last_used = time.monotonic() - USED_SECONDS - 1                                     # 15 minutes without a message or a press
    await bot.draw_buttons_ahead(context.bot)
    assert uploads == []


def test_a_chart_knows_the_time_of_its_newest_reading():
    from lib.specs import Bars
    assert Chart("Temperature", "", [Panel("Temperature", "°C", [Line("Outdoor", [1, 2], [1.0, 2.0]), Line("Indoor", [1, 5], [1.0, 2.0])])]).newest == 5
    assert Chart("Rain", "", [Panel("Rain", "mm", [], bars=Bars("Rain", "mm", [0, 3600], [0.0, 1.0], 3600))]).newest == 3600


async def test_the_chart_of_an_alert_sent_lately_is_drawn_ahead_with_the_keyboards(alert_state, monkeypatch):
    from lib.alerts.pictures import AlertCharts
    clock = [0.0]
    bot, uploads, context = preloading_bot(alert_state, monkeypatch, clock=clock)
    bot.alert_charts.clock = lambda: clock[0]
    uv, temperature = ["uv"], ["temperature"]
    drawn = lambda since: [c["readings"] for c in calls(bot)[since:]]
    await bot.draw_buttons_ahead(context.bot)
    assert uv not in drawn(0) and temperature in drawn(0)                                # UV is no button: not drawn until an alert
    bot.alert_charts.sent("uv", NS(photo=(NS(file_id="large"),)))
    bot.alert_charts.sent("temps", NS(photo=(NS(file_id="large"),)))
    since = len(calls(bot))
    clock[0] += 300
    await bot.draw_buttons_ahead(context.bot)
    assert drawn(since).count(uv) == 1 and drawn(since).count(temperature) == 1          # the alert's chart too, the button's once
    bot.noticed()
    clock[0] += 300
    uploads.clear()
    since = len(calls(bot))
    await bot.draw_buttons_ahead(context.bot)
    assert uv in drawn(since) and len(uploads) == len(drawn(since))                      # uploaded ahead with the keyboard's
    since = len(calls(bot))
    clock[0] += AlertCharts.WARM_SECONDS
    await bot.draw_buttons_ahead(context.bot)
    assert uv not in drawn(since)                                                        # six hours on: no longer kept warm


def test_a_chart_drawn_in_one_theme_is_not_sent_again_after_the_theme_changes(monkeypatch):
    from lib import charts
    images = periods.ImageCache(lambda: 0.0)
    monkeypatch.setattr(charts, "MODE", "off")
    images.put("summary {}", b"light", "T", "caption")
    assert images.get("summary {}") and images.ready("summary {}")
    monkeypatch.setattr(charts, "MODE", "on")                                      # 8pm under DARKMODE=8pm-8am
    assert images.ready("summary {}") is None and images.get("summary {}") is None


def test_a_chart_with_no_periods_to_offer_gets_a_reload_when_its_period_runs_to_now():
    """A chart whose call lists no periods gets a 🔄 when its period runs to the present (to now; for a tool of whole days, through
    yesterday), so it can be drawn again with what has come in since; one over a fixed past stretch gets nothing."""
    reload = [[("🔄", "pd:reload")]]

    def row(tool, **arguments):
        markup = periods.periods_keyboard({"tool": tool, "arguments": arguments, "periods": []}, None, NOW, TZ)
        return labels(markup) if markup else None
    assert row("summary", readings=["outdoor.temperature"], start="2026-10-02 09:00") == reload            # since 9am: to now
    assert row("summary", start="2026-10-01 09:00", end="2026-10-01 18:00") is None                        # a fixed stretch
    assert row("summary", period="today") == reload and row("summary", period="yesterday") is None
    assert row("days", group_by="year", period="on record") == reload                                      # through yesterday
    assert row("days", group_by="year", period="2024-01-01 to 2025-12-31") is None
    assert row("days", group_by="year", period="2025-01-01 to now") == reload
    for fixed in ("2026-09-01 to 2026-10-01", "September", "last month", "2025"):   # fixed dates, even ending yesterday: never more
        assert row("days", group_by="year", period=fixed) is None, fixed
    assert periods.periods_keyboard({"tool": "summary", "arguments": {"start": "2026-10-02 09:00"}, "periods": []}) is None   # no clock: none
    listed = {"tool": "summary", "arguments": {"period": "7d"}, "periods": [dict(p) for p in OVER_TIME]}
    assert labels(periods.periods_keyboard(listed, "7d", NOW, TZ)) == [ROW]                                # periods listed: those


async def test_pressing_reload_draws_the_same_call_again_in_place_with_the_reload_under_it(alert_state, monkeypatch):
    bot, edits, sent = make_bot(alert_state, monkeypatch), [], []
    msg = chat_message(sent, edits)
    start = f"{datetime.now(TZ):%Y-%m-%d} 00:00"
    drawn_by = {"tool": "summary", "arguments": {"readings": ["outdoor.temperature"], "start": start, "chart": True}, "periods": []}
    bot.charted.remember(1, 50, {"drawn_by": drawn_by, "words": False})
    key = periods.call_key("summary", drawn_by["arguments"])
    bot.images.put(key, b"old picture", "Temperature", drawn_by=drawn_by)   # drawn a moment ago: a reload draws it afresh all the same
    answers = []

    async def answer(text=None, **k):
        answers.append(text)
    await bot.on_period_button(press(msg, "pd:reload", answer), CONTEXT)
    assert calls(bot) == [drawn_by["arguments"]] and answers == ["Drawing again..."]
    caption, markup = edits[0]
    assert caption == "Temperature" and labels(markup) == [[("🔄", "pd:reload")]]
    assert bot.charted.get(1, 50)["drawn_by"]["arguments"] == drawn_by["arguments"] and bot.images.held(key).png == start.encode()
    listed = {"tool": "summary", "arguments": {"period": "7d"}, "periods": [dict(p) for p in OVER_TIME]}
    bot.charted.remember(1, 50, {"drawn_by": listed, "words": False})
    await bot.on_period_button(press(msg, "pd:reload", answer), CONTEXT)          # a chart with periods has no 🔄
    assert answers[-1] == "That chart is out of date, please ask again" and len(calls(bot)) == 1


async def test_pressing_a_1d_charts_own_button_draws_it_again_and_a_longer_ones_does_nothing(alert_state, monkeypatch):
    """The shaded 1D under a chart that runs to now draws it again, past the picture cache, with the latest readings; the shaded
    button of a longer period changes nothing (a few minutes more barely show on a week)."""
    bot, edits = make_bot(alert_state, monkeypatch), []
    msg = chat_message([], edits)
    day = {"tool": "summary", "arguments": {"readings": ["outdoor.temperature"], "period": "last 24 hours", "chart": True},
           "periods": [dict(p) for p in OVER_TIME]}
    bot.charted.remember(1, 50, {"drawn_by": day, "words": False})
    key = periods.call_key("summary", day["arguments"])
    bot.images.put(key, b"old picture", "Temperature", drawn_by=day)
    answers = []

    async def answer(text=None, **k):
        answers.append(text)
    await bot.on_period_button(press(msg, "pd:1d", answer), CONTEXT)
    assert calls(bot) == [day["arguments"]] and answers == ["Drawing 1D..."]
    caption, markup = edits[0]
    assert labels(markup) == [ROW] and shaded(markup) == ["1D"] and bot.images.held(key).png == b"last 24 hours"
    week = {**day, "arguments": {**day["arguments"], "period": "7d"}}
    bot.charted.remember(1, 50, {"drawn_by": week, "words": False})
    await bot.on_period_button(press(msg, "pd:7d", answer), CONTEXT)
    assert len(calls(bot)) == 1 and answers[-1] is None and len(edits) == 1
    yesterday = {**day, "arguments": {**day["arguments"], "period": "yesterday"}}   # (a 1D over a day that is over: nothing new)
    bot.charted.remember(1, 50, {"drawn_by": yesterday, "words": False})
    if periods.own_period(yesterday, botmod.now_local(TZ), TZ) == "1d":
        await bot.on_period_button(press(msg, "pd:1d", answer), CONTEXT)
        assert len(calls(bot)) == 1
