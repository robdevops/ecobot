"""The gauges' live web page beside /mcp (lib/web_dashboard.py), under uvicorn as tests/test_mcp_listen.py serves the app: the page, its
data (the tiles gauge_sheet lays out from current with context), its events (one when the live reading's gauges change, from the
server's own change detection), no token and any Host for it while /mcp still asks for both, MCP_DASHBOARD off (404), the request log,
and a shutdown that doesn't wait on an open stream. Then the page in a browser (Playwright's Chromium, through node, skipped without
it) at a phone's, a tablet's and a desktop's sizes: every tile on the screen where the size allows, nothing wider than the window, no
console error. With ECOBOT_SCREENSHOTS set, its screenshots are kept in that folder."""

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from datetime import date
from types import SimpleNamespace as NS

import httpx
import pytest

from lib import gauge_sheet
from lib.gauge_look import look, tile_json
from lib.mcp_server import AIR_URI, EcobotServer, WEATHER_URI, http_app
from lib.service.context import Context
from lib.service.names import Catalogue
from lib.web_dashboard import air_readings_of, readings_of
from tests.chart_samples import _air, _station
from tests.fakes import TZ, StandInService, gauges_result
from tests.test_mcp_listen import Serving, free_port, until

TOKEN = "s3cret"
AS_OF = {"text": "Tue 27 Oct 2026 6:42pm", "time": "2026-10-27T18:42:00+11:00"}
BROWSER = Path(__file__).with_name("web_dashboard_browser.js")


RAW = {"pm2_5": "pm02", "pm10": "pm10", "pm1": "pm01", "co2": "rco2", "voc_index": "tvocIndex", "nox_index": "noxIndex"}   # the monitor's names


class Station:
    """The fake station and air sensor: the station's kept live reading and the air sensor's latest (what ecobot://now/weather and
    ecobot://now/air are compared from) and current's result with context over them (the days' figures of chart_samples' October
    evening: the air sensor sends no NOx index just now)."""

    def __init__(self):
        self.live, self.values, self.today = _station()
        self.latest, self.air_values = _air(self.today)
        self.eco = NS(_live=(time.time(), self.live), storage=None, tz=TZ)
        self.air = NS(_current=None)
        self._keep_air()

    def _keep_air(self):
        row = {"timestamp": "2026-10-27T07:42:00Z", **{RAW[k]: e["value"] for k, e in self.latest.items()}}
        self.air._current = (row, time.time())

    def current(self, args, meta):
        assert args == gauge_sheet.call() and meta == {"fetch": False}   # every gauge's reading, with context; from what is held
        return {"as_of": AS_OF, **gauges_result(self.live, self.values, self.today, self.latest, self.air_values)}

    def reads(self, group: str, field: str, value: str):
        self.live = json.loads(json.dumps(self.live))
        self.live[group][field]["value"] = value
        self.eco._live = (time.time(), self.live)

    def air_reads(self, column: str, value: float | None):
        """The air sensor's latest has this reading (None: it hasn't)."""
        self.latest = {k: e for k, e in self.latest.items() if k != column}
        if value is not None:
            self.latest[column] = {"value": value, "unit": ""}
        self._keep_air()


@pytest.fixture
def station():
    return Station()


@pytest.fixture
def server(station):
    ctx = Context(eco=station.eco, air=station.air, tz=TZ)
    ctx.catalogue = Catalogue([])
    server = EcobotServer(ctx, None)
    server._service = StandInService(current=station.current)
    return server


async def get(url: str, **headers) -> httpx.Response:
    async with httpx.AsyncClient() as http:
        return await http.get(url, headers=headers)


