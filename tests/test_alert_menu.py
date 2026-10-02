from types import SimpleNamespace as NS

import pytest
from telegram.error import BadRequest

from lib.alerts import AlertState, Notifier
from lib.alerts.menu import ALL, LABELS, TITLE, available_kinds, keyboard, options, title
from lib.bot import Bot
from tests.fakes import TZ

KINDS = ["rain", "rain_likely", "gusts", "uv", "temps", "air", "pollen", "forecast"]


def rows(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def test_the_collapsed_menu_is_subscribe_and_unsubscribe():
    assert rows(keyboard(set(), KINDS)) == [[("➕ Subscribe", "al:open:sub"), ("➖ Unsubscribe", "al:open:unsub")]]


def test_unsubscribe_opens_the_types_that_are_on_and_subscribe_the_types_that_are_off():
    got = rows(keyboard({"pollen", "forecast"}, KINDS, "unsub"))
    assert got[0] == [("➕ Subscribe", "al:open:sub"), ("▾ ➖ Unsubscribe", "al:close")]
    assert got[1] == [("rain", "al:off:rain:unsub"), ("rain likely", "al:off:rain_likely:unsub")]
    assert [text for row in got[1:-1] for text, _ in row] == ["rain", "rain likely", "gusts", "UV", "temperature crossing", "air quality"]
    assert got[-1] == [("Unsubscribe from all", "al:off:all:unsub")]
    got = rows(keyboard({"pollen", "forecast"}, KINDS, "sub"))
    assert got[0][0] == ("▾ ➕ Subscribe", "al:close")
    assert got[1:] == [[("pollen & asthma", "al:on:pollen:sub"), ("forecast changes", "al:on:forecast:sub")], [("Subscribe to all", "al:on:all:sub")]]
    assert not any(data == "al:noop" for row in got for _, data in row)                  # no button that does nothing
    assert options({"pollen"}, KINDS, "sub") == ["pollen"] and options({"pollen"}, ["rain", "pollen"], "unsub") == ["rain"]
    assert len(rows(keyboard(set(), KINDS, "sub"))) == 1                                  # nothing off: nothing listed


def test_only_the_types_the_bot_has_are_listed_and_every_callback_is_short():
    assert available_kinds({"Ecowitt"}) == ["rain", "rain_likely", "gusts", "uv", "temps"]
    assert available_kinds({"Ecowitt", "AirGradient", "Pollen", "Forecast"}) == KINDS and available_kinds({"AirGradient"}) == ["air"]
    for section in (None, "sub", "unsub"):
        for text, data in (b for row in rows(keyboard({"uv"}, KINDS, section)) for b in row):
            assert len(data.encode()) <= 64


def test_the_title_lists_what_is_on_and_off():
    assert title({"gusts", "uv"}, KINDS[:5]) == f"{TITLE}\n✅ On: rain, rain likely, temperature crossing\n🔕 Off: gusts, UV"
    assert title(set(), ["air"]).endswith("🔕 Off: nothing") and title({"air"}, ["air"]).endswith("✅ On: nothing\n🔕 Off: air quality")


def test_state_mutes_one_type_or_all_and_old_files_load(tmp_path):
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "a")
    state.add_chat(2, "b")
    state.set_kind(1, "rain", False)
    state.set_kind(1, "bogus", False)
    assert state.muted(1) == {"rain"} and state.alert_chats("rain") == [2] and state.alert_chats("uv") == [1, 2] and state.alerts_on(1)
    state.set_kind(1, ALL, False)
    assert state.muted(1) == set(LABELS) and not state.alerts_on(1) and state.alert_chats() == [2]
    state.set_kind(1, ALL, True)
    assert state.muted(1) == set() and "muted" not in state.chats[1]
    state.set_kind(2, "gusts", False)
    assert AlertState(tmp_path / "s.json").muted(2) == {"gusts"}                       # saved
    (tmp_path / "old.json").write_text('{"chats": {"7": {"title": "x", "alerts": false}, "8": {"title": "y", "alerts": true}}, "monitor": {}}')
    old = AlertState(tmp_path / "old.json")
    assert old.muted(7) == set(LABELS) and old.muted(8) == set() and old.alert_chats() == [8]
    state.set_alerts(1, "a", False)                                                      # the /alerts off shortcut
    assert not state.alerts_on(1)


class Sent:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


