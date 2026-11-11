"""The bot as the service's client over MCP (WEATHER_MCP_URL: lib/weather.RemoteWeather, docs/mcp-service.md 3.8 and step 5), against the
real server (lib/mcp_server.EcobotServer): over the SDK's in-memory transport for a question answered with the remote tools, a chart drawn
from a remote result with its buttons, an alert delivered through the listen and the irrigation check's inputs; under uvicorn on a localhost
port (a connection can fail there) for a dropped connection rebuilt with the alerts after the last id read once, the service down (the bot
says so), and ecobot.main() as a client. The path with WEATHER_MCP_URL unset is the rest of the suite's."""

import asyncio
import json
import time
from types import SimpleNamespace as NS

import pytest

from lib import upstream
from lib import model_tools, weather as weather_module
from lib.alert_log import AlertLog
from lib.alerts.notify import AlertDelivery, AlertState, Notifier
from lib.bot import SERVICE_DOWN, Bot
from lib.llm import Agent
from lib.mcp_server import EcobotServer, http_app
from lib.service.context import Context
from lib.weather import RemoteWeather, ServiceForecast, ServiceRain, ServiceUnreachable
from tests import evalworld
from tests.fakes import TZ
from tests.test_mcp_listen import Serving, free_port, until

TOKEN = "s3cret"


def at_eval_now(patch: pytest.MonkeyPatch):
    evalworld.at_eval_now(patch, __name__)


@pytest.fixture(scope="module")
def eval_archive(tmp_path_factory):
    path = tmp_path_factory.mktemp("remote-eval-now")
    with pytest.MonkeyPatch.context() as patch:
        at_eval_now(patch)
        return asyncio.run(evalworld.build_archive(path))


@pytest.fixture
async def world(tmp_path, eval_archive, monkeypatch):
    """The real server over the four sources (faked, the clock at the evals' NOW) and an alert log."""
    at_eval_now(monkeypatch)
    ctx = await evalworld.real_context(tmp_path, eval_archive)
    alerts = AlertLog(tmp_path / "alerts.sqlite", ctx.zone)
    server = EcobotServer(ctx, alerts)
    yield server
    alerts.close()
    await evalworld.close(ctx)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """Reconnections and waits in tenths of a second, not seconds."""
    monkeypatch.setattr(upstream, "BACKOFF", (0.1,))
    monkeypatch.setattr(weather_module, "CALL_WAIT", 0.3)


@pytest.fixture
async def connected(world):
    """A RemoteWeather over the server in memory, its supervisor running and listening."""
    remote = RemoteWeather(world.server, tz=None)
    task = asyncio.create_task(remote.run())
    await asyncio.wait_for(remote.listening.wait(), 10)
    yield remote
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await remote.close()


class Telegram:
    """What the bot sends, kept."""
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return NS(photo=None)

    async def send_photo(self, chat_id, photo, **kw):
        self.sent.append((chat_id, kw.get("caption")))
        return NS(photo=(NS(file_id="small"), NS(file_id="big")))


