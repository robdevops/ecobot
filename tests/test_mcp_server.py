"""The service over MCP (lib/mcp_server.py, docs/mcp-service.md 3.4-3.8): the ten tools listed and called over the SDK's in-memory
transport with the same answers as the ServiceTools themselves, the resources and the alerts' template, the prompts, the publishing of
changes, the auth and the allowed hosts. The listen over the network is tests/test_mcp_listen.py's (under uvicorn,
as docs 7 asks)."""

import asyncio
import json
import time
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import mcp_types as types
import pytest
from mcp import Client
from mcp.server.subscriptions import ResourceUpdated, ToolsListChanged
from mcp.shared.exceptions import MCPError

from lib import mcp_server
from lib.alert_log import AlertLog
from lib.mcp_server import (ALERTS_TEMPLATE, ALERTS_URI, COMPLETE, PROMPTS, EcobotServer, TokenCheck, host_settings, prompt,
                            tool_result)
from lib.service.common import CHART_POINTS
from lib.service.context import Context
from lib.service.names import Catalogue
from tests import evalkit, evalworld

TZ = ZoneInfo("Australia/Melbourne")
EXPECT = evalkit.load_service_cases()


def at_eval_now(patch: pytest.MonkeyPatch):
    evalworld.at_eval_now(patch, __name__)


@pytest.fixture(scope="module")
def eval_archive(tmp_path_factory):
    """The fake station's whole archive built at the evals' NOW, once for the module (as tests/test_service_evals.py's)."""
    path = tmp_path_factory.mktemp("mcp-eval-now")
    with pytest.MonkeyPatch.context() as patch:
        at_eval_now(patch)
        return asyncio.run(evalworld.build_archive(path))


@pytest.fixture
async def world(tmp_path, eval_archive, monkeypatch):
    """The four sources (faked, the clock at the evals' NOW), an alert log, and the server over them."""
    at_eval_now(monkeypatch)
    ctx = await evalworld.real_context(tmp_path, eval_archive)
    alerts = AlertLog(tmp_path / "alerts.sqlite", ctx.zone)
    server = EcobotServer(ctx, alerts, health=lambda: {"loops": {"Ecowitt refresh": {"last_ok": "now"}}})
    yield server
    alerts.close()
    await evalworld.close(ctx)


def first_call(tool: str) -> dict:
    """An argument set the evals expect of this tool (any accepted call will do)."""
    return next((args for expect in EXPECT.values() for name, args in evalkit.example_calls(expect) if name == tool), {})


# ---------- tools ----------
async def test_every_tool_is_listed_with_its_schema_and_hints_and_answers_as_the_service_tool_does(world):
    async with Client(world.server) as client:
        listed = {t.name: t for t in (await client.list_tools()).tools}
        assert list(listed) == [t.name for t in world.service] and len(listed) == 10
        for tool in world.service:
            entry = listed[tool.name]
            assert entry.description == tool.description and entry.input_schema == tool.parameters
            assert entry.annotations.read_only_hint is True and entry.annotations.open_world_hint is tool.open_world
            args = first_call(tool.name)
            for _ in range(10):   # (what a call fetches inline, fetched first: the answers are then the same each time)
                if await tool.call(args) == await tool.call(args):
                    break
            got = await client.call_tool(tool.name, args)
            assert got.content[0].text == await tool.call(args), tool.name
            assert got.is_error is ("error" in json.loads(got.content[0].text))


async def test_a_refusal_is_an_error_result_with_what_teaches_the_right_call_and_an_unknown_tool_a_protocol_error(world):
    async with Client(world.server) as client:
        got = await client.call_tool("summary", {"period": "the age of the dinosaurs"})
        assert got.is_error and "forms" in json.loads(got.content[0].text)
        with pytest.raises(MCPError, match="No tool 'weather_now'"):
            await client.call_tool("weather_now", {})


async def test_the_requests_meta_reaches_the_tool_charts_come_whole_and_sized_by_chart_points(world):
    args = {"readings": ["temperature"], "period": "last 7 days", "chart": True}
    async with Client(world.server) as client:
        default = json.loads((await client.call_tool("summary", args)).content[0].text)
        small = json.loads((await client.call_tool("summary", args, meta={"chart_points": 40})).content[0].text)
    (chart,) = default["charts"]
    assert {"title", "period", "source", "drawn_by", "panels"} <= set(chart)   # the full chart JSON (3.7)
    longest = lambda c: max(len(s["points"]) for p in c["panels"] for s in p["series"])
    assert 40 < longest(chart) <= CHART_POINTS and longest(small["charts"][0]) <= 40
    assert default == json.loads(await world.service["summary"].call(args))


