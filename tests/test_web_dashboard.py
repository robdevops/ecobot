"""The gauges' live web page beside /mcp (lib/web_dashboard.py), under uvicorn as tests/test_mcp_listen.py serves the app: the page, its
data (the tiles gauge_sheet lays out from current with context), its events (one when the live reading's gauges change, from the
server's own change detection), no token and any Host for it while /mcp still asks for both, MCP_DASHBOARD off (404), MCP_DASHBOARD_INDOOR
off (no indoor reading drawn, in the data or in the events), the request log,
and a shutdown that doesn't wait on an open stream. Then the page in a browser (Playwright's Chromium, through node, skipped without
it) at a phone's, a tablet's and a desktop's sizes: every tile on the screen where the size allows, nothing wider than the window, no
console error; the tiles rearranged (dragged with a mouse and with a touch's long press, hidden by the × and the remove zone, kept
across reloads, reset), the strip's reading time with a device's reading over an hour old (greyed out), the drag's text left selectable, the fill of fewer tiles, and the tiles' clear backgrounds; a tile zoomed to
fill the window and the gallery through the others (keys, mouse drags, a trackpad's swipe, touch swipes, both ends, the text still
selectable, live readings); the weather behind the tiles (each effect at its reading's mark, its tile framed in red, several at once on
one canvas behind the tiles, "stop animation", reduced motion, the temporary test links and ?fx=, uv's beams swaying slowly in place
with no particles, the wind's leaves within the budget and blowing right to left, rain in the wind bunched into curtains, its drops
knocking the leaves they hit, humidity's fog and its beads, heavy rain's drops sticking to the glass, a flash flood filling the window
a quarter up, its waves, its leaves and its frog, a cow, a barn or a chicken blown across in a gust over 99 km/h), the data's rain
rate, and the
dials' thin red hands at the past hour's highest (the wind's gust, rain rate, sun, UV: read from the file and the reading now, moving
smoothly, at once with reduced motion) with the wind's Gust column.
With ECOBOT_SCREENSHOTS set, its screenshots are kept in that folder."""

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
from datetime import date, datetime
from types import SimpleNamespace as NS

import httpx
import pytest

from lib import gauge_sheet
from lib.gauge_look import look, tile_json
from lib.mcp_server import AIR_URI, EcobotServer, WEATHER_URI, http_app
from lib.service.context import Context
from lib.service.names import Catalogue
from lib.ecowitt.dashboard import GUST
from lib.ecowitt.reads import highest_on
from lib.gauge_look import START, SWEEP
from lib.web_dashboard import (DESCRIPTION, HOUR_SECONDS, HOURLY, ICON, ICON_URL, PAGE, WebDashboard, air_readings_of, rain_rate_of,
                               readings_of)
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
        self.air_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())   # (its latest's time: now, fresh; air_from moves it)
        self._keep_air()

    def _keep_air(self):
        row = {"timestamp": self.air_at, **{RAW[k]: e["value"] for k, e in self.latest.items()}}
        self.air._current = (row, time.time())

    def current(self, args, meta):
        assert args == gauge_sheet.call() and meta == {"fetch": False}   # every gauge's reading, with context; from what is held
        return {"as_of": AS_OF, **gauges_result(self.live, self.values, self.today, self.latest, self.air_values)}

    def reads(self, group: str, field: str, value: str):
        self.live = json.loads(json.dumps(self.live))
        self.live.setdefault(group, {}).setdefault(field, {})["value"] = value
        self.eco._live = (time.time(), self.live)

    def air_from(self, at: float):
        """The air sensor's latest was taken at this epoch."""
        self.air_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
        self._keep_air()

    def held(self, highs: dict):
        """The station's file holds these highest of the past hour ({(group, field): value}, highest_on's), as the dashboard reads it."""
        async def read(fn, mac, wheres, start, end):
            assert fn is highest_on and mac == "fake" and end - start == HOUR_SECONDS + 60 and abs(end - 60 - time.time()) < 5
            return {w: v for w, v in highs.items() if w in wheres}
        self.eco.storage, self.eco.mac = NS(read=read), "fake"

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


RATE, SOLAR, UVI = ("rainfall", "rain_rate"), ("solar_and_uvi", "solar"), ("solar_and_uvi", "uvi")


async def test_the_data_carries_the_past_hours_highest_of_the_wind_gust_rain_rate_sun_and_uv_and_the_gust_now(server, station):
    """The dials' thin red hands: each one's reading's highest of the past hour (Wind: its gust), the higher of the file's (its minutes
    and 5-minute readings: highest_on) and the reading now; with no file, the reading now; null with neither. And the gust now."""
    page = WebDashboard(server)
    assert HOURLY == {"Wind": GUST, "Rain rate": RATE, "Solar radiation": SOLAR, "UV index": UVI}
    got = await page.data()
    assert got["hour_highs"] == {"Rain rate": 0.0, "Solar radiation": 0.0, "UV index": 0.0, "Wind": None} and got["gust"] is None
    station.reads("wind", "wind_gust", "12")                                     # (no file: the readings now alone)
    got = await page.data()
    assert got["hour_highs"]["Wind"] == 12.0 and got["gust"] == 12.0
    station.held({GUST: 37.0, RATE: 4.2, SOLAR: 820.0})                         # the file's past hour: higher than now, but the UV's
    station.reads("solar_and_uvi", "uvi", "2")
    got = await page.data()
    assert got["hour_highs"] == {"Wind": 37.0, "Rain rate": 4.2, "Solar radiation": 820.0, "UV index": 2.0} and got["gust"] == 12.0
    station.reads("wind", "wind_gust", "41")                                     # a gust above the past hour's: it is the highest
    assert (await page.data())["hour_highs"]["Wind"] == 41.0
    station.held({})                                                             # the peaks an hour old: the next highest, the readings now
    assert (await page.data())["hour_highs"] == {"Wind": 41.0, "Rain rate": 0.0, "Solar radiation": 0.0, "UV index": 2.0}
    station.live = json.loads(json.dumps(station.live))                          # a reading now from over an hour ago: not counted
    station.live["wind"]["wind_gust"]["time"] = str(int(time.time()) - HOUR_SECONDS - 120)
    station.eco._live = (time.time(), station.live)
    assert (await page.data())["hour_highs"]["Wind"] is None

    async def broken(*a):
        raise OSError("disk")
    station.eco.storage = NS(read=broken)                                        # the file not read: the readings now
    assert (await page.data())["hour_highs"]["UV index"] == 2.0
    station.reads("wind", "wind_gust", "9")
    assert readings_of(station.live) != readings_of({**station.live, "wind": {**station.live["wind"], "wind_gust": {"value": "10"}}})


async def test_the_past_hours_highest_is_read_from_the_minutes_and_the_5_minute_readings_with_their_bands(tmp_path):
    """highest_on: of a reading's minutes and 5-minute readings from start to end, each with the highest of its band, a band alone (a
    5-minute mark not fetched yet) too; nothing older or newer, nor a 30-minute reading; one with none left out."""
    from lib.storage import Storage
    storage = Storage(tmp_path / "s.sqlite", TZ)
    mac, now = "AA:BB", 1_794_279_900
    with storage.writer.transaction() as w:
        w.put_minutes(mac, {"wind": {"wind_gust": {"unit": "km/h", "list": {str(now - 4000): "70", str(now - 3000): "33", str(now - 60): "21"}}},
                            "solar_and_uvi": {"uvi": {"unit": "", "list": {str(now - 120): "3"}}}}, now)
        w.put_ranges(mac, {"wind.wind_gust": {now - 900: (3, 20.0, 45.0)}, "solar_and_uvi.solar": {now - 600: (2, 400.0, 610.0)}})
    got = await storage.read(highest_on, mac, [GUST, SOLAR, UVI, RATE], now - 3600, now)
    assert got == {GUST: 45.0, SOLAR: 610.0, UVI: 3.0}
    assert await storage.read(highest_on, mac, [GUST], now - 600, now) == {GUST: 21.0}
    storage.close()


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


async def test_the_data_carries_each_devices_own_reading_time_and_none_for_a_device_not_configured(server, station):
    """Beside as_of (the newer of the two), each device's own reading time, for the strip's stalled device: the station's newest reading's
    own time in its kept live reading (else when that was kept), the air sensor's latest's; fresh, one stale (each), both, recovered."""
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    local = lambda at: datetime.fromtimestamp(int(at), TZ).isoformat()
    now = time.time()
    async with Serving(http_app(server, tokens=(TOKEN,)), port):
        data = lambda: get(f"{base}/dashboard/data")
        station.eco._live = (now, station.live)                        # both fresh (the station's reading with no time of its own)
        station.air_from(now - 60)
        assert (await data()).json()["devices"] == {"station": local(now), "air": local(now - 60)}
        station.air_from(now - 5400)                                   # the air sensor stalled
        assert (await data()).json()["devices"] == {"station": local(now), "air": local(now - 5400)}
        stamped = json.loads(json.dumps(station.live))                 # the station stalled: its readings' own times, the newest
        stamped["outdoor"]["temperature"]["time"], stamped["wind"]["wind_speed"]["time"] = str(int(now - 7300)), str(int(now - 7200))
        station.eco._live = (now, stamped)
        station.air_from(now - 30)
        assert (await data()).json()["devices"] == {"station": local(now - 7200), "air": local(now - 30)}
        station.air_from(now - 9000)                                   # both stalled
        assert (await data()).json()["devices"] == {"station": local(now - 7200), "air": local(now - 9000)}
        station.eco._live = (now, station.live)                        # both reporting again
        station.air_from(now)
        assert (await data()).json()["devices"] == {"station": local(now), "air": local(now)}
        station.eco._live, station.air._current = None, None           # nothing held
        assert (await data()).json()["devices"] == {"station": None, "air": None}
        server.ctx.air = None                                          # no air sensor configured: never named
        station.eco._live = (now, station.live)
        assert (await data()).json()["devices"] == {"station": local(now)}


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
    assert readings_of({**live, "wind": {**live["wind"], "wind_direction": {"value": "300"}}}) != shown   # (the compass's)


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


INDOOR_TITLES = ["Indoor temperature", "Indoor humidity"]


async def test_with_mcp_dashboard_indoor_off_no_indoor_reading_is_drawn_or_sent_in_the_data_or_the_events(server, station):
    """MCP_DASHBOARD_INDOOR (privacy: the page needs no token): on (the default), the indoor tiles as any other's; off, none of them in
    /dashboard/data (no indoor value anywhere in it: as with no indoor sensor) and an indoor change alone sends no event, the events
    carrying nothing indoor; the rest of the data, and the page itself, as with it on."""
    station.reads("indoor", "temperature", "23.7")
    station.reads("indoor", "humidity", "47")
    got, pages = {}, {}
    port = free_port()
    async with Serving(http_app(server, tokens=(TOKEN,)), port):                     # on: the default
        got[True] = (await get(f"http://127.0.0.1:{port}/dashboard/data")).text
        pages[True] = (await get(f"http://127.0.0.1:{port}/dashboard")).text
    port = free_port()
    events, opened, raw = [], asyncio.Event(), []
    async with Serving(http_app(server, tokens=(TOKEN,), dashboard_indoor=False), port) as serving:
        got[False] = (await get(f"http://127.0.0.1:{port}/dashboard/data")).text
        pages[False] = (await get(f"http://127.0.0.1:{port}/dashboard")).text

        # the events: an indoor change alone sends none (nor tells one happened); an outdoor one does, and nothing indoor in it
        async def read():
            async with httpx.AsyncClient(timeout=None) as http, http.stream("GET", f"http://127.0.0.1:{port}/dashboard/events") as response:
                async for line in response.aiter_lines():
                    opened.set()
                    raw.append(line)
                    if line.startswith("event: "):
                        events.append(line[len("event: "):])
        reader = asyncio.create_task(read())
        await asyncio.wait_for(opened.wait(), 5)
        await server.publish_changes()
        station.reads("indoor", "temperature", "24.9")
        assert await server.publish_changes() == [WEATHER_URI]                   # (published to MCP clients as ever)
        station.reads("indoor", "humidity", "52")
        assert await server.publish_changes() == [WEATHER_URI]
        station.reads("outdoor", "temperature", "13.4")
        await server.publish_changes()
        await until(lambda: events == ["readings"], what="the outdoor change's event")
        await serving.stop()
        await asyncio.wait_for(reader, 5)
    on, off = json.loads(got[True]), json.loads(got[False])
    titles = [t["title"] for t in on["tiles"]]
    assert set(INDOOR_TITLES) <= set(titles) and WebDashboard(server).indoor is True
    assert [t["title"] for t in off["tiles"]] == [t for t in titles if t not in INDOOR_TITLES]
    assert off["tiles"] == [t for t in on["tiles"] if t["title"] not in INDOOR_TITLES]
    assert {k: v for k, v in off.items() if k != "tiles"} == {k: v for k, v in on.items() if k != "tiles"}
    assert "indoor" not in got[False].lower() and "23.7" not in got[False] and "indoor" in got[True].lower()
    assert pages[False] == pages[True] and "indoor" not in re.sub(r"<!--.*?-->|//[^\n]*", "", pages[False]).lower()
    assert events == ["readings"] and not any("indoor" in line.lower() or "24.9" in line or "52" in line for line in raw), raw
    # ... and with it on, an indoor change alone sends one
    assert readings_of(station.live) != readings_of({**station.live, "indoor": {"temperature": {"value": "30"}}})
    assert readings_of(station.live, False) == readings_of({**station.live, "indoor": {"temperature": {"value": "30"}}}, False)
    dashboard = WebDashboard(server, (WEATHER_URI, AIR_URI))
    queue = asyncio.Queue()
    dashboard._streams.add(queue)
    station.reads("indoor", "temperature", "26.1")
    await server.publish_changes()
    assert queue.qsize() == 1


