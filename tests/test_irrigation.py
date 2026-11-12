"""The bot's side of the irrigation controller (step 8b: the controller is irrigation-mcp's, and the bot its client at
BOT_IRRIGATION_MCP_URL): the status message and its keyboard, the Irrigation button and every ir: button run against irrigation-mcp in
memory (its real server over the fake controller, tests/served.py), its alerts delivered through the listen, and irrigation-mcp down.
(The controller's own code, without Telegram, is tests/test_irrigation_controller.py.)"""

import asyncio
from types import SimpleNamespace as NS

import httpx
import pytest
from telegram.error import BadRequest

import lib.bot as bot_module
from lib import irrigation_menu as menu
from lib import irrigation_remote, templates
from lib.alerts.menu import available_kinds
from lib.alerts.notify import AlertDelivery, AlertState, Notifier, ReadMark
from lib.bot import Bot
from lib.buttons import struck as strike
from lib.irrigation import IrrigationError, Tuya
from lib.irrigation_remote import RemoteIrrigation
from tests.fakes import TZ, FakeController, tuya_config


@pytest.fixture(autouse=True)
def settle_at_once(monkeypatch):
    """The irrigation buttons wait a couple of seconds before redrawing the message; the tests do not."""
    monkeypatch.setattr(bot_module, "IRRIGATION_SETTLE_SECONDS", 0)


cfg = tuya_config
Remote = FakeController   # (the controller behind irrigation-mcp)


