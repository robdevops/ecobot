"""The irrigation controller: the Tuya client, the keyboard, the daily check, the watchdog, and the buttons."""

import asyncio
import hashlib
import hmac
import json
from datetime import datetime
from types import SimpleNamespace as NS

import httpx
import pytest
from telegram.error import BadRequest

from lib import templates
from lib.alerts import AlertState, IrrigationMonitor
from lib.alerts import irrigation as monitor_module
from lib.alerts.menu import available_kinds
from lib.bot import Bot
from lib.config import Config
from lib.irrigation import IrrigationError, Tuya, Watchdog, menu
from lib.irrigation.tuya import sign
from tests.fakes import TZ, config

SECRET = "s3cret"
STATUS = [{"code": "switch", "value": False}, {"code": "battery_percentage", "value": 100}, {"code": "weather_delay", "value": "cancel"},
          {"code": "countdown", "value": 0}, {"code": "work_state", "value": "idle"}, {"code": "use_time_one", "value": 40}]   # as the controller answered


def cfg(tmp_path, **over):
    return config(tmp_path, tuya_device_id="dev1", tuya_client_id="cid", tuya_client_secret=SECRET, **over)


class FakeTuya:
    """Stands in for the Tuya cloud: records the signed requests it gets and checks each signature."""

    def __init__(self, status=None, fail_token_once=False):
        self.calls, self.status, self.token_n, self.fail_token_once = [], status if status is not None else STATUS, 0, fail_token_once

    def __call__(self, request: httpx.Request) -> httpx.Response:
        h, path = request.headers, request.url.raw_path.decode()
        body = request.content
        token = h.get("access_token", "")
        expected = sign("cid", SECRET, token, h["t"], h["nonce"], request.method, path, body)   # signed over what was sent
        assert h["sign"] == expected and h["client_id"] == "cid" and h["sign_method"] == "HMAC-SHA256"
        if path.startswith("/v1.0/token"):
            self.token_n += 1
            return httpx.Response(200, json={"success": True, "result": {"access_token": f"tok{self.token_n}", "expire_time": 7200}})
        self.calls.append((request.method, path, json.loads(body) if body else None, token))
        if self.fail_token_once and token == "tok1":
            self.fail_token_once = False
            return httpx.Response(200, json={"success": False, "code": 1010, "msg": "token invalid"})
        if path.endswith("/status"):
            return httpx.Response(200, json={"success": True, "result": self.status})
        return httpx.Response(200, json={"success": True, "result": True})


def tuya(tmp_path, **kw):
    fake = FakeTuya(**kw)
    return Tuya(cfg(tmp_path), transport=httpx.MockTransport(fake)), fake


def test_the_signature_is_hmac_sha256_of_the_ids_the_time_and_the_request():
    body = b'{"commands":[{"code":"switch","value":true}]}'
    to_sign = f"POST\n{hashlib.sha256(body).hexdigest()}\n\n/v1.0/devices/d/commands"          # the way the sample script builds it
    want = hmac.new(b"sec", ("cid" + "tok" + "1700000000000" + "abc" + to_sign).encode(), hashlib.sha256).hexdigest().upper()
    assert sign("cid", "sec", "tok", "1700000000000", "abc", "POST", "/v1.0/devices/d/commands", body) == want
    empty = hashlib.sha256(b"").hexdigest()
    assert sign("cid", "sec", "", "1", "n", "GET", "/v1.0/token?grant_type=1", b"") == hmac.new(
        b"sec", f"cid1nGET\n{empty}\n\n/v1.0/token?grant_type=1".encode(), hashlib.sha256).hexdigest().upper()


async def test_status_is_read_as_code_and_value_and_the_token_is_fetched_once(tmp_path):
    device, fake = tuya(tmp_path)
    got = await device.status()
    assert got == {"switch": False, "battery_percentage": 100, "weather_delay": "cancel", "countdown": 0, "work_state": "idle", "use_time_one": 40}
    await device.status()
    assert fake.token_n == 1 and [c[1] for c in fake.calls] == ["/v1.0/devices/dev1/status"] * 2 and fake.calls[0][3] == "tok1"
    await device.close()


