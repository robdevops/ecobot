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
    assert got[1] == [("🌧️ rain", "al:off:rain:unsub"), ("🌦️ rain predicted", "al:off:rain_likely:unsub")]
    assert [text for row in got[1:-1] for text, _ in row] == ["🌧️ rain", "🌦️ rain predicted", "💨 gusts", "🧴 UV", "🌡️ temperature crossing", "😷 particulates"]
    assert got[-1] == [("🔕 Unsubscribe from all", "al:off:all:unsub")]
    got = rows(keyboard({"pollen", "forecast"}, KINDS, "sub"))
    assert got[0][0] == ("▾ ➕ Subscribe", "al:close")
    assert got[1:] == [[("🌼 pollen & asthma", "al:on:pollen:sub"), ("🔄 forecast changes", "al:on:forecast:sub")], [("🔔 Subscribe to all", "al:on:all:sub")]]
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
    assert title({"gusts", "uv"}, KINDS[:5]) == f"{TITLE}\n✅ On: rain, rain predicted, temperature crossing\n🔕 Off: gusts, UV"
    assert title(set(), ["air"]).endswith("🔕 Off: nothing") and title({"air"}, ["air"]).endswith("✅ On: nothing\n🔕 Off: particulates")


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
    assert rows(markup) == [[("➕ Subscribe", "al:open:sub:rain"), ("➖ Unsubscribe", "al:open:unsub:rain"),
                            ("⚙️ Settings ▸", "al:open:set:rain")]]   # the alert's own type rides along
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


async def test_pressing_unsubscribe_then_a_type_updates_the_state_and_redraws_the_open_section(tmp_path):
    bot, state = make_bot(tmp_path)
    q = Query("al:open:unsub")
    await press(bot, q)
    assert q.toast is None and rows(q.edits[0][1])[1][0] == ("🌧️ rain", "al:off:rain:unsub") and q.edits[0][0] == "markup"
    q = Query("al:off:rain:unsub")
    await press(bot, q)
    assert state.muted(1) == {"rain"} and q.toast == "Rain alerts off in this chat"
    drawn = rows(q.edits[0][1])
    assert drawn[0][1] == ("▾ ➖ Unsubscribe", "al:close") and "🌧️ rain" not in [t for row in drawn[1:] for t, _ in row]
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
    assert rows(q.edits[0][1])[-1] == [("🔔 Subscribe to all", "al:on:all:sub")]
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
    assert replies[0][0].startswith(TITLE) and "✅ On: rain, rain predicted, gusts, UV, temperature crossing, particulates, pollen & asthma, forecast changes" in replies[0][0]
    assert rows(replies[0][1]["reply_markup"])[0] == [("➕ Subscribe", "al:open:sub"), ("➖ Unsubscribe", "al:open:unsub"), ("⚙️ Settings ▸", "al:open:set")]
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
    await weather(rain(0, 0, 1, 1), rain(0, 0, 1, 1, *[0] * 13))
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


def test_under_an_alert_the_unsubscribe_list_is_its_type_then_other_which_holds_the_rest_and_all():
    got = rows(keyboard({"pollen", "forecast"}, KINDS, "unsub", parent="uv"))
    assert got == [[("➕ Subscribe", "al:open:sub:uv"), ("▾ ➖ Unsubscribe", "al:close:uv")],
                   [("● 🧴 UV", "al:off:uv:unsub:uv")], [("Other ▸", "al:open:other:uv")]]
    other = rows(keyboard({"pollen", "forecast"}, KINDS, "other", parent="uv"))
    assert other[1:3] == [[("● 🧴 UV", "al:off:uv:other:uv")], [("▾ Other", "al:open:unsub:uv")]]
    listed = [d for r in other[3:-1] for _, d in r]
    assert listed and all(d.startswith("al:off:") and d.endswith(":other:uv") and ":uv:" not in d for d in listed)   # the rest, not its own type
    assert other[-1] == [("🔕 Unsubscribe from all", "al:off:all:other:uv")]
    only = rows(keyboard(set(KINDS) - {"uv"}, KINDS, "unsub", parent="uv"))              # the alert's type is the only one on: no Other
    assert only[1:] == [[("● 🧴 UV", "al:off:uv:unsub:uv")]]
    assert rows(keyboard(set(KINDS) - {"uv"}, KINDS, "other", parent="uv")) == only       # and an open Other degrades to the plain list
    gone = rows(keyboard({"uv", "gusts"}, KINDS, "unsub", parent="uv"))                  # its type is already off: a plain list of the rest, with "all"
    assert "Other" not in str(gone) and gone[-1] == [("🔕 Unsubscribe from all", "al:off:all:unsub:uv")]
    assert len(rows(keyboard(set(KINDS), KINDS, "unsub", parent="uv"))) == 1             # nothing is on at all: nothing listed
    sub = rows(keyboard({"pollen", "forecast"}, KINDS, "sub", parent="uv"))              # Subscribe is the types that are off, as ever
    assert [t for row in sub[1:-1] for t, _ in row] == ["🌼 pollen & asthma", "🔄 forecast changes"] and sub[-1][0][0] == "🔔 Subscribe to all"
    assert rows(keyboard(set(), KINDS, "unsub"))[1][0][0] == "🌧️ rain"                      # the /alerts message has no parent: every type that is on


