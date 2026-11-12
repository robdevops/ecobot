"""irrigation-mcp's controller code, without Telegram (step 8b moved it out of the bot): the Tuya client (signed requests, the token,
its errors), the daily check (rain measured or likely, the battery, its days) and the watchdog that closes the valve after a run.
(The bot's menu and buttons, which call irrigation-mcp, are tests/test_irrigation.py; the server's tools tests/test_irrigation_mcp.py.)"""

import asyncio
import hashlib
import hmac
from datetime import datetime, timedelta

import httpx
import pytest

from lib.config import Config
from lib.irrigation import IrrigationError, Tuya, Watchdog
from lib.irrigation import check as monitor_module
from lib.irrigation.check import IrrigationMonitor
from lib.irrigation.state import IrrigationState
from lib.irrigation.tuya import sign
from tests.fakes import TUYA_SECRET as SECRET, TZ, FakeController, FakeTuya, config, tuya_config
from tests.test_mcp_listen import until

cfg = tuya_config
Device = FakeController   # (the controller as the check and the watchdog see it)

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


TOKEN = {"success": True, "result": {"access_token": "tok", "expire_time": 7200}}


@pytest.mark.parametrize("token, status", [({"success": True}, None),                         # a token reply with no token
                                           ({"success": True, "result": ["tok"]}, None),
                                           ([TOKEN], None),                                    # a reply that isn't an object
                                           (TOKEN, {"success": True, "result": {"switch": True}}),   # a status that isn't a list
                                           (TOKEN, "OK")])
async def test_a_reply_of_an_unexpected_shape_is_an_irrigation_error(tmp_path, token, status):
    def serve(request):
        return httpx.Response(200, json=token if request.url.path.startswith("/v1.0/token") else status)
    device = Tuya(cfg(tmp_path), transport=httpx.MockTransport(serve))
    with pytest.raises(IrrigationError):
        await device.status()
    await device.close()


async def test_status_points_with_no_value_or_no_code_are_read_safely(tmp_path):
    device, fake = tuya(tmp_path, status=[{"code": "switch"}, "battery", {"value": 3}, {"code": "countdown", "value": 60}])
    assert await device.status() == {"switch": None, "countdown": 60}
    await device.close()


