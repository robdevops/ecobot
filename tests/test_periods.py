import json
from datetime import datetime
from types import SimpleNamespace as NS

from lib import bot as botmod
from lib import intent, periods
from lib.bot import Bot
from lib.specs import Chart, Line, Panel
from tests.fakes import TZ

NOW = datetime(2026, 10, 2, 12, 0)


def labels(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def test_the_row_is_always_week_month_and_quarter():
    assert labels(periods.keyboard()) == [[("Week", "pd:7"), ("Month", "pd:30"), ("Quarter", "pd:90")]]
    assert [t for t, _ in labels(periods.keyboard(30))[0]] == ["Week", "\u25cf Month", "Quarter"]          # the chart's own period is marked
    assert [t for t, _ in labels(periods.keyboard(61))[0]] == ["Week", "Month", "Quarter"]
    assert periods.days_in("pd:90") == 90 and periods.days_in("pd:365") is None and periods.days_in("al:on:x") is None


def test_a_question_keeps_its_words_and_swaps_its_period():
    for ask, expected in (("Temperature chart 30d", "Temperature chart 90d"), ("chart air quality last month", "chart air quality 90d"),
                          ("rain chart for the last week", "rain chart 90d"), ("weather chart", "weather chart 90d"),
                          ("plot humidity and rain 3m", "plot humidity and rain 90d")):
        assert intent.with_period(ask, 90, NOW) == expected
    assert intent.period_days("Temperature chart 30d", NOW) == 30 and intent.period_days("weather chart", NOW) == 7
    assert intent.period_days("chart 24h", NOW) == 1 and intent.period_days("chart 365d", NOW) == 365


def test_only_the_newest_questions_are_remembered():
    charted = periods.Charted()
    for i in range(periods.REMEMBERED + 5):
        charted.remember(1, i, f"q{i}")
    assert len(charted) == periods.REMEMBERED and (1, 0) not in charted and (1, periods.REMEMBERED + 4) in charted


def test_the_questions_behind_charts_survive_a_restart(tmp_path):
    from lib.alerts import AlertState
    state = AlertState(tmp_path / "s.json")
    Bot(NS(tz=TZ), None, [], state).charted.remember(1, 101, "Temperature chart 7d")
    again = Bot(NS(tz=TZ), None, [], AlertState(tmp_path / "s.json"))
    assert again.charted.get(1, 101) == "Temperature chart 7d"


class Tools:
    def __init__(self):
        self.calls = []

    async def call(self, name, raw, turn=None):
        self.calls.append((name, json.loads(raw)))
        turn.charts.append(Chart("Temperature", "", [Panel("Temperature", "°C", [Line("Outdoor", [1, 2], [1.0, 2.0])])]))
        return json.dumps({"period": "Fri 25 Sep 2026 - Thu 01 Oct 2026", "series": {"outdoor.temperature": {"unit": "℃", "low": "1", "high": "2"}}})


def make_bot(monkeypatch):
    class Agent:
        tools = Tools()

        async def run(self, *a, **k):
            raise AssertionError("no model for a plain chart")
    monkeypatch.setattr(botmod, "render_chart", lambda spec, tz: b"png")
    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt")]
    return Bot(NS(tz=TZ), Agent(), sources, None)


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


async def test_a_chart_is_sent_with_the_other_periods_under_it_and_its_question_is_remembered(monkeypatch):
    bot, sent = make_bot(monkeypatch), []
    msg = chat_message(sent)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await bot.respond(update, CONTEXT, "Temperature chart 30d")
    caption, markup = sent[0]
    assert caption == "Temperature" and labels(markup) == [[("Week", "pd:7"), ("\u25cf Month", "pd:30"), ("Quarter", "pd:90")]]
    assert bot.charted[(1, 101)] == "Temperature chart 30d"


async def test_pressing_a_period_redraws_the_chart_in_place_with_that_period_left_out(monkeypatch):
    bot, edits = make_bot(monkeypatch), []
    msg = chat_message([], edits)
    bot.charted.remember(1, 50, "Temperature chart 30d")
    answers = []

    async def answer(text=None):
        answers.append(text)
    query = NS(data="pd:90", message=msg, answer=answer)
    update = NS(callback_query=query, effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await bot.on_period_button(update, CONTEXT)
    name, args = bot.agent.tools.calls[0]
    assert name == "weather_history" and args["start_date"].startswith("2026-") and answers == ["Drawing 90 days..."]
    caption, markup = edits[0]
    assert caption == "Temperature" and [t for t, _ in labels(markup)[0]] == ["Week", "Month", "\u25cf Quarter"]
    assert bot.charted[(1, 50)] == "Temperature chart 90d"


async def test_a_button_on_a_chart_from_before_a_restart_says_so(monkeypatch):
    bot = make_bot(monkeypatch)
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
    shown = []
    carried = await botmod.deliver(Replies(), "", [b"p"], markup=keyboard(), titles=["T"], period_row=periods.keyboard(), sent_photo=shown)
    assert carried is False and labels(sent[0])[0][0][0] == "Week" and shown[0].message_id == 7


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


async def test_toggling_back_to_a_period_just_drawn_uses_the_cached_image_and_asks_nothing_again(monkeypatch):
    bot, sent, edits = make_bot(monkeypatch), [], []
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


async def test_redrawing_a_chart_from_a_button_shows_no_typing_or_thinking(monkeypatch):
    bot = make_bot(monkeypatch)
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
