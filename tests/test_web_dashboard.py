"""The gauges' live web page beside /mcp (lib/web_dashboard.py), under uvicorn as tests/test_mcp_listen.py serves the app: the page, its
data (the tiles gauge_sheet lays out from current with context), its events (one when the live reading's gauges change, from the
server's own change detection), no token and any Host for it while /mcp still asks for both, MCP_DASHBOARD off (404), the request log,
and a shutdown that doesn't wait on an open stream. Then the page in a browser (Playwright's Chromium, through node, skipped without
it) at a phone's, a tablet's and a desktop's sizes: every tile on the screen where the size allows, nothing wider than the window, no
console error; the tiles rearranged (dragged with a mouse and with a touch's long press, hidden by the × and the remove zone, kept
across reloads, reset), the drag's text left selectable, the fill of fewer tiles, and the tiles' clear backgrounds; a tile zoomed to
fill the window and the gallery through the others (keys, mouse drags, a trackpad's swipe, touch swipes, both ends, the text still
selectable, live readings); the weather behind the tiles (each effect at its reading's mark, its tile framed in red, several at once on
one canvas behind the tiles, "stop animations", reduced motion, the temporary test links and ?fx=, uv's beams swaying slowly in place
with no particles, the wind's leaves within the budget and blowing right to left, rain in the wind bunched into curtains), and the
data's rain rate. With ECOBOT_SCREENSHOTS set, its screenshots are kept in that folder."""

import asyncio
import base64
import functools
import html
import io
import json
import re
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
from lib.web_dashboard import DESCRIPTION, ICON, ICON_URL, PAGE, WebDashboard, air_readings_of, rain_rate_of, readings_of
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
    links = re.findall(r'<link[^>]*href="([^"]*)"', html)                                            # (its icons: the file beside it, as the
    assert preview_of(html)["og:image"] == ICON_URL                                                  # (the link preview's, on GitHub: only
                                                                                                      # a preview fetches it, not the browser)
    urls = [u for u in re.findall(r"https?://[^\s\"'<>]+", html) if u != "http://www.w3.org/2000/svg"]   # (the SVG namespace: a name)
    assert urls == [ICON_URL] and " src=" not in html and "data:" not in html and links == ["dashboard/icon.png"] * 2
    assert "<title>Weather Dashboard</title>" in html                                                 # nothing fetched from elsewhere
    assert data.status_code == 200
    got = data.json()
    sheet = gauge_sheet.sheet(station.current(gauge_sheet.call(), {"fetch": False}))
    assert got["tiles"] == [tile_json(t) for t in sheet.tiles] and got["as_of"] == AS_OF and got["look"] == look()
    titles = [t["title"] for t in got["tiles"]]
    assert titles[:6] == ["Temperature", "Humidity", "Atmospheric pressure", "Rain", "UV index", "US AQI"]   # gauge_sheet.FIRST
    assert titles[-6:] == ["PM2.5", "PM10", "PM1", "CO₂", "VOC index", "NOx index"]       # the rest in their order: the air sensor's after
    tiles = {t["title"]: t for t in got["tiles"]}
    aqi, nox = tiles["US AQI"], tiles["NOx index"]
    assert (aqi["value"], aqi["band"], aqi["bounds"], aqi["faded"]) == (56, "Moderate", [0, 50, 100, 150, 200, 300, 500], False)
    assert (nox["value"], nox["faded"], nox["floor"], nox["rows"]["Daily"]) == (None, True, 1, [12, 1])   # no reading now: dimmed
    assert tiles["Atmospheric pressure"]["floor"] == 950 and tiles["Temperature"]["floor"] == 0
    t = got["tiles"][0]
    assert (t["kind"], t["value"], t["unit"], t["columns"], t["faded"]) == ("thermometer", 12.9, "°C", ["Max", "Min"], False)
    assert set(t["rows"]) == {"Daily", "Monthly", "Yearly"} and (t["low"], t["high"], t["ticks"]) == (0, 40, [40, 30, 20, 10, 0])   # (12.9: 0 to 40)
    assert mcp.status_code == 421 and no_token.status_code == 401                  # /mcp: the Host, then the token, as before


async def test_the_data_carries_the_stations_rain_rate_now_its_rain_gauges_or_else_its_piezo_gauges(server, station):
    """The page's rain behind the tiles: the rain rate now (mm/h), the rain gauge's, or the piezo gauge's where that's the one reporting
    (in current's result, or in the kept live reading, where current gives it only when it is named); null with neither."""
    page = WebDashboard(server)
    assert (await page.data())["rain_rate"] == 0.0                              # (the October evening: dry)
    station.reads("rainfall", "rain_rate", "2.4")
    assert (await page.data())["rain_rate"] == 2.4
    station.live = {**station.live, "rainfall": {"daily": {"value": "0.2"}}, "rainfall_piezo": {"rain_rate": {"value": "0.6"}}}
    station.eco._live = (time.time(), station.live)                             # no rain gauge's rate: the piezo's, as kept
    assert (await page.data())["rain_rate"] == 0.6
    station.live = {k: v for k, v in station.live.items() if k != "rainfall_piezo"}
    station.eco._live = (time.time(), station.live)
    assert (await page.data())["rain_rate"] is None
    piezo = {"sensors": {"rainfall": {"rain": {"value": 1.0}}, "rainfall_piezo": {"rain_rate": {"value": 3.2, "unit": "mm/h"}}}}
    assert rain_rate_of(piezo) == 3.2 and rain_rate_of({"sensors": {"rainfall": {"rain_rate": {"value": None}}}}) is None
    assert rain_rate_of({"sensors": {"rainfall": {"rain_rate": {"value": 0.0}}, "rainfall_piezo": {"rain_rate": {"value": 3.2}}}}) == 0.0
    live = {"rainfall_piezo": {"rain_rate": {"value": "1.5"}}}
    assert readings_of({**station.live, **live}) != readings_of(station.live)    # the piezo's rate changing: an event, the page fetches it


async def test_the_page_answers_at_dashboard_slash_too_so_a_proxy_mapping_a_sites_root_there_gets_no_redirect_to_the_lan(server):
    # nginx's `location / { proxy_pass http://deb1:8765/dashboard/; }` sends the site's root as /dashboard/ with ecobot-mcp's own Host:
    # a redirect to /dashboard would be built from that Host, the LAN address, and break the page from the internet
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    async with Serving(http_app(server, tokens=(TOKEN,)), port):
        async with httpx.AsyncClient(follow_redirects=False) as http:
            slash = await http.get(f"{base}/dashboard/", headers={"Host": "192.168.1.208:8765"})
            plain = await http.get(f"{base}/dashboard")
            data = await http.get(f"{base}/dashboard/" + "data")          # the page at the site's root asks for /data: the proxy's /dashboard/data
    assert slash.status_code == 200 and "location" not in slash.headers
    assert preview_of(slash.text)["og:image"] == preview_of(plain.text)["og:image"] == ICON_URL
    same = lambda page: re.sub(r'="(?:dashboard/)?icon\.png"', '="ICON"', page)   # (the icon links)
    assert same(slash.text) == same(plain.text)                         # the same page but for its picture, beside it as it was asked for
    assert data.status_code == 200 and "tiles" in data.json()
    script = slash.text
    assert 'location.pathname.replace(/\\/+$/, "")' in script and 'BASE + "/data"' in script and 'BASE + "/events"' in script