async def test_the_page_and_its_data_are_served_without_a_token_to_any_host_and_the_data_is_gauge_sheets_tiles(server, station):
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    async with Serving(http_app(server, tokens=(TOKEN,), allowed_hosts=("127.0.0.1",)), port):
        page = await get(f"{base}/dashboard", Host="deb1:8765")                       # by the machine's name: not an allowed host
        data = await get(f"{base}/dashboard/data", Host="deb1:8765")
        mcp = await get(f"{base}/mcp", Host="deb1:8765", Authorization=f"Bearer {TOKEN}")
        no_token = await get(f"{base}/mcp", Accept="text/event-stream")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    html = page.text
    assert html.startswith("<!DOCTYPE html>") and "<script>" in html and "EventSource" in html
    assert "https://" not in html and "<link" not in html and " src=" not in html                  # nothing fetched from elsewhere
    assert data.status_code == 200
    got = data.json()
    sheet = gauge_sheet.sheet(station.current(gauge_sheet.call(), {"fetch": False}))
    assert got["tiles"] == [tile_json(t) for t in sheet.tiles] and got["as_of"] == AS_OF and got["look"] == look()
    titles = [t["title"] for t in got["tiles"]]
    assert titles[:3] == ["Temperature", "Humidity", "Atmospheric pressure"]
    assert titles[-7:] == ["US AQI", "PM2.5", "PM10", "PM1", "CO₂", "VOC index", "NOx index"]                    # the air sensor's, after
    tiles = {t["title"]: t for t in got["tiles"]}
    aqi, nox = tiles["US AQI"], tiles["NOx index"]
    assert (aqi["value"], aqi["band"], aqi["bounds"], aqi["faded"]) == (56, "Moderate", [0, 50, 100, 150, 200, 300, 500], False)
    assert (nox["value"], nox["faded"], nox["floor"], nox["rows"]["Daily"]) == (None, True, 1, [12, 1])   # no reading now: dimmed
    assert tiles["Atmospheric pressure"]["floor"] == 950 and tiles["Temperature"]["floor"] == 0
    t = got["tiles"][0]
    assert (t["kind"], t["value"], t["unit"], t["columns"], t["faded"]) == ("thermometer", 12.9, "°C", ["Max", "Min"], False)
    assert set(t["rows"]) == {"Daily", "Monthly", "Yearly"} and (t["low"], t["high"], t["ticks"]) == (0, 40, [40, 30, 20, 10, 0])   # (12.9: 0 to 40)
    assert mcp.status_code == 421 and no_token.status_code == 401                  # /mcp: the Host, then the token, as before


async def test_the_data_says_so_when_the_station_gives_nothing(server):
    server._service = StandInService(current={"sensors": {}, "unavailable": {"station": "The weather station isn't answering right now."}})
    port = free_port()
    async with Serving(http_app(server, tokens=(TOKEN,)), port):
        got = (await get(f"http://127.0.0.1:{port}/dashboard/data")).json()
    assert got["tiles"] == [] and got["unavailable"] == "The weather station isn't answering right now."



async def test_the_data_is_answered_from_what_is_held_and_never_asks_the_station(tmp_path, daytime_archive):
    """Any host on the LAN may ask for the data, as often as it likes: with the kept reading stale (the station or the cloud not
    answering) it is still answered from it, and with none kept the page says the station isn't answering; nothing is fetched."""
    from lib.ecowitt.source import LIVE_GROUPS
    from tests.fakes import archived_station
    eco, fake = await archived_station(tmp_path, daytime_archive)
    live = await eco._fetch_live(list(LIVE_GROUPS))
    fake.calls.clear()

    async def asked(*a, **k):
        raise AssertionError("the dashboard asked the station")

    eco._live_data, eco._fetch_live = asked, asked
    ctx = Context(eco=eco, tz=eco.tz)
    ctx.catalogue = Catalogue([])
    server = EcobotServer(ctx, None)
    port = free_port()
    try:
        async with Serving(http_app(server, tokens=(TOKEN,)), port):
            eco._live = None
            nothing = await get(f"http://127.0.0.1:{port}/dashboard/data")
            eco._live = (time.time() - 3600, live)                                     # an hour old: well past LIVE_FRESH_SECONDS
            stale = [await get(f"http://127.0.0.1:{port}/dashboard/data") for _ in range(3)]
    finally:
        await eco.close()
    assert nothing.status_code == 200 and nothing.json()["tiles"] == [] and nothing.json()["unavailable"]
    assert all(r.status_code == 200 for r in stale)
    tiles = {t["title"]: t for t in stale[0].json()["tiles"]}
    assert tiles["Temperature"]["value"] is not None and "unavailable" not in stale[0].json()
    assert fake.calls == []

