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

import lib.bot as bot_module
from lib import templates
from lib.alerts import AlertState, IrrigationMonitor
from lib.alerts import irrigation as monitor_module
from lib.alerts.menu import available_kinds
from lib.bot import Bot
from lib.config import Config
from lib.irrigation import IrrigationError, Tuya, Watchdog, menu
from lib.irrigation.tuya import sign
from tests.fakes import TZ, config

@pytest.fixture(autouse=True)
def settle_at_once(monkeypatch):
    """The irrigation buttons wait a couple of seconds before redrawing the message; the tests do not."""
    monkeypatch.setattr(bot_module, "IRRIGATION_SETTLE_SECONDS", 0)


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
        "🌱 Irrigation\nstate: on ✅\nmode: auto\nbattery: 73% 🔋")
    assert menu.status_text({"switch": False, "battery_percentage": 9, "work_state": "idle", "weather_delay": "24h"}) == (
        "🌱 Irrigation\nstate: off ❌\npause: 24h\nmode: idle\nbattery: 9% 🪫")
    assert "🔋" in menu.status_text({"battery_percentage": 10}) and "🪫" in menu.status_text({"battery_percentage": 0})
    assert menu.status_text({}) == "🌱 Irrigation\nstate: off ❌"                                      # a missing reading is left out
    assert menu.status_text({"switch": True, "battery_percentage": 73, "work_state": "auto"}, True) == (
        "🌱 Irrigation\nstate: on ✅\nmode: auto\nbattery: 73% 🔋 (alerts: on)")             # state, mode, then the battery with this chat's alerts in brackets
    full = {"switch": True, "countdown": 600, "weather_delay": "48h", "work_state": "manual", "battery_percentage": 80}
    assert menu.status_text(full, True) == (                                                  # all of it, in order
        "🌱 Irrigation\nstate: on ✅\ntime until state off: 10 min\npause: 48h\nmode: manual\nbattery: 80% 🔋 (alerts: on)")
    for countdown, shown in ((600, "10 min"), (601, "11 min"), (30, "1 min"), (1, "1 min"), (5400, "90 min")):    # whole minutes, a started one counts
        assert f"time until state off: {shown}\n" in menu.status_text({**full, "countdown": countdown})
    for hidden in ({"countdown": 0}, {"countdown": None}, {"countdown": -5}, {"switch": False, "countdown": 600}, {"switch": False}):
        assert "time until" not in menu.status_text({**full, **hidden}), hidden             # only while it is on and counting down
    assert "pause:" not in menu.status_text({**full, "weather_delay": "cancel"}) and "pause:" not in menu.status_text({"switch": True})
    assert menu.status_text({"switch": False}, False) == "🌱 Irrigation\nstate: off ❌\nbattery: unknown (alerts: off)"      # no battery reading
    assert menu.status_text({"battery_percentage": 4}, False).endswith("battery: 4% 🪫 (alerts: off)")


def test_the_buttons_open_one_section_at_a_time_and_every_callback_is_short():
    assert rows(menu.keyboard(False)) == [[("💧 Water", "ir:open:water"), ("⏸ Pause timer", "ir:open:delay")],
                                          [("🔔 Enable battery alerts", "ir:batt:on"), ("🔄 Refresh", "ir:refresh")]]   # not subscribed: the button subscribes
    assert rows(menu.keyboard(True))[1] == [("🔕 Disable battery alerts", "ir:batt:off"), ("🔄 Refresh", "ir:refresh")]   # subscribed: it unsubscribes
    water = rows(menu.keyboard(True, "water"))
    assert water[0][0] == ("▾ 💧 Water", "ir:close") and water[-2] == [(f"{m} min", f"ir:water:{m}") for m in (5, 10, 20, 30)]
    assert water[-1] == [("Off ❌", "ir:sw:off")]                                           # the valve itself: only off (a run is always timed)
    assert "ir:sw:on" not in [data for row in water for _, data in row]
    assert rows(menu.keyboard(True, "delay"))[-1] == [("24h", "ir:delay:24h"), ("48h", "ir:delay:48h"), ("72h", "ir:delay:72h"),
                                                      ("unpause", "ir:delay:cancel")]
    assert menu.SECTIONS == ("water", "delay") and len(rows(menu.keyboard(True, "switch"))) == 2      # there is no Switch menu any more
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
    assert available_kinds({"Ecowitt", "Irrigation"})[-2:] == ["irrigation", "irrigation_pause"]
    assert "irrigation" not in available_kinds({"Ecowitt"}) and "irrigation_pause" not in available_kinds({"Ecowitt"})
    from lib.alerts.menu import LABELS, keyboard
    assert LABELS["irrigation"] == "irrigation battery" and LABELS["irrigation_pause"] == "irrigation pause"
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

    def _reachable(self):
        if self.down:
            raise IrrigationError("Tuya request failed (ConnectError)")

    async def delay(self, duration):
        self._reachable()
        self.commands.append(("delay", duration))

    async def set_switch(self, on):
        self._reachable()
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
    assert device.commands == ([("delay", "24h")] if delayed else [])
    assert [(text.startswith("☔ Irrigation paused for 24 hours: ") and text.endswith("."), kw) for text, kw in sent] == (
        [(True, {"kind": "irrigation_pause"})] if delayed else [])                  # told once when it paused, never otherwise
    assert state.monitor["irrigation"]["checked"] == "2026-10-08"