PREVIEW_TAG = r'<meta (?:property|name)="((?:og|twitter):[^"]+)" content="([^"]*)">'


def preview_of(page: str) -> dict:
    """The page's Open Graph tags and Twitter card ({property or name: content})."""
    return {k: html.unescape(v) for k, v in re.findall(PREVIEW_TAG, page)}


async def test_the_link_preview_names_the_sun_icon_beside_the_page_as_it_was_asked_for(server):
    # A chat app's link preview reads the page's Open Graph tags; its picture can't be the page's icon, a data: URI: so the icon is a
    # file beside the page, named relative to the path the page was asked for, which is right behind any proxy: the site's root mapped
    # to /dashboard/ (public https://site/ -> "icon.png" -> https://site/icon.png -> /dashboard/icon.png), or /dashboard passed as it is
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    async with Serving(http_app(server, tokens=(TOKEN,)), port):
        async with httpx.AsyncClient() as http:
            plain = await http.get(f"{base}/dashboard", headers={"Host": "deb1:8765"})
            slash = await http.get(f"{base}/dashboard/", headers={"Host": "192.168.1.208:8765"})
            icon = await http.get(f"{base}/dashboard/icon.png", headers={"Host": "deb1:8765"})        # no token, any Host
            head = await http.head(f"{base}/dashboard/icon.png")
            ranged = await http.get(f"{base}/dashboard/icon.png", headers={"Range": "bytes=0-32767"})   # (as Slack asks): all of it
    for page, image in ((plain, "dashboard/icon.png"), (slash, "icon.png")):
        tags = preview_of(page.text)
        assert tags == {"og:type": "website", "og:title": "Weather Dashboard", "og:description": DESCRIPTION, "og:image": ICON_URL,
                        "og:image:type": "image/png", "og:image:width": "512", "og:image:height": "512", "og:image:alt": "A sun",
                        "twitter:card": "summary"}                                                   # no og:url: the page's public URL isn't known
        head_start = page.text.index("<head>")
        assert page.text.index('property="og:type"') - head_start < 100                              # first in the head: Slack reads 32 KB
        assert page.text.index('name="twitter:card"') < page.text.index('rel="icon"') < 32_768          # (all of them before the icons)
        assert "<!--link preview-->" not in page.text
        icons = dict(re.findall(r'<link rel="([\w-]+)"[^>]*href="([^"]*)"', page.text))
        assert icons == {"icon": image, "apple-touch-icon": image}       # the page's own icon is the same file: Signal and iMessage take it
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/png"
    assert icon.content == ICON.read_bytes() and int(icon.headers["content-length"]) == len(icon.content) < 300_000   # WhatsApp: < 300 KB
    assert "max-age=" in icon.headers["cache-control"] and "public" in icon.headers["cache-control"]
    assert head.status_code == 200 and head.headers["content-type"] == "image/png"
    assert ranged.status_code == 200 and ranged.content == icon.content


def test_the_icon_is_the_pages_own_sun_at_512_px():
    from PIL import Image
    big = Image.open(io.BytesIO(ICON.read_bytes()))
    assert big.format == "PNG" and big.size == (512, 512)                 # Facebook asks 200 px, WhatsApp 300
    assert big.mode == "RGBA" and big.getpixel((0, 0))[3] == 0 and big.getpixel((256, 256))[3] == 255   # the sun on a clear ground
    assert "data:" not in PAGE.read_text()                                 # the page carries no icon of its own: it links this one
    assert ICON_URL.endswith("/main/" + ICON.relative_to(Path(__file__).resolve().parent.parent).as_posix())   # og:image: this file on GitHub


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
        paths = ("/dashboard", "/dashboard/", "/dashboard/icon.png", "/dashboard/data", "/dashboard/events")
        assert [(await get(f"{base}{path}")).status_code for path in paths] == [404] * 5
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


def test_mcp_dashboard_animations_is_read_as_on_or_off_on_by_default_and_a_typo_keeps_it_on_with_a_warning(monkeypatch, tmp_path, caplog):
    from lib.config import Config
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_ECOWITT_API_KEY", "a")
    monkeypatch.setenv("MCP_ECOWITT_APP_KEY", "b")
    monkeypatch.delenv("MCP_DASHBOARD_ANIMATIONS", raising=False)
    assert Config.from_env(bot=False).mcp_dashboard_animations is True
    for value, want in (("off", False), ("OFF", False), ("on", True), ("", True)):
        monkeypatch.setenv("MCP_DASHBOARD_ANIMATIONS", value)
        assert Config.from_env(bot=False).mcp_dashboard_animations is want, value
    caplog.set_level("WARNING", logger="lib.config")
    monkeypatch.setenv("MCP_DASHBOARD_ANIMATIONS", "sometimes")
    assert Config.from_env(bot=False).mcp_dashboard_animations is True
    assert any("MCP_DASHBOARD_ANIMATIONS" in r.getMessage() and "'sometimes'" in r.getMessage() for r in caplog.records), caplog.records


async def test_the_page_is_told_whether_to_draw_the_weather_behind_its_gauges(server):
    """MCP_DASHBOARD_ANIMATIONS: the page served with data-animations="on" on its <html> (the default), or "off"; nothing else changes."""
    pages = {}
    for animations in (True, False):
        port = free_port()
        async with Serving(http_app(server, tokens=(TOKEN,), dashboard_animations=animations), port):
            pages[animations] = (await get(f"http://127.0.0.1:{port}/dashboard")).text
    assert pages[True].count('<html lang="en" data-animations="on">') == 1 and '<html lang="en" data-animations="off">' not in pages[True]
    assert pages[False].count('<html lang="en" data-animations="off">') == 1
    assert pages[False].replace('data-animations="off"', 'data-animations="on"', 1) == pages[True]


# ---------- the page in a browser ----------
SIZES = {"phone-portrait": (390, 844), "phone-landscape": (844, 390), "tablet-landscape": (1024, 768), "tablet-portrait": (768, 1024),
         "desktop": (1920, 1080)}


@functools.cache
def browser_there() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True, timeout=60).returncode == 0