# ---------- the status message and its buttons ----------
def rows(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def test_the_status_always_shows_mode_state_pause_time_and_battery_in_that_order():
    assert menu.status_text({"switch": True, "battery_percentage": 73, "work_state": "auto", "weather_delay": "cancel"}) == (
        "🌱 Irrigation\nmode: auto\nstate: on ✅\npause: not paused\nbattery: 73% 🔋")
    assert menu.status_text({"switch": False, "battery_percentage": 9, "work_state": "idle", "weather_delay": "24h"}) == (
        "🌱 Irrigation\nmode: idle\nstate: off ❌\npause: 24h\nbattery: 9% 🪫")
    assert "🔋" in menu.status_text({"battery_percentage": 10}) and "🪫" in menu.status_text({"battery_percentage": 0})
    assert menu.status_text({}) == "🌱 Irrigation\nmode: unknown\nstate: off ❌\npause: not paused\nbattery: unknown"   # every element, even unread
    full = {"switch": True, "countdown": 600, "weather_delay": "48h", "work_state": "manual", "battery_percentage": 80}
    assert menu.status_text(full, True, False) == (                                          # all of it, with the chat's alerts in brackets
        "🌱 Irrigation\nmode: manual\nstate: on ✅ (10 mins until off)\npause: 48h (alerts: off)\nbattery: 80% 🔋 (alerts: on)")
    assert menu.status_text({**full, "weather_delay": "cancel"}, False, True).splitlines()[3] == "pause: not paused (alerts: on)"
    for countdown, shown in ((600, "10 mins"), (601, "11 mins"), (30, "1 min"), (1, "1 min"), (5400, "90 mins")):    # whole minutes, a started one counts
        assert f"state: on ✅ ({shown} until off)\n" in menu.status_text({**full, "countdown": countdown})
    for hidden in ({"countdown": 0}, {"countdown": None}, {"countdown": -5}, {"switch": False, "countdown": 600}, {"switch": False}):
        assert "until off" not in menu.status_text({**full, **hidden}), hidden               # only while it is on and counting down
    assert menu.status_text({"switch": True}).splitlines()[2] == "state: on ✅"                 # on, but no countdown: just on
    assert "(alerts" not in menu.status_text(full)                                           # no chat given: no brackets
    assert menu.status_text({"switch": False}, True).endswith("battery: unknown (alerts: on)") and "pause: not paused\n" in menu.status_text({}, True)


def test_the_buttons_open_one_section_at_a_time_and_every_callback_is_short():
    collapsed = [[("💧 Water ▸", "ir:open:water"), ("⏸ Pause ▸", "ir:open:delay")], [("🔔 Alerts ▸", "ir:open:alerts"), ("🔄", "ir:refresh")]]
    assert rows(menu.keyboard(False, False)) == collapsed == rows(menu.keyboard(True, True))              # the alert states show only once Alerts is open
    water = rows(menu.keyboard(True, True, "water"))
    assert water[0][0] == ("💧 Water ▾", "ir:close") and water[-1] == [(f"{m} min", f"ir:water:{m}") for m in (5, 10, 20, 30)]
    assert "ir:sw:off" not in [data for row in water for _, data in row]                    # Off is not in the Water list: it is a top-level row while on
    assert rows(menu.keyboard(True, True, running=True)) == collapsed + [[("Water off ❌", "ir:sw:off")]]   # only while the valve is on
    assert rows(menu.keyboard(True, True, paused=True, running=True))[-1] == [("Water off ❌", "ir:sw:off")]
    assert "ir:sw:on" not in [data for row in water for _, data in row]
    assert [len(r) for r in water] == [1, 4] and rows(menu.keyboard(True, True, "delay"))[0] == [("⏸ Pause ▾", "ir:close")]   # an open menu hides the other buttons
    assert rows(menu.keyboard(True, True, "water", running=True))[1] == [("Water off ❌", "ir:sw:off")]   # but Off stays
    assert rows(menu.keyboard(True, True, "delay"))[-1] == [("24h", "ir:delay:24h"), ("48h", "ir:delay:48h"), ("72h", "ir:delay:72h")]
    paused = rows(menu.keyboard(True, True, paused=True))                                      # paused: the button unpauses, in one press
    assert paused[0] == [("▶️ Unpause", "ir:delay:cancel")] and paused[1] == collapsed[1]   # no Water button while paused
    assert rows(menu.keyboard(True, True, "delay", paused=True)) == paused                      # no list of periods while paused
    assert rows(menu.keyboard(True, True, "water", paused=True)) == paused                     # and no minutes or Off either
    assert menu.SECTIONS == ("water", "delay", "alerts") and len(rows(menu.keyboard(True, True, "switch"))) == 2   # no Switch menu any more
    for battery, pause, battery_button, pause_button in (
            (False, False, (strike("battery alerts"), "ir:batt:on"), (strike("pause alerts"), "ir:pause:on")),
            (True, False, ("battery alerts", "ir:batt:off"), (strike("pause alerts"), "ir:pause:on")),
            (False, True, (strike("battery alerts"), "ir:batt:on"), ("pause alerts", "ir:pause:off")),
            (True, True, ("battery alerts", "ir:batt:off"), ("pause alerts", "ir:pause:off"))):
        alerts = rows(menu.keyboard(battery, pause, "alerts"))
        assert alerts == [[("🔔 Alerts ▾", "ir:close")], [battery_button], [pause_button]]   # the open menu's own button is its heading; the rest are hidden                                    # each names the action it takes
    for section in (None, *menu.SECTIONS):
        for battery in (True, False):
            for pause in (True, False):
                assert all(len(data.encode()) <= 64 for row in rows(menu.keyboard(battery, pause, section)) for _, data in row)


def test_the_irrigation_button_takes_wind_s_place_only_when_configured_and_changes_the_keyboard_version():
    plain, with_irrigation = templates.keyboard().keyboard, templates.keyboard(True).keyboard
    assert [b.text for row in plain for b in row][-1] == "\U0001f4a8 Wind" and templates.IRRIGATION not in [b.text for row in plain for b in row]
    labels = [b.text for row in with_irrigation for b in row]
    assert labels[-1] == templates.IRRIGATION and "\U0001f4a8 Wind" not in labels and len(labels) == 9 and with_irrigation[:2] == plain[:2]
    assert templates.version() != templates.version(True)
    assert templates.sentence(templates.IRRIGATION) is None                              # not a question for the model


def test_the_alert_list_gets_irrigation_battery_only_when_there_is_a_controller():
    assert available_kinds({"Ecowitt", "Irrigation"})[-2:] == ["irrigation_battery", "irrigation_pause"]
    assert "irrigation_battery" not in available_kinds({"Ecowitt"}) and "irrigation_pause" not in available_kinds({"Ecowitt"})
    from lib.alerts.menu import LABELS, keyboard
    assert LABELS["irrigation_battery"] == "irrigation battery" and LABELS["irrigation_pause"] == "irrigation pause"
    listed = rows(keyboard(set(), ["rain", "irrigation_battery"], "alerts"))
    assert ("irrigation battery", "al:off:irrigation_battery:alerts") in [b for row in listed for b in row]


# ---------- the buttons in the bot ----------
class Query:
    def __init__(self, data, chat_id=1, chat_type="private", user=7):
        self.data, self.message = data, NS(chat=NS(type=chat_type, id=chat_id), chat_id=chat_id, text="")
        self.from_user, self.toast, self.edits, self.fail = NS(id=user), "unset", [], None

    async def answer(self, text=None, **kw):
        self.toast = text

    async def edit_message_reply_markup(self, reply_markup=None):
        if self.fail:
            raise self.fail
        self.edits.append(("markup", reply_markup))

    async def edit_message_text(self, text, reply_markup=None, entities=None):
        if self.fail:
            raise self.fail
        self.edits.append(("text", text, reply_markup) + ((entities,) if entities else ()))


WEATHER = SERVE_IRRIGATION = None


@pytest.fixture(autouse=True)
async def ecobot_mcp(serve, serve_irrigation):
    """ecobot-mcp in memory with the weather station, the bot's client of it connected: what the bots here run over; and the way to
    irrigation-mcp in memory (serve_irrigation)."""
    global WEATHER, SERVE_IRRIGATION
    WEATHER, SERVE_IRRIGATION = await serve(tools=[], sources=["Ecowitt"]), serve_irrigation
    yield WEATHER
    WEATHER = SERVE_IRRIGATION = None


async def irrigation_bot(tmp_path, device=None):
    """A bot whose irrigation is irrigation-mcp in memory over `device` (default: the fake controller): (the bot, its state, the
    controller behind irrigation-mcp)."""
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "Rob")
    irrigation = await SERVE_IRRIGATION(tmp_path, device or Remote())
    return Bot(NS(tz=TZ), None, state, irrigation, service=WEATHER), state, irrigation.server.device


