from types import SimpleNamespace as NS

import pytest
from telegram.error import BadRequest

from lib.alerts import AlertState, Notifier
from lib.alerts.menu import ALL, LABELS, TITLE, Press, alert_keyboard, available_kinds, keyboard, parse, title
from lib.bot import Bot
from lib.buttons import struck as strike
from tests.fakes import TZ

KINDS = ["rain", "gusts", "uv", "temps", "particulates", "pollen_and_asthma", "forecast"]


def rows(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def test_alerts_has_two_buttons_alerts_and_sound_and_a_group_has_only_the_first():
    assert rows(keyboard({"rain"}, KINDS, loud=set())) == [[("🔔 Alerts ▸", "al:open:alerts"), ("🔊 Sound ▸", "al:open:sound")]]
    assert rows(keyboard(set(), KINDS)) == [[("🔔 Alerts ▸", "al:open:alerts")]]
    assert rows(keyboard(set(), KINDS, "sound")) == rows(keyboard(set(), KINDS))                     # a group cannot open Sound


def test_the_bell_toggles_are_green_when_on_and_struck_through_when_off():
    styles = lambda m: {b.text: b.style for row in m.inline_keyboard for b in row}
    assert styles(alert_keyboard("uv", set(), set(), "bell")) == {"🔔 ▾": None, "Alert": "success", strike("Sound"): None}
    assert styles(alert_keyboard("uv", {"uv"}, {"uv"}, "bell")) == {"🔔 ▾": None, strike("Alert"): None}


def test_every_button_of_every_keyboard_parses_and_builds_back_to_the_same_data():
    muted_sets = [set(), {"rain"}, {"rain", "uv", "particulates"}, set(LABELS)]
    seen = set()
    for muted in muted_sets:
        for loud in (None, set(), set(LABELS), {"rain", "uv"}):
            boards = [keyboard(muted, list(LABELS), open, loud) for open in (None, "alerts", "sound")]
            boards += [alert_keyboard(kind, muted, loud, open) for kind in LABELS for open in (None, "bell")]
            for board in boards:
                for row in board.inline_keyboard:
                    for b in row:
                        press = parse(b.callback_data)
                        assert press is not None and press.data() == b.callback_data, b.callback_data
                        seen.add(press.action)
    assert seen == {"open", "close", "alert", "sound"}
    assert parse("al:snd:uv:on:uv") == Press("sound", "uv", "bell", "uv", True) and parse("al:snd:uv:off") == Press("sound", "uv", "sound", None, False)
    for junk in (None, "", "al", "al:", "al:noop", "al:open", "al:on:rain", "al:snd:rain:maybe", "al:off:a:b:c:d", "ir:water:5", "garbage"):
        assert parse(junk) is None, junk


def test_a_type_that_is_on_is_a_green_button_and_one_that_is_off_is_struck_through_and_plain():
    markup = keyboard({"gusts"}, KINDS, "alerts", loud=set())
    styles = {b.text: b.api_kwargs.get("style") or b.style for row in markup.inline_keyboard for b in row}
    assert styles["rain"] == "success" and styles[strike("gusts")] is None and not styles["🔔 Alerts ▾"]
    assert markup.inline_keyboard[1][0].to_dict()["style"] == "success" and "style" not in markup.inline_keyboard[1][1].to_dict()   # and it is sent to Telegram


def test_each_menu_lists_its_types_struck_through_when_off_and_keeps_only_its_own_button():
    got = rows(keyboard({"gusts"}, KINDS, "alerts", loud={"rain"}))
    assert got[0] == [("🔔 Alerts ▾", "al:close")]                                                         # the button that opened it stays, open
    assert [t for row in got[1:] for t, _ in row] == ["rain", strike("gusts"), "UV", "temperature crossing", "particulates",
                                                       "pollen & asthma", "forecast changes"]
    assert got[1] == [("rain", "al:off:rain:alerts"), (strike("gusts"), "al:on:gusts:alerts")]
    assert "Sound" not in str(got)                                                                        # the other menu's button is hidden
    got = rows(keyboard({"gusts"}, KINDS, "sound", loud={"rain"}))
    assert got[0] == [("🔊 Sound ▾", "al:close")] and "Alerts" not in str(got)
    assert got[1] == [("rain", "al:snd:rain:off"), (strike("UV"), "al:snd:uv:on")]                          # gusts is off, so it is not listed at all
    assert [t for row in got[2:] for t, _ in row] == [strike(w) for w in ("temperature crossing", "particulates", "pollen & asthma", "forecast changes")]
    assert "gusts" not in str(got)
    assert rows(keyboard(set(KINDS), KINDS, "sound", loud=set())) == [[("🔔 Alerts ▸", "al:open:alerts")]]   # nothing is on: no Sound button either


def test_only_the_types_the_bot_has_are_listed_and_every_callback_is_short():
    assert available_kinds({"Ecowitt"}) == ["rain", "gusts", "uv", "temps"]
    assert available_kinds({"Ecowitt", "AirGradient", "Pollen", "Forecast"}) == KINDS and available_kinds({"AirGradient"}) == ["particulates"]
    for section in (None, "alerts", "sound"):
        for text, data in (b for row in rows(keyboard({"uv"}, KINDS, section, loud=set())) for b in row):
            assert len(data.encode()) <= 64


def test_the_title_lists_what_is_on_and_off():
    assert title({"gusts", "uv"}, KINDS[:4]) == f"{TITLE}\n✅ On: rain, temperature crossing\n🔕 Off: gusts, UV"
    assert title(set(), ["particulates"]).endswith("🔕 Off: nothing") and title({"particulates"}, ["particulates"]).endswith("On: nothing\n🔕 Off: particulates")


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
    notifier = Notifier(bot, state)
    await notifier("It's raining", kind="rain")
    await notifier("Gusts", kind="gusts")
    assert [(c, t.split("\n")[0]) for c, t, _ in bot.sent] == [(1, "It's raining"), (1, "Gusts"), (2, "Gusts")]
    markup = bot.sent[0][2]["reply_markup"]
    assert rows(markup) == [[("🔔 ▸", "al:open:bell:rain")]]   # the alert's own type rides along
    assert bot.sent[0][1] == "It's raining"                                    # no footer: the buttons say how to change it
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


async def test_the_alerts_menu_toggles_a_type_in_place_and_back_returns_to_the_two_buttons(tmp_path):
    bot, state = make_bot(tmp_path)
    q = Query("al:open:alerts", text=f"{TITLE}\n✅ On: ...")
    await press(bot, q)
    drawn = rows(q.edits[0][2])
    assert q.toast is None and drawn[0] == [("🔔 Alerts ▾", "al:close")] and drawn[1][0] == ("rain", "al:off:rain:alerts")
    q = Query("al:off:rain:alerts", text=f"{TITLE}\n✅ On: ...")
    await press(bot, q)
    assert state.muted(1) == {"rain"} and q.toast == "Rain alerts off in this chat" and "🔕 Off: rain" in q.edits[0][1]
    assert rows(q.edits[0][2])[1][0] == (strike("rain"), "al:on:rain:alerts")                                # stays open, now with a cross
    q = Query("al:on:rain:alerts", text=f"{TITLE}\n✅ On: ...")
    await press(bot, q)
    assert state.muted(1) == set() and q.toast == "Rain alerts on in this chat" and rows(q.edits[0][2])[1][0] == ("rain", "al:off:rain:alerts")
    q = Query("al:close", text=f"{TITLE}\n✅ On: ...")
    await press(bot, q)
    assert rows(q.edits[0][2]) == [[("🔔 Alerts ▸", "al:open:alerts"), ("🔊 Sound ▸", "al:open:sound")]]


async def test_the_alerts_message_keeps_its_summary_current_but_an_alert_keeps_its_text(tmp_path):
    bot, state = make_bot(tmp_path)
    q = Query("al:off:uv:alerts", text=f"{TITLE}\n✅ On: ...")
    await press(bot, q)
    assert q.edits[0][0] == "text" and "🔕 Off: UV" in q.edits[0][1]
    q = Query("al:off:gusts:alerts", text="💨 Strong gusts: 55 km/h")
    await press(bot, q)
    assert q.edits[0][0] == "markup"


async def test_noop_unknown_kinds_and_unchanged_markup_are_harmless(tmp_path):
    bot, state = make_bot(tmp_path, sources=("Ecowitt",))
    q = Query("al:noop")
    await press(bot, q)
    assert q.edits == [] and q.toast is None
    for data in ("al:off:pollen:alerts", "al:off:bogus:alerts", "al:weird", "garbage"):          # pollen is not a type here
        q = Query(data)
        await press(bot, q)
        assert q.edits == [] and state.muted(1) == set(), data
    state.set_kind(1, "rain", False)                                                          # something to list, so it edits
    q = Query("al:open:alerts")
    q.fail = BadRequest("Message is not modified: specified new message content and reply markup are exactly the same")
    await press(bot, q)                                                                       # swallowed
    q.fail = BadRequest("Message to edit not found")
    with pytest.raises(BadRequest):
        await press(bot, q)


async def test_in_a_group_only_admins_may_change_alerts(tmp_path):
    bot, state = make_bot(tmp_path)
    q = Query("al:off:rain:alerts", chat_type="supergroup", chat_id=-5)
    await press(bot, q, context=ctx("member"))
    assert q.toast == "Only group admins can change alerts" and state.muted(-5) == set() and q.edits == []
    await press(bot, q, context=ctx("administrator"))
    assert state.muted(-5) == {"rain"} and q.edits
    q = Query("al:off:uv:alerts", chat_type="group", chat_id=-5)
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
    assert replies[0][0].startswith(TITLE) and "On: rain, gusts, UV, temperature crossing, particulates, pollen & asthma, forecast changes" in replies[0][0]
    assert rows(replies[0][1]["reply_markup"])[0] == [("🔔 Alerts ▸", "al:open:alerts"), ("🔊 Sound ▸", "al:open:sound")]
    await bot.on_alerts(update, NS(args=["off"]))
    assert not state.alerts_on(1) and "On: nothing" in replies[1][0]
    await bot.on_alerts(update, NS(args=["on"]))
    assert state.alerts_on(1) and "🔕 Off: nothing" in replies[2][0]


async def test_every_monitors_alert_names_its_type(tmp_path):
    from lib.alerts import ParticulatesMonitor, ForecastMonitor, PollenAndAsthmaMonitor, WeatherMonitor
    from tests.test_alerts import FakePollen, FakeStation, T0, air_reading, gusts, rain, uv
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
    await weather(rain(0, 0, 1, 1), rain(0, 0, 1, 1, *[0] * 13))
    await weather(gusts(10, 12), gusts(10, 12, 60))
    await weather(uv(3, 5, start=T0 + 86400), uv(3, 5, 10, 10, start=T0 + 86400))   # later than the readings above: the monitors share one state
    assert kinds == ["rain", "rain", "gusts", "uv"]
    kinds.clear()
    mask = ParticulatesMonitor(NS(tz=TZ, link=None), state, notify)
    for pm in (80, 80, 20, 20):
        await mask.check(air_reading(pm))
    assert kinds == ["particulates", "particulates"]
    kinds.clear()
    pollen = FakePollen()
    pollen.grass = "High"
    await PollenAndAsthmaMonitor(pollen, state, notify).check()
    assert kinds == ["pollen_and_asthma"]
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


def test_under_an_alert_one_bell_opens_the_alert_toggle_and_then_the_sound_toggle():
    shut = rows(alert_keyboard("uv", set(), set()))
    assert shut == [[("🔔 ▸", "al:open:bell:uv")]]
    on = rows(alert_keyboard("uv", set(), set(), "bell"))                           # both on... the sound is off
    assert on == [[("🔔 ▾", "al:close:uv")], [("Alert", "al:off:uv:bell:uv")], [(strike("Sound"), "al:snd:uv:on:uv")]]
    both = rows(alert_keyboard("uv", set(), {"uv"}, "bell"))
    assert both[2] == [("Sound", "al:snd:uv:off:uv")]
    off = rows(alert_keyboard("uv", {"uv"}, {"uv"}, "bell"))                        # alert off: struck through, and no sound toggle
    assert off == [[("🔔 ▾", "al:close:uv")], [(strike("Alert"), "al:on:uv:bell:uv")]]
    group = rows(alert_keyboard("uv", set(), None, "bell"))                         # a group has no sound
    assert group == [[("🔔 ▾", "al:close:uv")], [("Alert", "al:off:uv:bell:uv")]]


async def test_the_bell_under_an_alert_toggles_the_alert_and_its_sound_and_stays_open(tmp_path):
    bot, state = make_bot(tmp_path)
    q = Query("al:open:bell:uv", text="☀️ UV 10")
    await press(bot, q)
    assert rows(q.edits[0][1]) == [[("🔔 ▾", "al:close:uv")], [("Alert", "al:off:uv:bell:uv")], [(strike("Sound"), "al:snd:uv:on:uv")]]
    q = Query("al:snd:uv:on:uv", text="☀️ UV 10")
    await press(bot, q)
    assert state.loud(1) == {"uv"} and q.toast == "UV alerts now make a sound" and rows(q.edits[0][1])[2] == [("Sound", "al:snd:uv:off:uv")]
    q = Query("al:off:uv:bell:uv", text="☀️ UV 10")                            # alert off: the sound toggle goes, the struck alert stays to turn it back on
    await press(bot, q)
    assert "uv" in state.muted(1) and q.toast == "UV alerts off in this chat"
    assert rows(q.edits[0][1]) == [[("🔔 ▾", "al:close:uv")], [(strike("Alert"), "al:on:uv:bell:uv")]]
    q = Query("al:on:uv:bell:uv", text="☀️ UV 10")
    await press(bot, q)
    assert "uv" not in state.muted(1) and rows(q.edits[0][1])[2] == [("Sound", "al:snd:uv:off:uv")]    # the sound setting was kept
    q = Query("al:close:uv", text="☀️ UV 10")
    await press(bot, q)
    assert rows(q.edits[0][1]) == [[("🔔 ▸", "al:open:bell:uv")]]


async def test_an_unknown_parent_in_a_button_is_ignored(tmp_path):
    bot, state = make_bot(tmp_path)
    q = Query("al:open:alerts:bogus", text="☀️ UV 10")
    await press(bot, q)
    drawn = rows(q.edits[0][1])
    assert drawn[1][0] == ("rain", "al:off:rain:alerts")       # the /alerts menu, not carried on


def test_the_title_stays_plain_words():
    assert title(set(), ["rain"]) == f"{TITLE}\n✅ On: rain\n🔕 Off: nothing"


async def test_the_sound_menu_toggles_every_type_in_private_chats_only_and_back_returns(tmp_path):
    bot, state = make_bot(tmp_path)
    state.set_kind(1, "gusts", False)
    q = Query("al:open:sound")
    await press(bot, q)
    drawn = rows(q.edits[0][1])
    assert drawn[0] == [("🔊 Sound ▾", "al:close")] and drawn[1] == [(strike("rain"), "al:snd:rain:on"), (strike("UV"), "al:snd:uv:on")]
    assert "gusts" not in str(drawn)                                            # an alert that is off has no sound to set
    assert "Alerts" not in str(drawn)                                           # the Alerts menu is hidden while Sound is open
    q = Query("al:snd:rain:on")
    await press(bot, q)
    assert state.loud(1) == {"rain"} and q.toast == "Rain alerts now make a sound" and rows(q.edits[0][1])[1][0] == ("rain", "al:snd:rain:off")
    await press(bot, Query("al:snd:uv:on"))
    assert state.loud(1) == {"rain", "uv"}
    q = Query("al:snd:rain:off")
    await press(bot, q)
    assert state.loud(1) == {"uv"} and q.toast == "Rain alerts no longer make a sound"
    for data in ("al:snd:bogus:on", "al:snd:rain:maybe", "al:snd:rain", "al:snd:all:on"):         # nothing from the data reaches the state
        await press(bot, Query(data))
    assert state.loud(1) == {"uv"}
    q = Query("al:close")
    await press(bot, q)
    assert rows(q.edits[0][1]) == [[("🔔 Alerts ▸", "al:open:alerts"), ("🔊 Sound ▸", "al:open:sound")]]
    q = Query("al:snd:rain:on", chat_type="supergroup", chat_id=-5)              # groups have no sound settings
    state.add_chat(-5, "G")
    await press(bot, q)
    assert state.loud(-5) == set() and q.edits == []
    q = Query("al:open:sound", chat_type="supergroup", chat_id=-5)
    await press(bot, q)
    assert rows(q.edits[0][1]) == [[("🔔 Alerts ▸", "al:open:alerts")]]


async def test_an_alert_has_a_sound_only_in_a_chat_that_turned_it_on_and_groups_get_no_settings_button(tmp_path):
    state, bot = AlertState(tmp_path / "s2.json"), Sent()
    state.add_chat(1, "1")
    state.add_chat(-5, "G")
    state.set_sound(1, "rain", True)
    notifier = Notifier(bot, state)
    await notifier("It's raining", kind="rain")
    await notifier("UV", kind="uv")
    flags = {(c, t.split("\n")[0]): kw["disable_notification"] for c, t, kw in bot.sent}
    assert flags[(1, "It's raining")] is False and flags[(1, "UV")] is True and flags[(-5, "It's raining")] is True
    group = next(kw for c, _, kw in bot.sent if c == -5)
    assert [b[0] for b in rows(group["reply_markup"])[0]] == ["🔔 ▸"]


async def test_a_chat_that_became_a_supergroup_is_followed_not_a_crash(tmp_path):
    from telegram.error import ChatMigrated

    class Moved(Sent):
        async def send_message(self, chat_id, text, **kw):
            if chat_id == -5:
                raise ChatMigrated(-1005)
            await super().send_message(chat_id, text, **kw)

    state, bot = AlertState(tmp_path / "s3.json"), Moved()
    state.add_chat(-5, "G")
    state.set_kind(-5, "uv", False)
    await Notifier(bot, state)("It's raining", kind="rain")
    assert list(state.chats) == [-1005] and state.muted(-1005) == {"uv"} and [c for c, *_ in bot.sent] == [-1005]
    state.add_chat(-7, "H")                                                    # a press under an old alert of a moved group
    bot2, _ = make_bot(tmp_path)
    bot2.state = state
    q = Query("al:open:alerts", chat_type="supergroup", chat_id=-7)
    q.message.reply_markup = keyboard(set(), KINDS)
    q.message.chat.title = "H"

    async def moved(*a, **k):
        raise ChatMigrated(-1007)
    bot2._is_group_admin = moved
    await bot2.on_button(NS(callback_query=q, effective_user=NS(id=7), effective_message=q.message, effective_chat=q.message.chat), ctx())
    assert -1007 in state.chats and -7 not in state.chats and "upgraded" in q.toast


def test_every_alert_type_sent_is_registered_and_each_monitor_declares_what_it_sends():
    import re
    from pathlib import Path
    from lib import alerts
    monitors = [alerts.WeatherMonitor, alerts.ParticulatesMonitor, alerts.ForecastMonitor, alerts.PollenAndAsthmaMonitor, alerts.IrrigationMonitor]
    assert sorted(k for m in monitors for k in m.KINDS) == sorted(LABELS)       # every type has exactly one monitor
    for m in monitors:
        sent = set(re.findall(r'kind="(\w+)"', Path(__import__(m.__module__, fromlist=["x"]).__file__).read_text()))
        assert sent == set(m.KINDS), m.__name__


class SentWithPhotos(Sent):
    def __init__(self, refuse=None):
        super().__init__()
        self.photos, self.refuse = [], refuse

    async def send_photo(self, chat_id, photo, **kw):
        if self.refuse:
            raise self.refuse
        self.photos.append((chat_id, photo, kw))


async def test_an_alert_with_a_picture_is_the_picture_with_the_text_as_its_caption_and_the_buttons(tmp_path):
    state, bot = AlertState(tmp_path / "s.json"), SentWithPhotos()
    state.add_chat(1, "one")
    await Notifier(bot, state)("It's warmer outside", kind="temps", photo=b"png")
    assert bot.sent == [] and len(bot.photos) == 1
    chat, photo, kw = bot.photos[0]
    assert (chat, photo, kw["caption"]) == (1, b"png", "It's warmer outside") and kw["disable_notification"] is True
    assert rows(kw["reply_markup"])[0][0] == ("🔔 ▸", "al:open:bell:temps")                   # the alert's own buttons ride along


async def test_an_alerts_picture_is_uploaded_once_and_the_other_chats_are_sent_telegrams_copy(tmp_path):
    class Keeps(SentWithPhotos):   # Telegram answers with the photo it now holds, in its sizes (the largest last)
        async def send_photo(self, chat_id, photo, **kw):
            await super().send_photo(chat_id, photo, **kw)
            return NS(photo=(NS(file_id="small"), NS(file_id="large")))
    state, bot = AlertState(tmp_path / "s.json"), Keeps()
    for chat in (1, 2, 3):
        state.add_chat(chat, str(chat))
    await Notifier(bot, state)("It's warmer outside", kind="temps", photo=b"png")
    assert [(chat, photo) for chat, photo, _ in bot.photos] == [(1, b"png"), (2, "large"), (3, "large")]


async def test_a_picture_telegram_refuses_falls_back_to_the_text_but_a_missing_chat_is_still_forgotten(tmp_path):
    from telegram.error import BadRequest
    state, bot = AlertState(tmp_path / "s.json"), SentWithPhotos(refuse=BadRequest("Wrong file identifier"))
    state.add_chat(1, "one")
    await Notifier(bot, state)("It's warmer outside", kind="temps", photo=b"png")
    assert bot.photos == [] and [(c, t) for c, t, _ in bot.sent] == [(1, "It's warmer outside")]
    gone = SentWithPhotos(refuse=BadRequest("Chat not found"))
    await Notifier(gone, state)("again", kind="temps", photo=b"png")
    assert gone.sent == [] and 1 not in state.chats