async def test_a_complete_answer_carries_its_reply_for_the_person_and_the_complete_mark(world):
    args = {"readings": ["pressure"], "period": "this week", "chart_answers_it": True}
    async with Client(world.server) as client:
        got = await client.call_tool("summary", args)
        plain = await client.call_tool("summary", {"readings": ["pressure"], "period": "this week"})
    data = json.loads(got.content[0].text)
    assert data["complete"] is True and len(got.content) == 2
    reply = got.content[1]
    assert reply.text == data["reply"] and reply.annotations.audience == ["user"]
    assert got.meta[COMPLETE] is True
    assert len(plain.content) == 1 and COMPLETE not in (plain.meta or {})


def test_a_result_is_complete_only_with_a_reply_and_no_error():
    assert tool_result('{"complete": true, "reply": "Pressure ...", "charts": []}').meta == {COMPLETE: True}
    assert tool_result('{"complete": true}').meta is None and len(tool_result('{"complete": true}').content) == 1
    assert tool_result('{"error": "no"}').is_error and tool_result("not json").content[0].text == "not json"


async def test_the_tools_are_built_again_when_the_catalogue_gains_a_reading_and_listeners_are_told(world, monkeypatch):
    seen = []
    world.bus.subscribe(seen.append)
    assert await world.refresh_catalogue() is False and seen == []     # (nothing gained: nothing sent)
    held = world.ctx.readings()
    new = "outdoor.dew_point"   # (as if the station had just sent it for the first time)
    assert new in held.names
    world.ctx.catalogue = Catalogue([r for r in held if r.name != new])
    world._service = world.service.of(world.ctx)
    described = lambda: world.service["readings"].parameters["properties"]["reading"]["description"]
    assert new not in described()
    assert await world.refresh_catalogue() is True
    assert seen == [ToolsListChanged(), ResourceUpdated("ecobot://station")]
    assert new in described() and new in world.server.instructions or new.split(".")[1] in world.server.instructions


# ---------- the instructions ----------
async def test_the_instructions_are_the_services_and_come_with_discover(world):
    from lib.service.instructions import instructions
    async with Client(world.server) as client:
        assert client.instructions == instructions(world.ctx) and "Australia/Melbourne" in client.instructions


# ---------- resources ----------
async def test_each_resource_is_listed_and_readable_as_json(world):
    world.alert_log.append("rain", "🌧️ It's started raining", "Rain started", data={"mm": 0.4}, expires=None)
    async with Client(world.server) as client:
        assert json.loads((await client.read_resource("ecobot://now/weather")).contents[0].text)["unavailable"]   # (none kept yet)
        for name in ("current", "forecast"):   # (the station's live reading and the forecast asked for, and kept: a resource fetches nothing)
            await world.service[name].call({})
        listed = [r.uri for r in (await client.list_resources()).resources]
        assert listed == list(mcp_server.RESOURCES)
        read = {}
        for uri in listed:
            (content,) = (await client.read_resource(uri)).contents
            assert content.mime_type == "application/json" and content.uri == uri
            read[uri] = json.loads(content.text)
    station = read["ecobot://station"]
    assert station["name"] == world.ctx.eco.station_name and station["time_zone"] == "Australia/Melbourne"
    assert {"station", "air"} == set(station["sensors"]) and "temperature" in station["words"]
    assert {r["name"] for r in station["sensors"]["station"]["readings"]} >= {"outdoor.temperature", "pressure.relative"}
    assert set(station["sources"]) == {"Ecowitt", "AirGradient", "Forecast", "Pollen"} and station["history"]["station"]["held"]
    assert {a["kind"] for a in station["alerts"]} >= {"rain", "gusts", "particulates", "forecast", "pollen_and_asthma"}
    weather = read["ecobot://now/weather"]
    assert weather["sensors"]["outdoor"]["temperature"]["unit"] and weather["lines"] and weather["as_of"]
    air = read["ecobot://now/air"]
    assert air["sensors"]["air"]["pm2_5"]["rating"] and "aqi_us" in air["sensors"]["air"]["pm2_5"]
    assert [a["kind"] for a in read[ALERTS_URI]["alerts"]] == ["rain"] and read[ALERTS_URI]["last_id"] == 1
    forecast = read["ecobot://forecast"]
    assert forecast["place"] == "Melbourne" and forecast["days"] and {"date", "min_c", "max_c", "text"} <= set(forecast["days"][0])
    assert {"in_season", "grass", "asthma", "fetched_at"} <= set(read["ecobot://pollen"])
    health = read["ecobot://health"]
    assert health["file"]["bytes"] > 0 and health["alert_log"] == {"last_id": 1} and "Ecowitt refresh" in health["loops"]
    assert read["ecobot://schema/chart"]["$id"] == "ecobot://schema/chart"