async def test_a_delay_already_set_is_never_shortened_or_repeated(tmp_path):
    device = Device(weather_delay="72h")
    m, _, sent = daily(tmp_path, device, Station(0, 5), Forecast(90, 90))
    await m.check(NOW)
    assert device.commands == [] and sent == []                                          # nothing changed: nothing to tell


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
    assert len(sent) == 1 and sent[0][1] == {"kind": "irrigation"}                         # no "paused" alert for a pause that was not made


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
    def __init__(self, data, chat_id=1, chat_type="private", user=7):
        self.data, self.message = data, NS(chat=NS(type=chat_type, id=chat_id), chat_id=chat_id)
        self.from_user, self.toast, self.edits, self.fail = NS(id=user), "unset", [], None

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
        self._reachable()
        self.commands.append(("water", minutes))
        self.status_now.update(switch=True, work_state="manual", countdown=minutes * 60)


def irrigation_bot(tmp_path, device=None):
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "Rob")
    device = device or Remote()
    return Bot(NS(tz=TZ), None, [NS(name="Ecowitt")], state, device), state, device


def admin(status):
    async def get_chat_member(chat_id, user_id):
        return NS(status=status)
    return NS(bot=NS(get_chat_member=get_chat_member))


async def press(bot, data, status=None, **kw):
    q = Query(data, **kw)
    await bot.on_irrigation_button(NS(callback_query=q), admin(status) if status else NS())
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
    assert sent[0][0].startswith("🌱 Irrigation\nstate: off ❌\nmode: idle\nbattery: 100% 🔋 (alerts: on)")
    assert rows(sent[0][1]["reply_markup"])[0][0] == ("💧 Water", "ir:open:water") and rows(sent[0][1]["reply_markup"])[1][0][1] == "ir:batt:off"   # every chat starts subscribed
    device.down = True
    await bot.on_message(update, NS(bot=NS(username="b", id=99)))
    assert sent[1][0] == "Sorry, I couldn't reach the irrigation controller." and len(sent) == 2


async def test_opening_and_closing_a_section_only_changes_the_buttons(tmp_path):
    bot, _, device = irrigation_bot(tmp_path)
    q = await press(bot, "ir:open:water")
    assert q.edits[0][0] == "markup" and rows(q.edits[0][1])[-2][0] == ("5 min", "ir:water:5") and rows(q.edits[0][1])[-1][0] == ("Off ❌", "ir:sw:off") and device.commands == []
    q = await press(bot, "ir:close")
    assert len(rows(q.edits[0][1])) == 2
    for gone in ("ir:open:bogus", "ir:open:switch"):                                     # an unknown section, or an old message's Switch, just closes
        q = await press(bot, gone)
        assert len(rows(q.edits[0][1])) == 2


async def test_watering_a_delay_and_the_switch_act_on_the_controller_and_refresh_the_message(tmp_path):
    bot, state, device = irrigation_bot(tmp_path)
    q = await press(bot, "ir:water:10")
    assert device.commands == [("water", 10)] and q.toast == "Watering for 10 minutes" and bot.watchdog.off_at
    assert q.edits[0][0] == "text" and "state: on ✅" in q.edits[0][1] and "mode: manual" in q.edits[0][1]
    assert "time until state off: 10 min" in q.edits[0][1]
    q = await press(bot, "ir:sw:off")
    assert device.commands[-1] == ("switch", False) and q.toast == "Switched off" and bot.watchdog.off_at is None and "state: off ❌" in q.edits[0][1]
    q = await press(bot, "ir:delay:72h")
    assert device.commands[-1] == ("delay", "72h") and q.toast == "Timer paused 72h"
    q = await press(bot, "ir:delay:cancel")
    assert q.toast == "Timer unpaused" and device.commands[-1] == ("delay", "cancel")                  # unpause is the device's own "cancel"
    n = len(device.commands)
    for data in ("ir:water:7", "ir:water:x", "ir:delay:1h", "ir:sw:on", "ir:sw:maybe", "ir:what", "ir:", "garbage"):   # nothing the buttons offer (On is gone)
        q = await press(bot, data)
        assert q.edits == [] and q.toast is None, data
    assert len(device.commands) == n