def watched(bot):
    """When irrigation-mcp's watchdog closes the valve (None: no run watched)."""
    return bot.irrigation.server.watchdog.off_at


async def press(bot, data, **kw):
    q = Query(data, **kw)
    await bot.on_irrigation_button(NS(callback_query=q), NS())     # no chat-member lookup is available: none is made
    return q


async def test_the_button_sends_the_status_with_its_buttons_and_a_failure_says_so(tmp_path):
    bot, state, device = await irrigation_bot(tmp_path)
    sent = []

    async def reply_text(body, **kw):
        sent.append((body, kw))
    msg = NS(text=templates.IRRIGATION, chat=NS(type="private", id=1, title=None), chat_id=1, reply_text=reply_text, reply_to_message=None,
             message_thread_id=None, is_topic_message=False)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(id=7, full_name="Rob", username="rob"))
    await bot.on_message(update, NS(bot=NS(username="b", id=99)))                          # no model is involved (the agent is None)
    assert sent[0][0].startswith("🌱 Irrigation\nmode: idle\nstate: off ❌\npause: not paused (alerts: on)\nbattery: 100% 🔋 (alerts: on)")
    assert rows(sent[0][1]["reply_markup"])[0][0] == ("💧 Water ▸", "ir:open:water") and rows(sent[0][1]["reply_markup"])[1][0] == ("🔔 Alerts ▸", "ir:open:alerts")
    device.down = True
    await bot.on_message(update, NS(bot=NS(username="b", id=99)))
    assert sent[1][0] == "Sorry, I couldn't reach the irrigation controller." and len(sent) == 2


async def test_an_odd_reply_from_the_controller_is_a_toast_not_a_press_left_hanging(tmp_path):
    def odd(request):
        return httpx.Response(200, json=["not", "a", "reply"])
    bot, state, device = await irrigation_bot(tmp_path, Tuya(cfg(tmp_path), transport=httpx.MockTransport(odd)))   # (irrigation-mcp's)
    q = await press(bot, "ir:refresh")
    assert q.toast == "Couldn't reach the irrigation controller" and q.edits == []
    await device.close()


async def test_a_run_whose_reply_is_lost_is_still_watched(tmp_path):
    class Lost(Remote):
        async def water(self, minutes):
            await super().water(minutes)                                 # the valve opened
            raise IrrigationError("Tuya request failed (ReadTimeout)")   # but the reply never came
    bot, state, device = await irrigation_bot(tmp_path, Lost())
    q = await press(bot, "ir:water:10")
    assert q.toast == "Couldn't reach the irrigation controller" and watched(bot) is not None


async def test_opening_and_closing_a_section_only_changes_the_buttons(tmp_path):
    bot, _, device = await irrigation_bot(tmp_path)
    q = await press(bot, "ir:open:water")
    assert q.edits[0][0] == "markup" and rows(q.edits[0][1])[-1][0] == ("5 min", "ir:water:5") and device.commands == []
    q = await press(bot, "ir:close")
    assert len(rows(q.edits[0][1])) == 2
    for gone in ("ir:open:bogus", "ir:open:switch"):                                     # an unknown section, or an old message's Switch, just closes
        q = await press(bot, gone)
        assert len(rows(q.edits[0][1])) == 2


async def test_watering_a_delay_and_the_switch_act_on_the_controller_and_refresh_the_message(tmp_path):
    bot, state, device = await irrigation_bot(tmp_path)
    q = await press(bot, "ir:water:10")
    assert device.commands == [("water", 10)] and q.toast == "Watering for 10 minutes" and watched(bot)
    assert q.edits[0][0] == "text" and "state: on ✅" in q.edits[0][1] and "mode: manual" in q.edits[0][1]
    assert "state: on ✅ (10 mins until off)" in q.edits[0][1]
    q = await press(bot, "ir:sw:off")
    assert device.commands[-1] == ("switch", False) and q.toast == "Switched off" and watched(bot) is None and "state: off ❌" in q.edits[0][1]
    q = await press(bot, "ir:delay:72h")
    assert device.commands[-1] == ("delay", "72h") and q.toast == "Paused 72h"
    q = await press(bot, "ir:delay:cancel")
    assert q.toast == "Unpaused" and device.commands[-1] == ("delay", "cancel")                  # unpause is the device's own "cancel"
    n = len(device.commands)
    for data in ("ir:water:7", "ir:water:x", "ir:delay:1h", "ir:sw:on", "ir:sw:maybe", "ir:what", "ir:", "garbage"):   # nothing the buttons offer (On is gone)
        q = await press(bot, data)
        assert q.edits == [] and q.toast is None, data
    assert len(device.commands) == n