async def test_watering_sends_the_length_and_the_switch_in_one_command_and_other_commands_are_exact(tmp_path):
    device, fake = tuya(tmp_path)
    await device.water(10)
    await device.delay("48h")
    await device.set_switch(False)
    assert [(m, p, b) for m, p, b, _ in fake.calls] == [
        ("POST", "/v1.0/devices/dev1/commands", {"commands": [{"code": "countdown", "value": 600}, {"code": "switch", "value": True}]}),
        ("POST", "/v1.0/devices/dev1/commands", {"commands": [{"code": "weather_delay", "value": "48h"}]}),
        ("POST", "/v1.0/devices/dev1/commands", {"commands": [{"code": "switch", "value": False}]})]
    for bad in (0, 61, -5):
        with pytest.raises(IrrigationError):
            await device.water(bad)
    with pytest.raises(IrrigationError):
        await device.delay("96h")
    assert len(fake.calls) == 3                                                          # nothing was sent for the bad ones


async def test_an_expired_token_is_replaced_and_the_request_repeated_once(tmp_path):
    device, fake = tuya(tmp_path, fail_token_once=True)
    assert (await device.status())["battery_percentage"] == 100
    assert fake.token_n == 2 and [c[3] for c in fake.calls] == ["tok1", "tok2"]


async def test_errors_name_the_problem_without_the_secrets(tmp_path):
    def refuse(request):
        return httpx.Response(200, json={"success": False, "code": 1004, "msg": "sign invalid"})
    device = Tuya(cfg(tmp_path), transport=httpx.MockTransport(refuse))
    with pytest.raises(IrrigationError, match="1004"):
        await device.status()

    def down(request):
        raise httpx.ConnectError("no route to host https://openapi.tuyaeu.com?secret=" + SECRET)
    device = Tuya(cfg(tmp_path), transport=httpx.MockTransport(down))
    with pytest.raises(IrrigationError) as e:
        await device.status()
    assert SECRET not in str(e.value) and "ConnectError" in str(e.value)