async def test_an_alert_goes_to_the_chats_subscribed_to_its_type_and_carries_the_menu(tmp_path):
    state, bot = AlertState(tmp_path / "s.json"), Sent()
    for chat in (1, 2, 3):
        state.add_chat(chat, str(chat))
    state.set_kind(2, "rain", False)
    state.set_kind(3, ALL, False)
    notifier = Notifier(bot, state, KINDS)
    await notifier("It's raining", kind="rain")
    await notifier("Gusts", kind="gusts")
    assert [(c, t.split("\n")[0]) for c, t, _ in bot.sent] == [(1, "It's raining"), (1, "Gusts"), (2, "Gusts")]
    markup = bot.sent[0][2]["reply_markup"]
    assert rows(markup) == [[("➕ Subscribe", "al:open:sub"), ("➖ Unsubscribe", "al:open:unsub")]]
    assert "/alerts to change" in bot.sent[0][1]
    assert await notifier.to_chat(2, "x", kind="rain") is False and await notifier.to_chat(2, "x", kind="uv") is True


class Query:
    """A press on a button: records the toast and what was edited."""

    def __init__(self, data, text="🔔 alert text", chat_type="private", chat_id=1, user=7):
        self.data, self.message = data, NS(text=text, chat=NS(type=chat_type, id=chat_id))
        self.from_user, self.toast, self.edits, self.fail = NS(id=user), "unset", [], None

    async def answer(self, text=None, **kw):
        self.toast = text

    async def edit_message_reply_markup(self, reply_markup=None):
        if self.fail:
            raise self.fail
        self.edits.append(("markup", reply_markup))

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append(("text", text, reply_markup))


def make_bot(tmp_path, sources=("Ecowitt", "AirGradient", "Pollen", "Forecast")):
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "private")
    state.add_chat(-5, "group")
    bot = Bot(NS(tz=TZ), None, [NS(name=n) for n in sources], state)
    return bot, state


def ctx(status="administrator"):
    async def get_chat_member(chat_id, user_id):
        return NS(status=status)
    return NS(bot=NS(get_chat_member=get_chat_member))


async def press(bot, query, **kw):
    await bot.on_alert_button(NS(callback_query=query), kw.get("context") or ctx())


async def test_pressing_unsubscribe_then_a_type_updates_the_state_and_redraws_the_open_section(tmp_path):
    bot, state = make_bot(tmp_path)
    q = Query("al:open:unsub")
    await press(bot, q)
    assert q.toast is None and rows(q.edits[0][1])[1][0] == ("rain", "al:off:rain:unsub") and q.edits[0][0] == "markup"
    q = Query("al:off:rain:unsub")
    await press(bot, q)
    assert state.muted(1) == {"rain"} and q.toast == "Rain alerts off in this chat"
    drawn = rows(q.edits[0][1])
    assert drawn[0][1] == ("▾ ➖ Unsubscribe", "al:close") and "rain" not in [t for row in drawn[1:] for t, _ in row]
    q = Query("al:on:rain:sub")
    await press(bot, q)
    assert state.muted(1) == set() and q.toast == "Rain alerts on in this chat"
    q = Query("al:off:all:unsub")
    await press(bot, q)
    assert q.toast == "All alerts off in this chat" and not state.alerts_on(1) and len(rows(q.edits[0][1])) == 1    # nothing left: closed
    q = Query("al:open:unsub")
    await press(bot, q)
    assert q.toast == "No alerts are on" and q.edits == []                                  # nothing to list: a toast, no dead button
    q = Query("al:open:sub")
    await press(bot, q)
    assert rows(q.edits[0][1])[-1] == [("Subscribe to all", "al:on:all:sub")]
    q = Query("al:close")
    await press(bot, q)
    assert len(rows(q.edits[0][1])) == 1


async def test_the_alerts_message_keeps_its_summary_current_but_an_alert_keeps_its_text(tmp_path):
    bot, state = make_bot(tmp_path)
    q = Query("al:off:uv:unsub", text=f"{TITLE}\n✅ On: ...")
    await press(bot, q)
    assert q.edits[0][0] == "text" and "🔕 Off: UV" in q.edits[0][1]
    q = Query("al:off:gusts:unsub", text="💨 Strong gusts: 55 km/h")
    await press(bot, q)
    assert q.edits[0][0] == "markup"


