"""The service over the network (docs/mcp-service.md 7, "a client that listens", "a deploy check"): the real Streamable HTTP app under
uvicorn on a localhost port each test starts and stops, not the in-memory transport (a connection can't fail there, and it buffers a
listen stream). A client that listens hears a new alert, loses its listen when the service stops, listens again once it is back and
reads the alerts after its last id, once; a request without the token, with a wrong or revoked one, or to a Host not allowed is
refused; and `python -m ecobot_service` runs the sources and answers a tool call through the name a proxy uses."""

import asyncio
import contextlib
import json
import os
import signal
import socket
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from mcp import Client

from lib import upstream
from ecobot_service.__main__ import Uvicorn, server_config
from lib.upstream import Upstream, http_transport
from lib.alert_log import AlertLog
from lib.mcp_server import ALERTS_URI, EcobotServer, http_app
from lib.service.context import Context

TZ = ZoneInfo("Australia/Melbourne")
TOKENS = {"bot": "bot-s3cret", "phone": "phone-s3cret"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(check, seconds: float = 10.0, what: str = "it"):
    """Wait until check() is true (polled), failing after `seconds`."""
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


class Serving:
    """The app under uvicorn on 127.0.0.1:port, started and stopped by the test."""

    def __init__(self, app, port: int):
        self.web = Uvicorn(server_config(app, "127.0.0.1", port))
        self.task: asyncio.Task | None = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.web.serve())
        await until(lambda: self.web.started or self.task.done(), what="uvicorn to start")
        return self

    async def stop(self):
        self.web.should_exit = True
        await self.task

    async def __aexit__(self, *exc):
        if not self.task.done():
            await self.stop()


@pytest.fixture
def service(tmp_path):
    alerts = AlertLog(tmp_path / "alerts.sqlite", TZ)
    server = EcobotServer(Context(tz=TZ), alerts)
    yield server
    alerts.close()


async def test_a_client_that_listens_hears_an_alert_and_after_a_lost_listen_listens_again_and_reads_what_it_missed(service, monkeypatch):
    monkeypatch.setattr(upstream, "BACKOFF", (0.1,))
    alerts, port = service.alert_log, free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    publishing = asyncio.create_task(service.watch_alerts())
    handled, connections = [], []

    async def read_new(*_):
        """The client's delivery: the alerts after the last id it handled, each once."""
        after = handled[-1] if handled else 0
        got = json.loads((await up.client.read_resource(f"{ALERTS_URI}?since={after}")).contents[0].text)
        handled.extend(a["id"] for a in got["alerts"] if a["id"] > (handled[-1] if handled else 0))

    async def listening():
        connections.append(True)
        await read_new()
    up = Upstream(url, TOKENS["bot"], on_event=read_new, on_listening=listening)
    supervisor = asyncio.create_task(up.run())
    try:
        async with Serving(http_app(service, tokens=TOKENS.values()), port) as first:
            await until(lambda: connections and service.listen._streams, what="the listen")
            alerts.append("rain", "🌧️ It's started raining", "Rain started", expires=time.time() + 3600)
            await until(lambda: handled == [1], what="alert 1")
            await first.stop()                                             # the service goes away: the listen is lost
        await until(lambda: not up.connected.is_set(), what="the client to notice")
        alerts.append("gusts", "💨 A gust of 72 km/h", "Gusts", expires=time.time() + 3600)   # missed while it was down
        alerts.append("uv", "UV 9", "UV", expires=time.time() - 1)         # ... and already expired: never news
        async with Serving(http_app(service, tokens=TOKENS.values()), port):
            await until(lambda: len(connections) == 2, what="the client to listen again")
            await until(lambda: handled == [1, 2], what="alert 2, read after its last id")
            alerts.append("temps", "Warmer outside", "Temperatures", expires=time.time() + 3600)
            await until(lambda: handled == [1, 2, 4], what="alert 4 on the new listen")
    finally:
        supervisor.cancel()
        publishing.cancel()
        await asyncio.gather(supervisor, publishing, return_exceptions=True)
    assert handled == [1, 2, 4]


