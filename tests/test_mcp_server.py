"""The service over MCP (lib/mcp_server.py, docs/ecobot-mcp.md 3.4-3.8): the ten tools listed and called over the SDK's in-memory
transport with the same answers as the ServiceTools themselves, the resources and the alerts' template, the prompts, the publishing of
changes, the auth and the allowed hosts. The listen over the network is tests/test_mcp_listen.py's (under uvicorn,
as docs 7 asks)."""

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import mcp_types as types
import pytest
from mcp import Client
from mcp.server.subscriptions import ResourceUpdated, ToolsListChanged
from mcp.shared.exceptions import MCPError

from lib import chart_images, charts, mcp_server
from lib.config import ROOT
from lib.alert_log import AlertLog
from lib.mcp_server import (ALERTS_TEMPLATE, ALERTS_URI, COMPLETE, PROMPTS, EcobotServer, TokenCheck, host_settings, prompt,
                            tool_result)
from lib.service.common import AS_DRAWN, CHART_POINTS
from lib.service.context import Context
from lib.service.names import Catalogue
from tests import evalkit, evalworld

TZ = ZoneInfo("Australia/Melbourne")
EXPECT = evalkit.load_service_cases()


def at_eval_now(patch: pytest.MonkeyPatch):
    evalworld.at_eval_now(patch, __name__)


@pytest.fixture(scope="module")
def eval_archive(shared_archive):
    """The fake station's whole archive (its ecobot.sqlite) built at EVAL_NOW, the air's month held in it too (real_context), once for every
    worker and run (tests/evalworld.py's)."""
    return evalworld.shared_eval_archive(shared_archive)


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
        assert list(listed) == ["now", "report", "will_it_rain", "weather_chart", "chart", "rainfall", "air"] == list(PROMPTS)
        assert [a.name for a in listed["chart"].arguments] == ["reading", "period"] and listed["chart"].arguments[0].required
        got = await client.get_prompt("chart", {"reading": "pressure", "period": "this week"})
        assert got.messages[0].role == "user" and got.messages[0].content.text == "Chart pressure over this week."
        with pytest.raises(MCPError, match="needs reading"):
            await client.get_prompt("chart", {})
    assert prompt("rainfall").messages[0].content.text == "How much rain fell over today?"
    assert prompt("air").messages[0].content.text == "How is the air quality now?"
    assert prompt("air", {"period": "last week"}).messages[0].content.text == "How was the air quality over last week?"
    assert prompt("weather_chart").messages[0].content.text == "Chart every weather reading, a panel each, over the last 7 days."
    with pytest.raises(MCPError, match="No prompt"):
        prompt("lightning")


# ---------- auth and hosts ----------
async def test_a_token_is_one_of_auth_tokens_and_one_can_be_revoked_alone():
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
    for key, value in {"MCP_ECOWITT_API_KEY": "a", "MCP_ECOWITT_APP_KEY": "b", "BOT_TELEGRAM_TOKEN": ""}.items():
        monkeypatch.setenv(key, value)
    for key in ("AUTH_TOKENS", "MCP_BIND", "MCP_ALLOWED_HOSTS"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit, match="BOT_TELEGRAM_TOKEN is not set"):
        Config.from_env()                                                   # the bot can't start without it ...
    cfg = Config.from_env(bot=False)                                        # ... the service can
    assert cfg.auth_tokens == () and cfg.mcp_bind == ("127.0.0.1", 8765) and cfg.mcp_allowed_hosts == ("127.0.0.1", "localhost")
    monkeypatch.setenv("AUTH_TOKENS", " s3cret , t0k:en,, s3cret")
    monkeypatch.setenv("MCP_BIND", "0.0.0.0:9000")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "weather.lan, 192.168.1.5:9000")
    cfg = Config.from_env(bot=False)
    assert cfg.auth_tokens == ("s3cret", "t0k:en") and "s3cret" not in caplog.text
    assert cfg.mcp_bind == ("0.0.0.0", 9000) and cfg.mcp_allowed_hosts == ("weather.lan", "192.168.1.5:9000")
    for value, want in (("[::1]:8800", ("::1", 8800)), ("9001", ("127.0.0.1", 9001)), ("host:port", ("127.0.0.1", 8765)),
                        ("h:70000", ("127.0.0.1", 8765))):
        monkeypatch.setenv("MCP_BIND", value)
        assert Config.from_env(bot=False).mcp_bind == want, value