async def test_the_battery_button_subscribes_or_unsubscribes_this_chat_to_the_alert_type(tmp_path):
    bot, state, _ = irrigation_bot(tmp_path)
    q = await press(bot, "ir:batt:off")
    assert state.muted(1) == {"irrigation"} and q.toast == "Irrigation battery alerts off in this chat"
    assert rows(q.edits[0][2])[1][0] == ("🔔 Enable battery alerts", "ir:batt:on")           # unsubscribed now: the button offers to enable
    q = await press(bot, "ir:batt:on")
    assert state.muted(1) == set() and rows(q.edits[0][2])[1][0] == ("🔕 Disable battery alerts", "ir:batt:off")
    for _ in range(2):                                                                        # whatever the button says, pressing it does that
        text, data = rows(menu.keyboard(bool(state.subscribed(1, "irrigation"))))[1][0]
        was = state.subscribed(1, "irrigation")
        await press(bot, data)
        assert state.subscribed(1, "irrigation") is (text.startswith("🔔 Enable")), (text, was)
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
    bot, state, device = irrigation_bot(tmp_path)
    bot.agent = NoModel()
    ctx = NS(bot=NS(username="testbot", id=99))
    for text in ("irrigation", "water", "turn the sprinkler on", templates.IRRIGATION):
        update, sent = private_message(text)
        await bot.on_message(update, ctx)
        assert sent[0][0].startswith("🌱 Irrigation\nstate: off ❌") and sent[0][1]["reply_markup"], text
    update, sent = group_message("@testbot irrigation")
    await bot.on_message(update, ctx)
    assert sent[0][0].startswith("🌱 Irrigation") and rows(sent[0][1]["reply_markup"])[0][0] == ("💧 Water", "ir:open:water")
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
    bot = Bot(NS(tz=TZ), Agent(), [], state, Remote())
    update, sent = private_message("how much water did the rain add this week")
    await bot.on_message(update, NS(bot=NS(username="testbot", id=99)))
    assert asked == ["how much water did the rain add this week"]
    plain = Bot(NS(tz=TZ), Agent(), [], AlertState(tmp_path / "t.json"))
    update, sent = private_message("irrigation")
    await plain.on_message(update, NS(bot=NS(username="testbot", id=99)))
    assert asked[-1] == "irrigation"                                                       # nothing to show: the model answers as before


async def test_in_a_group_only_admins_may_use_the_irrigation_buttons(tmp_path):
    bot, state, device = irrigation_bot(tmp_path)
    state.add_chat(-5, "Home")
    for data in ("ir:open:water", "ir:water:10", "ir:sw:off", "ir:batt:off"):
        q = await press(bot, data, status="member", chat_id=-5, chat_type="supergroup")
        assert q.toast == "Only group admins can change irrigation" and q.edits == [], data
    assert device.commands == [] and state.muted(-5) == set()
    q = await press(bot, "ir:water:10", status="administrator", chat_id=-5, chat_type="supergroup")
    assert device.commands == [("water", 10)] and q.edits
    q = await press(bot, "ir:delay:24h", status="creator", chat_id=-5, chat_type="group")
    assert device.commands[-1] == ("delay", "24h")


async def test_the_message_is_redrawn_a_couple_of_seconds_after_a_command_and_shows_the_chat_s_battery_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_module, "IRRIGATION_SETTLE_SECONDS", 3)
    bot, state, device = irrigation_bot(tmp_path)
    events = []

    async def sleep(seconds):
        events.append(("sleep", seconds))
    original_status = device.status

    async def status():
        events.append(("status",))
        return await original_status()
    device.status = status
    monkeypatch.setattr(bot_module.asyncio, "sleep", sleep)
    q = await press(bot, "ir:water:5")
    assert events == [("sleep", 3), ("status",)] and q.toast == "Watering for 5 minutes"           # the command, a wait, then the fresh state
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
    bot, state, device = irrigation_bot(tmp_path)

    async def delay(duration):
        device.commands.append(("delay", duration))
        device.down = True                                                                          # the command went through, then the line dropped
    device.delay = delay
    q = await press(bot, "ir:delay:48h")
    assert device.commands == [("delay", "48h")] and q.toast == "Timer paused 48h" and q.edits == []


