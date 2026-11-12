"""irrigation-mcp (docs/ecobot-mcp.md 3.9, step 8): the irrigation controller's own MCP server. Its tools over the SDK's in-memory
transport with a fake controller (and once with the fake Tuya cloud, signed); the watchdog closing a run after a restart; the daily check
asking ecobot-mcp (in memory) for the rain and the forecast, pausing on either and leaving a delay someone set, and going on the forecast
last read when ecobot-mcp can't be reached; its alerts heard through a listen; its state copied once from the bot's; the token and the
Host checked over HTTP; `python -m irrigation_mcp` starting and stopping; and what it imports (no Telegram, no model client, no Pillow,
no source)."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import time
from datetime import datetime, timedelta

import httpx
import pytest
from mcp import Client

from lib import upstream
from lib.alert_log import AlertLog
from lib.irrigation import Tuya, Watchdog
from lib.irrigation import server as server_module
from lib.irrigation.check import IrrigationMonitor
from lib.irrigation.ecobot import EcobotWeather
from lib.irrigation.server import ALERTS_URI, TOOLS, IrrigationServer, http_app
from lib.irrigation.state import IrrigationState
from lib.mcp_http import Uvicorn, server_config
from lib.timeutil import now_local
from lib.upstream import VERSION, Upstream, http_transport
from tests.fakes import TUYA_SECRET, TUYA_STATUS, TZ, FakeController, FakeTuya, StandInService, tuya_config
from tests.served import ecobot_mcp

TOKENS = ("irrigation-s3cret", "phone-s3cret")


async def until(check, seconds: float = 10.0, what: str = "it"):
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


@pytest.fixture
def served(tmp_path):
    """irrigation-mcp's server over a fake controller, its watchdog keeping its state in tmp_path, its own alert log."""
    device = FakeController()
    state = IrrigationState(tmp_path / "irrigation_state.json")
    alerts = AlertLog(tmp_path / "irrigation_alerts.sqlite", TZ)
    server = IrrigationServer(device, Watchdog(device, state), alerts, tz=TZ)
    yield server
    alerts.close()


async def call(client, name, args=None):
    result = await client.call_tool(name, args or {})
    return result.is_error, json.loads(result.content[0].text)