def a_chat(kind="private"):
    """A message in a chat, its replies kept (text, or (caption, buttons) for a photo)."""
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)
        return NS(message_id=8)

    async def reply_photo(photo, caption=None, reply_markup=None, **kw):
        replies.append((caption, reply_markup))
        return NS(message_id=9)

    async def nothing(*a, **k):
        pass
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text, reply_photo=reply_photo,
             chat=NS(type=kind, title="g" if kind != "private" else None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    context = NS(bot=NS(send_chat_action=nothing, send_message_draft=nothing, id=99))
    return update, context, replies


class Model:
    """A model that makes `calls` (name, arguments) in its first step, then answers `answer`; what it was sent is kept."""

    def __init__(self, calls, answer="It's 14.2 °C."):
        self.calls, self.answer, self.seen = calls, answer, []
        self.chat = NS(completions=self)

    async def create(self, **kw):
        self.seen.append(kw)
        if len(self.seen) == 1 and self.calls:
            made = [NS(id=str(i), function=NS(name=name, arguments=json.dumps(args))) for i, (name, args) in enumerate(self.calls)]
            return NS(choices=[NS(message=NS(content="", tool_calls=made))], usage=None)
        return NS(choices=[NS(message=NS(content=self.answer, tool_calls=None))], usage=None)


# ---------- in memory ----------
async def test_a_question_is_answered_with_the_remote_tools_and_the_instructions_from_discover(connected, world, alert_state):
    model = Model([("current", {"readings": ["temperature"]})])
    bot = Bot(NS(tz=TZ), Agent(model, "m", model_tools.tools(connected)), [], alert_state, service=connected)
    update, context, replies = a_chat("group")   # (no draft streamed: the model's answer comes whole)
    await bot.respond(update, context, "how warm is it outside?")
    first = model.seen[0]
    assert [t["function"]["name"] for t in first["tools"]] == [t.name for t in world.service]   # the service's ten, as listed
    assert world.server.instructions and world.server.instructions in first["messages"][0]["content"]   # discover's instructions
    told = json.loads(model.seen[1]["messages"][-1]["content"])
    assert told == json.loads(await world.service["current"].call({"readings": ["temperature"]}))   # the remote tool's own answer
    assert replies == ["It's 14.2 °C."]
    assert bot.names == {"Ecowitt", "AirGradient", "Forecast", "Pollen"} and bot.sources == []   # (from ecobot://station; no sources here)
    assert bot._catalogue().names == world.ctx.readings().names


async def test_a_chart_is_drawn_from_a_remote_result_with_its_period_buttons(connected, alert_state, monkeypatch):
    drawn = []
    monkeypatch.setattr("lib.bot.Bot._draw", lambda self, c, times=None: drawn.append(c) or (b"png", c["title"], 0))
    bot = Bot(NS(tz=TZ), Agent(None, "m", model_tools.tools(connected)), [], alert_state, service=connected)
    update, context, replies = a_chat("group")
    await bot.respond(update, context, "Humidity chart 7d")     # (a shortcut: a fixed summary call, no model)
    (caption, markup), = replies
    (chart,) = drawn
    assert chart["drawn_by"]["tool"] == "summary" and chart["drawn_by"]["arguments"]["period"] == "7d"
    assert len(max((s["points"] for p in chart["panels"] for s in p["series"]), key=len)) > 150   # (asked as drawn, not 150 points)
    assert [b.callback_data for b in markup.inline_keyboard[0]] == ["pd:1d", "pd:7d", "pd:1m", "pd:3m", "pd:1y"]
    assert bot.charted.get(1, 9)["drawn_by"] == chart["drawn_by"]


async def test_an_alert_reaches_the_subscribed_chats_through_the_listen(connected, world, tmp_path):
    publishing = asyncio.create_task(world.watch_alerts())
    state = AlertState(tmp_path / "bot_state.json")
    state.add_chat(1, "private")
    telegram = Telegram()
    delivery = asyncio.create_task(AlertDelivery(connected.alerts, Notifier(telegram, state), state, TZ).run())
    try:
        await until(lambda: state.read is not None, what="the delivery to begin")
        world.alert_log.append("rain", "🌧️ It's started raining: 0.4 mm so far", "Rain started")   # (no expiry: the clock here is the evals')
        await until(lambda: telegram.sent, what="the alert")
    finally:
        for task in (publishing, delivery):
            task.cancel()
        await asyncio.gather(publishing, delivery, return_exceptions=True)
    assert telegram.sent == [(1, "🌧️ It's started raining: 0.4 mm so far")] and state.read == 1


async def test_the_irrigation_check_reads_the_measured_rain_and_the_forecast_from_the_service(connected, world):
    rain = await ServiceRain(connected).rain_mm(24)
    summary = json.loads(await world.service["summary"].call({"readings": ["rain"], "period": "last 24 hours"}))
    assert rain == summary["rain_total_mm"]
    await world.ctx.forecast.warm()   # (the service fetches its forecast)
    forecast = connected.forecast
    await forecast.refresh()          # (as the check does; the listen's ResourceUpdated does it too)
    assert forecast.days
    assert [d["date"] for d in forecast.days] == [d["date"] for d in world.ctx.forecast.days]
    assert forecast.fetched_at == pytest.approx(world.ctx.forecast.fetched_at, abs=1)


async def test_the_irrigation_check_runs_on_the_forecast_last_read_when_the_service_cant_be_reached(caplog):
    from datetime import date, datetime
    from lib.alerts.irrigation import IrrigationMonitor

    class Down:
        tz = TZ

        async def call_text(self, *a, **k):
            raise ServiceUnreachable()

        async def resource(self, uri, wait=None):
            raise ServiceUnreachable()
    forecast = ServiceForecast(Down())
    now = datetime(2026, 11, 10, 18, 0, tzinfo=TZ)
    forecast.days, forecast.fetched_at = [{"date": date(2026, 11, 11), "rain_1mm_pct": 70}], now.timestamp() - 3600   # (read an hour ago)
    forecast.now = lambda: now
    monitor = IrrigationMonitor(None, NS(monitor={}), None, TZ, station=ServiceRain(Down()), forecast=forecast)
    assert await monitor.rain_reason(now) == "70% chance of at least 1 mm tomorrow"
    assert "the measured rain is unavailable" in caplog.text and "using the one last read" in caplog.text


# ---------- under uvicorn ----------
@pytest.fixture
def bare(tmp_path):
    """A server with no sources (the alerts and their listen are what matter) and its alert log."""
    alerts = AlertLog(tmp_path / "alerts.sqlite", TZ)
    yield EcobotServer(Context(tz=TZ), alerts)
    alerts.close()


async def test_a_dropped_connection_is_rebuilt_and_the_alerts_after_the_last_id_are_read_once(bare, tmp_path):
    port = free_port()
    remote = RemoteWeather(f"http://127.0.0.1:{port}/mcp", TOKEN, TZ)
    state = AlertState(tmp_path / "bot_state.json")
    state.add_chat(1, "private")
    telegram = Telegram()
    publishing = asyncio.create_task(bare.watch_alerts())
    supervisor = asyncio.create_task(remote.run())
    delivery = asyncio.create_task(AlertDelivery(remote.alerts, Notifier(telegram, state), state, TZ).run())
    texts = lambda: [text for _, text in telegram.sent]
    alerts = bare.alert_log
    try:
        async with Serving(http_app(bare, tokens=(TOKEN,)), port) as first:
            await until(lambda: state.read is not None and bare.listen._streams, what="the listen")
            alerts.append("rain", "first", "Rain started", expires=time.time() + 3600)
            await until(lambda: texts() == ["first"], what="alert 1")
            await first.stop()                                              # the service goes away: the listen is lost
        await until(lambda: not remote.upstream.connected.is_set(), what="the bot to notice")
        assert not await remote.ready(0.1)
        alerts.append("gusts", "missed", "Gusts", expires=time.time() + 3600)   # sent while it was down
        alerts.append("uv", "stale", "UV", expires=time.time() - 1)             # ... and expired already: not news
        async with Serving(http_app(bare, tokens=(TOKEN,)), port):
            await until(lambda: texts() == ["first", "missed"], what="the alert missed, read after the last id")
            await until(lambda: bare.listen._streams, what="the listen again")
            alerts.append("temps", "after", "Temperatures", expires=time.time() + 3600)
            await until(lambda: texts() == ["first", "missed", "after"], what="alert 4 on the new listen")
            await asyncio.sleep(0.3)
    finally:
        for task in (delivery, supervisor, publishing):
            task.cancel()
        await asyncio.gather(delivery, supervisor, publishing, return_exceptions=True)
    assert texts() == ["first", "missed", "after"] and state.read == 4    # each once, the expired one skipped


async def test_the_service_back_on_a_fresh_alert_log_while_the_bot_runs_has_its_new_alerts_delivered(tmp_path, caplog):
    """The service stops and comes back on a new alerts.sqlite (deleted, moved, redeployed: its ids start again at 1) while the bot runs:
    delivery goes on in the new log with what came after the old one was last read (one logged while it was down included), not after
    the old last id (3, which the new ids would take weeks to pass). An event of the new log from before then is not news."""
    port = free_port()
    path = tmp_path / "service" / "alerts.sqlite"
    path.parent.mkdir()
    old = AlertLog(path, TZ)
    remote = RemoteWeather(f"http://127.0.0.1:{port}/mcp", TOKEN, TZ)
    state = AlertState(tmp_path / "bot_state.json")
    state.add_chat(1, "private")
    telegram = Telegram()
    supervisor = asyncio.create_task(remote.run())
    delivery = asyncio.create_task(AlertDelivery(remote.alerts, Notifier(telegram, state), state, TZ).run())
    texts = lambda: [text for _, text in telegram.sent]
    tasks = [delivery, supervisor]
    try:
        first = EcobotServer(Context(tz=TZ), old)
        tasks.append(asyncio.create_task(first.watch_alerts()))
        async with Serving(http_app(first, tokens=(TOKEN,)), port):
            await until(lambda: state.read is not None and first.listen._streams, what="the listen")
            for text in ("one", "two", "three"):
                old.append("rain", text, "Rain", expires=time.time() + 3600)
            await until(lambda: texts() == ["one", "two", "three"] and state.read == 3, what="the old log's alerts")
            old_identity = old.identity
        old.close()
        for leftover in path.parent.iterdir():   # (alerts.sqlite and its -wal, -shm)
            leftover.unlink()
        new = AlertLog(path, TZ)
        assert new.identity != old_identity
        new.append("uv", "old news", "UV", expires=time.time() + 3600, time=time.time() - 3600)   # (from before the bot last read)
        new.append("gusts", "while down", "Gusts", expires=time.time() + 3600)
        second = EcobotServer(Context(tz=TZ), new)
        tasks.append(asyncio.create_task(second.watch_alerts()))
        async with Serving(http_app(second, tokens=(TOKEN,)), port):
            await until(lambda: texts()[3:] == ["while down"], what="the new log's alert logged while it was down")
            await until(lambda: second.listen._streams, what="the listen again")
            new.append("temps", "after", "Temperatures", expires=time.time() + 3600)
            await until(lambda: texts()[3:] == ["while down", "after"], what="the new log's next alert")
            await asyncio.sleep(0.3)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await remote.close()
    assert texts() == ["one", "two", "three", "while down", "after"]
    assert (state.read, state.read_log) == (3, new.identity) and AlertState(state.path).read_log == new.identity
    assert "is another than the one the last alert delivered (3) is from" in caplog.text
    new.close()


async def test_with_the_service_down_the_bot_says_so_and_carries_on(alert_state, caplog):
    remote = RemoteWeather(f"http://127.0.0.1:{free_port()}/mcp", TOKEN, TZ)   # (nothing listens there)
    supervisor = asyncio.create_task(remote.run())
    try:
        model = Model([("current", {})])
        bot = Bot(NS(tz=TZ), Agent(model, "m", model_tools.tools(remote)), [], alert_state, service=remote)
        for text in ("how warm is it?", "weather now", "Humidity chart 7d"):
            update, context, replies = a_chat()
            await bot.respond(update, context, text)
            assert replies == [SERVICE_DOWN], text
        assert model.seen == []                                            # (no model call for an answer it couldn't get)
        assert model_tools.tools(remote).schemas == []                     # (no tools listed while it has not been reached)
        with pytest.raises(ServiceUnreachable):
            await remote.call_text("current", {}, {"priority": "background"})   # (a call nobody waits for is refused at once)
        assert "isn't reachable" in caplog.text
    finally:
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)