async def test_the_alerts_menu_toggles_each_alert_type_for_this_chat_alone_and_stays_open(tmp_path):
    bot, state, _ = await irrigation_bot(tmp_path)
    q = await press(bot, "ir:open:alerts")
    assert q.edits[0][0] == "markup" and [r[0][0] for r in rows(q.edits[0][1])[1:]] == ["battery alerts", "pause alerts"]
    q = await press(bot, "ir:batt:off")
    assert state.muted(1) == {"irrigation_battery"} and q.toast == "Irrigation battery alerts off in this chat"
    assert rows(q.edits[0][2])[1:] == [[(strike("battery alerts"), "ir:batt:on")], [("pause alerts", "ir:pause:off")]]   # still open
    assert q.edits[0][1].endswith("pause: not paused (alerts: on)\nbattery: 100% 🔋 (alerts: off)")        # the status text follows
    q = await press(bot, "ir:pause:off")
    assert state.muted(1) == {"irrigation_battery", "irrigation_pause"} and q.toast == "Irrigation pause alerts off in this chat"
    assert q.edits[0][1].endswith("pause: not paused (alerts: off)\nbattery: 100% 🔋 (alerts: off)") and rows(q.edits[0][2])[2] == [(strike("pause alerts"), "ir:pause:on")]
    q = await press(bot, "ir:batt:on")
    assert state.muted(1) == {"irrigation_pause"} and q.toast == "Irrigation battery alerts on in this chat"           # the other type is untouched
    q = await press(bot, "ir:pause:on")
    assert state.muted(1) == set() and state.alert_chats("irrigation_battery") == [1] == state.alert_chats("irrigation_pause")
    for kind, prefix in (("irrigation_battery", "batt"), ("irrigation_pause", "pause")):               # a button shows the state, pressing it flips it
        for _ in range(2):
            alerts = rows(menu.keyboard(state.subscribed(1, "irrigation_battery"), state.subscribed(1, "irrigation_pause"), "alerts"))
            text, data = next(b for row in alerts[1:] for b in row if b[1].startswith(f"ir:{prefix}:"))
            await press(bot, data)
            assert state.subscribed(1, kind) is (text != text.replace("\u0336", "")), (text, data)
    other = await press(bot, "ir:water:5")
    assert len(rows(other.edits[0][2])) == 3                                                    # any other action redraws collapsed (3 rows: the valve is on)
    other = await press(bot, "ir:refresh")
    assert len(rows(other.edits[0][2])) == 3


async def test_a_controller_that_cannot_be_reached_gives_a_toast_not_a_traceback_and_an_unchanged_message_is_fine(tmp_path):
    bot, state, device = await irrigation_bot(tmp_path)
    device.down = True
    q = await press(bot, "ir:delay:24h")
    assert q.toast == "Couldn't reach the irrigation controller" and q.edits == []
    device.down = False
    q = Query("ir:delay:24h")
    q.fail = BadRequest("Message is not modified: specified new message content and reply markup are exactly the same")
    await bot.on_irrigation_button(NS(callback_query=q), NS())                            # swallowed
    q.fail = BadRequest("Message to edit not found")
    with pytest.raises(BadRequest):
        await bot.on_irrigation_button(NS(callback_query=q), NS())
    plain = Bot(NS(tz=TZ), None, AlertState(tmp_path / "t.json"), service=WEATHER)       # no controller configured: nothing happens
    q = await press(plain, "ir:delay:24h")
    assert q.edits == [] and plain.irrigation is None


async def test_the_handler_is_registered_and_the_alert_list_includes_irrigation_only_with_a_controller(tmp_path):
    bot, state, _ = await irrigation_bot(tmp_path)
    added = []
    app = NS(add_handler=lambda h, group=0: added.append(h), add_error_handler=lambda h: None)
    bot.register(app)
    plain = Bot(NS(tz=TZ), None, AlertState(tmp_path / "t.json"), service=WEATHER)
    added_plain = []
    plain.register(NS(add_handler=lambda h, group=0: added_plain.append(h), add_error_handler=lambda h: None))
    assert len(added) == len(added_plain)                                                    # one entry point for every button, with or without a controller
    assert "ir:" in bot.buttons() and "ir:" not in plain.buttons() and {"al:", "pd:"} <= set(plain.buttons())
    assert "irrigation_battery" in bot._alert_kinds() and "irrigation_battery" not in plain._alert_kinds()
    assert bot.keyboard_version == templates.version(True) != plain.keyboard_version == templates.version()