# ---------- the tools ----------
async def test_the_four_tools_are_listed_with_their_hints_and_schemas(served):
    async with Client(served.server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert list(tools) == ["irrigation_status", "water", "stop", "delay"]
    assert tools["irrigation_status"].annotations.read_only_hint and not tools["irrigation_status"].annotations.destructive_hint
    for name in ("water", "stop", "delay"):
        assert tools[name].annotations.destructive_hint and not tools[name].annotations.read_only_hint, name
    assert tools["water"].input_schema["properties"]["minutes"] == {**TOOLS["water"][1]["properties"]["minutes"]}
    assert tools["water"].input_schema["properties"]["minutes"]["maximum"] == 60
    assert tools["delay"].input_schema["properties"]["duration"]["enum"] == ["24h", "48h", "72h", "cancel"]


async def test_status_water_stop_and_delay_act_on_the_controller_and_water_is_watched(served):
    device, dog = served.device, served.watchdog
    async with Client(served.server) as client:
        error, status = await call(client, "irrigation_status")
        assert not error and status == {"switch": False, "countdown": None, "weather_delay": "cancel", "paused": False,
                                         "battery_percentage": 100, "work_state": "idle"}            # (no off_at: nothing watched)
        before = time.time()
        error, out = await call(client, "water", {"minutes": 10})
        assert not error and out["done"] == "Watering for 10 minutes" and device.commands == [("water", 10)]
        assert before + 600 <= dog.off_at <= time.time() + 600                                         # the watchdog armed
        error, status = await call(client, "irrigation_status")
        assert status["switch"] is True and status["countdown"] == 600 and status["work_state"] == "manual"
        assert status["off_at"] == out["off_at"] == datetime.fromtimestamp(dog.off_at, TZ).isoformat(timespec="seconds")
        error, out = await call(client, "stop")
        assert not error and out == {"done": "Switched off"} and device.commands[-1] == ("switch", False) and dog.off_at is None
        error, out = await call(client, "delay", {"duration": "48h"})
        assert not error and out == {"done": "Paused 48h", "weather_delay": "48h"} and device.commands[-1] == ("delay", "48h")
        error, status = await call(client, "irrigation_status")
        assert status["paused"] is True and status["weather_delay"] == "48h" and "off_at" not in status
        error, out = await call(client, "delay", {"duration": "cancel"})
        assert out["done"] == "Unpaused" and not (await call(client, "irrigation_status"))[1]["paused"]


async def test_water_is_refused_while_a_weather_delay_is_on_saying_so(served):
    served.device.status_now["weather_delay"] = "24h"
    async with Client(served.server) as client:
        error, out = await call(client, "water", {"minutes": 5})
    assert error and "paused (weather delay 24h)" in out["error"] and "cancel" in out["error"] and out["refused"] == "paused"
    assert served.device.commands == [] and served.watchdog.off_at is None


@pytest.mark.parametrize("name, args, says", [
    ("water", {"minutes": 0}, "from 1 to 60"), ("water", {"minutes": 61}, "from 1 to 60"), ("water", {"minutes": "ten"}, "from 1 to 60"),
    ("water", {"minutes": True}, "from 1 to 60"), ("water", {"minutes": 2.5}, "from 1 to 60"), ("water", {}, "needs minutes"),
    ("water", {"minutes": 5, "zone": 2}, "not zone"), ("delay", {"duration": "96h"}, "one of 24h, 48h, 72h, cancel"),
    ("delay", {}, "needs duration"), ("stop", {"now": True}, "takes no arguments"), ("irrigation_status", {"x": 1}, "takes no arguments")])
async def test_wrong_arguments_are_refused_saying_what_is_wrong_and_nothing_is_sent(served, name, args, says):
    async with Client(served.server) as client:
        error, out = await call(client, name, args)
    assert error and says in out["error"] and out["refused"] == "arguments"
    assert served.device.commands == [] and served.watchdog.off_at is None


async def test_a_controller_that_cant_be_reached_is_a_tool_error_safe_to_show_and_a_lost_water_reply_is_still_watched(served, tmp_path):
    served.device.down = True
    async with Client(served.server) as client:
        error, out = await call(client, "irrigation_status")
        assert error and out == {"error": "The irrigation controller couldn't be reached: Tuya request failed (ConnectError)."}

        class Lost(FakeController):
            async def water(self, minutes):
                await super().water(minutes)                                   # the valve opened
                from lib.irrigation import IrrigationError
                raise IrrigationError("Tuya request failed (ReadTimeout)")     # but the reply never came
        served.device = served.watchdog.device = Lost()
        error, out = await call(client, "water", {"minutes": 5})
    assert error and "ReadTimeout" in out["error"] and served.watchdog.off_at is not None

    def refuse(request):   # (the real client: its message names no secret)
        raise httpx.ConnectError("no route to https://openapi.tuyaeu.com?secret=" + TUYA_SECRET)
    served.device = Tuya(tuya_config(tmp_path), transport=httpx.MockTransport(refuse))
    async with Client(served.server) as client:
        error, out = await call(client, "irrigation_status")
    assert error and TUYA_SECRET not in json.dumps(out) and "ConnectError" in out["error"]
    await served.device.close()


async def test_the_tools_over_the_tuya_cloud_send_signed_commands(tmp_path):
    fake = FakeTuya()
    device = Tuya(tuya_config(tmp_path), transport=httpx.MockTransport(fake))
    state = IrrigationState(tmp_path / "irrigation_state.json")
    server = IrrigationServer(device, Watchdog(device, state), None, tz=TZ)
    async with Client(server.server) as client:
        assert (await call(client, "irrigation_status"))[1]["battery_percentage"] == 100
        assert not (await call(client, "water", {"minutes": 3}))[0]
        assert not (await call(client, "stop"))[0]
        assert (await client.list_resources()).resources == []                                     # (no alert log: no alerts)
    assert [(m, p, b) for m, p, b, _ in fake.calls if m == "POST"] == [
        ("POST", "/v1.0/devices/dev1/commands", {"commands": [{"code": "countdown", "value": 180}, {"code": "switch", "value": True}]}),
        ("POST", "/v1.0/devices/dev1/commands", {"commands": [{"code": "switch", "value": False}]})]
    await device.close()


# ---------- the watchdog and the state ----------
async def test_a_run_started_through_the_tools_is_closed_by_the_watchdog_after_a_restart(served, tmp_path):
    async with Client(served.server) as client:
        await call(client, "water", {"minutes": 10})
    off_at = served.watchdog.off_at
    device = served.device                                         # the valve is still open (its own countdown failed)
    reborn = Watchdog(device, IrrigationState(tmp_path / "irrigation_state.json"), clock=lambda: off_at + 31)   # irrigation-mcp restarted
    assert reborn.off_at == off_at
    await reborn.check()
    assert device.commands[-1] == ("switch", False) and reborn.off_at is None
    assert json.loads((tmp_path / "irrigation_state.json").read_text()) == {"irrigation_watchdog": {}}


def bot_state(tmp_path, monitor: dict) -> str:
    path = tmp_path / "bot_state.json"
    path.write_text(json.dumps({"chats": {"1": {"title": "Rob"}}, "monitor": monitor, "charts": {}}))
    return path.read_text()


async def test_at_its_first_start_the_bots_watchdog_and_check_state_is_copied_once(tmp_path, caplog):
    caplog.set_level("INFO")
    off_at = time.time() - 100
    bots = bot_state(tmp_path, {"irrigation_watchdog": {"off_at": off_at}, "rain": {"since": 5},
                                "irrigation_monitor": {"checked": "2026-11-11", "paused": "2026-11-11", "battery_warned": "2026-11-10"}})
    state = IrrigationState(tmp_path / "irrigation_state.json", tmp_path / "bot_state.json")
    assert state.monitor == {"irrigation_watchdog": {"off_at": off_at},
                             "irrigation_monitor": {"checked": "2026-11-11", "paused": "2026-11-11", "battery_warned": "2026-11-10"}}
    assert json.loads((tmp_path / "irrigation_state.json").read_text()) == state.monitor     # (kept at once: copied this once only)
    assert (tmp_path / "bot_state.json").read_text() == bots                                    # the bot's file as it was
    assert caplog.text.count("copied from") == 1
    device = FakeController(switch=True)
    await Watchdog(device, state).check()                                                      # a run the old bot armed: closed
    assert device.commands == [("switch", False)]
    m = IrrigationMonitor(device, state, None, TZ, 18)
    assert not m.due(datetime(2026, 11, 11, 19, 0)) and m.due(datetime(2026, 11, 12, 18, 0))   # the bot's day is not checked again
    caplog.clear()
    again = IrrigationState(tmp_path / "irrigation_state.json", tmp_path / "bot_state.json")   # the next start: its own file
    assert again.monitor == {"irrigation_watchdog": {}, "irrigation_monitor": state.monitor["irrigation_monitor"]}
    assert "copied" not in caplog.text


def test_without_the_bots_entries_nothing_is_copied_not_even_at_a_later_start_and_a_damaged_bot_file_is_left_alone(tmp_path, caplog):
    caplog.set_level("INFO")
    mine = tmp_path / "irrigation_state.json"
    bot_state(tmp_path, {"rain": {"since": 5}})
    assert IrrigationState(mine, tmp_path / "bot_state.json").monitor == {}
    assert json.loads(mine.read_text()) == {}                                  # written at the first start, with nothing in it
    bot_state(tmp_path, {"irrigation_watchdog": {"off_at": 5.0}})              # (an old bot run since): not copied at a later start
    assert IrrigationState(mine, tmp_path / "bot_state.json").monitor == {} and "copied" not in caplog.text
    mine.unlink()
    assert IrrigationState(mine, tmp_path / "none.json").monitor == {}
    mine.unlink()
    (tmp_path / "bot_state.json").write_text("{not json")
    assert IrrigationState(mine, tmp_path / "bot_state.json").monitor == {}
    assert (tmp_path / "bot_state.json").read_text() == "{not json" and "copied" not in caplog.text


# ---------- the daily check, as ecobot-mcp's client ----------
class Ecobot:
    """ecobot-mcp in memory with stand-in tools (summary's rain total) and a forecast (ecobot://forecast), and irrigation-mcp's client of
    it (lib/irrigation/ecobot.EcobotWeather), connected."""

    def __init__(self, rain_mm=0.0, chances=(10, 20)):
        self.rain_mm, self.chances, self.fetched = rain_mm, list(chances), now_local(TZ) - timedelta(hours=1)
        self.service = StandInService(summary=lambda args, meta: {"rain_total_mm": self.rain_mm, "period": args.get("period")})

    async def forecast(self) -> dict:
        today = now_local(TZ).date()
        return {"place": "Home", "fetched_at": self.fetched.isoformat(timespec="seconds"),
                "days": [{"date": (today + timedelta(days=i)).isoformat(), "rain_1mm_pct": c} for i, c in enumerate(self.chances)]}


@contextlib.asynccontextmanager
async def connected(ecobot: Ecobot):
    async with ecobot_mcp(tools=ecobot.service, sources=["Ecowitt", "Forecast"]) as remote:
        remote.server.forecast = ecobot.forecast
        weather = EcobotWeather(remote.server.server, tz=None)
        zones = []
        weather.on_zone = zones.append
        task = asyncio.create_task(weather.run())
        try:
            await asyncio.wait_for(weather.listening.wait(), 10)
            await until(lambda: weather.forecast.days, what="the forecast")
            weather.zones = zones
            yield weather
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await weather.close()


def checker(tmp_path, device, weather):
    state, sent = IrrigationState(tmp_path / "irrigation_state.json"), []

    async def notify(text, kind):
        sent.append((kind, text))
    return IrrigationMonitor(device, state, notify, weather.tz, 18, weather.rain, weather.forecast), state, sent


@pytest.mark.parametrize("rain_mm, chances, reason", [
    (3.0, (0, 0), "3 mm of rain in the last 24 hours"),
    (0.0, (10, 80), "80% chance of at least 1 mm tomorrow"),
    (0.4, (55, 0), "55% chance of at least 1 mm today"),
    (0.9, (10, 49), None)])
async def test_the_check_asks_ecobot_mcp_for_the_rain_and_the_forecast_and_pauses_on_either(tmp_path, rain_mm, chances, reason):
    ecobot = Ecobot(rain_mm, chances)
    async with connected(ecobot) as weather:
        assert str(weather.tz) == str(TZ) and [str(z) for z in weather.zones] == [str(TZ)]   # the station's zone, read
        device = FakeController()
        m, state, sent = checker(tmp_path, device, weather)
        await m.check(now_local(TZ))
    assert ("summary", {"readings": ["rain"], "period": "last 24 hours"}) in ecobot.service.calls
    assert device.commands == ([("delay", "24h")] if reason else [])
    assert sent == ([("irrigation_pause", f"☔ Irrigation paused for 24 hours: {reason}.")] if reason else [])
    assert state.monitor["irrigation_monitor"]["checked"] == now_local(TZ).date().isoformat()


async def test_the_check_leaves_a_delay_someone_set(tmp_path):
    async with connected(Ecobot(5.0, (90, 90))) as weather:
        device = FakeController(weather_delay="48h")
        m, _, sent = checker(tmp_path, device, weather)
        await m.check(now_local(TZ))
    assert device.commands == [] and sent == []


async def test_with_ecobot_mcp_unreachable_the_check_goes_on_the_forecast_last_read_without_the_measured_rain(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(upstream, "CONNECT_WAIT", 0.1)
    ecobot = Ecobot(0.0, (10, 80))
    async with connected(ecobot) as weather:
        pass                                                                   # the forecast read; then ecobot-mcp goes away
    assert weather.upstream.client is None and weather.forecast.days
    ecobot.service.calls.clear()
    device = FakeController()
    m, _, sent = checker(tmp_path, device, weather)
    await m.check(now_local(TZ))
    assert ecobot.service.calls == [] and device.commands == [("delay", "24h")]
    assert sent == [("irrigation_pause", "☔ Irrigation paused for 24 hours: 80% chance of at least 1 mm tomorrow.")]
    assert "the measured rain is unavailable" in caplog.text and "using the one last read" in caplog.text
    never = EcobotWeather("http://127.0.0.1:9/mcp", tz=TZ)                     # never reached: no forecast to go on
    (tmp_path / "fresh").mkdir()
    m, _, sent = checker(tmp_path / "fresh", FakeController(), never)
    await m.check(now_local(TZ))
    assert sent == [] and "there is none to go on" in caplog.text


# ---------- its alerts ----------
async def test_its_alerts_are_heard_through_a_listen_and_read_in_ecobot_alerts_shape(served, tmp_path):
    publishing = asyncio.create_task(served.run())
    heard, listening = [], asyncio.Event()

    async def on_event(event):
        heard.append(str(event.uri))

    async def on_listening():
        listening.set()
    up = Upstream(served.server, on_event=on_event, on_listening=on_listening)
    supervisor = asyncio.create_task(up.run())
    try:
        await asyncio.wait_for(listening.wait(), 10)
        served.device.status_now["battery_percentage"] = 3
        m = IrrigationMonitor(served.device, served.watchdog.state, served.notify, TZ, 18)
        await m.check(now_local(TZ))
        await until(lambda: heard == [ALERTS_URI], what="the alert's ResourceUpdated")
        client = await up.get()
        listed = (await client.list_resources()).resources
        templates = (await client.list_resource_templates()).resource_templates
        data = json.loads((await client.read_resource(ALERTS_URI)).contents[0].text)
        later = json.loads((await client.read_resource(f"{ALERTS_URI}?since=1")).contents[0].text)
        with pytest.raises(Exception, match="whole number"):
            await client.read_resource(f"{ALERTS_URI}?since=x")
        with pytest.raises(Exception, match="No resource"):
            await client.read_resource("irrigation://nothing")
    finally:
        supervisor.cancel()
        publishing.cancel()
        await asyncio.gather(supervisor, publishing, return_exceptions=True)
    assert [str(r.uri) for r in listed] == [ALERTS_URI] and templates[0].uri_template == "irrigation://alerts{?since,expired}"
    assert data["last_id"] == 1 and data["identity"] == served.alert_log.identity and later["alerts"] == []
    (alert,) = data["alerts"]
    assert alert["kind"] == "irrigation_battery" and alert["title"] == "Irrigation battery low" and alert["id"] == 1
    assert alert["text"] == "🪫 The irrigation battery is at 3%. Replace or recharge it soon, or watering will stop."
    assert datetime.fromisoformat(alert["expires"]) - datetime.fromisoformat(alert["time"]) == timedelta(hours=24)


# ---------- over HTTP ----------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.asynccontextmanager
async def serving(app, port):
    web = Uvicorn(server_config(app, "127.0.0.1", port, 0.25))
    task = asyncio.create_task(web.serve())
    await until(lambda: web.started or task.done(), what="uvicorn to start")
    try:
        yield web
    finally:
        web.should_exit = True
        await task


async def test_a_request_without_the_token_with_a_wrong_or_revoked_one_or_to_a_host_not_allowed_is_refused(served, caplog):
    caplog.set_level("INFO", logger="lib.irrigation.server.requests")
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    app = http_app(served, tokens=TOKENS, allowed_hosts=("127.0.0.1", "irrigation.lan"))
    body = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": VERSION}}}

    async def post(token=None, host=None) -> int:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Mcp-Protocol-Version": VERSION,
                   "Mcp-Method": "server/discover", **({"Authorization": f"Bearer {token}"} if token else {}), **({"Host": host} if host else {})}
        async with httpx.AsyncClient() as http:
            return (await http.post(url, headers=headers, json=body)).status_code
    async with serving(app, port):
        assert await post() == 401 and await post("nope") == 401 and await post(TOKENS[0].upper()) == 401
        assert await post(TOKENS[1], host="evil.example") == 421
        assert await post(TOKENS[1], host=f"irrigation.lan:{port}") != 421
        async with Client(http_transport(url, TOKENS[1]), mode=VERSION) as client:
            assert not (await client.call_tool("water", {"minutes": 4})).is_error
        app.state.tokens.revoke(TOKENS[1])
        assert await post(TOKENS[1]) == 401
        async with Client(http_transport(url, TOKENS[0]), mode=VERSION) as client:
            assert json.loads((await client.call_tool("irrigation_status", {})).content[0].text)["switch"] is True
    lines = [r.getMessage() for r in caplog.records if r.name == "lib.irrigation.server.requests"]
    assert any("POST /mcp tools/call water 4 -> 200" in line for line in lines), lines
    assert any("-> 401, no token, or one that isn't in AUTH_TOKENS" in line for line in lines), lines
    assert any("-> 421, its Host isn't in IRRIGATION_ALLOWED_HOSTS (Host evil.example)" in line for line in lines), lines
    assert not any(t in line for t in TOKENS for line in lines)


async def test_python_m_irrigation_mcp_starts_answers_with_the_generated_token_and_stops_on_sigterm(tmp_path, monkeypatch, caplog):
    """A deploy check: main() with the fake Tuya cloud, AUTH_TOKENS unset (the token generated in the data folder, the one ecobot-mcp
    would use), ecobot-mcp not reachable: it starts all the same, answers a tool call through IRRIGATION_ALLOWED_HOSTS' name, keeps one
    process per data folder, and SIGTERM closes it."""
    import irrigation_mcp.__main__ as main_module
    from lib import auth_token
    from lib.config import Config
    caplog.set_level("INFO")
    port = free_port()
    cfg = tuya_config(tmp_path, irrigation_bind=("127.0.0.1", port), irrigation_allowed_hosts=("irrigation.lan",),
                      irrigation_ecobot_mcp_url="http://127.0.0.1:9/mcp")
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls, bot=True, program=None: cfg))
    monkeypatch.setattr(main_module, "STARTUP_WAIT", 0.2)
    monkeypatch.setattr(main_module, "SHUTDOWN_SECONDS", 0.25)
    fake = FakeTuya()
    monkeypatch.setattr(Tuya, "__init__", lambda self, c, transport=None, orig=Tuya.__init__: orig(self, c, httpx.MockTransport(fake)))
    answered = {}

    async def ask_then_stop():
        await until(lambda: "MCP: http://127.0.0.1:" in caplog.text, 30, "irrigation-mcp to listen")
        token = auth_token.read(tmp_path)
        url = f"http://127.0.0.1:{port}/mcp"

        @contextlib.asynccontextmanager
        async def through_the_proxy():
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared._httpx_utils import create_mcp_http_client
            async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}", "Host": "irrigation.lan"}) as http:
                async with streamable_http_client(url, http_client=http) as streams:
                    yield streams
        async with Client(through_the_proxy(), mode=VERSION) as client:
            answered["status"] = json.loads((await client.call_tool("irrigation_status", {})).content[0].text)
        from lib.mcp_http import hold_folder
        with contextlib.ExitStack() as second:   # (a second on the same folder stops)
            with pytest.raises(SystemExit, match="Another irrigation-mcp"):
                hold_folder(tmp_path, main_module.LOCK, "irrigation-mcp", "python -m irrigation_mcp", second)
        await until(lambda: "the daily check at" in caplog.text, 10, "the daily check to start")   # (after STARTUP_WAIT)
        os.kill(os.getpid(), signal.SIGTERM)
    asker = asyncio.create_task(ask_then_stop())
    await main_module.main()
    await asker
    assert answered["status"]["battery_percentage"] == 100 and answered["status"]["paused"] is False
    assert f"the token in {tmp_path / 'auth_token'}" in caplog.text and "ecobot-mcp at http://127.0.0.1:9/mcp isn't reachable yet" in caplog.text
    assert "the daily check at 18:00" in caplog.text and "Telegram" not in caplog.text
    assert (tmp_path / "irrigation.lock").exists()