def test_mcp_dashboard_indoor_is_read_as_on_or_off_on_by_default_and_a_typo_keeps_it_on_with_a_warning(monkeypatch, tmp_path, caplog):
    from lib.config import Config
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_ECOWITT_API_KEY", "a")
    monkeypatch.setenv("MCP_ECOWITT_APP_KEY", "b")
    monkeypatch.delenv("MCP_DASHBOARD_INDOOR", raising=False)
    assert Config.from_env(bot=False).mcp_dashboard_indoor is True
    for value, want in (("off", False), ("no", False), ("FALSE", False), ("0", False), ("on", True), ("yes", True), ("", True)):
        monkeypatch.setenv("MCP_DASHBOARD_INDOOR", value)
        assert Config.from_env(bot=False).mcp_dashboard_indoor is want, value
    caplog.set_level("WARNING", logger="lib.config")
    monkeypatch.setenv("MCP_DASHBOARD_INDOOR", "hidden")
    assert Config.from_env(bot=False).mcp_dashboard_indoor is True
    assert any("MCP_DASHBOARD_INDOOR" in r.getMessage() and "'hidden'" in r.getMessage() for r in caplog.records), caplog.records


def test_mcp_dashboard_animations_is_read_as_on_off_or_low_on_by_default_and_a_typo_keeps_it_on_with_a_warning(monkeypatch, tmp_path, caplog):
    from lib.config import Config
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_ECOWITT_API_KEY", "a")
    monkeypatch.setenv("MCP_ECOWITT_APP_KEY", "b")
    monkeypatch.delenv("MCP_DASHBOARD_ANIMATIONS", raising=False)
    assert Config.from_env(bot=False).mcp_dashboard_animations == "on"
    for value, want in (("off", "off"), ("OFF", "off"), ("no", "off"), ("on", "on"), ("true", "on"), ("", "on"), ("low", "low"), ("LOW", "low")):
        monkeypatch.setenv("MCP_DASHBOARD_ANIMATIONS", value)
        assert Config.from_env(bot=False).mcp_dashboard_animations == want, value
    caplog.set_level("WARNING", logger="lib.config")
    monkeypatch.setenv("MCP_DASHBOARD_ANIMATIONS", "sometimes")
    assert Config.from_env(bot=False).mcp_dashboard_animations == "on"
    assert any("MCP_DASHBOARD_ANIMATIONS" in r.getMessage() and "'sometimes'" in r.getMessage() for r in caplog.records), caplog.records


async def test_the_page_is_told_whether_to_draw_the_weather_behind_its_gauges_and_whether_anything_moves(server):
    """MCP_DASHBOARD_ANIMATIONS: the page served with data-animations="on" on its <html> (the default), "off" or "low" (low motion);
    nothing else changes. (True and False, as earlier: "on" and "off"; anything else refused.)"""
    pages = {}
    for animations in ("on", "off", "low", True, False, None):
        port = free_port()
        async with Serving(http_app(server, tokens=(TOKEN,), **({} if animations is None else {"dashboard_animations": animations})), port):
            pages[animations] = (await get(f"http://127.0.0.1:{port}/dashboard")).text
    assert pages["on"].count('<html lang="en" data-animations="on">') == 1 and '<html lang="en" data-animations="off">' not in pages["on"]
    assert pages["off"].count('<html lang="en" data-animations="off">') == 1
    assert pages["low"].count('<html lang="en" data-animations="low">') == 1
    for value in ("off", "low"):
        assert pages[value].replace(f'data-animations="{value}"', 'data-animations="on"', 1) == pages["on"], value
    assert pages[True] == pages[None] == pages["on"] and pages[False] == pages["off"]
    with pytest.raises(ValueError, match="sometimes"):
        WebDashboard(server, animations="sometimes")


# ---------- the page in a browser ----------
SIZES = {"phone-portrait": (390, 844), "phone-landscape": (844, 390), "tablet-landscape": (1024, 768), "tablet-portrait": (768, 1024),
         "desktop": (1920, 1080)}


@functools.cache
def browser_there() -> bool:
    if not shutil.which("node"):
        return False   # (Playwright found, not loaded: loading it is most of a script's start)
    return subprocess.run(["node", "-e", "require.resolve('playwright')"], capture_output=True, timeout=60).returncode == 0


@pytest.fixture
def chromium():
    """Skips the test without node and Playwright's Chromium: asked as a test runs, once in a worker (not as every worker collects the
    tests: a node each, a third of a second)."""
    if not browser_there():
        pytest.skip("needs node with Playwright's Chromium")


needs_browser = pytest.mark.usefixtures("chromium")


def one_end_to_read(compass: dict):
    """The compass's needle (compassLook's): its head a bold arrow in the needles' colour, its tail a short, thin, dim stub of another
    colour; never two ends alike."""
    head, tail = compass["ends"]
    needle = "rgb({}, {}, {})".format(*(int(look()["needle"][i:i + 2], 16) for i in (1, 3, 5)))
    assert head["colour"] == needle and tail["colour"] != needle and head["opacity"] == 1 and tail["opacity"] <= 0.6, compass
    assert head["area"] > 6 * tail["area"] and head["length"] > 2.5 * tail["length"], compass