async def test_noop_unknown_kinds_and_unchanged_markup_are_harmless(tmp_path):
    bot, state = make_bot(tmp_path, sources=("Ecowitt",))
    q = Query("al:noop")
    await press(bot, q)
    assert q.edits == [] and q.toast is None
    for data in ("al:off:pollen:unsub", "al:off:bogus:unsub", "al:weird", "garbage"):          # pollen is not a type here
        q = Query(data)
        await press(bot, q)
        assert q.edits == [] and state.muted(1) == set(), data
    state.set_kind(1, "rain", False)                                                          # something to list, so it edits
    q = Query("al:open:sub")
    q.fail = BadRequest("Message is not modified: specified new message content and reply markup are exactly the same")
    await press(bot, q)                                                                       # swallowed
    q.fail = BadRequest("Message to edit not found")
    with pytest.raises(BadRequest):
        await press(bot, q)


async def test_in_a_group_only_admins_may_change_alerts(tmp_path):
    bot, state = make_bot(tmp_path)
    q = Query("al:off:rain:unsub", chat_type="supergroup", chat_id=-5)
    await press(bot, q, context=ctx("member"))
    assert q.toast == "Only group admins can change alerts" and state.muted(-5) == set() and q.edits == []
    await press(bot, q, context=ctx("administrator"))
    assert state.muted(-5) == {"rain"} and q.edits
    q = Query("al:off:uv:unsub", chat_type="group", chat_id=-5)
    await press(bot, q, context=ctx("creator"))
    assert state.muted(-5) == {"rain", "uv"}


async def test_the_alerts_command_sends_the_menu_and_on_off_still_work(tmp_path):
    bot, state = make_bot(tmp_path)
    replies = []

    async def reply_text(body, **kw):
        replies.append((body, kw))
    chat = NS(type="private", id=1, title=None)
    update = NS(effective_message=NS(reply_text=reply_text, chat_id=1), effective_chat=chat, effective_user=NS(full_name="Rob", username="rob"))
    await bot.on_alerts(update, NS(args=[]))
    assert replies[0][0].startswith(TITLE) and "✅ On: rain, rain likely, gusts, UV, temperature crossing, air quality, pollen & asthma, forecast changes" in replies[0][0]
    assert rows(replies[0][1]["reply_markup"])[0] == [("➕ Subscribe", "al:open:sub"), ("➖ Unsubscribe", "al:open:unsub")]
    await bot.on_alerts(update, NS(args=["off"]))
    assert not state.alerts_on(1) and "✅ On: nothing" in replies[1][0]
    await bot.on_alerts(update, NS(args=["on"]))
    assert state.alerts_on(1) and "🔕 Off: nothing" in replies[2][0]


async def test_every_monitors_alert_names_its_type(tmp_path):
    from lib.alerts import AirMonitor, ForecastMonitor, PollenMonitor, WeatherMonitor
    from tests.test_alerts import FakePollen, FakeStation, air_reading, gusts, rain, uv
    from tests.test_forecast_alert import FakeForecast, day
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "chat")
    kinds = []

    async def notify(text, link=None, kind=None):
        kinds.append(kind)

    async def weather(first, then):
        mon = WeatherMonitor(FakeStation(first), state, notify)
        await mon.check()
        mon.station.data = then
        await mon.check()
    await weather(rain(0, 0, 0), rain(0, 0, 1))
    await weather(rain(0, 0, 1, 1), rain(0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0))
    await weather(gusts(10, 12), gusts(10, 12, 60))
    await weather(uv(3, 5), uv(3, 5, 10))
    assert kinds == ["rain", "rain", "gusts", "uv"]
    kinds.clear()
    mask = AirMonitor(NS(tz=TZ, link=None), state, notify)
    for pm in (80, 80, 20, 20):
        await mask.check(air_reading(pm))
    assert kinds == ["air", "air"]
    kinds.clear()
    pollen = FakePollen()
    pollen.grass = "High"
    await PollenMonitor(pollen, state, notify).check()
    assert kinds == ["pollen"]
    seen = []
    notifier = Notifier(Sent(), state)
    real = notifier.to_chat

    async def spy(chat_id, text, link=None, kind=None):
        seen.append(kind)
        return await real(chat_id, text, link, kind)
    notifier.to_chat = spy
    state.record_forecast(1, [day(0, pct=90)])
    await ForecastMonitor(FakeForecast([day(0, pct=5)]), state, notifier).check()
    assert seen == ["forecast"]