def test_the_controller_is_on_only_with_all_three_settings_and_the_hour_is_read_safely(monkeypatch, tmp_path):
    assert cfg(tmp_path).irrigation and not config(tmp_path).irrigation and not config(tmp_path, tuya_device_id="d", tuya_client_id="c").irrigation
    env = {"TELEGRAM_BOT_TOKEN": "t", "XAI_API_KEY": "k", "ECOWITT_API_KEY": "a", "ECOWITT_APP_KEY": "b"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for value, hour in (("", 18), ("6", 6), ("0", 0), ("23", 23), ("24", 18), ("noon", 18), ("-1", 18)):
        monkeypatch.setenv("IRRIGATION_CHECK_HOUR", value)
        assert Config.from_env().irrigation_check_hour == hour, value
    monkeypatch.setenv("TUYA_BASE_URL", "https://openapi.tuyaus.com/")
    monkeypatch.setenv("TUYA_DEVICE_ID", " dev ")
    assert Config.from_env().tuya_base_url == "https://openapi.tuyaus.com" and Config.from_env().tuya_device_id == "dev"


# ---------- the status message and its buttons ----------
def rows(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def test_the_status_says_switch_battery_mode_and_a_delay_only_when_there_is_one():
    assert menu.status_text({"switch": True, "battery_percentage": 73, "work_state": "auto", "weather_delay": "cancel"}) == (
        "🌱 Irrigation\nswitch: on ✅\nbattery: 73% 🔋\nmode: auto")
    assert menu.status_text({"switch": False, "battery_percentage": 9, "work_state": "idle", "weather_delay": "24h"}) == (
        "🌱 Irrigation\nswitch: off ❌\nbattery: 9% 🪫\nmode: idle\nweather delay: 24h")
    assert "🔋" in menu.status_text({"battery_percentage": 10}) and "🪫" in menu.status_text({"battery_percentage": 0})
    assert menu.status_text({}) == "🌱 Irrigation\nswitch: off ❌"                                      # a missing reading is left out


def test_the_buttons_open_one_section_at_a_time_and_every_callback_is_short():
    assert rows(menu.keyboard(False)) == [[("💧 Water", "ir:open:water"), ("⏸ Delay", "ir:open:delay")],
                                          [("🔔 Battery warnings", "ir:batt:on"), ("⏻ Switch", "ir:open:switch")]]
    assert rows(menu.keyboard(True))[1][0] == ("🔕 Stop battery warnings", "ir:batt:off")
    water = rows(menu.keyboard(True, "water"))
    assert water[0][0] == ("▾ 💧 Water", "ir:close") and water[-1] == [(f"{m} min", f"ir:water:{m}") for m in (5, 10, 20, 30)]
    assert rows(menu.keyboard(True, "delay"))[-1] == [("Delay 24h", "ir:delay:24h"), ("Delay 48h", "ir:delay:48h"), ("Delay 72h", "ir:delay:72h"),
                                                      ("cancel", "ir:delay:cancel")]
    assert rows(menu.keyboard(True, "switch"))[-1] == [("On ✅", "ir:sw:on"), ("Off ❌", "ir:sw:off")]
    for section in (None, *menu.SECTIONS):
        for sub in (True, False):
            assert all(len(data.encode()) <= 64 for row in rows(menu.keyboard(sub, section)) for _, data in row)


def test_the_irrigation_button_takes_wind_s_place_only_when_configured_and_changes_the_keyboard_version():
    plain, with_irrigation = templates.keyboard().keyboard, templates.keyboard(True).keyboard
    assert [b.text for row in plain for b in row][-1] == "\U0001f4a8 Wind" and templates.IRRIGATION not in [b.text for row in plain for b in row]
    labels = [b.text for row in with_irrigation for b in row]
    assert labels[-1] == templates.IRRIGATION and "\U0001f4a8 Wind" not in labels and len(labels) == 9 and with_irrigation[:2] == plain[:2]
    assert templates.version() == templates.VERSION != templates.version(True)
    assert templates.sentence(templates.IRRIGATION) is None                              # not a question for the model


def test_the_alert_list_gets_irrigation_battery_only_when_there_is_a_controller():
    assert available_kinds({"Ecowitt", "Irrigation"})[-1] == "irrigation" and "irrigation" not in available_kinds({"Ecowitt"})
    from lib.alerts.menu import LABELS, keyboard
    assert LABELS["irrigation"] == "irrigation battery"
    unsub = rows(keyboard(set(), ["rain", "irrigation"], "unsub"))
    assert ("irrigation battery", "al:off:irrigation:unsub") in [b for row in unsub for b in row]


# ---------- the daily check ----------
class Device:
    def __init__(self, **status):
        self.status_now = {"switch": False, "battery_percentage": 100, "weather_delay": "cancel", "work_state": "idle", **status}
        self.commands, self.down = [], False

    async def status(self):
        if self.down:
            raise IrrigationError("Tuya request failed (ConnectError)")
        return dict(self.status_now)

    async def delay(self, duration):
        self.commands.append(("delay", duration))

    async def set_switch(self, on):
        self.commands.append(("switch", on))
        self.status_now["switch"] = on


class Station:
    """The weather station: readings whose daily rain total rises by the given amounts."""

    def __init__(self, *rises, fail=False):
        total, self.rows, self.fail = 0.0, [], fail
        for i, rise in enumerate(rises):
            total += rise
            self.rows.append((1_000_000 + i * 300, {"rainfall.daily": total}))

    async def recent(self, hours):
        assert hours == 24
        if self.fail:
            raise RuntimeError("Ecowitt is down")
        return self.rows


class Forecast:
    def __init__(self, *chances):
        self.days = [{"date": datetime(2026, 10, 8 + i).date(), "rain_1mm_pct": c} for i, c in enumerate(chances)] + [
            {"date": datetime(2026, 10, 10).date(), "rain_1mm_pct": 90}]               # the day after tomorrow never counts

    def now(self):
        return datetime(2026, 10, 8, 18, 0)

    def upcoming(self, count=2):
        return self.days[:count]


NOW = datetime(2026, 10, 8, 18, 0)


def daily(tmp_path, device, station=None, forecast=None):
    state, sent = AlertState(tmp_path / "s.json"), []

    async def notify(text, link=None, **kw):
        sent.append((text, kw))
    return IrrigationMonitor(device, state, notify, TZ, 18, station, forecast), state, sent


@pytest.mark.parametrize("station, forecast, delayed", [
    (Station(0, 0.5, 0.4), Forecast(10, 20), False),                  # 0.9 mm: not more than 1
    (Station(0, 0.6, 0.6), Forecast(10, 20), True),                   # 1.2 mm fell
    (Station(0, 0, 0), Forecast(10, 50), True),                       # tomorrow 50%: at the line
    (Station(0, 0, 0), Forecast(55, 0), True),                        # today
    (Station(0, 0, 0), Forecast(45, 49), False),                      # both under the line
    (Station(0, 0, 0), Forecast(None, None), False),                  # no ensemble figure: nothing to go on
    (None, Forecast(0, 80), True),                                    # no station: the forecast decides
    (Station(0, 3, 0), None, True),                                   # no forecast: the measured rain decides
    (Station(fail=True), Forecast(0, 80), True),                      # the station failing leaves the forecast
    (Station(fail=True), Forecast(0, 0), False),
    (None, None, False),
    (Station(2, 0, 0), Forecast(0, 0), False),                        # the first reading's total fell before the window: only rises count
])
async def test_the_check_delays_a_day_when_more_than_1_mm_fell_or_is_likely(tmp_path, station, forecast, delayed):
    device = Device()
    m, state, sent = daily(tmp_path, device, station, forecast)
    await m.check(NOW)
    assert device.commands == ([("delay", "24h")] if delayed else []) and sent == []
    assert state.monitor["irrigation"]["checked"] == "2026-10-08"


async def test_a_delay_already_set_is_never_shortened_or_repeated(tmp_path):
    device = Device(weather_delay="72h")
    m, _, _ = daily(tmp_path, device, Station(0, 5), Forecast(90, 90))
    await m.check(NOW)
    assert device.commands == []


async def test_a_battery_under_5_percent_is_told_once_a_day_to_the_irrigation_alert_type(tmp_path):
    device = Device(battery_percentage=4)
    m, state, sent = daily(tmp_path, device)
    await m.check(NOW)
    await m.check(NOW)
    assert len(sent) == 1 and sent[0][1] == {"kind": "irrigation"} and "4%" in sent[0][0] and "🪫" in sent[0][0]
    await m.check(datetime(2026, 10, 9, 18, 0))                                          # the next day: again, until it is charged
    assert len(sent) == 2
    device.status_now["battery_percentage"] = 5                                         # 5% is not below 5%
    await m.check(datetime(2026, 10, 10, 18, 0))
    assert len(sent) == 2


async def test_the_battery_is_still_told_when_the_delay_cannot_be_sent_and_not_told_twice_on_the_retry(tmp_path):
    class Failing(Device):
        async def delay(self, duration):
            raise IrrigationError("Tuya error 28841002: no permission")
    m, state, sent = daily(tmp_path, Failing(battery_percentage=2), None, Forecast(90, 90))
    with pytest.raises(IrrigationError):
        await m.check(NOW)
    assert len(sent) == 1 and "checked" not in state.monitor["irrigation"]              # not recorded: the next try repeats the check
    with pytest.raises(IrrigationError):
        await m.check(NOW)
    assert len(sent) == 1


def test_a_check_is_due_once_a_day_after_the_hour_and_the_next_is_the_following_18_00(tmp_path):
    m, state, _ = daily(tmp_path, Device())
    assert not m.due(datetime(2026, 10, 8, 17, 59)) and m.due(datetime(2026, 10, 8, 18, 0)) and m.due(datetime(2026, 10, 8, 23, 0))
    state.monitor["irrigation"] = {"checked": "2026-10-08"}
    assert not m.due(datetime(2026, 10, 8, 23, 0)) and not m.due(datetime(2026, 10, 9, 3, 0)) and m.due(datetime(2026, 10, 9, 18, 0))
    assert m.seconds_until_next(datetime(2026, 10, 8, 17, 0)) == 3600 and m.seconds_until_next(datetime(2026, 10, 8, 18, 0)) == 86400
    assert m.seconds_until_next(datetime(2026, 10, 8, 23, 30)) == 18.5 * 3600


async def test_run_checks_when_due_retries_after_a_failure_and_then_waits_for_tomorrow(tmp_path, monkeypatch):
    device = Device()
    device.down = True
    m, state, _ = daily(tmp_path, device, None, Forecast(90, 90))
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            device.down = False                                                           # the controller is back after the first wait
        if len(sleeps) == 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(monitor_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(monitor_module, "now_local", lambda tz: NOW)
    with pytest.raises(asyncio.CancelledError):
        await m.run()
    assert sleeps[0] == 1800 and device.commands == [("delay", "24h")]                    # tried, failed, waited 30 minutes, tried, did it
    assert state.monitor["irrigation"]["checked"] == "2026-10-08"
    assert sleeps[1] == 86400                                                             # then until 18:00 tomorrow (now is exactly 18:00 here)


async def test_the_watchdog_closes_a_valve_that_is_still_open_after_the_run_and_survives_a_restart(tmp_path):
    clock, state = [1000.0], AlertState(tmp_path / "s.json")
    device = Device(switch=True)
    dog = Watchdog(device, state, clock=lambda: clock[0])
    await dog.check()
    assert device.commands == []                                                          # nothing armed: the controller is not even asked
    dog.arm(10)
    clock[0] = 1000 + 600 + 29
    await dog.check()
    assert device.commands == [] and dog.off_at                                           # inside the grace period: the device's own countdown may still end it
    clock[0] = 1000 + 600 + 31
    reborn = Watchdog(device, AlertState(tmp_path / "s.json"), clock=lambda: clock[0])    # the bot restarted in the middle of the run
    await reborn.check()
    assert device.commands == [("switch", False)] and reborn.off_at is None


async def test_the_watchdog_leaves_a_closed_valve_alone_tries_again_on_failure_and_disarms_on_request(tmp_path):
    clock, state = [0.0], AlertState(tmp_path / "s.json")
    device = Device(switch=False)
    dog = Watchdog(device, state, clock=lambda: clock[0])
    dog.arm(5)
    clock[0] = 400
    await dog.check()
    assert device.commands == [] and dog.off_at is None                                   # it ended by itself
    dog.arm(5)
    device.down, device.status_now["switch"] = True, True
    clock[0] = 800
    with pytest.raises(IrrigationError):
        await dog.check()
    assert dog.off_at is not None                                                         # kept: the next minute tries again
    device.down = False
    await dog.check()
    assert device.commands == [("switch", False)]
    dog.arm(5)
    dog.disarm()
    assert dog.off_at is None


# ---------- the buttons in the bot ----------
class Query:
    def __init__(self, data, chat_id=1):
        self.data, self.message = data, NS(chat=NS(type="private", id=chat_id), chat_id=chat_id)
        self.toast, self.edits, self.fail = "unset", [], None

    async def answer(self, text=None, **kw):
        self.toast = text

    async def edit_message_reply_markup(self, reply_markup=None):
        if self.fail:
            raise self.fail
        self.edits.append(("markup", reply_markup))

    async def edit_message_text(self, text, reply_markup=None):
        if self.fail:
            raise self.fail
        self.edits.append(("text", text, reply_markup))


class Remote(Device):
    """The controller as the bot's buttons see it: also water()."""

    async def water(self, minutes):
        self.commands.append(("water", minutes))
        self.status_now.update(switch=True, work_state="manual")


def irrigation_bot(tmp_path, device=None):
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "Rob")
    device = device or Remote()
    return Bot(NS(tz=TZ), None, [NS(name="Ecowitt")], state, device), state, device


async def press(bot, data, **kw):
    q = Query(data, **kw)
    await bot.on_irrigation_button(NS(callback_query=q), NS())
    return q


async def test_the_button_sends_the_status_with_its_buttons_and_a_failure_says_so(tmp_path):
    bot, state, device = irrigation_bot(tmp_path)
    sent = []

    async def reply_text(body, **kw):
        sent.append((body, kw))
    msg = NS(text=templates.IRRIGATION, chat=NS(type="private", id=1, title=None), chat_id=1, reply_text=reply_text, reply_to_message=None,
             message_thread_id=None, is_topic_message=False)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(id=7, full_name="Rob", username="rob"))
    await bot.on_message(update, NS(bot=NS(username="b", id=99)))                          # no model is involved (the agent is None)
    assert sent[0][0].startswith("🌱 Irrigation\nswitch: off ❌\nbattery: 100% 🔋\nmode: idle")
    assert rows(sent[0][1]["reply_markup"])[0][0] == ("💧 Water", "ir:open:water") and rows(sent[0][1]["reply_markup"])[1][0][1] == "ir:batt:off"   # every chat starts subscribed
    device.down = True
    await bot.on_message(update, NS(bot=NS(username="b", id=99)))
    assert sent[1][0] == "Sorry, I couldn't reach the irrigation controller." and len(sent) == 2


async def test_opening_and_closing_a_section_only_changes_the_buttons(tmp_path):
    bot, _, device = irrigation_bot(tmp_path)
    q = await press(bot, "ir:open:water")
    assert q.edits[0][0] == "markup" and rows(q.edits[0][1])[-1][0] == ("5 min", "ir:water:5") and device.commands == []
    q = await press(bot, "ir:close")
    assert len(rows(q.edits[0][1])) == 2
    q = await press(bot, "ir:open:bogus")
    assert len(rows(q.edits[0][1])) == 2                                                  # an unknown section is just closed


async def test_watering_a_delay_and_the_switch_act_on_the_controller_and_refresh_the_message(tmp_path):
    bot, state, device = irrigation_bot(tmp_path)
    q = await press(bot, "ir:water:10")
    assert device.commands == [("water", 10)] and q.toast == "Watering for 10 minutes" and bot.watchdog.off_at
    assert q.edits[0][0] == "text" and "switch: on ✅" in q.edits[0][1] and "mode: manual" in q.edits[0][1]
    q = await press(bot, "ir:sw:off")
    assert device.commands[-1] == ("switch", False) and q.toast == "Switched off" and bot.watchdog.off_at is None and "switch: off ❌" in q.edits[0][1]
    q = await press(bot, "ir:delay:72h")
    assert device.commands[-1] == ("delay", "72h") and q.toast == "Delayed 72h"
    q = await press(bot, "ir:delay:cancel")
    assert q.toast == "Delay cancelled"
    q = await press(bot, "ir:sw:on")
    assert device.commands[-1] == ("switch", True) and "stays on" in q.toast
    n = len(device.commands)
    for data in ("ir:water:7", "ir:water:x", "ir:delay:1h", "ir:sw:maybe", "ir:what", "ir:", "garbage"):   # nothing the buttons offer
        q = await press(bot, data)
        assert q.edits == [] and q.toast is None, data
    assert len(device.commands) == n


async def test_the_battery_button_subscribes_or_unsubscribes_this_chat_to_the_alert_type(tmp_path):
    bot, state, _ = irrigation_bot(tmp_path)
    q = await press(bot, "ir:batt:off")
    assert state.muted(1) == {"irrigation"} and q.toast == "Irrigation battery warnings off in this chat"
    assert rows(q.edits[0][2])[1][0] == ("🔔 Battery warnings", "ir:batt:on")             # the button now offers to turn it back on
    q = await press(bot, "ir:batt:on")
    assert state.muted(1) == set() and rows(q.edits[0][2])[1][0] == ("🔕 Stop battery warnings", "ir:batt:off")
    assert state.alert_chats("irrigation") == [1]


async def test_a_controller_that_cannot_be_reached_gives_a_toast_not_a_traceback_and_an_unchanged_message_is_fine(tmp_path):
    bot, state, device = irrigation_bot(tmp_path)
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
    plain = Bot(NS(tz=TZ), None, [], AlertState(tmp_path / "t.json"))                    # no controller configured: nothing happens
    q = await press(plain, "ir:delay:24h")
    assert q.edits == [] and plain.watchdog is None


async def test_the_handler_is_registered_and_the_alert_list_includes_irrigation_only_with_a_controller(tmp_path):
    bot, state, _ = irrigation_bot(tmp_path)
    added = []
    app = NS(add_handler=lambda h, group=0: added.append(h), add_error_handler=lambda h: None)
    bot.register(app)
    plain = Bot(NS(tz=TZ), None, [NS(name="Ecowitt")], AlertState(tmp_path / "t.json"))
    added_plain = []
    plain.register(NS(add_handler=lambda h, group=0: added_plain.append(h), add_error_handler=lambda h: None))
    assert len(added) == len(added_plain) + 1
    assert "irrigation" in bot._alert_kinds() and "irrigation" not in plain._alert_kinds()
    assert bot.keyboard_version == templates.version(True) != plain.keyboard_version == templates.VERSION