def test_one_service_per_data_folder(tmp_path):
    """The bot running the service in-process and python -m ecobot_mcp on one folder would both fetch and both write each alert."""
    import contextlib
    from types import SimpleNamespace

    from ecobot_mcp.core import hold_data_folder
    cfg = SimpleNamespace(state_path=tmp_path / "bot_state.json")
    with contextlib.ExitStack() as first:
        hold_data_folder(cfg, first)
        with contextlib.ExitStack() as second, pytest.raises(SystemExit, match="already runs ecobot-mcp.s work"):
            hold_data_folder(cfg, second)
    with contextlib.ExitStack() as again:   # free once the first has closed
        hold_data_folder(cfg, again)


# ---------- chart images (3.7) ----------
CHARTED = {"readings": ["temperature"], "period": "last 7 days", "chart": True}


def ours(result) -> dict:
    """The result's _meta that is the service's own (not the SDK's serverInfo)."""
    return {k: v for k, v in (result.meta or {}).items() if k.startswith("ecobot/")}


def images_of(result) -> list[bytes]:
    return [base64.b64decode(b.data) for b in result.content if b.type == "image"]


async def test_without_images_asked_a_chart_comes_as_json_alone_as_before(world):
    async with Client(world.server) as client:
        plain = await client.call_tool("summary", CHARTED)
        none = await client.call_tool("summary", CHARTED, meta={"chart_image": "none"})
    for got in (plain, none):
        assert [b.type for b in got.content] == ["text"] and not ours(got)
        assert got.content[0].text == await world.service["summary"].call(CHARTED)


async def test_a_call_asking_for_images_gets_each_chart_as_a_png_after_the_figures_and_the_charts_json_in_meta(world):
    async with Client(world.server) as client:
        got = await client.call_tool("summary", CHARTED, meta={"chart_image": "png"})
        dark = await client.call_tool("summary", CHARTED, meta={"chart_image": "png", "chart_dark": True})
    told = json.loads(got.content[0].text)
    whole = json.loads(await world.service["summary"].call(CHARTED, {"chart_points": AS_DRAWN}))   # (drawn from every point, as the bot's)
    assert told["charts"] == [{"title": c["title"], "period": c["period"]} for c in whole["charts"]]
    assert told["chart_note"] == chart_images.IMAGE_NOTE and told["caption"] == whole["caption"]
    assert {k: v for k, v in told.items() if k not in ("charts", "chart_note")} == {k: v for k, v in whole.items() if k not in ("charts", "chart_note")}
    assert got.meta[chart_images.CHARTS_META] == whole["charts"] and got.meta[chart_images.DARK_META] is False
    (png,) = images_of(got)
    assert got.content[1].mime_type == "image/png" and png.startswith(b"\x89PNG")
    assert png == charts.render_data(whole["charts"][0], TZ, dark=False)   # the bot's renderer, byte for byte
    assert dark.meta[chart_images.DARK_META] is True and images_of(dark)[0] == charts.render_data(whole["charts"][0], TZ, dark=True)
    assert not got.is_error and COMPLETE not in got.meta


async def test_a_complete_answer_with_images_still_carries_its_reply_for_the_person_last(world):
    args = {"readings": ["pressure"], "period": "this week", "chart_answers_it": True}
    async with Client(world.server) as client:
        got = await client.call_tool("summary", args, meta={"chart_image": "png"})
    data = json.loads(got.content[0].text)
    assert [b.type for b in got.content] == ["text", "image", "text"] and data["complete"] is True
    assert got.content[2].text == data["reply"] and got.content[2].annotations.audience == ["user"]
    assert got.meta[COMPLETE] is True and len(got.meta[chart_images.CHARTS_META]) == 1