async def test_at_start_the_watchdog_and_the_tools_dont_wait_for_ecobot_mcp(tmp_path, monkeypatch, caplog):
    """A restart mid-run with ecobot-mcp unreachable: the run left armed (past its end, the valve still open) is closed at once, and the
    tools answer, while the daily check still waits for ecobot-mcp's time zone (STARTUP_WAIT, long here); SIGTERM during that wait stops it."""
    import irrigation_mcp.__main__ as main_module
    from lib import auth_token
    from lib.config import Config
    caplog.set_level("INFO")
    port = free_port()
    cfg = tuya_config(tmp_path, irrigation_bind=("127.0.0.1", port), irrigation_ecobot_mcp_url="http://127.0.0.1:9/mcp")
    (tmp_path / "irrigation_state.json").write_text(json.dumps({"irrigation_watchdog": {"off_at": time.time() - 120}}))
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls, bot=True, program=None: cfg))
    monkeypatch.setattr(main_module, "STARTUP_WAIT", 60)
    monkeypatch.setattr(main_module, "SHUTDOWN_SECONDS", 0.25)
    fake = FakeTuya(status=[{"code": "switch", "value": True}, *TUYA_STATUS[1:]])
    monkeypatch.setattr(Tuya, "__init__", lambda self, c, transport=None, orig=Tuya.__init__: orig(self, c, httpx.MockTransport(fake)))
    answered = {}

    async def ask_then_stop():
        started = time.monotonic()
        await until(lambda: any(p.endswith("/commands") for _, p, _, _ in fake.calls), 10, "the watchdog to close the valve")
        await until(lambda: "MCP: http://127.0.0.1:" in caplog.text, 10, "irrigation-mcp to listen")
        async with Client(http_transport(f"http://127.0.0.1:{port}/mcp", auth_token.read(tmp_path)), mode=VERSION) as client:
            answered["status"] = json.loads((await client.call_tool("irrigation_status", {})).content[0].text)
        answered["after"] = time.monotonic() - started
        os.kill(os.getpid(), signal.SIGTERM)
    asker = asyncio.create_task(ask_then_stop())
    await asyncio.wait_for(main_module.main(), 30)
    await asker
    commands = [body for _, p, body, _ in fake.calls if p.endswith("/commands")]
    assert commands == [{"commands": [{"code": "switch", "value": False}]}] and "valve was still open" in caplog.text
    assert "battery_percentage" in answered["status"] and "off_at" not in answered["status"] and answered["after"] < 20
    assert "the daily check at" not in caplog.text                     # (it was still waiting for ecobot-mcp when stopped)
    assert json.loads((tmp_path / "irrigation_state.json").read_text())["irrigation_watchdog"] == {}


