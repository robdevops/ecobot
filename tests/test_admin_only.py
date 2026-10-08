"""ADMIN_ONLY (who may use the bot), /alerts in groups, /usage, and the checks every button press goes through."""

import random
from types import SimpleNamespace as NS

import pytest
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler

from lib import periods, templates
from lib.admins import Admins
from lib.alerts import AlertState, menu
from lib.bot import MAX_CALLBACK_BYTES, NOT_ADMIN, NOT_ADMIN_TOAST, STALE_BUTTON, Bot
from lib.config import Config, _ids
from lib.irrigation import menu as irrigation_menu
from tests.fakes import TZ
from tests.test_irrigation import Remote

ADMIN, STRANGER, GROUP, OTHER_GROUP = 1, 7, -5, -6
ANONYMOUS = 1087968824           # the user Telegram shows for an admin who posts "as the group"


class TG:
    """The bot's Telegram client: who the admins of each group are, and the status of each member."""
    username, id = "testbot", 99

    def __init__(self, admins=None, statuses=None):
        self.admins = {GROUP: [ADMIN]} if admins is None else admins
        self.statuses = statuses if statuses is not None else {(GROUP, ADMIN): "administrator"}
        self.fetches = []

    async def get_chat_administrators(self, chat_id):
        self.fetches.append(chat_id)
        got = self.admins[chat_id]
        if isinstance(got, Exception):
            raise got
        return [NS(user=NS(id=u, is_bot=False)) for u in got]

    async def get_chat_member(self, chat_id, user_id):
        return NS(status=self.statuses.get((chat_id, user_id), "member"))


class Asked(Bot):
    """A Bot that records the questions it is asked instead of asking a model."""
    asked: list

    async def respond(self, update, context, text, redraw=None):
        self.asked.append(text.strip())


def make(tmp_path, admin_only=True, extra=(), irrigation=None, groups=(GROUP,), tg=None):
    state = AlertState(tmp_path / "s.json")
    for group in groups:
        state.add_chat(group, "Home")
    bot = Asked(NS(tz=TZ, admin_only=admin_only, admin_chat_ids=frozenset(extra)), None, [NS(name="Ecowitt")], state, irrigation)
    bot.asked = []
    return bot, state, tg or TG()


def ctx(tg, *args):
    return NS(bot=tg, args=list(args))


def message(text, user=STRANGER, kind="private", chat=None, sender_chat=None, replied=False):
    sent = []

    async def reply_text(body, **kw):
        sent.append((body, kw))
    chat_id = chat if chat is not None else (user if kind == "private" else GROUP)
    where = NS(type=kind, id=chat_id, title=None if kind == "private" else "Home")
    msg = NS(text=text, chat=where, chat_id=chat_id, reply_text=reply_text, message_thread_id=None, is_topic_message=False, date=None,
             reply_to_message=NS(from_user=NS(id=99), text="hi", caption=None) if replied else None, sender_chat=sender_chat)
    update = NS(effective_message=msg, effective_chat=where, effective_user=NS(id=user, full_name="U", username="u"), callback_query=None)
    return update, sent


class Press:
    """A button press on a message that carries `markup` (as a real one does) with the given data."""

    def __init__(self, data, markup, user=STRANGER, kind="private", chat=None):
        chat_id = chat if chat is not None else (user if kind == "private" else GROUP)
        self.data, self.from_user, self.toast, self.edits = data, NS(id=user, full_name="U", username="u"), "unset", []
        self.message = NS(chat=NS(type=kind, id=chat_id, title=None), chat_id=chat_id, message_id=5, text="🔔 Alerts in this chat",
                          reply_markup=markup)

    async def answer(self, text=None, **kw):
        self.toast = text

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edits.append(("markup", reply_markup))

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.edits.append(("text", text, reply_markup))

    def update(self):
        where = self.message.chat if self.message is not None else None
        return NS(callback_query=self, effective_user=self.from_user, effective_chat=where, effective_message=self.message)