@pytest.mark.parametrize("text, yes", [
    ("irrigation", True), ("Irrigation", True), ("water", True), ("tap", True), ("sprinkler", True), ("sprinklers on?", True),
    ("turn the sprinkler on", True), ("is the tap on", True), ("watering", True), ("irrigate the garden", True), ("🌱 Irrigation", True),
    ("how much water did the rain add this week", False),                       # a long question is the model's
    ("tapestry", False), ("waterfall", False), ("taping", False), ("tapped out", False), ("rain", False), ("", False), ("   ", False),
    ("temperature chart 1d", False), ("Weather", False)])
def test_short_messages_with_a_keyword_ask_for_the_controller(text, yes):
    assert menu.asked(text) is yes


def private_message(text):
    sent = []

    async def reply_text(body, **kw):
        sent.append((body, kw))
    msg = NS(text=text, chat=NS(type="private", id=1, title=None), chat_id=1, reply_text=reply_text, reply_to_message=None,
             message_thread_id=None, is_topic_message=False)
    return NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(id=7, full_name="Rob", username="rob")), sent


def group_message(text, replied=False):
    sent = []

    async def reply_text(body, **kw):
        sent.append((body, kw))
    reply = NS(from_user=NS(id=99)) if replied else None
    msg = NS(text=text, chat=NS(type="supergroup", id=-5, title="Home"), chat_id=-5, reply_text=reply_text, reply_to_message=reply,
             message_thread_id=None, is_topic_message=False)
    return NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(id=7, full_name="Rob", username="rob")), sent


class NoModel:
    async def run(self, *a, **k):
        raise AssertionError("the model was asked")


async def test_typing_irrigation_does_what_the_button_does_in_private_chats_and_when_addressed_in_groups(tmp_path):
    bot, state, device = await irrigation_bot(tmp_path)
    bot.agent = NoModel()
    ctx = NS(bot=NS(username="testbot", id=99))
    for text in ("irrigation", "water", "turn the sprinkler on", templates.IRRIGATION):
        update, sent = private_message(text)
        await bot.on_message(update, ctx)
        assert sent[0][0].startswith("🌱 Irrigation\nmode: idle\nstate: off ❌") and sent[0][1]["reply_markup"], text
    update, sent = group_message("@testbot irrigation")
    await bot.on_message(update, ctx)
    assert sent[0][0].startswith("🌱 Irrigation") and rows(sent[0][1]["reply_markup"])[0][0] == ("💧 Water ▸", "ir:open:water")
    update, sent = group_message("water", replied=True)                                   # a reply to the bot counts as addressing it
    await bot.on_message(update, NS(bot=NS(username="testbot", id=99)))
    assert sent == [] or sent[0][0].startswith("🌱 Irrigation")
    update, sent = group_message("irrigation please")                                     # not addressed: ignored, as every group message is
    await bot.on_message(update, ctx)
    assert sent == []


async def test_other_questions_and_a_bot_without_a_controller_still_go_to_the_model(tmp_path):
    asked = []

    class Agent:
        async def run(self, working, *a, **k):
            asked.append(working[-1]["content"])
            return "ok"
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "Rob")
    async def quiet(*a, **k):
        pass
    ctx = NS(bot=NS(username="testbot", id=99, send_chat_action=quiet, send_message_draft=quiet))   # the model is asked: "typing..." and a draft go first
    bot = Bot(NS(tz=TZ), Agent(), state, await SERVE_IRRIGATION(tmp_path), service=WEATHER)
    update, sent = private_message("how much water did the rain add this week")
    await bot.on_message(update, ctx)
    assert asked == ["how much water did the rain add this week"]
    plain = Bot(NS(tz=TZ), Agent(), AlertState(tmp_path / "t.json"), service=WEATHER)
    update, sent = private_message("irrigation")
    await plain.on_message(update, ctx)
    assert asked[-1] == "irrigation"                                                       # nothing to show: the model answers as before


async def test_the_message_is_redrawn_a_couple_of_seconds_after_a_command_and_shows_the_chat_s_battery_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_module, "IRRIGATION_SETTLE_SECONDS", 3)
    bot, state, device = await irrigation_bot(tmp_path)
    events = []

    async def sleep(seconds):
        events.append(("sleep", seconds))
    original_status = bot.irrigation.status

    async def status():                                                                             # (the bot's reads, over irrigation-mcp)
        events.append(("status",))
        return await original_status()
    bot.irrigation.status = status
    monkeypatch.setattr(bot_module.asyncio, "sleep", sleep)
    q = await press(bot, "ir:water:5")
    assert events == [("status",), ("sleep", 3), ("status",)] and q.toast == "Watering for 5 minutes"   # a look at the pause, the command, a wait, the fresh state
    for data in ("ir:delay:24h", "ir:delay:cancel", "ir:sw:off", "ir:batt:off", "ir:batt:on"):   # every action waits the same 3 seconds
        events.clear()
        await press(bot, data)
        assert events == [("sleep", 3), ("status",)], data
    assert "(alerts: on)" in q.edits[0][1]
    events.clear()
    q = await press(bot, "ir:open:delay")                                                           # opening a list changes nothing: no wait
    assert events == [] and q.edits[0][0] == "markup"
    q = await press(bot, "ir:batt:off")                                                             # an action too, so the same wait
    assert events == [("sleep", 3), ("status",)] and "(alerts: off)" in q.edits[0][1]
    events.clear()
    device.down = True
    q = await press(bot, "ir:delay:24h")                                                            # a failed command: no wait, no redraw
    assert events == [] and q.toast == "Couldn't reach the irrigation controller" and q.edits == []