def test_the_all_button_is_only_there_when_the_list_has_more_than_one_item():
    assert not any("all" in data for row in rows(keyboard({"uv", "gusts", "rain", "rain_likely", "temps", "air", "pollen"}, KINDS, "unsub"))
                   for _, data in row)                                               # one type on (forecast changes): no "Unsubscribe from all"
    assert not any("all" in data for row in rows(keyboard({"uv"}, KINDS, "sub")) for _, data in row)       # one type off: no "Subscribe to all"
    assert rows(keyboard({"uv", "gusts"}, KINDS, "sub"))[-1][0][0] == "🔔 Subscribe to all"
    assert rows(keyboard(set(), KINDS, "unsub"))[-1][0][0] == "🔕 Unsubscribe from all"


async def test_the_alerts_own_type_survives_opening_unsubscribing_and_closing_the_menu_under_it(tmp_path):
    bot, state = make_bot(tmp_path)
    q = Query("al:open:unsub:uv", text="☀️ UV 10")
    await press(bot, q)
    drawn = rows(q.edits[0][1])
    assert drawn[0][1] == ("▾ ➖ Unsubscribe", "al:close:uv") and drawn[1:] == [
        [("● 🧴 UV", "al:off:uv:unsub:uv")], [("Other ▸", "al:open:other:uv")]]
    q = Query("al:open:other:uv", text="☀️ UV 10")
    await press(bot, q)
    assert rows(q.edits[0][1])[-1] == [("🔕 Unsubscribe from all", "al:off:all:other:uv")]
    q = Query("al:off:uv:unsub:uv", text="☀️ UV 10")                           # unsubscribing from this type leaves a plain list of the rest
    await press(bot, q)
    assert "uv" in state.muted(1) and q.toast == "UV alerts off in this chat"
    assert "Other" not in str(rows(q.edits[0][1])) and rows(q.edits[0][1])[-1] == [("🔕 Unsubscribe from all", "al:off:all:unsub:uv")]
    q = Query("al:off:all:unsub:uv", text="☀️ UV 10")                          # and "all" turns the rest off, closing the empty section
    await press(bot, q)
    assert rows(q.edits[0][1]) == [[("➕ Subscribe", "al:open:sub:uv"), ("➖ Unsubscribe", "al:open:unsub:uv"),
                                   ("⚙️ Settings ▸", "al:open:set:uv")]]
    q = Query("al:open:unsub:uv", text="☀️ UV 10")                             # nothing is on now: a toast, no menu
    await press(bot, q)
    assert q.toast == "No alerts are on" and not q.edits


async def test_an_unknown_parent_in_a_button_is_ignored(tmp_path):
    bot, state = make_bot(tmp_path)
    q = Query("al:open:unsub:bogus", text="☀️ UV 10")
    await press(bot, q)
    drawn = rows(q.edits[0][1])
    assert [t for row in drawn[1:] for t, _ in row][0] == "🌧️ rain" and not any(t.startswith("●") for row in drawn for t, _ in row)
    assert drawn[0][0][1] == "al:open:sub"                                       # and it is not carried on