async def test_a_refusal_or_an_answer_without_a_chart_is_as_it_was_with_images_asked(world):
    async with Client(world.server) as client:
        refused = await client.call_tool("summary", {"period": "the age of the dinosaurs"}, meta={"chart_image": "png"})
        plain = await client.call_tool("current", {"readings": ["temperature"]}, meta={"chart_image": "png"})
    assert refused.is_error and [b.type for b in refused.content] == ["text"] and not ours(refused)
    assert [b.type for b in plain.content] == ["text"] and not ours(plain) and "charts" not in json.loads(plain.content[0].text)


async def test_without_pillow_images_asked_for_give_the_json_with_a_note_and_one_warning(world, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "lib.charts", None)   # (as if Pillow weren't installed: the import fails)
    monkeypatch.setattr(chart_images, "_warned", False)
    async with Client(world.server) as client:
        first = await client.call_tool("summary", CHARTED, meta={"chart_image": "png"})
        again = await client.call_tool("summary", CHARTED, meta={"chart_image": "png"})
    for got in (first, again):
        data = json.loads(got.content[0].text)
        assert [b.type for b in got.content] == ["text"] and not ours(got)
        assert data["chart_image_note"] == chart_images.UNAVAILABLE_NOTE
        assert {k: v for k, v in data.items() if k != "chart_image_note"} == json.loads(await world.service["summary"].call(CHARTED))
    warned = [r for r in caplog.records if r.name == "lib.chart_images" and r.levelname == "WARNING"]
    assert len(warned) == 1 and "requirements-charts.txt" in warned[0].getMessage()


def test_images_are_wanted_per_call_else_per_connection():
    assert not chart_images.wanted({}) and not chart_images.wanted({}, "json") and chart_images.wanted({}, "png")
    assert chart_images.wanted({"chart_image": "png"}) and chart_images.wanted({"chart_image": "PNG"}, None)
    assert not chart_images.wanted({"chart_image": "none"}, "png") and chart_images.wanted({"chart_image": "jpeg"}, "png")
    assert chart_images.asked_dark({"chart_dark": True}) is True and chart_images.asked_dark({"chart_dark": "yes"}) is None


def test_the_service_imports_no_pillow_until_an_image_is_asked_for_and_no_telegram_ever(tmp_path):
    """Importing the server and building its app (as python -m ecobot_mcp does) imports neither Pillow nor the renderer, nor the
    Telegram library: the service knows nothing of Telegram."""
    code = ("import sys, ecobot_mcp.__main__; from zoneinfo import ZoneInfo; from lib.mcp_server import EcobotServer, http_app; "
            "from lib.service.context import Context; http_app(EcobotServer(Context(tz=ZoneInfo('UTC'))), tokens=('t',)); "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in ('PIL', 'telegram') or m in ('lib.charts', 'lib.draw', 'lib.gauges')))")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                          env={**os.environ, "DATA_DIR": str(tmp_path)})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


async def test_over_http_the_connections_url_asks_for_images_and_a_call_can_say_otherwise(world):
    from lib.upstream import http_transport
    from tests.test_mcp_listen import Serving, free_port
    port = free_port()
    base = f"http://127.0.0.1:{port}/mcp"
    async with Serving(mcp_server.http_app(world, tokens=("t",)), port):
        async with Client(http_transport(f"{base}?charts=png", "t"), mode="2026-07-28") as client:
            images = await client.call_tool("summary", CHARTED)
            without = await client.call_tool("summary", CHARTED, meta={"chart_image": "none"})
        async with Client(http_transport(base, "t"), mode="2026-07-28") as client:
            plain = await client.call_tool("summary", CHARTED)
            asked = await client.call_tool("summary", CHARTED, meta={"chart_image": "png"})
    for got in (images, asked):
        assert [b.type for b in got.content] == ["text", "image"] and len(got.meta[chart_images.CHARTS_META]) == 1
        assert images_of(got)[0] == charts.render_data(got.meta[chart_images.CHARTS_META][0], TZ, dark=False)
    for got in (without, plain):
        assert [b.type for b in got.content] == ["text"] and not ours(got)
        assert got.content[0].text == await world.service["summary"].call(CHARTED)