async def test_a_status_that_cannot_be_read_after_a_command_leaves_the_message_alone(tmp_path):
    bot, state, device = await irrigation_bot(tmp_path)

    async def delay(duration):
        device.commands.append(("delay", duration))
        device.down = True                                                                          # the command went through, then the line dropped
    device.delay = delay
    q = await press(bot, "ir:delay:48h")
    assert device.commands == [("delay", "48h")] and q.toast == "Paused 48h" and q.edits == []



async def until(check, seconds: float = 10.0, what: str = "it"):
    deadline = asyncio.get_running_loop().time() + seconds
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def test_an_irrigation_alert_reaches_every_subscribed_chat_through_the_listen_once_with_the_usual_menu(tmp_path):
    """irrigation-mcp's daily check appends to its own alert log (irrigation://alerts); the bot hears it through its listen and delivers
    it as it delivers ecobot-mcp's: to the chats subscribed to its kind, with the alert's menu, its last id kept apart in bot_state.json,
    nothing sent twice (a reconnect reads once; a restart resumes after it)."""
    state, delivered = AlertState(tmp_path / "s.json"), []
    for chat in (1, 2, 3, -7):
        state.add_chat(chat, str(chat))
    state.set_kind(2, "irrigation_pause", False)                       # this chat turned the pause alerts off
    state.set_kind(3, "irrigation_battery", False)                     # ... and this one only the battery alerts: it still gets the pause

    class Telegram:
        async def send_message(self, chat_id, text, **kw):
            delivered.append((chat_id, text, kw))
    irrigation = await SERVE_IRRIGATION(tmp_path)
    mark = ReadMark(state, irrigation_remote.NAME)

    async def delivering(state, mark):
        delivery = AlertDelivery(irrigation.alerts, Notifier(Telegram(), state), state, TZ, mark=mark)
        task = asyncio.create_task(delivery.run())
        await until(lambda: delivery.begun and mark.read is not None, what="the delivery to begin")
        return task
    task = await delivering(state, mark)
    await irrigation.server.notify("☔ Irrigation paused for 24 hours: 90% chance of at least 1 mm today.", kind="irrigation_pause")
    await until(lambda: len(delivered) == 3, what="the pause alert")
    assert sorted(c for c, _, _ in delivered) == [-7, 1, 3] and all(text.startswith("☔ Irrigation paused") for _, text, _ in delivered)
    assert rows(delivered[0][2]["reply_markup"]) == [[("🔔 ▸", "al:open:bell:irrigation_pause")]]
    await irrigation.server.notify("🪫 The irrigation battery is at 4%.", kind="irrigation_battery")
    await until(lambda: len(delivered) == 6, what="the battery alert")
    assert sorted(c for c, text, _ in delivered if "battery" in text) == [-7, 1, 2]
    saved = AlertState(tmp_path / "s.json")
    assert saved.marks[irrigation_remote.NAME]["read"] == 2 and saved.read is None   # (ecobot-mcp's own mark untouched)
    irrigation.alerts.changed()                                                      # (as a reconnect does: read once, nothing new)
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    restarted = AlertState(tmp_path / "s.json")                                       # the bot restarted: it resumes after the last one
    task = await delivering(restarted, ReadMark(restarted, irrigation_remote.NAME))
    await asyncio.sleep(0.2)
    assert len(delivered) == 6
    await irrigation.server.notify("☔ Irrigation paused for another 24 hours: 3 mm of rain in the last 24 hours.", kind="irrigation_pause")
    await until(lambda: len(delivered) == 9, what="the next alert")
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(delivered) == 9 and [t for _, t, _ in delivered].count(delivered[0][1]) == 3   # each alert once per chat
    from lib.alerts.menu import alert_keyboard, keyboard
    kinds = available_kinds({"Ecowitt", "AirGradient", "Pollen", "Forecast", "Irrigation"})
    boards = [keyboard(set(), kinds, section, set()) for section in (None, "alerts", "sound")]
    boards += [alert_keyboard(kind, set(), set(), section) for kind in ("irrigation_pause", "irrigation_battery") for section in (None, "bell")]
    for board in boards:                                                # every button's data fits Telegram's 64 bytes, for the new type too
        assert all(len(data.encode()) <= 64 for row in rows(board) for _, data in row)