async def test_a_request_without_the_token_with_a_wrong_or_revoked_one_or_to_a_host_not_allowed_is_refused(service):
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    app = http_app(service, tokens=TOKENS.values(), allowed_hosts=("127.0.0.1", "weather.lan"))
    body = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28"}}}

    async def post(token=None, host=None) -> int:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Mcp-Protocol-Version": "2026-07-28", "Mcp-Method": "server/discover"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if host:
            headers["Host"] = host
        async with httpx.AsyncClient() as http:
            return (await http.post(url, headers=headers, json=body)).status_code
    async with Serving(app, port):
        assert await post() == 401 and await post("nope") == 401 and await post(TOKENS["bot"].upper()) == 401
        assert await post(TOKENS["phone"], host="evil.example") == 421                 # DNS rebinding: a Host not allowed
        async with Client(http_transport(url, TOKENS["phone"]), mode="2026-07-28") as client:
            assert not (await client.call_tool("about", {})).is_error
        app.state.tokens.revoke(TOKENS["phone"])                                                # one client's token revoked alone
        assert await post(TOKENS["phone"]) == 401
        with pytest.raises(BaseException):
            async with Client(http_transport(url, TOKENS["phone"]), mode="2026-07-28") as client:
                await client.call_tool("about", {})
        async with Client(http_transport(url, TOKENS["bot"]), mode="2026-07-28") as client:
            assert not (await client.call_tool("about", {})).is_error


async def test_the_service_runs_the_sources_and_answers_a_tool_call_through_the_proxys_name(tmp_path, monkeypatch, caplog):
    """A deploy check: `python -m ecobot_service` (main) with the fake sources, MCP_SERVICE_ALLOWED_HOSTS the name a reverse proxy sends, a
    tool call with a token through that name answered, the archives run, then SIGTERM closes it all."""
    import ecobot_service.__main__ as service_main
    from ecobot_service import core
    from lib.config import Config
    from lib.ecowitt import archive
    from lib.airgradient import source as air_source
    from tests.fakes import air_transport, config, ecowitt_transport
    caplog.set_level("INFO")
    port = free_port()
    cfg = config(tmp_path, telegram_token="", mcp_service_tokens=("pr0xy",), mcp_service_bind=("127.0.0.1", port),
                 mcp_service_allowed_hosts=("weather.lan",))
    eco_t, _ = ecowitt_transport(history_days=10)
    air_t, _ = air_transport(oldest=datetime.now(timezone.utc) - timedelta(days=5))
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls, bot=True: cfg))
    for cls, transport in ((core.Ecowitt, eco_t), (core.AirGradient, air_t)):
        monkeypatch.setattr(cls, "__init__", lambda self, c, transport=None, orig=cls.__init__, t=transport, **kw: orig(self, c, t, **kw))
    monkeypatch.setattr(archive, "PACE_SECONDS", 0)
    monkeypatch.setattr(air_source, "BACKFILL_PACE", 0)
    monkeypatch.setattr(air_source, "BACKFILL_EMPTY_STOP", 3)
    answered = {}

    async def ask_then_stop():
        await until(lambda: "MCP: http://127.0.0.1:" in caplog.text, 30, "the service to listen")
        url = f"http://127.0.0.1:{port}/mcp"
        from mcp.shared._httpx_utils import create_mcp_http_client
        import contextlib
        from mcp.client.streamable_http import streamable_http_client

        @contextlib.asynccontextmanager
        async def through_the_proxy():
            headers = {"Authorization": "Bearer pr0xy", "Host": "weather.lan"}
            async with create_mcp_http_client(headers=headers) as http:
                async with streamable_http_client(url, http_client=http) as streams:
                    yield streams
        async with Client(through_the_proxy(), mode="2026-07-28") as client:
            answered["tools"] = [t.name for t in (await client.list_tools()).tools]
            answered["current"] = json.loads((await client.call_tool("current", {"readings": ["temperature"]})).content[0].text)
            answered["health"] = json.loads((await client.read_resource("ecobot://health")).contents[0].text)
        answered["stranger"] = await post_status(url, "evil.example")   # a Host it doesn't answer (127.0.0.1, localhost and its own
        answered["own name"] = await post_status(url, socket.gethostname())   # hostname always are, beside MCP_SERVICE_ALLOWED_HOSTS)
        await until(lambda: any("Backfill Ecowitt: " in l and " fetched/failed, " in l for l in caplog.text.splitlines())
                    and "Backfill AirGradient:" in caplog.text, 60, "the archives")
        os.kill(os.getpid(), signal.SIGTERM)

    async def post_status(url, host) -> int:
        async with httpx.AsyncClient() as http:
            return (await http.post(url, headers={"Authorization": "Bearer pr0xy", "Content-Type": "application/json", "Host": host},
                                    json={})).status_code
    asker = asyncio.create_task(ask_then_stop())
    await service_main.main()
    await asker
    log = caplog.text
    assert "Ecowitt: 'Fairleigh' (" in log and "Service tools: current, summary" in log and "1 token(s)" in log
    assert "MCP 127.0.0.1: POST /mcp tools/call current temperature -> 200" in log and "-> 421, its Host isn't in" in log   # (the request log)
    assert answered["tools"] == ["current", "summary", "readings", "days", "relate", "scan", "outlook", "about"]   # (no forecast, no pollen)
    assert answered["current"]["sensors"]["outdoor"]["temperature"]["unit"]
    assert answered["stranger"] == 421 and answered["own name"] != 421
    assert "Ecowitt refresh" in answered["health"]["loops"] and answered["health"]["file"]["bytes"] > 0
    assert "Alerts NOT running" not in log and "Telegram" not in log