def test_the_watchdog_checks_again_when_the_run_is_due_not_a_whole_minute_later(tmp_path):
    from lib.irrigation.state import IrrigationState
    from lib.irrigation.watchdog import CHECK_SECONDS, GRACE_SECONDS
    now = [1000.0]
    watchdog = Watchdog(FakeController(), IrrigationState(tmp_path / "irrigation_state.json"), clock=lambda: now[0])
    assert watchdog.next_check() == CHECK_SECONDS                                     # nothing armed
    watchdog.arm(1)
    assert watchdog.next_check() == CHECK_SECONDS                                     # due in 90 s
    now[0] += 50
    assert watchdog.next_check() == 60 + GRACE_SECONDS - 50                           # due in 40 s: then
    now[0] += 39.5
    assert watchdog.next_check() == 1.0                                               # (never sooner than a second)
    now[0] += 10
    assert watchdog.next_check() == CHECK_SECONDS                                     # past it (its check failed): the usual minute


def test_without_the_controllers_settings_irrigation_mcp_stops_saying_which_with_the_status_its_unit_doesnt_restart(monkeypatch,
                                                                                                                    tmp_path, caplog):
    """Its unit restarts it on a failure, but not on this one (RestartPreventExitStatus=78): until the settings change it would only
    fail again, every 10 seconds, for good (ecobot.service wants irrigation-mcp.service, so an installed one is started at every boot)."""
    import irrigation_mcp.__main__ as main_module
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IRRIGATION_TUYA_DEVICE_ID", "dev")
    with pytest.raises(SystemExit) as stopped:
        asyncio.run(main_module.main())
    assert stopped.value.code == main_module.SETTINGS_EXIT == 78
    assert "IRRIGATION_TUYA_CLIENT_ID, IRRIGATION_TUYA_CLIENT_SECRET not set" in caplog.text