async def test_irrigation_mcp_down_the_button_says_it_cant_reach_the_controller_and_the_bot_carries_on(tmp_path, monkeypatch):
    monkeypatch.setattr(irrigation_remote, "CALL_WAIT", 0.2)
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "Rob")
    down = RemoteIrrigation("http://127.0.0.1:9/mcp")                   # nothing answers there (its supervisor not even started)
    bot = Bot(NS(tz=TZ), None, state, down, service=WEATHER)
    update, sent = private_message(templates.IRRIGATION)
    await bot.on_message(update, NS(bot=NS(username="testbot", id=99)))
    assert sent == [("Sorry, I couldn't reach the irrigation controller.", {})]
    q = await press(bot, "ir:water:5")
    assert q.toast == "Couldn't reach the irrigation controller" and q.edits == []
    q = await press(bot, "ir:open:water")                                 # what needs no controller still works
    assert q.edits[0][0] == "markup"
    q = await press(bot, "ir:batt:off")                                   # and so do the chat's own alert settings
    assert q.toast == "Irrigation battery alerts off in this chat" and state.muted(1) == {"irrigation_battery"}
    with pytest.raises(IrrigationError, match="irrigation-mcp isn't reachable"):
        await down.status()


async def test_refresh_reads_the_controller_again_and_redraws_without_sending_anything_or_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_module, "IRRIGATION_SETTLE_SECONDS", 3)
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(bot_module.asyncio, "sleep", sleep)
    bot, state, device = await irrigation_bot(tmp_path)
    device.status_now.update(switch=True, countdown=300, work_state="auto")              # changed on the controller (its own schedule, or the app)
    q = await press(bot, "ir:refresh")
    assert q.toast == "Refreshed" and device.commands == [] and slept == []
    assert q.edits[0][0] == "text" and "mode: auto\nstate: on ✅ (5 mins until off)" in q.edits[0][1] and "(alerts: on)" in q.edits[0][1]
    assert rows(q.edits[0][2])[1][1] == ("🔄", "ir:refresh")                       # the buttons come back collapsed
    device.down = True
    q = await press(bot, "ir:refresh")
    assert q.toast == "Couldn't reach the irrigation controller" and q.edits == []
    q = Query("ir:refresh")                                                                # the same text again: Telegram says "not modified", which is fine
    q.fail = BadRequest("Message is not modified: specified new message content and reply markup are exactly the same")
    device.down = False
    await bot.on_irrigation_button(NS(callback_query=q), NS())
    assert q.toast == "Refreshed"


async def test_every_member_of_a_group_may_use_the_irrigation_buttons(tmp_path):
    bot, state, device = await irrigation_bot(tmp_path)
    state.add_chat(-5, "Home")
    group = dict(chat_id=-5, chat_type="supergroup", user=42)                                 # an ordinary member: no admin check is made
    q = await press(bot, "ir:open:water", **group)
    assert q.edits[0][0] == "markup" and rows(q.edits[0][1])[-1][0] == ("5 min", "ir:water:5")
    q = await press(bot, "ir:water:10", **group)
    assert device.commands == [("water", 10)] and q.toast == "Watering for 10 minutes" and "state: on ✅" in q.edits[0][1]
    q = await press(bot, "ir:sw:off", **group)
    assert device.commands[-1] == ("switch", False) and q.toast == "Switched off"
    q = await press(bot, "ir:delay:24h", **group)
    assert device.commands[-1] == ("delay", "24h") and q.toast == "Paused 24h"
    q = await press(bot, "ir:delay:cancel", **group)
    assert q.toast == "Unpaused"
    q = await press(bot, "ir:refresh", **group)
    assert q.toast == "Refreshed" and q.edits
    q = await press(bot, "ir:batt:off", **group)
    assert state.muted(-5) == {"irrigation_battery"} and q.toast == "Irrigation battery alerts off in this chat"


def test_the_error_footer_is_bold_and_positioned_in_utf16_units():
    text, entities = menu.with_error("🌱 Irrigation\nmode: idle")
    assert text == "🌱 Irrigation\nmode: idle\n\nerror: can not turn on water while paused!"
    (bold,) = entities
    units = lambda s: len(s.encode("utf-16-le")) // 2
    assert bold.type == "bold" and bold.length == units(menu.PAUSED_ERROR) == len(menu.PAUSED_ERROR)
    assert bold.offset == units("🌱 Irrigation\nmode: idle\n\n") == len("🌱 Irrigation\nmode: idle\n\n") + 1      # the emoji counts twice