async def sse_events(url: str, into: list, opened: asyncio.Event):
    """Read a Server-Sent Events stream, keeping each event's name, until it ends."""
    async with httpx.AsyncClient(timeout=None) as http:
        async with http.stream("GET", url) as response:
            assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
            async for line in response.aiter_lines():
                opened.set()
                if line.startswith("event: "):
                    into.append(line[len("event: "):])
    into.append("(ended)")


async def test_an_event_is_sent_when_the_gauges_live_reading_changes_and_the_stream_ends_promptly_at_shutdown(server, station, caplog):
    caplog.set_level("INFO", logger="lib.mcp_server.requests")
    port = free_port()
    events, opened = [], asyncio.Event()
    async with Serving(http_app(server, tokens=(TOKEN,)), port) as serving:
        reader = asyncio.create_task(sse_events(f"http://127.0.0.1:{port}/dashboard/events", events, opened))
        await asyncio.wait_for(opened.wait(), 5)
        await server.publish_changes()                                   # nothing changed since the server started
        station.reads("outdoor", "temperature", "13.4")
        assert await server.publish_changes() == [WEATHER_URI]
        await until(lambda: events == ["readings"], what="the event")
        station.live = {**station.live, "outdoor": {**station.live["outdoor"], "extra": {"value": "x"}}}   # a change no gauge shows:
        station.eco._live = (time.time() + 60, station.live)                                              # published, but no event
        assert await server.publish_changes() == [WEATHER_URI]
        station.reads("wind", "wind_speed", "9")
        await server.publish_changes()
        await until(lambda: events == ["readings", "readings"], what="the second event")
        started = time.monotonic()
        await serving.stop()                                             # the open stream doesn't hold the shutdown up
        assert time.monotonic() - started < 2
        await asyncio.wait_for(reader, 5)
    assert events == ["readings", "readings", "(ended)"]
    lines = [r.getMessage() for r in caplog.records if r.name == "lib.mcp_server.requests"]
    assert sum("GET /dashboard/events -> 200" in line for line in lines) == 1, lines   # logged once, when it opened


def test_what_the_events_compare_is_the_gauges_readings_with_the_gust():
    live, _, _ = _station()
    assert readings_of(None) is None and readings_of({}) is None
    shown = readings_of(live)
    assert 12.9 in shown and 1026.0 in shown
    assert readings_of({**live, "wind": {"wind_speed": {"value": "5"}, "wind_gust": {"value": "11"}}}) != shown


async def test_with_mcp_dashboard_off_there_is_no_page_and_mcp_is_as_it_was(server, caplog):
    caplog.set_level("INFO", logger="lib.mcp_server.requests")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    async with Serving(http_app(server, tokens=(TOKEN,), dashboard=False), port):
        assert [(await get(f"{base}{path}")).status_code for path in ("/dashboard", "/dashboard/data", "/dashboard/events")] == [404] * 3
        assert (await get(f"{base}/mcp", Accept="text/event-stream")).status_code == 401
    lines = [r.getMessage() for r in caplog.records if r.name == "lib.mcp_server.requests"]
    assert any("refused: GET /dashboard/data -> 404" in line for line in lines), lines


def test_mcp_dashboard_is_read_as_on_or_off(monkeypatch, tmp_path):
    from lib.config import Config
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_ECOWITT_API_KEY", "a")
    monkeypatch.setenv("MCP_ECOWITT_APP_KEY", "b")
    monkeypatch.delenv("MCP_DASHBOARD", raising=False)
    assert Config.from_env(bot=False).mcp_dashboard is True
    monkeypatch.setenv("MCP_DASHBOARD", "off")
    assert Config.from_env(bot=False).mcp_dashboard is False