@pytest.fixture
def chromium():
    """Skips the test without node and Playwright's Chromium: asked as a test runs, once in a worker (not as every worker collects the
    tests: a node each, a third of a second)."""
    if not browser_there():
        pytest.skip("needs node with Playwright's Chromium")


needs_browser = pytest.mark.usefixtures("chromium")


@needs_browser
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
        assert got["status"] == "last reading 18:42", (name, got)   # (the US AQI's 56, Moderate: smog behind the tiles)
        assert all(r["right"] <= width + 0.5 and r["left"] >= -0.5 for r in got["rects"]), (name, got)
        assert (shots / f"dashboard-{name}.png").exists() and (shots / f"dashboard-{name}-zoomed.png").exists()
        assert got["zoomed"]["zoomed"], (name, got["zoomed"])
        fills(got["zoomed"], width, height)                                       # zoomed: its title and gauge fill the window, no table
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


@needs_browser
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


# ---------- arranging the tiles (drag, hide, reset, preferences) and their fill ----------
async def run_page(server, *args, timeout=120) -> dict:
    """The page served, driven by the browser script with these arguments; its report."""
    port = free_port()
    async with Serving(http_app(server, tokens=(TOKEN,)), port):
        page = await asyncio.create_subprocess_exec("node", str(BROWSER), f"http://127.0.0.1:{port}/dashboard", *args,
                                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = await asyncio.wait_for(page.communicate(), timeout)
    assert page.returncode == 0, err.decode()
    return json.loads(out)


def titles_of(station) -> list[str]:
    return [t.title for t in gauge_sheet.sheet(station.current(gauge_sheet.call(), {"fetch": False})).tiles]


def moved(titles, title, after):
    """The titles with `title` taken out and put back just after `after`."""
    out = [t for t in titles if t != title]
    out.insert(out.index(after) + 1, title)
    return out


@needs_browser
async def test_tiles_are_dragged_to_reorder_hidden_by_the_x_or_the_remove_zone_kept_across_reloads_and_reset(server, station):
    got = await run_page(server, "--arrange")
    default = titles_of(station)
    assert got["errors"] == [] and got["initial"] == default, got["errors"]
    # mouseover: the grip and the × on the tile under the mouse only
    assert got["hover"] == [{"grip": "0", "close": "0"}, {"grip": "1", "close": "1"}, {"grip": "0", "close": "0"}]
    # dragged from its gauge: the others make room as it goes (transforms), the remove zone shows; the order kept
    assert got["during"] == {"zone": "none", "over": False, "dragging": True, "moved": ["Humidity", "Atmospheric pressure"]}
    reordered = moved(default, "Temperature", "Atmospheric pressure")
    assert got["dragged"] == reordered and got["transforms"] == 0
    assert got["storedAfterDrag"] == {"order": reordered, "hidden": []}
    # a drag starting on the title, the table or the reading moves nothing: it selects the text
    assert got["text"] == {"title": {"same": True, "dragging": False, "selected": "Rain"},
                           "table": {"same": True, "dragging": False, "selected": "85"},
                           "reading": {"same": True, "dragging": False, "selected": "70"}}
    # the ×, then the remove zone, hide; the reset link shows beside the time
    assert got["closed"] == {"titles": [t for t in reordered if t != "Rain"], "status": "reset preferences · last reading 18:42"}
    assert got["removing"]["over"] and got["removing"]["zone"] == "none"
    shown = [t for t in reordered if t not in ("Rain", "Wind")]
    assert got["removed"] == {"titles": shown, "stored": {"order": reordered, "hidden": ["Rain", "Wind"]}}
    assert got["reloaded"] == {"titles": shown, "status": "reset preferences · last reading 18:42"}
    # reset: every tile, in the default order, nothing kept, no link
    assert got["reset"] == {"titles": default, "status": "last reading 18:42", "stored": None}
    # unreadable preferences: the defaults; a tile new to the saved order (Rain) in its usual place, after Atmospheric pressure; an
    # absent tile's place and a later stage's keys kept
    assert got["corrupt"] == default
    saved = [t for t in default if t not in ("Rain", "Temperature")] + ["Temperature"]
    assert got["newTile"] == moved(saved, "Rain", "Atmospheric pressure")
    assert got["keptOthers"] == {"order": moved(got["newTile"], "Humidity", "Rain") + ["Gone sensor"], "hidden": ["Gone sensor"], "later": True}
    # a click 150 ms after a drop reaches the page (only the drop's own click, if the browser sends one, is swallowed)
    assert got["clickAfterDrop"] == ["tile"] and got["zoomedAfterDrop"]
    # the × pressed from the keyboard: the focus moves on to the next tile's ×, or with no tile left, to the reset link
    order = moved(got["newTile"], "Humidity", "Rain")
    assert got["keyboardHide"] == [f"Hide {order[0]}", f"Hide {order[1]}", "Hide Rain", "reset preferences"]
    assert got["keyboardTile"].startswith("Rain ")                              # (Tab reaches the tile itself before its ×)
    # storage blocked: it all works, nothing saved, no error
    assert got["blocked"] == {"titles": [t for t in moved(default, "Temperature", "Humidity") if t != "Rain"], "errors": []}
    # see-through: the tile's own background clear, its foreground (title, reading, frame) opaque, the page its dark colour
    look_ = got["look"]
    assert look_["background"] == "rgba(0, 0, 0, 0)" and look_["image"] == "none" and look_["grid"] == "rgba(0, 0, 0, 0)"
    assert look_["opacity"] == "1" and look_["body"] == "rgb(20, 25, 29)"
    assert all(look_[k].startswith("rgb(") for k in ("title", "reading")) and look_["frameWidth"] == "1px", look_
    # the frame unseen until the mouse is over its tile, then on that tile only
    assert look_["frame"] == "rgba(0, 0, 0, 0)", look_
    assert got["hoverFrames"] == ["rgba(0, 0, 0, 0)", "rgb(38, 46, 52)", "rgba(0, 0, 0, 0)"], got["hoverFrames"]


@needs_browser
async def test_on_a_touch_screen_a_long_press_picks_a_tile_up_and_a_tap_or_a_swipe_is_left_alone(server, station):
    got = await run_page(server, "--touch")
    default = titles_of(station)
    assert got["errors"] == [] and got["initial"] == default
    # a short tap: a click (it zooms the tile), nothing picked up; a swipe: the page scrolls
    assert got["tap"] == {"buzz": [], "clicks": 1, "scrollY": 0, "zone": False, "armed": [], "close": None, "titles": default, "zoomed": True}
    assert got["swipe"]["scrollY"] > 100 and got["swipe"]["armed"] == [] and got["swipe"]["titles"] == default
    # held: a buzz, its grip and ×, the remove zone; moved without scrolling the page; dropped in its new place
    assert got["held"] == {"buzz": [15], "clicks": 1, "scrollY": 0, "zone": True, "armed": ["Temperature"], "close": "1"}
    assert got["moving"]["scrollY"] == 0 and got["moving"]["during"]["moved"] == ["Humidity", "Atmospheric pressure"]
    reordered = moved(default, "Temperature", "Atmospheric pressure")
    assert got["dropped"]["titles"] == reordered and not got["dropped"]["zone"] and got["dropped"]["clicks"] == 1
    # held and let go in place: the grip and × stay, and a tap on the × hides it
    assert got["armed"]["armed"] == ["Humidity"] and got["armed"]["close"] == "1"
    assert got["closed"] == {"titles": [t for t in reordered if t != "Humidity"], "status": "reset preferences · last reading 18:42"}
    # held at the bottom edge: the page scrolls to its end, never past it, and stays there once let go
    edge = got["edge"]
    assert edge["seen"][-1] == pytest.approx(edge["end"], abs=1) and max(edge["seen"]) <= edge["end"] + 1, edge
    assert edge["after"] <= edge["end"] + 1, edge


@needs_browser
async def test_fewer_tiles_grow_evenly_to_fill_the_window_and_never_shrink_below_the_full_dashboards_size(server, station):
    every = titles_of(station)
    but = lambda *keep: [t for t in every if t not in keep]
    cases = {"desktop-all": (1920, 1080, []), "desktop-one": (1920, 1080, but("Temperature")),
             "tablet-all": (1024, 768, []), "tablet-three": (1024, 768, but("Temperature", "Wind", "US AQI")),
             "tablet-many": (1024, 768, ["Rain", "Wind"]),
             "phone-all": (390, 844, []), "phone-one": (390, 844, but("Humidity")), "phone-eight": (390, 844, every[:9])}
    got = await run_page(server, "--fill", json.dumps(cases))
    assert all(c["errors"] == [] and c["scrollWidth"] <= cases[name][0] for name, c in got.items()), got
    width = lambda name: {round(r["width"]) for r in got[name]["rects"]}
    aspect = look()["cell"][0] / look()["cell"][1]
    # one tile fills the window: its whole height (less the margins and the strip), or its width on a phone, centred
    (one,) = got["desktop-one"]["rects"]
    assert one["height"] == pytest.approx(1080 - 16 - 18, abs=1) and one["width"] == pytest.approx(one["height"] * aspect, abs=1)
    assert (one["left"] + one["right"]) / 2 == pytest.approx(1920 / 2, abs=1)
    (one,) = got["phone-one"]["rects"]
    assert one["width"] == pytest.approx(390 - 16, abs=1) and got["phone-one"]["scroll"] == "no"
    # a few grow, evenly, all on the screen and centred
    three = got["tablet-three"]["rects"]
    assert len(width("tablet-three")) == 1 and min(width("tablet-three")) > 1.5 * min(width("tablet-all")), got["tablet-three"]
    assert all(r["bottom"] <= 768 and r["right"] <= 1024 for r in three)
    assert (min(r["left"] for r in three) + max(r["right"] for r in three)) / 2 == pytest.approx(512, abs=1)
    # many: at least the full dashboard's size; a phone's scrolling tiles stay at theirs rather than shrink to fit
    assert min(width("tablet-many")) >= min(width("tablet-all")) and len(got["tablet-many"]["rects"]) == len(every) - 2
    assert width("phone-eight") == width("phone-all") and got["phone-eight"]["columns"] == "2" and len(got["phone-eight"]["rects"]) == 8


# ---------- the zoomed view and its gallery ----------
ZOOM_MS = 200
TABLE_TOP = 372     # (the web page's: where a tile's table starts, in its own pixels; above it the title and gauge, all a zoomed tile shows)


def zoomed_rect(width, height):
    """The zoomed tile's place at this window size: its title and gauge (no table) as large as fits beside the margins and the status
    strip, centred."""
    aspect = look()["cell"][0] / TABLE_TOP
    w = min(width - 16, (height - 16 - 18) * aspect)
    h = w / aspect
    return {"left": (width - w) / 2, "top": 8 + (height - 16 - 18 - h) / 2, "width": w, "height": h}


def near(rect, want, tolerance=1.0):
    return all(abs(rect[k] - want[k]) <= tolerance for k in ("left", "top", "width", "height"))


def fills(zl, width, height):
    """A zoomed tile's look (__look) at this window size: no table; its box (the title and gauge) as large as fits, its limiting axis
    within a few px of the window's room (less the margins and the status strip), centred, clear of the strip; its text inside it."""
    box, room = zl["box"], (width - 16, height - 16 - 18)
    assert zl["table"] == ["hidden 0"], zl
    assert box["width"] <= room[0] + 1 and box["height"] <= room[1] + 1, zl
    assert room[0] - box["width"] <= 3 or room[1] - box["height"] <= 3, zl                    # (one axis filled)
    assert box["width"] / box["height"] == pytest.approx(look()["cell"][0] / TABLE_TOP, rel=0.01), zl   # (its proportions kept)
    assert (box["left"] + box["right"]) / 2 == pytest.approx(width / 2, abs=1), zl
    assert (box["top"] + box["bottom"]) / 2 == pytest.approx(8 + room[1] / 2, abs=1) and box["bottom"] <= height - 18, zl
    for r in [zl["title"], *zl["texts"]]:                                                     # (the reading, a dial's band word)
        assert box["left"] - 1 <= r["left"] and r["right"] <= box["right"] + 1 and box["top"] - 1 <= r["top"] and r["bottom"] <= box["bottom"] + 1, (r, zl)


@needs_browser
async def test_a_click_on_a_gauge_zooms_its_tile_to_fill_the_window_the_tile_beside_it_live_and_the_keys_page(server, station):
    port = free_port()
    async with Serving(http_app(server, tokens=(TOKEN,)), port):
        page = await asyncio.create_subprocess_exec("node", str(BROWSER), f"http://127.0.0.1:{port}/dashboard", "--zoom", "in", stdin=subprocess.PIPE,
                                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        before = json.loads(await asyncio.wait_for(page.stdout.readline(), 60))
        station.reads("outdoor", "temperature", "21.5")                         # Temperature: beside the tile shown, off the screen
        await server.publish_changes()
        page.stdin.write(b"go\n")
        await page.stdin.drain()
        out, err = await asyncio.wait_for(page.communicate(), 120)
    assert page.returncode == 0, err.decode()
    got = json.loads(out)
    default = titles_of(station)
    at = default.index
    full = zoomed_rect(1024, 768)
    assert got["errors"] == [] and got["initial"] == default, got["errors"]
    assert default[:2] == ["Temperature", "Humidity"] and before["label"] == "Temperature 12.9 °C"
    # the cursor: a magnifier with + over a gauge, the usual one over the text; zoomed, a magnifier with − (around the tile too)
    assert got["cursor"] == {"gauge": "zoom-in", "title": "auto", "figure": "auto", "reading": "auto"}
    assert got["zoomedCursor"] == {"gauge": "zoom-out", "margin": "zoom-out", "title": "auto"}
    # a click on the gauge: its title and gauge (the grid tile's top part) grow from their place to fill the window, in 200 ms (a FLIP:
    # transforms only), ending exactly there; the title moving with the tile all the way (no jump); the table gone from the start
    zoom_in = got["zoomIn"]
    assert zoom_in["zoomed"] and zoom_in["shown"] == "Humidity" and ZOOM_MS in zoom_in["anims"], zoom_in
    grid_rect = got["gridRect"] | {"left": got["gridRect"]["x"], "top": got["gridRect"]["y"]}
    assert near(zoom_in["first"], grid_rect | {"height": grid_rect["width"] * TABLE_TOP / look()["cell"][0]}, 2), zoom_in
    assert near(zoom_in["last"], full) and zoom_in["between"] and 150 <= zoom_in["settledMs"] <= 330, zoom_in
    assert all(t == pytest.approx(zoom_in["titles"][0], abs=0.002) for t in zoom_in["titles"]), zoom_in["titles"]   # (in the tile's terms)
    tables = zoom_in["tables"]
    assert tables and set(tables) == {0}, tables
    assert got["grip"] == "none"                                                 # (no grip or × in the zoomed view)
    # zoomed: no table (the gallery's others neither); the title and gauge fill the window, centred, clear of the strip
    fills(got["zoomedLook"], 1024, 768)
    assert got["neighbourTable"] == ["hidden 0"], got["neighbourTable"]
    # live: the tile beside it, off the screen, takes the new reading in place, and shows it as it slides in
    assert got["live"] == {"label": "Temperature 21.5 °C", "sameTile": True, "zoomed": True, "shown": "Humidity", "track": -1024}
    keys = got["keys"]
    assert keys["left"]["shown"] == "Temperature" and keys["left"]["between"] and 150 <= keys["left"]["settledMs"] <= 330, keys
    assert keys["leftReading"] == "21.5 °C"
    # each key's default prevented: Backspace never goes back a page, Space never scrolls
    # ← at the first: a nudge and back, still the first
    assert keys["pastFirst"]["shown"] == "Temperature" and keys["pastFirst"]["track"] == 0 and keys["pastFirst"]["extreme"] >= 20, keys
    assert [keys[k]["shown"] for k in ("space", "right", "backspace")] == default[1:3] + default[1:2], keys
    assert all(k["zoomed"] for k in keys.values() if isinstance(k, dict)) and keys["url"]   # (Backspace: still this page)
    assert keys["prevented"] == [["ArrowLeft", True], ["ArrowLeft", True], [" ", True], ["ArrowRight", True], ["Backspace", True]], keys


async def zoomed(server, station, part: str) -> tuple[dict, list[str], dict]:
    """A part of the zoom at 1024x768 (its own page in its own browser: the parts run side by side): its report, no error; the tiles'
    titles; a tile's rect zoomed."""
    got = await run_page(server, "--zoom", part)
    default = titles_of(station)
    assert got["errors"] == [] and got["initial"] == default, got["errors"]
    return got, default, zoomed_rect(1024, 768)


@needs_browser
async def test_zoomed_the_gallery_pages_by_mouse_drags_and_a_trackpads_swipes_springing_back_at_the_first(server, station):
    got, default, full = await zoomed(server, station, "pager")
    # mouse drags on the gauge: followed directly; let go past halfway, the next; a short slow one back; a quick short flick on
    drag = got["drag"]
    assert drag["start"]["shown"] == default[1]
    assert drag["far"]["shown"] == default[2] and drag["far"]["during"] == pytest.approx(-1024 - 600, abs=12), drag
    assert drag["short"]["shown"] == default[2] and drag["short"]["track"] == -2048, drag
    assert drag["flick"]["shown"] == default[3] and drag["back"]["shown"] == default[2], drag
    # at the first, dragged on: it gives (less than the drag), springs back
    assert drag["pastFirst"]["shown"] == default[0] and 20 < drag["pastFirst"]["during"] < 300 * 0.6 and drag["pastFirst"]["track"] == 0, drag
    assert all(d["zoomed"] for d in drag.values())                                  # (the click ending a drag doesn't go back)
    # a trackpad's two-finger swipe (wheel deltaX): the next, or back if short; never reaching the browser
    wheel = got["wheel"]
    assert [wheel[k]["shown"] for k in ("far", "short", "back")] == [default[1], default[1], default[0]], wheel
    assert wheel["prevented"] and all(wheel["prevented"]), wheel


@needs_browser
async def test_zoomed_the_last_tile_stops_paging_its_text_is_selectable_a_click_goes_back_and_a_single_tile_zooms_too(server, station):
    got, default, full = await zoomed(server, station, "last")
    # the last: → past it nudges and stays, as do a swipe and a drag on
    last = got["last"]
    assert last["reached"]["shown"] == default[-1] and last["pastLast"]["shown"] == default[-1] and last["pastLast"]["extreme"] >= 20, last
    assert last["wheeled"]["shown"] == default[-1] and last["dragged"]["shown"] == default[-1], last
    assert last["wheeled"]["track"] == last["dragged"]["track"] == -1024 * (len(default) - 1)
    # a drag on the text selects it: no paging, still zoomed
    text = got["text"]
    assert {k: (v["selected"], v["shown"], v["zoomed"]) for k, v in text.items()} == {
        "title": (default[-1], default[-1], True), "reading": (text["reading"]["selected"], default[-1], True)}, text
    assert text["reading"]["selected"].strip(), text
    # a click without moving: back to the dashboard in 200 ms, the tile in its place in the grid
    zoom_out = got["zoomOut"]
    whole = full | {"height": full["width"] * look()["cell"][1] / look()["cell"][0]}   # (the whole tile, from there: its row height rounded, scaled up)
    assert not zoom_out["zoomed"] and near(zoom_out["first"], whole, 3) and zoom_out["between"] and 150 <= zoom_out["settledMs"] <= 330, zoom_out
    assert near(zoom_out["last"], got["outRect"] | {"left": got["outRect"]["x"], "top": got["outRect"]["y"]})
    assert zoom_out["last"]["width"] == pytest.approx(got["gridRect"]["width"], abs=0.5)
    tables = zoom_out["tables"]                                                   # (its table back: hidden as it shrinks, then fading in)
    assert tables[0] == 0 and tables[-1] == 1 and all(a <= b for a, b in zip(tables, tables[1:])) and got["outTable"] == ["visible 1"], (tables, got["outTable"])
    # a single tile (Humidity, the others hidden) zooms too, a gallery of one
    one = got["single"]
    # the magnifier over its gauge; a click zooms it to its title and gauge alone, filling the window
    assert one["cursor"] == "zoom-in" and one["zoomed"] | {"look": None} == {"zoomed": True, "shown": "Humidity", "track": 0,
                                                                             "cursor": "zoom-out", "look": None}, one
    fills(one["zoomed"]["look"], 1024, 768)
    assert near(one["zoomed"]["look"]["box"], full), one["zoomed"]
    # a gallery of one: the keys nudge it and back, a drag either way gives a little and springs back, as does a trackpad's swipe
    for name, key in one["keys"].items():
        assert key["zoomed"] and key["shown"] == "Humidity" and key["track"] == 0 and key["extreme"] >= 20, (name, key)
    for name, drag in one["dragged"].items():
        assert drag["zoomed"] and drag["shown"] == "Humidity" and drag["track"] == 0 and 20 < abs(drag["during"]) < 400 * 0.6, (name, drag)
    assert one["wheeled"] | {"prevented": None} == {"zoomed": True, "shown": "Humidity", "track": 0, "prevented": None}, one["wheeled"]
    assert one["wheeled"]["prevented"] and all(one["wheeled"]["prevented"]), one["wheeled"]
    # a click goes back, the tile in its place; Esc too
    box = lambda r: r | {"left": r["x"], "top": r["y"]}
    rect = one["back"].pop("rect")
    assert one["back"] == {"zoomed": False, "shown": "Humidity", "track": 0} and near(box(rect), box(one["gridRect"])), (one["back"], rect)
    assert one["esc"]["zoomed"] is False, one["esc"]


@needs_browser
async def test_a_double_click_esc_the_bounce_cut_short_the_keyboard_and_esc_mid_zoom(server, station):
    got, default, full = await zoomed(server, station, "out")
    # Esc: back too, the order as it was
    assert got["esc"]["before"]["zoomed"] and got["esc"]["before"]["shown"] == "Wind" and not got["esc"]["after"]["zoomed"]
    assert got["esc"]["titles"] == default
    # (a double-click: its second click ignored, so Esc above found it still zoomed; and a click on a gauge doesn't take the focus, so
    # a key pressed after it shows no × on that tile)
    assert got["esc"]["close"] == {"focused": False, "opacity": "0"}, got["esc"]
    # the bounce past the last, cut short: by a key, that step slides from the start; by Esc, the dashboard at once in its place
    cut = got["bounceCut"]
    last_x = -1024 * (len(default) - 1)
    assert cut["step"]["shown"] == default[-2] and cut["step"]["mid"] and all(last_x + 100 < x < last_x + 1024 for x in cut["step"]["mid"]), cut   # (on its way, ~100 ms in)
    assert cut["esc"]["grid"] == "none" and not cut["esc"]["zoomed"] and 0 <= cut["esc"]["firstLeft"] < 100, cut
    # the keyboard: Enter on a focused tile zooms it; the focus goes along, the other tiles out of reach (inert); Esc back, Space too
    kb = got["keyboard"]
    assert kb["tabbable"] and kb["focusedClose"] == "1", kb
    assert kb["enter"] | {"track": 0} == {"zoomed": True, "shown": "Temperature", "track": 0, "focus": "Temperature", "reachable": ["Temperature"]}, kb
    assert kb["right"]["shown"] == kb["right"]["focus"] == "Humidity" and kb["right"]["reachable"] == ["Humidity"], kb
    assert kb["tab"]["focus"] != "Temperature" and kb["tab"]["focus"] not in default[2:] and kb["tab"]["zoomed"], kb
    assert not kb["esc"]["zoomed"] and kb["esc"]["focus"] == "Humidity" and kb["esc"]["reachable"] == default, kb
    assert kb["space"]["zoomed"] and kb["space"]["shown"] == "Humidity" == kb["space"]["focus"], kb
    # Esc mid-way through a zoom-in: back from where the tile is drawn (no jump on to the full size), its table hidden until it's in its
    # grid place; the others keep their tables while they fade out (not all gone at once on the first frame)
    esc = got["escMid"]
    assert esc["others"] == ["visible 1"] and not esc["zoomed"], esc
    assert esc["atEsc"] < full["width"] - 50 and esc["peakAfter"] < esc["atEsc"] * 1.15, esc
    assert esc["tableWhileLarge"] == 0 and esc["tableEnd"] == 1, esc
    assert esc["last"]["width"] == pytest.approx(got["gridRect"]["width"], abs=0.5), esc


@needs_browser
async def test_on_a_touch_screen_a_tap_zooms_a_swipe_pages_from_anywhere_and_a_tap_goes_back_where_the_page_was(server, station):
    got = await run_page(server, "--zoom-touch")
    default = titles_of(station)
    assert got["errors"] == [] and got["initial"] == default, got["errors"]
    t = default.index(got["tapped"])
    assert got["scrolled"] > 100
    assert got["zoomed"]["zoomed"] and got["zoomed"]["shown"] == got["tapped"]
    assert not got["backStraight"]["zoomed"] and abs(got["backStraight"]["scrollY"] - got["scrolled"]) <= 2, got["backStraight"]
    assert near(got["zoomed"]["rect"] | {"left": got["zoomed"]["rect"]["x"], "top": got["zoomed"]["rect"]["y"]}, zoomed_rect(390, 844))
    fills(got["zoomed"]["look"], 390, 844)
    assert [got[k]["shown"] for k in ("next", "previous", "flick", "fromTitle")] == [default[t + 1], default[t], default[t + 1], default[t + 2]], got
    assert got["vertical"]["shown"] == default[t + 2] and got["vertical"]["zoomed"]
    assert got["held"] == {"zoomed": True, "shown": default[t + 2], "track": got["vertical"]["track"], "buzz": [], "zone": False}
    assert got["backAt"]["shown"] == default[t - 1]
    assert not got["out"]["zoomed"] and got["out"]["titles"] == default and got["out"]["lastShown"], got["out"]


# ---------- the weather behind the tiles ----------
EFFECTS = {"rain": "Rain", "smog": "US AQI", "uv": "UV index", "humidity": "Humidity", "wind": "Wind", "heat": "Temperature",
           "ice": "Temperature"}


async def weather(server, part: str) -> dict:
    """One part of the weather behind the tiles, its own page in its own browser (the parts run side by side): its report, no error."""
    got = await run_page(server, "--weather", part)
    assert got["errors"] == [], got["errors"]
    return got


@needs_browser
async def test_the_weather_behind_the_tiles_comes_with_its_reading_at_its_mark_and_frames_its_tile_in_red(server, station):
    got = await weather(server, "marks")
    # nothing called for (the US AQI Good): no effect, no frame, no link, no loop, nothing drawn
    assert got["nothing"] == {"effects": [], "levels": {k: 0 for k in got["nothing"]["levels"]}, "running": False, "framed": [],
                              "link": None, "frames": 0, "ink": 0}
    # each effect at its mark and not just short of it (rain above 0 mm/h, the AQI's Moderate band from 51, UV 11, 90 %, wind above
    # 15 km/h, 35 °C, below 0 °C, not at it), its tile framed in red, the strip's link with it
    for name, mark in got["marks"].items():
        effect = "ice" if name == "iceAtZero" else name
        assert mark["below"] | {"levels": None} == {"effects": [], "levels": None, "running": mark["below"]["running"], "framed": [],
                                                     "link": None}, (name, mark)
        assert mark["at"]["effects"] == [effect] and mark["at"]["framed"] == [EFFECTS[effect]], (name, mark)
        assert mark["at"]["link"] == "stop animations" and mark["at"]["running"], (name, mark)
    assert got["smogBands"] == [2, 3, 4, 5, 5]                                   # thicker with each band: USG to Hazardous
    assert got["rainStrength"][0] < 0.4 and got["rainStrength"][1:] == [1, 1]    # light at 0.5 mm/h, heavy from 8
    assert got["iceDepth"] == [0.5, 5]                                           # further in the colder


@needs_browser
async def test_several_effects_share_one_canvas_behind_the_see_through_tiles_zoomed_too(server, station):
    got = await weather(server, "several")
    # several at once: one canvas, one loop, at most 30 frames drawn a second
    several = got["several"]
    assert several["effects"] == ["smog", "uv", "rain", "wind", "humidity", "ice"] and several["canvases"] == 1, several
    assert sorted(several["framed"]) == sorted({EFFECTS[k] for k in several["effects"]}) and several["ink"] > 0, several
    assert 20 <= several["drawn"] <= 32 and several["frames"] <= 64, several    # (a second's: one request a display frame, every other one drawn)
    # the canvas: filling the window, fixed, first, behind the tiles, never in the pointer's way, its pixels crisp
    assert got["canvas"] == {"rect": [0, 0, 1024, 768], "canvas": [True, True], "position": "fixed", "z": "0", "events": "none",
                             "first": True, "pixelated": "pixelated", "under": True}
    # the tiles see-through (what's behind shows in a tile's margin), their foreground opaque; zoomed too
    assert got["through"] == {"differs": True, "still": True}
    assert got["look"] == {"background": "rgba(0, 0, 0, 0)", "opacity": "1", "title": "rgb(232, 234, 237)", "frame": "rgba(0, 0, 0, 0)",
                           "alertFrame": "rgba(0, 0, 0, 0)",   # (the red frame off for now; the class still set)
                           # dragged, a framed tile shows the drag's frames as any other: lifted grey, then the remove zone's red
                           "dragFrames": {"lifted": "rgb(90, 102, 112)", "doomed": "rgb(229, 72, 77)", "after": "rgba(0, 0, 0, 0)"}}
    assert got["zoomed"] == {"zoomed": True, "title": "UV index", "background": "rgba(0, 0, 0, 0)", "grid": "rgba(0, 0, 0, 0)",
                             "alert": True, "running": True}


@needs_browser
async def test_the_weather_can_be_stopped_kept_stopped_across_a_reload_and_started_again_or_reset(server, station):
    got = await weather(server, "stop")
    # "stop animations": nothing drawn, no loop, the frame stays, kept across a reload; "start animations" and the reset put them back
    assert got["stopped"] | {"levels": None} == {"effects": ["uv"], "levels": None, "running": False, "framed": ["UV index"],
                                                 "link": "start animations", "frames": 0, "ink": 0, "stored": False}, got["stopped"]
    assert got["reloaded"]["link"] == "start animations" and got["reloaded"]["frames"] == 0 and not got["reloaded"]["running"]
    for step in ("restarted", "reset"):
        assert got[step]["link"] == "stop animations" and got[step]["running"] and got[step]["frames"] > 10, (step, got[step])
    # from the keyboard: Enter on it stops them, the focus kept on the link
    assert got["keyboard"]["link"] == got["keyboard"]["focused"] == "start animations" and not got["keyboard"]["running"], got["keyboard"]


@needs_browser
async def test_the_temporary_test_links_turn_each_effect_on_and_clear_them_and_with_animations_off_nothing_is_drawn(server, station):
    got = await weather(server, "links")
    # the temporary test links: each its effect and frame (the gauges keep their readings), the URL following; smog steps through its
    # bands, rain light then heavy; ice melts back; "clear" ends them all; no "all" link, but ?fx=all
    links = got["links"]
    assert links["buttons"] == ["rain", "smog", "uv", "humidity", "wind", "heat", "ice", "clear"], links["buttons"]
    on = []
    for name in ("uv", "humidity", "wind", "heat", "ice"):
        on.append(name)
        assert set(links[name]["effects"]) == set(on) and EFFECTS[name] in links[name]["framed"], (name, links[name])
        assert links[name]["url"] == "?fx=" + ",".join(on)
    assert links["label"] == "UV index 0.0"                                        # (the stand-in's reading, not the simulated one)
    # "stop animations" at the bottom left, just right of the row's "clear", beside the strip (at the window's bottom), clear of it
    corner = links["corner"]
    assert corner["link"] == "stop animations" and corner["apart"] and corner["onScreen"], corner
    just_right_of_clear(corner)
    assert corner["row"]["left"] == 0 and corner["motion"]["bottom"] == corner["window"][1] == corner["strip"]["bottom"], corner
    assert links["smog"] == [1, 2, 3, 4, 5, 0] and links["rain"] == [0.35, 1, 0]
    melting = links["melting"]
    assert melting["grown"] > 2 and 0 < melting["reach"] < melting["grown"] and "ice" not in melting["effects"] and melting["running"], melting
    assert links["cleared"] | {"levels": None, "running": None} == {"effects": [], "levels": None, "running": None, "framed": [],
                                                                     "link": None, "url": ""}
    assert links["all"]["effects"] == EVERY and links["all"]["url"] == "?fx=rain2,smog3,uv,humidity,wind,heat,ice"
    assert links["role"] == "group" and links["pressed"] == {"rain": "false", "smog": "false", "uv": "true", "humidity": "true",
                                                             "wind": "true", "heat": "true", "ice": "false"}, links["pressed"]
    # MCP_DASHBOARD_ANIMATIONS off (the page as served then, data-animations="off"), every effect asked for (?fx=all) and called for by
    # the readings, the animations not stopped in the browser: no loop, nothing drawn, no link, no test links; the tiles and the
    # strip as ever
    off = got["off"]
    assert off["off"] == {"effects": off["off"]["effects"], "levels": off["off"]["levels"], "running": False, "framed": off["off"]["framed"],
                          "link": None, "frames": 0, "ink": 0, "sky": "none", "corner": "none", "row": False}, off["off"]
    assert off["off"]["effects"] == EVERY, off["off"]                                    # (asked for all the same: just never drawn)
    assert off["tiles"] == titles_of(station) and off["strip"] == "last reading 18:42", off
    # a tile hidden: "reset preferences" in the strip as ever; still nothing drawn
    assert off["hidden"] == {"strip": "reset preferences · last reading 18:42", "frames": 0, "ink": 0, "running": False}, off["hidden"]


EVERY = ["smog", "heat", "uv", "rain", "wind", "humidity", "ice"]


def just_right_of_clear(corner):
    """ "stop animations" right after the test row, on its line, a few pixels right of its "clear" (the row's end, in sight)."""
    motion, row, clear = corner["motion"], corner["row"], corner["clear"]
    assert motion["left"] == pytest.approx(row["right"], abs=0.5) and -1 <= motion["left"] - clear["right"] <= 10, corner
    assert row["left"] <= clear["left"] and clear["right"] <= row["right"] + 1, corner   # (-1, +1: the row scrolled to a whole px)
    assert motion["bottom"] == pytest.approx(row["bottom"], abs=0.5) and motion["top"] == pytest.approx(row["top"], abs=0.5), corner


@needs_browser
async def test_every_effect_at_once_is_cheap_no_brighter_than_the_strongest_alone_and_within_the_particle_budget(server, station):
    got = await weather(server, "all")
    assert got["cost"] < 8, got["cost"]
    # several at once no brighter than one strong one: every effect on (the ice grown all the way in), against Hazardous smog alone
    assert 0 < got["light"]["all"] <= got["light"]["smog5"], got["light"]
    together = got["particles"]["all"]
    assert together["uv"] == 0 and together["wind"] > 0 and together["rain"] > 0, together
    assert sum(v for k, v in together.items() if k != "budget") <= together["budget"] + 3, together   # (each rounded)


@needs_browser
async def test_effects_asked_for_as_the_page_opens_alone_on_a_phone_and_with_reduced_motion(server, station):
    got = await weather(server, "opened")
    # ?fx= when the page opens
    assert got["param"]["levels"] == {"smog": 3, "heat": 0, "uv": 0, "rain": 0, "wind": 1, "humidity": 0, "ice": 5}
    assert sorted(got["param"]["framed"]) == ["Temperature", "US AQI", "Wind"] and got["param"]["errors"] == []
    # (the effects' earlier names, in links saved before, still work: as wind, uv, humidity)
    assert got["paramOld"]["effects"] == ["uv", "wind", "humidity"] and got["paramOld"]["errors"] == [], got["paramOld"]
    assert got["paramAll"]["effects"] == EVERY and got["paramRain"]["levels"]["rain"] == 1
    # the particles: uv's beams none (no motes); the wind's leaves alone within the shared budget
    assert got["particles"]["uv"] == {"uv": 0, "budget": got["particles"]["uv"]["budget"]}, got["particles"]
    wind = got["particles"]["wind"]
    assert 0 < wind["wind"] <= wind["budget"], wind
    # uv's beams over 40 s: each swaying a pixel or so about its own place (not travelling on), so slowly that it turns back at most
    # once in that time (a sway in 80 s or more), still brightening and dimming
    uv = got["uv"]
    assert uv["errors"] == [] and uv["frames"] >= 300 and len(uv["beams"]) >= 4, uv
    assert all(b["most"] - b["least"] <= 4 and b["turns"] <= 1 and b["bright"] - b["faint"] > 0.2 for b in uv["beams"]), uv
    assert any(b["most"] > b["least"] for b in uv["beams"]), uv   # (swaying all the same)
    assert all(a["most"] < b["least"] for a, b in zip(uv["beams"], uv["beams"][1:])), uv   # (each in its own place, apart)
    # heavy rain in the wind: its drops blown leftward, faster than with no wind; bunched by the gust bands into curtains and gaps,
    # their density across uneven, and changing as the bands pass, far more than the rain alone (scattered evenly: about 1)
    alone, windy = got["rain"]["alone"], got["rain"]["windy"]
    assert alone["errors"] == windy["errors"] == [] and alone["looks"] >= 20 and windy["looks"] >= 20, got["rain"]
    assert windy["vx"] < alone["vx"] < 0 and alone["drops"] > 30 and windy["drops"] > 30, got["rain"]
    assert alone["across"] < 1.4 and alone["overTime"] < 1.4, got["rain"]
    assert windy["across"] > max(2, 1.6 * alone["across"]) and windy["overTime"] > max(2, 1.6 * alone["overTime"]), got["rain"]
    # the wind blows right to left: its leaves leftward overall, as drawn on the canvas too (frame to frame); some caught in a loop
    # going rightward a moment (on a phone too)
    for seen in (got["wind"], got["phoneWind"]):
        assert seen["vx"] < -10 and seen["against"] > 0 and seen["frames"] > 20 and seen["left"] > 3 * seen["right"] > 0, seen
    # the strip at the bottom right: its links, then the time at the far right; on a phone with both its links, the test row
    # beside it or above it, never over it
    phone = got["phone"]
    assert phone["strip"] == "reset preferences · last reading 18:42" and phone["right"], phone
    assert phone["apart"] and phone["onScreen"], phone
    # "stop animations" at the bottom left, just right of the test row's "clear" (with the row above the strip on a phone, both clear
    # of it); without the test row (the page as it will be), at the bottom left on its own, at 1024x768 and on a phone
    corner = got["phoneCorner"]
    assert corner["link"] == "stop animations" and corner["apart"] and corner["onScreen"], corner
    just_right_of_clear(corner)
    assert corner["motion"]["bottom"] == pytest.approx(corner["strip"]["top"], abs=0.5), corner                  # (above the strip)
    for bare in (got["bare"], got["barePhone"]):
        assert bare["errors"] == [] and bare["row"] is None and bare["link"] == "stop animations", bare
        assert bare["motion"]["left"] == 0 and bare["motion"]["bottom"] == bare["window"][1] and bare["apart"] and bare["onScreen"], bare
    # reduced motion: no loop, one still, faint frame (the ice as it would be), the frames and the link as ever
    still = got["still"]
    assert still["effects"] == ["uv", "ice"] and not still["running"] and still["frames"] == 0 and still["stateFrames"] == 0, still
    assert still["ink"] > 0 and sorted(still["framed"]) == ["Temperature", "UV index"] and still["link"] == "stop animations", still