async def test_the_service_will_not_serve_without_tokens_on_an_address_others_can_reach(tmp_path, monkeypatch):
    import ecobot_service.__main__ as service_main
    from lib.config import Config
    from tests.fakes import config
    cfg = config(tmp_path, telegram_token="", mcp_service_bind=("0.0.0.0", free_port()))
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls, bot=True: cfg))
    with pytest.raises(SystemExit, match="MCP_SERVICE_TOKENS"):
        await service_main.main()


async def test_the_bot_s_warning_says_why_the_service_refused_it(service, monkeypatch, caplog):
    """The SDK reports every HTTP error as "Server returned an error response": the warning names the status and what to change."""
    monkeypatch.setattr(upstream, "BACKOFF", (0.1,))
    port = free_port()
    async with Serving(http_app(service, tokens=TOKENS.values(), allowed_hosts=("localhost",)), port):
        for url, token, want in ((f"http://localhost:{port}/mcp", "wrong", "401: WEATHER_MCP_TOKEN"),
                                 (f"http://127.0.0.1:{port}/mcp", TOKENS["bot"], "421: the service doesn't answer to this address")):
            caplog.clear()
            task = asyncio.create_task(Upstream(url, token).run())
            for _ in range(100):
                if want in caplog.text:
                    break
                await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            assert want in caplog.text, caplog.text


async def test_each_request_is_logged_with_who_asked_what_and_a_refusal_with_its_reason(service, caplog):
    caplog.set_level("INFO", logger="lib.mcp_server.requests")
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    async with Serving(http_app(service, tokens=TOKENS.values(), allowed_hosts=("127.0.0.1",)), port):
        async with Client(http_transport(url, TOKENS["bot"]), mode="2026-07-28") as client:
            assert not (await client.call_tool("about", {})).is_error
            await client.call_tool("summary", {"readings": ["rain"], "period": "1d", "chart": True})
            await client.read_resource("ecobot://health")
        async with httpx.AsyncClient() as http:
            await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            headers={"Authorization": "Bearer wrong", "Accept": "application/json, text/event-stream"})
            await http.post(url, json={}, headers={"Authorization": f"Bearer {TOKENS['bot']}", "Host": "192.168.1.208:8765"})
    lines = [r.getMessage() for r in caplog.records if r.name == "lib.mcp_server.requests"]
    assert any("MCP 127.0.0.1: POST /mcp tools/call about -> 200" in line for line in lines), lines
    assert any("tools/call summary rain 1d -> 200" in line for line in lines), lines
    assert any("resources/read ecobot://health -> 200" in line for line in lines), lines
    assert any("refused: POST /mcp tools/list -> 401, no token, or one that isn't in MCP_SERVICE_TOKENS" in line for line in lines), lines
    assert any("-> 421, its Host isn't in MCP_SERVICE_ALLOWED_HOSTS (Host 192.168.1.208:8765)" in line for line in lines), lines
    assert not any(TOKENS["bot"] in line or "wrong" in line for line in lines)