def test_every_prompt_name_is_a_telegram_command_and_none_is_one_of_the_bots_own():
    """The bot offers each prompt as a /command (docs/ecobot-mcp.md step 6c): Telegram's form, and no clash with the keyboard's."""
    import re
    from lib import templates
    from lib.mcp_server import PROMPTS
    for name in PROMPTS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", name), name
    assert not set(PROMPTS) & (set(templates.COMMANDS) - {"report"})   # (/report becomes the report prompt, step 6c)


# ---------- chart links (3.7: ?charts=link) ----------
BASE = "https://example.org/ecobot-charts"


def link_ids(result) -> list[str]:
    """The ids of a link-mode result's pictures, from its resource links."""
    return [str(b.uri).rsplit("/", 1)[-1].removesuffix(".png") for b in result.content if b.type == "resource_link"]


def headers_transport(url: str, token: str, headers: dict):
    """http_transport with extra headers on every request (as a proxy would add them)."""
    import contextlib
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    @contextlib.asynccontextmanager
    async def connect():
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}", **headers}, timeout=httpx2.Timeout(30.0)) as http:
            async with streamable_http_client(url, http_client=http) as streams:
                yield streams
    return connect()


async def test_a_call_asking_for_links_gets_each_chart_s_url_and_markdown_and_a_resource_link_but_no_image(world):
    world.chart_base_url = BASE
    async with Client(world.server) as client:
        got = await client.call_tool("summary", CHARTED, meta={"chart_image": "link"})
        png = await client.call_tool("summary", CHARTED, meta={"chart_image": "png"})
    told = json.loads(got.content[0].text)
    whole = json.loads(await world.service["summary"].call(CHARTED, {"chart_points": AS_DRAWN}))
    assert [b.type for b in got.content] == ["text", "resource_link"] and not images_of(got)
    (chart,) = told["charts"]
    (key,) = link_ids(got)
    assert chart == {"title": whole["charts"][0]["title"], "period": whole["charts"][0]["period"], "url": f"{BASE}/{key}.png",
                     "markdown": f"![{whole['charts'][0]['title']}]({BASE}/{key}.png)"}
    assert told["chart_note"] == chart_images.LINK_NOTE and "markdown" in told["chart_note"]
    assert {k: v for k, v in told.items() if k not in ("charts", "chart_note")} == {k: v for k, v in whole.items() if k not in ("charts", "chart_note")}
    link = got.content[1]
    assert str(link.uri) == chart["url"] and link.name == chart["title"] and link.mime_type == "image/png"
    assert got.meta[chart_images.CHARTS_META] == whole["charts"] and got.meta[chart_images.DARK_META] is False
    assert len(key) >= 22 and world.chart_links.get(key)[0] == images_of(png)[0]   # the picture png mode sends, byte for byte
    assert not got.is_error and COMPLETE not in got.meta


async def test_a_complete_answer_with_links_has_the_markdown_image_in_its_reply_for_the_person(world):
    world.chart_base_url = BASE
    args = {"readings": ["pressure"], "period": "this week", "chart_answers_it": True}
    async with Client(world.server) as client:
        got = await client.call_tool("summary", args, meta={"chart_image": "link"})
    data = json.loads(got.content[0].text)
    assert [b.type for b in got.content] == ["text", "resource_link", "text"] and data["complete"] is True
    (chart,) = data["charts"]
    assert data["reply"].endswith("\n\n" + chart["markdown"]) and got.content[2].text == data["reply"]
    assert got.content[2].annotations.audience == ["user"] and got.meta[COMPLETE] is True