async def test_a_resource_there_is_none_of_is_refused_naming_those_there_are(tmp_path):
    server = EcobotServer(Context(tz=TZ), AlertLog(tmp_path / "a.sqlite", TZ))
    async with Client(server.server) as client:
        listed = [r.uri for r in (await client.list_resources()).resources]
        assert listed == ["ecobot://station", ALERTS_URI, "ecobot://health", "ecobot://schema/chart"]   # (no sensor, no forecast, no pollen)
        for uri in ("ecobot://now/weather", "ecobot://nowhere", "ecobot://health?x=1"):
            with pytest.raises(MCPError, match="No resource") as e:
                await client.read_resource(uri)
            assert e.value.error.code == mcp_server.RESOURCE_NOT_FOUND


async def test_the_alerts_template_reads_after_an_id_and_leaves_the_expired_out_unless_asked(tmp_path):
    alerts = AlertLog(tmp_path / "a.sqlite", TZ)
    server = EcobotServer(Context(tz=TZ), alerts)
    now = time.time()
    alerts.append("rain", "It rained", "Rain", expires=now - 60)                  # 1: expired
    alerts.append("gusts", "Gusty", "Gusts", expires=now + 3600)                   # 2
    alerts.append("uv", "UV high", "UV", expires=None)                             # 3: never expires
    async with Client(server.server) as client:
        (template,) = (await client.list_resource_templates()).resource_templates
        assert template.uri_template == ALERTS_TEMPLATE

        async def ids(uri):
            return [a["id"] for a in json.loads((await client.read_resource(uri)).contents[0].text)["alerts"]]
        assert await ids(ALERTS_URI) == [2, 3]
        assert await ids(f"{ALERTS_URI}?since=2") == [3]
        assert await ids(f"{ALERTS_URI}?expired=true") == [1, 2, 3]
        assert await ids(f"{ALERTS_URI}?since=1&expired=true") == [2, 3]
        assert await ids(f"{ALERTS_URI}?since=3") == []
        with pytest.raises(MCPError, match="whole number"):
            await ids(f"{ALERTS_URI}?since=last")
        whole = json.loads((await client.read_resource(ALERTS_URI)).contents[0].text)
        assert whole["last_id"] == 3 and whole["identity"] == alerts.identity
    alerts.close()


# ---------- publishing ----------
async def test_a_live_resource_is_published_when_its_reading_changes_and_not_when_it_repeats(tmp_path):
    eco = NS(_live=None, storage=None, tz=TZ)
    ctx = Context(eco=eco, tz=TZ)
    ctx.catalogue = Catalogue([])
    server = EcobotServer(ctx, None)
    seen = []
    server.bus.subscribe(seen.append)
    assert await server.publish_changes() == []                                    # nothing kept yet
    eco._live = (time.time(), {"outdoor": {"temperature": {"value": "12.3", "unit": "℃", "time": "1000"}}})
    assert await server.publish_changes() == ["ecobot://now/weather"]
    eco._live = (time.time() + 5, {"outdoor": {"temperature": {"value": "12.3", "unit": "℃", "time": "1000"}}})   # the same reading again
    assert await server.publish_changes() == []
    eco._live = (time.time() + 10, {"outdoor": {"temperature": {"value": "12.4", "unit": "℃", "time": "1060"}}})
    assert await server.publish_changes() == ["ecobot://now/weather"]
    assert seen == [ResourceUpdated("ecobot://now/weather")] * 2


async def test_a_new_alert_is_published_to_a_listener(tmp_path):
    alerts = AlertLog(tmp_path / "a.sqlite", TZ)
    server = EcobotServer(Context(tz=TZ), alerts)
    publishing = asyncio.create_task(server.watch_alerts())
    try:
        async with Client(server.server) as client:
            async with client.listen(resource_subscriptions=[ALERTS_URI, "ecobot://now/weather"]) as sub:
                alerts.append("gusts", "💨 A gust of 72 km/h", "Gusts", expires=time.time() + 3600)
                event = await asyncio.wait_for(sub.__anext__(), 5)
                assert event == ResourceUpdated(ALERTS_URI)
                got = json.loads((await client.read_resource(f"{ALERTS_URI}?since=0")).contents[0].text)
                assert [a["text"] for a in got["alerts"]] == ["💨 A gust of 72 km/h"]
    finally:
        publishing.cancel()
        alerts.close()