def test_every_alert_type_has_its_emoji_and_every_button_in_the_lists_starts_with_it():
    from lib.alerts.menu import EMOJI, label
    assert set(EMOJI) == set(LABELS)                                                     # a new type cannot be added without one
    assert len(set(EMOJI.values())) == len(EMOJI) and all(not e.isascii() for e in EMOJI.values())   # all different, all emoji
    assert label("rain") == "🌧️ rain" and label("irrigation") == "🪫 irrigation battery" and label("irrigation_pause") == "☔ irrigation pause"
    everything = list(LABELS)
    for section in ("sub", "unsub"):
        for parent in (None, "uv"):
            drawn = rows(keyboard(set(LABELS) if section == "sub" else set(), everything, section, parent))
            texts = [t for row in drawn[1:] for t, _ in row]
            texts = [t for t in texts if "Other" not in t]                                    # (the Other menu is a word, not an alert type)
            assert texts and all(not t.removeprefix("● ")[0].isascii() for t in texts), texts      # every button under it starts with an emoji
    assert title(set(), ["rain"]) == f"{TITLE}\n✅ On: rain\n🔕 Off: nothing"                # the on/off summary and the toasts stay plain words


async def test_settings_hold_enable_and_disable_sub_menus_of_notification_sounds_in_private_chats_only(tmp_path):
    bot, state = make_bot(tmp_path)
    q = Query("al:open:set")                                                    # /alerts: Settings opens the two sub-menus (▸ marks a menu)
    await press(bot, q)
    drawn = rows(q.edits[0][1])
    assert drawn[0][2] == ("▾ ⚙️ Settings", "al:close") and drawn[1:] == [[("🔔 Enable notification sounds ▸", "al:open:snd_on")]]
    q = Query("al:open:snd_on")                                                 # only the types with the sound off, as buttons
    await press(bot, q)
    drawn = rows(q.edits[0][1])
    assert drawn[1] == [("▾ 🔔 Enable notification sounds", "al:open:set")] and ("🌧️ rain", "al:snd:rain:on") in [b for r in drawn[2:] for b in r]
    q = Query("al:snd:rain:on")
    await press(bot, q)
    assert state.loud(1) == {"rain"} and "make a sound" in q.toast
    drawn = rows(q.edits[0][1])                                                 # stays in the list, rain gone from it; Disable appears
    assert "al:snd:rain:on" not in [d for r in drawn for _, d in r] and ("🔕 Disable notification sounds ▸", "al:open:snd_off") in [b for r in drawn for b in r]
    q = Query("al:open:snd_off")
    await press(bot, q)
    assert [b for r in rows(q.edits[0][1]) for b in r if b[1].startswith("al:snd")] == [("🌧️ rain", "al:snd:rain:off")]
    await press(bot, Query("al:snd:rain:off"))
    assert state.loud(1) == set()
    q = Query("al:open:snd_on:uv", text="☀️ UV 10")                             # under an alert: its own type, then Other
    await press(bot, q)
    drawn = rows(q.edits[0][1])
    assert drawn[2:] == [[("● 🧴 UV", "al:snd:uv:on:uv")], [("Other ▸", "al:open:snd_on_o:uv")]]
    q = Query("al:open:snd_on_o:uv", text="☀️ UV 10")                           # Other holds the rest
    await press(bot, q)
    drawn = rows(q.edits[0][1])
    assert drawn[3] == [("▾ Other", "al:open:snd_on:uv")] and ("🌧️ rain", "al:snd:rain:on:uv") in [b for r in drawn for b in r]
    assert "al:snd:uv:on:uv" not in [d for r in drawn[4:] for _, d in r]
    q = Query("al:snd:rain:on:uv", text="☀️ UV 10")                             # a press in Other stays in Other
    await press(bot, q)
    assert state.loud(1) == {"rain"} and ("▾ Other", "al:open:snd_on:uv") in [b for r in rows(q.edits[0][1]) for b in r]
    q = Query("al:snd:uv:on:uv", text="☀️ UV 10")                               # a press on its own type stays on its level
    await press(bot, q)
    assert state.loud(1) == {"rain", "uv"} and ("🔕 Disable notification sounds ▸", "al:open:snd_off:uv") in [b for r in rows(q.edits[0][1]) for b in r]
    await press(bot, Query("al:snd:rain:off:uv", text="☀️ UV 10"))
    await press(bot, Query("al:snd:uv:off:uv", text="☀️ UV 10"))
    assert state.loud(1) == set()
    for data in ("al:snd:bogus:on", "al:snd:rain:maybe", "al:snd:rain"):         # nothing from the data reaches the state
        await press(bot, Query(data))
    assert state.loud(1) == set()
    q = Query("al:snd:rain:on", chat_type="supergroup", chat_id=-5)              # groups have no sound settings
    state.add_chat(-5, "G")
    await press(bot, q)
    assert state.loud(-5) == set() and q.edits == []