async def test_links_without_pillow_or_without_a_chart_are_as_with_images(world, monkeypatch):
    async with Client(world.server) as client:
        refused = await client.call_tool("summary", {"period": "the age of the dinosaurs"}, meta={"chart_image": "link"})
        plain = await client.call_tool("current", {"readings": ["temperature"]}, meta={"chart_image": "link"})
        monkeypatch.setitem(sys.modules, "lib.charts", None)
        monkeypatch.setattr(chart_images, "_warned", True)
        unavailable = await client.call_tool("summary", CHARTED, meta={"chart_image": "link"})
    assert refused.is_error and [b.type for b in refused.content] == ["text"] and not ours(refused)
    assert [b.type for b in plain.content] == ["text"] and not ours(plain)
    data = json.loads(unavailable.content[0].text)
    assert [b.type for b in unavailable.content] == ["text"] and data["chart_image_note"] == chart_images.UNAVAILABLE_NOTE
    assert "url" not in data["charts"][0] and len(world.chart_links) == 0


def test_links_are_wanted_per_call_else_per_connection():
    assert chart_images.wanted({}, "link") == "link" and chart_images.wanted({"chart_image": "link"}) == "link"
    assert chart_images.wanted({"chart_image": "png"}, "link") == "png" and chart_images.wanted({"chart_image": "link"}, "png") == "link"
    assert chart_images.wanted({"chart_image": "none"}, "link") is None and chart_images.wanted({}, "LINK") == "link"


def test_the_links_store_is_bounded_by_count_and_bytes_and_forgets_after_its_hours():
    now = [1000.0]
    links = chart_images.Links(hours=2, most=3, most_bytes=10, clock=lambda: now[0])
    a, b, c = links.put(b"aaa"), links.put(b"bbb"), links.put(b"ccc")
    assert len({a, b, c}) == 3 and all(len(k) >= 22 for k in (a, b, c))
    assert links.get(a) == (b"aaa", 7200.0)
    d = links.put(b"d")                                     # a fourth: the oldest goes
    assert links.get(a) is None and links.get(d)[0] == b"d" and len(links) == 3
    e = links.put(b"eeee")                                  # 3 + 3 + 1 + 4 = 11 bytes > 10: the oldest goes again
    assert links.get(b) is None and links.bytes == 8 and links.get(e)[0] == b"eeee"
    now[0] += 3600
    assert links.get(c)[1] == 3600.0
    now[0] += 3600                                          # their two hours are up
    assert links.get(c) is None and links.get(e) is None and len(links) == 0 and links.bytes == 0
    assert links.get("nope") is None


def test_a_charts_markdown_keeps_its_brackets_and_lines_out_of_the_way():
    assert chart_images.markdown("Rain [mm]\nlast week", "u") == "![Rain (mm) last week](u)"
    assert chart_images.markdown(None, "u") == "![Chart](u)"