# ---------- prompts ----------
async def test_the_prompts_are_questions_in_words_with_their_period_an_argument(world):
    async with Client(world.server) as client:
        listed = {p.name: p for p in (await client.list_prompts()).prompts}
        assert list(listed) == ["now", "report", "will_it_rain", "weather", "chart", "rain", "air"] == list(PROMPTS)
        assert [a.name for a in listed["chart"].arguments] == ["reading", "period"] and listed["chart"].arguments[0].required
        got = await client.get_prompt("chart", {"reading": "pressure", "period": "this week"})
        assert got.messages[0].role == "user" and got.messages[0].content.text == "Chart pressure over this week."
        with pytest.raises(MCPError, match="needs reading"):
            await client.get_prompt("chart", {})
    assert prompt("rain").messages[0].content.text == "How much rain fell over today?"
    assert prompt("air").messages[0].content.text == "How is the air quality now?"
    assert prompt("air", {"period": "last week"}).messages[0].content.text == "How was the air quality over last week?"
    assert prompt("weather").messages[0].content.text == "Chart every weather reading, a panel each, over the last 7 days."
    with pytest.raises(MCPError, match="No prompt"):
        prompt("lightning")


# ---------- auth and hosts ----------
async def test_a_token_is_one_of_mcp_service_tokens_and_one_can_be_revoked_alone():
    check = TokenCheck(("s3cret-bot", "s3cret-phone"))
    assert await check.verify_token("s3cret-bot") and await check.verify_token("s3cret-phone")
    for wrong in ("", "s3cret", "s3cret-bot ", "S3CRET-BOT"):
        assert await check.verify_token(wrong) is None
    check.revoke("s3cret-phone")
    assert await check.verify_token("s3cret-phone") is None and await check.verify_token("s3cret-bot")


def test_the_allowed_hosts_take_any_port_unless_one_is_named():
    settings = host_settings(("127.0.0.1", "weather.lan:443"))
    assert settings.enable_dns_rebinding_protection
    assert settings.allowed_hosts == ["127.0.0.1", "127.0.0.1:*", "weather.lan:443"]
    assert "https://weather.lan:443" in settings.allowed_origins and "http://127.0.0.1:*" in settings.allowed_origins


def test_the_service_reads_its_settings_and_needs_no_telegram_token(monkeypatch, tmp_path, caplog):
    from lib.config import Config
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for key, value in {"ECOWITT_API_KEY": "a", "ECOWITT_APP_KEY": "b", "TELEGRAM_BOT_TOKEN": ""}.items():
        monkeypatch.setenv(key, value)
    for key in ("MCP_SERVICE_TOKENS", "MCP_SERVICE_BIND", "MCP_SERVICE_ALLOWED_HOSTS"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN is not set"):
        Config.from_env()                                                   # the bot can't start without it ...
    cfg = Config.from_env(bot=False)                                        # ... the service can
    assert cfg.mcp_service_tokens == () and cfg.mcp_service_bind == ("127.0.0.1", 8765) and cfg.mcp_service_allowed_hosts == ("127.0.0.1", "localhost")
    monkeypatch.setenv("MCP_SERVICE_TOKENS", " s3cret , t0k:en,, s3cret")
    monkeypatch.setenv("MCP_SERVICE_BIND", "0.0.0.0:9000")
    monkeypatch.setenv("MCP_SERVICE_ALLOWED_HOSTS", "weather.lan, 192.168.1.5:9000")
    cfg = Config.from_env(bot=False)
    assert cfg.mcp_service_tokens == ("s3cret", "t0k:en") and "s3cret" not in caplog.text
    assert cfg.mcp_service_bind == ("0.0.0.0", 9000) and cfg.mcp_service_allowed_hosts == ("weather.lan", "192.168.1.5:9000")
    for value, want in (("[::1]:8800", ("::1", 8800)), ("9001", ("127.0.0.1", 9001)), ("host:port", ("127.0.0.1", 8765)),
                        ("h:70000", ("127.0.0.1", 8765))):
        monkeypatch.setenv("MCP_SERVICE_BIND", value)
        assert Config.from_env(bot=False).mcp_service_bind == want, value


def test_one_service_per_data_folder(tmp_path):
    """The bot running the service in-process and python -m ecobot_service on one folder would both fetch and both write each alert."""
    import contextlib
    from types import SimpleNamespace

    from ecobot_service.core import hold_data_folder
    cfg = SimpleNamespace(state_path=tmp_path / "bot_state.json")
    with contextlib.ExitStack() as first:
        hold_data_folder(cfg, first)
        with contextlib.ExitStack() as second, pytest.raises(SystemExit, match="already runs the weather service"):
            hold_data_folder(cfg, second)
    with contextlib.ExitStack() as again:   # free once the first has closed
        hold_data_folder(cfg, again)