@pytest.mark.parametrize("station, forecast, why", [
    (Station(0, 0.6, 0.6), None, "1.2 mm of rain in the last 24 hours"),
    (None, Forecast(10, 50), "50% chance of at least 1 mm tomorrow"),
    (None, Forecast(55, 0), "55% chance of at least 1 mm today"),
    (Station(0, 3, 0), Forecast(90, 90), "3 mm of rain in the last 24 hours"),                    # what fell comes first
])
async def test_the_pause_alert_says_why(tmp_path, station, forecast, why):
    m, _, sent = daily(tmp_path, Device(), station, forecast)
    await m.check(NOW)
    assert sent == [(f"☔ Irrigation paused for 24 hours: {why}.", {"kind": "irrigation_pause"})]


async def test_a_pause_that_fails_and_is_retried_is_told_exactly_once(tmp_path):
    class Flaky(Device):
        failures = 1                                                                  # the first command is refused, the second goes through

        async def delay(self, duration):
            if self.failures:
                self.failures -= 1
                raise IrrigationError("Tuya request failed (ConnectError)")
            await super().delay(duration)
    device = Flaky()
    m, state, sent = daily(tmp_path, device, None, Forecast(90, 90))
    with pytest.raises(IrrigationError):
        await m.check(NOW)
    assert sent == [] and "checked" not in state.monitor["irrigation"]               # nothing told for a pause that was not made
    await m.check(NOW)
    assert device.commands == [("delay", "24h")] and len(sent) == 1 and state.monitor["irrigation"]["checked"] == "2026-10-08"


async def test_the_pause_alert_reaches_every_subscribed_chat_with_the_usual_menu(tmp_path):
    from lib.alerts import Notifier
    state, delivered = AlertState(tmp_path / "s.json"), []
    for chat in (1, 2, 3, -7):
        state.add_chat(chat, str(chat))
    state.set_kind(2, "irrigation_pause", False)                       # this chat turned the pause alerts off
    state.set_kind(3, "irrigation", False)                             # ... and this one only the battery alerts: it still gets the pause

    class Telegram:
        async def send_message(self, chat_id, text, **kw):
            delivered.append((chat_id, text, kw))
    notifier = Notifier(Telegram(), state, available_kinds({"Ecowitt", "Irrigation"}))
    m = IrrigationMonitor(Device(), state, notifier, TZ, 18, None, Forecast(90, 90))
    await m.check(NOW)
    assert sorted(c for c, _, _ in delivered) == [-7, 1, 3] and all(text.startswith("☔ Irrigation paused") for _, text, _ in delivered)
    markup = delivered[0][2]["reply_markup"]
    assert rows(markup) == [[("➕ Subscribe", "al:open:sub:irrigation_pause"), ("➖ Unsubscribe", "al:open:unsub:irrigation_pause")]]
    from lib.alerts.menu import keyboard
    kinds = available_kinds({"Ecowitt", "AirGradient", "Pollen", "Forecast", "Irrigation"})
    for section in (None, "sub", "unsub"):                              # every button's data fits Telegram's 64 bytes, for the new type too
        for parent in (None, "irrigation_pause", "irrigation"):
            assert all(len(data.encode()) <= 64 for row in rows(keyboard(set(), kinds, section, parent)) for _, data in row)


async def test_refresh_reads_the_controller_again_and_redraws_without_sending_anything_or_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_module, "IRRIGATION_SETTLE_SECONDS", 3)
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(bot_module.asyncio, "sleep", sleep)
    bot, state, device = irrigation_bot(tmp_path)
    device.status_now.update(switch=True, countdown=300, work_state="auto")              # changed on the controller (its own schedule, or the app)
    q = await press(bot, "ir:refresh")
    assert q.toast == "Refreshed" and device.commands == [] and slept == []
    assert q.edits[0][0] == "text" and "state: on ✅\ntime until state off: 5 min" in q.edits[0][1] and "(alerts: on)" in q.edits[0][1]
    assert rows(q.edits[0][2])[1][1] == ("🔄 Refresh", "ir:refresh")                       # the buttons come back collapsed
    device.down = True
    q = await press(bot, "ir:refresh")
    assert q.toast == "Couldn't reach the irrigation controller" and q.edits == []
    q = Query("ir:refresh")                                                                # the same text again: Telegram says "not modified", which is fine
    q.fail = BadRequest("Message is not modified: specified new message content and reply markup are exactly the same")
    device.down = False
    await bot.on_irrigation_button(NS(callback_query=q), NS())
    assert q.toast == "Refreshed"


async def test_in_a_group_only_admins_may_refresh_too(tmp_path):
    bot, state, device = irrigation_bot(tmp_path)
    state.add_chat(-5, "Home")
    q = await press(bot, "ir:refresh", status="member", chat_id=-5, chat_type="supergroup")
    assert q.toast == "Only group admins can change irrigation" and q.edits == []
    q = await press(bot, "ir:refresh", status="administrator", chat_id=-5, chat_type="supergroup")
    assert q.toast == "Refreshed" and q.edits