def board(*data):
    return InlineKeyboardMarkup([[InlineKeyboardButton(d, callback_data=d) for d in data]])


# ---------- the settings ----------
def test_admin_only_is_on_unless_switched_off_and_the_listed_ids_are_read_safely(monkeypatch, caplog):
    for k, v in {"TELEGRAM_BOT_TOKEN": "t", "XAI_API_KEY": "k", "ECOWITT_API_KEY": "a", "ECOWITT_APP_KEY": "b"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ADMIN_ONLY", raising=False)
    assert Config.from_env().admin_only is True and Config.from_env().admin_chat_ids == frozenset()
    for value, on in (("", True), ("on", True), ("ON", True), ("1", True), ("off", False), ("Off", False), ("0", False), ("no", False), ("false", False)):
        monkeypatch.setenv("ADMIN_ONLY", value)
        assert Config.from_env().admin_only is on, value
    assert _ids("1, -1001  22,,x, 3.5 -7") == frozenset({1, -1001, 22, -7}) and _ids("") == frozenset()
    assert "ADMIN_CHAT_IDS: ignoring 'x', '3.5'" in caplog.text
    monkeypatch.setenv("ADMIN_CHAT_IDS", "5 -9")
    assert Config.from_env().admin_chat_ids == frozenset({5, -9})


# ---------- who is a known admin ----------
async def test_admins_of_any_known_group_are_allowed_and_the_lists_are_kept_ten_minutes(tmp_path):
    clock, state, tg = [0.0], AlertState(tmp_path / "s.json"), TG({GROUP: [ADMIN, 2], OTHER_GROUP: [3]})
    state.add_chat(GROUP, "Home")
    state.add_chat(OTHER_GROUP, "Away")
    state.add_chat(ADMIN, "private chats are not groups")
    admins = Admins(state, clock=lambda: clock[0])
    assert admins.groups() == [OTHER_GROUP, GROUP]
    assert await admins.allowed(tg, [ADMIN]) and await admins.allowed(tg, [3]) and not await admins.allowed(tg, [STRANGER])
    assert not await admins.allowed(tg, []) and not await admins.allowed(tg, [None])
    asked = len(tg.fetches)
    clock[0] = 599
    await admins.allowed(tg, [STRANGER])
    assert len(tg.fetches) == asked                                                          # kept
    tg.admins[GROUP] = [STRANGER]
    clock[0] = 601
    assert await admins.allowed(tg, [STRANGER]) and not await admins.allowed(tg, [2])        # asked again after ten minutes


async def test_a_failed_lookup_keeps_the_old_list_and_is_retried_soon_without_raising(tmp_path, caplog):
    clock, state, tg = [0.0], AlertState(tmp_path / "s.json"), TG({GROUP: [ADMIN]})
    state.add_chat(GROUP, "Home")
    admins = Admins(state, clock=lambda: clock[0])
    assert await admins.allowed(tg, [ADMIN])
    tg.admins[GROUP] = RuntimeError("bot was kicked")
    clock[0] = 700
    assert await admins.allowed(tg, [ADMIN]) and not await admins.allowed(tg, [STRANGER])   # the last known list stands
    fetches = len(tg.fetches)
    clock[0] = 730
    await admins.allowed(tg, [ADMIN])
    assert len(tg.fetches) == fetches and caplog.text.count("Couldn't list the admins of -5") == 1   # not again for a minute, said once
    clock[0] = 800
    tg.admins[GROUP] = [STRANGER]
    assert await admins.allowed(tg, [STRANGER])                                              # and it recovers
    fresh = Admins(state)
    tg.admins[GROUP] = RuntimeError("down")
    assert not await fresh.allowed(tg, [ADMIN])                                              # nothing known: nobody


async def test_listed_ids_are_added_to_the_known_admins_never_replacing_them(tmp_path):
    state, tg = AlertState(tmp_path / "s.json"), TG({GROUP: [ADMIN], OTHER_GROUP: [3]})
    admins = Admins(state, extra_ids=[STRANGER, OTHER_GROUP])
    assert admins.groups() == [OTHER_GROUP]                                                  # no group known: only the listed group's admins are looked up
    assert await admins.allowed(tg, [STRANGER])                                              # a listed user, with no group known at all
    assert await admins.allowed(tg, [3]) and tg.fetches == [OTHER_GROUP]                     # a listed group's admins
    assert not await admins.allowed(tg, [ADMIN])                                             # this group is not known yet
    state.add_chat(GROUP, "Home")
    assert await admins.allowed(tg, [ADMIN]) and await admins.allowed(tg, [STRANGER])        # known and listed, together
    assert await admins.allowed(tg, [ANONYMOUS, OTHER_GROUP]) and not await admins.allowed(tg, [ANONYMOUS, GROUP])   # an anonymous admin: only a listed group


# ---------- the gate: private chats ----------
@pytest.mark.parametrize("command, args", [("on_start", []), ("on_reset", []), ("on_keyboard", []), ("on_usage", []), ("on_alerts", [])])
async def test_a_stranger_is_refused_every_command_in_a_private_chat_and_is_not_remembered(tmp_path, command, args):
    bot, state, tg = make(tmp_path)
    update, sent = message("/x", user=STRANGER)
    await getattr(bot, command)(update, ctx(tg, *args))
    assert [body for body, _ in sent] == [NOT_ADMIN] and STRANGER not in state.chats
    update, sent = message("/x", user=ADMIN)                                                  # an admin of a known group is let in
    await getattr(bot, command)(update, ctx(tg, *args))
    assert sent and NOT_ADMIN not in [body for body, _ in sent]
    if command in ("on_start", "on_usage", "on_alerts"):
        assert ADMIN in state.chats


async def test_a_stranger_in_a_private_chat_gets_no_answer_and_an_admin_does(tmp_path):
    bot, state, tg = make(tmp_path)
    update, sent = message("how warm is it", user=STRANGER)
    await bot.on_message(update, ctx(tg))
    assert [b for b, _ in sent] == [NOT_ADMIN] and bot.asked == [] and STRANGER not in state.chats
    update, sent = message("how warm is it", user=ADMIN)
    await bot.on_message(update, ctx(tg))
    assert sent == [] and bot.asked == ["how warm is it"] and ADMIN in state.chats


async def test_with_admin_only_off_everyone_is_answered_as_before(tmp_path):
    bot, state, tg = make(tmp_path, admin_only=False)
    update, sent = message("how warm is it", user=STRANGER)
    await bot.on_message(update, ctx(tg))
    assert sent == [] and bot.asked == ["how warm is it"] and STRANGER in state.chats and tg.fetches == []


async def test_a_listed_user_can_use_the_bot_before_it_is_in_any_group(tmp_path):
    bot, state, tg = make(tmp_path, extra=[STRANGER], groups=(), tg=TG({}))
    update, sent = message("hello", user=STRANGER)
    await bot.on_message(update, ctx(tg))
    assert sent == [] and bot.asked == ["hello"] and STRANGER in state.chats
    update, sent = message("hello", user=2)
    await bot.on_message(update, ctx(tg))
    assert [b for b, _ in sent] == [NOT_ADMIN]


# ---------- the gate: groups ----------
async def test_in_a_group_only_a_message_meant_for_the_bot_is_refused_and_ordinary_talk_is_left_alone(tmp_path):
    bot, state, tg = make(tmp_path, groups=())
    update, sent = message("anyone want lunch?", user=STRANGER, kind="supergroup")
    await bot.on_message(update, ctx(tg))
    assert sent == [] and bot.asked == [] and GROUP in state.chats                          # unaddressed: nothing, and the group is now known
    update, sent = message("@testbot how warm is it", user=STRANGER, kind="supergroup")
    await bot.on_message(update, ctx(tg))
    assert [b for b, _ in sent] == [NOT_ADMIN] and bot.asked == []
    update, sent = message("how warm is it", user=STRANGER, kind="supergroup", replied=True)
    await bot.on_message(update, ctx(tg))
    assert [b for b, _ in sent] == [NOT_ADMIN]
    update, sent = message("@testbot how warm is it", user=ADMIN, kind="supergroup")
    await bot.on_message(update, ctx(tg))
    assert sent == [] and bot.asked == ["how warm is it"]


async def test_an_anonymous_admin_gets_in_only_through_a_listed_group_id(tmp_path):
    update, sent = message("@testbot hi", user=ANONYMOUS, kind="supergroup", sender_chat=NS(id=GROUP))
    bot, state, tg = make(tmp_path)
    await bot.on_message(update, ctx(tg))
    assert [b for b, _ in sent] == [NOT_ADMIN] and bot.asked == []                          # no automatic pass
    bot, state, tg = make(tmp_path, extra=[GROUP])
    update, sent = message("@testbot hi", user=ANONYMOUS, kind="supergroup", sender_chat=NS(id=GROUP))
    await bot.on_message(update, ctx(tg))
    assert sent == [] and bot.asked == ["hi"]


# ---------- /alerts in groups, /usage ----------
async def test_anyone_may_open_alerts_in_a_group_but_only_admins_may_change_them(tmp_path):
    bot, state, tg = make(tmp_path)
    update, sent = message("/alerts", user=STRANGER, kind="supergroup")
    await bot.on_alerts(update, ctx(tg))
    assert [b for b, _ in sent] == [menu.title(set(), bot._alert_kinds())] and NOT_ADMIN not in [b for b, _ in sent]
    assert sent[0][1]["reply_markup"] is not None
    for arg in ("off", "on"):
        update, sent = message(f"/alerts {arg}", user=STRANGER, kind="supergroup")
        await bot.on_alerts(update, ctx(tg, arg))
        assert [b for b, _ in sent][0] == "Only group admins can change alerts" and len(sent) == 2 and state.muted(GROUP) == set()
    update, sent = message("/alerts off", user=ADMIN, kind="supergroup")
    await bot.on_alerts(update, ctx(tg, "off"))
    assert "Only group admins" not in sent[0][0] and state.muted(GROUP) == set(menu.LABELS)           # an admin may
    update, sent = message("/alerts", user=STRANGER, kind="private")                                    # a private chat is gated like the rest
    await bot.on_alerts(update, ctx(tg))
    assert [b for b, _ in sent] == [NOT_ADMIN]


async def test_with_admin_only_off_anyone_may_open_alerts_but_telegram_s_own_admins_still_change_them(tmp_path):
    bot, state, tg = make(tmp_path, admin_only=False)
    update, sent = message("/alerts off", user=STRANGER, kind="supergroup")
    await bot.on_alerts(update, ctx(tg, "off"))
    assert sent[0][0] == "Only group admins can change alerts" and state.muted(GROUP) == set()
    update, sent = message("/alerts off", user=ADMIN, kind="supergroup")
    await bot.on_alerts(update, ctx(tg, "off"))
    assert state.muted(GROUP) == set(menu.LABELS)


@pytest.mark.parametrize("kind", ["private", "supergroup"])
async def test_usage_is_the_help_and_alerts_button(tmp_path, kind):
    bot, state, tg = make(tmp_path)
    update, from_button = message(templates.CAPABILITIES, user=ADMIN, kind=kind)
    await bot.on_message(update, ctx(tg)) if kind == "private" else await bot._help_and_alerts(update, ctx(tg))
    update, from_command = message("/usage", user=ADMIN, kind=kind)
    await bot.on_usage(update, ctx(tg))
    shown = lambda sent: [(b, [[(x.text, x.callback_data) for x in row] for row in kw["reply_markup"].inline_keyboard] if kw.get("reply_markup") else None)
                          for b, kw in sent]
    assert len(from_command) == 2 and shown(from_command) == shown(from_button)
    assert from_command[0][0] == templates.capabilities_text(True, False, False, False)
    update, refused = message("/usage", user=STRANGER, kind=kind)
    await bot.on_usage(update, ctx(tg))
    assert [b for b, _ in refused] == [NOT_ADMIN]                                              # gated like the other commands


# ---------- every button comes in through one place ----------
def alert_keyboard(opened="unsub", parent=None):
    return menu.keyboard(set(), menu.available_kinds({"Ecowitt"}), opened, parent)


async def test_a_stranger_is_refused_on_every_family_of_buttons_and_an_admin_is_let_through(tmp_path):
    bot, state, tg = make(tmp_path, irrigation=Remote())
    state.add_chat(ADMIN, "Rob")
    cases = [("al:off:rain:unsub", alert_keyboard()), (f"{periods.PREFIX}7", periods.keyboard(1)),
             ("ir:open:water", irrigation_menu.keyboard(True, True))]
    for data, markup in cases:
        press = Press(data, markup, user=STRANGER)
        await bot.on_button(press.update(), ctx(tg))
        assert press.toast == NOT_ADMIN_TOAST and press.edits == [] and state.muted(STRANGER) == set(), data
        press = Press(data, markup, user=ADMIN)
        await bot.on_button(press.update(), ctx(tg))
        assert press.toast != NOT_ADMIN_TOAST, data
    assert state.muted(ADMIN) == {"rain"}                                                      # the admin's alert press went through
    press = Press("ir:open:water", irrigation_menu.keyboard(True, True), user=STRANGER)
    off = make(tmp_path, admin_only=False, irrigation=Remote())[0]
    await off.on_button(press.update(), ctx(tg))
    assert press.edits                                                                          # ADMIN_ONLY=off: as before


async def test_alert_buttons_in_a_group_stay_admin_only_even_when_admin_only_is_off(tmp_path):
    bot, state, tg = make(tmp_path, admin_only=False)
    press = Press("al:off:rain:unsub", alert_keyboard(), user=STRANGER, kind="supergroup")
    await bot.on_button(press.update(), ctx(tg))
    assert press.toast == "Only group admins can change alerts" and state.muted(GROUP) == set()
    press = Press("al:off:rain:unsub", alert_keyboard(), user=ADMIN, kind="supergroup")
    await bot.on_button(press.update(), ctx(tg))
    assert state.muted(GROUP) == {"rain"}


# ---------- a press is believed only if the message really has that button ----------
def every_keyboard(bot):
    """Every inline keyboard the bot can draw, in every state."""
    kinds = menu.available_kinds({"Ecowitt", "AirGradient", "Pollen", "Forecast", "Irrigation"})
    muted_sets = [set(), {"rain"}, {"rain", "uv", "air"}, set(menu.LABELS)]
    for muted in muted_sets:
        for opened in (None, "sub", "unsub"):
            for parent in (None, *kinds):
                yield menu.keyboard(muted, kinds, opened, parent)
    for battery in (True, False):
        for pause in (True, False):
            for opened in (None, *irrigation_menu.SECTIONS, "bogus"):
                for paused in (True, False):
                    yield irrigation_menu.keyboard(battery, pause, opened, paused)
    for current in (None, *periods.PERIODS):
        yield periods.keyboard(current)


def test_every_button_the_bot_draws_is_short_plain_and_has_exactly_one_handler(tmp_path):
    bot, state, tg = make(tmp_path, irrigation=Remote())
    table, seen = bot.buttons(), set()
    for markup in every_keyboard(bot):
        for row in markup.inline_keyboard:
            for button in row:
                data = button.callback_data
                seen.add(data)
                assert 0 < len(data.encode()) <= MAX_CALLBACK_BYTES and data.isprintable(), data
                assert sum(data.startswith(prefix) for prefix in table) == 1, data
                assert bot._on_keyboard(NS(reply_markup=markup), data)
    assert len(seen) > 60 and {p for p in table} == {"al:", "ir:", periods.PREFIX}
    assert "ir:" not in make(tmp_path, irrigation=None)[0].buttons()


def test_there_is_one_callback_handler_in_the_application_and_no_other_way_in(tmp_path):
    bot, state, tg = make(tmp_path, irrigation=Remote())
    added = []
    bot.register(NS(add_handler=lambda h, group=0: added.append(h), add_error_handler=lambda h: None))
    callbacks = [h for h in added if isinstance(h, CallbackQueryHandler)]
    assert len(callbacks) == 1 and callbacks[0].callback == bot.on_button and callbacks[0].pattern is None   # every press, whatever its data


FORGED = ["ir:water:30", "ir:delay:72h", "ir:delay:cancel", "ir:sw:off", "ir:refresh", "ir:batt:off", "ir:pause:off", "al:off:all:unsub",
          "al:off:rain:unsub", "al:on:all:sub", f"{periods.PREFIX}30", f"{periods.PREFIX}1", "zz:1", "ir:", "al:", "", " ",
          "ir:water:30\n", "ir:water:30\x00", "ir:water:" + "9" * 80, "al:" + "x" * 200, "ir:water:３０", "ir:water:30 ", "ïr:water:30",
          "ir:water:30:extra:colons:::::", "al:off:rain:unsub:rain:more", "../../etc/passwd", "ir:water:$(reboot)", "ir:water:;rm -rf /",
          "{0.__class__}", "%s%s%s%n", "ir:water:-1", "ir:water:1e9", "al:off:__class__:unsub", "pd:__import__('os')"]


class Spy(Remote):
    """The controller, recording everything it is asked to do."""


async def press_through(bot, tg, data, markup, user=ADMIN, kind="private"):
    press = Press(data, markup, user=user, kind=kind)
    await bot.on_button(press.update(), ctx(tg))
    return press


async def test_a_forged_press_does_nothing_whoever_sends_it_and_whichever_family_it_imitates(tmp_path):
    spy = Spy()
    bot, state, tg = make(tmp_path, irrigation=spy)
    unrelated = alert_keyboard("sub")                                                         # a keyboard that has none of the forged data
    before = (dict(state.chats), list(spy.commands))
    for data in FORGED:
        for user in (ADMIN, STRANGER):
            press = await press_through(bot, tg, data, unrelated, user=user)
            assert press.edits == [] and press.toast == STALE_BUTTON, (data, user, press.toast)       # believed or not comes first, for everyone
    assert (state.chats, spy.commands) == before and spy.commands == []                       # nothing changed, nothing sent to the controller


async def test_malformed_presses_are_answered_and_never_raise(tmp_path):
    bot, state, tg = make(tmp_path, irrigation=Remote())
    keyboard = irrigation_menu.keyboard(True, True)
    for data in (None, 5, b"ir:refresh", ["ir:refresh"], "x" * 65, "ir:ré" + "é" * 30, "ir:refresh\n", "\x1b[31mir:refresh", "ir:refresh‮"):
        press = Press(data, keyboard)
        await bot.on_button(press.update(), ctx(tg))
        assert press.toast == STALE_BUTTON and press.edits == [], repr(data)
    no_keyboard = Press("ir:refresh", None)                                                   # a message without a keyboard (or an inaccessible one)
    await bot.on_button(no_keyboard.update(), ctx(tg))
    assert no_keyboard.toast == STALE_BUTTON
    inaccessible = Press("ir:refresh", keyboard)
    inaccessible.message = NS(chat=NS(type="private", id=ADMIN), chat_id=ADMIN, message_id=5, date=0)     # Telegram's InaccessibleMessage: no keyboard to check
    await bot.on_button(inaccessible.update(), ctx(tg))
    assert inaccessible.toast == STALE_BUTTON
    gone = Press("ir:refresh", keyboard)
    gone.message = None
    await bot.on_button(gone.update(), ctx(tg))
    assert gone.toast == STALE_BUTTON
    await bot.on_button(NS(callback_query=None), ctx(tg))                                      # no query at all


async def test_a_prefix_nobody_handles_is_refused_even_when_the_message_really_has_that_button(tmp_path):
    bot, state, tg = make(tmp_path)
    press = Press("zz:do-something", board("zz:do-something"))
    await bot.on_button(press.update(), ctx(tg))
    assert press.toast == STALE_BUTTON and press.edits == []
    press = Press("ir:refresh", irrigation_menu.keyboard(True, True))                         # an irrigation button when there is no controller
    await bot.on_button(press.update(), ctx(tg))
    assert press.toast == STALE_BUTTON


async def test_a_real_press_on_a_button_the_message_carries_still_works(tmp_path):
    bot, state, tg = make(tmp_path, irrigation=Remote())
    state.add_chat(ADMIN, "Rob")
    for data, markup in (("ir:refresh", irrigation_menu.keyboard(True, True)), ("al:off:rain:unsub", alert_keyboard()),
                         ("ir:water:10", irrigation_menu.keyboard(True, True, "water"))):
        press = await press_through(bot, tg, data, markup)
        assert press.toast not in (STALE_BUTTON, NOT_ADMIN_TOAST) and press.edits, data
    assert bot.irrigation.commands == [("water", 10)] and state.muted(ADMIN) == {"rain"}
    stale = await press_through(bot, tg, "ir:water:10", irrigation_menu.keyboard(True, True))   # the Water list is not open on this message
    assert stale.toast == STALE_BUTTON and bot.irrigation.commands == [("water", 10)]


async def test_random_data_never_gets_past_the_entry_point(tmp_path):
    rng = random.Random(7)
    spy = Spy()
    bot, state, tg = make(tmp_path, irrigation=spy)
    boards = [alert_keyboard("sub"), irrigation_menu.keyboard(True, True), periods.keyboard(1)]
    known = {b.callback_data for board_ in boards for row in board_.inline_keyboard for b in row}
    pieces = ["ir", "al", "pd", "on", "off", "water", "delay", "sw", "batt", "pause", "refresh", "open", "close", "all", "sub", "unsub",
              "rain", "uv", "24h", "cancel", "5", "30", "99999", "-1", "", " ", "\n", "é", "💧", "*", "%s", "{}", "\\", ";", "$(x)", "None"]
    before = (dict(state.chats), dict(state.charts))
    for _ in range(3000):
        data = rng.choice(["ir", "al", "pd", ""]) + ":" + ":".join(rng.choice(pieces) for _ in range(rng.randint(0, 4)))
        if data in known:
            continue
        press = await press_through(bot, tg, data, rng.choice(boards), user=rng.choice([ADMIN, STRANGER]))
        assert press.toast in (STALE_BUTTON, NOT_ADMIN_TOAST) and press.edits == [], repr(data)
    assert spy.commands == [] and (dict(state.chats), dict(state.charts)) == before


def test_a_callback_query_from_python_telegram_bot_is_what_the_checks_read():
    """The fields the checks use exist on the library's real types, so the fakes above stand for the real thing."""
    assert {"data", "message", "from_user", "answer"} <= set(dir(CallbackQuery))
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("x", callback_data="ir:refresh")]])
    assert Bot._on_keyboard(NS(reply_markup=markup), "ir:refresh") and not Bot._on_keyboard(NS(reply_markup=markup), "ir:water:5")
    assert not Bot._on_keyboard(NS(), "ir:refresh") and not Bot._on_keyboard(None, "ir:refresh")