async def test_an_alert_has_a_sound_only_in_a_chat_that_turned_it_on_and_groups_get_no_settings_button(tmp_path):
    state, bot = AlertState(tmp_path / "s2.json"), Sent()
    state.add_chat(1, "1")
    state.add_chat(-5, "G")
    state.set_sound(1, "rain", True)
    notifier = Notifier(bot, state, KINDS)
    await notifier("It's raining", kind="rain")
    await notifier("UV", kind="uv")
    flags = {(c, t.split("\n")[0]): kw["disable_notification"] for c, t, kw in bot.sent}
    assert flags[(1, "It's raining")] is False and flags[(1, "UV")] is True and flags[(-5, "It's raining")] is True
    group = next(kw for c, _, kw in bot.sent if c == -5)
    assert [b[0] for b in rows(group["reply_markup"])[0]] == ["➕ Subscribe", "➖ Unsubscribe"]


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
    await Notifier(bot, state, KINDS)("It's raining", kind="rain")
    assert list(state.chats) == [-1005] and state.muted(-1005) == {"uv"} and [c for c, *_ in bot.sent] == [-1005]
    state.add_chat(-7, "H")                                                    # a press under an old alert of a moved group
    bot2, _ = make_bot(tmp_path)
    bot2.state = state
    q = Query("al:open:unsub", chat_type="supergroup", chat_id=-7)
    q.message.reply_markup = keyboard(set(), KINDS)
    q.message.chat.title = "H"

    async def moved(*a, **k):
        raise ChatMigrated(-1007)
    bot2._is_group_admin = moved
    await bot2.on_button(NS(callback_query=q, effective_user=NS(id=7), effective_message=q.message, effective_chat=q.message.chat), ctx())
    assert -1007 in state.chats and -7 not in state.chats and "upgraded" in q.toast


async def test_the_settings_list_only_the_subscribed_types(tmp_path):
    bot, state = make_bot(tmp_path)
    state.set_kind(1, "rain", False)
    q = Query("al:open:set")
    await press(bot, q)
    q = Query("al:open:snd_on")
    await press(bot, q)
    listed = [d for r in rows(q.edits[0][1])[1:] for _, d in r]
    assert "al:snd:rain:on" not in listed and "al:snd:uv:on" in listed
    state.set_kind(1, ALL, False)
    q = Query("al:open:set")
    await press(bot, q)
    assert q.toast == "No alerts are on" and q.edits == []


async def test_the_sound_lists_have_an_all_button(tmp_path):
    bot, state = make_bot(tmp_path)
    state.set_kind(1, "gusts", False)
    q = Query("al:open:snd_on")                                                 # /alerts: a flat list with "Enable all" last
    await press(bot, q)
    assert rows(q.edits[0][1])[-1] == [("🔔 Enable all", "al:snd:all:on")]
    q = Query("al:snd:all:on")
    await press(bot, q)
    assert state.loud(1) == {k for k in KINDS if k != "gusts"} and q.toast == "All alerts now make a sound"   # only the types it gets
    assert ("🔕 Disable notification sounds ▸", "al:open:snd_off") in [b for r in rows(q.edits[0][1]) for b in r]
    q = Query("al:open:snd_off")
    await press(bot, q)
    assert rows(q.edits[0][1])[-1] == [("🔕 Disable all", "al:snd:all:off")]
    await press(bot, Query("al:snd:all:off"))
    assert state.loud(1) == set()
    q = Query("al:open:snd_on_o:uv", text="☀️ UV 10")                           # under an alert, "all" sits in Other
    await press(bot, q)
    assert rows(q.edits[0][1])[-1] == [("🔔 Enable all", "al:snd:all:on:uv")]
    await press(bot, Query("al:snd:all:on:uv", text="☀️ UV 10"))
    assert "uv" in state.loud(1) and "rain" in state.loud(1) and "gusts" not in state.loud(1)