@needs_browser
async def test_the_page_fits_its_tiles_to_the_window_at_each_size_with_no_overflow_and_no_console_error(server, station, tmp_path):
    shots = Path(os.getenv("ECOBOT_SCREENSHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    port = free_port()
    url = f"http://127.0.0.1:{port}/dashboard"
    station.reads("wind", "wind_gust", "12")
    station.held({GUST: 37.0, RATE: 4.2, SOLAR: 820.0, UVI: 6.5})        # (the past hour: higher than the readings now, all but 0)
    station.reads("indoor", "temperature", "23.7")                       # an indoor sensor, served with MCP_DASHBOARD_INDOOR off:
    station.reads("indoor", "humidity", "47")                            # its tiles never drawn (the same tiles as with none)
    async with Serving(http_app(server, tokens=(TOKEN,), dashboard_indoor=False), port):
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
        station.reads("wind", "wind_direction", "300")                           # (and the wind backs from the east to WNW)
        station.held({GUST: 60.0, RATE: 4.2, SOLAR: 820.0, UVI: 6.5})            # (and the past hour's highest gust rises)
        await server.publish_changes()
        live.stdin.write(b"go\n")
        await live.stdin.drain()
        out, err = await asyncio.wait_for(live.communicate(), 60)
        assert live.returncode == 0, err.decode()
        after = json.loads(out)
    sheet = gauge_sheet.sheet(Station().current(gauge_sheet.call(), {"fetch": False})).tiles
    n, kinds = len(sheet), [t.kind for t in sheet]
    wind = next(t for t in gauge_sheet.sheet(station.current(gauge_sheet.call(), {"fetch": False})).tiles if t.title == "Wind")
    for name, (width, height) in SIZES.items():
        got = report[name]
        assert got["errors"] == [], (name, got["errors"])
        assert got["tiles"] == n and got["scrollWidth"] <= width, (name, got)
        assert [g["title"] for g in got["gauges"]] == [t.title for t in sheet], (name, got["gauges"])   # (no indoor tile drawn)
        assert got["status"] == "last reading 18:42", (name, got)   # (the US AQI's 56, Moderate: haze behind the tiles)
        assert all(r["right"] <= width + 0.5 and r["left"] >= -0.5 for r in got["rects"]), (name, got)
        assert (shots / f"dashboard-{name}.png").exists() and (shots / f"dashboard-{name}-zoomed.png").exists()
        assert got["zoomed"]["zoomed"], (name, got["zoomed"])
        fills(got["zoomed"], width, height)                                       # zoomed: its title and gauge fill the window, no table
        # no band word ("Good", "Moderate"...) under any dial: its words are its scale's numbers, its unit and its reading alone; every
        # gauge drawn lower than it was, the same for every kind (its top this far further below the title's bottom), all of it above
        # the table's place
        for g in got["gauges"]:
            t = next(t for t in sheet if t.title == g["title"])
            assert g["bands"] == 0, (name, g)
            if g["kind"] == "dial":                                                # (besides its reading)
                assert sorted(g["words"]) == sorted([*(str(int(x)) if float(x).is_integer() else str(x) for x in t.ticks), t.unit]), (name, g)
            assert g["gap"] >= GAPS_BEFORE[g["kind"]] + 12 and g["gap"] >= 15, (name, g)
            assert g["bottom"] <= TABLE_TOP + 0.5, (name, g)
        # the thermometers' glass catches the light: one straight line down the tube (no curve on the bulb, no second line); no dial
        # or beaker has it
        assert sorted(k for k, _ in got["glass"]) == sorted(kinds) and {"thermometer", "dial", "beaker"} <= set(kinds), got["glass"]
        assert all(len(g) == (1 if k == "thermometer" else 0) for k, g in got["glass"]), got["glass"]
        for _, (d,) in (g for g in got["glass"] if g[0] == "thermometer"):
            m = re.fullmatch(r"M([\d.]+),([\d.]+) L([\d.]+),([\d.]+)", d)                  # (a straight line, upright: one segment)
            assert m and m[1] == m[3] and float(m[4]) - float(m[2]) > 100, d
        tile, glass = got["zoomed"]["box"], got["zoomed"]["glass"]                # (the first tile, Temperature) zoomed: the glass with it
        assert len(glass) == 1 and all(g["height"] > 0 and tile["left"] <= g["left"] and g["right"] <= tile["right"] for g in glass)
        assert glass[0]["height"] > tile["width"] * 0.2 and glass[0]["width"] < glass[0]["height"] * 0.06, (glass, tile)
        # the wind's direction, a compass: N, E, S, W and the points between named, its needle's head turned to where the wind comes
        # from (99°, east), its reading "99° E" (the service's compass point); no table, the same size as its neighbours
        c, rects = got["compass"], got["rects"]
        assert (c["label"], c["reading"], c["turn"], c["faded"]) == ("Wind direction 99° E", "99° E", "rotate(99.00 240 188)", False), c
        assert c["points"] == ["N", "NE", "E", "SE", "S", "SW", "W", "NW"] and c["table"] == 0, c
        assert any(near(c["box"], r, 0.5) for r in rects) and len({(round(r["width"]), round(r["height"])) for r in rects}) == 1, c
        ring, head = c["ring"], c["head"]
        assert (head["left"] + head["right"]) / 2 > (ring["left"] + ring["right"]) / 2 + ring["width"] * 0.1 and abs((head["top"] + head["bottom"]) / 2 - (ring["top"] + ring["bottom"]) / 2) < ring["height"] * 0.1, c
        # ... the end to read unmistakable: the head a bold arrow in the needles' colour, the tail a short, thin, dim stub of another
        one_end_to_read(c)
        assert all(c["box"]["left"] <= r["left"] and r["right"] <= c["box"]["right"] and r["bottom"] <= c["box"]["top"] + c["box"]["width"] * TABLE_TOP / look()["cell"][0]
                   for r in c["texts"]), c                                         # (all of it above where a table would start)
        # zoomed: the compass and its reading fill the zoomed place, as any tile's title and gauge
        z = got["compassZoomed"]
        assert z["zoomed"] and z["shown"] and near(z["box"], zoomed_rect(width, height), 1.5), (name, z)
        for r in [z["title"], z["ring"], *z["texts"]]:
            assert z["box"]["left"] - 1 <= r["left"] and r["right"] <= z["box"]["right"] + 1 and z["box"]["top"] - 1 <= r["top"] and r["bottom"] <= z["box"]["bottom"] + 1, (r, z)
        assert z["ring"]["width"] > z["box"]["width"] * 0.45, z
        one_end_to_read(z)                                                        # (zoomed too)
        assert (shots / f"dashboard-{name}-compass.png").exists()
        # the dials with a thin red hand at the past hour's highest (Wind's gust, Rain rate's, Solar radiation's, UV's), over the dial
        # and the needle, under the hub; none on any other tile; the Wind's table: its highest speed, and its highest gust beside it
        hands = got["hands"]
        assert {t for t, h in hands.items() if h} == set(HOURLY), hands
        for title, value in {"Wind": 37.0, "Rain rate": 4.2, "Solar radiation": 820.0, "UV index": 6.5}.items():
            h, tile = hands[title], next(t for t in sheet if t.title == title)
            assert h["turn"] == hand_turn(tile, value) and h["shown"], (name, title, h)
            assert h["colour"] == look()["hour_hand"] and h["width"] <= 1.5 and h["needleWidth"] > 10 * h["width"], (name, title, h)
            assert h["order"][0] < h["order"][1] < h["order"][2], (name, title, h)
        assert hands["Wind"]["heads"] == ["▲ Max", "» Gust"] and wind.columns == ("Max", "Gust"), hands["Wind"]
        assert hands["Wind"]["daily"] == [f"{v:.0f}" for v in wind.rows["Daily"]] and wind.rows["Daily"][1] >= 12, (hands["Wind"], wind)
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
    # the compass's needle swung from 99° to 300° the short way round, through north (never through the south), the reading WNW
    c = after["compass"]
    assert (c["label"], c["reading"], c["turn"]) == ("Wind direction 300° WNW", "300° WNW", "rotate(300.00 240 188)"), c
    turns = c["turns"]
    assert all(t <= 99.01 or t >= 299.99 for t in turns) and any(t < 30 for t in turns) and any(t > 330 for t in turns), turns
    # the Wind's red hand moved smoothly, frame by frame, from the past hour's highest gust before (37) to the new one (60)
    windy = next(t for t in sheet if t.title == "Wind")
    h, frames = after["hands"]["Wind"], after["hands"]["frames"]
    assert h["turn"] == hand_turn(windy, 60.0) and frames[0] == turn_of(windy, 37.0) and frames[-1] == turn_of(windy, 60.0), h
    between = [f for f in frames if turn_of(windy, 37.0) < f < turn_of(windy, 60.0)]
    assert len(set(between)) >= 8 and all(a <= b for a, b in zip(frames, frames[1:])), frames


def turn_of(tile, value: float) -> float:
    """A red hand's turn (degrees, SVG's: clockwise) at this reading on a dial (gauge_look's START and SWEEP; drawn pointing right)."""
    return round(-(START - SWEEP * min(max((value - tile.low) / (tile.high - tile.low), 0), 1)), 2)


def hand_turn(tile, value: float) -> str:
    return f"rotate({turn_of(tile, value):.2f} 240 203)"


@needs_browser
@pytest.mark.parametrize("comes", [True, False])
async def test_a_tile_with_no_reading_is_dimmed_at_its_floor_and_a_reading_coming_brightens_it_and_moves_it_up_in_place(server, station,
                                                                                                                     comes):
    """The air sensor sends no NOx index: its tile is dimmed (L.fade), its needle at the dial's start and its number the floor (1). A
    reading arriving: the same tile brightens and its gauge moves up from the floor, frame by frame, nothing drawn again (no flash). A
    reading going away (the VOC index): it dims and drops to its floor; the wind's direction gone with it, its compass dims, its needle
    back at north and no figure claimed."""
    title, column, value = ("NOx index", "nox_index", 60) if comes else ("VOC index", "voc_index", None)
    port = free_port()
    async with Serving(http_app(server, tokens=(TOKEN,), dashboard_animations=False), port):   # (no weather: as run_page's weather=False)
        page = await asyncio.create_subprocess_exec("node", str(BROWSER), f"http://127.0.0.1:{port}/dashboard", "--arrive", title,
                                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        before = json.loads(await asyncio.wait_for(page.stdout.readline(), 60))
        station.air_reads(column, value)
        if not comes:                                                            # (the wind's direction gone too: the station's)
            station.reads("wind", "wind_direction", "--")
        assert sorted(await server.publish_changes()) == sorted([AIR_URI] if comes else [AIR_URI, WEATHER_URI])   # the page is told
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
        assert not before["faded"] and before["number"] == "112" and "Average" not in before["shown"], before   # (no band word, ever)
        assert after["faded"] and after["opacity"] == pytest.approx(look()["fade"]) and after["number"] == "1", after
        assert "Average" not in after["shown"] and after["label"] == "VOC index: no reading"   # no category without a reading
        assert numbers == sorted(numbers, reverse=True) and numbers[-1] == 1, numbers              # down to the floor
        assert opacities == sorted(opacities, reverse=True), opacities
        # the compass with no reading: dimmed, its needle back at north, no figure or point claimed
        was, now = got["compass"]
        assert not was["faded"] and was["reading"] == "99° E", was
        assert now["faded"] and (now["label"], now["reading"], now["turn"]) == ("Wind direction: no reading", "–", "rotate(0.00 240 188)"), now


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
async def run_page(server, *args, timeout=120, weather=True, parts=None, nice=0) -> dict:
    """The page served, driven by the browser script with these arguments; its report. With `parts`, each part after the arguments,
    side by side (each its own page, in one browser: one script's start, not one a part): their reports in one, the first part's
    where two report the same, their errors together. (`weather` False: served as with MCP_DASHBOARD_ANIMATIONS off, no weather drawn
    behind the tiles: for the pages that drive the tiles and time their moves, not the weather, so the machine's time goes to those
    moves. The weather's own tests drive the tiles over it, zoomed too.) `nice`: the browser run at that lower priority."""
    port = free_port()
    async with Serving(http_app(server, tokens=(TOKEN,), dashboard_animations=weather), port):
        lower = ["nice", "-n", str(nice)] if nice and shutil.which("nice") else []
        page = await asyncio.create_subprocess_exec(*lower, "node", str(BROWSER), f"http://127.0.0.1:{port}/dashboard", *args,
                                                    *(["+".join(parts)] if parts else []), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = await asyncio.wait_for(page.communicate(), timeout)
    assert page.returncode == 0, err.decode()
    reports = json.loads(out)
    if parts is None:
        return reports
    if len(parts) == 1:
        reports = [reports]
    got = {"errors": []}
    for report in reports:
        got["errors"] += report.pop("errors")
        got |= {k: v for k, v in report.items() if k not in got}
    return got


def titles_of(station) -> list[str]:
    return [t.title for t in gauge_sheet.sheet(station.current(gauge_sheet.call(), {"fetch": False})).tiles]


def moved(titles, title, after):
    """The titles with `title` taken out and put back just after `after`."""
    out = [t for t in titles if t != title]
    out.insert(out.index(after) + 1, title)
    return out


@needs_browser
async def test_tiles_are_dragged_to_reorder_hidden_by_the_x_or_the_remove_zone_kept_across_reloads_and_reset(server, station):
    got = await run_page(server, "--arrange", weather=False, parts=("dragged", "hidden", "prefs", "stale"))
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
    # unreadable preferences: the defaults; a tile new to the saved order in its usual place (Rain, after Atmospheric pressure; the
    # wind's direction, after Wind); an
    # absent tile's place and a later stage's keys kept
    assert got["corrupt"] == default
    saved = [t for t in default if t not in ("Rain", "Temperature", "Wind direction")] + ["Temperature"]   # (saved before the compass)
    assert got["newTile"] == moved(moved(saved, "Rain", "Atmospheric pressure"), "Wind direction", "Wind")
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
    # the strip's reading time with a device's reading over an hour old ("stale", its data's devices altered on the way): both fresh,
    # as ever; one stale, its name and own time first, greyed out, then the fresh one's; both stale (or the only device), all greyed out
    stale = got["stale"]
    hm = lambda step, device: stale[step]["sent"][device][11:16]
    assert stale["real"] == {"text": "last reading 18:42", "grey": [], "sent": None}, stale["real"]   # (the fixture's devices: fresh)
    for step in ("fresh", "recovered", "onlyFresh"):
        assert (stale[step]["text"], stale[step]["grey"]) == ("last reading 18:42", []), (step, stale[step])
    for step, old, new, name in (("airStale", "air", "station", "air sensor"), ("stationStale", "station", "air", "weather station")):
        assert stale[step]["text"] == f"{name} {hm(step, old)} · last reading {hm(step, new)}", (step, stale[step])
        assert stale[step]["grey"] == [f"{name} {hm(step, old)}"] and hm(step, old) != hm(step, new), (step, stale[step])
    assert stale["noReading"]["text"] == f"weather station no reading · last reading {hm('noReading', 'air')}", stale["noReading"]
    assert stale["noReading"]["grey"] == ["weather station no reading"], stale["noReading"]
    for step in ("bothStale", "only"):                                        # (only: no air sensor configured, never named)
        assert (stale[step]["text"], stale[step]["grey"]) == ("last reading 18:42", ["last reading 18:42"]), (step, stale[step])
    assert stale["colours"][0] != stale["colours"][1], stale["colours"]       # greyed: not the strip's own colour
    # the station's reading turning an hour old with no new data: the strip greys out by itself, then
    turning = stale["turning"]
    assert turning["before"]["text"] == f"air sensor {turning['before']['sent']['air'][11:16]} · last reading {turning['before']['sent']['station'][11:16]}"
    assert turning["after"]["grey"] == ["last reading 18:42"] and turning["after"]["text"] == "last reading 18:42", turning
    assert 800 < turning["ms"] < 5000, turning


@needs_browser
async def test_on_a_touch_screen_a_long_press_picks_a_tile_up_and_a_tap_or_a_swipe_is_left_alone(server, station):
    got = await run_page(server, "--touch", weather=False, parts=("taps", "edge", "held"))
    default = titles_of(station)
    assert got["errors"] == [] and got["initial"] == default
    # a short tap: a click (it zooms the tile), nothing picked up; a swipe: the page scrolls
    assert got["tap"] == {"buzz": [], "clicks": 1, "scrollY": 0, "zone": False, "armed": [], "close": None, "titles": default, "zoomed": True}
    assert got["swipe"]["scrollY"] > 100 and got["swipe"]["armed"] == [] and got["swipe"]["titles"] == default
    # held (on a page of its own, beside the tap's): a buzz, its grip and ×, the remove zone, no click; moved without scrolling the
    # page; dropped in its new place, still no click
    assert got["held"] == {"buzz": [15], "clicks": 0, "scrollY": 0, "zone": True, "armed": ["Temperature"], "close": "1"}
    assert got["moving"]["scrollY"] == 0 and got["moving"]["during"]["moved"] == ["Humidity", "Atmospheric pressure"]
    reordered = moved(default, "Temperature", "Atmospheric pressure")
    assert got["dropped"]["titles"] == reordered and not got["dropped"]["zone"] and got["dropped"]["clicks"] == 0
    # held and let go in place: the grip and × stay, and a tap on the × hides it
    assert got["armed"]["armed"] == ["Humidity"] and got["armed"]["close"] == "1"
    assert got["closed"] == {"titles": [t for t in reordered if t != "Humidity"], "status": "reset preferences · last reading 18:42"}
    # held at the bottom edge (on a page of its own, beside them): the page scrolls to its end, never past it, and stays there once let go
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
             "phone-all": (390, 844, []), "phone-one": (390, 844, but("Humidity")), "phone-eight": (390, 844, every[:-8])}
    got = await run_page(server, "--fill", json.dumps(cases), weather=False)
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
GAPS_BEFORE = {"dial": 2, "thermometer": 21.7, "beaker": 16.7, "compass": -5}   # (the gap under the title, each kind, before the gauges
                                                                                  # were moved down: the tile's own px)
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
    for r in [zl["title"], *zl["texts"]]:                                                     # (the reading, the scale's words)
        assert box["left"] - 1 <= r["left"] and r["right"] <= box["right"] + 1 and box["top"] - 1 <= r["top"] and r["bottom"] <= box["bottom"] + 1, (r, zl)


@needs_browser
async def test_a_click_on_a_gauge_zooms_its_tile_to_fill_the_window_the_tile_beside_it_live_and_the_keys_page(server, station):
    port = free_port()
    async with Serving(http_app(server, tokens=(TOKEN,), dashboard_animations=False), port):   # (no weather: as run_page's weather=False)
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


ZOOM_PARTS = {"pager": ("pager", "wheel"), "last": ("last", "text", "single", "lone"), "out": ("out", "keys")}   # (the browser script's, side by side)


async def zoomed(server, station, part: str) -> tuple[dict, list[str], dict]:
    """A part of the zoom at 1024x768 (its own pages, side by side in one browser): its report, no error; the
    tiles' titles; a tile's rect zoomed."""
    got = await run_page(server, "--zoom", weather=False, parts=ZOOM_PARTS[part])
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
    # a trackpad's two-finger swipe (wheel deltaX), from the first (on a page of its own, beside that one): the next, or back if short;
    # never reaching the browser
    wheel = got["wheel"]
    assert wheel["from"] == {"zoomed": True, "shown": default[0], "track": 0}, wheel
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
    # a drag on the text selects it: no paging, still zoomed (on a page of its own, beside that one, the last reached the same way)
    text = got["text"]
    assert got["textOn"] == default[-1], got["textOn"]
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
    # a gallery of one: the keys nudge it and back, a drag either way gives a little and springs back, as does a trackpad's swipe (on a
    # page of its own, beside that one, zoomed the same way)
    lone = got["lone"]
    for name, key in one["keys"].items():
        assert key["zoomed"] and key["shown"] == "Humidity" and key["track"] == 0 and key["extreme"] >= 20, (name, key)
    for name, drag in one["dragged"].items():
        assert drag["zoomed"] and drag["shown"] == "Humidity" and drag["track"] == 0 and 20 < abs(drag["during"]) < 400 * 0.6, (name, drag)
    assert lone["wheeled"] | {"prevented": None} == {"zoomed": True, "shown": "Humidity", "track": 0, "prevented": None}, lone["wheeled"]
    assert lone["wheeled"]["prevented"] and all(lone["wheeled"]["prevented"]), lone["wheeled"]
    # a click goes back, the tile in its place; Esc too
    box = lambda r: r | {"left": r["x"], "top": r["y"]}
    rect = lone["back"].pop("rect")
    assert lone["back"] == {"zoomed": False, "shown": "Humidity", "track": 0} and near(box(rect), box(lone["gridRect"])), (lone["back"], rect)
    assert near(box(lone["gridRect"]), box(one["gridRect"])), (lone["gridRect"], one["gridRect"])
    assert lone["esc"]["zoomed"] is False, lone["esc"]


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
    got = await run_page(server, "--zoom-touch", weather=False, parts=("back", "swipes", "later", "return"))
    default = titles_of(station)
    assert got["errors"] == [] and got["initial"] == default, got["errors"]
    t = default.index(got["tapped"])
    assert got["swipesTapped"] == got["laterTapped"] == got["returnTapped"] == got["tapped"], got   # (the swipes' pages, beside it: the same tile tapped)
    assert got["scrolled"] > 100
    assert got["zoomed"]["zoomed"] and got["zoomed"]["shown"] == got["tapped"]
    assert not got["backStraight"]["zoomed"] and abs(got["backStraight"]["scrollY"] - got["scrolled"]) <= 2, got["backStraight"]
    assert near(got["zoomed"]["rect"] | {"left": got["zoomed"]["rect"]["x"], "top": got["zoomed"]["rect"]["y"]}, zoomed_rect(390, 844))
    fills(got["zoomed"]["look"], 390, 844)
    assert [got[k]["shown"] for k in ("next", "previous", "flick", "fromTitle")] == [default[t + 1], default[t], default[t + 1], default[t + 2]], got
    # (on pages of their own, beside them, the same tile tapped and two on: there,) a vertical swipe, a long press: nothing; three
    # swipes back, and a tap back to the dashboard
    assert got["vertical"]["shown"] == default[t + 2] and got["vertical"]["zoomed"]
    assert got["returnFrom"] == got["vertical"], (got["returnFrom"], got["vertical"])
    assert got["held"] == {"zoomed": True, "shown": default[t + 2], "track": got["vertical"]["track"], "buzz": [], "zone": False}
    assert got["backAt"]["shown"] == default[t - 1]
    assert not got["out"]["zoomed"] and got["out"]["titles"] == default and got["out"]["lastShown"], got["out"]


# ---------- the weather behind the tiles ----------
STREAK = [[142, 157, 168], [76, 88, 97], [239, 250, 255]]   # a trail's thin streak on clear glass: its film, grime, glint
DROPLET = [[166, 215, 242], [239, 250, 255]]                   # a tiny droplet left on a trail, its spark
EFFECTS = {"rain": "Rain", "haze": "US AQI", "uv": "UV index", "humidity": "Humidity", "wind": "Wind", "heat": "Temperature",
           "ice": "Temperature"}


# The weather's parts whose pages are driven (tests/web_dashboard_browser.js, weatherPage): what they see doesn't depend on the time
# their frames take, so they run a little behind the rest (nice 5): the pages whose own timing is tested (a frame rate, the zoom's
# moves) get a CPU first when the machine is busy; the rest of the suite (conftest: nice 10) after them
DRIVEN = {"all", "flood", "opened", "gust"}


async def weather(server, part: str) -> dict:
    """One part of the weather behind the tiles, its own page in its own browser (the parts run side by side): its report, no error."""
    got = await run_page(server, "--weather", part, nice=5 if part in DRIVEN else 0)
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
        assert mark["at"]["link"] == "stop animation" and mark["at"]["running"], (name, mark)
    assert got["hazeBands"] == [2, 3, 4, 5, 5]                                   # thicker with each band: USG to Hazardous
    assert got["rainStrength"][0] < 0.4 and got["rainStrength"][1:] == [1, 1]    # light at 0.5 mm/h, heavy from 8
    assert got["humidityStrength"] == pytest.approx([0.1, 0.5, 1])              # heavier from 90 % to 100 %
    assert got["iceDepth"] == [0.5, 5]                                           # further in the colder


@needs_browser
async def test_several_effects_share_one_canvas_behind_the_see_through_tiles_zoomed_too(server, station):
    got = await weather(server, "several")
    # several at once: one canvas, one loop, at most 30 frames drawn a second
    several = got["several"]
    assert several["effects"] == ["haze", "uv", "rain", "wind", "humidity", "ice"] and several["canvases"] == 1, several
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
async def test_stop_animation_stops_and_clears_for_this_page_view_only_nothing_saved_a_reload_or_a_test_link_runs_them_again(server, station):
    got = await weather(server, "stop")
    # "stop animation" (a test link's wind on too), its tooltip naming the setting for good
    assert got["simulating"]["effects"] == ["uv", "wind"] and got["simulating"]["link"] == "stop animation", got["simulating"]
    assert got["simulating"]["title"] == "stop until reload; set MCP_DASHBOARD_ANIMATIONS=off to turn off for good", got["simulating"]
    # ... nothing drawn, no loop, the test link's effect ended (?fx= gone), the frame stays; the link gone (no "start animation");
    # nothing saved
    assert got["stopped"] | {"levels": None} == {"effects": ["uv"], "levels": None, "running": False, "framed": ["UV index"],
                                                 "link": None, "frames": 0, "ink": 0, "stored": None, "url": "",
                                                 "pressed": 0}, got["stopped"]
    # the readings calling for more, and "reset preferences": still stopped, nothing drawn, no link
    reset = got["reset"]
    assert reset["link"] is None and not reset["running"] and reset["frames"] == 0 and reset["ink"] == 0, reset
    assert set(reset["framed"]) == {"UV index", "Wind"}, reset
    # a reload: running as normal (an earlier version's saved {animations: false} ignored and dropped, the arrangement kept)
    reloaded = got["reloaded"]
    assert reloaded["link"] == "stop animation" and reloaded["running"] and reloaded["frames"] > 10, reloaded
    assert reloaded["stored"] == {"order": [], "hidden": ["Rain"]}, reloaded
    # stopped again, then a test link (heat): on again, its effect and the readings' with it, the link back
    resumed = got["resumed"]
    assert resumed["link"] == "stop animation" and resumed["running"] and resumed["frames"] > 10, resumed
    assert set(resumed["effects"]) == {"uv", "wind", "heat"} and resumed["url"] == "?fx=heat", resumed
    # from the keyboard: Enter on it stops them, the link gone (the focus not left on it)
    assert got["keyboard"]["link"] is None and not got["keyboard"]["running"] and got["keyboard"]["focused"] == "BODY", got["keyboard"]


@needs_browser
async def test_the_temporary_test_links_turn_each_effect_on_and_stop_animation_ends_them_and_with_animations_off_nothing_is_drawn(server, station):
    got = await weather(server, "links")
    # the temporary test links: each its effect and frame (the gauges keep their readings), the URL following; haze steps through its
    # bands, rain light then heavy; ice melts back; "stop animation" ends them all (no "clear"); no "all" link, but ?fx=all
    links = got["links"]
    assert links["buttons"] == ["rain", "haze", "uv", "humidity", "wind", "heat", "ice", "gust"], links["buttons"]
    on = []
    for name in ("uv", "humidity", "wind", "heat", "ice"):
        on.append(name)
        assert set(links[name]["effects"]) == set(on) and EFFECTS[name] in links[name]["framed"], (name, links[name])
        assert links[name]["url"] == "?fx=" + ",".join(on)
    assert links["label"] == "UV index 0.0"                                        # (the stand-in's reading, not the simulated one)
    # "stop animation" at the bottom left, just right of the row's last link, beside the strip (at the window's bottom), clear of it;
    # the row with its "test:", all of it in sight
    corner = links["corner"]
    assert corner["link"] == "stop animation" and corner["apart"] and corner["onScreen"], corner
    just_right_of_the_row(corner)
    assert corner["tag"] and corner["fits"], corner
    assert corner["row"]["left"] == 0 and corner["motion"]["bottom"] == corner["window"][1] == corner["strip"]["bottom"], corner
    assert links["haze"] == [1, 2, 3, 4, 5, 0]
    assert links["rain"] == [[0.35, 0], [1, 0], [1, 2], [0, 0]]   # (light, heavy, heavy with a flash flood (the test's own), off)
    melting = links["melting"]
    assert melting["grown"] > 2 and 0 < melting["reach"] < melting["grown"] and "ice" not in melting["effects"] and melting["running"], melting
    # "stop animation" with only test links on: every one off, ?fx= gone, the animations stopped (nothing saved), the link gone
    assert links["cleared"] | {"levels": None, "running": None} == {"effects": [], "levels": None, "running": None, "framed": [],
                                                                     "link": None, "url": "", "pressed": 0, "stored": None}
    assert links["all"]["effects"] == EVERY and links["all"]["url"] == "?fx=rain2,haze3,uv,humidity2,wind,heat,ice"
    # humidity light (91 %) first, then heavy (100 %), then off
    assert links["humiditySteps"] == [{"level": 1, "url": "?fx=uv,humidity2,wind,heat", "label": "humidity: heavy"},
                                      {"level": 0, "url": "?fx=uv,wind,heat", "label": "humidity"}], links["humiditySteps"]
    assert links["role"] == "group" and links["pressed"] == {"rain": "false", "haze": "false", "uv": "true", "humidity": "true",
                                                             "wind": "true", "heat": "true", "ice": "false"}, links["pressed"]
    # on a phone (360x740, 390x844), every effect on (its links' longest): no "test:", each link ("gust" too) and "stop animation" in sight,
    # unscrolled, with room for one more short link (some 30 px); a link's step a raised figure, its full words its label
    for phone in (got["phone360"], got["phone390"]):
        assert not phone["tag"] and phone["fits"] and phone["spare"] >= 30 and phone["apart"] and phone["onScreen"], phone
        just_right_of_the_row(phone)
    for phone in (got["phone360"], got["phone390"]):
        assert phone["errors"] == [] and phone["link"] == "stop animation", phone
        assert phone["buttons"] == ["rain²", "haze³", "uv", "humidity²", "wind", "heat", "ice", "gust"], phone["buttons"]
        assert phone["labels"] == ["rain: heavy", "haze 3", "uv", "humidity: heavy", "wind", "heat", "ice", "gust"], phone["labels"]
        assert phone["motion"]["bottom"] == pytest.approx(phone["strip"]["top"], abs=0.5), phone              # (above the strip)
        assert phone["flood"] == ["rain³", "rain: flood"], phone["flood"]                                   # (rain's third step)
    # MCP_DASHBOARD_ANIMATIONS off (the page as served then, data-animations="off"), every effect asked for (?fx=all) and called for by
    # the readings (a gust over 99 km/h and ?fx=gust too), the animations not stopped in the browser: no loop, nothing drawn, nothing
    # blown across, no link, no test links; the tiles and the strip as ever
    off = got["off"]
    assert off["off"] == {"effects": off["off"]["effects"], "levels": off["off"]["levels"], "running": False, "framed": off["off"]["framed"],
                          "link": None, "frames": 0, "ink": 0, "gust": 0, "sky": "none", "corner": "none", "row": False}, off["off"]
    assert off["off"]["effects"] == EVERY, off["off"]                                    # (asked for all the same: just never drawn)
    assert off["tiles"] == titles_of(station) and off["strip"] == "last reading 18:42", off
    # a tile hidden: "reset preferences" in the strip as ever; still nothing drawn
    assert off["hidden"] == {"strip": "reset preferences · last reading 18:42", "frames": 0, "ink": 0, "running": False}, off["hidden"]
    # MCP_DASHBOARD_ANIMATIONS low (low motion, for e-ink: the page as served then, data-animations="low"): nothing moves. Opened
    # with nothing called for: nothing drawn, no frame asked for (the gauges at their readings at once, no rise); no transition
    low = got["low"]
    assert low["opened"] | {"levels": None} == {"effects": [], "levels": None, "running": False, "framed": [], "link": None, "frames": 0,
                                                "drawn": 0, "stills": 0, "gust": 0, "ink": 0}, low["opened"]
    assert low["transitions"] == 0, low["transitions"]
    # every effect called for (but the heat: below 0 °C; a gust over 99 km/h too): one faint still frame, no loop, no frame asked for (the gauges and the UV's red
    # hand put at their new readings at once), nothing blown across, no "stop animation" (nothing to stop)
    assert low["called"] | {"levels": None, "ink": None} == {
        "effects": [e for e in EVERY if e != "heat"], "levels": None, "running": False, "framed": low["called"]["framed"], "link": None, "frames": 0, "drawn": 0,
        "stills": 1, "gust": 0, "ink": None, "hand": "rotate(-27.50 240 203)"}, low["called"]
    assert low["called"]["ink"] > 0, low["called"]
    # the data asked for again and again, the same conditions: fetched once more, no new frame; new conditions: one new still frame
    assert low["same"] == {k: v for k, v in low["called"].items() if k != "hand"}, low["same"]
    assert low["changed"]["stills"] == 2 and low["changed"]["effects"] == [e for e in EVERY if e not in ("heat", "uv")] and low["changed"]["frames"] == 0, low["changed"]
    # the data applied at most once a minute (here every 1.2 s): the page's opening fetch and three more, each 1.2 s or more after the last
    at = low["dataAt"]
    assert len(at) == 4 and all(b - a >= 1200 - 20 for a, b in zip(at, at[1:])), at
    # zoomed, paged and back, a tile hidden: each in one step, no animation, no tile moving; still no frame asked for
    assert low["zoomed"] == {"zoomed": True, "animations": 0} and low["paged"] == {"animations": 0, "transition": "0s"}, low
    assert low["back"] == {"zoomed": False, "animations": 0}, low["back"]
    assert low["hidden"] == {"animations": 0, "moved": 0, "tiles": len(titles_of(station)) - 1} and low["frames"] == 0, low


EVERY = ["haze", "heat", "uv", "rain", "wind", "humidity", "ice"]
# humidity's beads' colours, [r, g, b, alpha] (alpha 0-255): the glass's pale body (#A6D7F2, 34) and crescent (105), the white glint
# (#EFFAFF, 240), the deep blue rim (#2C5573, 110); the faintest first
BEAD_COLOURS = [[166, 215, 242, 34], [166, 215, 242, 105], [44, 85, 115, 110], [239, 250, 255, 240]]


def just_right_of_the_row(corner):
    """ "stop animation" right after the test row, on its line, a few pixels right of its last link (the row's end, in sight)."""
    motion, row, last = corner["motion"], corner["row"], corner["last"]
    assert motion["left"] == pytest.approx(row["right"], abs=0.5) and -1 <= motion["left"] - last["right"] <= 10, corner
    assert row["left"] <= last["left"] and last["right"] <= row["right"] + 1, corner   # (-1, +1: the row scrolled to a whole px)
    assert motion["bottom"] == pytest.approx(row["bottom"], abs=0.5) and motion["top"] == pytest.approx(row["top"], abs=0.5), corner


@needs_browser
async def test_every_effect_at_once_is_cheap_no_brighter_than_the_strongest_alone_and_within_the_particle_budget(server, station):
    got = await weather(server, "all")
    # (the cost: the script's own time a frame, the main thread's CPU time, however busy the machine: about 1.5 ms)
    assert got["cost"] < 8 and got["flying"] == 1, (got["cost"], got["flying"])   # (something blown across too: ?fx=all,gust)
    # ... the ice grown all the way in too, its frost across much of the glass: as cheap
    assert got["costFrosted"]["cost"] < 8 and got["costFrosted"]["crystals"] > 0.15, got["costFrosted"]
    # ... after minutes of the weather's time (over 200 s, the humidity's trails and beads built up: tens of thousands of trail
    # pixels): no dearer than at the start (its cost not growing with them: about 1.2 ms against 2; it grew to over twice that at
    # full screen); and with its time 20 times as fast, the trails' sweep capped (a slice of them a frame, never all), still cheap
    late, fast = got["costFrosted"], got["costFast"]
    assert late["clock"] > 200 and late["trails"] > 15000 and late["beads"] > 100 and late["cost"] < 1.5 * got["cost"], (got["cost"], late)
    assert fast["trails"] > 15000 and 0 < fast["swept"] <= fast["most"] == 2500 and fast["cost"] < 8, fast
    # several at once no brighter than one strong one: every effect on (the ice grown all the way in), against Hazardous haze alone
    assert 0 < got["light"]["all"] <= got["light"]["haze5"], got["light"]
    # the haze (the particulates), each band alone: soft clouds over the whole window, more of it under them with each band (at
    # Moderate a few scattered ones, under a quarter of it; at Hazardous most of it), and denser
    levels = got["haze"]["levels"]
    assert [lv["level"] for lv in levels] == [1, 2, 3, 4, 5], levels
    covers = [round(lv["cover"], 3) for lv in levels]
    assert 0.03 < covers[0] < 0.25 and covers[-1] > 0.6 and all(a * 1.15 < b for a, b in zip(covers, covers[1:])), covers
    assert all(a["mean"] * 1.2 < b["mean"] for a, b in zip(levels, levels[1:])), [lv["mean"] for lv in levels]
    # ... dithered irregularly: no grid in its pixels (its strongest fine frequency's power, over the median's: under 400; the even
    # veil's ordered dither, 75000 and more)
    assert all(lv["grid"] < 400 for lv in levels), [round(lv["grid"]) for lv in levels]
    # ... each cloud lit from above: its upper half lighter than its lower (by 15 % or more: its lit top over its darker core and
    # underside)
    looks = [look for lv in levels for look in lv["looks"]]
    assert all(len(lv["looks"]) >= 2 for lv in levels) and all(upper > 1.15 * lower for upper, lower in looks), looks
    # ... drifting right to left, the wind's way: every cloud and mote leftward (about 3 px/s), its drift too; faster in the wind
    calm, windy = got["haze"]["calm"], got["haze"]["windy"]
    assert calm["n"] >= 20 and calm["left"] == calm["n"] and 1.5 < calm["speed"] < 6 and calm["slide"] > 2, calm
    assert windy["n"] >= 20 and windy["left"] == windy["n"] and windy["speed"] > 2 * calm["speed"] and windy["slide"] > 2 * calm["slide"], windy
    for clouds in (calm["clouds"], windy["clouds"]):
        assert clouds["n"] >= 20 and clouds["left"] == clouds["n"] and clouds["speed"] > 1, clouds
    assert windy["clouds"]["speed"] > 2 * calm["clouds"]["speed"], (calm, windy)
    together = got["particles"]["all"]
    assert together["uv"] == 0 and together["wind"] > 0 and together["rain"] > 0, together
    # ... the shared budget kept by every effect but humidity's beads (their own limits bound them: heavy rain keeps every drop)
    assert sum(v for k, v in together.items() if k not in ("budget", "humidity")) <= together["budget"] + 3, together   # (each rounded)


@needs_browser
async def test_effects_asked_for_as_the_page_opens_alone_on_a_phone_and_with_reduced_motion(server, station):
    got = await weather(server, "opened")
    # ?fx= when the page opens
    assert got["param"]["levels"] == {"gust": 0, "haze": 3, "heat": 0, "uv": 0, "rain": 0, "wind": 1, "humidity": 0, "flood": 0, "ice": 5}
    assert sorted(got["param"]["framed"]) == ["Temperature", "US AQI", "Wind"] and got["param"]["errors"] == []
    # (the effects' earlier names, in links saved before, still work: as wind, uv, humidity, and smog4 as haze4)
    assert got["paramOld"]["effects"] == ["haze", "uv", "wind", "humidity"] and got["paramOld"]["errors"] == [], got["paramOld"]
    assert got["paramOld"]["levels"]["haze"] == 4, got["paramOld"]
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
    # ... their glow dithered irregularly: no grid in its pixels (as gridPeak sees it: under 100; ordered, about 660)
    assert uv["grid"] < 100, uv["grid"]
    # heavy rain in the wind: its drops blown leftward, faster than with no wind; bunched by the gust bands into curtains and gaps,
    # their density across uneven, and changing as the bands pass, far more than the rain alone (scattered evenly: about 1)
    alone, windy = got["rain"]["alone"], got["rain"]["windy"]
    assert alone["errors"] == windy["errors"] == [] and alone["looks"] >= 20 and windy["looks"] >= 20, got["rain"]
    assert windy["vx"] < alone["vx"] < 0 and alone["drops"] > 30 and windy["drops"] > 30, got["rain"]
    assert alone["across"] < 1.4 and alone["overTime"] < 1.4, got["rain"]
    assert windy["across"] > max(2, 1.6 * alone["across"]) and windy["overTime"] > max(2, 1.6 * alone["overTime"]), got["rain"]
    # ... on a phone too (its art narrow, the bands shorter so a curtain and a gap still fit across: about 1.6-3; alone there, about 0.8)
    rain_phone = got["rain"]["phone"]
    assert rain_phone["errors"] == [] and rain_phone["looks"] >= 20 and rain_phone["drops"] > 5, rain_phone
    assert rain_phone["vx"] < alone["vx"] and rain_phone["across"] > 1.3 and rain_phone["overTime"] > 1.3, rain_phone
    # heavy drops knock the leaves they hit (a push along their slant, a burst of spin): many hits, each a bash; light rain's (before
    # it turned heavy) barely nudge them; the rain alone (no leaves) and the wind alone (no drops), none
    assert alone["leaves"]["hits"] == 0, alone["leaves"]
    for heavy, many in ((windy, 25), (rain_phone, 5)):   # (in 3 s or so: about 50-65; on a phone, its art small, about 15-30)
        assert heavy["leaves"]["hits"] >= many and heavy["leaves"]["push"] > 25, heavy["leaves"]
        assert heavy["leaves"]["kick"] == pytest.approx(heavy["leaves"]["push"], rel=0.01), heavy["leaves"]   # (each leaf's velocity: so changed)
    assert windy["leaves"]["whirl"] > 2, windy["leaves"]
    assert windy["light"]["hits"] >= 3 and 0 < windy["light"]["push"] < 10, windy["light"]
    assert windy["light"]["kick"] == pytest.approx(windy["light"]["push"], rel=0.01), windy["light"]
    assert windy["leaves"]["kick"] > 2.5 * windy["light"]["kick"], (windy["leaves"], windy["light"])   # (heavy rain's knocks: far harder)
    # humidity (heavy: 100 %): condensation beading all over the glass in its fog (3.6 minutes of it, on a small page, its time run on
    # fast), the beads condensing at every height (more toward the top), tiny, and only where the fog shows; never a line of water
    # along the top edge. One set of rules for every bead (BEAD_RULES below): its speed rising gradually with its size (on fog, by
    # size: each band faster than the one before); down only, never back up, well down; most wandering a little either side of where
    # each began (never further than its bound), the lot not drifting either way; reaching the window's bottom edge and running off it
    # (none left there); its trail behind it. On fog it grows (gathering the fog), on a trail it keeps its size: neither ever shrinks a
    # bead (only dry glass does: heavy rain's beads, below). Beads that touch merge, their water adding up exactly, however big: no
    # largest bead. Glass: mostly see-through (a faint body; they were opaque, about 217 of 255 as drawn), a crisp glint. The same
    # with the wind on: water on the glass isn't blown
    for name, beads in got["humidity"].items():
        assert beads["errors"] == [] and beads["runners"] >= 10 and beads["up"] == 0 and beads["down"] > 50, (name, beads)
        assert 2 <= beads["wander"] <= beads["meander"] and beads["wandered"] >= beads["runners"] / 2, (name, beads)
        assert abs(beads["offset"]) < 3, (name, beads)   # (about 1 or less; blown, many px)
        assert beads["lowest"] >= 1 and beads["ranOff"] >= 5 and beads["under"] == 0, (name, beads)   # (run off the bottom: gone)
        born = beads["born"]   # (each condensed: its radius, the fog shown where it showed, its height, of the window's)
        thirds = [sum(lo <= b["y"] < lo + 1 / 3 for b in born) for lo in (0, 1 / 3, 2 / 3)]
        assert len(born) >= 20 and min(thirds) >= 2 and sum(b["y"] > 0.5 for b in born) >= 0.15 * len(born), (name, thirds, born)
        assert thirds[0] >= 0.3 * len(born), (name, thirds)   # (more toward the top)
        assert all(b["r"] <= 0.75 and b["fog"] >= 0.06 for b in born), (name, born)   # (tiny, and in the fog only)
        assert beads["behind"] > 0.95 and beads["trails"] > 200, (name, beads)
        glass = beads["glass"]
        assert glass["fog"]["gained"] > 0 and glass["fog"]["lost"] == 0, (name, glass)            # (on fog: growing, never shrinking)
        assert glass["trail"]["moves"] > 100 and glass["trail"]["gained"] == glass["trail"]["lost"] == 0, (name, glass)   # (a trail: kept)
        assert glass["dry"]["lost"] == 0, (name, glass)
        # (the same size, r 1.8-3.2: faster down a trail than on fog (water on water), each bead's px against what it'd have run on fog
        # at its size then, so the sizes that happened to run on each don't count; any size, the same: about 1 on fog, 1.6 on a trail)
        speed = {g: o["px"] / o["band"] for g, o in glass.items() if o["band"] > 0}
        assert speed["trail"] > speed["fog"], (name, speed)
        assert 0.75 < glass["fog"]["all"] / glass["fog"]["norm"] < 1.3 < glass["trail"]["all"] / glass["trail"]["norm"] < 2, (name, glass)
        on_fog = [px / s for px, s in beads["sizes"] if s > 0]   # (by size: under 1, 1-1.6, 1.6-2.4, 2.4-3.2, 3.2 and over)
        assert beads["sizes"][0] == [0, 0], (name, beads["sizes"])   # (under r 1: none running, they run from r 1.1)
        assert len(on_fog) == 4 and on_fog == sorted(on_fog) and on_fog[0] < 0.5 and on_fog[-1] > 5, (name, beads["sizes"])   # (1-1.6: creeping)
        # (merges: each bead's water the two's together, exactly, however big)
        assert beads["merges"] >= 3 and beads["merged"], (name, beads)
        for m in beads["merged"]:
            assert m["into"] == pytest.approx(sum(m["vol"]), rel=1e-12) and m["intoR"] ** 3 == pytest.approx(m["into"]), (name, m)
            assert m["intoR"] > max(m["r"]), (name, m)
        assert beads["largest"] >= max(m["intoR"] for m in beads["merged"]) - 1e-9, (name, beads["largest"])
        # (looked at as drawn: the still beads whole, every frame; their own alpha, the fog under them taken out)
        assert beads["look"]["beads"] >= 20 and beads["look"]["mean"] < 110 and beads["look"]["most"] > 170, (name, beads["look"])
        # (the glass well beaded: more beads on it still at a time than running, many: about 60 at once)
        assert beads["showing"]["still"] > beads["showing"]["running"] > 0, (name, beads["showing"])
        assert beads["showing"]["still"] > 36, (name, beads["showing"])
        # (lasting: many on the glass 25 s or more by the end)
        assert sum(age > 25 for age in beads["aged"]) >= 10, (name, sorted(beads["aged"]))
        # (nothing left showing on the fog's canvas where no trail is any more; no bead vanishing (each merged into another, run off
        # the bottom, shrunk to nothing or dried away)
        assert beads["stray"] == 0 and beads["popped"] == 0 and beads["landed"] == 0, (name, beads)
        # (tiny beads never moving but to snap together, many snapping; condensing on and on, more and more of them, up to the most
        # there may be (the cap; it shrinks as small beads take their room), never more; beads from r 1.1, the run size, to 1.6 running)
        assert beads["crept"] == 0 and beads["snaps"] >= 20, (name, beads["crept"], beads["snaps"])
        # (heavy humidity condensing them fast, its glass beaded up a while first (150 frames): full from the watch's start; light's
        # watched from the first, below: more and more of them)
        assert beads["tinies"][1]["n"] >= beads["tinies"][1]["cap"] - 3, (name, beads["tinies"])
        assert any(t["n"] >= t["cap"] - 3 for t in beads["tinies"]), (name, beads["tinies"])
        assert all(t["n"] <= 1.05 * t["cap"] + 4 for t in beads["tinies"]), (name, beads["tinies"])
        assert beads["sizes"][1][0] > 0, (name, beads["sizes"])
    # ... nothing at all drawn in the canvas's top two rows (humidity alone: no water along the top edge)
    assert got["humidity"]["alone"]["top"] == 0, got["humidity"]["alone"]
    # ... two tiny beads put close together (not touching, within the snap): snapped into one small bead (not tiny) in a few frames,
    # their water adding up (then growing on the fog as ever), where the lower one was (none pulled up the glass); two further apart, beside them: still there, unmoved
    snapped = got["humidity"]["alone"]["snapped"]
    assert snapped["spot"] and len(snapped["at"]) == 4 and all(b["r"] <= snapped["tinyR"] for b in snapped["at"]), snapped
    pair, apart = snapped["placed"][:2], snapped["placed"][2:]
    assert pair[0] is None and pair[1] and not pair[1]["tiny"] and pair[1]["r"] > snapped["tinyR"], snapped
    together = snapped["at"][0]["vol"] + snapped["at"][1]["vol"]   # (and since, as it sat on the fog, growing a very little)
    assert together <= pair[1]["vol"] < 1.05 * together, snapped
    assert snapped["snap"] == 1, snapped   # (a pixel's gap between their edges or less: the pair 1.6 across snapped, the two 2.5 across not)
    assert pair[1]["y"] >= snapped["at"][1]["y"] and len(snapped["near"]) == 1, snapped
    assert [(b["x"], b["y"], b["tiny"]) for b in apart] == [(b["x"], b["y"], True) for b in snapped["at"][2:]], snapped
    # ... the fog: forming first (the first bead only once it has, there and on the glass at large), its trails wiped clear through it
    # about as wide as the bead was there (its edges ragged: a px in or out; a px more where another's beside it), clearer than the
    # fog beside them (where there is fog); the beads' colours as ever (the glass: the deep blue rim, a faint pale body, a crescent, a
    # white glint)
    for name, beads in got["humidity"].items():
        first = beads["first"]
        assert first["after"] >= 2 and first["cover"] >= 0.3 and first["fog"] > 0.2 and first["formed"]["edge"] > 0.2, (name, first)
        wiped = beads["wiped"]
        # (wiped clear, or near its ragged edge all but: the fog already creeping back there a little)
        assert len(wiped) >= 8 and all(t["shown"] <= 0.25 * t["fog"] and t["wet"] > 0.85 for t in wiped), (name, wiped)
        assert sum(t["shown"] == 0 for t in wiped) >= 0.3 * len(wiped), (name, wiped)
        assert all(-1 <= t["across"] - t["w"] <= 3 for t in wiped), (name, [t for t in wiped if not -1 <= t["across"] - t["w"] <= 3])
        fogged = [t for t in wiped if t["fog"] > 0.05 and t["beside"] > 0.05]
        assert len(fogged) >= 5 and all(t["beside"] > t["shown"] for t in fogged), (name, wiped)
        assert sum(t["end"]["wet"] < t["wet"] for t in wiped) >= 0.75 * len(wiped), (name, wiped)   # (fogging over again, slowly)
        # (natural trails through the fog, as they look while fresh: about the bead's width all along (its width as it grew), never
        # a gap; their edges ragged, each wandering a px in or out down the trail, not straight)
        fog_trails = [t for t in beads["looks"] if t["kind"] == "fog"]
        assert len(fog_trails) >= 6, (name, beads["looks"])
        for t in fog_trails:
            assert min(t["widths"]) >= 1 and abs(sum(t["widths"]) / len(t["widths"]) - t["w"]) <= 1.5, (name, t)
            moves = [sum(a != b for a, b in zip(edge, edge[1:])) for edge in (t["lefts"], t["rights"])]
            assert len(set(t["lefts"] + t["rights"])) >= 2 and sum(moves) >= 0.1 * len(t["lefts"]), (name, t)
            assert len(set(t["widths"])) >= 2, (name, t)
            # (clean inside: no droplet, glint or other trail's look in it, only the fog's faintest two shades as residue; and no
            # dots: whatever shows inside it, not touching the fog beside it, is a streak of residue down it (3 rows or more, 2 px
            # wide at most, taller than wide), never a lone pixel or a small blob)
            inside = t["inside"]
            assert inside["odd"] == [] and all(set(d["shows"]) <= {1, 2} for d in inside["patches"]), (name, t["id"], inside)
            dots = [d for d in inside["patches"] if d["tall"] < 3 or d["wide"] > 2 or d["tall"] <= d["wide"]]
            assert dots == [], (name, t["id"], dots)
        # (a little streaky residue in them all told: some, under a tenth of the wiped px)
        residue, px = sum(t["inside"]["residue"] for t in fog_trails), sum(t["inside"]["px"] for t in fog_trails)
        assert 0 < residue < 0.1 * px, (name, residue, px)
        colours = sorted(beads["colours"], key=lambda c: c[3])
        assert len(colours) == len(BEAD_COLOURS) and all(c == pytest.approx(want, abs=3) for c, want in zip(colours, BEAD_COLOURS)), (name, colours)
    # ... the trails through the fog, their time run on fast: each fogging over again all the way in 3 to 10 minutes (each trail its
    # own: some sooner, some later), wiped clear only a short while first, the fog coming back over the rest; the fog then as if never
    # wiped
    refog = got["humidity"]["windy"]["refog"]
    lasted = [t["lasted"] for t in refog]
    assert len(refog) >= 10 and all(170 <= t <= 610 for t in lasted) and max(lasted) - min(lasted) > 150, refog
    assert all(t["crispTill"] is not None and t["crispTill"] < 0.25 * t["lasted"] for t in refog), refog
    assert all(t["fog"][0] == t["fog"][1] > 0 for t in refog), refog
    # (as it was picked, in the middle of the trail's row, fresh: wiped clear mostly; here and there a streak of the fog left
    # unwiped (its two faintest shades), no more: no droplet left in it)
    assert sum(t["look"] == [0, 0, 0, 0] for t in refog) >= 0.5 * len(refog), refog
    assert all(t["look"] in ([0, 0, 0, 0], [50, 60, 67, 56], [74, 85, 92, 69]) for t in refog), refog
    # ... light (91 %): a thinner fog than heavy (100 %), the fog first all the same; fewer tiny beads than heavy (the user: slightly
    # fewer tiny dots in light, more in heavy than light): its tiny ones condensing a little slower (0.95 of MISTING, heavy 1.35), so
    # fewer still on the glass (200 frames, against heavy's 150 + 540), and far fewer running
    light, heavy = got["humidityLight"], got["humidity"]["alone"]
    assert light["errors"] == [] and light["popped"] == 0 and light["stray"] == 0, light
    tinies = [t["n"] for t in light["tinies"]]   # (condensing on and on, more and more of them)
    assert tinies[1] < tinies[4] < tinies[8], light["tinies"]
    assert 0.1 < light["fog"]["haze"] < 0.7 * heavy["fog"]["haze"], (light["fog"], heavy["fog"])
    assert 0 < light["showing"]["still"] < heavy["showing"]["still"], (light["showing"], heavy["showing"])
    assert light["showing"]["running"] < 0.5 * heavy["showing"]["running"], (light["showing"], heavy["showing"])
    # the fog dithered irregularly: no grid in its pixels (as gridPeak sees it: under 100; ordered, about 420)
    assert heavy["grid"] < 100, heavy["grid"]
    assert light["first"]["after"] >= 2 and light["first"]["fog"] > 0.1 and light["first"]["formed"]["edge"] > 0.1, light["first"]
    # ... in each, the fog a fairly even haze (gently varied, no gaps: its spread inside, about 0.1; it was patchy, about 0.4, with
    # gaps), a little denser at the edges (the sides and the bottom) than in the middle; dense (its mean opacity as drawn: heavy about
    # 0.28, light about 0.28 (a little less wiped)), see-through all the same (no fog pixel more than half opaque)
    for fog in (light["fog"], heavy["fog"]):
        assert fog["cover"] == 1 and fog["spread"] < 0.2 and fog["gaps"] == 0, fog
        assert 1.1 * fog["middle"] < fog["edge"] < 2 * fog["middle"], fog
    assert heavy["fog"]["opacity"] > 0.19 and light["fog"]["opacity"] > 0.15 and heavy["fog"]["most"] < 0.5, (light["fog"], heavy["fog"])
    # ... and the humidity then below 90 % (heavy, in the wind): the fog clearing as from a demister, the middle first, the edges
    # still fogged then, all gone in the end (the effect off, the wind on); the slow beads drying away (shrinking, fading: none
    # vanishing), those in the middle sooner than those at the edges; the rest running off or shrinking away on the clear glass
    clears = got["humidity"]["windy"]["clears"]
    assert clears["start"] >= 5 and clears["middleGone"] is not None and clears["edgeGone"] is not None, clears
    assert clears["middleGone"] < clears["edgeGone"] and clears["edgeThen"] > 0.3, clears
    assert clears["left"] == {"cover": 0, "thickness": 0, "beads": 0} and clears["effects"] == ["wind"], clears
    assert clears["popped"] == 0 and clears["shrank"] > clears["start"], clears
    # (the condensed beads' trails going with the fog as it clears: none of them ever showing a clear-glass streak's look (film,
    # grime, glint, droplet) while it clears, and once it has cleared nothing of them on the glass, many still listed (fresh still))
    assert clears["looks"] == 0, clears
    assert clears["cleared"]["trails"] > 200 and clears["cleared"]["ink"] == 0 and clears["cleared"]["looks"] == 0, clears["cleared"]
    assert clears["end"]["ink"] == 0 and clears["end"]["looks"] == 0, clears["end"]
    middle = [g["frame"] for g in clears["gone"] if g["far"] < 0.4]
    edges = [g["frame"] for g in clears["gone"] if g["far"] > 0.7]
    assert middle and edges and sum(middle) / len(middle) < sum(edges) / len(edges), clears["gone"]
    # ... every other effect then on too: humidity's beads outside the shared budget (their own limits bound them), so none squeezed
    # out (as many, give or take a few run off), none vanishing. (This checked fewer before, from when they shared it; since they
    # haven't, it held only while heavy humidity's beads were short of their most and a few ran off. Heavy now condenses enough to
    # keep them at their most.)
    squeezed = got["humidity"]["alone"]["squeezed"]
    assert squeezed["count"][1] >= squeezed["count"][0] - 3 and squeezed["popped"] == 0 and squeezed["stray"] == 0, squeezed
    # ... then all off (trails there as it went), faded out all the way: every trail and bead forgotten, its canvas clear; and
    # humidity on again: the glass fresh
    again = got["humidity"]["alone"]["again"]
    assert again["before"] > 0 and again == {"before": again["before"], "trails": 0, "beads": 0, "stray": 0, "gone": again["gone"], "marks": again["marks"]}, again
    clean_glass(again["gone"])
    clean_glass(again["marks"])
    # ... then its beads leaving trails, and "stop animation", humidity on again (as a test link after it): the glass fresh too
    marked = got["humidity"]["alone"]["marked"]
    assert marked["trails"] > 30 and marked["ink"] > 0 and marked["cover"] > 0, marked
    started_afresh(got["humidity"]["alone"]["restarted"], "humidity")
    # BEAD_RULES, the physics every bead runs by: its speed on each glass, dry slowest (0.3 of fog's), fog quicker, a trail fastest
    # (1.6 times fog's: water on water; the user: fog beads move fast down tracks like with rain), for the same size; still under
    # the run size (about eight tiny ones' water: r 1.1), from there creeping extremely slowly, rising gradually with its size (then
    # faster and faster, no fastest); after 20 px in 5 s: on fog bigger (however big already: no largest), on a trail the same, on
    # dry glass smaller, a small one shrunk to nothing (gone)
    physics = got["humidity"]["alone"]["physics"]
    speed, after, sizes = physics["speed"], physics["after"], physics["sizes"]
    for i, r in enumerate(sizes):
        if r < physics["run"]:   # (under the size a bead runs from, about large: it stays put on any glass)
            assert speed["dry"][i] == speed["trail"][i] == speed["fog"][i] == 0, (r, speed)
        else:
            assert speed["dry"][i] < speed["fog"][i] < speed["trail"][i], (r, speed)
            assert speed["trail"][i] == pytest.approx(1.6 * speed["fog"][i]) and speed["dry"][i] == pytest.approx(0.3 * speed["fog"][i]), speed
        assert after["fog"][i] > r and after["trail"][i] == r and after["dry"][i] < r, (r, after)
    for g in ("dry", "trail", "fog"):
        moving = [v for r, v in zip(sizes, speed[g]) if r >= physics["run"]]   # (from the run size up: under it, still)
        assert speed[g] == sorted(speed[g]) and len(set(moving)) == len(moving) >= 6, (g, speed[g])
        steps = [b / a for a, b in zip(moving, moving[1:])]
        assert all(1 < k < 15 for k in steps), (g, steps)   # (gradually: no jump from creeping to running)
    assert 1.05 <= physics["run"] <= 1.15, physics["run"]   # (about eight tiny ones' water)
    assert physics["sizes"][:3] == [0.6, 1, 1.1] and physics["run"] <= 1.1, physics   # (r 0.6 and 1 still; 1.1 running, creeping)
    assert speed["fog"][0] == speed["fog"][1] == 0 and 0.05 < speed["fog"][2] < 0.3 and speed["fog"][-1] > 100, speed["fog"]
    assert after["dry"][0] == 0 and after["fog"][-1] > sizes[-1], after
    # heavy rain's beads: light rain none; heavy, a drop sticking every few s (one in 3 s), a dozen or so (14) on the glass at most, each landing at once
    # (no fading in) as a large bead (r 3 or more) with a splash (a ring of 10 or more droplets); on dry glass slow (about 0.3 of its
    # speed on fog, the same size), shrinking only (its water left behind), never growing; none vanishing; their wet streaks on the
    # clear glass drying over 3 to 10 minutes, each its own: thin streaks (a third of the bead's width or less: a px or two), faint
    # (a greyish film, grime along an edge, a glint, a tiny droplet), uneven along their length (thicker and thinner, now and then
    # broken for a px or three, never more, no regular pattern of gaps: no dashes), their thin parts drying first (gone from view
    # well before the trail is dry), the thick ones last. The rain light again: none sticking; those there running off or shrinking away
    sticking = got["rainBeads"]
    assert sticking["errors"] == [] and sticking["light"] == {"landed": 0, "levels": 0.35}, sticking
    heavy_rain = sticking["heavy"]
    # (75 s of heavy rain at one in 3 s: about 26; more than the handful (5) there were at most before, up to 14)
    assert 12 <= heavy_rain["landed"] <= 42 and 6 <= heavy_rain["rainMost"] <= 14 and heavy_rain["popped"] == 0, heavy_rain
    assert len(sticking["stuck"]) == heavy_rain["landed"], sticking["stuck"]
    assert all(s["r"] >= 3 and s["alpha"] == 1 and s["dots"] >= 10 and s["phase"] == "heavy" for s in sticking["stuck"]), sticking["stuck"]
    dry = heavy_rain["glass"]["dry"]
    assert dry["lost"] > 0 and dry["gained"] == 0 and 0.2 < dry["all"] / dry["norm"] < 0.4, dry
    assert heavy_rain["glass"]["fog"]["moves"] == 0, heavy_rain["glass"]   # (no fog: rain alone)
    drying = [t["lasted"] for t in sticking["drying"]]
    assert len(drying) >= 2 and all(170 <= t <= 610 for t in drying), sticking["drying"]
    assert all(t["look"][:3] in STREAK + DROPLET and 0 < t["look"][3] <= (150 if t["look"][:3] in DROPLET else 80) for t in sticking["drying"]), sticking["drying"]
    hidden = sorted(t["hidden"] for t in sticking["drying"])
    assert hidden[0] < 0.75 and hidden[-1] > 0.7, sticking["drying"]
    streaks = list({t["id"]: t for t in sticking["looks"] if t["kind"] == "clear"}.values())   # (each bead's once)
    assert len(streaks) >= 2 and all(t["kind"] == "clear" for t in sticking["looks"]), sticking["looks"]
    # (thin: all told a third of the beads' width or less; each a px or two (2 at most on the whole: a bead shrunk on dry glass to
    # under 6 px wide keeps its streak's width, so a third of its own width may be under 2); a blob)
    rows = [w for t in streaks for w in t["widths"]]
    assert sum(rows) / len(rows) <= sum(t["w"] for t in streaks) / len(streaks) / 3, streaks
    for t in streaks:
        widths = t["widths"]
        assert sum(widths) / len(widths) <= (2 if t["w"] >= 5.5 else max(1.6, t["w"] / 3)) and max(widths) <= t["w"] / 2 + 1.5, t
        assert len(set(widths)) >= 2 and max(widths) - min(widths) >= 1, t                              # (uneven along it)
        empty = [w == 0 for w in widths]
        runs = [len(r) for r in "".join("x" if e else "." for e in empty).split(".") if r]
        assert sum(empty) <= 0.25 * len(widths) and max(runs, default=0) <= 3, t                        # (broken a little, at most)
        starts = [i for i, e in enumerate(empty) if e and (i == 0 or not empty[i - 1])]
        assert len(starts) < 3 or len({b - a for a, b in zip(starts, starts[1:])}) > 1, t               # (no regular dashes)
    eased, gone = sticking["eased"], sticking["after"]
    assert gone["landed"] == eased["landed"] and gone["beads"] == 0 and gone["popped"] == 0, (eased, gone)
    # (each run off or shrunk to nothing, or merged into one that was: none dried away otherwise)
    assert gone["ranOff"] + gone["shrunk"] > eased["ranOff"] + eased["shrunk"] and eased["beads"] > 0, (eased, gone)
    assert gone["faded"] == eased["faded"], (eased, gone)
    # ... their streaks, the rain still on, drying on their own time: there still 6 s after the last bead had gone
    easing = sticking["easing"]
    assert easing["at"]["beads"] == 0 and easing["at"]["trails"] > 0, easing
    assert easing["after"]["trails"] > 0 and easing["after"]["ink"] > 0 and easing["after"]["amount"] == 1, easing
    # heavy again, streaks on the glass; off, faded out all the way: every streak and stain forgotten, its canvas clear; heavy
    # again: clean glass, nothing from before (no trail, no stain, no bead, no drop); and the same with the animations stopped
    # and started between
    for streaked in (sticking["streaked"], sticking["streakedAgain"]):
        assert streaked["trails"] >= 30 and streaked["ink"] > 0, streaked
    cleared = sticking["cleared"]
    clean_glass(cleared["gone"])
    clean_glass(cleared["again"])
    assert cleared["stray"] == 0 and cleared["drops"] == 0, cleared
    started_afresh(sticking["restarted"], "rain")
    # the wind blows right to left: its leaves leftward overall, as drawn on the canvas too (each one's middle, frame to frame, over 20 s
    # of its time: its gusts and lulls); some caught in a loop going rightward a moment (on a phone too)
    for seen in (got["wind"], got["phoneWind"]):
        assert seen["vx"] < -10 and seen["against"] > 0 and seen["frames"] > 20 and seen["left"] > 3 * seen["right"] > 0, seen
        assert seen["hits"] == 0 and seen["whirl"] == 0, seen   # (no rain: nothing knocks the leaves)
    # the strip at the bottom right: its links, then the time at the far right; on a phone with both its links, the test row
    # beside it or above it, never over it
    phone = got["phone"]
    assert phone["strip"] == "reset preferences · last reading 18:42" and phone["right"], phone
    assert phone["apart"] and phone["onScreen"], phone
    # "stop animation" at the bottom left, just right of the test row's last link (with the row above the strip on a phone, both clear
    # of it); without the test row (the page as it will be), at the bottom left on its own, at 1024x768 and on a phone
    corner = got["phoneCorner"]
    assert corner["link"] == "stop animation" and corner["apart"] and corner["onScreen"], corner
    just_right_of_the_row(corner)
    assert corner["motion"]["bottom"] == pytest.approx(corner["strip"]["top"], abs=0.5), corner                  # (above the strip)
    # (no "test:" there, all of it unscrolled, room for one more short link: as with every effect on, in the links' test)
    assert not corner["tag"] and corner["fits"] and corner["spare"] >= 30, corner
    for bare in (got["bare"], got["barePhone"]):
        assert bare["errors"] == [] and bare["row"] is None and bare["link"] == "stop animation", bare
        assert bare["motion"]["left"] == 0 and bare["motion"]["bottom"] == bare["window"][1] and bare["apart"] and bare["onScreen"], bare
    # the frost: fern-like crystals from the window's edges, further in and faster the colder, over minutes; melting back from the
    # tips, the edges last. Just below 0 °C (-0.5) a fringe at the edges, grown in a minute or two; then gone again, the loop with it
    frost = got["frost"]
    assert frost["errors"] == [], frost["errors"]
    slight, cold, minute = frost["slight"], frost["cold"], frost["minute"]
    assert 40 <= slight["took"] <= 180 and slight["deepest"] < 0.2 and 0.01 < slight["crystals"] < 0.1 and slight["rim"] > 0.4, slight
    assert slight["fresh"] == 0 and not slight["growing"], slight                     # (the fresh tips only while it grows)
    assert frost["slightGone"]["extent"] == 0 and not frost["slightGone"]["running"], frost["slightGone"]
    # -5 °C: a minute in, growing on (its fresh tips, white, at the front), past the fringe already but not half way to where it goes
    assert minute["growing"] and minute["fresh"] > 20 and minute["extent"] < 0.5 * minute["target"], minute
    assert minute["deepest"] > slight["deepest"] and minute["crystals"] > slight["crystals"], (minute, slight)
    # ... as far as it goes some 3 to 5 minutes in: well across the glass, frost over much of it; uneven fingers, some reaching far
    # in, others stopping short; sparkles catching the light now and then
    assert 180 <= cold["took"] <= 300 and not cold["growing"] and cold["fresh"] == 0, cold
    assert cold["deepest"] > 0.4 and cold["crystals"] > 0.18 and cold["frosted"] > 0.3 and cold["rim"] > 0.9, cold
    fingers = sorted(cold["fingers"])
    assert fingers[-1] > 0.35 and fingers[0] < 0.6 * fingers[-1] and fingers[-1] - fingers[len(fingers) // 2] > 0.05, fingers
    assert frost["sparkles"] >= 1, frost["sparkles"]
    # ... its haze dithered irregularly: its commonest shade (the haze's faint one) on no grid, about a third of its pixels in their
    # 4 commonest places of 16 (x mod 4, y mod 4: on an ordered 4x4 dither, as it was, over 0.8)
    assert cold["grid"]["n"] > 500 and cold["grid"]["share"] < 0.5, cold["grid"]
    # ... and varied, as real frost on a window: fern fronds, short plumes, needles radiating from a point; ahead of the front only a
    # sparse dusting of tiny specks (a pixel or two each: no snowflake-shaped stars or plates), a few fronds from a little way in (as if from a scratch); the fronds' lengths and their branches' spacing spread widely, the
    # branches mostly near 60° to their stems (ice's hexagonal habit), each frond its own; the seeds along the edges uneven, crowded
    # in some stretches with bare gaps in others; the shading uneven: ridges brighter than side branches, those than the fine
    # barbs, the shades spread widely and in several tints
    made, shades = cold["made"], cold["shades"]
    assert set(made["forms"]) == {"fern", "plume", "needles", "speck"}, made["forms"]
    assert all(made["forms"][k] >= 5 for k in ("fern", "plume", "needles")) and made["scratches"] >= 1, made["forms"]
    specks = made["specks"]
    assert len(specks) == made["forms"]["speck"] >= 5 and set(specks) <= {1, 2} and 1 in specks and 2 in specks, specks
    assert len(specks) < cold["size"][0] * cold["size"][1] / 1000, (len(specks), cold["size"])   # (sparse)
    q = lambda v, p: sorted(v)[int(p * (len(v) - 1))]
    lengths = [f["length"] for f in made["fronds"]]
    spacings, angles = [f["spacing"] for f in made["fronds"] if f["spacing"]], [f["angle"] for f in made["fronds"] if f["angle"]]
    assert len(lengths) >= 40 and q(lengths, 0.9) > 5 * q(lengths, 0.1) and max(lengths) > 60, sorted(lengths)
    assert len(spacings) >= 30 and q(spacings, 0.9) > 2.5 * q(spacings, 0.1), sorted(spacings)
    assert 55 <= q(angles, 0.5) <= 65 and q(angles, 0.1) > 45 and q(angles, 0.9) < 75 and q(angles, 0.9) - q(angles, 0.1) > 6, sorted(angles)
    gaps = [b[1] - a[1] for a, b in zip(made["seeds"], made["seeds"][1:]) if a[0] == b[0]]
    assert len(gaps) >= 40 and q(gaps, 0.9) > 4 * q(gaps, 0.1) and max(gaps) > 4 * q(gaps, 0.5), sorted(gaps)
    assert shades["stem"] > shades["side"] + 20 > shades["barb"] + 40 and shades["spread"] > 35 and shades["tints"] >= 3, shades
    # ... faint (the tiles read over it), pale cyan to white
    shown = cold["pixels"]
    assert shown["drawn"] > 0.3 and shown["alpha"] < 0.15 and shown["strong"] < 0.002, shown
    assert shown["light"] > 180 and shown["bluer"] > 0.95, shown
    # melting: back from the tips, the edges last: half a minute in, fewer crystals, the rim still frosted; then gone, the loop too
    melting = frost["melting"]
    assert melting["extent"] < cold["extent"] and melting["crystals"] < 0.9 * cold["crystals"], (melting, cold)
    assert melting["rim"] >= 0.97 * cold["rim"] and melting["fresh"] == 0, (melting, cold)
    assert frost["gone"]["extent"] == 0 and frost["gone"]["frosted"] == 0 and not frost["gone"]["running"], frost["gone"]
    # ... and on again, nothing from before (its canvas clear, no sparkle); grown half a minute, then the animations stopped and
    # started again, and on: the same
    assert frost["again"] == {"extent": 0, "ink": 0, "sparkles": 0}, frost["again"]
    assert frost["grown"]["extent"] > 0 and frost["grown"]["ink"] > 0, frost["grown"]
    started_afresh(frost["restarted"], "ice")
    assert frost["restarted"]["frost"] == {"extent": 0, "ink": 0, "sparkles": 0}, frost["restarted"]
    # reduced motion: no loop, one still, faint frame (the ice as it would be), the frames and the link as ever
    still = got["still"]
    assert still["effects"] == ["uv", "ice"] and not still["running"] and still["frames"] == 0 and still["stateFrames"] == 0, still
    assert still["ink"] > 0 and sorted(still["framed"]) == ["Temperature", "UV index"] and still["link"] == "stop animation", still
    assert still["hand"] == "rotate(-27.50 240 203)", still["hand"]                 # (the UV's red hand at once at 9: no frames for it)
    # ... and humidity's still frame as it would be some minutes in: the fog formed, many beads on it, some that have run, their trails
    beading = still["humidity"]
    assert beading["fog"] == 1 and beading["beads"] >= 20 and beading["ran"] >= 2 and beading["trails"] > 30 and beading["stray"] == 0, beading
    assert not beading["running"] and beading["frames"] == 0, beading
    # ... and a flash flood's: full at once, still, no frog
    flood = still["flood"]
    assert flood["depth"] == flood["full"] > 0 and flood["frog"] is None and not flood["running"] and flood["frames"] == 0, flood
    # ... and a gust (the page's, the test link's, asked for by name): nothing blown across, no loop
    assert still["gust"] == {"fling": None, "sent": 0, "running": False, "frames": 0}, still["gust"]


def clean_glass(marks):
    """Nothing on the glass (Sky.state().humidity.marks()): no trail, no stain on its canvas, no bead, no fog"""
    assert {k: marks[k] for k in ("trails", "listed", "ink", "beads", "cover")} == {"trails": 0, "listed": 0, "ink": 0, "beads": 0, "cover": 0}, marks


def started_afresh(seen, effect):
    """"stop animation", then `effect` on (as a test link after it: restarted() in the browser script): the loop stopped and the link
    gone between, then running again with the link back, and nothing from before: the glass clean, no drop, no stray pixel"""
    assert seen["link"] == "stop animation" and seen["stopped"]["link"] is None and not seen["stopped"]["running"], seen
    assert seen["again"] == {"running": True, "link": "stop animation"}, seen
    clean_glass(seen["stopped"]["marks"])
    clean_glass(seen["marks"])
    assert seen["stray"] == 0 and seen["drops"] == 0 and effect in seen["effects"], seen


@needs_browser
async def test_a_flash_flood_fills_the_window_a_quarter_up_its_surface_choppy_what_falls_ending_at_it_and_a_frog_now_and_then(server, station):
    got = await weather(server, "flood")
    # the station's rain rate over 30 mm/h, not at 30: a flash flood (the rain's too); the Rain tile its tile
    assert got["at30"]["effects"] == ["rain"] and got["at30"]["levels"]["flood"] == 0, got["at30"]
    assert got["over"]["effects"] == ["rain", "flood"] and got["over"]["levels"]["flood"] == 1 and got["over"]["framed"] == ["Rain"], got["over"]
    # the water rising from the bottom at a steady pace to a quarter of the window's height, in 3 to 5 s
    rising = got["rising"]
    assert rising["full"] == round(rising["height"] / 4), rising["full"]
    speeds = rising["speeds"]
    assert len(speeds) >= 20 and max(speeds) - min(speeds) < 0.05 * max(speeds), speeds
    assert 3 <= rising["full"] / speeds[0] <= 5, speeds[0]
    # its surface choppy all across (many crests, about one every 8 px; each third of the width as choppy), higher at the sides
    # (waves piling up and slapping there: about 1.6 times the middle's), the chop up while it pours; the rain ending at it (none
    # drawn below it), every drop a ripple there, the near ones a splash
    assert rising["crests"] > rising["width"] / 20 and rising["middle"] > 0.8, rising
    assert min(rising["thirds"]) > max(0.15, 0.5 * max(rising["thirds"])), rising["thirds"]   # (about 0.3 to 0.5 px each)
    assert rising["edges"] > 1.25 * rising["middle"], rising
    assert rising["chop"] > 0.8 and rising["under"] == 0 and rising["hits"] > 200 and rising["splashes"] > 3, rising
    # (no frog for a real flood's first 30 s; after that only now and then, at random)
    assert not rising["test"] and rising["nextFrog"] >= 30 and rising["frog"] is None, rising
    # its waves (a damped wave equation): one push travelling both ways at their speed, reflecting off a side, dying away slowly
    waves = rising["waves"]
    there = lambda xs, x: any(abs(t - x) <= 5 for t in xs)
    assert there(waves["at1"], 60 - waves["speed"]) and there(waves["at1"], 60 + waves["speed"]), waves
    assert there(waves["at3"], 3 * waves["speed"] - 60) and there(waves["at3"], 60 + 3 * waves["speed"]), waves   # (back from the left)
    assert -1.5 < waves["deepest3"] < -0.25, waves
    # the rate 30 again: draining in about 6 s, the chop calming slowly (still much of it half way down); then gone, the rain on
    draining = got["draining"]
    assert 5 <= draining["took"] <= 7 and draining["surface"] == 0 and got["drained"]["effects"] == ["rain"], (draining, got["drained"])
    assert draining["half"]["chop"] > 0.7 * draining["chop0"], draining
    # the test link (every other effect on): rain light, heavy, then heavy with a flash flood (rain3; the test's own flood level, 2),
    # then off (the wind turned off before it): draining
    linked = got["linked"]
    assert [(s["url"].split(",")[-1], s["label"], s["rain"], s["flood"]) for s in linked["steps"]] == [
        ("rain", "rain: light", 0.35, 0), ("rain2", "rain: heavy", 1, 0), ("rain3", "rain: flood", 1, 2)], linked["steps"]
    assert linked["off"]["url"] == "?fx=haze3,uv,humidity2,heat,ice" and linked["off"]["flood"] == linked["off"]["rain"] == 0, linked["off"]
    # ... its first frog some 30 s after the water started rising, not before; one, crossing right to left, on the waves (bobbing)
    assert 29 <= linked["before"] < 30 <= linked["first"] <= 31 and linked["frogs"] == 1, linked
    track = linked["track"]
    assert len(track) > 15 and track[0][0] > 0.9 * got["rising"]["width"] and track[-1][0] < 0.1 * got["rising"]["width"], track[::10]
    assert all(b[0] <= a[0] for a, b in zip(track, track[1:])), track   # (two looks a frame drawn, at times)
    assert all(abs(y - surface) <= 1 for _, y, surface in track) and max(t[1] for t in track) - min(t[1] for t in track) >= 2, track
    # ... the leaves: landing on it and lying there (on the surface line, or a px into it), then sinking and resting on the bottom (on
    # the pile of those before, 6 px high at most); within the budget (those resting none of it), a third of them or more flying on the whole
    leaves = linked["leaves"]
    assert leaves["float"] >= 2 and leaves["sink"] >= 1 and leaves["rest"] >= 1, leaves
    assert linked["gaps"] and set(linked["gaps"]) <= {0, 1}, sorted(set(linked["gaps"]))
    assert linked["resting"] and all(-8 <= r <= 0 for r in linked["resting"]), sorted(set(linked["resting"]))
    assert 0 < linked["most"] < 1 and linked["over"] <= 3, (linked["most"], linked["over"])
    assert linked["fewest"] > 0 and linked["flying"] >= 1 / 3, (linked["fewest"], linked["flying"])   # (never all in the water)
    # ... every one stuck on it sinking and resting on the bottom, none lost over the long run (the wind's, the heavy rain's, 100 s
    # and more): every one it took still in it at the end, those it took all resting but those still floating or sinking; and every
    # one resting showing on the bottom all along (its px never fewer)
    kept = linked["kept"]
    assert kept["took"] >= 30 and kept["ids"] == kept["took"] and kept["missing"] == [], kept
    assert kept["took"] == kept["rested"] + kept["float"] + kept["sink"] and kept["rested"] >= 20, kept
    assert kept["looks"] >= 10 and kept["hidden"] == 0 and kept["shrank"] == 0 and kept["last"]["shown"] == kept["last"]["of"] >= 20, kept
    # ... every leaf its wavy surface reaches stuck on it (many, in the wind, the heavy rain knocking them about): none flying drawn
    # touching it or under it, none flying again, above it or going back a state (floating, sinking, resting, in that order); those
    # seen flying the frame before it took them, all of them above it then (some well above: crossing it between frames); none
    # vanishing near it but into it
    stuck = linked["stuck"]
    assert stuck["wet"] >= 8 and stuck["seen"] >= 0.6 * stuck["wet"] and stuck["near"] >= 5 and stuck["hits"] > 0, stuck
    assert stuck["touching"] == 0 and stuck["back"] == 0 and stuck["above"] == 0 and stuck["backwards"] == 0, stuck
    assert stuck["lost"] == [] and all(g is None or g >= 1 for g in stuck["lastGaps"]), stuck
    assert sum(g is None or g > 3 for g in stuck["lastGaps"]) >= 3, stuck   # (crossing it between frames: from well above it)
    # ... the drips: joining it as they reach it, none below it; each running on down to the waves' own height where it is (its
    # sprite's bottom at the surface there, ripples and all, or under a crest risen over it), never joining it short of that, its
    # trail all the way down with it, wet to the water
    assert linked["joined"] >= 1 and linked["under"] == 0, linked
    joins = linked["joins"]
    assert joins and all(j["bottom"] >= j["surface"] for j in joins), joins
    assert all(j["trail"] == j["bottom"] and j["wet"] > 0 for j in joins if j["trail"] is not None) and any(j["trail"] for j in joins), joins
    # ... and nothing on the glass cut off in a straight line at its level: where a trough dips 3 px or more under it, the rows just
    # under the level as drawn on as those just above (the haze, the heat, the rain's spray, the fog, all down to the wavy surface)
    cut = linked["cut"]
    assert cut["n"] >= 100 and cut["at"] >= 0.8 * cut["above"], cut
    # ... its body dithered irregularly: no grid in its pixels (as gridPeak sees it, its bottom 32 rows: under 100; ordered, about 200)
    assert linked["grid"] < 100, linked["grid"]
    # ... the wind off (and gone): the leaves in the water its own, staying (those resting still resting), the floating ones sinking
    # or resting by 16 s on, none flying; then every one resting, showing on the bottom, none of them particles
    off = linked["windOff"]
    before, after = {i: st for i, st, _ in off["before"]}, {i: st for i, st, _ in off["after"]}
    assert "wind" not in off["effects"] and off["flyers"] == 0, off
    assert "rest" in before.values() and "float" in before.values() and set(before) <= set(after), off
    assert all(after[i] == "rest" for i, st in before.items() if st == "rest"), off
    assert all(after[i] in ("sink", "rest") for i, st in before.items() if st == "float"), off
    settled = off["settled"]
    assert settled["took"] == settled["rested"] == settled["all"] == len(after) == settled["shows"]["shown"] and settled["particles"] == 0, settled
    # ... off: draining in about 6 s, its leaves all there till its last few px, then gone with it; a new flood: none from before
    drained = linked["drained"]
    assert 5 <= drained["took"] <= 7 and drained["wet"] == drained["all"] == 0 and drained["frog"] is None, drained
    assert drained["low"] == drained["n0"] == len(after), drained
    assert drained["effects"] == ["haze", "heat", "uv", "humidity", "ice"], drained
    again = linked["again"]
    assert again["url"].endswith(",rain3") and again["leaves"]["all"] == 0 and again["leaves"]["wet"] == [], again
    # every effect on with the flood (full): as cheap as ever (the script's own time a frame, the main thread's CPU time, whatever else
    # the machine is doing: about 2 ms), within the budget (kept by every effect but humidity's beads, heavy rain's among them: their
    # own limits bound them)
    costly = linked["costly"]
    assert costly["effects"] == ["haze", "heat", "uv", "rain", "wind", "humidity", "flood", "ice"] and costly["cost"] < 8, costly
    assert sum(v for k, v in costly["particles"].items() if k != "humidity") <= costly["budget"] + 3, costly


@needs_browser
async def test_a_gust_over_99_kmh_blows_a_cow_a_barn_or_a_chicken_across_far_behind_the_tiles(server, station):
    got = await weather(server, "gust")
    # the live gust: at 99 nothing; just over, one sent; the same reading fetched again, no more; a new one over, another; under, none
    assert got["live"] == [0, 1, 1, 2, 2] and got["liveEffects"] == ["gust"], got["live"]
    assert set(got["liveKinds"]) <= {"cow", "barn", "chicken"}, got["liveKinds"]
    # a cow, a barn and a chicken at once: each right to left all the way across (in at the right, out at the left), every frame
    # further left, in the window's upper part; large (a sixth of the window across or so) and faint (most pixels under half
    # opaque), on the back layer, alone here; crossing in some 6-14 s of the weather's time
    crossing = got["crossing"]
    width, height, _ = crossing["size"]
    assert crossing["kinds"] == ["cow", "barn", "chicken"] and crossing["effects"] == ["gust"], crossing
    for kind, track in crossing["tracks"].items():
        xs, ys = [p[0] for p in track], [p[1] for p in track]
        assert len(track) >= 30 and xs[0] > width and xs[-1] < 0 and all(b < a for a, b in zip(xs, xs[1:])), (kind, xs)
        assert all(0 < y < 0.6 * height for y in ys), (kind, ys)
        assert track[0][5] >= 50 and track[0][5] >= width / 7, (kind, track[0])
        assert 6 <= track[-1][6] - track[0][6] <= 14, (kind, track[0], track[-1])
    # the cow and the barn tumbling end over end, slowly (anticlockwise, rolling over leftward: 0.3-1 rad/s, more than a turn
    # across); the chicken not: facing out, swaying a little (under 0.2 rad either way), its head turning a touch each way, its legs
    # swinging through all their frames
    for kind in ("cow", "barn"):
        track = crossing["tracks"][kind]
        angles, took = [p[2] for p in track], track[-1][6] - track[0][6]
        assert all(b < a for a, b in zip(angles, angles[1:])), (kind, angles)
        assert 0.3 <= (angles[0] - angles[-1]) / took <= 1 and angles[0] - angles[-1] > 3.2, (kind, angles[0], angles[-1], took)
    chicken = crossing["tracks"]["chicken"]
    assert all(abs(p[2]) <= 0.2 for p in chicken) and max(p[2] for p in chicken) - min(p[2] for p in chicken) > 0.1, chicken
    assert {p[3] for p in chicken} == {-1, 0, 1} and {p[4] for p in chicken} == {0, 1, 2, 3}, chicken
    assert crossing["inked"] > 1500 and 60 <= crossing["median"] <= 130, crossing
    assert crossing["cost"] < 8, crossing["cost"]
    # once they're gone: nothing left, nothing drawn, the loop ended
    assert crossing["after"]["flyers"] == [] and crossing["after"]["bits"] == 0 and crossing["ink"] == 0 and not crossing["running"], crossing
    # the test link: each click one more, at random (all three kinds in five clicks), ?fx=gust; not a switch: never pressed
    link = got["link"]
    assert link["sent"] == 5 and set(link["kinds"]) == {"cow", "barn", "chicken"} and link["flying"] >= 1, link
    assert link["url"] == "?fx=gust" and link["pressed"] is None and "on" not in link["classes"].split(), link
    # "stop animation": gone at once, nothing drawn, ?fx= gone; stopped, a live gust over 99 km/h sends none; the gust link after it
    # starts them again, one sent
    assert got["stopped"] == {"flyers": 0, "running": False, "url": "", "ink": 0, "sent": 5}, got["stopped"]
    assert got["resumed"] == {"sent": 6, "running": True}, got["resumed"]
    # ?fx=gust as the page opens: one sent
    opened = got["opened"]
    assert opened["sent"] == 1 and opened["effects"] == ["gust"] and opened["url"] == "?fx=gust" and opened["errors"] == [], opened
    # on a phone, every effect on and "gust" clicked: one sent; no "test:", every link ("gust" too) and "stop animation" in sight
    phone = got["phone"]
    assert phone["sent"] == 1 and phone["buttons"][-1] == "gust" and not phone["tag"] and phone["fits"] and phone["onScreen"], phone
    just_right_of_the_row(phone)
    assert got["errors"] == [] and phone["errors"] == [], (got["errors"], phone["errors"])