# ---------- the page in a browser ----------
SIZES = {"phone-portrait": (390, 844), "phone-landscape": (844, 390), "tablet-landscape": (1024, 768), "tablet-portrait": (768, 1024),
         "desktop": (1920, 1080)}


def browser_there() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True, timeout=60).returncode == 0


@pytest.mark.skipif(not browser_there(), reason="needs node with Playwright's Chromium")
async def test_the_page_fits_its_tiles_to_the_window_at_each_size_with_no_overflow_and_no_console_error(server, station, tmp_path):
    shots = Path(os.getenv("ECOBOT_SCREENSHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    port = free_port()
    url = f"http://127.0.0.1:{port}/dashboard"
    async with Serving(http_app(server, tokens=(TOKEN,)), port):
        sizes = await asyncio.create_subprocess_exec("node", str(BROWSER), url, str(shots), json.dumps(SIZES), stdout=subprocess.PIPE,
                                                     stderr=subprocess.PIPE)   # (not asyncio.to_thread: the tests run it inline)
        out, err = await asyncio.wait_for(sizes.communicate(), 180)
        assert sizes.returncode == 0, err.decode()
        report = json.loads(out)
        # a new reading: the open page fetches it and moves its gauges in place, without a reload or redrawing the tiles
        live = await asyncio.create_subprocess_exec("node", str(BROWSER), url, "--live", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                    stderr=subprocess.PIPE)
        before = json.loads(await asyncio.wait_for(live.stdout.readline(), 60))
        station.reads("outdoor", "temperature", "21.5")
        await server.publish_changes()
        live.stdin.write(b"go\n")
        await live.stdin.drain()
        out, err = await asyncio.wait_for(live.communicate(), 60)
        assert live.returncode == 0, err.decode()
        after = json.loads(out)
    n = len(gauge_sheet.sheet(Station().current(gauge_sheet.call(), {"fetch": False})).tiles)
    for name, (width, height) in SIZES.items():
        got = report[name]
        assert got["errors"] == [], (name, got["errors"])
        assert got["tiles"] == n and got["scrollWidth"] <= width, (name, got)
        assert got["status"] == "updated 18:42", (name, got)
        assert all(r["right"] <= width + 0.5 and r["left"] >= -0.5 for r in got["rects"]), (name, got)
        assert (shots / f"dashboard-{name}.png").exists()
    for name in ("tablet-landscape", "tablet-portrait", "desktop"):            # these fit every tile without scrolling
        got = report[name]
        assert got["scroll"] == "no" and got["scrollHeight"] <= SIZES[name][1], (name, got)
        assert all(r["bottom"] <= SIZES[name][1] + 0.5 for r in got["rects"]), (name, got)
        assert min(r["width"] for r in got["rects"]) >= 160, (name, got)
    assert report["desktop"]["columns"] == "6" and report["tablet-portrait"]["columns"] == "4" and report["tablet-landscape"]["columns"] == "6"
    assert report["phone-portrait"]["columns"] == "2"                         # too small for all at once: two to a row, scrolling
    assert before["label"] == "Temperature 12.9 °C" and after["label"] == "Temperature 21.5 °C", (before, after)
    assert after["same_page"] and after["errors"] == [], after
    assert after["same_tile"] and "21.5 °C" in after["shown"], after                 # the gauge moved in place to the new reading


@pytest.mark.skipif(not browser_there(), reason="needs node with Playwright's Chromium")
@pytest.mark.parametrize("comes", [True, False])
async def test_a_tile_with_no_reading_is_dimmed_at_its_floor_and_a_reading_coming_brightens_it_and_moves_it_up_in_place(server, station,
                                                                                                                     comes):
    """The air sensor sends no NOx index: its tile is dimmed (L.fade), its needle at the dial's start and its number the floor (1). A
    reading arriving: the same tile brightens and its gauge moves up from the floor, frame by frame, nothing drawn again (no flash). A
    reading going away (the VOC index): it dims and drops to its floor."""
    title, column, value = ("NOx index", "nox_index", 60) if comes else ("VOC index", "voc_index", None)
    port = free_port()
    async with Serving(http_app(server, tokens=(TOKEN,)), port):
        page = await asyncio.create_subprocess_exec("node", str(BROWSER), f"http://127.0.0.1:{port}/dashboard", "--arrive", title,
                                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        before = json.loads(await asyncio.wait_for(page.stdout.readline(), 60))
        station.air_reads(column, value)
        assert await server.publish_changes() == [AIR_URI]                       # the air sensor's change: the page is told
        page.stdin.write(b"go\n")
        await page.stdin.drain()
        out, err = await asyncio.wait_for(page.communicate(), 60)
        assert page.returncode == 0, err.decode()
    got = json.loads(out)
    after, frames = got["after"], got["frames"]
    assert got["errors"] == [] and got["same_tile"], got                         # moved in place: the same tile, not drawn again
    numbers = [float(f["number"]) for f in frames]
    opacities = [f["opacity"] for f in frames]
    if comes:
        assert before["faded"] and before["opacity"] == pytest.approx(look()["fade"]) and before["number"] == "1", before
        assert before["label"] == "NOx index: no reading"
        assert not after["faded"] and after["opacity"] == 1 and after["number"] == "60" and after["label"] == "NOx index 60"
        assert numbers == sorted(numbers) and numbers[-1] == 60 and len(set(numbers)) > 3, numbers   # up from the floor, smoothly
        assert opacities == sorted(opacities) and opacities[0] < 1, opacities                       # brightening, never a flash
        assert before["needle"] != after["needle"]
    else:
        assert not before["faded"] and before["number"] == "112" and "Average" in before["shown"], before
        assert after["faded"] and after["opacity"] == pytest.approx(look()["fade"]) and after["number"] == "1", after
        assert "Average" not in after["shown"] and after["label"] == "VOC index: no reading"   # no category without a reading
        assert numbers == sorted(numbers, reverse=True) and numbers[-1] == 1, numbers              # down to the floor
        assert opacities == sorted(opacities, reverse=True), opacities


async def test_an_event_is_sent_when_the_air_sensors_gauges_readings_change(server, station):
    port = free_port()
    events, opened = [], asyncio.Event()
    async with Serving(http_app(server, tokens=(TOKEN,)), port) as serving:
        reader = asyncio.create_task(sse_events(f"http://127.0.0.1:{port}/dashboard/events", events, opened))
        await asyncio.wait_for(opened.wait(), 5)
        await server.publish_changes()
        station.air_reads("co2", 910)
        assert await server.publish_changes() == [AIR_URI]
        await until(lambda: events == ["readings"], what="the event")
        station.air_reads("pm1", 30.0)                                          # PM1's gauge: an event
        assert await server.publish_changes() == [AIR_URI]
        await until(lambda: events == ["readings"] * 2, what="the second event")
        station.air_reads("nox_index", 4)                                       # a reading the sensor didn't have: an event
        await server.publish_changes()
        await until(lambda: events == ["readings"] * 3, what="the third event")
        await serving.stop()
        await asyncio.wait_for(reader, 5)
    assert events == ["readings"] * 3 + ["(ended)"]


def test_what_the_events_compare_of_the_air_is_its_gauges_readings():
    latest, _ = _air(date(2026, 10, 27))
    row = {RAW[k]: e["value"] for k, e in latest.items()}
    assert air_readings_of(None) is None and air_readings_of({}) is None
    assert air_readings_of(row) == (12.0, 18.0, 7.0, 520, 112, None)           # PM2.5 (the AQI's too), PM10, PM1, CO2, VOC, NOx
    assert air_readings_of({**row, "atmp": 99}) == air_readings_of(row)         # its own temperature: no gauge