async def test_main_as_a_client_runs_no_sources_and_answers_from_the_service(bare, tmp_path, monkeypatch, caplog):
    """ecobot.main() with WEATHER_MCP_URL: no source is started (no keys are set), the service's station names the time zone, the
    bot draws its keyboard's charts ahead, and an alert the service logs reaches a chat."""
    import os
    import signal
    import ecobot
    from tests.fakes import config
    from tests.test_startup import FakeApp, FakeBuilder
    caplog.set_level("INFO")
    port = free_port()
    cfg = config(tmp_path, weather_mcp_url=f"http://127.0.0.1:{port}/mcp", weather_mcp_token=TOKEN, ecowitt_api_key="",
                 ecowitt_app_key="", airgradient_token="", airgradient_location="", state_path=tmp_path / "bot" / "bot_state.json")
    (tmp_path / "bot").mkdir()
    AlertState(cfg.state_path).add_chat(5, "private")
    app = FakeApp()
    monkeypatch.setattr(ecobot._format, "width", ecobot._format.width)   # (main sets LOG_WIDTH's: put back after; the levels: conftest)
    drawn = []

    async def note(self, tg_bot):
        drawn.append(tg_bot)
    monkeypatch.setattr(ecobot.Bot, "draw_buttons_ahead", note)
    monkeypatch.setattr(ecobot.Config, "from_env", classmethod(lambda cls: cfg))
    monkeypatch.setattr(ecobot.Application, "builder", staticmethod(lambda: FakeBuilder(app)))
    started = []
    monkeypatch.setattr(ecobot, "start_sources", lambda *a: started.append(a))
    publishing = asyncio.create_task(bare.watch_alerts())

    async def alert_then_stop():
        try:
            await alert()
        finally:   # (main() runs until it is stopped, whatever happened)
            os.kill(os.getpid(), signal.SIGTERM)

    async def alert():
        await until(lambda: bare.listen._streams and cfg.state_path.exists() and "alerts_read" in cfg.state_path.read_text(),
                    what="the bot to listen and its delivery to begin")
        bare.alert_log.append("rain", "🌧️ Raining", "Rain started", expires=time.time() + 3600)
        await until(lambda: app.sent, what="the alert sent")
    try:
        async with Serving(http_app(bare, tokens=(TOKEN,)), port):
            stopper = asyncio.create_task(alert_then_stop())
            await ecobot.main()
            await stopper
    finally:
        publishing.cancel()
        await asyncio.gather(publishing, return_exceptions=True)
    assert started == [] and "Ecowitt: " not in caplog.text and "Telegram: @testbot" in caplog.text
    assert f"Weather service: {cfg.weather_mcp_url}" in caplog.text
    assert [(chat, text) for chat, text, _ in app.sent] == [(5, "🌧️ Raining")]
    assert drawn == [app.bot]