def test_it_sends_ecobot_mcp_the_first_of_auth_tokens_else_the_generated_one(tmp_path):
    from irrigation_mcp.__main__ import ecobot_token
    from lib import auth_token
    assert ecobot_token(tuya_config(tmp_path, auth_tokens=("first", "second"))) == "first"
    read = ecobot_token(tuya_config(tmp_path))
    assert read() is None                                       # (none yet: read again for each connection)
    token = auth_token.ensure(tmp_path)
    assert read() == token


# ---------- what it imports ----------
NOT_IRRIGATIONS = ("telegram", "openai", "PIL", "lib.ecowitt", "lib.airgradient", "lib.forecast", "lib.pollen", "lib.storage",
                   "lib.service", "lib.mcp_server", "lib.bot", "lib.weather", "lib.charts", "lib.draw", "lib.gauges", "ecobot_mcp", "ecobot")


def test_irrigation_mcp_imports_no_telegram_no_model_client_no_pillow_and_no_source(tmp_path):
    """irrigation-mcp runs beside the valve on its own packages: building everything it runs (the server, the watchdog, the check, its
    state, its client of ecobot-mcp, the HTTP app) imports nothing of the bot's, nothing that draws, and none of ecobot-mcp's sources,
    readings file or tools."""
    import subprocess
    import sys
    from lib.config import ROOT
    code = ("import sys, irrigation_mcp, irrigation_mcp.__main__; from zoneinfo import ZoneInfo; "
            "from lib.alert_log import AlertLog; from lib.irrigation import Tuya, Watchdog; "
            "from lib.irrigation.check import IrrigationMonitor; from lib.irrigation.ecobot import EcobotWeather; "
            "from lib.irrigation.server import IrrigationServer, http_app; from lib.irrigation.state import IrrigationState; "
            "from lib.config import Config; from lib.warm import Tasks, every; "
            f"folder = {str(tmp_path)!r}; "
            "cfg = Config.from_env(program='irrigation'); tz = ZoneInfo('UTC'); "
            "state = IrrigationState(cfg.irrigation_state_path, cfg.state_path); device = Tuya(cfg); dog = Watchdog(device, state); "
            "w = EcobotWeather(cfg.irrigation_ecobot_mcp_url, 't', tz); log = AlertLog(cfg.irrigation_alert_log_path, tz); "
            "s = IrrigationServer(device, dog, log, tz=tz); IrrigationMonitor(device, state, s.notify, tz, 18, w.rain, w.forecast); "
            "http_app(s, tokens=('t',)); "
            f"print(sorted(m for m in sys.modules if m.split('.')[0] in {NOT_IRRIGATIONS!r} or m.startswith(tuple(n + '.' for n in {NOT_IRRIGATIONS!r}))))")
    env = {**os.environ, "DATA_DIR": str(tmp_path), "IRRIGATION_TUYA_DEVICE_ID": "d", "IRRIGATION_TUYA_CLIENT_ID": "c",
           "IRRIGATION_TUYA_CLIENT_SECRET": "s"}
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120, env=env)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "[]"


def test_the_server_module_names_what_it_runs():
    assert set(server_module.ALERTS) == {"irrigation_battery", "irrigation_pause"} == set(IrrigationMonitor.KINDS)
    assert logging.getLogger("lib.irrigation.server.requests") is server_module.requests_log