async def test_over_http_a_link_serves_the_png_without_a_token_or_an_allowed_host_while_mcp_still_needs_both(world, monkeypatch, caplog):
    import httpx
    from lib.upstream import http_transport
    from tests.test_mcp_listen import Serving, free_port
    caplog.set_level("INFO", logger="lib.mcp_server.requests")
    now = [time.time()]
    world.chart_links = chart_images.Links(hours=24, clock=lambda: now[0])
    port = free_port()
    base = f"http://127.0.0.1:{port}/mcp"
    async with Serving(mcp_server.http_app(world, tokens=("t",), dashboard=False), port):
        async with Client(http_transport(f"{base}?charts=link", "t"), mode="2026-07-28") as client:
            linked = await client.call_tool("summary", CHARTED)
            as_png = await client.call_tool("summary", CHARTED, meta={"chart_image": "png"})
            without = await client.call_tool("summary", CHARTED, meta={"chart_image": "none"})
        async with Client(http_transport(f"{base}?charts=png", "t"), mode="2026-07-28") as client:
            asked = await client.call_tool("summary", CHARTED, meta={"chart_image": "link"})
        async with Client(headers_transport(base, "t", {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "weather.example.org"}),
                          mode="2026-07-28") as client:
            forwarded = await client.call_tool("summary", CHARTED, meta={"chart_image": "link"})
        url = json.loads(linked.content[0].text)["charts"][0]["url"]
        assert url == f"http://127.0.0.1:{port}/charts/{link_ids(linked)[0]}.png"   # the MCP request's own address, with /charts
        assert json.loads(forwarded.content[0].text)["charts"][0]["url"].startswith("https://weather.example.org/charts/")
        async with httpx.AsyncClient() as http:
            got = await http.get(url, headers={"Host": "evil.example"})                # no token, any Host
            missing = await http.get(f"http://127.0.0.1:{port}/charts/{'x' * 22}.png")
            other = await http.get(f"http://127.0.0.1:{port}/charts/{link_ids(linked)[0]}")
            refused = await http.post(base, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                      headers={"Accept": "application/json, text/event-stream"})
            now[0] += 24 * 3600 + 1
            expired = await http.get(url)
    assert got.status_code == 200 and got.headers["content-type"] == "image/png" and got.content == images_of(as_png)[0]
    assert got.headers["cache-control"].startswith("private, max-age=") and 0 < int(got.headers["cache-control"].rsplit("=")[-1]) <= 86400
    assert missing.status_code == other.status_code == expired.status_code == 404 and refused.status_code == 401
    assert [b.type for b in as_png.content] == ["text", "image"] and [b.type for b in without.content] == ["text"]
    assert [b.type for b in asked.content] == ["text", "resource_link"]
    lines = [r.getMessage() for r in caplog.records if r.name == "lib.mcp_server.requests"]
    key = link_ids(linked)[0]
    assert any(f"GET /charts/{key[:6]}….png -> 200" in line for line in lines), lines
    assert any("-> 404, the chart's link has expired, or never was" in line for line in lines), lines
    assert not any(key in line for line in lines)


async def test_the_links_base_url_is_the_setting_when_set(world):
    from lib.upstream import http_transport
    from tests.test_mcp_listen import Serving, free_port
    world.chart_base_url = BASE
    port = free_port()
    async with Serving(mcp_server.http_app(world, tokens=("t",)), port):
        async with Client(headers_transport(f"http://127.0.0.1:{port}/mcp?charts=link", "t", {"X-Forwarded-Host": "other.example"}),
                          mode="2026-07-28") as client:
            got = await client.call_tool("summary", CHARTED)
    assert json.loads(got.content[0].text)["charts"][0]["url"] == f"{BASE}/{link_ids(got)[0]}.png"


def test_the_request_base_reads_the_proxy_s_headers_else_the_request_s_own():
    from starlette.requests import Request
    from lib.mcp_server import request_base

    def request(*headers):
        return Request({"type": "http", "scheme": "http", "server": ("127.0.0.1", 8765), "path": "/mcp", "query_string": b"",
                        "headers": [(k.encode(), v.encode()) for k, v in headers]})
    assert request_base(request(("host", "127.0.0.1:8765"))) == "http://127.0.0.1:8765"
    assert request_base(request(("host", "127.0.0.1:8765"), ("x-forwarded-proto", "https, http"),
                                ("x-forwarded-host", "api.example.org"))) == "https://api.example.org"
    assert request_base(None) == ""


async def test_the_bot_on_a_link_connection_still_gets_its_pictures_and_its_charts_json(world):
    from lib.weather import RemoteWeather
    from tests.test_mcp_listen import Serving, free_port
    port = free_port()
    async with Serving(mcp_server.http_app(world, tokens=("t",)), port):
        remote = RemoteWeather(f"http://127.0.0.1:{port}/mcp?charts=link", "t")
        task = asyncio.create_task(remote.run())
        try:
            await asyncio.wait_for(remote.listening.wait(), 20)
            out, image = await remote.chart_image("summary", CHARTED, {"chart_dark": False})
            plain = json.loads(await remote.call_text("summary", CHARTED))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await remote.close()
    assert image.png == charts.render_data(out["charts"][0], TZ, dark=False) and "url" not in out["charts"][0]
    assert "panels" in plain["charts"][0] and "url" not in plain["charts"][0] and len(world.chart_links) == 0