async def test_water_is_refused_while_paused_with_a_toast_and_a_bold_footer_until_it_is_unpaused(tmp_path, monkeypatch):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(bot_module, "IRRIGATION_SETTLE_SECONDS", 3)
    monkeypatch.setattr(bot_module.asyncio, "sleep", sleep)
    bot, state, device = await irrigation_bot(tmp_path)
    await press(bot, "ir:delay:48h")                                                       # paused
    device.commands.clear()
    slept.clear()
    for minutes in (5, 30):
        q = await press(bot, f"ir:water:{minutes}")
        assert device.commands == [] and watched(bot) is None and slept == []        # nothing sent, no run to watch, no wait
        assert q.toast == "error: can not turn on water while paused!"
        kind, text, markup, entities = q.edits[0]
        assert text.startswith("🌱 Irrigation\nmode: idle\nstate: off ❌\npause: 48h") and text.endswith("\n\nerror: can not turn on water while paused!")
        assert [(e.type, e.length) for e in entities] == [("bold", len(menu.PAUSED_ERROR))] and len(rows(markup)) == 2    # collapsed
    q = await press(bot, "ir:sw:off")                                                      # switching off is still allowed while paused
    assert device.commands == [("switch", False)] and "error:" not in q.edits[0][1]
    q = await press(bot, "ir:refresh")
    assert "error:" not in q.edits[0][1]                                                    # the footer goes with the next redraw
    await press(bot, "ir:delay:cancel")                                                    # unpaused: water works again
    q = await press(bot, "ir:water:10")
    assert device.commands[-1] == ("water", 10) and q.toast == "Watering for 10 minutes" and "error:" not in q.edits[0][1]
    assert watched(bot) and slept[-1] == 3


async def test_water_while_the_status_cannot_be_read_says_so_and_sends_nothing(tmp_path):
    bot, state, device = await irrigation_bot(tmp_path)
    device.down = True
    q = await press(bot, "ir:water:5")
    assert q.toast == "Couldn't reach the irrigation controller" and q.edits == [] and device.commands == []


def test_a_status_message_says_whether_it_is_paused():
    assert menu.is_paused({"weather_delay": "24h"}) and not menu.is_paused({"weather_delay": "cancel"}) and not menu.is_paused({})
    assert menu.paused_in(menu.status_text({"weather_delay": "72h"}, True, True)) is True
    assert menu.paused_in(menu.status_text({"weather_delay": "cancel"}, True, True)) is False
    assert menu.paused_in(menu.status_text({}, True, True)) is False and menu.paused_in("") is False and menu.paused_in(None) is False
    assert menu.paused_in("mode: idle\npause: 24h (alerts: on)") is True


async def test_while_paused_the_button_is_unpause_and_one_press_ends_the_pause(tmp_path):
    bot, state, device = await irrigation_bot(tmp_path)
    update, sent = private_message("irrigation")
    await bot.on_message(update, NS(bot=NS(username="testbot", id=99)))
    assert rows(sent[0][1]["reply_markup"])[0][1] == ("⏸ Pause ▸", "ir:open:delay")               # not paused
    q = await press(bot, "ir:open:delay")
    assert [b[0] for b in rows(q.edits[0][1])[-1]] == ["24h", "48h", "72h"]
    q = await press(bot, "ir:delay:48h")                                                        # pause it
    assert q.toast == "Paused 48h" and "pause: 48h" in q.edits[0][1]
    assert rows(q.edits[0][2])[0][0] == ("▶️ Unpause", "ir:delay:cancel")                       # the redrawn button follows the controller
    shown = q.edits[0][1]
    q = await press_on(bot, "ir:open:water", shown)                                              # opening another list keeps the Unpause button
    assert rows(q.edits[0][1])[0] == [("▶️ Unpause", "ir:delay:cancel")] and len(rows(q.edits[0][1])) == 2   # no Water button, so no minutes
    q = await press_on(bot, "ir:close", shown)
    assert rows(q.edits[0][1])[0][0] == ("▶️ Unpause", "ir:delay:cancel")
    q = await press(bot, "ir:refresh")
    assert rows(q.edits[0][2])[0][0] == ("▶️ Unpause", "ir:delay:cancel")
    q = await press(bot, "ir:delay:cancel")                                                     # the Unpause button
    assert q.toast == "Unpaused" and device.commands[-1] == ("delay", "cancel") and "pause: not paused" in q.edits[0][1]
    assert rows(q.edits[0][2])[0][1] == ("⏸ Pause ▸", "ir:open:delay")                            # and it is Pause again
    device.status_now["weather_delay"] = "24h"                                                  # paused from the app
    q = await press(bot, "ir:refresh")
    assert rows(q.edits[0][2])[0][0] == ("▶️ Unpause", "ir:delay:cancel")
    q = await press(bot, "ir:water:5")                                                          # a refused water press keeps it too
    assert rows(q.edits[0][2])[0][0] == ("▶️ Unpause", "ir:delay:cancel") and q.edits[0][1].endswith("paused!")


async def press_on(bot, data, text):
    """A press under a message that currently reads `text`."""
    q = Query(data)
    q.message.text = text
    await bot.on_irrigation_button(NS(callback_query=q), NS())
    return q