async def test_a_start_while_the_service_cant_be_reached_follows_the_station_s_zone_once_read_and_keeps_it(tmp_path, monkeypatch, caplog):
    """ecobot.main() with WEATHER_MCP_URL and the service not up within STARTUP_WAIT starts in TZ's zone (UTC here, as a container's); once
    the service is reached, its station's zone (Melbourne) is the bot's, the charts', the alert delivery's and the irrigation check's,
    and it is kept in bot_state.json: the next start with the service down runs in it, not in UTC."""
    import os
    import signal
    from zoneinfo import ZoneInfo
    import ecobot
    from lib import charts
    from tests.fakes import config
    from tests.test_startup import FakeApp, FakeBuilder
    caplog.set_level("INFO")
    utc = ZoneInfo("Etc/UTC")
    port = free_port()
    cfg = config(tmp_path, tz=utc, weather_mcp_url=f"http://127.0.0.1:{port}/mcp", weather_mcp_token=TOKEN, ecowitt_api_key="",
                 ecowitt_app_key="", airgradient_token="", airgradient_location="", state_path=tmp_path / "bot" / "bot_state.json")
    (tmp_path / "bot").mkdir()
    monkeypatch.setattr(ecobot, "STARTUP_WAIT", 0.3)
    monkeypatch.setattr(ecobot._format, "width", ecobot._format.width)
    monkeypatch.setattr(ecobot.Config, "from_env", classmethod(lambda cls: cfg))
    monkeypatch.setattr(ecobot, "start_sources", lambda *a: pytest.fail("no sources when remote"))
    monkeypatch.setattr(charts, "ZONE", charts.ZONE)   # (put back after)
    made = {}

    class Kept(ecobot.Bot):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made["bot"] = self

    class Delivery(ecobot.AlertDelivery):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made["delivery"] = self
    monkeypatch.setattr(ecobot, "Bot", Kept)
    monkeypatch.setattr(ecobot, "AlertDelivery", Delivery)

    async def until_then_stop(check, what):
        try:
            await until(check, what=what)
        finally:
            os.kill(os.getpid(), signal.SIGTERM)

    alerts = AlertLog(tmp_path / "alerts.sqlite", TZ)
    server = EcobotServer(Context(tz=TZ), alerts)   # (its station: Australia/Melbourne)
    try:
        monkeypatch.setattr(ecobot.Application, "builder", staticmethod(lambda: FakeBuilder(FakeApp())))
        bot_up = lambda: "delivery" in made and made["bot"].cfg.tz is not None

        async def serve_late():
            await until(bot_up, what="the bot started without the service")
            assert str(made["bot"].cfg.tz) == "Etc/UTC" and str(made["delivery"].tz) == "Etc/UTC"   # (TZ's, while unread)
            async with Serving(http_app(server, tokens=(TOKEN,)), port):
                await until_then_stop(lambda: str(made["bot"].cfg.tz) == "Australia/Melbourne", "the station's zone")
        late = asyncio.create_task(serve_late())
        await ecobot.main()
        await late
        bot, delivery = made["bot"], made["delivery"]
        assert str(bot.cfg.tz) == str(delivery.tz) == str(bot.alert_charts.tz) == str(charts.ZONE) == "Australia/Melbourne"
        assert json.loads(cfg.state_path.read_text())["station_zone"] == "Australia/Melbourne"
        assert "isn't reachable yet" in caplog.text and "TZ's, Etc/UTC" in caplog.text

        made.clear()   # the next start, the service down: the zone last read, not UTC
        caplog.clear()
        monkeypatch.setattr(ecobot.Application, "builder", staticmethod(lambda: FakeBuilder(FakeApp())))
        stopper = asyncio.create_task(until_then_stop(bot_up, "the bot to start again"))
        await ecobot.main()
        await stopper
        assert str(made["bot"].cfg.tz) == str(made["delivery"].tz) == str(charts.ZONE) == "Australia/Melbourne"
        assert "the station's last read, Australia/Melbourne" in caplog.text
    finally:
        alerts.close()


async def test_the_irrigation_check_is_timed_again_in_a_zone_read_later(tmp_path, monkeypatch):
    """The irrigation check's wait for 18:00 is cut short when the station's zone arrives, and timed again in it."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from lib.alerts import irrigation as monitor_module
    from lib.alerts.irrigation import IrrigationMonitor
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


def test_a_client_of_the_service_needs_no_source_settings(monkeypatch, tmp_path):
    from lib.config import Config
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    for name in ("ECOWITT_API_KEY", "ECOWITT_APP_KEY", "HOMEASSISTANT_URL", "HOMEASSISTANT_TOKEN", "AIRGRADIENT_API_TOKEN",
                 "AIRGRADIENT_LOCATION_ID", "AIRGRADIENT_LOCAL_URL", "WEATHER_MCP_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="ECOWITT_API_KEY"):
        Config.from_env()
    monkeypatch.setenv("WEATHER_MCP_URL", "http://127.0.0.1:8765/mcp/")
    monkeypatch.setenv("WEATHER_MCP_TOKEN", "t0ken")
    cfg = Config.from_env()
    assert (cfg.weather_mcp_url, cfg.weather_mcp_token) == ("http://127.0.0.1:8765/mcp", "t0ken") and not cfg.ecowitt