def test_the_controller_is_on_only_with_all_three_settings_and_the_hour_is_read_safely_by_irrigation_mcp_alone(monkeypatch, tmp_path):
    assert cfg(tmp_path).irrigation and not config(tmp_path).irrigation and not config(tmp_path, tuya_device_id="d", tuya_client_id="c").irrigation
    env = {"BOT_TELEGRAM_TOKEN": "t", "BOT_OPENROUTER_API_KEY": "k", "BOT_MCP_URL": "http://127.0.0.1:8765/mcp",
           "IRRIGATION_TUYA_DEVICE_ID": " dev ", "IRRIGATION_TUYA_CLIENT_ID": "cid", "IRRIGATION_TUYA_CLIENT_SECRET": "sec"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for value, hour in (("", 18), ("6", 6), ("0", 0), ("23", 23), ("24", 18), ("noon", 18), ("-1", 18)):
        monkeypatch.setenv("IRRIGATION_CHECK_HOUR", value)
        assert Config.from_env(program="irrigation").irrigation_check_hour == hour, value
    monkeypatch.setenv("IRRIGATION_TUYA_BASE_URL", "https://openapi.tuyaus.com/")
    irrigation = Config.from_env(program="irrigation")
    assert irrigation.tuya_base_url == "https://openapi.tuyaus.com" and irrigation.tuya_device_id == "dev" and irrigation.irrigation
    bot = Config.from_env()                                                              # the bot reads none of them (step 8b)
    assert not bot.irrigation and bot.tuya_device_id == "" and bot.irrigation_mcp_url == ""


# ---------- the daily check ----------
class Station:
    """The weather station: readings whose daily rain total rises by the given amounts."""

    def __init__(self, *rises, fail=False):
        total, self.rows, self.fail = 0.0, [], fail
        for i, rise in enumerate(rises):
            total += rise
            self.rows.append((1_000_000 + i * 300, {"rainfall.daily": total}))

    async def rain_mm(self, hours):
        """The measured rain as ecobot-mcp's summary of the rain gives it (lib/irrigation/ecobot.ServiceRain): the rises of the daily total."""
        from lib.ecowitt.rain import rain_amount
        assert hours == 24
        if self.fail:
            raise RuntimeError("Ecowitt is down")
        return rain_amount(self.rows, self.rows[0][0], self.rows[-1][0]) if self.rows else None


class Forecast:
    def __init__(self, *chances):
        self.days = [{"date": datetime(2026, 10, 8 + i).date(), "rain_1mm_pct": c} for i, c in enumerate(chances)] + [
            {"date": datetime(2026, 10, 10).date(), "rain_1mm_pct": 90}]               # the day after tomorrow never counts
        self.fetched_at = datetime(2026, 10, 8, 17, 45, tzinfo=TZ).timestamp()

    def now(self):
        return datetime(2026, 10, 8, 18, 0)

    def upcoming(self, count=2):
        return self.days[:count]


NOW = datetime(2026, 10, 8, 18, 0)


def daily(tmp_path, device, station=None, forecast=None):
    state, sent = IrrigationState(tmp_path / "irrigation_state.json"), []

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
    assert state.monitor["irrigation_monitor"]["checked"] == "2026-10-08"


async def test_a_delay_already_set_is_never_shortened_or_repeated(tmp_path):
    device = Device(weather_delay="72h")
    m, _, sent = daily(tmp_path, device, Station(0, 5), Forecast(90, 90))
    await m.check(NOW)
    assert device.commands == [] and sent == []                                          # nothing changed: nothing to tell


async def test_the_bots_own_pause_from_yesterday_is_started_again_and_one_someone_set_is_not(tmp_path):
    device = Device()
    m, state, sent = daily(tmp_path, device, Station(0, 3))
    await m.check(NOW)                                                                   # paused at 6pm
    await m.check(NOW + timedelta(days=1))                                               # 6pm the next day: still on, for seconds
    assert device.commands == [("delay", "24h"), ("delay", "cancel"), ("delay", "24h")]
    assert sent[1][0] == "☔ Irrigation paused for another 24 hours: 3 mm of rain in the last 24 hours."
    m.station = Station(0, 0)
    await m.check(NOW + timedelta(days=2))                                               # dry: it lapses
    m.station = Station(0, 3)
    await m.check(NOW + timedelta(days=4))                                               # a 24h someone set (the bot's is days old)
    assert len(device.commands) == 3 and len(sent) == 2


async def test_a_forecast_more_than_a_day_old_does_not_decide(tmp_path, caplog):
    stale = Forecast(90, 90)
    stale.fetched_at -= 25 * 3600
    device = Device()
    m, state, sent = daily(tmp_path, device, Station(0, 0), stale)
    with caplog.at_level("INFO"):
        await m.check(NOW)
    assert device.commands == [] and sent == [] and "over a day old: only the measured rain decides" in caplog.text


async def test_a_battery_under_5_percent_is_told_once_a_day_to_the_irrigation_alert_type(tmp_path):
    device = Device(battery_percentage=4)
    m, state, sent = daily(tmp_path, device)
    await m.check(NOW)
    await m.check(NOW)
    assert len(sent) == 1 and sent[0][1] == {"kind": "irrigation_battery"} and "4%" in sent[0][0] and "🪫" in sent[0][0]
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
    assert len(sent) == 1 and "checked" not in state.monitor["irrigation_monitor"]              # not recorded: the next try repeats the check
    with pytest.raises(IrrigationError):
        await m.check(NOW)
    assert len(sent) == 1 and sent[0][1] == {"kind": "irrigation_battery"}                         # no "paused" alert for a pause that was not made


def test_a_check_is_due_once_a_day_after_the_hour_and_the_next_is_the_following_18_00(tmp_path):
    m, state, _ = daily(tmp_path, Device())
    assert not m.due(datetime(2026, 10, 8, 17, 59)) and m.due(datetime(2026, 10, 8, 18, 0)) and m.due(datetime(2026, 10, 8, 23, 0))
    state.monitor["irrigation_monitor"] = {"checked": "2026-10-08"}
    assert not m.due(datetime(2026, 10, 8, 23, 0)) and not m.due(datetime(2026, 10, 9, 3, 0)) and m.due(datetime(2026, 10, 9, 18, 0))
    assert m.seconds_until_next(datetime(2026, 10, 8, 17, 0)) == 3600 and m.seconds_until_next(datetime(2026, 10, 8, 18, 0)) == 86400
    assert m.seconds_until_next(datetime(2026, 10, 8, 23, 30)) == 18.5 * 3600
    assert m.seconds_until_next(datetime(2026, 10, 3, 18, 0)) == 23 * 3600   # the clocks go forward that night: 23 real hours, not 24


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
    assert state.monitor["irrigation_monitor"]["checked"] == "2026-10-08"
    assert sleeps[1] == 86400                                                             # then until 18:00 tomorrow (now is exactly 18:00 here)


async def test_the_watchdog_closes_a_valve_that_is_still_open_after_the_run_and_survives_a_restart(tmp_path):
    clock, state = [1000.0], IrrigationState(tmp_path / "irrigation_state.json")
    device = Device(switch=True)
    dog = Watchdog(device, state, clock=lambda: clock[0])
    await dog.check()
    assert device.commands == []                                                          # nothing armed: the controller is not even asked
    dog.arm(10)
    clock[0] = 1000 + 600 + 29
    await dog.check()
    assert device.commands == [] and dog.off_at                                           # inside the grace period: the device's own countdown may still end it
    clock[0] = 1000 + 600 + 31
    reborn = Watchdog(device, IrrigationState(tmp_path / "irrigation_state.json"), clock=lambda: clock[0])    # irrigation-mcp restarted in the middle of the run
    await reborn.check()
    assert device.commands == [("switch", False)] and reborn.off_at is None


async def test_the_watchdog_leaves_a_closed_valve_alone_tries_again_on_failure_and_disarms_on_request(tmp_path):
    clock, state = [0.0], IrrigationState(tmp_path / "irrigation_state.json")
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
    assert sent == [] and "checked" not in state.monitor["irrigation_monitor"]               # nothing told for a pause that was not made
    await m.check(NOW)
    assert device.commands == [("delay", "24h")] and len(sent) == 1 and state.monitor["irrigation_monitor"]["checked"] == "2026-10-08"


async def test_the_irrigation_check_runs_on_the_forecast_last_read_when_the_service_cant_be_reached(caplog):
    from datetime import date
    from types import SimpleNamespace as NS
    from mcp.shared.exceptions import MCPError
    from lib.irrigation.ecobot import ServiceForecast, ServiceRain

    class Down:   # (ecobot-mcp unreachable: lib/upstream refuses with an MCPError)
        tz = TZ

        async def call_text(self, *a, **k):
            raise MCPError(-32603, "ecobot-mcp isn't reachable")

        async def resource(self, uri, wait=None):
            raise MCPError(-32603, "ecobot-mcp isn't reachable")
    forecast = ServiceForecast(Down())
    now = datetime(2026, 11, 10, 18, 0, tzinfo=TZ)
    forecast.days, forecast.fetched_at = [{"date": date(2026, 11, 11), "rain_1mm_pct": 70}], now.timestamp() - 3600   # (read an hour ago)
    forecast.now = lambda: now
    monitor = IrrigationMonitor(None, NS(monitor={}), None, TZ, station=ServiceRain(Down()), forecast=forecast)
    assert await monitor.rain_reason(now) == "70% chance of at least 1 mm tomorrow"
    assert "the measured rain is unavailable" in caplog.text and "using the one last read" in caplog.text


async def test_the_irrigation_check_is_timed_again_in_a_zone_read_later(tmp_path, monkeypatch):
    """The irrigation check's wait for 18:00 is cut short when the station's zone arrives, and timed again in it."""
    from types import SimpleNamespace as NS
    from zoneinfo import ZoneInfo
    utc = ZoneInfo("Etc/UTC")
    monitor = IrrigationMonitor(None, NS(monitor={"irrigation_monitor": {}}), None, utc, 18)
    waits = []

    def now_local(tz):
        return datetime(2026, 11, 10, 6, 0) if str(tz) == "Etc/UTC" else datetime(2026, 11, 10, 17, 0)   # (06:00 UTC: 17:00 there)
    monkeypatch.setattr(monitor_module, "now_local", now_local)
    timed = monitor.seconds_until_next
    monitor.seconds_until_next = lambda now: waits.append(timed(now)) or waits[-1]   # (the waits themselves are real)
    task = asyncio.create_task(monitor.run())
    try:
        await until(lambda: waits, what="the first wait")
        monitor.tz = TZ
        await until(lambda: len(waits) == 2, what="the wait timed again")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert waits == [12 * 3600, 3600]   # 12 hours to 18:00 UTC; then the hour to 18:00 in Melbourne
