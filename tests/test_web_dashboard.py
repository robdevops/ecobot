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
one canvas behind the tiles, "stop", the temporary test links and ?fx=, MCP_DASHBOARD_ANIMATIONS on, low and off and reduced motion
(what moves in each), uv's beams swaying slowly in place
with no particles, the wind's leaves within the budget and blowing right to left, rain in the wind bunched into curtains, its drops
knocking the leaves they hit, humidity's fog and its beads, heavy rain's drops sticking to the glass, a flash flood filling the window
a quarter up, its waves, its leaves and its frog, a cow, a barn or a chicken blown across in a gust over 99 km/h), the data's rain
rate, and the
dials' thin red hands at today's highest (the wind's gust, rain rate, sun, UV: their tiles' Daily figures, the reading now counted in,
moving smoothly) with the wind's Gust column.
With ECOBOT_SCREENSHOTS set, its screenshots are kept in that folder."""

import asyncio
import colorsys
import base64
import functools
import html
import io
import json
import math
import re
import os
import shutil
import subprocess
import time
from pathlib import Path
from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

import httpx
import pytest

from lib import gauge_sheet, solar, web_dashboard
from lib.ecowitt.direction import POINTS
from lib.gauge_look import look, tile_json
from lib.mcp_server import AIR_URI, EcobotServer, WEATHER_URI, http_app
from lib.service.context import Context
from lib.service.names import Catalogue
from lib.ecowitt.reads import directions_on, pressure_on, rain_rates_on
from lib.gauge_look import CHIPS, MAX_CHIP, START, SWEEP
from lib.web_dashboard import (DESCRIPTION, ICON, ICON_URL, PAGE, RED_HANDS, WebDashboard, air_readings_of, rain_rate_of, readings_of,
                               today_highs)
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

    def held(self, rose: list[int] | None = None, pressure: list[tuple[float, float]] | None = None,
             rain: list[tuple[float, float]] | None = None):
        """The station's file holds today's wind directions (directions_on's: minutes from each of the 16 compass points; none: all
        0) and the past four hours' pressure (pressure_on's, given as [(minutes before now, hPa)], oldest first; none: no
        reading) and the past six hours' 5-minute rain rates (rain_rates_on's, [(minutes before now, mm/h)], oldest first; none:
        no history), as the dashboard reads them (nothing else: the red hands' highs are the sheet's)."""
        async def read(fn, mac, *args):
            assert mac == "fake" and fn in (directions_on, pressure_on, rain_rates_on)
            start, end = args
            if fn is rain_rates_on:                                              # (the past 6 hours: FLOOD_READ)
                assert end - 60 - start == web_dashboard.FLOOD_READ == 6 * 3600, (start, end)
                return [(int(end - 60 - m * 60), v) for m, v in (rain or [])]
            assert abs(end - 60 - time.time()) < 5
            if fn is pressure_on:                                                # (the past 4 hours: TENDENCY_READ)
                assert end - 60 - start == web_dashboard.TENDENCY_READ == 240 * 60, (start, end)
                return [(int(end - 60 - m * 60), v) for m, v in (pressure or [])]
            assert datetime.fromtimestamp(start, TZ).time() == datetime.min.time()   # (today's: from the station's midnight to now)
            assert datetime.fromtimestamp(start, TZ).date() == datetime.now(TZ).date()
            return list(rose or [0] * 16)
        self.eco.storage, self.eco.mac = NS(read=read), "fake"

    def highs_today(self, highs: dict):
        """Today's day rows hold these highest ({day figure: value}, e.g. {"wind_gust": 37.0}), as current's context gives them."""
        self.values = {k: dict(v) for k, v in self.values.items()}
        for figure, value in highs.items():
            self.values.setdefault(figure, {})[self.today] = value

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
    assert got["tiles"] == [{**tile_json(t), **({"rose": []} if t.kind == "compass" else {}),             # (no file: no rose, no
                             **({"tendency": None} if t.title == "Atmospheric pressure" else {})} for t in sheet.tiles]   # arrow)
    assert got["as_of"] == AS_OF and got["look"] == look()
    titles = [t["title"] for t in got["tiles"]]
    assert titles[:6] == ["Temperature", "Humidity", "Atmospheric pressure", "Rain", "UV index", "AQI"]   # gauge_sheet.FIRST
    assert titles[-6:] == ["PM2.5", "PM10", "PM1", "CO₂", "VOC index", "NOx index"]       # the rest in their order: the air sensor's after
    tiles = {t["title"]: t for t in got["tiles"]}
    aqi, nox = tiles["AQI"], tiles["NOx index"]
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


async def test_the_data_carries_the_sun_sunrise_sunset_and_the_time_now_by_the_stations_clock_from_solar_py_else_six_to_six(server, station):
    """`sun`: {rise, set, now}, minutes since the station's local midnight (its zone, DST included): with the station's location today's
    sunrise and sunset from the sunrise equation (lib/solar.py: not the forecast's); 06:00 and 18:00 with no location; polar day 0 and
    1440, polar night none."""
    page = WebDashboard(server)
    before = datetime.now(TZ)
    sun = (await page.data())["sun"]                                            # (no location)
    after = datetime.now(TZ)
    assert (sun["rise"], sun["set"]) == (360, 1080) and set(sun) == {"rise", "set", "now"}
    assert web_dashboard.minutes_of(before) - 0.02 <= sun["now"] <= web_dashboard.minutes_of(after) + 0.02 or after.date() != before.date(), sun
    today = datetime.now(TZ).date()
    server.ctx.forecast = NS(days=[{"date": today, "sunrise": f"{today}T05:52", "sunset": f"{today}T20:07"}], location=None)
    sun = (await page.data())["sun"]
    assert (sun["rise"], sun["set"]) == (360, 1080), sun                       # (the forecast's times are not used)
    melbourne = (-37.81, 144.96)
    server.ctx.forecast = NS(days=[{"date": today, "sunrise": f"{today}T01:00", "sunset": f"{today}T02:00"}], location=melbourne)
    sun = (await page.data())["sun"]
    want = solar.sun_times(*melbourne, today, TZ)
    assert (sun["rise"], sun["set"]) == (want.rise, want.set) and 240 < sun["rise"] < 480 and 960 < sun["set"] < 1260, sun
    # by the station's zone, not the server's: the same instant, 12:30 in Melbourne in summer (UTC+11)
    noon = datetime(2026, 1, 15, 12, 30, 15, tzinfo=TZ)
    assert web_dashboard.sun_of(noon) == {"rise": 360, "set": 1080, "now": 750.25}
    summer = web_dashboard.sun_of(datetime(2026, 12, 21, 12, 0, tzinfo=TZ), melbourne)
    assert summer["rise"] == pytest.approx(5 * 60 + 54, abs=2) and summer["set"] == pytest.approx(20 * 60 + 41, abs=2), summer   # (DST's clock)
    winter = web_dashboard.sun_of(datetime(2026, 6, 21, 12, 0, tzinfo=TZ), melbourne)
    assert winter["rise"] == pytest.approx(7 * 60 + 35, abs=2) and winter["set"] == pytest.approx(17 * 60 + 8, abs=2), winter
    # polar day: up all day (0 to 1440), the beams with the sun's own elevation; polar night: none, the sun's el below the horizon
    day = web_dashboard.sun_of(datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc), (80.0, 0.0))
    assert (day["rise"], day["set"]) == (0, 1440) and day["el"] > 0, day
    night = web_dashboard.sun_of(datetime(2026, 12, 21, 12, 0, tzinfo=timezone.utc), (80.0, 0.0))
    assert night["rise"] is None and night["set"] is None and night["el"] < 0, night
    assert "noon_el" not in night and "noon_el" not in web_dashboard.sun_of(datetime(2026, 12, 21, 12, 0, tzinfo=timezone.utc)), "no noon elevation in polar night or with no location"
    assert day["noon_el"] > 0 and summer["noon_el"] > 60 and winter["noon_el"] < 35, (day, summer, winter)


async def test_the_sun_carries_its_real_azimuth_and_elevation_at_the_stations_location_and_clock_and_none_without_a_location(server, station):
    """`sun` gains az (degrees clockwise from north) and el (above the horizon) from the station's latitude and longitude (the forecast's
    when it has one: MCP_FORECAST_LAT and LON, else the station's own) and its zone's clock; with no location neither key (the page keeps to the
    sunrise and sunset curve)."""
    page = WebDashboard(server)
    melbourne = (-37.81, 144.96)
    noon = datetime(2026, 1, 15, 13, 15, tzinfo=TZ)           # (about solar noon in Melbourne's midsummer, daylight saving's clock)
    sun = web_dashboard.sun_of(noon, melbourne)
    assert set(sun) == {"rise", "set", "now", "az", "el", "noon_el"} and 70 <= sun["noon_el"] <= 80 and sun["now"] == 795 and sun["rise"] == pytest.approx(6 * 60 + 13, abs=2) and sun["set"] == pytest.approx(20 * 60 + 44, abs=2), sun   # (solar.py's)
    assert 70 <= sun["el"] <= 80 and (sun["az"] < 15 or sun["az"] > 345), sun            # (high in the north)
    winter = web_dashboard.sun_of(datetime(2026, 7, 15, 12, 20, tzinfo=TZ), melbourne)
    assert 25 <= winter["el"] <= 32 and (winter["az"] < 15 or winter["az"] > 345), winter
    morning = web_dashboard.sun_of(datetime(2026, 3, 20, 8, 0, tzinfo=TZ), melbourne)
    assert 60 < morning["az"] < 110 and morning["el"] > 0, morning                      # (the east)
    assert "az" not in web_dashboard.sun_of(noon) and "el" not in web_dashboard.sun_of(noon, None), "no location"
    # the page's data: the forecast's location first, else the station's, else none
    assert "az" not in (await page.data())["sun"]                                       # (neither here)
    server.ctx.forecast = NS(days=[], location=melbourne)
    assert {"az", "el"} <= set((await page.data())["sun"])
    server.ctx.forecast = NS(days=[], location=None)
    assert "az" not in (await page.data())["sun"]
    station.eco.location = (51.5, -0.12)
    got = (await page.data())["sun"]
    assert {"az", "el"} <= set(got), got
    server.ctx.forecast = NS(days=[], location=melbourne)                             # (a configured one beats the station's)
    here = (await page.data())["sun"]
    assert (here["az"], here["el"]) != (got["az"], got["el"]), (here, got)
    assert web_dashboard.location_of(server.ctx) == melbourne
    server.ctx.forecast = None
    assert web_dashboard.location_of(server.ctx) == (51.5, -0.12)
    station.eco.location = None
    assert web_dashboard.location_of(server.ctx) is None


def test_the_sun_days_file_is_what_the_solar_module_gives():
    """tests/sun_days.json (the browser test's sun: Melbourne every 10 minutes on the solstices, while up) is lib/solar.py's own."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from lib import solar
    file = json.loads((Path(__file__).parent / "sun_days.json").read_text())
    for name, (day, rows) in file["days"].items():
        start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(file["zone"]))
        want = [[m, *(round(v, 3) for v in reversed(solar.sun_position(*file["place"], start + timedelta(minutes=m))))] for m in range(0, 1440, 10)]
        assert rows == [r for r in want if r[2] > 0], name


async def test_the_data_carries_todays_highest_of_the_wind_gust_rain_rate_sun_and_uv_and_the_gust_now(server, station):
    """The dials' thin red hands: each one's reading's highest today (the station's day; Wind: its gust), its tile's Daily figure (Max;
    the Wind's Gust), which counts the reading now in, so it rises with it; null with none. Nothing read from the file for it. And the
    gust now."""
    page = WebDashboard(server)
    assert RED_HANDS == {"Wind": "Gust", "Rain rate": "Max", "Solar radiation": "Max", "UV index": "Max"}
    got = await page.data()                                                      # (the October evening: today's day rows, the sun down)
    assert got["today_highs"] == {"Wind": 28.0, "Rain rate": 8.0, "Solar radiation": 700.0, "UV index": 6.0} and got["gust"] is None
    tiles = {t["title"]: t for t in got["tiles"]}
    assert all(got["today_highs"][t] == dict(zip(tiles[t]["columns"], tiles[t]["rows"]["Daily"]))[c] for t, c in RED_HANDS.items())
    station.highs_today({"wind_gust": 37.0, "rain_rate_max": 4.2, "solar_max": 820.0, "uv_max": 2.0})
    station.reads("wind", "wind_gust", "12")                                     # (a gust now below today's highest)
    got = await page.data()
    assert got["today_highs"] == {"Wind": 37.0, "Rain rate": 4.2, "Solar radiation": 820.0, "UV index": 2.0} and got["gust"] == 12.0
    station.reads("wind", "wind_gust", "41")                                     # a gust above today's highest: it is the highest,
    station.reads("solar_and_uvi", "uvi", "3")                                   # as is the UV now
    got = await page.data()
    assert (got["today_highs"]["Wind"], got["today_highs"]["UV index"], got["gust"]) == (41.0, 3.0, 41.0)
    station.values = {k: {d: v for d, v in days.items() if d != station.today or k not in ("rain_rate_max", "solar_max")}
                      for k, days in station.values.items()}                     # no day row today for the rain rate and the sun,
    station.live = {k: v for k, v in station.live.items() if k != "rainfall"}    # nor a rain rate now: null, its tile dimmed with the
    station.eco._live = (time.time(), station.live)                             # month's (the sun's: its reading now, 0)
    got = await page.data()
    assert got["today_highs"] == {"Wind": 41.0, "Rain rate": None, "Solar radiation": 0.0, "UV index": 3.0}, got["today_highs"]
    assert today_highs([]) == {}
    station.reads("wind", "wind_gust", "9")
    assert readings_of(station.live) != readings_of({**station.live, "wind": {**station.live["wind"], "wind_gust": {"value": "10"}}})


async def test_the_data_carries_todays_wind_rose_on_the_wind_direction_tile_read_from_the_file_and_kept_a_minute(server, station,
                                                                                                                  monkeypatch, caplog):
    """The Wind direction tile's `rose`: the share of today's readings of the wind's direction (the station's day, in its zone; the
    file's: directions_on) from each of the 16 compass points, N first; [] with no file, no reading today, or the file not read. Read at
    most every ROSE_SECONDS, else kept; no other tile has one, and the sheet's tiles are as ever."""
    page = WebDashboard(server)
    compass = lambda got: next(t for t in got["tiles"] if t["kind"] == "compass")
    got = await page.data()
    assert compass(got)["rose"] == [] and all("rose" not in t for t in got["tiles"] if t["kind"] != "compass")   # (no file)
    assert [{k: v for k, v in t.items() if k not in ("rose", "tendency")} for t in got["tiles"]] == [tile_json(t) for t in gauge_sheet.sheet(
        station.current(gauge_sheet.call(), {"fetch": False})).tiles]
    minutes = [10, 4, 2, 0, 30, 12, 6, 0, 0, 0, 3, 0, 0, 0, 5, 8]
    station.held(minutes)
    assert compass(await page.data())["rose"] == [] and web_dashboard.ROSE_SECONDS == 60   # (kept: read under a minute ago)
    page._rose = None
    got = compass(await page.data())["rose"]
    assert len(got) == 16 and got == [round(n / 80, 4) for n in minutes] and got[4] == 0.375, got
    station.held([0] * 16)                                                   # read again a minute on: none today
    page._rose = (page._rose[0] - 61, *page._rose[1:])
    assert compass(await page.data())["rose"] == []

    async def broken(*a):
        raise OSError("disk")
    station.eco.storage = NS(read=broken)
    monkeypatch.setattr(web_dashboard, "ROSE_SECONDS", 0)
    assert compass(await page.data())["rose"] == [] and "couldn't read today's wind directions: OSError: disk" in caplog.text


async def test_todays_wind_directions_are_counted_from_the_5_minute_readings_and_the_minutes_they_dont_cover_calm_left_out(tmp_path):
    """directions_on: minutes from each of the 16 compass points from start to end: a 5-minute reading counts 5, a minute 1 where no
    5-minute reading is within 300 s of it (never counted twice); a calm one (its speed 0) left out, one with no speed counted; nothing
    older or newer, nor a 30-minute reading; all 0 with none, or no wind sensor."""
    from lib.storage import Storage
    storage = Storage(tmp_path / "s.sqlite", TZ)
    mac, now = "AA:BB", 1_794_279_900
    start = now - 6 * 3600
    assert await storage.read(directions_on, mac, start, now) == [0] * 16
    fives = {now - 3600: ("90", "5"), now - 3300: ("95", "0"), now - 3000: ("180", "3"), now - 7 * 3600: ("0", "4")}   # (calm; too old)
    wind = lambda rows: {"wind": {"wind_direction": {"unit": "º", "list": {str(t): d for t, (d, _) in rows.items()}},
                                  "wind_speed": {"unit": "km/h", "list": {str(t): v for t, (_, v) in rows.items()}}}}
    with storage.writer.transaction() as w:
        w.put_ecowitt(mac, "5min", ["wind"], wind(fives), now - 8 * 3600, now - 2000, now=now)
        w.put_ecowitt(mac, "30min", ["wind"], wind({now - 1800: ("270", "4")}), now - 1800, now - 1800, now=now)
        w.put_minutes(mac, wind({now - 3060: ("200", "2"),                                       # (within the 5-minute readings: theirs)
                                 now - 600: ("350", "6"), now - 540: ("5", "6"), now - 480: ("22", "1"), now - 420: ("300", "0"),
                                 now + 120: ("90", "9")}), now)                                     # (newer than `end`)
        w.put_minutes(mac, {"wind": {"wind_direction": {"unit": "º", "list": {str(now - 360): "135"}}}}, now)   # (no speed: counted)
    got = await storage.read(directions_on, mac, start, now)
    want = [0] * 16
    want[4] += 5; want[8] += 5; want[0] += 2; want[1] += 1; want[6] += 1   # (E, S; N twice, NNE, SE)
    assert got == want, got
    storage.close()


async def test_the_data_carries_the_pressures_3_hour_tendency_on_its_tile_only_read_from_the_file_and_kept_a_minute(
        server, station, monkeypatch, caplog):
    """The Atmospheric pressure tile's `tendency`: its WMO 3-hour tendency, {"change": hPa, "way", "class"} (tendency_of: the file's newest
    reading less the one nearest 3 hours before it, in hPa, from the past 4 hours' 5-minute readings and minutes: pressure_on); null
    with no file, too little history, or the file not read. Read at most every TENDENCY_SECONDS, else kept; no other tile has one."""
    page = WebDashboard(server)
    pressure = lambda got: next(t for t in got["tiles"] if t["title"] == "Atmospheric pressure")
    got = await page.data()
    assert pressure(got)["tendency"] is None and [t["title"] for t in got["tiles"] if "tendency" in t] == ["Atmospheric pressure"]   # (no file)
    assert pressure(got)["decimals"] == 0 and next(g for g in web_dashboard.GAUGES if g.title == "Atmospheric pressure").decimals == 0
    station.held(pressure=pressure_hour(-2.4))
    assert pressure(await page.data())["tendency"] is None and web_dashboard.TENDENCY_SECONDS == 60   # (kept: read under a minute ago)
    page._tendency = None
    assert pressure(await page.data())["tendency"] == {"change": -2.4, "way": "down", "class": "moderate"}
    station.held(pressure=pressure_hour(0.8))                                # read again a minute on: risen
    page._tendency = (page._tendency[0] - 61, page._tendency[1])
    assert pressure(await page.data())["tendency"] == {"change": 0.8, "way": "up", "class": "slow"}
    station.held(pressure=[(m, 1012.0) for m in range(120, -1, -5)])         # only 2 hours held: none
    page._tendency = None
    assert pressure(await page.data())["tendency"] is None

    async def broken(*a):
        raise OSError("disk")
    station.eco.storage = NS(read=broken)
    monkeypatch.setattr(web_dashboard, "TENDENCY_SECONDS", 0)
    assert pressure(await page.data())["tendency"] is None and "couldn't read the past hours' pressure: OSError: disk" in caplog.text


def test_the_pressures_3_hour_tendency_classes_by_the_size_of_its_change_and_reads_the_reading_nearest_3_hours_back():
    """tendency_of: [(epoch, hPa)] oldest first: the newest less the reading nearest 180 minutes before it (150 to 210 minutes before it;
    none there: None), to a tenth; by its size: under 0.1 "none" (steady), 0.1 to 1.5 "slow", 1.6 to 3.5 "moderate", 3.6 to 6.0 "quick",
    over 6.0 "rapid" (the edges inclusive, as listed), "up" or "down" by its sign."""
    t = 1_794_200_000
    hour = lambda *pairs: [(t - m * 60, v) for m, v in pairs]                 # ((minutes before the newest, hPa), ...), oldest first
    for size, cls in ((0.05, "none"), (0.0, "none"), (0.1, "slow"), (1.5, "slow"), (1.6, "moderate"), (3.5, "moderate"), (3.6, "quick"),
                      (6.0, "quick"), (6.1, "rapid"), (12.0, "rapid")):
        assert web_dashboard.tendency_of(hour((180, 1010.0), (0, 1010.0 + size))) == {
            "change": 0.0 if cls == "none" else size, "way": "steady" if cls == "none" else "up", "class": cls}, (size, cls)
        assert web_dashboard.tendency_of(hour((180, 1010.0 + size), (0, 1010.0))) == {
            "change": 0.0 if cls == "none" else -size, "way": "steady" if cls == "none" else "down", "class": cls}, (size, cls)
    # (a change between the tenths: taken to a tenth first: 1.55 is 1.6, moderate)
    assert web_dashboard.tendency_of(hour((180, 1000.0), (0, 1001.55)))["class"] in ("slow", "moderate")
    assert web_dashboard.tendency_of(hour((180, 1000.0), (0, 1001.549)))["class"] == "slow"
    # the reading nearest 3 hours before the newest: 183 minutes (3 off) rather than 175 (5 off), or 230 (out of reach) and 190 (10 off)
    assert web_dashboard.tendency_of(hour((230, 1000.0), (183, 1011.0), (175, 1013.0), (0, 1012.0))) == {"change": 1.0, "way": "up", "class": "slow"}
    assert web_dashboard.tendency_of(hour((230, 1000.0), (190, 1013.0), (0, 1012.0))) == {"change": -1.0, "way": "down", "class": "slow"}
    assert web_dashboard.tendency_of(hour((180, 1012.0), *((m, 1013.0) for m in range(179, -1, -1)))) == {"change": 1.0, "way": "up", "class": "slow"}
    # the window 150 to 210 minutes, edges in; too little history: None
    assert web_dashboard.tendency_of(hour((150, 1012.0), (0, 1010.0))) == {"change": -2.0, "way": "down", "class": "moderate"}
    assert web_dashboard.tendency_of(hour((210, 1012.0), (0, 1010.0))) == {"change": -2.0, "way": "down", "class": "moderate"}
    assert web_dashboard.tendency_of(hour((149, 1012.0), (0, 1010.0))) is None
    assert web_dashboard.tendency_of(hour((211, 1012.0), (0, 1010.0))) is None
    assert web_dashboard.tendency_of(hour((120, 1012.0), (60, 1011.0), (0, 1010.0))) is None            # (only 2 hours)
    assert web_dashboard.tendency_of(hour((240, 1012.0), (212, 1012.0), (148, 1011.0), (0, 1010.0))) is None
    assert web_dashboard.tendency_of(hour((0, 1012.0))) is None and web_dashboard.tendency_of([]) is None
    assert web_dashboard.TENDENCY_READ == 240 * 60 and web_dashboard.TENDENCY_AGO == (150 * 60, 180 * 60, 210 * 60)


@functools.cache
def pressure_hour(change: float) -> list[tuple[float, float]]:
    """The past 4 hours' pressure, a reading every 5 minutes ([(minutes before now, hPa)], oldest first), changing `change` hPa
    over the last 3 hours, steadily."""
    return [(m, round(1013.2 - change * m / 180, 3)) for m in range(240, -1, -5)]


async def test_todays_pressure_is_read_from_the_5_minute_readings_and_the_minutes_they_dont_cover(tmp_path):
    """pressure_on: the relative pressure from start to end, [(epoch, hPa)] oldest first: the 5-minute readings, and the minutes where
    no 5-minute reading is within 300 s (never counted twice); nothing older or newer, nor a 30-minute reading; [] with none."""
    from lib.storage import Storage
    storage = Storage(tmp_path / "s.sqlite", TZ)
    mac, now = "AA:BB", 1_794_279_900
    start = now - 6 * 3600
    assert await storage.read(pressure_on, mac, start, now) == []
    hpa = lambda rows: {"pressure": {"relative": {"unit": "hPa", "list": {str(t): v for t, v in rows.items()}},
                                     "absolute": {"unit": "hPa", "list": {str(t): "990.0" for t in rows}}}}
    with storage.writer.transaction() as w:
        w.put_ecowitt(mac, "5min", ["pressure"], hpa({now - 3600: "1012.3", now - 3300: "1012.5", now - 7 * 3600: "1000.0"}),   # (too old)
                      now - 8 * 3600, now - 3000, now=now)
        w.put_ecowitt(mac, "30min", ["pressure"], hpa({now - 1800: "1020.0"}), now - 1800, now - 1800, now=now)
        w.put_minutes(mac, hpa({now - 3240: "1050.0",                                          # (within the 5-minute readings: theirs)
                                now - 600: "1012.9", now - 540: "1013.0", now + 120: "1013.4"}), now)   # (newer than `end`)
    got = await storage.read(pressure_on, mac, start, now)
    assert got == [(now - 3600, 1012.3), (now - 3300, 1012.5), (now - 600, 1012.9), (now - 540, 1013.0)], got
    storage.close()


def hours_at(rate: float, hours: float, ago: float = 0.0) -> list[tuple[float, float]]:
    """A steady rain rate in the 5-minute readings: [(epoch, mm/h)] each 300 s, ending `ago` s before the moment the tests call `NOW`."""
    end = NOW - ago
    return [(end - 300 * i, rate) for i in range(int(hours * 12) - 1, -1, -1)]


NOW = 1_794_279_900.0


def test_the_flash_floods_depth_is_a_leaky_bucket_over_the_past_hours_rain_runoff_half_the_rate_draining_with_a_30_minute_time_constant():
    """flood_of: depth' = 0.5 R - depth / tau (tau 30 min, R mm/h), full at 15 mm, stepped exactly over the 5-minute rows and run on to
    now with the live rate; 0 to 1; None with no history."""
    flood = web_dashboard.flood_of
    assert (web_dashboard.FLOOD_TAU, web_dashboard.FLOOD_RUNOFF, web_dashboard.FLOOD_FULL) == (1800.0, 0.5, 15.0)
    assert flood([], NOW, 5.0) is None                                              # (no history: none, a live reading or not)
    assert flood(hours_at(0.0, 3), NOW, 0.0) == 0.0
    assert flood(hours_at(60.0, 3), NOW, 60.0) >= 0.95                              # (a sustained 60 mm/h: full)
    assert flood(hours_at(30.0, 3), NOW, 30.0) == pytest.approx(0.5, abs=0.05)      # (30 mm/h: about half)
    assert flood(hours_at(120.0, 3), NOW, 120.0) == 1.0                             # (clamped)
    assert flood(hours_at(10.0, 6), NOW, 10.0) == pytest.approx(0.167, abs=0.02)    # (light: a sixth)
    # 60 mm/h for an hour, then none: the peak 1 - exp(-2) = 0.865, drained to 1/e of it in 30 minutes, to 5 % in 90
    peak = flood(hours_at(60.0, 1), NOW, 0.0)
    assert peak == pytest.approx(1 - math.exp(-2), abs=0.01)
    assert flood(hours_at(60.0, 1), NOW + 1800, 0.0) == pytest.approx(peak * math.exp(-1), abs=0.01)    # about 0.37 of the peak
    assert flood(hours_at(60.0, 1), NOW + 5400, 0.0) == pytest.approx(peak * math.exp(-3), abs=0.01)    # about 0.05 of it
    # a 5-minute burst of 100 mm/h: 25 * (1 - exp(-1/6)) mm = 0.26 of full
    assert flood([(NOW, 100.0)], NOW, 100.0) == pytest.approx(25 * (1 - math.exp(-1 / 6)) / 15, abs=0.01)
    assert 0.2 <= flood([(NOW, 100.0)], NOW, 0.0) <= 0.3
    # the newest row run on to now with the live rate: rain still falling keeps it filling, none lets it drain; a gap in the rows is dry
    rain, rows = flood(hours_at(60.0, 1), NOW + 600, 60.0), hours_at(60.0, 1)
    assert rain > peak > flood(rows, NOW + 600, 0.0) and flood(rows, NOW + 600, None) == flood(rows, NOW + 600, 0.0)
    assert flood([(NOW - 3600, 60.0), (NOW, 0.0)], NOW, 0.0) < 0.15                  # (the hour-old burst, mostly drained)
    assert flood(hours_at(60.0, 1), NOW + 600, 0.0) < flood(hours_at(60.0, 1), NOW + 300, 0.0) < peak


async def test_the_data_carries_the_flash_floods_depth_read_from_the_file_and_kept_a_minute_and_the_air_sensors_pm25(server, station, monkeypatch, caplog):
    """`flood`: {"depth": 0 to 1} from the past six hours' 5-minute rain rates (flood_of; the live rate runs it on to now), read from the
    file at most every FLOOD_SECONDS; null with no history or no file or a failed read. `pm25`: the air sensor's PM2.5 now (µg/m³), null
    with none."""
    page = WebDashboard(server)
    got = await page.data()
    assert got["flood"] is None and got["pm25"] == station.latest["pm2_5"]["value"], (got["flood"], got["pm25"])   # (no file)
    station.air_reads("pm2_5", 55.4)
    assert (await page.data())["pm25"] == 55.4
    station.air_reads("pm2_5", None)
    assert (await page.data())["pm25"] is None
    rate = lambda mm: [(m, mm) for m in range(360 - 5, -1, -5)]                    # (minutes before now, mm/h: every 5 minutes for 6 hours)
    station.held(rain=rate(60.0))
    station.reads("rainfall", "rain_rate", "60")
    assert (await page.data())["flood"] is None and web_dashboard.FLOOD_SECONDS == 60   # (kept: read under a minute ago, no history then)
    page._flood = None
    depth = (await page.data())["flood"]["depth"]
    assert depth >= 0.95, depth
    station.held(rain=rate(30.0))                                                      # (the file changes: not read again for a minute)
    assert (await page.data())["flood"]["depth"] == depth
    page._flood = (page._flood[0] - 61, page._flood[1])
    assert (await page.data())["flood"]["depth"] == pytest.approx(0.5, abs=0.05)
    station.reads("rainfall", "rain_rate", "0")                                        # (the rain stops: drains from the rows' end)
    assert (await page.data())["flood"]["depth"] <= 0.5
    station.held(rain=[])
    page._flood = None
    assert (await page.data())["flood"] is None

    async def broken(*a):
        raise OSError("disk")
    station.eco.storage = NS(read=broken)
    monkeypatch.setattr(web_dashboard, "FLOOD_SECONDS", 0)
    assert (await page.data())["flood"] is None and "couldn't read the past hours' rain rates: OSError: disk" in caplog.text


async def test_the_5_minute_rain_rates_are_read_from_the_rain_gauge_else_the_piezo_gauge_and_nothing_older_or_newer(tmp_path):
    """rain_rates_on: [(epoch, mm/h)] oldest first, the 5-minute readings of the rain gauge's rate, else the piezo gauge's where that is the one
    with any; [] with none."""
    from lib.storage import Storage
    storage = Storage(tmp_path / "s.sqlite", TZ)
    mac, now = "AA:BB", 1_794_279_900
    start = now - 6 * 3600
    assert await storage.read(rain_rates_on, mac, start, now) == []
    rates = lambda group, rows: {group: {"rain_rate": {"unit": "mm/h", "list": {str(t): v for t, v in rows.items()}}}}
    with storage.writer.transaction() as w:
        w.put_ecowitt(mac, "5min", ["rainfall_piezo"], rates("rainfall_piezo", {now - 600: "9.0"}), now - 7 * 3600, now - 300, now=now)
    assert await storage.read(rain_rates_on, mac, start, now) == [(now - 600, 9.0)]            # (the piezo's: the only one)
    with storage.writer.transaction() as w:
        w.put_ecowitt(mac, "5min", ["rainfall"], rates("rainfall", {now - 3600: "4.5", now - 300: "6.0", now - 7 * 3600: "1.0", now + 600: "2.0"}),
                      now - 8 * 3600, now + 600, now=now)
    assert await storage.read(rain_rates_on, mac, start, now) == [(now - 3600, 4.5), (now - 300, 6.0)]   # (the rain gauge's: not older or newer)
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
    # (the sun block carries the clock, which ticks between the two reads: left out)
    assert {k: v for k, v in off.items() if k not in ("tiles", "sun")} == {k: v for k, v in on.items() if k not in ("tiles", "sun")}
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
    """MCP_DASHBOARD_ANIMATIONS: the page served with data-animations="on" on its <html> (the default), "off" or "low";
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


def rgb(colour: str) -> str:
    return "rgb({}, {}, {})".format(*(int(colour[i:i + 2], 16) for i in (1, 3, 5)))


def one_end_to_read(compass: dict):
    """The compass's needle (compassLook's, in its own px): its head a slim blade to a sharp tip in the red hands' red (not the rose's
    blue), solid; its tail a short, dim taper of another colour; never two ends alike."""
    head, tail = compass["ends"]
    assert head["colour"] == rgb(look()["high_hand"]) != rgb(look()["needle"]) and tail["colour"] != head["colour"], compass
    assert head["opacity"] == 1 and tail["opacity"] <= 0.6, compass
    assert compass["hubColour"] == head["colour"], compass                                  # (its hub ringed in the same red)
    assert head["length"] > 4 * tail["length"] and head["area"] > 3 * tail["area"] and head["width"] <= 12, compass   # (slim)


ROSE = [10, 4, 2, 0, 30, 12, 6, 0, 0, 0, 3, 0, 0, 0, 5, 8]   # today's minutes from each compass point (N first): mostly from the east
ROSE_LATER = [0, 0, 0, 0, 5, 0, 0, 0, 0, 0, 10, 0, 40, 20, 0, 0]   # ... later: from the west


def compass_as_a_dial(c: dict):
    """The compass (compassLook's) the size of a dial, lined up with them: its ring a dial's (centre and radius in the tile's own px, its
    line as thin), as wide drawn; the layers from the bottom: the rose, the reading, the face (its ring, ticks and N, E, S and W), the
    needle, its hub; the reading inside the ring, in its lower half, centred."""
    assert c["circle"] == c["dialCircle"] == [240, 203, 138] and c["ringStroke"] == pytest.approx(3 * 100 / 72), c
    if not c["zoomed"]:                                                          # (in the grid: as large as its neighbours' drawn)
        assert c["ring"]["width"] == pytest.approx(c["dialRing"]["width"], abs=0.5), c
    assert c["layers"] == ["rose", "reading", "face", "needle", "hub"], c
    reading = next(r for r in c["texts"] if r["width"] and abs((r["left"] + r["right"]) / 2 - (c["ring"]["left"] + c["ring"]["right"]) / 2) < 1
                   and r["top"] > (c["ring"]["top"] + c["ring"]["bottom"]) / 2)
    assert c["readingBelow"] > 30 and c["readingAt"] + 20 < 138 and c["ring"]["left"] < reading["left"] and reading["right"] < c["ring"]["right"], c
    assert reading["bottom"] < c["ring"]["bottom"] - c["ring"]["height"] * 0.1, (reading, c)


def rose_drawn(c: dict, minutes: list[int]):
    """Today's rose as drawn (compassLook's petals): a petal for each compass point with any share, in the dials' needles' blue, see-through,
    its reach from the centre in proportion to its share, the longest 80% of the radius."""
    total, most = sum(minutes), max(minutes)
    want = {POINTS[i]: n / total for i, n in enumerate(minutes) if n}
    petals = c["petals"]
    assert {p["point"]: p["share"] for p in petals} == pytest.approx(want, abs=1e-4), petals
    for p in petals:
        assert p["reach"] == pytest.approx(138 * 0.8 * minutes[POINTS.index(p["point"])] / most, abs=0.05), p
        fills = {f["id"]: f for f in c["fades"]}
        assert p["fill"].startswith("url(#") and p["stroke"].startswith("url(#") and p["fill"] != p["stroke"], p   # (no flat colour)
        for ref, top in ((p["fill"], 0.32), (p["stroke"], 0.55)):
            g = fills[ref[5:-1]]
            ops = [o for _, _, o in g["stops"]]
            assert g["units"] == "userSpaceOnUse" and (g["cx"], g["cy"]) == tuple(c["ringCentre"]) and g["r"] == pytest.approx(138 * 0.8), g
            assert ops[0] == 0 and ops[-1] == top and ops == sorted(ops) and len(set(ops)) == len(ops), g      # (clear at the hub, rising)
            assert [s[0] for s in g["stops"]] == sorted(s[0] for s in g["stops"]) and g["stops"][0][0] == 0 and g["stops"][-1][0] == 1, g
            assert all(s[1] == rgb(look()["needle"]) for s in g["stops"]), g                                    # (the blue unchanged)
    assert len(c["fades"]) == 2 and not c["patterns"], c["fades"]        # (one shared pair, ids unique; smooth: no pattern or dither)


@needs_browser
async def test_the_page_fits_its_tiles_to_the_window_at_each_size_with_no_overflow_and_no_console_error(server, station, tmp_path,
                                                                                                         monkeypatch):
    shots = Path(os.getenv("ECOBOT_SCREENSHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    port = free_port()
    url = f"http://127.0.0.1:{port}/dashboard"
    monkeypatch.setattr(web_dashboard, "ROSE_SECONDS", 0)                  # (today's rose read again with each data: it changes below)
    station.reads("wind", "wind_gust", "12")
    station.highs_today({"wind_gust": 37.0, "rain_rate_max": 4.2, "solar_max": 820.0, "uv_max": 6.5})   # (today's highest: above the
    station.held(ROSE, pressure_hour(2.0))                               # readings now, all but 0; today's rose, and the pressure
    monkeypatch.setattr(web_dashboard, "TENDENCY_SECONDS", 0)              # risen over the past 3 hours; read again: it falls below)
    wind = next(t for t in gauge_sheet.sheet(station.current(gauge_sheet.call(), {"fetch": False})).tiles if t.title == "Wind")
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
        station.highs_today({"wind_gust": 60.0, "uv_max": 9.0, "rain_rate_max": 0.0})   # (and today's highest gust and UV rise, rain
                                                                                 # rate's a dry day's 0, and today's
        station.held(ROSE_LATER, pressure_hour(-2.5))                             # rose turns to the west; the pressure falls)
        await server.publish_changes()
        live.stdin.write(b"go\n")
        await live.stdin.drain()
        out, err = await asyncio.wait_for(live.communicate(), 60)
        assert live.returncode == 0, err.decode()
        after = json.loads(out)
    sheet = gauge_sheet.sheet(Station().current(gauge_sheet.call(), {"fetch": False})).tiles
    n, kinds = len(sheet), [t.kind for t in sheet]
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
        # the wind's direction, a compass: N, E, S and W named (all four alike: no red N), its needle's head turned to where the wind comes
        # from (99°, east), its reading "99°" (the degrees only; its label keeps the compass point); no table, the same size as its neighbours
        c, rects = got["compass"], got["rects"]
        assert (c["label"], c["reading"], c["turn"], c["faded"]) == ("Wind direction 99° E", "99°", "rotate(99.00 240 203)", False), c
        assert c["points"] == ["N", "E", "S", "W"] and c["table"] == 0, c
        assert c["pointColours"] == [look()["soft"]] * 4 and "compass_north" not in look(), c
        assert any(near(c["box"], r, 0.5) for r in rects) and len({(round(r["width"]), round(r["height"])) for r in rects}) == 1, c
        compass_as_a_dial(c)
        ring, head = c["ring"], c["head"]
        assert (head["left"] + head["right"]) / 2 > (ring["left"] + ring["right"]) / 2 + ring["width"] * 0.1 and abs((head["top"] + head["bottom"]) / 2 - (ring["top"] + ring["bottom"]) / 2) < ring["height"] * 0.1, c
        # ... the end to read unmistakable: the head a slim sharp blade in the red hands' red, the tail a short, dim taper of another
        one_end_to_read(c)
        # ... today's rose under it all, a petal per compass point blown from, as long as its share (the longest 80% of the radius)
        rose_drawn(c, ROSE)
        assert all(c["box"]["left"] <= r["left"] and r["right"] <= c["box"]["right"] and r["bottom"] <= c["box"]["top"] + c["box"]["width"] * TABLE_TOP / look()["cell"][0]
                   for r in c["texts"]), c                                         # (all of it above where a table would start)
        # zoomed: the compass and its reading fill the zoomed place, as any tile's title and gauge
        z = got["compassZoomed"]
        assert z["zoomed"] and z["shown"] and near(z["box"], zoomed_rect(width, height), 1.5), (name, z)
        for r in [z["title"], z["ring"], *z["texts"]]:
            assert z["box"]["left"] - 1 <= r["left"] and r["right"] <= z["box"]["right"] + 1 and z["box"]["top"] - 1 <= r["top"] and r["bottom"] <= z["box"]["bottom"] + 1, (r, z)
        assert z["ring"]["width"] > z["box"]["width"] * 0.45, z
        one_end_to_read(z)                                                        # (zoomed too)
        compass_as_a_dial(z)
        rose_drawn(z, ROSE)
        assert (shots / f"dashboard-{name}-compass.png").exists()
        # the dials with a red mark at today's highest (Wind's gust, Rain rate's, Solar radiation's, UV's), over the dial and the
        # needle, under the hub; none on any other tile; the Wind's table: its highest speed, and its highest gust beside it
        hands = got["hands"]
        assert {t for t, h in hands.items() if h} == set(RED_HANDS), hands
        for title, value in {"Wind": 37.0, "Rain rate": 4.2, "Solar radiation": 820.0, "UV index": 6.5}.items():
            h, tile = hands[title], next(t for t in sheet if t.title == title)
            assert h["turn"] == hand_turn(tile, value) and h["shown"], (name, title, h)
            assert h["order"][0] < h["order"][1] < h["order"][2], (name, title, h)
            limit_mark(h, name, title)   # (every one, the Wind's gust too: no hand, a limit mark (rim triangle) pointing in)
        # ... zoomed, the UV's the same, scaled with the tile
        z, grid = got["uvZoomed"], hands["UV index"]
        assert z["zoomed"] and z["turn"] == grid["turn"] and z["limit"]["tileWidth"] > grid["limit"]["tileWidth"], (name, z)
        limit_mark(z, name, "UV index (zoomed)")
        assert (z["limit"]["drawnWidth"] / z["limit"]["tileWidth"] == pytest.approx(grid["limit"]["drawnWidth"] / grid["limit"]["tileWidth"], rel=0.05)), (z, grid)
        assert (shots / f"dashboard-{name}-uv.png").exists()
        assert hands["Wind"]["heads"] == ["▲ Max", "» Gust"] and wind.columns == ("Max", "Gust"), hands["Wind"]
        # ... its heads: Max grey, Gust in the red hand's very red (the hand and its column go together); every other tile's Max red
        assert hands["Wind"]["headColours"] == [rgb(look()["needle"]), rgb(look()["high_hand"])], hands["Wind"]
        others = got["headColours"]
        assert others["Wind"] == [["▲ Max", rgb(look()["needle"])], ["» Gust", rgb(look()["high_hand"])]], others
        assert any(t != "Wind" and heads for t, heads in others.items()), others
        assert all(c == rgb(CHIPS[n.split()[-1]]) for t, heads in others.items() if t != "Wind" for n, c in heads), others
        assert all(c == rgb(MAX_CHIP) for t, heads in others.items() if t != "Wind" for n, c in heads if n.endswith("Max")), others
        assert hands["Wind"]["daily"] == [f"{v:.0f}" for v in wind.rows["Daily"]] and wind.rows["Daily"][1] >= 12, (hands["Wind"], wind)
        # every round dial's hub small (about half the old 25), its outline as thin, its needle pivoting on its centre; the compass's
        # smaller still; the pressure in whole hPa, its reading and its table; its arrow after its reading (risen 2 hPa over the past 3 hours: moderate, up, blue), no other's
        dials = got["dials"]
        assert {d["kind"] for d in dials.values()} == {"dial", "compass"} and len(dials) >= 6, dials
        for title, d in dials.items():
            assert d["hub"][:2] == [240, 203] and d["pivot"] == pytest.approx([240, 203], abs=0.01), (name, title, d)
            if d["kind"] == "dial":
                assert d["hub"][2] == 13 and d["hub"][3] == pytest.approx(1.5 * 100 / 72), (name, title, d)
            else:
                assert d["hub"][2] <= 13, (name, title, d)
            assert (d["arrow"] is not None) == (title == "Atmospheric pressure"), (name, title, d)
        p = dials["Atmospheric pressure"]
        assert re.fullmatch(r"\d{3,4}", p["reading"]) and p["figures"] and all(re.fullmatch(r"\d{3,4}|-|", f) for f in p["figures"]), p
        arrow_drawn(p, "up", name)
        z = got["pressureZoomed"]                                             # ... zoomed: the same, scaled with the tile
        assert z["zoomed"] and z["arrow"]["d"] == p["arrow"]["d"] and z["hub"] == p["hub"], (name, z)
        arrow_drawn(z, "up", f"{name}, zoomed")
        drawn = lambda d: (d["arrow"]["rect"][3] - d["arrow"]["rect"][1]) / d["arrow"]["tileWidth"]
        assert drawn(z) == pytest.approx(drawn(p), rel=0.05) and z["arrow"]["tileWidth"] > p["arrow"]["tileWidth"], (name, z)
        assert (shots / f"dashboard-{name}-pressure.png").exists()
        # ... in inHg (zoomed, its switch pressed): its reading in inHg, wider: the arrow the same way, kept just after it
        h = got["pressureInHg"]
        assert re.fullmatch(r"\d\d\.\d\d", h["reading"]) and h["arrow"]["box"][0] > z["arrow"]["box"][0] + 5, (name, h)
        arrow_drawn(h, "up", f"{name}, inHg")
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
    assert (c["label"], c["reading"], c["turn"]) == ("Wind direction 300° WNW", "300°", "rotate(300.00 240 203)"), c
    rose_drawn(c, ROSE_LATER)                                                     # (today's rose redrawn in place with it)
    arrow_drawn(before["pressure"], "up", "live, before")                         # ... and the pressure's arrow turned down, in place
    arrow_drawn(after["pressure"], "down", "live, after")
    assert after["pressure"]["same"], after["pressure"]
    turns = c["turns"]
    assert all(t <= 99.01 or t >= 299.99 for t in turns) and any(t < 30 for t in turns) and any(t > 330 for t in turns), turns
    # the Wind's red hand moved smoothly, frame by frame, from today's highest gust before (37) to the new one (60)
    windy = next(t for t in sheet if t.title == "Wind")
    h, frames = after["hands"]["Wind"], after["hands"]["frames"]
    assert h["turn"] == hand_turn(windy, 60.0) and frames[0] == turn_of(windy, 37.0) and frames[-1] == turn_of(windy, 60.0), h
    between = [f for f in frames if turn_of(windy, 37.0) < f < turn_of(windy, 60.0)]
    assert len(set(between)) >= 8 and all(a <= b for a, b in zip(frames, frames[1:])), frames
    # ... and the UV's limit mark, eased the same way from today's highest before (6.5) to the new one (9), still a limit mark
    uv = next(t for t in sheet if t.title == "UV index")
    h, frames = after["hands"]["UV index"], after["hands"]["limitFrames"]
    assert h["kind"] == "limit" and h["turn"] == hand_turn(uv, 9.0) and frames[0] == turn_of(uv, 6.5) and frames[-1] == turn_of(uv, 9.0), (h, frames)
    between = [f for f in frames if turn_of(uv, 6.5) < f < turn_of(uv, 9.0)]
    assert len(set(between)) >= 8 and all(a <= b for a, b in zip(frames, frames[1:])), frames
    # ... and Rain rate's hidden while today's highest is 0 (a dry day: nothing to mark)
    assert after["hands"]["Rain rate"]["kind"] == "limit" and not after["hands"]["Rain rate"]["shown"], after["hands"]["Rain rate"]


def arrow_drawn(p: dict, way: str | None, name: str, cls: str = "moderate"):
    """The pressure's arrow after its reading (dialsLook's), its way over the past 3 hours and its class (`cls`: slow, a short arrow, 0.5
    of the normal length; moderate, the normal; quick, 1.35 times; rapid, the normal with a second chevron behind the first): none (`way` None: hidden), or a fine,
    instrument-style arrow: one thin straight shaft and a small, narrow, sharp open head (two short strokes from its tip, back and
    out to either side: a slim chevron, narrower than 45 degrees), no fill, no outline; up and blue (the needle's) when rising, down
    and red (the red hands') when falling, none when steady (`way` None), about the digits' height; just after the reading (its
    head's back 2 px from the last digit's ink), vertically centred on its digits' ink; clear of the hub, the unit, the tick numbers,
    the title and the table (and, in hPa, inside the dial's outline; inHg's wider reading reaches the ring and the arrow is drawn over it)."""
    a = p["arrow"]
    if way is None:
        assert a is not None and not a["shown"] and a["way"] == "", (name, a)
        return
    assert a["shown"] and a["way"] == way and a["cls"] == cls and a["fill"] == "none" and "edge" not in a, (name, a)
    assert a["stroke"] == rgb({"up": look()["needle"], "down": look()["high_hand"]}[way]), (name, a)
    assert look()["rising"] == look()["needle"] == "#3E86C0" and look()["falling"] == look()["high_hand"] == "#FF3B30", look()
    assert 1 <= a["width"] <= 2 and a["width"] < a["hubWidth"], (name, a)                                     # (fine: about 1.2)
    chevrons = 2 if cls == "rapid" else 1
    assert re.fullmatch(r"M[\d.]+,[\d.]+ L[\d.]+,[\d.]+" + r" M[\d.]+,[\d.]+ L[\d.]+,[\d.]+ L[\d.]+,[\d.]+" * chevrons, a["d"]), (name, a["d"])
    x0, y0, x1, y1, hx0, hy0, tx, ty, hx1, hy1, *second = map(float, re.findall(r"[\d.]+", a["d"]))   # (the shaft, then the head, then a second)
    if second:                                                                   # rapid: a second chevron behind the first, the same size
        sx0, sy0, sx, sy, sx1, sy1 = second
        assert sx == tx and sx1 - sx == pytest.approx(hx1 - tx) and sx - sx0 == pytest.approx(tx - hx0) and sy1 == sy0, (name, a["d"])
        assert abs(sy - ty) >= 4 and (sy - ty) * (1 if way == "up" else -1) > 0 and abs(sy0 - hy0) == pytest.approx(abs(sy - ty)), (name, a["d"])
        assert abs(y1 - y0) > abs(sy - ty) + abs(hy0 - ty), (name, a["d"])   # (it is behind the first, on the shaft)
    assert (tx, ty) == (x1, y1), (name, a["d"])                                  # the head at the shaft's tip
    k, back = abs(hx0 - tx), abs(hy0 - ty)
    assert 2 <= k <= 5 and 1.6 * k <= back <= 3 * k, (name, a["d"])              # a slim chevron: longer than wide (under 32 degrees a side)
    assert x0 == x1 and hy0 == hy1 and (hx1 - tx) == pytest.approx(tx - hx0), (name, a["d"])
    assert hy0 == pytest.approx(ty + back if way == "up" else ty - back), (name, a["d"])
    ink_top, ink_bottom = a["ink"]
    tall = ink_bottom - ink_top                                                  # (the digits' height: about 38 px at 50 px)
    scale = {"slow": 0.5, "moderate": 1, "quick": 1.35, "rapid": 1}[cls]         # (the normal arrow: 0.8 of the digits' height)
    assert (y1 < y0) == (way == "up") and 0.7 * tall * scale <= abs(y1 - y0) <= tall * scale, (name, a["d"], tall)
    bx0, by0, bx1, by1 = a["box"]
    assert bx0 - a["inkRight"] == pytest.approx(2, abs=0.5), (name, a["box"], a["inkRight"])     # a 2 px gap after the last digit's ink
    assert (by0 + by1) / 2 == pytest.approx((ink_top + ink_bottom) / 2, abs=1), (name, a["box"], a["ink"])   # centred on its digits
    pad = a["width"] / 2
    clear = lambda b: b[2] < bx0 - pad or b[0] > bx1 + pad or b[3] < by0 - pad or b[1] > by1 + pad
    assert clear(a["hub"]) and clear(a["unit"]) and all(map(clear, a["ticks"])), (name, a["box"], [t for t in a["ticks"] if not clear(t)])
    rx0, ry0, rx1, ry1 = a["rect"]
    assert all(r[2] <= rx0 or r[0] >= rx1 or r[3] <= ry0 or r[1] >= ry1 for r in a["words"]), (name, a["rect"], a["words"])   # (window px)
    cx, cy, r, w = a["ring"]
    reach = max(math.hypot(x - cx, y - cy) for x, y in a["along"]) + a["width"] / 2
    if "inHg" not in name:
        assert reach < r - w / 2 - 1, (name, reach, a["ring"])                  # inside the outline, clear of it
    else:
        assert reach < r + w / 2 + 6, (name, reach, a["ring"])                  # (inHg: at most across it, over the ring)


@needs_browser
async def test_the_pressures_arrow_is_drawn_by_its_class_short_normal_long_or_double_in_its_colour_and_none_when_steady(server, station):
    """The page draws the pressure's `tendency` by class: slow a short arrow (0.5 of the normal length), moderate the normal, quick a long one
    (1.35 times), rapid a double arrow (two chevrons, the second behind the first); up blue and down red; placed 2 px after the digits'
    ink; none for steady or without a tendency; the same in inHg (the change stays in hPa)."""
    wanted = [{"change": c, "way": w, "class": k} for c, w, k in ((0.8, "up", "slow"), (-0.8, "down", "slow"), (2.0, "up", "moderate"),
              (-2.0, "down", "moderate"), (4.5, "up", "quick"), (-4.5, "down", "quick"), (7.0, "up", "rapid"), (-7.0, "down", "rapid"),
              (0.0, "steady", "none"))] + [None]
    port = free_port()
    async with Serving(http_app(server, tokens=(TOKEN,)), port):
        proc = await asyncio.create_subprocess_exec("node", str(BROWSER), f"http://127.0.0.1:{port}/dashboard", "--arrows", json.dumps(wanted),
                                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=NO_WEATHER)
        out, err = await asyncio.wait_for(proc.communicate(), 120)
        assert proc.returncode == 0, err.decode()
    got = json.loads(out)
    length = {}
    for run in got:
        t = run["tendency"]
        assert run["errors"] == [], run
        if t is None or t["way"] == "steady":
            arrow_drawn(run["hpa"], None, f"none {t}")
            arrow_drawn(run["inhg"], None, f"none {t}, inHg")
            continue
        arrow_drawn(run["hpa"], t["way"], f"{t['class']} {t['way']}", t["class"])
        arrow_drawn(run["inhg"], t["way"], f"{t['class']} {t['way']}, inHg", t["class"])
        a = run["hpa"]["arrow"]
        assert a["d"] and run["inhg"]["arrow"]["way"] == t["way"] and run["inhg"]["arrow"]["cls"] == t["class"]
        nums = list(map(float, re.findall(r"[\d.]+", a["d"])))
        length[t["class"], t["way"]] = abs(nums[3] - nums[1])
        assert a["stroke"] == rgb(look()["rising"] if t["way"] == "up" else look()["falling"]), a
    for way in ("up", "down"):
        normal = length["moderate", way]
        assert length["slow", way] == pytest.approx(0.5 * normal, rel=0.02) and length["quick", way] == pytest.approx(1.35 * normal, rel=0.02)
        assert length["rapid", way] == pytest.approx(normal, rel=0.02)


def limit_mark(h, name, title):
    """A dial's limit mark at today's highest (handsLook's): a small solid triangle in the red hands' red, no hand; its base flush with
    the dial's outer edge (the outline), its tip reaching into the coloured band, pointing in at the centre; small (about 4 % of the
    tile's width); clear of the title's text and of every tick number."""
    m = h["limit"]
    assert h["kind"] == "limit" and h["colour"] is None and m["fill"] == look()["high_hand"], (name, title, h)
    tip, *base = m["radii"]   # (the base flush with the dial's outer edge, the tip reaching into the coloured band)
    assert tip < look()["ring_outer"] < min(base) and abs(min(base) - m["ring"]) <= 1 and m["inward"], (name, title, m)
    assert 12 <= m["size"]["width"] <= 24 and 12 <= m["size"]["height"] <= 24, (name, title, m)
    assert 0.02 < m["drawnWidth"] / m["tileWidth"] < 0.06, (name, title, m)
    assert m["clearOfTitle"] and m["clearOfTicks"], (name, title, m)


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
    station.held(pressure=pressure_hour(0.0) if comes else [(m, 1012.0) for m in range(100, -1, -5)])   # (the pressure steady over
                                                                                 # the past 3 hours; or only 100 minutes of it: no arrow)
    port = free_port()
    async with Serving(http_app(server, tokens=(TOKEN,)), port):   # (no weather: as run_page's weather=False)
        page = await asyncio.create_subprocess_exec("node", str(BROWSER), f"http://127.0.0.1:{port}/dashboard", "--arrive", title,
                                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=NO_WEATHER)
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
    arrow_drawn(got["pressure"], None, f"arrive, {comes}")   # (the pressure's arrow: none, steady or without 3 hours of readings)
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
        assert not was["faded"] and was["reading"] == "99°", was
        assert now["faded"] and (now["label"], now["reading"], now["turn"]) == ("Wind direction: no reading", "–", "rotate(0.00 240 203)"), now
        assert was["petals"] == [] == now["petals"] and was["layers"][0] == "rose", was   # (no file: no rose drawn, nothing else in its place)


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
# The browser script's environment for a page with no weather behind the tiles, the rest animated (web_dashboard_browser.js's NO_WEATHER)
NO_WEATHER = {**os.environ, "DASHBOARD_NO_WEATHER": "1"}
async def run_page(server, *args, timeout=120, weather=True, parts=None, nice=0) -> dict:
    """The page served, driven by the browser script with these arguments; its report. With `parts`, each part after the arguments,
    side by side (each its own page, in one browser: one script's start, not one a part): their reports in one, the first part's
    where two report the same, their errors together. (`weather` False: no weather drawn behind the tiles, everything else animated as
    with MCP_DASHBOARD_ANIMATIONS on (NO_WEATHER): for the pages that drive the tiles and time their moves, not the weather, so the
    machine's time goes to those moves. The weather's own tests drive the tiles over it, zoomed too.) `nice`: the browser run at that
    lower priority."""
    port = free_port()
    async with Serving(http_app(server, tokens=(TOKEN,)), port):
        lower = ["nice", "-n", str(nice)] if nice and shutil.which("nice") else []
        page = await asyncio.create_subprocess_exec(*lower, "node", str(BROWSER), f"http://127.0.0.1:{port}/dashboard", *args,
                                                    *(["+".join(parts)] if parts else []), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                    env=None if weather else NO_WEATHER)
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


TEMPERATURES = ("Temperature", "Dew point", "Feels like")   # (the fixture's tiles in °C; Indoor temperature's the same, by its unit)


def in_units(figure: str, convert, decimals: int) -> str:
    """A figure as the page shows it in the other unit: converted, the same decimals ("-" and "" as they are)."""
    return figure if figure in ("-", "") else f"{convert(float(figure)):.{decimals}f}"


def units_switched(seen: dict):
    """The units' switches (unitsSwitched's): °F converts every temperature tile (its reading, scale, ticks, figures and unit), mph the
    Wind's (its reading, dial, ticks, figures, unit and gust hand), inHg Atmospheric pressure's (its arrow's way as it was), in the
    Rain's and the Rain rate's (its limit mark too), the same decimals (two in inHg and inches), the rest untouched; the gauges eased
    there; kept across a reload; km/h again from the keyboard; "reset preferences" metric again, nothing kept."""
    f, mph, inhg, inch = (lambda c: c * 9 / 5 + 32), (lambda k: k / 1.609344), (lambda h: h / 33.8639), (lambda m: m / 25.4)
    metric, fahrenheit, both = seen["metric"], seen["fahrenheit"], seen["mph"]
    pairs = (("°C", "°F"), ("km/h", "mph"), ("hPa", "inHg"), ("mm", "in"))
    switch = lambda *on: [[u, str(o if i else not o).lower()] for (pair, o) in zip(pairs, [*on, False, False][:4]) for i, u in enumerate(pair)]
    RAINS = ("Atmospheric pressure", "Rain", "Rain rate")
    assert metric["pressed"] == switch(False, False) and metric["status"] == "last reading 18:42" and metric["stored"] is None, metric
    for title in TEMPERATURES:
        was, now = metric["tiles"][title], fahrenheit["tiles"][title]
        number, unit = was["reading"].split()
        assert unit == "°C" and now["reading"] == f"{f(float(number)):.1f} °F" and now["label"] == f"{title} {now['reading']}", (title, now)
        assert was["words"] == ["40", "30", "20", "10", "0"] and now["words"] == ["100", "80", "60", "40"], (title, now)   # (0-40 °C: 32-104 °F)
        assert was["unit"] == "°C" and now["unit"] == "°F", now
        assert now["figures"] == [in_units(x, f, 1) for x in was["figures"]] and len(now["figures"]) == 6, (title, was, now)
    assert fahrenheit["tiles"]["Temperature"]["reading"] == "55.2 °F"                     # (spot values: 12.9 °C, its Daily Max 20.0 °C)
    assert fahrenheit["tiles"]["Temperature"]["figures"][0] == "68.0"
    assert fahrenheit["tiles"]["Wind"] == metric["tiles"]["Wind"] and fahrenheit["tiles"]["Humidity"] == metric["tiles"]["Humidity"]
    assert fahrenheit["pressed"] == switch(True, False) and fahrenheit["focused"] == "°F", fahrenheit
    assert fahrenheit["status"] == "last reading 18:42" and fahrenheit["stored"] == {"order": [], "hidden": [], "temperature": "°F"}
    # mph: the Wind's reading, dial (0-60 mph, every 10), figures, unit and gust hand; the temperatures still °F, Humidity untouched
    was, now = metric["tiles"]["Wind"], both["tiles"]["Wind"]
    assert was["words"] == ["0", "20", "40", "60", "80", "100", "km/h"] and now["words"] == ["0", "10", "20", "30", "40", "50", "60", "mph"], now
    assert now["reading"] == in_units(was["reading"], mph, 0) and now["unit"] == "mph" and now["label"] == f"Wind {now['reading']} mph", now
    assert now["figures"] == [in_units(x, mph, 0) for x in was["figures"]] and (was["figures"][1], now["figures"][1]) == ("28", "17"), (was, now)
    share = mph(28) / 60   # (the red hand: today's highest gust, its Daily Gust)
    assert now["hand"] == f"rotate({-(look()['start'] - look()['sweep'] * share):.2f} 240 203)" != was["hand"], (was, now)
    assert seen["needleSteps"] > 3, seen["needleSteps"]                                   # (eased there, frame by frame)
    assert {t: both["tiles"][t] for t in TEMPERATURES} == {t: fahrenheit["tiles"][t] for t in TEMPERATURES}
    assert both["tiles"]["Humidity"] == metric["tiles"]["Humidity"] and both["pressed"] == switch(True, True)
    assert both["stored"] == {"order": [], "hidden": [], "temperature": "°F", "wind": "mph"}, both["stored"]
    assert {t: fahrenheit["tiles"][t] for t in RAINS} == {t: both["tiles"][t] for t in RAINS} == {t: metric["tiles"][t] for t in RAINS}
    # inHg: Atmospheric pressure's reading, dial (950-1050 hPa: 28-31 inHg, every 0.5), figures (two decimals) and unit; its arrow's
    # way the same; the rest as they were
    was, now = metric["tiles"]["Atmospheric pressure"], seen["inHg"]["tiles"]["Atmospheric pressure"]
    assert was["words"] == ["950", "970", "990", "1010", "1030", "1050", "hPa"], was
    assert now["words"] == ["28", "28.5", "29", "29.5", "30", "30.5", "31", "inHg"], now
    assert (was["reading"], now["reading"]) == ("1026", "30.30") and now["label"] == "Atmospheric pressure 30.30 inHg", now
    assert now["figures"] == [in_units(x, inhg, 2) for x in was["figures"]] and now["figures"][-1] == "29.47", (was, now)   # (998 hPa)
    assert (was["unit"], now["unit"]) == ("hPa", "inHg") and now["arrow"] == was["arrow"], now   # (its arrow: the sizes' test's)
    assert {t: v for t, v in seen["inHg"]["tiles"].items() if t != "Atmospheric pressure"} == \
        {t: v for t, v in both["tiles"].items() if t != "Atmospheric pressure"}
    assert seen["inHg"]["pressed"] == switch(True, True, True)
    assert seen["inHg"]["stored"] == {"order": [], "hidden": [], "temperature": "°F", "wind": "mph", "pressure": "inHg"}
    # in: the Rain's reading, beaker (0-100 mm: 0-4 in), figures and unit; the Rain rate's reading, dial (0-50 mm/h: 0-2 in/h, every
    # 0.5), figures, unit and limit mark (today's high, 8 mm/h); two decimals; the rest as they were
    inches = seen["inches"]
    was, now = metric["tiles"]["Rain"], inches["tiles"]["Rain"]
    assert was["words"] == ["100", "80", "60", "40", "20", "0"] and now["words"] == ["4", "3", "2", "1", "0"], now
    assert (was["reading"], now["reading"]) == ("0.2 mm", "0.01 in") and now["label"] == "Rain 0.01 in" and now["unit"] == "in", now
    assert now["figures"] == [in_units(x, inch, 2) for x in was["figures"]] and now["figures"][-1] == "9.14", (was, now)   # (232.2 mm)
    was, now = metric["tiles"]["Rain rate"], inches["tiles"]["Rain rate"]
    assert was["words"] == ["0", "10", "20", "30", "40", "50", "mm/h"] and now["words"] == ["0", "0.5", "1", "1.5", "2", "in/h"], now
    assert (was["reading"], now["reading"]) == ("0.0", "0.00") and now["label"] == "Rain rate 0.00 in/h" and now["unit"] == "in/h", now
    assert now["figures"] == [in_units(x, inch, 2) for x in was["figures"]] and now["figures"][0] == "0.31", (was, now)   # (8.0 mm/h)
    turned = lambda share: f"rotate({-(look()['start'] - look()['sweep'] * share):.2f} 240 203)"
    assert was["hand"] == turned(8 / 50) and now["hand"] == turned(inch(8) / 2), (was, now)
    assert {t: v for t, v in inches["tiles"].items() if t not in ("Rain", "Rain rate")} == \
        {t: v for t, v in seen["inHg"]["tiles"].items() if t not in ("Rain", "Rain rate")}
    assert inches["pressed"] == switch(True, True, True, True) and inches["focused"] == "in", inches
    assert inches["status"] == "last reading 18:42"
    assert inches["stored"] == {"order": [], "hidden": [], "temperature": "°F", "wind": "mph", "pressure": "inHg", "rain": "in"}
    # reloaded: as it was left
    assert seen["reloaded"]["tiles"] == inches["tiles"] and seen["reloaded"]["pressed"] == switch(True, True, True, True), seen["reloaded"]
    # km/h from the keyboard (Enter): the Wind metric again, the rest as they were, the focus kept on the switch
    keyboard = seen["keyboard"]
    assert keyboard["tiles"]["Wind"] == metric["tiles"]["Wind"] and keyboard["pressed"] == switch(True, False, True, True)
    assert keyboard["focused"] == "km/h"
    assert {t: v for t, v in keyboard["tiles"].items() if t != "Wind"} == {t: v for t, v in inches["tiles"].items() if t != "Wind"}
    assert keyboard["stored"] == {"order": [], "hidden": [], "temperature": "°F", "pressure": "inHg", "rain": "in"}
    # "reset preferences": all four metric, nothing kept, the link gone
    assert seen["reset"]["tiles"] == metric["tiles"] and seen["reset"]["pressed"] == switch(False, False), seen["reset"]
    assert seen["reset"]["stored"] is None and seen["reset"]["status"] == "last reading 18:42"


@needs_browser
async def test_tiles_are_dragged_to_reorder_hidden_by_the_x_or_the_remove_zone_kept_across_reloads_and_reset(server, station):
    got = await run_page(server, "--arrange", weather=False, parts=("dragged", "hidden", "prefs", "stale", "reset"))
    default = titles_of(station)
    assert got["errors"] == [] and got["initial"] == default, got["errors"]
    # mouseover: the grip and the × on the tile under the mouse only
    assert got["hover"] == [{"grip": "0", "close": "0"}, {"grip": "1", "close": "1"}, {"grip": "0", "close": "0"}]
    # the grip: 15 by 20 px (dots 5 px apart: three across, four down), in the tile's top left corner
    assert got["grip"] == {"width": 15, "height": 20, "left": 8, "top": 6, "dots": "5px 5px"}, got["grip"]
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
    assert got["closed"] == {"titles": [t for t in reordered if t != "Rain"], "status": "last reading 18:42"}
    assert got["removing"]["over"] and got["removing"]["zone"] == "none"
    shown = [t for t in reordered if t not in ("Rain", "Wind")]
    assert got["removed"] == {"titles": shown, "stored": {"order": reordered, "hidden": ["Rain", "Wind"]}}
    assert got["reloaded"] == {"titles": shown, "status": "last reading 18:42"}
    # reset: every tile, in the default order, nothing kept, no link
    assert got["reset"] == {"titles": default, "status": "last reading 18:42", "stored": None}
    units_switched(got["units"])
    # "reset preferences" left of the units' switches, in the bar with them: the switches never move for it (their boxes the same
    # with it shown and gone), the link left of the first switch (or above them when the row is full), on screen, over nothing,
    # not in the strip; clicking it puts the tiles, units and order back
    bar = got["resetBar"]
    assert sorted(bar["widths"], key=int) == ["320", "390", "768", "1024", "1440"], list(bar["widths"])
    for width, seen in bar["widths"].items():
        assert seen["first"]["link"] is None and seen["gone"]["link"] is None, (width, seen)
        assert seen["shown"]["name"] == "reset preferences" and not seen["shown"]["linkInStrip"], (width, seen["shown"])
        assert seen["shown"]["units"] == seen["first"]["units"] == seen["gone"]["units"], (width, seen)   # (identical boxes)
        assert seen["shown"]["strip"] == seen["first"]["strip"] and seen["shown"]["status"] == "last reading 18:42", (width, seen)
        assert seen["shown"]["onScreen"] and seen["shown"]["clear"], (width, seen["shown"])
        link, first = seen["shown"]["link"], seen["shown"]["units"][0]
        if seen["shown"]["reset"] == "row":   # (left of the switches, in their row, touching their bar)
            assert link["right"] <= first["left"] and link["top"] < first["bottom"] and first["top"] < link["bottom"], (width, seen["shown"])
            assert seen["shown"]["bar"]["left"] - link["right"] < 12, (width, seen["shown"])
        else:   # (a full row: on its own line above, never over the switches)
            assert link["bottom"] <= seen["shown"]["bar"]["top"], (width, seen["shown"])
        assert seen["restored"] and seen["stored"] is None, (width, seen)
    assert {w: s["shown"]["reset"] for w, s in bar["widths"].items()}["1440"] == "row"   # (side by side where there's room)
    assert bar["hidden"]["link"] == 1 and "Rain" not in bar["hidden"]["titles"] and bar["back"]["link"] == 0 and bar["back"]["same"], bar
    # unreadable preferences: the defaults; a tile new to the saved order in its usual place (Rain, after Atmospheric pressure; the
    # wind's direction, after Wind); an
    # absent tile's place and a later stage's keys kept
    assert got["corrupt"] == default
    # a layout saved while the AQI tile was titled "US AQI": carried over to "AQI", the tile moved or hidden as it was left
    assert "AQI" in default and got["renamedMoved"] == ["AQI"] + [t for t in default if t != "AQI"]
    assert got["renamedHidden"] == [t for t in default if t not in ("AQI", "Rain")]
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
    # the frame unseen, the mouse over its tile too; shown while it is picked up, and with the keyboard's focus
    assert look_["frame"] == "rgba(0, 0, 0, 0)", look_
    assert got["hoverFrames"] == ["rgba(0, 0, 0, 0)"] * 3, got["hoverFrames"]
    assert got["shownFrames"] == {"lifted": "rgb(90, 102, 112)", "focused": rgb(look()["soft"]), "after": "rgba(0, 0, 0, 0)"}, got["shownFrames"]
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
    assert got["closed"] == {"titles": [t for t in reordered if t != "Humidity"], "status": "last reading 18:42"}
    # held at the bottom edge (on a page of its own, beside them): the page scrolls to its end, never past it, and stays there once let go
    edge = got["edge"]
    assert edge["seen"][-1] == pytest.approx(edge["end"], abs=1) and max(edge["seen"]) <= edge["end"] + 1, edge
    assert edge["after"] <= edge["end"] + 1, edge


@needs_browser
async def test_fewer_tiles_grow_evenly_to_fill_the_window_and_never_shrink_below_the_full_dashboards_size(server, station):
    every = titles_of(station)
    but = lambda *keep: [t for t in every if t not in keep]
    cases = {"desktop-all": (1920, 1080, []), "desktop-one": (1920, 1080, but("Temperature")),
             "tablet-all": (1024, 768, []), "tablet-three": (1024, 768, but("Temperature", "Wind", "AQI")),
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


@needs_browser
async def test_a_phone_or_tablet_turned_and_turned_back_lays_the_page_out_exactly_as_a_fresh_load_with_no_zoom_or_overflow(server, station):
    got = await run_page(server, "--rotate")
    for device, report in got.items():
        assert report["errors"] == [], (device, report["errors"])
        for step in report["steps"]:
            now, orientation = step["now"], step["orientation"]
            assert now["fit"] == {**now["fit"], "scrollWidth": now["fit"]["clientWidth"], "innerWidth": now["fit"]["clientWidth"], "scale": 1,
                                  "scrollX": 0, "inside": True, "skyCovers": True}, (device, orientation, now["fit"])
            assert now["snapshot"] == report["fresh"][orientation]["snapshot"], (device, orientation)
        late = report["stale"]   # (the real size arriving late, the page shifted sideways meanwhile: laid out as a fresh load, put back)
        assert late["landscape"]["fit"] == {**late["landscape"]["fit"], "scrollWidth": late["landscape"]["fit"]["clientWidth"], "scale": 1,
                                            "scrollX": 0, "inside": True, "skyCovers": True}, (device, late["landscape"]["fit"])
        assert late["landscape"]["snapshot"] == report["fresh"]["landscape"]["snapshot"] and late["sideways"] == 0, (device, late)
        assert late["wide"] >= 0, (device, late["wide"])
        for orientation, fresh in report["fresh"].items():   # (a fresh load fits too)
            assert fresh["fit"]["scrollWidth"] == fresh["fit"]["clientWidth"] and fresh["fit"]["inside"] and fresh["fit"]["skyCovers"], (device, orientation)


# ---------- the zoomed view and its gallery ----------
ZOOM_MS = 200
GAPS_BEFORE = {"dial": 2, "thermometer": 21.7, "beaker": 16.7, "compass": 2}   # (the gap under the title, each kind, before the gauges
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
    async with Serving(http_app(server, tokens=(TOKEN,)), port):   # (no weather: as run_page's weather=False)
        page = await asyncio.create_subprocess_exec("node", str(BROWSER), f"http://127.0.0.1:{port}/dashboard", "--zoom", "in", stdin=subprocess.PIPE,
                                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=NO_WEATHER)
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
TRACK = 25.6   # px/s: down a trail, every running bead at least this fast (a freshly landed rain bead's there, r 3: 1.6 times its 16 on fog)
EFFECTS = {"rain": "Rain", "flood": "Rain", "haze": "AQI", "uv": "UV index", "humidity": "Humidity", "wind": "Wind", "heat": "Temperature",
           "ice": "Temperature"}


# The weather's parts whose pages are driven (tests/web_dashboard_browser.js, weatherPage): what they see doesn't depend on the time
# their frames take, so they run a little behind the rest (nice 5): the pages whose own timing is tested (a frame rate, the zoom's
# moves) get a CPU first when the machine is busy; the rest of the suite (conftest: nice 10) after them
DRIVEN = {"push", "trails", "frost", "budget", "flood", "bucket", "pm25", "opened", "gust", "over", "direction", "uturn", "slant", "pace", "hysteresis", "drops", "eddies", "gives"}


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
    # each effect at its mark and not just short of it (rain above 0 mm/h, the AQI's Moderate band from 51, above UV 6, 90 %, wind above
    # 15 km/h, 35 °C, below 0 °C, not at it), its tile framed in red, the strip's link with it
    for name, mark in got["marks"].items():
        effect = "ice" if name == "iceAtZero" else name
        assert mark["below"] | {"levels": None} == {"effects": [], "levels": None, "running": mark["below"]["running"], "framed": [],
                                                     "link": None}, (name, mark)
        assert mark["at"]["effects"] == [effect] and mark["at"]["framed"] == [EFFECTS[effect]], (name, mark)
        assert mark["at"]["link"] == "stop" and mark["at"]["running"], (name, mark)
    assert got["hazeBands"] == [2, 3, 4, 5, 5]                                   # thicker with each band: USG to Hazardous
    assert got["rainStrength"][0] < 0.4 and got["rainStrength"][1:] == [1, 1]    # light at 0.5 mm/h, heavy from 8
    assert got["humidityStrength"] == pytest.approx([0.1, 0.5, 1])              # heavier from 90 % to 100 %
    assert got["uvStrength"] == pytest.approx([0.2, 0.5, 1, 1])                  # the beams rise from UV 6 to UV 11: the level (UV - 6) / 5
    assert got["iceDepth"] == [0.5, 5]                                           # further in the colder
    assert got["heatStrength"] == pytest.approx([0.3, 0.65, 1, 1])               # stronger from 35 °C to 43 °C
    # shown in °F, mph, inHg and in: the same marks, of the readings in °C, km/h and mm/h (heat from 35 °C, leaves above 15 km/h, ice
    # below 0 °C, rain above 0 mm/h, heavy from 8; the flood from the server's bucket depth 0.05, not below it)
    calm = lambda m: {**m, "below": m["below"] | {"running": None}}   # (just short: whether the last effect's still fading out aside)
    assert {k: calm(m) for k, m in got["unitsMarks"].items()} == {k: calm(got["marks"][k]) for k in ("heat", "wind", "ice", "rain")}
    assert got["unitsRain"][0][0] < 0.4 and [r for r, _ in got["unitsRain"][1:]] == [1, 1, 1], got["unitsRain"]
    assert [bool(f) for _, f in got["unitsRain"]] == [False] * 4, got["unitsRain"]   # (the rate alone calls for no flood)
    # the thermometer's scale widened as the server widens it in °C (-10 to 40 °C: 14 to 104 °F; 0 to 50 °C: 32 to 122 °F)
    assert got["widened"] == ["Temperature 23.0 °F", ["100", "80", "60", "40", "20"]], got["widened"]
    assert got["widenedHot"] == ["Temperature 109.4 °F", ["120", "100", "80", "60", "40"]], got["widenedHot"]


@needs_browser
async def test_several_effects_share_one_canvas_behind_the_see_through_tiles_zoomed_too(server, station):
    got = await weather(server, "several")
    # several at once: one canvas, one loop, at most 30 frames drawn a second
    several = got["several"]
    assert several["effects"] == ["uv", "haze", "rain", "wind", "humidity", "ice"] and several["canvases"] == 3, several   # (the weather's canvas and its two margins: see test_the_weather_reaches_past...)
    assert sorted(several["framed"]) == sorted({EFFECTS[k] for k in several["effects"]}) and several["ink"] > 0, several
    assert 20 <= several["drawn"] <= 32 and several["frames"] <= 64, several    # (a second's: one request a display frame, every other one drawn)
    # the canvas: filling the window, fixed, first, behind the tiles, never in the pointer's way, its pixels crisp
    # (#sky now reaches OVER = 100 px past the window's top and bottom: the weather's own canvas is still the window's size)
    assert got["canvas"] == {"rect": [0, -100, 1024, 968], "canvas": [True, True], "position": "fixed", "z": "0", "events": "none",
                             "first": True, "pixelated": "pixelated", "under": True}
    # the tiles see-through (what's behind shows in a tile's margin), their foreground opaque; zoomed too
    assert got["through"] == {"differs": True, "still": True}
    assert got["look"] == {"background": "rgba(0, 0, 0, 0)", "opacity": "1", "title": "rgb(232, 234, 237)", "frame": "rgba(0, 0, 0, 0)",
                           "alertFrame": "rgba(0, 0, 0, 0)",   # (the red frame off for now; the class still set)
                           # dragged, a framed tile shows the drag's frames as any other: lifted grey, then the remove zone's red
                           "dragFrames": {"lifted": "rgb(90, 102, 112)", "doomed": "rgb(229, 72, 77)", "after": "rgba(0, 0, 0, 0)"}}
    assert got["zoomed"] == {"zoomed": True, "title": "UV index", "background": "rgba(0, 0, 0, 0)", "grid": "rgba(0, 0, 0, 0)",
                             "alert": True, "running": True}


def test_the_page_and_its_browser_bars_are_the_dark_colour_and_the_weather_reaches_past_the_window_in_css():
    page = PAGE.read_text()
    assert re.search(r"--bg: #14191D", page) and re.search(r"html, body \{[^}]*background: var\(--bg\)", page)
    assert '<meta name="theme-color" content="#14191D">' in page
    assert re.search(r"#sky \{[^}]*--over: 100px;[^}]*top: calc\(-1 \* var\(--over\)\); bottom: calc\(-1 \* var\(--over\)\)", page)


@needs_browser
async def test_the_weather_reaches_past_the_windows_top_and_bottom_so_a_hiding_address_bar_shows_no_bare_strip(server, station):
    got = await weather(server, "over")
    OVER = 100
    for name in ("first", "grown", "relaid"):
        seen = got[name]
        h = seen["inner"][1]
        assert seen["canvases"] == 3, (name, seen)
        # the weather's canvas and its margins: at least OVER past the top and the bottom of the window, the whole width, even with the
        # viewport made taller before any relayout (the bar hidden); the margins end where the window's canvas begins
        # (made taller before any relayout, the margins still cover the whole window; only the margin's own reach is what is left)
        assert seen["above"][0] <= -OVER and seen["below"][1] >= (h if name == "grown" else h + OVER), (name, seen)
        # (the weather's own canvas alone covers the window but when it was made taller; then it and the margin below it do together)
        assert seen["canvas"][0] <= 0 and (seen["canvas"][1] >= h or (name == "grown" and seen["below"][1] >= h)), (name, seen)
        assert abs(seen["above"][1] - seen["canvas"][0]) < 0.01 and abs(seen["below"][0] - seen["canvas"][1]) < 0.01, (name, seen)
        assert seen["canvas"][2] <= 0 and seen["canvas"][3] >= seen["inner"][0], (name, seen)
        # the weather's own world is the window's size alone (the flood's level, the gust's height: from the visible bottom): the
        # margins are only the first and last rows mirrored
        width, rows, p = seen["size"]
        assert (name == "grown" or rows == -(-seen["client"][1] // p)) and seen["flood"] == rows, (name, seen)
        assert seen["mirrored"] == {"top": True, "bottom": True, "top2": True}, (name, seen)
        assert seen["ink"][1] > 0 and seen["ink"][0] > 0 and seen["ink"][2] > 0, (name, seen)
        # the page's own colour all round, and the browser's bars the same
        assert seen["bg"] == ["rgb(20, 25, 29)", "rgb(20, 25, 29)"] and seen["theme"] == "#14191D", (name, seen)
    assert got["grown"]["inner"][1] == got["first"]["inner"][1] + 90 and got["grown"]["size"] == got["first"]["size"], got
    assert got["relaid"]["size"][1] > got["first"]["size"][1], got


@needs_browser
async def test_stop_animation_stops_and_clears_for_this_page_view_only_nothing_saved_a_reload_or_a_test_link_runs_them_again(server, station):
    got = await weather(server, "stop")
    # "stop" (a test link's wind on too), its tooltip naming the setting for good
    assert got["simulating"]["effects"] == ["uv", "wind"] and got["simulating"]["link"] == "stop", got["simulating"]
    assert got["simulating"]["title"] == "stop until reload; set MCP_DASHBOARD_ANIMATIONS=off to turn off for good", got["simulating"]
    assert got["simulating"]["label"] == "stop the animation until reload", got["simulating"]   # (it reads just "stop")
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
    assert reloaded["link"] == "stop" and reloaded["running"] and reloaded["frames"] > 10, reloaded
    assert reloaded["stored"] == {"order": [], "hidden": ["Rain"]}, reloaded
    # stopped again, then a test link (heat): on again, its effect and the readings' with it, the link back
    resumed = got["resumed"]
    assert resumed["link"] == "stop" and resumed["running"] and resumed["frames"] > 10, resumed
    assert set(resumed["effects"]) == {"uv", "wind", "heat"} and resumed["url"] == "?fx=heat", resumed
    # from the keyboard: Enter on it stops them, the link gone (the focus not left on it)
    assert got["keyboard"]["link"] is None and not got["keyboard"]["running"] and got["keyboard"]["focused"] == "BODY", got["keyboard"]


@needs_browser
async def test_the_temporary_test_links_turn_each_effect_on_and_stop_animation_ends_them_and_with_animations_off_nothing_is_drawn(server, station):
    got = await weather(server, "links")
    # the temporary test links: each its effect and frame (the gauges keep their readings), the URL following; haze steps through its
    # bands, rain light then heavy; ice melts back; "stop" ends them all (no "clear"); no "all" link, but ?fx=all
    links = got["links"]
    assert links["buttons"] == ["rain", "haze", "uv", "humidity", "wind", "heat", "ice", "gust"], links["buttons"]
    on = []
    for name in ("uv", "humidity", "wind", "heat", "ice"):
        on.append(name)
        assert set(links[name]["effects"]) == set(on) and EFFECTS[name] in links[name]["framed"], (name, links[name])
        assert links[name]["url"] == "?fx=" + ",".join(on)
    assert links["label"] == "UV index 0.0"                                        # (the stand-in's reading, not the simulated one)
    # "stop" at the bottom left, just right of the row's last link, beside the strip (at the window's bottom), clear of it;
    # the row (no "test:" label any more), all of it in sight
    corner = links["corner"]
    assert corner["link"] == "stop" and corner["apart"] and corner["onScreen"], corner
    just_right_of_the_row(corner)
    assert not corner["tag"] and corner["fits"], corner
    assert corner["row"]["left"] == 0 and corner["motion"]["bottom"] == corner["window"][1] == corner["strip"]["bottom"], corner
    assert links["haze"] == [1, 2, 3, 4, 5, 0]
    assert links["rain"] == [[0.35, 0], [1, 0], [1, 2], [0, 0]]   # (light, heavy, heavy with a flash flood (the test's own), off)
    melting = links["melting"]
    assert melting["grown"] > 2 and 0 < melting["reach"] < melting["grown"] and "ice" not in melting["effects"] and melting["running"], melting
    # "stop" with only test links on: every one off, ?fx= gone, the animations stopped (nothing saved), the link gone
    assert links["cleared"] | {"levels": None, "running": None} == {"effects": [], "levels": None, "running": None, "framed": [],
                                                                     "link": None, "url": "", "pressed": 0, "stored": None}
    assert links["all"]["effects"] == EVERY and links["all"]["url"] == "?fx=rain2,haze3,uv,humidity2,wind,heat,ice"
    # humidity light (91 %) first, then heavy (100 %), then off
    assert links["humiditySteps"] == [{"level": 1, "url": "?fx=uv,humidity2,wind,heat", "label": "humidity: heavy"},
                                      {"level": 0, "url": "?fx=uv,wind,heat", "label": "humidity"}], links["humiditySteps"]
    assert links["role"] == "group" and links["pressed"] == {"rain": "false", "haze": "false", "uv": "true", "humidity": "true",
                                                             "wind": "true", "heat": "true", "ice": "false"}, links["pressed"]
    # on a phone (360x740, 390x844), every effect on (its links' longest): no "test:", each link ("gust" too) and "stop" in sight,
    # unscrolled, with room for one more short link (some 30 px); a link's step a raised figure, its full words its label
    for phone in (got["phone360"], got["phone390"]):
        assert not phone["tag"] and phone["fits"] and phone["spare"] >= 30 and phone["apart"] and phone["onScreen"], phone
        just_right_of_the_row(phone)
    for phone in (got["phone360"], got["phone390"]):
        assert phone["errors"] == [] and phone["link"] == "stop", phone
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
    assert off["hidden"] == {"strip": "last reading 18:42", "frames": 0, "ink": 0, "running": False}, off["hidden"]


@needs_browser
async def test_the_motion_setting_on_low_and_off_and_reduced_motion_as_off(server, station):
    """MCP_DASHBOARD_ANIMATIONS, each page as served with it, and a browser asking for reduced motion (served with on): on, everything
    animated; low, the gauges sweeping and the tiles reflowing but no weather at all and the zoom at once; off and reduced motion, no
    weather and nothing animated."""
    got = await weather(server, "motion")
    tiles = len(titles_of(station)) - 1   # (Rain hidden)
    called = [e for e in EVERY if e != "heat"]   # (every effect called for but the heat: below 0 °C)
    for mode in ("on", "low", "off", "reduced"):
        seen = got[mode]
        weather_on, sweeps, zooms = mode == "on", mode in ("on", "low"), mode == "on"
        # the gauges: Humidity's new reading (97 %) swept to, through the values between, or straight there, its one change
        gauge = seen["gauge"]
        assert gauge[-1].startswith("97"), (mode, gauge)
        assert (len(gauge) > 3) if sweeps else (len(gauge) == 1), (mode, gauge)
        # the weather: drawn and its loop running, "stop" and the test links shown, a gust sending something across; or none at all:
        # no canvas shown, not one frame, nothing on it, nothing blown across, no "stop", no test links (asked for all the same)
        w = seen["weather"]
        assert [e for e in w["effects"] if e != "gust"] == called, (mode, w)   # (the gust on while one crosses)
        if weather_on:
            assert w["running"] and w["frames"] > 0 and w["ink"] > 0 and w["gust"] >= 1, (mode, w)
            assert w["link"] == "stop" and w["shownLink"] and w["row"] and w["sky"] != "none" and w["corner"] != "none", (mode, w)
        else:
            assert w | {"effects": None, "levels": None, "framed": None} == {
                "effects": None, "levels": None, "framed": None, "running": False, "frames": 0, "ink": 0, "gust": 0, "link": None,
                "shownLink": False, "row": False, "sky": "none", "corner": "none"}, (mode, w)
        # the zoom: animated (its animations running just after the click and after Esc, the gallery's step a transition), or at once
        assert seen["zoomed"]["zoomed"] and not seen["back"]["zoomed"], (mode, seen)
        if zooms:
            assert seen["zoomed"]["animations"] > 0 and seen["back"]["animations"] > 0 and seen["paged"]["transition"] != "0s", (mode, seen)
        else:
            assert seen["zoomed"]["animations"] == seen["back"]["animations"] == 0 and seen["paged"]["transition"] == "0s", (mode, seen)
        # a tile hidden: the others gliding to their places (on their way the next frame), or there at once
        hidden = seen["hidden"]
        assert hidden["tiles"] == tiles, (mode, hidden)
        if sweeps:
            assert hidden["moved"] > 0 and hidden["gliding"] > 0, (mode, hidden)
        else:
            assert hidden["moved"] == hidden["gliding"] == 0, (mode, hidden)


EVERY = ["heat", "uv", "haze", "rain", "wind", "humidity", "ice"]
# humidity's beads' colours, [r, g, b, alpha] (alpha 0-255): the glass's pale body (#A6D7F2, 34) and crescent (105), the white glint
# (#EFFAFF, 240), the deep blue rim (#2C5573, 110); the faintest first
BEAD_COLOURS = [[166, 215, 242, 34], [166, 215, 242, 105], [44, 85, 115, 110], [239, 250, 255, 240]]


def just_right_of_the_row(corner):
    """ "stop" right after the test row, on its line, a few pixels right of its last link (the row's end, in sight)."""
    motion, row, last = corner["motion"], corner["row"], corner["last"]
    assert motion["left"] == pytest.approx(row["right"], abs=0.5) and -1 <= motion["left"] - last["right"] <= 10, corner
    assert row["left"] <= last["left"] and last["right"] <= row["right"] + 1, corner   # (-1, +1: the row scrolled to a whole px)
    assert motion["bottom"] == pytest.approx(row["bottom"], abs=0.5) and motion["top"] == pytest.approx(row["top"], abs=0.5), corner


def _hue(rgb: list[float]) -> float:
    """The hue (degrees, -180 to 180: red 0, yellow 60) of a colour [r, g, b] 0-255."""
    hue = colorsys.rgb_to_hsv(*(v / 255 for v in rgb))[0] * 360
    return hue - 360 if hue > 180 else hue


@needs_browser
async def test_the_haze_alone_in_each_band_a_static_smog_veil_tinted_by_the_band_panned_slowly_only_with_wind(server, station):
    got = await weather(server, "haze")
    # the haze (the particulates), each band alone: one translucent veil over the whole window (no clouds), more of it covered and
    # more opaque with each band (at Moderate thin and patchy, at Hazardous nearly everywhere)
    levels = got["haze"]["levels"]
    assert [lv["level"] for lv in levels] == [1, 2, 3, 4, 5], levels
    covers = [round(lv["cover"], 3) for lv in levels]
    assert 0.15 < covers[0] < 0.7 and covers[-1] > 0.9 and all(a < b for a, b in zip(covers, covers[1:])), covers
    assert all(a["mean"] * 1.1 < b["mean"] for a, b in zip(levels, levels[1:])), [lv["mean"] for lv in levels]
    # ... a sickly tint that deepens with the band, on the AQI gauge's own scale: a grey-yellow at Moderate (hue 40-70 degrees, pale
    # and greyish), ochre and brown through the middle bands, a brown-red at Very Unhealthy and a maroon-brown at Hazardous (hue under
    # 20 degrees, red's side), dark and murky: Hazardous darker than Moderate (by half or more), and never bright
    hues = [_hue(lv["rgb"]) for lv in levels]
    assert 40 <= hues[0] <= 70 and all(a > b for a, b in zip(hues, hues[1:])), hues
    assert 25 <= hues[1] <= 55 and 15 <= hues[2] <= 40, hues
    assert hues[4] < 20, hues
    lums = [lv["lum"] for lv in levels]
    assert lums[4] < 0.5 * lums[0] and lums[4] < 80 and all(a > b for a, b in zip(lums, lums[1:])), lums
    # ... nothing lit from one side: no light tops over darker undersides, the upper half of what is drawn as bright as the lower
    # (within 15 %)
    assert all(abs(lv["upper"] / lv["lower"] - 1) < 0.15 for lv in levels), [(lv["upper"], lv["lower"]) for lv in levels]
    # ... streaks: its density runs longer along the wind than across it (the autocorrelation summed over the first lags: 1.5 times)
    assert all(lv["acrossX"] >= 1.5 * lv["acrossY"] for lv in levels), [(lv["acrossX"], lv["acrossY"]) for lv in levels]
    # ... dithered irregularly: no grid in its pixels (its strongest fine frequency's power, over the median's: under 400; an ordered
    # dither's, 75000 and more)
    assert all(lv["grid"] < 400 for lv in levels), [round(lv["grid"]) for lv in levels]
    # ... static, baked once and panned sideways, only with wind: with no wind (the live speed 0) nothing moves at all, the picture
    # identical 2 s apart (and again a minute on), its motes too, no new bake
    h = got["haze"]
    for still in (h["still"], h["stillLong"]):
        assert still["identical"] and still["slide"] == 0 and still["speed"] == 0 and still["moteSpread"] == 0 and still["moteDx"] == 0 and still["renders"][0] == still["renders"][1], still
    # ... with wind a pure sideways shift (the best match at no rows up or down, nearly every pixel identical: the pan is whole pixels
    # of one baked image), leftward, at about 0.4 px/s at 3 km/h and about 3 at 40 (a gentle curve: 3.15 * (1 - exp(-w / 22.1)), w the
    # speed plus 0.3 of the gust over it), no bake while it pans, the motes carried along at the same pace and none moving up or down
    pan = lambda w: 3.15 * (1 - math.exp(-w / 22.1))
    assert 0.32 < pan(3) < 0.48 and 2.4 < pan(40) < 3.6
    for key, w in (("light", 3), ("strong", 40), ("gusty", 25 + 0.3 * 15)):
        m = h[key]
        assert m["rate"] == pytest.approx(pan(w), rel=0.2) and m["speed"] == pytest.approx(pan(w), rel=0.2), (key, m)
        assert m["down"] == 0 and m["match"] > 0.97 and not m["identical"], (key, m)
        assert abs(m["shift"] - m["slide"]) <= 1.01, (key, m)   # (the picture's shift, leftward positive, the pan's)
        assert m["renders"][0] == m["renders"][1], (key, m)
        assert m["motes"] >= 20 and m["moteDx"] == pytest.approx(-m["slide"], abs=0.05) and m["moteSpread"] < 0.05 and m["moteDy"] == 0, (key, m)
    assert 0.3 < h["light"]["rate"] < 0.5 and 2.4 < h["strong"]["rate"] < 3.6 and h["strong"]["rate"] > 4 * h["light"]["rate"], (h["light"], h["strong"])
    assert h["gusty"]["rate"] > h["light"]["rate"] and h["gusty"]["rate"] < h["strong"]["rate"], (h["light"], h["gusty"], h["strong"])
    # ... a minute of weather time: still no vertical change (every step a pure sideways shift, the rows' profile as it was)
    assert all(m["down"] == 0 and m["match"] > 0.97 and m["renders"][0] == m["renders"][1] for m in h["minute"]["steps"]), h["minute"]
    assert h["minute"]["rows"] > 0.6, h["minute"]   # (the window slides along streaks: its rows alike, not equal)
    # ... the wind's other side: panning rightward, the same baked image drawn mirrored (no new bake)
    mirrored = h["mirrored"]
    assert mirrored["slide"] < 0 and mirrored["down"] == 0 and mirrored["match"] > 0.97 and mirrored["baked"] == 0, mirrored
    assert abs(mirrored["shift"] - mirrored["slide"]) <= 1.01 and mirrored["rate"] == pytest.approx(-pan(40), rel=0.2), mirrored
    # ... baked again only for a new band (once, the old and new cross-fading: pending, then settling, then done) or a resized window
    band = h["band"]
    assert band["baked"] == 1 and band["mid"]["settling"] == 1 and band["settled"] == 0, band
    assert got["resized"]["baked"] == 1 and got["resized"]["width"] >= 2 * got["resized"]["art"], got["resized"]
    # ... wider than the window and seamless: its left and right edges continue each other (the alpha step across the seam as small as
    # between neighbouring columns)
    assert h["width"][0] >= 2 * h["width"][1], h["width"]
    assert h["seam"]["edge"] <= 1.3 * h["seam"]["inner"], h["seam"]
    # ... and cheap: Hazardous, the dearest, under 4 ms a frame
    assert got["haze"]["cost"] < 4, got["haze"]["cost"]


@needs_browser
async def test_heavy_humidity_alone_costs_no_more_as_its_trails_build_up_and_its_sweep_is_capped(server, station):
    got = await weather(server, "trails")
    # (the cost: the script's own time a frame, the main thread's CPU time, however busy the machine)
    # after minutes of the weather's time (over 200 s, the trails and beads built up: tens of thousands of trail pixels): no dearer
    # than twice the start's plus 1.5 ms (its cost not growing with the trails; the start's itself about 1-2 ms); and with its time 20 times as fast, the trails' sweep capped (a
    # slice of them a frame, never all), still cheap
    late, fast = got["late"], got["fast"]
    assert got["cost"] < 8, got["cost"]
    assert late["clock"] > 200 and late["trails"] > 15000 and late["beads"] > 100 and late["cost"] < 2 * got["cost"] + 1.5, (got["cost"], late)
    assert fast["trails"] > 15000 and 0 < fast["swept"] <= fast["most"] == 2500 and fast["cost"] < 20, fast   # (20x time: several times the work a frame, 8-12 ms here; the sweep cap above is the real check)


@needs_browser
async def test_the_ice_grown_all_the_way_in_is_cheap_with_its_frost_across_the_glass(server, station):
    got = await weather(server, "frost")
    assert got["frosted"]["cost"] < 8 and got["frosted"]["crystals"] > 0.15, got["frosted"]


@needs_browser
async def test_heavy_rain_with_wind_and_haze_keeps_to_the_shared_particle_budget_and_is_cheap(server, station):
    got = await weather(server, "budget")
    assert got["cost"] < 8, got["cost"]
    together = got["particles"]
    assert together["wind"] > 0 and together["rain"] > 0, together
    # ... the shared budget kept by every effect but humidity's beads (their own limits bound them: heavy rain keeps every drop)
    assert sum(v for k, v in together.items() if k not in ("budget", "humidity")) <= together["budget"] + 3, together   # (each rounded)


def _whole_streaks(looks):
    """Each bead's clear-glass streak, from all the looks at it (each its latest rows as it ran on): each row as first looked at
    (fresh), in order down the glass; its width (w) the rows' mean."""
    rows = {}
    for t in looks:
        if t["kind"] != "clear":
            continue
        mine = rows.setdefault(t["id"], {})
        for k, y in enumerate(t["ys"]):
            mine.setdefault(y, (t["widths"][k], t["paused"][k], t["w"]))
    streaks = []
    for bead, mine in rows.items():
        ys = sorted(mine)
        streaks.append({"id": bead, "kind": "clear", "ys": ys, "widths": [mine[y][0] for y in ys], "paused": [mine[y][1] for y in ys],
                        "w": sum(mine[y][2] for y in ys) / len(ys)})
    return streaks


@needs_browser
@pytest.mark.timeout(150)   # (about 70 s of browser time: the long scenario of every effect)
async def test_effects_asked_for_as_the_page_opens_alone_and_on_a_phone(server, station):
    got = await weather(server, "opened")
    # ?fx= when the page opens
    assert got["param"]["levels"] == {"gust": 0, "haze": 3, "heat": 0, "uv": 0, "rain": 0, "wind": 1, "humidity": 0, "flood": 0, "ice": 5}
    assert sorted(got["param"]["framed"]) == ["AQI", "Temperature", "Wind"] and got["param"]["errors"] == []
    # (the effects' earlier names, in links saved before, still work: as wind, uv, humidity, and smog4 as haze4)
    assert got["paramOld"]["effects"] == ["uv", "haze", "wind", "humidity"] and got["paramOld"]["errors"] == [], got["paramOld"]
    assert got["paramOld"]["levels"]["haze"] == 4, got["paramOld"]
    assert got["paramAll"]["effects"] == EVERY and got["paramRain"]["levels"]["rain"] == 1
    # the particles: uv's beams none (no motes); the wind's leaves alone within the shared budget
    assert got["particles"]["uv"] == {"uv": 0, "budget": got["particles"]["uv"]["budget"]}, got["particles"]
    wind = got["particles"]["wind"]
    assert 0 < wind["wind"] <= wind["budget"], wind
    # uv's beams over 40 s at a fixed sun position: static between frames (every frame pixel-identical to the first; no beam's angle,
    # length, strength or width changes)
    uv = got["uv"]
    assert uv["errors"] == [] and uv["frames"] >= 400, uv
    assert uv["changed"] == 0 and uv["most"] == 0 and uv["moved"] == 0, uv
    # ... their dither irregular: no grid in its pixels (as gridPeak sees it: under 400)
    assert uv["grid"] < 400, uv["grid"]
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
        # at its size then, so the sizes that happened to run on each don't count; any size, faster down a trail than 1.6 times fog's
        # (every running bead there at least a fresh rain bead's speed, TRACK: the smaller far faster than that); each glass, about its
        # own speed (pace: px it'd have run at its glass's mean speed for its size, TRACK's floor and all): no faster, no slower)
        speed = {g: o["px"] / o["band"] for g, o in glass.items() if o["band"] > 0}
        assert speed["trail"] > 1.6 * speed["fog"], (name, speed)
        assert 0.75 < glass["fog"]["all"] / glass["fog"]["norm"] < 1.3 and 1.6 < glass["trail"]["all"] / glass["trail"]["norm"], (name, glass)
        assert all(0.75 < glass[g]["all"] / glass[g]["pace"] < 1.3 for g in ("fog", "trail")), (name, glass)
        # (down a trail, r 1.8-3.2, all at about TRACK: seen over all three, below)
        on_fog = [px / s for px, s in beads["sizes"] if s > 0]   # (by size: under 1, 1-1.6, 1.6-2.4, 2.4-3.2, 3.2 and over)
        assert beads["sizes"][0] == [0, 0], (name, beads["sizes"])   # (under r 1: none running, they run from r 1.4)
        assert len(on_fog) == 4 and on_fog == sorted(on_fog) and on_fog[0] < 1 and on_fog[-1] > 5, (name, beads["sizes"])   # (1.4-1.6: creeping)
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
        # (a long run on fog, no rain: no bead on fog dries away (faded: the cap, the budget or otherwise), none but a bead stopped
        # on dry glass shrinks; none condenses in a run)
        assert beads["faded"] == 0, (name, beads["faded"])
        # (tiny beads never moving but to snap together, many snapping; condensing on and on, more and more of them, up to the most
        # there may be (the cap; it shrinks as small beads take their room), never more; beads from r 1.4, the run size, to 1.6 running)
        assert beads["crept"] == 0 and beads["snaps"] >= 10, (name, beads["crept"], beads["snaps"])
        # (heavy humidity condensing them fast, its glass beaded up a while first (150 frames): full from the watch's start; light's
        # watched from the first, below: more and more of them)
        assert beads["tinies"][1]["n"] >= beads["tinies"][1]["cap"] - 3, (name, beads["tinies"])
        # (the biggest (r 2.5 and up) now and then elongating as they set off again after a pause: about one restart in three, as
        # seen over all three, below; never a smaller one (as followed here, the weather's time 6 times as fast: a stretch is over
        # within a frame, seen in rain's, below))
        assert beads["watched"]["small"] == 0 and all(e["r"] >= 2.5 for e in beads["watched"]["stretches"]), (name, beads["watched"])
        assert any(t["n"] >= t["cap"] - 3 for t in beads["tinies"]), (name, beads["tinies"])
        # (none condensing at the cap; the cap falling as small beads take room, tiny ones on fog never dried for it: never past the most it was, before)
        assert all(t["n"] <= 1.4 * max(u["cap"] for u in beads["tinies"]) + 4 for t in beads["tinies"]), (name, beads["tinies"])
        assert beads["sizes"][1][0] > 0, (name, beads["sizes"])
        # (tiny beads sitting on the fog swelling slowly with it, each its own rate (as thick as the fog is under it): in a minute or
        # two (as felt here, about 0.4) small beads, no longer tiny (from then on as small ones are); never quicker than their rate)
        swells, rate, spread = beads["swells"], beads["swell"]["rate"], beads["swell"]["spread"]
        assert beads["swelled"] >= 20 and len(swells) >= 20, (name, beads["swelled"])
        assert all(s["r0"] <= beads["tiny"] < s["vol"] ** (1 / 3) and 0 < s["fogged"] <= s["took"] + 1e-6 for s in swells), (name, swells)
        rates = [(s["vol"] - s["r0"] ** 3) / s["soaked"] for s in swells]   # (water gained for each s on fog as thick as 1)
        assert all(rate * spread[0] - 1e-9 <= k <= rate * spread[1] + 1e-9 for k in rates), (name, rates)
        assert max(rates) - min(rates) > 0.4 * rate, (name, rates)   # (each its own)
        took = sorted(s["took"] for s in swells)
        assert 40 <= took[len(took) // 2] <= 200 and took[0] >= 8 and took[-1] <= 400, (name, took)
    # (down a trail, r 1.8-3.2, all at about TRACK (a rain bead's least there, about 25.6 px/s; r 3.2: 31), not their size's 2-20:
    # seen over all three pages' together, a page's own a second or two of them, too few for a mean, its pauses one way or another)
    px, s = (sum(beads["glass"]["trail"][k] for beads in got["humidity"].values()) for k in ("px", "s"))
    assert s >= 3 and 0.75 * TRACK < px / s < 1.3 * 31, {n: b["glass"]["trail"] for n, b in got["humidity"].items()}
    # (about one restart in three elongating: seen over all of each page's beads since it opened (the glass beading up first too)
    # together, a hundred restarts or more, so a watch's handful more or fewer is no matter)
    restarts, stretches = (sum(beads["since"][k] for beads in got["humidity"].values()) for k in ("restarts", "stretches"))
    assert restarts >= 20 * len(got["humidity"]) and 0.15 * restarts <= stretches <= 0.55 * restarts, {n: b["since"] for n, b in got["humidity"].items()}
    # (the biggest sprites (r 3.6 and up) never stop-start on fog or in a trail: no paused time there, however long they run; on dry glass
    # they may pause (here or there), and the others still do)
    big = {k: [sum(beads["since"]["big"][k][i] for beads in got["humidity"].values()) for i in (0, 1)] for k in ("fog", "trail", "dry")}
    assert big["fog"][0] > 0 and big["fog"][1] == 0 and big["trail"][1] == 0, big
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
    # ... one of the largest (r 4.2) taking in a tiny one, beside it (its water added exactly): swelling a size (a px or two wider and taller than
    # its own, its lower edge where it was) for a few tenths of a second at most, then round again; a smaller one (r 3.4) taking in the same: no swell
    gulped = got["humidity"]["alone"]["gulped"]
    assert gulped["spot"], gulped
    big, smaller = gulped["pairs"]
    first = big["frames"][0]
    assert first["vol"] == pytest.approx(sum(big["before"]), rel=0.01) and first["r"] ** 3 == pytest.approx(first["vol"]), big
    swollen = [f for f in big["frames"] if f["w"] > f["rw"] and f["h"] > f["rh"]]
    assert swollen and all(f["w"] - f["rw"] <= 2 and f["h"] - f["rh"] <= 2 and f["low"] == f["bottom"] for f in swollen), big
    lasted = [f["t"] for f in big["frames"] if f["gulp"] > 0]
    assert 1 <= len(lasted) <= 6 and lasted == list(range(lasted[0], lasted[-1] + 1)) and max(f["gulp"] for f in big["frames"]) == 1, big   # (up, then back down, eased)
    after = [f for f in big["frames"] if f["t"] > lasted[-1] + 1]
    assert after and all((f["w"], f["h"]) == (f["rw"], f["rh"]) and f["gulp"] == 0 for f in after), big   # (settled: round again)
    assert smaller["frames"] and all(f["gulp"] == 0 for f in smaller["frames"]), smaller
    # ... the fog: forming first (the first bead only once it has, there and on the glass at large), its trails wiped clear through it
    # about as wide as the bead was there (its edges soft: a px or so either way; a px more where another's beside it), clearer than the
    # fog beside them (where there is fog); the beads' colours as ever (the glass: the deep blue rim, a faint pale body, a crescent, a
    # white glint)
    for name, beads in got["humidity"].items():
        first = beads["first"]
        assert first["after"] >= 2 and first["cover"] >= 0.3 and first["fog"] > 0.2 and first["formed"]["edge"] > 0.2, (name, first)
        wiped = beads["wiped"]
        # (wiped clear, or near its soft edge all but: the fog already creeping back there a little)
        assert len(wiped) >= 8 and all(t["shown"] <= 0.25 * t["fog"] and t["wet"] > 0.85 for t in wiped), (name, wiped)
        assert sum(t["shown"] == 0 for t in wiped) >= 0.3 * len(wiped), (name, wiped)
        assert all(-1 <= t["across"] - t["w"] <= 3 for t in wiped), (name, [t for t in wiped if not -1 <= t["across"] - t["w"] <= 3])
        fogged = [t for t in wiped if t["fog"] > 0.05 and t["beside"] > 0.05]
        assert len(fogged) >= 5 and all(t["beside"] > t["shown"] for t in fogged), (name, wiped)
        assert sum(t["end"]["wet"] < t["wet"] for t in wiped) >= 0.75 * len(wiped), (name, wiped)   # (fogging over again, slowly)
        # (natural trails through the fog, as they look while fresh: about the bead's width all along (its width as it grew), never
        # a gap; their edges smooth: soft (partly wiped px), each wandering gently down the trail, a px over many rows, never row to
        # row; not all straight)
        fog_trails = [t for t in beads["looks"] if t["kind"] == "fog"]
        assert len(fog_trails) >= 6, (name, beads["looks"])
        for t in fog_trails:
            assert min(t["widths"]) >= 1 and abs(sum(t["widths"]) / len(t["widths"]) - t["w"]) <= 1.5, (name, t)
            # (each edge, on the glass, row by row: shifting a px now and then as the trail wanders, never hopping out and back
            # (a px over for a row, then back: once in a trail at most), never shifting most rows; its soft edge px partly wiped,
            # the fog's own shades between, in most rows)
            edges = [e for e in t["edges"] if e]
            for side in (0, 1):
                edge = [e[side] for e in edges]
                hops = sum(edge[k - 1] == edge[k + 1] != edge[k] for k in range(1, len(edge) - 1))
                moves = sum(a != b for a, b in zip(edge, edge[1:]))
                assert hops <= 1 and moves <= 0.4 * len(edge), (name, t["id"], side, edge)
            assert sum(k >= 1 for k in t["soft"]) >= 0.7 * len(t["soft"]), (name, t["id"], t["soft"])
            # (clean inside: no droplet, glint or other trail's look in it, only the fog's faint shades (FOG's two thinnest at most:
            # residueShades, the residue's own) as residue or as the fog coming back; and no
            # dots: whatever shows inside it, not touching the fog beside it, is a streak of residue down it (3 rows or more, 2 px
            # wide at most, taller than wide), never a lone pixel or a small blob)
            inside = t["inside"]
            assert inside["odd"] == [] and all(1 <= k <= inside["residueShades"][1] for d in inside["patches"] for k in d["shows"]), (name, t["id"], inside)
            dots = [d for d in inside["patches"] if d["tall"] < 3 or d["wide"] > 2 or d["tall"] <= d["wide"]]
            assert dots == [], (name, t["id"], dots)
        # (most wandering, not ruled straight)
        assert sum(len({e[0] for e in t["edges"] if e}) > 1 for t in fog_trails) >= 0.5 * len(fog_trails), (name, fog_trails)
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
    # unwiped (FOG's two faintest shades, or the fine ones between and below them), no more: no droplet left in it)
    assert sum(t["look"] == [0, 0, 0, 0] for t in refog) >= 0.5 * len(refog), refog
    assert all(t["look"] == [0, 0, 0, 0] or ([50, 60, 67] <= t["look"][:3] <= [74, 85, 92] and t["look"][3] <= 69) for t in refog), refog
    # ... light (91 %): a thinner fog than heavy (100 %), the fog first all the same; its tiny ones condensing a little slower (0.95
    # of MISTING, heavy 1.35) and swelling slower on its thinner fog (so more of them stay tiny a while: heavy's glass mostly small
    # beads by then, a small one a whole particle of the budget, a tiny one a quarter); far fewer running
    light, heavy = got["humidityLight"], got["humidity"]["alone"]
    assert light["errors"] == [] and light["popped"] == 0 and light["stray"] == 0, light
    tinies = [t["n"] for t in light["tinies"]]   # (condensing on and on, more and more of them)
    assert tinies[1] < tinies[4] < tinies[8], light["tinies"]
    assert 0.1 < light["fog"]["haze"] < 0.7 * heavy["fog"]["haze"], (light["fog"], heavy["fog"])
    assert light["showing"]["still"] > 0, light["showing"]
    per_s = {k: sorted((s["vol"] - s["r0"] ** 3) / s["fogged"] for s in b["swells"]) for k, b in (("light", light), ("heavy", heavy))}
    assert len(per_s["light"]) >= 3 and per_s["light"][len(per_s["light"]) // 2] < 0.85 * per_s["heavy"][len(per_s["heavy"]) // 2], per_s
    assert light["showing"]["running"] < 0.5 * heavy["showing"]["running"], (light["showing"], heavy["showing"])
    # ... but heavy's glass shows more still beads than light's after the same time (clearly: grown beads cost their whole particle of
    # the budget, so a heavy glass of them showed fewer than light's mostly tiny ones; heavy's budget is the larger)
    still_light, still_heavy = got["stillBeads"]
    assert still_heavy > 1.1 * still_light > 0, got["stillBeads"]
    # the fog dithered irregularly: no grid in its pixels (as gridPeak sees it: under 100; ordered, about 420)
    assert heavy["grid"] < 100, heavy["grid"]
    # ... and only faintly: in fine steps (6 between each two of FOG's 5 shades), dithered a third of a step, it reads as smooth
    # translucency with a faint grain: between pixels side by side, the alpha changing far less than in FOG's 5 shades dithered
    # whole, as before (about 1.5 of 255 against about 5), and hardly ever by a whole shade of those (under 1 %; before, about a
    # third of the time)
    for fog in (light["fog"], heavy["fog"]):
        now, before = fog["grain"]["now"], fog["grain"]["before"]
        assert now["mean"] < 0.4 * before["mean"] and now["jumps"] < 0.01 and before["jumps"] > 0.1, fog["grain"]
    assert light["first"]["after"] >= 2 and light["first"]["fog"] > 0.1 and light["first"]["formed"]["edge"] > 0.1, light["first"]
    # ... then heavy rain on it too (3 minutes): its splashes' droplets left on the fog as tiny beads, a few a landing, staying there
    # (snapping, swept up, growing with the fog as tiny ones do) well past the 40 s they'd last on clear glass, never drying away on
    # the fog (only once a trail is wiped through it under one, 15 s or more after it was left)
    splashed = light["splashed"]
    on_fog = splashed["splashes"]
    assert splashed["s"] >= 170 and splashed["landed"] >= 20 and len(on_fog) >= 3 * splashed["landed"], {k: v for k, v in splashed.items() if k != "splashes"}
    assert all(d["moved"] == 0 for d in on_fog) and sum(d["fogged"] > 45 for d in on_fog) >= 10, on_fog
    assert all(d["glass"] != "fog" and d["fadedAt"] >= 15 for d in on_fog if d["fadedAt"] is not None), on_fog
    assert sum(d["end"] == "on" and d["fogged"] > 45 for d in on_fog) >= 3, on_fog   # (there still, long after)
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
    # grime, glint, droplet) while it clears, and once it has cleared nothing of them on the glass, many still listed (fresh still:
    # about a hundred or more px of them, how many as the beads happened to run just before))
    assert clears["looks"] == 0, clears
    assert clears["cleared"]["trails"] > 60 and clears["cleared"]["ink"] == 0 and clears["cleared"]["looks"] == 0, clears["cleared"]
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
    # ... then its beads leaving trails, and "stop", humidity on again (as a test link after it): the glass fresh too
    marked = got["humidity"]["alone"]["marked"]
    assert marked["trails"] > 30 and marked["ink"] > 0 and marked["cover"] > 0, marked
    started_afresh(got["humidity"]["alone"]["restarted"], "humidity")
    # BEAD_RULES, the physics every bead runs by: its speed on each glass, dry slowest (0.3 of fog's: crawling), fog quicker, a trail
    # fastest (1.6 times fog's: water on water; the user: fog beads move fast down tracks like with rain), for the same size, and down
    # a trail never under TRACK (a freshly landed rain bead's speed there, r 3: a condensed one as quick as a rain one down a track);
    # still under the run size (about sixteen tiny ones' water: r 1.4), from there creeping extremely slowly, rising gradually with
    # its size (then faster and faster, no fastest); after 20 px in 5 s: on fog bigger (however big already: no largest), on a trail
    # the same, on dry glass smaller, a small one shrunk to nothing (gone)
    physics = got["humidity"]["alone"]["physics"]
    speed, after, sizes = physics["speed"], physics["after"], physics["sizes"]
    track = speed["trail"][sizes.index(3)]
    assert track == pytest.approx(1.6 * speed["fog"][sizes.index(3)]) and track == pytest.approx(TRACK, abs=0.1), speed
    for i, r in enumerate(sizes):
        if r < physics["run"]:   # (under the size a bead runs from, about large: it stays put on any glass)
            assert speed["dry"][i] == speed["trail"][i] == speed["fog"][i] == 0, (r, speed)
        else:
            assert speed["dry"][i] < speed["fog"][i] < speed["trail"][i], (r, speed)
            assert speed["trail"][i] == pytest.approx(max(1.6 * speed["fog"][i], track)), (r, speed)
            assert speed["dry"][i] == pytest.approx(0.3 * speed["fog"][i]), (r, speed)
        assert after["fog"][i] > r and after["trail"][i] == r and after["dry"][i] < r, (r, after)
    assert [v for r, v in zip(sizes, speed["trail"]) if physics["run"] <= r <= 3] == [track] * 4, speed["trail"]   # (r 1.4-3: TRACK)
    for g in ("dry", "trail", "fog"):
        moving = [v for r, v in zip(sizes, speed[g]) if r >= (physics["run"] if g != "trail" else 3)]   # (from the run size up: under it, still)
        assert speed[g] == sorted(speed[g]) and len(set(moving)) == len(moving) >= (8 if g != "trail" else 5), (g, speed[g])
        steps = [b / a for a, b in zip(moving, moving[1:])]
        assert all(1 < k < 15 for k in steps), (g, steps)   # (gradually: no jump from creeping to running)
    assert 1.35 <= physics["run"] <= 1.45, physics["run"]   # (about sixteen tiny ones' water)
    assert physics["sizes"][:5] == [0.6, 1, 1.1, 1.3, 1.4] and physics["run"] <= 1.4, physics   # (r 0.6 to 1.3 still; 1.4 running, creeping)
    assert speed["fog"][3] == 0 and 0.2 < speed["fog"][4] < 0.6 and speed["fog"][-1] > 100, speed["fog"]
    assert after["dry"][0] == 0 and after["fog"][-1] > sizes[-1], after
    # heavy rain's beads: light rain none; heavy, a drop sticking every few s (one in 3 s), a dozen or so (14) on the glass at most, each landing at once
    # (no fading in) as a large bead (r 2.6 to 2.97: smaller than the 3 to 3.8 they were; 2.97 is 3.3 less 10 %, the top end of the range only; the large sprite class, r 2.5 or more), hard: squashed flat and wide a frame or two, wobbling back to round, with a splash
    # (a ring of 14 to 20 droplets thrown out fast, lopsided, some of them spray streaks, 4 to 8 of them left on the glass as tiny
    # beads); on dry glass crawling (about 0.3 of its speed on fog, the same size), down a trail at TRACK or more (its own size's
    # speed there), shrinking only (its water left behind), never growing; none vanishing; their wet streaks on the
    # clear glass drying over 3 to 10 minutes, each its own: thin streaks (a third of the bead's width or less: a px or two), faint
    # (a greyish film, grime along an edge, a glint, a tiny droplet), uneven along their length (thicker and thinner, now and then
    # broken for a px or three, never more, no regular pattern of gaps: no dashes), their thin parts drying first (gone from view
    # well before the trail is dry), the thick ones last. The rain light again: none sticking; those there running off or shrinking away
    sticking = got["rainBeads"]
    assert sticking["errors"] == [] and sticking["light"] == {"landed": 0, "levels": 0.35}, sticking
    heavy_rain = sticking["heavy"]
    # (75 s of heavy rain at one in 3 s: about 26; more than the handful (5) there were at most before, up to 14)
    assert 12 <= heavy_rain["landed"] <= 42 and 6 <= heavy_rain["rainMost"] <= 14 and heavy_rain["popped"] == 0, heavy_rain
    # (each landing seen: shown at once, or, landing on a bead already there, merged into it at once (in its absorbed))
    assert len(sticking["stuck"]) == heavy_rain["landed"], sticking["stuck"]
    assert all(2.6 <= s["r"] <= 2.97 and (s["alpha"] == 1 or s.get("merged")) and 14 <= s["dots"] <= 20 and len(s["thrown"]) == s["dots"] and s["phase"] == "heavy"
               for s in sticking["stuck"]), sticking["stuck"]
    assert len({s["dots"] for s in sticking["stuck"]}) >= 4, sticking["stuck"]
    # (smaller than they were (r 3 to 3.8, mean 3.4): the range now 2.6 to 2.97, so a mean of 2.785 (midpoint), 18 % under that; and shrinking slower as they run on
    # dry glass (a rain bead leaves half the water a px that others do): the run each would make down dry glass, till too small to run,
    # (r squared goes down a steady 4/3 of LEAVE (0.1) a px: (r2 - RUN_R2) / (4/3 * 0.1) for the old mean) at least 1.2 times as far as the old (1.3 before the top end of the radii came down 10 %: the median run is about 90 px, 1.25 times)
    # beads' (the median, over the beads that landed)
    radii = [s["r"] for s in sticking["stuck"]]
    mean_r = sum(radii) / len(radii)
    # (now by the rain's drops: the page's rain here a test link's, a nominal 5 mm/h, D0 1.26 mm: 2.6 + 0.37 * (0.362 / 1.3) = 2.703, every bead the same)
    assert mean_r == pytest.approx(2.6 + 0.37 * (0.9 * 5 ** 0.21 - 0.9) / 1.3, abs=0.005) and max(radii) - min(radii) < 1e-9, mean_r
    old_run = (3.4**2 - physics["run"] ** 2) / (4 / 3 * 0.1)   # (about 72 px)
    new_run = sorted(sticking["runs"])[len(sticking["runs"]) // 2]
    assert len(sticking["runs"]) == len(radii) and new_run >= 1.1 * old_run, (new_run, old_run, sticking["runs"])
    # (the burst lopsided: its droplets' mean throw a fifth or so of their speed or more one way, a direction of its own each landing;
    # some of them (a third or so all told, at least two a splash) spray streaks; squashed 0.5 to 0.75 wider at first)
    for s in sticking["stuck"]:
        vx, vy = (sum(v * f(a) for a, v, *_ in s["thrown"]) for f in (math.cos, math.sin))
        s["lean"], s["aim"] = math.hypot(vx, vy) / sum(v for _, v, *_ in s["thrown"]), math.atan2(vy, vx)
    assert all(s["lean"] >= 0.12 for s in sticking["stuck"]), [s["lean"] for s in sticking["stuck"]]
    assert len({int((s["aim"] + math.pi) // (math.pi / 2)) for s in sticking["stuck"]}) == 4, [s["aim"] for s in sticking["stuck"]]
    sprays = [sum(t[2] for t in s["thrown"]) for s in sticking["stuck"]]
    assert min(sprays) >= 2 and 0.2 <= sum(sprays) / sum(s["dots"] for s in sticking["stuck"]) <= 0.5, sprays
    assert all(0.5 <= s["squash"] <= 0.75 for s in sticking["stuck"]), sticking["stuck"]
    # (followed frame by frame (a 30th of a second each) as they land: as wide as that, as much shorter, held a frame or two; then
    # within 0.2 s wobbling back (taller and narrower a moment), round again from there on (but elongated as it sets off, as below); its water no different for it (but what
    # it leaves on dry glass as it runs, or a tiny bead it sweeps up))
    landings = sticking["landings"]
    assert len(landings) == 4 and all(len(l["frames"]) >= 10 for l in landings), landings
    for l in landings:
        f = l["frames"]
        held = [x for x in f if x["impact"] < 0.05]
        assert f[0]["impact"] == 0 and 1 <= len(held) <= 2 and all((x["w"], x["h"]) == (f[0]["w"], f[0]["h"]) for x in held), l
        assert f[0]["w"] >= 1.4 * f[0]["rw"] and f[0]["h"] <= 0.7 * f[0]["rh"], l
        assert any(x["w"] < x["rw"] and x["h"] > x["rh"] for x in f if 0.05 <= x["impact"] < 0.2), l
        assert all((x["w"], x["h"]) == (x["rw"], x["rh"]) for x in f if x["impact"] >= 0.2 - 1e-9 and not x["stretch"]), l   # (round, but as
                                                                                                                       # it sets off, elongating)
        assert all(0.75 <= x["w"] * x["h"] / (x["rw"] * x["rh"]) <= 1.3 for x in f), l
        # (till it takes in another bead as it settles (its size stepping up: that bead's water joins it), as one of these landings, seeded, now does)
        assert all(abs(x["vol"] - f[0]["vol"]) <= 0.03 * f[0]["vol"] for x in f if x["impact"] < 0.2 and x["rw"] == f[0]["rw"]), l
    # (4 to 8 of its droplets meant to stay, all of them left but those thrown off the window or into a flood (off), as many as the
    # beads' own most had room for as it landed (room): none cut while there was room, every one past it cut)
    assert all(4 <= s["meant"] <= 8 and s["stay"] + s["cut"] + s["off"] == s["meant"] and s["stay"] == min(s["meant"] - s["off"], s["room"])
               for s in sticking["stuck"]), sticking["stuck"]
    # (... where there was room, 4 or more left, as ever, but one now and then thrown off the window by a drop landing near its edge
    # (scattered further out, more of the landings lose one so, but hardly any of their droplets))
    roomy = [s for s in sticking["stuck"] if s["room"] >= s["meant"]]
    assert roomy and all(s["stay"] >= 4 for s in roomy if not s["off"]) and sum(s["off"] for s in roomy) <= 0.1 * sum(s["meant"] for s in roomy), sticking["stuck"]
    assert len({s["meant"] for s in sticking["stuck"]}) >= 3, sticking["stuck"]
    dry, trail = heavy_rain["glass"]["dry"], heavy_rain["glass"]["trail"]
    assert dry["lost"] > 0 and dry["gained"] == 0 and 0.2 < dry["all"] / dry["norm"] < 0.4, dry
    assert trail["moves"] > 100 and all(0.75 < o["all"] / o["pace"] < 1.3 for o in (dry, trail)), heavy_rain["glass"]
    assert 0.75 * TRACK < trail["px"] / trail["s"] < 1.3 * 31, trail   # (r 1.8-3.2 down a trail: about TRACK, not their size's)
    # (the splash's droplets left on the glass: tiny beads (r 0.55) like any other, from the splash's end on, as many as were left;
    # never moving but to snap (merged into a neighbour, or swept up by a runner passing: as many as eight thrown out, now and then
    # three of them together at most); on the clear glass, nothing taking them, drying away after 15 to 40 s each)
    left_on = sticking["watched"]["splashes"]
    assert sum(s["stay"] for s in sticking["stuck"]) - 8 <= len(left_on) <= sum(s["stay"] for s in sticking["stuck"]), (len(left_on), sticking["stuck"])
    assert all(d["moved"] == 0 and d["r"] < 0.85 for d in left_on) and sum(d["tiny"] for d in left_on) >= 0.9 * len(left_on), left_on
    assert all(d["r"] == pytest.approx(0.55) for d in left_on if d["tiny"]), left_on   # (no fog to swell with: as left, tiny)
    ends = {end: [d for d in left_on if d["end"] == end] for end in ("faded", "taken", "on", "other")}
    assert len(ends["faded"]) >= 10 and len(ends["taken"]) >= 10, {end: len(d) for end, d in ends.items()}
    assert all(15 <= d["fadedAt"] <= 41 and d["glass"] != "fog" for d in ends["faded"]), ends["faded"]
    assert len({round(d["fadedAt"]) for d in ends["faded"]}) >= 8, ends["faded"]   # (each its own time)
    # (thrown past the drop's edge every way, up and down too: none landing on its drop, or close enough to snap back into it at
    # once (they did, about one in ten); those thrown up or to the sides (no further down than half as far across) hardly ever
    # taken back the moment they're left (by a runner passing); those below it may be: its drop, running at once, runs down through
    # them)
    # (each left in the heavy rain, as followed here and on, below, as its biggest beads set off again: every one once)
    every = list({d["id"]: d for d in left_on + sticking["stretching"]["splashes"]}.values())
    across = [d for d in every if d["thrown"][1] <= abs(d["thrown"][0]) / 2]
    assert len(every) >= 120 and len(across) >= 60, every
    assert sum(d["end"] == "taken" and d["aged"] <= 0.45 for d in across) <= 0.08 * len(across), across   # (8 %: 5.7 % with the beads all the nominal 5 mm/h rain's 2.70 px, not 2.6 to 2.97 as they were: 5 % was the old mix's)
    # (none outstaying its drying (shown 0.4 s in, 40 s at most, half a second drying away): gone, a merged one shrunk away)
    assert all(d["aged"] <= 42 for d in ends["on"] + ends["other"]), left_on
    # (the biggest (r 2.5 and up: here most) setting off again after a pause (heavy rain's own time, 30 s): about one restart in three
    # elongating a little: a row or two taller, a column narrower, its lower edge where it was, eased in and held, round again within
    # 0.75 s or so (sooner, paused again); never a smaller one)
    stretching = sticking["stretching"]
    stretches = stretching["stretches"]
    assert stretching["s"] == pytest.approx(30, abs=0.5) and stretching["restarts"] >= 15, {k: v for k, v in stretching.items() if k != "splashes"}
    assert 0.15 * stretching["restarts"] <= stretching["begun"] <= 0.55 * stretching["restarts"], stretching
    assert stretching["begun"] - 2 <= len(stretches) <= stretching["begun"] and stretching["small"] == 0, stretching
    assert all(e["r"] >= 2.5 and 0 < e["most"] <= 1 and 0 <= e["rows"] <= 2 and 0 <= e["cols"] <= 1 and e["low"] == 0 for e in stretches), stretches
    assert sum(e["most"] == 1 and e["rows"] >= 1 and e["cols"] == 1 for e in stretches) >= 3, stretches   # (all the way, now and then)
    assert all(e["lasted"] is None or e["lasted"] <= 0.85 for e in stretches) and sum(e["lasted"] is not None for e in stretches) >= 3, stretches
    # (a bead that has steered into another's run and reached it locks on: it follows the run's middle row by row, wherever it
    # wanders, to the bottom (never off partway: none let go with the run still under or just ahead of it), till it merges, runs
    # off the bottom, or the run ends)
    locks = stretching["locks"]
    assert len(locks) >= 2 and sum(k["rows"] for k in locks) >= 24, locks   # (30 before the rain beads landed smaller (seeded: 27 with the top end of their radii 10 % less))
    assert all(k["off"] <= 3 for k in locks), locks   # (3: 2.4 at most with the beads all the nominal rain's 2.70 px, seeded; it was 2)
    assert not any(k["dropped"] for k in locks), locks
    assert heavy_rain["glass"]["fog"]["moves"] == 0, heavy_rain["glass"]   # (no fog: rain alone)
    drying = [t["lasted"] for t in sticking["drying"]]
    assert len(drying) >= 2 and all(170 <= t <= 610 for t in drying), sticking["drying"]
    assert all(t["look"][:3] in STREAK + DROPLET and 0 < t["look"][3] <= (150 if t["look"][:3] in DROPLET else 80) for t in sticking["drying"]), sticking["drying"]
    hidden = sorted(t["hidden"] for t in sticking["drying"])
    assert hidden[0] < 0.75 and hidden[-1] > 0.7, sticking["drying"]
    assert all(t["kind"] == "clear" for t in sticking["looks"]), sticking["looks"]
    streaks = list({t["id"]: t for t in reversed(sticking["looks"])}.values())   # (each bead's first look, as it had run 20 px)
    whole = {t["id"]: t for t in _whole_streaks(sticking["looks"])}   # (... and all its looks together: its trail as far as it ran)
    assert len(streaks) >= 2, sticking["looks"]
    # (thin: all told a third of the beads' width or less; each a px or two (2 at most on the whole, but where it paused: a little
    # wider there, a blob; a bead shrunk on dry glass to under 6 px wide keeps its streak's width, so a third of its own width may
    # be under 2); a blob)
    rows = [w for t in streaks for w in t["widths"]]
    assert sum(rows) / len(rows) <= sum(t["w"] for t in streaks) / len(streaks) / 3, streaks
    # (their width where they ran, on the whole: over all their trails, as far as each ran, each row held to its own bead's most
    # (a mean over one bead's rows alone, a dozen or two of them, is a px or two's wobble)); a px or two everywhere
    ran, most = [], 0
    for t in whole.values():
        rows = [w for w, paused in zip(t["widths"], t["paused"]) if not paused]
        ran += rows
        most += len(rows) * (2 if t["w"] >= 5.5 else max(1.6, t["w"] / 3))
    assert len(ran) >= 100 and sum(ran) <= most, list(whole.values())
    for t in streaks:
        widths = t["widths"]
        assert max(widths) <= t["w"] / 2 + 1.5, t
        assert len(set(widths)) >= 2 and max(widths) - min(widths) >= 1, t                              # (uneven along it)
        empty = [w == 0 for w in widths]
        runs = [len(r) for r in "".join("x" if e else "." for e in empty).split(".") if r]
        assert sum(empty) <= 0.3 * len(widths) and max(runs, default=0) <= 3, t                         # (broken a little, at most)
        starts = [i for i, e in enumerate(empty) if e and (i == 0 or not empty[i - 1])]
        assert len(starts) < 3 or len({b - a for a, b in zip(starts, starts[1:])}) > 1, t               # (no regular dashes)
    eased, gone = sticking["eased"], sticking["after"]
    assert gone["landed"] == eased["landed"] and gone["beads"] == 0 and gone["popped"] == 0, (eased, gone)
    # (each run off or shrunk to nothing, or merged into one that was: none dried away otherwise but the splashes' droplets, each
    # after 15 to 40 s (some within a frame there: gone between two looks), unless swept up first by a runner passing (taken), as
    # ever; two snapped together (too small to run) the same, or shrunk away first on dry glass, never faded)
    assert gone["ranOff"] + gone["shrunk"] > eased["ranOff"] + eased["shrunk"] and eased["beads"] > 0, (eased, gone)
    dried = sticking["afterWatched"]["splashes"]
    assert dried and all(d["end"] in ("faded", "other", "taken") and d["aged"] <= 42 and d["glass"] != "fog" for d in dried), dried
    shrunk = [d for d in dried if d["end"] == "other" and d["fadedAt"] is None and not d["tiny"]]
    assert len(shrunk) <= gone["shrunk"] - eased["shrunk"], shrunk
    assert gone["faded"] - eased["faded"] == sum(d["end"] != "taken" for d in dried) - len(shrunk) > 0, (eased, gone, dried)
    assert all(15 <= d["fadedAt"] <= 41 for d in dried if d["fadedAt"] is not None), dried
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
    assert phone["strip"] == "last reading 18:42" and phone["right"], phone
    assert phone["apart"] and phone["onScreen"], phone
    # "reset preferences" on its own, left of the switches (or above them), on screen, right of the test row or clear of it
    assert phone["resetRight"] and phone["resetAfterRow"], phone
    # "stop" at the bottom left, just right of the test row's last link (with the row above the strip on a phone, both clear
    # of it); without the test row (the page as it will be), at the bottom left on its own, at 1024x768 and on a phone
    corner = got["phoneCorner"]
    assert corner["link"] == "stop" and corner["apart"] and corner["onScreen"], corner
    just_right_of_the_row(corner)
    # (above the strip; the units' four switches there too, at the right, too wide for the row beside them: just above those)
    # (the strip is only the time now: the units' switches fit beside it at the bottom, "reset preferences" (a tile is hidden) on a line
    # of its own just above them, at the right; the test row and "stop" too wide for what's left of that line: on the line above it)
    assert corner["units"]["bottom"] == corner["window"][1] and corner["units"]["right"] == pytest.approx(corner["strip"]["left"], abs=0.5), corner
    assert corner["reset"]["right"] == corner["window"][0] and corner["reset"]["bottom"] == pytest.approx(corner["units"]["top"], abs=0.5), corner
    assert corner["motion"]["bottom"] == pytest.approx(corner["reset"]["top"], abs=0.5), corner
    # (no "test:" there, all of it unscrolled, room for one more short link: as with every effect on, in the links' test)
    assert not corner["tag"] and corner["fits"] and corner["spare"] >= 30, corner
    for bare in (got["bare"], got["barePhone"]):
        assert bare["errors"] == [] and bare["row"] is None and bare["link"] == "stop", bare
        assert bare["motion"]["left"] == 0 and bare["apart"] and bare["onScreen"], bare
        if bare is got["bare"]:   # (the bottom left, on its own)
            assert bare["motion"]["bottom"] == bare["window"][1], bare
        else:   # (a phone: the bottom row is the switches and the time; "stop" on the line above, left of "reset preferences")
            assert bare["motion"]["bottom"] == pytest.approx(bare["reset"]["bottom"], abs=0.5) and bare["motion"]["right"] <= bare["reset"]["left"], bare
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
    assert set(made["forms"]) == {"fern", "plume", "cluster", "needles", "speck"}, made["forms"]   # (no star, no plate)
    assert all(made["forms"][k] >= 5 for k in ("fern", "plume", "cluster", "needles")) and made["scratches"] >= 1, made["forms"]
    specks = made["specks"]
    assert len(specks) == made["forms"]["speck"] >= 5 and set(specks) <= {1, 2} and 1 in specks and 2 in specks, specks
    assert len(specks) < cold["size"][0] * cold["size"][1] / 1000, (len(specks), cold["size"])   # (sparse)
    q = lambda v, p: sorted(v)[int(p * (len(v) - 1))]
    lengths = [f["length"] for f in made["fronds"]]
    spacings, angles = [f["spacing"] for f in made["fronds"] if f["spacing"]], [f["angle"] for f in made["fronds"] if f["angle"]]
    assert len(lengths) >= 40 and q(lengths, 0.9) > 5 * q(lengths, 0.1) and max(lengths) > 60, sorted(lengths)
    assert len(spacings) >= 30 and q(spacings, 0.9) > 2.5 * q(spacings, 0.1), sorted(spacings)
    assert 55 <= q(angles, 0.5) <= 65 and q(angles, 0.1) > 45 and q(angles, 0.9) < 75 and q(angles, 0.9) - q(angles, 0.1) > 6, sorted(angles)
    # sizes spread wide and skewed: a few long trunks (the longest many times the median), most medium, many tiny
    assert max(lengths) >= 4 * q(lengths, 0.5) and q(lengths, 0.5) < 25 and q(lengths, 0.95) > 3 * q(lengths, 0.5), sorted(lengths)
    # ... the long trunks (the longest tenth of the stems and side fronds) curve rarely, the finer fronds (the shorter half) 3 times
    # as much or more (mean turning per px)
    strands = sorted(made["strands"], key=lambda t: -t["length"])
    turn = lambda part: sum(t["turn"] for t in part) / len(part)
    longest, finest = strands[: len(strands) // 10], strands[len(strands) // 2:]
    assert len(strands) >= 400 and turn(finest) >= 3 * turn(longest), (turn(finest), turn(longest))
    trunks = [t for t in longest if t["depth"] == 0]
    assert len(trunks) >= 20 and sum(t["turn"] > 0.01 for t in trunks) < 0.25 * len(trunks) and turn(finest) > 0.05, (turn(finest), turn(longest))
    # ... each one's core whiter than its edge (the spine against its rim and barbs, as they read over the glass)
    assert shades["core"] >= 1.15 * shades["edge"] > 0, shades
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
    # the sun (?fx=heat,uv): a small white ball on a clean elliptical arc along the strip above the first row of tiles, x = W / 2 + 0.42 W
    # cos(pi t), y = yLo - yLo sin(noon elevation) / sin(75.6) sin(pi t): from its low point at sunrise and sunset to its high point at noon
    # (the season's noon elevation scaling the rise: at the summer solstice's noon its centre is on the window's top edge, half the ball
    # showing), the ball whole in the window except near the apex of the highest arcs, at most half cut by the top edge; nothing outside daylight
    hot = got["heat"]
    assert hot["errors"] == [], hot["errors"]
    assert sorted(hot["arcs"]) == ["equinox", "summer", "winter"], sorted(hot["arcs"])
    for day, rows in hot["arcs"].items():
        w, h = rows[0]["size"][:2]
        up = [r for r in rows if r["day"]]
        assert len(up) > 100 and all(0 <= r["t"] <= 1 for r in up) and not any(0 <= r["t"] <= 1 for r in rows if not r["day"]), (day, len(up))
        assert rows[0]["day"] is False and rows[-1]["day"] is False and up[0]["t"] < 0.01 and up[-1]["t"] > 0.99, day
        sin_el = math.sin(math.radians(rows[0]["noon"]))
        for r in up:   # (the clean arc: its own formula, the ball whole in the window, its centre in the strip above the first row)
            b = r["band"]
            assert r["x"] == pytest.approx(w / 2 + 0.42 * w * math.cos(math.pi * r["t"]), abs=1e-6), (day, r)
            assert r["y"] == pytest.approx(b["yLo"] - b["yLo"] * min(1, sin_el / math.sin(math.radians(75.6))) * math.sin(math.pi * r["t"]), abs=1e-6), (day, r)
            assert r["r"] == pytest.approx(b["R"]) and b["R"] <= 0.04 * w + 1e-9 and b["R"] >= 2.6 - 1e-9, (day, r)
            assert r["x"] - b["Rmax"] >= 0 and r["x"] + b["Rmax"] <= w, (day, r)   # (the glow too: whole across the window)
            assert r["y"] >= -1e-9 and r["y"] <= b["yLo"] and r["y"] - r["r"] >= -r["r"] - 1e-9 and b["S"] <= 0.16 * h + 1e-9 and r["y"] + b["Rmax"] < 0.2 * h, (day, r)
            if day == "winter":   # (a winter noon well inside the window: the ball whole)
                assert r["y"] - b["Rmax"] >= 0 or r["t"] < 0.1 or r["t"] > 0.9 or r["y"] - r["r"] > 1, (day, r)
            if r["t"] < 0.08 or r["t"] > 0.92:   # (at sunrise and sunset: low in the strip, fully visible)
                assert r["y"] - r["r"] > 1, (day, r)
            if b["top"] > b["S"] + 1:   # (tiles start below the strip: the ball and its pink halo, 3 R out, wholly above the first row)
                assert r["y"] + 3 * b["R"] <= b["top"] and r["y"] < b["top"], (day, r)
        xs = [r["x"] for r in up]
        assert all(b < a for a, b in zip(xs, xs[1:])), (day, "x not falling")                     # (monotone: no stall; east, the screen's right, to west, the left)
        assert xs[0] > 0.8 * w and xs[-1] < 0.2 * w and abs(xs[len(xs) // 2] - w / 2) < 0.03 * w, (day, xs[0], xs[-1])   # (sunrise on the right, noon in the middle, sunset on the left)
        assert all(r["x"] - r["band"]["Rmax"] > 1 and r["x"] + r["band"]["Rmax"] < w - 1 for r in up), day   # (no flat run along a side border)
        apex = min(r["y"] for r in up)
        if day == "summer":   # (the highest noon of the year: the centre on the top edge, half the ball showing)
            assert apex == pytest.approx(0, abs=0.35), (day, apex)
        if day == "winter":
            assert apex > 0.4 * up[0]["band"]["yLo"] and apex - up[0]["r"] > 1, (day, apex)
        # (the direction of travel turns smoothly: no step of the path turns more than 0.35 rad from the one before)
        turns = []
        for p, q, s in zip(up, up[1:], up[2:]):
            a1, a2 = math.atan2(q["y"] - p["y"], q["x"] - p["x"]), math.atan2(s["y"] - q["y"], s["x"] - q["x"])
            turns.append(abs((a2 - a1 + math.pi) % (2 * math.pi) - math.pi))   # (the arc runs leftward: the angle wraps at pi)
        assert max(turns) < 0.35, (day, max(turns))
    # the real dashboard at 640 x 480 has its first row 8 px down: the strip is then its minimum (0.06 H); at 1024 x 768 the reach rows below
    # have a strip of 24 art px: the arc's rise by the season there
    # a very wide window (1600 x 400): the arc a flat run along the strip at most, whole all day, symmetric about noon, the same x as before
    wide = [r for r in hot["wide"] if r["day"]]
    w, h = wide[0]["size"][:2]
    assert w / h > 3.5 and len(wide) > 100, (w, h)
    assert all(r["x"] - r["band"]["Rmax"] >= 0 and r["x"] + r["band"]["Rmax"] <= w and r["y"] >= -1e-9 and r["y"] + r["band"]["Rmax"] < 0.2 * h for r in wide), min(r["y"] - r["r"] for r in wide)
    assert all(r["x"] == pytest.approx(w / 2 + 0.42 * w * math.cos(math.pi * r["t"]), abs=1e-6) for r in wide)
    assert max(abs(a["y"] - b["y"]) for a, b in zip(wide, wide[::-1])) < 0.02 * h                         # (symmetric about solar noon)
    summer_ry, winter_ry = (min(r["y"] for r in hot["arcs"][d] if r["day"]) for d in ("summer", "winter"))
    assert summer_ry < hot["arcs"]["equinox"][len(hot["arcs"]["equinox"]) // 2]["y"] and summer_ry < winter_ry, (summer_ry, winter_ry)   # (higher in summer)
    # the reach: nothing the sun draws (glow, ember, beams) lit below 0.34 of the window's height, at five window sizes (a phone's too),
    # places of the sun, UV levels and heats; the beams there (with the UV) and some light drawn everywhere
    assert len(hot["reach"]) == 5 * 5 * 5, len(hot["reach"])
    for r in hot["reach"]:
        w, h = r["size"][:2]
        assert r["lit"] > 0 and r["low"] < 0.34 * h, r
        if r["uv"] >= 0.6:
            assert r["beams"] == 12, r
        if r["uv"] == 1 and r["heat"] == 1 and r["name"] == "summer noon":
            assert r["low"] > 0.5 * r["band"]["cap"] and r["low"] > 0.2 * h, r   # (straight down at noon the beams really reach: over half the way to the line, and past the old fifth)
    # ... nothing drawn at night, the sun up at midday
    assert hot["night"]["early"] == {"ink": 0, "day": False} and hot["night"]["late"] == {"ink": 0, "day": False}, hot["night"]
    assert hot["night"]["noon"]["day"] and hot["night"]["noon"]["ink"] > 500, hot["night"]
    # the ball: a blown-out white ball with no disc edge: its brightness (the mean at each whole pixel of distance from the centre) never
    # rises going out and no step of one pixel is more than a sixth of the full scale (a disc's rim would be), white at its centre at
    # 43 C (every channel high) and cream at 35 C, the ball much larger at 43 C than 35 C (the radius of half its brightness), its glow
    # reaching 3.4 R to 6 R (R (3.4 + 2.6 heat)) and gone beyond that
    cool, hotter = hot["balls"]
    assert cool["level"] == pytest.approx(0.3) and hotter["level"] == 1 and cool["drawn"] <= cool["cap"] and hotter["drawn"] <= hotter["cap"], hot["balls"]
    for ball in hot["balls"]:
        lum = ball["lum"]
        assert ball["Rh"] == pytest.approx(ball["r"] * (3.4 + 2.6 * ball["level"])), ball["Rh"]
        assert all(b <= a + 4 for a, b in zip(lum, lum[1:])), lum                                  # (never rising outward: the dither's grain aside)
        assert max(a - b for a, b in zip(lum, lum[1:])) < 0.4 * 255, lum                          # (no rim: a ball 1.6 art px across its core falls over a few pixels, no step over 0.4 of the scale)
        assert max(ball["centre"]) > 240 and max(lum[int(ball["Rh"]) + 4:]) < 3, (lum, ball["centre"])   # (bright at the middle; nothing past the reach)
    assert min(sum(hotter["rgb"][q][c] for q in range(2)) / 2 for c in range(3)) > 225, hotter["rgb"][:4]   # (white: every channel high)
    assert cool["centre"][0] > 240 and cool["lum"][2] < hotter["lum"][2], (cool["centre"], cool["lum"], hotter["lum"])   # (hot at its middle, dimmer round it than at 43 C)
    assert hotter["half"] > 1.8 * cool["half"] > 0 and hotter["Rh"] > 1.35 * cool["Rh"], (hotter["half"], cool["half"])
    # ... and its ember: a dark-orange flare outside the ball, there at 43 C but subtle (its field's largest value, the page showing 0.45 of
    # it: under 0.6 and over 0.1), dimmer at 35 C
    assert 0.1 < hotter["ember"] < 0.6 and cool["ember"] < 0.6 * hotter["ember"], (hotter["ember"], cool["ember"])
    # no glow pixel on or beyond the edge of the ball's box (Rh + 3 from the sun), nor of the ember bloom's (the picture's own box), at the
    # sun's places through a summer and a winter day, the box's sides inside the window (its sides at the window's edge cut by it)
    assert len(hot["edges"]) == 14, hot["edges"]
    for look in hot["edges"]:
        assert look["ball"]["n"] == 0 and look["all"]["n"] == 0 and look["ball"]["sides"] >= 2 and look["all"]["sides"] >= 2, look
    # the beams: twelve, as wide at the base as their length says (0.26 of the longest tip: BEAM_BL 0.13 each side, within 20 %), their lengths varying, blunt
    beams = hot["beams"]
    assert beams["count"] == 12 and len(beams["widths"]) == 12 and "lobe" not in beams and beams["edge"] == 34, beams
    for width in beams["widths"]:
        assert width == pytest.approx(0.26 * max(beams["lengths"]), rel=0.2), (width, beams["lengths"])
    # ... spaced about 30 degrees all round (each one off its place by up to 3.5 degrees: a gap within 13 degrees of even)
    angles = sorted(a % 360 for a in (b for b in hot["alike"][0] for b in [b["angle"]]))
    gaps = [(b - a) for a, b in zip(angles, angles[1:] + [angles[0] + 360])]
    assert all(17 < g < 43 for g in gaps), gaps
    # the UV: none below index 6, the violet rising with it (never falling) to full at 11, none drawn at the lowest
    amounts = {a["index"]: a for a in hot["amounts"]}
    assert amounts[5.9]["amp"] == 0 and amounts[5.9]["violet"] == 0 and amounts[5.9]["beams"] == 0, amounts[5.9]
    assert amounts[6]["amp"] == 0 and amounts[6]["violet"] == 0, amounts[6]
    amps = [a["amp"] for a in hot["amounts"]]
    assert all(b >= a for a, b in zip(amps, amps[1:])) and amounts[11]["amp"] == pytest.approx(1) and amounts[12]["amp"] == pytest.approx(1), amps
    assert 0 < amounts[6.5]["amp"] < amounts[7]["amp"] < amounts[8]["amp"] < amounts[9]["amp"] < amounts[10]["amp"] < amounts[11]["amp"], amps
    violet = [a["violet"] for a in hot["amounts"]]
    assert all(b > a for a, b in zip(violet[1:7], violet[2:7])) and min(violet[-3:]) > 0.8 * violet[6] and violet[-3] > 5 * violet[2], str(violet)   # (more of it drawn; the top flat)
    assert amounts[7]["amp"] == pytest.approx((0.3 + 0.7 * 0.2) * 0.7407, rel=0.15), amounts[7]                       # (0.44 smoothstep(6, 7.5, 7))
    # no lobe: the real sun's place shows nowhere in the beams (their angles, lengths and strengths the same at every place of the sun), and
    # every beam alike: lengths within the 0.9 to 1.1 jitter, strengths within 0.9 to 1.1
    for look in hot["alike"][1:]:
        assert look == hot["alike"][0], (look, hot["alike"][0])
    lens = [b["length"] for b in hot["alike"][0]]
    assert max(lens) < 1.3 * min(lens) and max(b["bright"] for b in hot["alike"][0]) < 1.3 * min(b["bright"] for b in hot["alike"][0]), hot["alike"][0]
    # the ends: blunt, their brightness falling smoothly to zero over the last 35 % of the length (never rising again from 0.4 on: no hard
    # cut-off), nothing at the very end, in the picture too (the violet along the longest: fainter toward the end, none past it)
    ends = hot["ends"]
    for profile, end, start in zip(ends["profiles"], ends["ends"], ends["start"]):
        assert all(b <= a + 1e-9 for a, b in zip(profile, profile[1:])), profile
        assert end < 1e-6 and end < 0.01 * start and profile[-3] < 0.1 * profile[0], (profile, end, start)
        assert max(abs(b - a) for a, b in zip(profile, profile[1:])) < 0.3 * profile[0], profile   # (no step in it)
    seen = [v for v in ends["violet"] if v["inside"]]
    assert len(seen) >= 4 and seen[0]["v"] > seen[1]["v"] * 0.9 and seen[1]["v"] > seen[2]["v"] and seen[-1]["v"] < 0.25 * seen[0]["v"], ends["violet"]
    # the shafts: distinct, not a puff: at half the longest reach the violet round the sun (by angle over the lower half) has at least five
    # separate peaks (the beams 30 degrees apart: 0, +-30, +-60 about straight down) with valleys between them under 0.98 of the lower neighbouring peak (the red and green channels' sum, at 1024 x 768)
    sh = hot["shafts"]
    prof = sh["prof"]
    peaks = [i for i in range(1, len(prof) - 1) if prof[i] >= prof[i - 1] and prof[i] > prof[i + 1] and prof[i] > 0.3 * max(prof)]
    strong = [i for i in peaks if all(abs(i - j) >= 15 for j in peaks if prof[j] > prof[i])]   # (one per beam: at least 15 degrees from a higher one)
    assert len(strong) >= 5, (strong, [round(v, 1) for v in prof])
    strong.sort()
    for a_, b_ in zip(strong, strong[1:]):
        assert min(prof[a_:b_ + 1]) < 0.98 * min(prof[a_], prof[b_]), (a_, b_, [round(v, 1) for v in prof])
    # the longest beam about twice the old reach (0.20 H): over 1.7 times the room the old cap left (0.20 H - 3 - y - R)
    assert sh["longest"] > 1.7 * (0.20 * sh["h"] - 3 - sh["y"] - sh["r"]) * 0.8, sh
    # ... faded out short of the window's edge: no violet in its outer ring
    assert hot["edge"]["violet"] == 0 and hot["edge"]["whole"] > 100, hot["edge"]
    # no dot pattern in the dither (its grid power), no flash faster than 0.5 Hz (the brightness over 24 s: its dominant frequency, the heat
    # alone and with the UV), none drawn stronger than its strength under the cap
    assert all(g < 400 for g in hot["grid"]), hot["grid"]
    for flash in hot["flash"]:
        assert flash["dominant"] <= 0.5 and flash["maxAlpha"] <= flash["drawn"] + 0.01 and flash["drawn"] <= flash["cap"], flash
    # made again every third frame (0.09 s: 30 frames in 3 s), baked once: not again for a sun moved under a quarter pixel or by the frames, once more
    # for one moved a few pixels and for a new size
    bk = hot["bakes"]
    assert 27 <= bk["renders"] <= 33 and bk["every"] == 0.09 and bk["start"] == bk["after"] and bk["moved"] == bk["after"] + 1 and bk["tiny"] == bk["moved"], bk
    assert bk["resized"]["baked"] == bk["beforeResize"] + 1 and bk["resized"]["size"] != bk["size"], bk
    # the cost on the real window (1024 x 768): the weather's frame cost, heat and UV together, at most 2.5 ms
    cost = hot["cost"]
    assert cost["errors"] == [] and cost["size"][:2] == [342, 256] and cost["cost"] < 2.5 and cost["mean"] < 2.5, cost
    # the lean through the day: a smooth curve, 70 degrees at most, no jump (under a degree a minute), the sun's side east (right of the
    # screen): negative (down-left) in the morning, positive (down-right) in the afternoon, rising all day
    curve = hot["sun"]["curve"]
    assert len(curve) == 1441 and max(abs(c) for c in curve) <= 70 + 1e-9, curve[:3]
    assert all(a <= b + 1e-9 for a, b in zip(curve, curve[1:])), "not rising through the day"
    assert max(abs(b - a) for a, b in zip(curve, curve[1:])) < 1, "jumps"
    assert curve[720] == pytest.approx(0, abs=1e-9) and all(c < 0 for c in curve[361:720]) and all(c > 0 for c in curve[721:1080])
    assert curve[0] == curve[360] == pytest.approx(-70) and curve[1080] == curve[1440] == pytest.approx(70)
    # ... by the page's own sun (sunrise 07:00, sunset 19:00: midday 13:00), going on with the page's clock (a minute in 60 s of it)
    sun = hot["sun"]
    assert sun["sunrise"]["lean"] == pytest.approx(-70) and sun["midday"]["lean"] == pytest.approx(0, abs=1e-9), sun
    assert sun["later"]["minutes"] == pytest.approx(781, abs=0.05) and sun["later"]["lean"] > 0, sun["later"]
    assert sun["ignored"]["rise"] == 420 and sun["ignored"]["set"] == 1140, sun["ignored"]   # (a "day" that ends before it begins: ignored)
    # the lean by the sun's real place (the station's latitude and longitude, lib/solar.py; no place: the curve above, which these pages
    # have: no location): -atan(sin az / tan el) from vertical, east (az 90) on the screen's right so a morning sun leans down-left
    place = hot["place"]
    pts = place["points"]
    assert pts["noon"]["lean"] == pytest.approx(0, abs=1e-9) and pts["south"]["lean"] == pytest.approx(0, abs=1e-9), pts   # (due north or south: straight down)
    assert pts["east"]["lean"] == pytest.approx(-math.degrees(math.atan(1 / math.tan(math.radians(40)))), abs=1e-6) and pts["east"]["lean"] < 0, pts["east"]
    assert pts["west"]["lean"] == pytest.approx(-pts["east"]["lean"]) and pts["west"]["lean"] > 0, pts
    assert pts["lowEast"]["lean"] == -75 and pts["lowWest"]["lean"] == 75, pts      # (a low sun: the cap)
    assert abs(pts["overhead"]["lean"]) < 2, pts["overhead"]                         # (overhead: nearly straight down)
    assert pts["down"]["up"] is False and pts["noon"]["up"] and pts["noon"]["real"] and pts["noon"]["az"] == 0 and pts["noon"]["el"] == 70, pts
    # ... the season: the noon azimuth off north (20 degrees), the lean larger at the lower winter elevation; none at az 0 in either
    assert pts["off20high"]["lean"] < 0 and abs(pts["off20high"]["lean"]) < 10 < abs(pts["off20low"]["lean"]), (pts["off20high"], pts["off20low"])
    # ... through each solstice's day at Melbourne (the sun every 10 minutes from lib/solar.py): rising from negative (morning, down-left)
    # through about nothing at solar noon (the sun due north) to positive (afternoon), within the cap; in summer the sun's high: the
    # lean at noon straight down and small most of the day (its median size well under winter's); in winter larger
    days = json.loads((Path(__file__).parent / "sun_days.json").read_text())["days"]
    summer, winter = [[p["lean"] for p in place["through"][d]] for d in ("summer", "winter")]
    for name, leans in (("summer", summer), ("winter", winter)):
        assert len(leans) == len(days[name][1]) and all(abs(l) <= 75 for l in leans), (name, leans)
        assert leans[0] < 0 < leans[-1] and all(a <= b + 1e-9 for a, b in zip(leans, leans[1:])), (name, leans)
        near = min(place["through"][name], key=lambda p: abs(p["az"] - 0 if p["az"] < 180 else p["az"] - 360))   # (the sun nearest due north)
        assert abs(near["lean"]) < 3, (name, near)
    mid = lambda leans: sorted(abs(l) for l in leans[len(leans) // 4: 3 * len(leans) // 4])[len(leans) // 4]
    assert mid(summer) < mid(winter), (mid(summer), mid(winter))
    # ... run on by the page's clock between the data's samples: no jump from one minute to the next of more than a degree (the day's
    # lean up to its cap), none from a new sample to the page's own run on (under a degree); it ends where the last sample put it
    for name in ("summer", "winter"):
        run = place["run"][name]
        assert max(abs(b - a) for a, b in zip(run["leans"], run["leans"][1:])) < 1.0, (name, max(abs(b - a) for a, b in zip(run["leans"], run["leans"][1:])))
        assert max(run["jumps"]) < 1.0, (name, max(run["jumps"]))
        assert run["ends"]["real"] and run["ends"]["el"] == pytest.approx(days[name][1][-1][2], abs=0.2), run["ends"]
    assert place["curve"]["real"] is False and place["curve"]["lean"] == pytest.approx(0, abs=1e-9) and place["curve"]["az"] is None, place["curve"]   # (no place: the curve)

def clean_glass(marks):
    """Nothing on the glass (Sky.state().humidity.marks()): no trail, no stain on its canvas, no bead, no fog"""
    assert {k: marks[k] for k in ("trails", "listed", "ink", "beads", "cover")} == {"trails": 0, "listed": 0, "ink": 0, "beads": 0, "cover": 0}, marks


def started_afresh(seen, effect):
    """"stop", then `effect` on (as a test link after it: restarted() in the browser script): the loop stopped and the link
    gone between, then running again with the link back, and nothing from before: the glass clean, no drop, no stray pixel"""
    assert seen["link"] == "stop" and seen["stopped"]["link"] is None and not seen["stopped"]["running"], seen
    assert seen["again"] == {"running": True, "link": "stop"}, seen
    clean_glass(seen["stopped"]["marks"])
    clean_glass(seen["marks"])
    assert seen["stray"] == 0 and seen["drops"] == 0 and effect in seen["effects"], seen


@needs_browser
async def test_a_flash_flood_fills_the_window_a_quarter_up_its_surface_choppy_what_falls_ending_at_it_and_a_frog_now_and_then(server, station):
    got = await weather(server, "flood")
    # the server's bucket at 0.04 of full (60 mm/h falling): no flood, only the rain; full: a flash flood (the rain's too); the Rain tile its tile
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
    # the bucket below 0.05 again: draining in about 6 s, the chop calming slowly (still much of it half way down); then gone, the rain on
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
    # ... paddling: it went through every stroke of its kick on the way across (4 or more, all of them in time)
    assert len(linked["strokes"]) >= 4 and set(linked["strokes"]) <= set(range(len(FROG_FRAMES))), linked["strokes"]
    # ... the leaves: landing on it and lying there (on the surface line, or a px into it), then sinking and resting on the bottom (on
    # the pile of those before, 6 px high at most); within the budget (those resting none of it), about a third of them or more (0.3) flying on the whole
    leaves = linked["leaves"]
    assert leaves["float"] >= 2 and leaves["sink"] >= 1 and leaves["rest"] >= 1, leaves
    assert linked["gaps"] and set(linked["gaps"]) <= {0, 1}, sorted(set(linked["gaps"]))
    assert linked["resting"] and all(-8 <= r <= 0 for r in linked["resting"]), sorted(set(linked["resting"]))
    assert 0 < linked["most"] < 1 and linked["over"] <= 3, (linked["most"], linked["over"])
    assert linked["fewest"] > 0 and linked["flying"] >= 0.3, (linked["fewest"], linked["flying"])   # (never all in the water: 0.327 of them at the fewest with the rain by drop size and the eddies half as often, a third before)
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
    # under the level as drawn on as those just above (the haze, the heat, the fog, all down to the wavy surface)
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
    assert drained["effects"] == ["heat", "uv", "haze", "humidity", "ice"], drained
    again = linked["again"]
    assert again["url"].endswith(",rain3") and again["leaves"]["all"] == 0 and again["leaves"]["wet"] == [], again
    # every effect on with the flood (full): as cheap as ever (the script's own time a frame, the main thread's CPU time, whatever else
    # the machine is doing: about 2 ms), within the budget (kept by every effect but humidity's beads, heavy rain's among them: their
    # own limits bound them)
    costly = linked["costly"]
    assert costly["effects"] == ["heat", "uv", "haze", "rain", "wind", "humidity", "flood", "ice"] and costly["cost"] < 8, costly
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
    # the test link: each click one more, at random (all three kinds in five clicks); a one-off, never in the URL (so a reload
    # sends none), "wind" clicked after it: only ?fx=wind; not a switch: never pressed
    link = got["link"]
    assert link["sent"] == 5 and set(link["kinds"]) == {"cow", "barn", "chicken"} and link["flying"] >= 1, link
    assert link["url"] == "" and link["afterWind"] == "?fx=wind", link
    assert link["pressed"] is None and "on" not in link["classes"].split(), link
    # "stop": gone at once, nothing drawn, ?fx= gone; stopped, a live gust over 99 km/h sends none; the gust link after it
    # starts them again, one sent
    assert got["stopped"] == {"flyers": 0, "running": False, "url": "", "ink": 0, "sent": 5}, got["stopped"]
    assert got["resumed"] == {"sent": 6, "running": True}, got["resumed"]
    # ?fx=gust as the page opens: ignored, nothing sent (a reload after a click on "gust" no chicken), the word gone from the URL;
    # beside an effect (?fx=wind,gust): that effect on, nothing sent, ?fx=wind
    opened = got["opened"]
    assert opened["sent"] == 0 and opened["effects"] == [] and opened["url"] == "" and opened["errors"] == [], opened
    assert opened["beside"] == {"sent": 0, "effects": ["wind"], "url": "?fx=wind"}, opened["beside"]
    # on a phone, every effect on and "gust" clicked: one sent; no "test:", every link ("gust" too) and "stop" in sight
    phone = got["phone"]
    assert phone["sent"] == 1 and phone["buttons"][-1] == "gust" and not phone["tag"] and phone["fits"] and phone["onScreen"], phone
    just_right_of_the_row(phone)
    assert got["errors"] == [] and phone["errors"] == [], (got["errors"], phone["errors"])


@needs_browser
async def test_a_strong_wind_pushes_the_humidity_beads_downwind_and_everything_else_about_them_stays(server, station):
    got = await weather(server, "push")
    assert got["errors"] == [], got["errors"]
    # the push ratio by the effective speed, 0.6 * smoothstep((s - 15) / 30), signed by the wind's side (west: rightward, +1)
    for way, sign in (("west", 1), ("east", -1)):
        for speed, want in ((0, 0), (15, 0), (30, 0.3), (45, 0.6), (60, 0.6)):
            r = got["ratio"][f"{way}/{speed}"]
            assert r["p"] == pytest.approx(want, abs=1e-9) and r["side"] == sign and r["factor"] == sign, (way, speed, r)
            assert r["push"] == pytest.approx(sign * want, abs=1e-9), (way, speed, r)
            assert r["angle"] == pytest.approx(sign * math.degrees(math.atan(want)), abs=1e-6), (way, speed, r)
    # the mean run: 28 to 34 degrees off vertical toward the downwind side at 45 km/h, mirrored; vertical at 10 and none
    west, east = got["runs"]["west45"], got["runs"]["east45"]
    assert west["running"] >= 15 and east["running"] >= 15, got["runs"]
    assert 28 <= west["degrees"] <= 34 and -34 <= east["degrees"] <= -28, (west, east)
    assert west["angles"] == pytest.approx([30.96, 30.96], abs=0.05) and east["angles"] == pytest.approx([-30.96, -30.96], abs=0.05)
    assert west["dx"] > 20 and east["dx"] < -20 and west["ranOff"] > 0 and east["ranOff"] > 0, (west, east)   # (running off the downwind edge)
    for name in ("west10", "west0"):
        assert abs(got["runs"][name]["degrees"]) < 2 and got["runs"][name]["angles"] == [0, 0], (name, got["runs"][name])
    mid = got["runs"]["west25"]   # (a part of the push: 25 km/h, p = 0.156, 8.8 degrees)
    assert 5 < mid["degrees"] < 13 and mid["push"]["p"] == pytest.approx(0.1556, abs=0.001), mid
    # their speed is the same: the path length per bead at 45 against upright
    path = lambda r: math.hypot(r["dx"], r["dy"])
    assert path(west) == pytest.approx(path(got["runs"]["west10"]), rel=0.3), got["runs"]
    # flat (pooled): none runs and no push, though the wind blows
    flat = got["flat"]
    assert flat["running"] == 0 and flat["push"]["push"] == 0 and flat["push"]["p"] == pytest.approx(0.6) and flat["push"]["angle"] == 0 and flat["ranOff"] == 0, flat
    # tilted 30 degrees left: alone 30 degrees leftward; a rightward push of 0.6 added as a vector (-0.5 + 0.6, 0.866): 6.6 degrees
    # rightward; a leftward one: (-1.1, 0.866), 51.8 degrees leftward
    tilted = got["tilted"]
    assert tilted["alone"]["angles"] == pytest.approx([-30, -30], abs=0.1) and -34 <= tilted["alone"]["degrees"] <= -26, tilted["alone"]
    assert tilted["against"]["angles"] == pytest.approx([6.59, 6.59], abs=0.1) and 3 <= tilted["against"]["degrees"] <= 10, tilted["against"]
    assert tilted["with"]["angles"] == pytest.approx([-51.79, -51.79], abs=0.1) and -56 <= tilted["with"]["degrees"] <= -47, tilted["with"]
    # the trails laid along the slanted run: a row after row (none missing), each row's span touching the last's, running about 31 degrees
    trails = got["trails"]
    assert len(trails) >= 8, trails
    for t in trails:   # (a row's span a pixel off the last's, once, at a bead's hop onto a neighbour's trail (LOCK_GAP, 5 px), aside)
        assert t["jumps"] == 0 and t["gaps"] <= 1 and t["wide"] <= 6 and t["rows"] >= 40, t
    assert sum(t["gaps"] for t in trails) <= 0.1 * len(trails), trails
    assert all(t["jumps"] == 0 and t["gaps"] == 0 for t in got["trailsUpright"]), got["trailsUpright"]
    assert 26 <= sum(t["degrees"] for t in trails) / len(trails) <= 36, trails


@needs_browser
async def test_a_phones_gravity_turns_the_humidity_beads_downhill_by_the_screens_rotation_and_nothing_else_changes(server, station):
    got = await weather(server, "tilt")
    cases = got["cases"]
    # (synthetic devicemotion readings, the spec's (what Android sends: upright, y +9.8) at screen angles 0, 90, 180 and 270: the beads'
    # mean move while running, the window 134 by 100 px)
    down = ["upright", "landscape", "landscapeFlipped", "upsideDown"]
    for name in down:   # (held so the screen's down is down: as ever, the same run as with no sensor at all)
        c = cases[name]
        assert not c["tilt"]["tilted"] and c["tilt"]["epoch"] == 0 and c["tilt"]["mode"] == 0 and c["tilt"]["readings"] == 1, (name, c)
        assert (c["dx"], c["dy"], c["ranOff"]) == (got["none"]["dx"], got["none"]["dy"], got["none"]["ranOff"]), (name, c, got["none"])
    assert not cases["upright"]["tilt"]["flat"] and not cases["upright"]["tilt"]["pool"] and cases["upright"]["tilt"]["steep"] == 1
    # (the steepness, sin of the screen's angle off level: pooling under 5 degrees, not at all above 7 on the way up; smoothstep to 1 at
    # 40 degrees, the same at 60, 90 and 120, down again, symmetric about 90, to pooling again face down; the readings' size and the
    # beads' own speed aside, the factor is the beads' speed against upright's)
    pools = got["pools"]
    for theta in ["0", "3", "175", "177", "180"]:   # (flat, face up or down: nothing runs, no trail, nothing off the window, merging into ever larger blobs)
        c = pools[theta]
        assert c["ran"] == 0 and c["off"] == 0 and c["ranOff"] == 0 and c["trailsAfter"] == 0 and c["tilt"]["flat"], (theta, c)   # (175: right at the 5 degree edge: pooled by its factor)
        assert c["merges"] > 1000 and c["largest"] > 2 * c["early"] and c["largest"] > 4.52, (theta, c)   # (past the biggest sprite: no cap)
        assert c["tilt"]["steep"] == 0 and not c["tilt"]["tilted"], (theta, c)
    full = pools["90"]["factor"]
    # (only the beads big enough for the steepness run: r_crit 3.4 at 10 degrees, 1.98 at 30: fewer there than at 60 and 90)
    assert full > 0.5 and all(pools[t]["ran"] > 100 and not pools[t]["tilt"]["pool"] for t in ["30", "60", "90", "120", "150"]), pools
    assert pools["10"]["ran"] > 20 and not pools["10"]["tilt"]["pool"], pools["10"]
    ratio = {t: pools[t]["factor"] / full for t in ["10", "30", "60", "90", "120", "150"]}
    assert ratio["10"] == pytest.approx(0.064, abs=0.04), ratio                  # (smoothstep((sin 10 - sin 5) / (sin 40 - sin 5)))
    assert ratio["30"] == pytest.approx(0.845, abs=0.12) and ratio["10"] < ratio["30"] < 1, ratio
    assert ratio["60"] == ratio["120"] and ratio["60"] == pytest.approx(1, abs=0.08) and ratio["90"] == 1, ratio   # (full speed, the same by the steepness factor: the beads running differ a little by r_crit)
    assert ratio["150"] == pytest.approx(ratio["30"], abs=0.03) and 0 < ratio["150"] < 1, ratio   # (symmetric about 90: 150 as 30)
    assert pools["10"]["ranOff"] < pools["60"]["ranOff"] and pools["10"]["off"] == 0 == pools["60"]["off"], pools
    # (the pure turn, by readings worked out from the physics, at every screen angle and both signs: the lowered edge is the downhill one;
    # the user's iPad landscape regressions: left tilted -> left, the top raised -> the bottom, the top lowered -> the top)
    want = {"left": (-9.8, 0), "right": (9.8, 0), "top": (0, -9.8), "bottom": (0, 9.8)}
    assert len(got["pure"]) == 5 * 2 * 4
    for key, (x, y) in got["pure"].items():
        angle, sign, edge = key.split("/")
        assert (x, y) == pytest.approx(want[edge], abs=0.02), (key, x, y)
    assert len(got["ipads"]) == 16 + 3 + 1 + 2
    for name, c in got["ipads"].items():
        t, edge = c["tilt"], c["edge"]
        if edge == "flat":   # (flat on a table, screen up: pooling, no direction to run)
            assert t["flat"] and t["pool"] and not t["tilted"] and t["steep"] == 0, (name, c)
            continue
        ux, uy = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}[edge]
        tol = 0.15 if name.startswith("real/") else 0.05   # (the real readings are a little off the axis: left-edge-down is (-1.81, 0.18))
        assert t["tilted"] == (edge != "bottom") and t["x"] == pytest.approx(ux, abs=tol), (name, c)   # (the bottom: straight down, as ever)
        assert t["y"] == pytest.approx(uy, abs=tol), (name, c)
    assert "window.orientation 90" in got["debugIos"] and "used angle 90 (from window.orientation)" in got["debugIos"], got["debugIos"]
    assert "screen.orientation.angle 0 type landscape-primary" in got["debugIos"] and "ios true" in got["debugIos"], got["debugIos"]
    assert got["debugOff"] and "raw x 1.50 y 9.50 z 0.50" in got["debugText"] and "window.orientation" in got["debugText"], got["debugText"]
    # (no sensor: straight down, no reading taken; an empty event (a desktop's) ignored)
    none = got["none"]
    assert none["tilt"] == {"x": 0, "y": 1, "angle": 0, "tilted": False, "mode": 0, "readings": 0, "raw": None, "flat": True, "pool": False, "steep": 1, "sin": 1, "epoch": 0}, none
    assert none["running"] >= 20 and abs(none["dx"]) < 3 and none["dy"] > 30 and none["ranOff"] > 0, none
    assert got["empty"] == none["tilt"], got["empty"]
    # (leaning: sideways as well as down, by the screen's own right and down however it's turned)
    for name, side, edge in [("rolledRight", 1, 0), ("rolledLeft", -1, 0), ("landscapeLeaning", 1, 0), ("landscapeFlippedLeaning", 1, 0),
                             ("upsideDownLeaning", 1, 0)]:
        c = cases[name]
        assert c["tilt"]["tilted"] and c["tilt"]["mode"] == edge and 30 <= abs(c["tilt"]["angle"]) <= 40, (name, c)
        assert c["running"] >= 15 and side * c["dx"] > 20 and c["dy"] > 30 and c["ranOff"] > 0, (name, c)
    assert cases["rolledRight"]["dx"] == pytest.approx(-cases["rolledLeft"]["dx"], rel=0.3)
    # (on a side: the beads run along the screen's row to the edge, off it; upside down on its top: up and off the top)
    right, top = cases["onItsRight"], cases["onItsTop"]
    assert right["tilt"]["mode"] == 2 and right["tilt"]["angle"] == pytest.approx(90) and right["dx"] > 60 and abs(right["dy"]) < 5 and right["ranOff"] > 0, right
    assert top["tilt"]["mode"] == 1 and abs(top["tilt"]["angle"]) == pytest.approx(180) and top["dy"] < -40 and abs(top["dx"]) < 5 and top["ranOff"] > 0, top
    # (window.orientation, the old way, when there's no screen.orientation)
    assert got["oldWay"]["tilt"]["readings"] == 1 and not got["oldWay"]["tilt"]["tilted"], got["oldWay"]
    # (the animations low and off: nothing listened to; "stop": ignored, straight down)
    for mode, seen in got["ignored"].items():
        assert seen["tilt"]["readings"] == 0 and not seen["tilt"]["tilted"] and seen["link"] == "" and not seen["running"], (mode, seen)
    assert got["stopped"]["before"]["tilted"] and not got["stopped"]["after"]["tilted"] and got["stopped"]["after"]["raw"] is None, got["stopped"]
    # (an iPhone: no link at first, a "tilt" link a moment later, asked from its tap, gone at once; granted: its readings (its sign the
    # other way about: the pull to the right edge) taken; denied, or an error: not; no link where readings came, with the animations
    # low, or on a desktop)
    ios = got["ios"]
    for answer, listening in [("granted", True), ("denied", False), ("error", False)]:
        seen = ios[answer]
        assert seen["early"]["link"] == "" and seen["was"]["link"] == "tilt" and seen["was"]["asked"] == 0, (answer, seen)
        assert seen["now"]["link"] == "" and seen["now"]["asked"] == 1 and (seen["now"]["tilt"]["readings"] == 1) == listening, (answer, seen)
    assert ios["granted"]["now"]["tilt"]["raw"] == [pytest.approx(6), pytest.approx(7.7), pytest.approx(0)] and ios["granted"]["now"]["tilt"]["tilted"]
    assert ios["free"]["link"] == "" and ios["free"]["asked"] == 0 and ios["free"]["tilt"]["readings"] == 1, ios["free"]
    assert ios["low"] == "" and ios["desktop"] == "", ios
    # (smoothed: a sudden turn eased in over a fraction of a second, then all of the way; a hand's tremor makes no change; within 4 degrees
    # of straight down, down; laid flat, pooling (steepness 0, its direction kept; 6 degrees from flat pools, 8 runs, and 6 then runs on; a zero reading ignored); a reading that isn't numbers ignored; level(): down)
    f = got["filter"]
    assert 5 < f["eased"][0] < 30 and f["eased"][1] == pytest.approx(90, abs=3), f
    assert f["still"]["epoch"] == 0, f
    assert not f["snapped"]["tilted"] and f["flat"]["flat"] and f["flat"]["pool"] and f["flat"]["steep"] < 0.01 and f["bad"] == [False] * 3, f
    assert f["flat"]["tilted"] and f["flat"]["angle"] == pytest.approx(f["still"]["angle"], abs=2), f   # (flat: the last direction kept, nothing runs)
    assert f["hyst"] == [True, False, False, True], f
    assert not f["level"]["tilted"] and f["level"]["raw"] is None, f
    assert got["errors"] == [], got["errors"]


@needs_browser
async def test_on_a_tilted_device_a_bead_runs_only_from_its_critical_size_which_falls_with_the_steepness_and_leans_before_giving_way(server, station):
    got = await weather(server, "gives")
    # (the pinning: sin(alpha_crit) ~ 1 / r^2, so r_crit = RUN_R / sqrt(s), s = sin(theta): 1.4 upright, 1.98 at 30 degrees, 3.36 at 10)
    crit = {k: v["critical"] for k, v in got["critical"].items()}
    assert crit["90"] == pytest.approx(1.4, abs=0.05) and crit["30"] == pytest.approx(1.98, abs=0.05) and crit["10"] == pytest.approx(3.4, abs=0.05), crit
    assert got["critical"]["90"]["of"] == pytest.approx([1.4, 1.98, 3.4], abs=0.05)
    assert got["noSensor"] == {"critical": 1.4, "run": 1.4, "lean": 0}   # (no sensor: RUN_R as ever, nothing leans)
    # (10 degrees, 20 s: every bead under 3.36 stays put (merging aside: not a px), a 3.6 runs; 30 degrees: 2.2 and 2.6 run, 1.6 and 1.8 stay)
    for name, rc in [("ten", crit["10"]), ("thirty", crit["30"])]:
        beads = [t for batch in got[name] for t in batch["tracked"]]
        assert len(beads) >= 5, got[name]
        for t in beads:
            if t["r"] < rc:   # (never as big as r_crit: held all the while)
                assert t["ran"] == 0 and t["moved"] < 0.1 and t["held"] > 100 and not t["gone"], (name, t)
            else:
                assert t["ran"] > 0 and t["moved"] > 5, (name, t)
    ten = {t["r0"]: t for batch in got["ten"] for t in batch["tracked"]}
    thirty = {t["r0"]: t for batch in got["thirty"] for t in batch["tracked"]}
    assert ten[3.6]["ran"] > 0 and ten[3.2]["ran"] == 0 and ten[3.0]["ran"] == 0, ten
    assert thirty[2.2]["ran"] > 0 and thirty[1.8]["ran"] == 0, thirty
    # (the lean toward downhill (straight down here), r / r_crit 0.8: lambda 0.5, shifted 0.3 to 1 px and longer by a row or more (5 % or
    # more); 0.99: all of it, 1 px and 15 % or so; 1.03: let go (held no more, not leaning, running, the restart stretch))
    eight, nearly, over = (got["lean"][k][-1] for k in ("eight", "nearly", "over"))
    assert eight["ratio"] == pytest.approx(0.8, abs=0.01) and eight["held"] and eight["v"] == 0, eight
    assert 0.4 <= eight["lean"] <= 0.6 and 0.3 <= (eight["dx"] ** 2 + eight["dy"] ** 2) ** 0.5 <= 1 and eight["dy"] > 0, eight
    assert eight["drawnH"] >= 1.05 * eight["ownH"] and eight["drawnTop"] - math.floor(eight["y"]) in (0, 1), eight
    assert nearly["lean"] > 0.95 and (nearly["dx"] ** 2 + nearly["dy"] ** 2) ** 0.5 == pytest.approx(1, abs=0.06), nearly
    assert nearly["drawnH"] / nearly["ownH"] <= 1.2 and nearly["held"] and nearly["v"] == 0, nearly
    assert not over["held"] and over["lean"] == 0 and over["dx"] == over["dy"] == 0, over
    assert any(f["v"] > 0 for f in got["lean"]["over"]), got["lean"]["over"]
    # (giving way as the glass steepens: held and leaning, then running with the restart stretch, no longer leaning)
    rel = got["release"]
    assert rel["held"]["held"] and rel["held"]["lean"] > 0.5 and rel["held"]["v"] == 0, rel["held"]
    assert all(not f["held"] and f["lean"] == 0 for f in rel["frames"]) and any(f["v"] > 0 for f in rel["frames"]) and any(f["stretch"] > 0 for f in rel["frames"]), rel["frames"]
    # (levelled while running: flat stops it at once (the pooling's), 10 degrees from upright brakes it to a stop within 0.3 s; none after 0.5 s)
    for name in ("flat", "gentle"):
        lev = got["level"][name]
        assert lev["before"][1] > 10, lev   # (it ran: px/s)
        assert all(m[1] == 0 and m[0] < 0.05 for m in lev["moves"][5:]), lev   # (pooled: the water's own slow drift aside)
    gentle = got["level"]["gentle"]["moves"]
    assert gentle[0][0] > 0 and gentle[0][0] > gentle[1][0] > gentle[2][0] >= 0 and gentle[2][0] < 0.5 * gentle[0][0], gentle   # (no sudden freeze)


@needs_browser
async def test_a_bead_that_pooled_and_runs_again_lays_its_trail_at_once_from_its_own_back_edge(server, station):
    got = await weather(server, "tilt")
    import math
    recs = [r for r in got["restarts"] if "ys" in r]
    assert {r["edge"] for r in recs} == {0, 1, 2, 3} and {r["cycle"] for r in recs if r["cycle"] > 0} == {1, 2, 3, 4}, got["restarts"][:3]
    for r in recs:
        # (the rows (columns, running sideways) laid since it set off again: the last unbroken run of them, counted from its back edge:
        # edge 0 runs down (its top the back edge), 1 up (its bottom), 2 left (its right edge), 3 right (its left edge))
        ys, back = r["ys"], {0: math.floor(r["y0"]), 1: r["bottom0"] - 1, 2: r["right0"] - 1, 3: r["left0"]}[r["edge"]]
        run = [ys[-1]]
        for y in reversed(ys[:-1]):
            if abs(y - run[0]) > 1:
                break
            run.insert(0, y)
        # (from its own back edge: no first rows skipped; a px of leeway for where it first ran from, its half-length (2 px or more) is not)
        ahead = (run[0] <= back + 1) if r["edge"] in (0, 3) else (run[0] >= back - 1)
        assert ahead, (r["ys"], r, back)
        assert len(run) >= max(1, math.floor(r["along"]) - 1), r   # (and rows enough for the way it has gone: within 2 px of travel, at once)


# The frog's head at 2/3 of what it was (rows 0-8, 21 wide in the 65 x 52 sprite; now 15 x 7 in 47 x 40)
OLD_FROG_HEAD = [".....kkkk...kkkk.....", "....klwwwk.kllggk....", "....kwwwwkkkkkkkk....", "....kwwwpk.kwwwpk....",
                 "...kkgkkkgkgkkkgkk...", "..kgggggggggggggggk..", "..kglllllllllllllgk..", "..kgkkkkkkkkkkkkkgk..",
                 "..kggggggggggggkdgk.."]
HEAD_AT, HEAD_W, HEAD_H = 16, 15, 7   # (the head's first column, its width and rows in the sprite)
OLD_SPRITE = (65, 52)
SPRITE_W, SPRITE_H, CX = 47, 40, 23


def frog_frames() -> list[list[str]]:
    html = (Path(__file__).parent.parent / "lib" / "web_dashboard.html").read_text()
    body = html.split("const FROG_FRAMES = [", 1)[1].split("\n    ];", 1)[0]
    rows = re.findall(r'"([^"]+)"', body)
    return [rows[i:i + SPRITE_H] for i in range(0, len(rows), SPRITE_H)]


FROG_FRAMES = frog_frames()
HTML = (Path(__file__).parent.parent / "lib" / "web_dashboard.html").read_text()


def test_the_frog_is_two_thirds_the_size_with_its_head_as_designed_and_no_lily_pad():
    assert len(FROG_FRAMES) >= 6 and all(len(f) == SPRITE_H and all(len(r) == SPRITE_W for r in f) for f in FROG_FRAMES)
    assert 0.55 <= SPRITE_W / OLD_SPRITE[0] <= 0.8 and 0.55 <= SPRITE_H / OLD_SPRITE[1] <= 0.8
    for rows in FROG_FRAMES:
        head = [r[HEAD_AT:HEAD_AT + HEAD_W] for r in rows[:HEAD_H]]
        assert all(r[:HEAD_AT] == r[HEAD_AT + HEAD_W:][::-1] for r in rows[:HEAD_H])   # (hands raised beside the head mirror too)
        # two eyes: one wide (white over pupil, two rows), one half-lidded (a lid line over a sliver)
        assert head[1][1:7].count("w") >= 3 and head[2][1:7].count("p") == 1 and head[2][1:7].count("w") >= 3, head
        assert head[1][8:14] == "kkkkkk" and head[2][8:14].count("w") >= 3 and head[2][8:14].count("p") == 1, head   # (the lid, the sliver)
        assert head[1][1:7].count("w") > head[2][8:14].count("w") - 1
        # the long flat drooping mouth, one end lower
        mouth = max((len(m) for m in re.findall(r"k+", head[5][1:-1])), default=0)
        assert mouth >= 8 and "kd" in head[6], head
        assert not set("".join(rows)) & set("QPRS"), "no lily pad colours"
    assert all([r[HEAD_AT:HEAD_AT + HEAD_W] for r in f[:HEAD_H]] == [r[HEAD_AT:HEAD_AT + HEAD_W] for r in FROG_FRAMES[0][:HEAD_H]] for f in FROG_FRAMES)   # (the same head every frame)
    assert len({tuple(f) for f in FROG_FRAMES}) >= 6
    joined = HTML.split("const FROG = {", 1)[1].split("};", 1)[0]
    assert not re.search(r"\b[QPRS]:", joined)


def test_every_frame_of_the_frog_is_an_exact_mirror_image_below_the_head():
    for rows in FROG_FRAMES:
        for y in range(HEAD_H, SPRITE_H):
            assert rows[y] == rows[y][::-1], (y, rows[y])   # (the trunk and every limb)
        assert all(r[:HEAD_AT] == r[HEAD_AT + HEAD_W:][::-1] for r in rows[:HEAD_H])   # (the head alone may be lopsided: its eyes and mouth)
    # (the blink changes only the eyes' own rows)
    assert 'y >= 1 && y <= 2 ? row.replace(/[wp]/g, "g")' in HTML


def test_the_frog_has_a_long_soft_teardrop_belly_wide_under_the_head_narrow_at_the_hips():
    for rows in FROG_FRAMES:
        trunk = sum(1 for r in rows[HEAD_H:] if "y" in r[CX - 4:CX + 5])   # (rows of pale belly)
        assert trunk >= 10, trunk
        belly = [max((len(m) for m in re.findall(r"y+", r)), default=0) for r in rows[7:23]]
        belly = belly[next(i for i, n in enumerate(belly) if n):]
        assert 7 <= max(belly) <= 10 and belly.index(max(belly)) <= 4, belly
        assert all(a >= b - 1 for a, b in zip(belly, belly[1:])) and belly[-3] <= 4, belly


def test_the_frogs_limbs_are_short_tapering_and_curved_as_in_the_references():
    for rows in FROG_FRAMES:
        px = {(x, y) for y in range(7, SPRITE_H) for x in range(SPRITE_W) if rows[y][x] in "gl"}
        assert all(math.dist((x, y), (CX + (2 if x > CX else -2), 20)) <= 27 for x, y in px if y > 21), rows
        assert all(math.dist((x, y), (CX + (5 if x > CX else -5), 9)) <= 18 for x, y in px if abs(x - CX) > 7 and y < 20), rows
        limb = thick = 0
        tips = [(x, y) for y in range(7, SPRITE_H) for x in range(SPRITE_W) if rows[y][x] == "l"]
        for x, y in px:
            if abs(x - CX) <= 7 and y < 22 or any(abs(x - tx) <= 4 and abs(y - ty) <= 4 for tx, ty in tips):
                continue
            across = len(re.search(r"[gl]*$", rows[y][:x + 1]).group()) + len(re.match(r"[gl]*", rows[y][x + 1:]).group()) - 1
            col = "".join(r[x] for r in rows)
            top = y
            while top > 7 and col[top - 1] in "gl": top -= 1
            bot = y
            while bot < SPRITE_H - 1 and col[bot + 1] in "gl": bot += 1
            limb += 1
            thick += min(across, bot - top + 1) > 3
        assert len(px) > 40 and thick <= 0.25 * max(limb, 1), (limb, thick)


def test_the_frog_is_drawn_only_within_the_flood_and_only_when_it_is_deep_enough():
    assert "depth >= FROG_DEPTH * full" in HTML and "FROG_DEPTH = 0.9;" in HTML and 0.9 > 0.7
    assert "Math.min(pic.under.height, H - frog.y)" in HTML and "pic.under, 0, 0, pic.under.width, rowsBelow" in HTML
    assert 0.7 * 192 > SPRITE_H - 8   # (the sprite's underwater rows fit above the floor of a 768 px window's flood)


def test_the_frog_swims_a_steady_breast_stroke_one_smooth_cycle():
    beats = [float(v) for v in re.search(r"FROG_BEATS = \[([\d., ]+)\]", HTML).group(1).split(",")]
    assert len(beats) == len(FROG_FRAMES) and all(0.15 <= b <= 0.5 for b in beats) and 1.5 <= sum(beats) <= 2.1, beats
    assert beats[0] == max(beats) and max(beats[1:]) <= 0.25   # (a long glide, then even beats)
    bob = [int(v) for v in re.search(r"FROG_BOB = \[([\d, ]+)\]", HTML).group(1).split(",")]
    assert len(bob) == len(beats) and set(bob) <= {0, 1} and bob[0] == 1 and bob[1] == 0   # (down on the glide, up on the pull)
    pace = [float(v) for v in re.search(r"FROG_SURGE = \[([\d., ]+)\]", HTML).group(1).split(",")]
    assert max(pace) <= 1.25 and min(pace) >= 0.75   # (a mild surge with the kick)
    assert "FROG_RIPPLES" in HTML and "for (const d of [-1, 1])" in HTML   # (a ripple each side)


def test_every_limb_of_the_frog_shows_separate_toes_or_fingers():
    for rows in FROG_FRAMES:
        tips = [(x, y) for y in range(SPRITE_H) for x in range(SPRITE_W) if rows[y][x] == "l" and not (y < HEAD_H and HEAD_AT <= x < HEAD_AT + HEAD_W)]
        assert all(max(abs(a - c), abs(b - d)) >= 2 for i, (a, b) in enumerate(tips) for c, d in tips[i + 1:]), tips
        for side in (lambda x: x < CX, lambda x: x > CX):
            mine = [t for t in tips if side(t[0])]
            hand, foot = [t for t in mine if t[1] < 24], [t for t in mine if t[1] >= 24]
            assert len(hand) >= 2 and len(foot) >= 3, (hand, foot)   # (a hand of 2+ fingers, a foot of 3+ toes)


def _right_limbs(rows):
    return {(x, y) for y in range(HEAD_H, SPRITE_H) for x in range(CX + 1, SPRITE_W) if rows[y][x] in "gl"}


def _belly_edge(rows, y):   # (the belly's outer column on the right at row y: its last pale pixel + 1 for the outline)
    pale = [x for x in range(CX, SPRITE_W) if rows[y][x] == "y"]
    return max(pale) + 1 if pale else None


def test_in_the_recover_and_knees_up_frames_the_knees_sit_beside_the_belly_not_below_the_hips():
    for n in (3, 4):
        rows = FROG_FRAMES[n]
        belly_rows = [y for y in range(HEAD_H, SPRITE_H) if _belly_edge(rows, y)]
        legs = {(x, y) for x, y in _right_limbs(rows) if y >= 15}   # (below the hands)
        # the knee: the leg's topmost pixel out beside the belly (the thigh runs up and out to it) ...
        knee = min((p for p in legs if p[0] >= CX + 6), key=lambda p: (p[1], -p[0]))
        assert belly_rows[0] + 6 <= knee[1] <= belly_rows[-1] - 3, (n, knee, belly_rows)   # (level with the belly)
        assert knee[0] - _belly_edge(rows, knee[1]) >= 3, (n, knee)    # (and clear of it: a gap of outline-coloured pixels)
        # ... the shin folded back down under it, the foot drawn in by the hips (not out below, as in the glide)
        assert max(y for x, y in legs if x <= CX + 8 and y <= 26) >= knee[1] + 5, n
        # nothing of the leg touches the arm above it
        arm = {(x, y) for x, y in _right_limbs(rows) if y < 15 and x > CX + 4}
        assert all(max(abs(x - a), abs(y - b)) >= 2 for x, y in legs if y <= knee[1] + 1 for a, b in arm), n
        assert knee[1] < 22   # (above the hips' row)


def _toe_lengths(rows, side_of):
    """each light tip and how far it is from the limb's thick part (the pixels in a 2 x 2 block: the shin): the toe's length"""
    px = {(x, y) for y in range(HEAD_H, SPRITE_H) for x in range(SPRITE_W) if rows[y][x] in "gl" and side_of(x)}
    thick = {q for (x, y) in px if {(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)} <= px for q in ((x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1))}
    tips = [(x, y) for (x, y) in px if rows[y][x] == "l" and y >= 24]
    return tips, [3 + min(max(abs(x - a), abs(y - b)) for a, b in thick) for x, y in tips]   # (+3: the toes' roots fuse into a small blob at the ankle)


def test_the_frogs_toes_are_short_thin_light_tipped_and_webbed():   # (toes ~7-8 px; the web 'v' between them out to only ~60 % of their length: about 3 px of every toe's tip is free)
    kick = None
    for n, rows in enumerate(FROG_FRAMES):
        tips, lens = _toe_lengths(rows, lambda x: x > CX)
        assert len(tips) == 4, (n, tips)   # (four toes a foot)
        assert 6 <= min(lens) and max(lens) <= 9, (n, lens)
        assert all(max(abs(a - c), abs(b - d)) >= 2 for i, (a, b) in enumerate(tips) for c, d in tips[i + 1:]), (n, tips)   # (the tips separated; the web fills between the toes below them)
        assert all(rows[y][x] == "l" for x, y in tips)
        # the web reaches about 0.55-0.7 of the toe length, leaving the tips free
        px = {(x, y) for y in range(HEAD_H, SPRITE_H) for x in range(SPRITE_W) if rows[y][x] in "gl" and x > CX}
        thick = {q for (x, y) in px if {(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)} <= px for q in ((x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1))}
        web = [3 + min(max(abs(x - a), abs(y - b)) for a, b in thick) for y in range(HEAD_H, SPRITE_H) for x in range(SPRITE_W) if rows[y][x] == "v" and x > CX and y >= 24]   # (the feet's web only)
        assert 0.55 <= max(web) / max(lens) <= 0.7, (n, max(web), lens)
        assert sum(lens) / 4 - max(web) >= 1.5, (n, max(web), lens)   # (on average a toe's last 2 px or so are free; the +3 convention is only approximate)
        if n == 5:
            kick = lens
    assert min(kick) >= 7, kick
    # splayed widest in the kick, closed while the legs sweep in, spread again by the glide's end
    width = lambda n: (lambda t: max(x for x, _ in t) - min(x for x, _ in t))(_toe_lengths(FROG_FRAMES[n], lambda x: x > CX)[0])
    assert width(5) > width(6) and width(0) > width(6) - 1 and width(5) >= 7, [width(n) for n in range(7)]
    # the tucked feet's toes show beside the hips, out from the belly's line, not hidden behind the shin
    for n in (3, 4):
        tips, _ = _toe_lengths(FROG_FRAMES[n], lambda x: x > CX)
        assert all(24 <= y <= 36 for _, y in tips) and all(x >= CX + 2 for x, _ in tips), (n, tips)


def test_the_frog_blinks_over_the_same_head():
    assert 'y >= 1 && y <= 2 ? row.replace(/[wp]/g, "g")' in HTML


def _webs(rows, side_of):
    """per foot, the web pixels ('v'): for each pair of adjacent toe tips, how many lie in the gap between them (in the toe direction)"""
    tips, _ = _toe_lengths(rows, side_of)
    web = [(x, y) for y in range(24, SPRITE_H) for x in range(SPRITE_W) if rows[y][x] == "v" and side_of(x)]   # (the feet's: the lower rows)
    tips = sorted(tips)
    return tips, web


def test_the_frogs_feet_are_webbed_between_the_toes():
    for n, rows in enumerate(FROG_FRAMES):
        for side in (lambda x: x > CX, lambda x: x < CX):
            tips, web = _webs(rows, side)
            assert len(tips) == 4, (n, tips)
            cx = sum(x for x, _ in tips) / 4
            cy = sum(y for _, y in tips) / 4
            # each web pixel is a foot's (lower part of the sprite); for each neighbouring pair of tips (by angle about the foot's base) the web lies between them
            base = (sum(x for x, _ in web) / len(web), min(y for _, y in web)) if web else None
            assert web and all(y >= 24 for _, y in web), (n, web)
            assert len(web) >= 3, (n, len(web))
            ordered = sorted(tips, key=lambda t: math.atan2(t[1] - cy, t[0] - cx))
            for a, b in zip(ordered, ordered[1:]):
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                near = [w for w in web if math.dist(w, (mx, my)) <= max(6, math.dist(a, b))]
                assert len(near) >= 2, (n, a, b, near)   # (web between each pair of toes)
    # the webs are lighter than the limb but not the tip colour
    assert re.search(r'v: rgb\("#5F9C47"\)', HTML)
    # wider webbing in the kick (the fan open) than while the legs sweep together
    span = lambda n: (lambda w: max(x for x, _ in w) - min(x for x, _ in w))(_webs(FROG_FRAMES[n], lambda x: x > CX)[1])
    assert span(5) > span(6), (span(5), span(6))
    n_web = lambda n: len(_webs(FROG_FRAMES[n], lambda x: x > CX)[1])
    assert n_web(5) > n_web(6), (n_web(5), n_web(6))
    for n in (3, 4):   # (it shows on the tucked feet too)
        assert n_web(n) >= 3, n


# ---------- the rain's slant: the live wind's speed ----------
@needs_browser
async def test_the_rain_slants_with_the_live_wind_speed_straight_down_in_no_wind_never_past_70_degrees_eased_and_mirrored(server, station):
    got = await weather(server, "slant")
    assert got["errors"] == [], got["errors"]
    speeds = {int(k): v for k, v in got["speeds"].items()}
    # no wind: exactly vertical, every drop, with the wind effect off and on; nothing of the old fixed slant left
    for calm in (speeds[0], got["calm"], got["calmWind"]):
        assert calm["slantDeg"] == 0 and calm["vxMax"] == 0 and calm["vx"] == 0 and calm["tilts"] == [0], calm
    assert "wind" in got["calmWind"]["effects"] and "rain" in got["calmWind"]["effects"], got["calmWind"]
    # each layer's tan(angle) = speed / (its terminal speed v_t * 3.6 km/h) (the nominal 5 mm/h here: the test link's rain over a dry
    # gauge), capped at 70; slantDeg is the mid layer's (D0 1.26 mm, v_t 4.8 m/s, 17.3 km/h: 5, 10, 40, 60 km/h about 16, 30, 67, 74 -> 70)
    mid = lambda kmh, key="vt": speeds[kmh]["layers"][1][key]
    for kmh in (5, 10, 20, 40, 60):
        for lay in speeds[kmh]["layers"]:
            assert lay["angleDeg"] == pytest.approx(min(70, math.degrees(math.atan(kmh / (lay["vt"] * 3.6)))), abs=0.1), (kmh, lay)
        assert speeds[kmh]["slantDeg"] == speeds[kmh]["layers"][1]["angleDeg"], speeds[kmh]
    assert speeds[5]["slantDeg"] == pytest.approx(16.1, abs=1) and speeds[10]["slantDeg"] == pytest.approx(30, abs=1.5), speeds
    # (the small far drops lean furthest, the large near ones least, at each speed)
    for kmh in (5, 10, 20):
        far, middle, near = (lay["angleDeg"] for lay in speeds[kmh]["layers"])
        assert far > middle > near > 0, (kmh, speeds[kmh]["layers"])
    # 25 km/h at a nominal 5 mm/h: far about 66, mid about 55, near about 48 degrees (the formula's)
    far, middle, near = (abs(a) for a in got["paths"]["25"]["layers"])
    assert (far, middle, near) == pytest.approx((65.9, 55.3, 47.6), abs=1.5), got["paths"]["25"]
    deg = [speeds[k]["slantDeg"] for k in sorted(speeds)]   # (0, 2, 5, 10, 20, 40, 60, 100 km/h)
    assert all(a < b for a, b in zip(deg[:5], deg[1:5])) and max(deg) <= 70.0001 and deg[7] == pytest.approx(70, abs=0.01), deg   # (mid layer: capped from about 47 km/h)
    assert all(0 < speeds[k]["slantDeg"] for k in (2, 5, 10)) and speeds[2]["slantDeg"] < speeds[5]["slantDeg"] / 2, speeds
    # the gust adds a little: speed + 0.3 * (gust - speed); a gust under the speed adds nothing
    assert got["gust"]["target"] == pytest.approx(16) and got["gust"]["slantDeg"] == pytest.approx(math.degrees(math.atan(16 / (mid(10) * 3.6))), abs=0.1), got["gust"]
    assert got["noGust"]["slantDeg"] == pytest.approx(speeds[10]["slantDeg"], abs=0.01), got["noGust"]
    # the drops move across as their slant says (leftward: negative), the wind effect on too (its bands bunch them)
    assert speeds[20]["vx"] < speeds[5]["vx"] < speeds[0]["vx"] == 0 and got["blowing"]["vx"] < speeds[20]["vx"] * 0.5, (speeds, got["blowing"])
    # eased: a jump from no wind to 40 km/h (at 60 frames a second) never steps more than 5 degrees in a frame, rising in about 2 s
    # and falling in about 4 (to half way: under 2 s up, between 1 and 4 s down), smoothly (never backing up); 40 km/h is 59 degrees
    # (the jump's goal: the mid layer's, 66.6 degrees at 5 mm/h), the half way point 33
    jump = got["jump"]
    assert jump["maxUp"] < 5 and jump["maxDown"] < 5 and jump["monotonic"] and jump["final"] == pytest.approx(math.degrees(math.atan(40 / (jump["vt"] * 3.6))), abs=0.5), jump
    assert jump["upSecondsHalf"] < 2 and jump["upTo50"] > 0.4 and 0.8 < jump["downSecondsHalf"] < 4 and abs(jump["back"]) < 2, jump   # (and back near vertical after 10 s)
    # the streaks tilt granularly: at least 16 distinct tilts between 0 and 70 degrees (21 steps, 3.5 degrees apart), as the speed goes up
    tilts = {t for each in got["tilts"] for t in each}
    assert got["tiltSteps"] >= 21 and len(tilts) >= 16, (sorted(tilts), got["tiltSteps"])
    # blowing rightward: the exact mirror: a negative angle, the drops going right, their streaks mirrored
    right = got["rightward"]
    assert right["slantDeg"] == pytest.approx(-speeds[20]["slantDeg"], abs=0.01) and right["vx"] > 20 and right["mirrored"] > 0, right
    # the drops cover the whole width at the steepest slant, either way (no bare left or right strip)
    for way, cover in got["coverage"].items():
        assert cover["deg"] == pytest.approx(70 * -int(way), abs=0.01), cover
        assert min(cover["cols"]) > 0.4 * (sum(cover["cols"]) / 8), (way, cover)
    # the test link's wind (11 km/h; a real reading above it would win), its rain the nominal 5 mm/h: the layers lean about 26 (near), 32
    # (mid) and 44 (far) degrees
    assert got["link"]["speed"] == pytest.approx(11, abs=0.05) and got["link"]["rate"] == 5, got["link"]
    assert [abs(d) for d in got["link"]["layers"]] == pytest.approx([44.5, 32.4, 25.8], abs=1.5), got["link"]
    assert [abs(d) for d in got["paths"]["11"]["layers"]] == pytest.approx([44.5, 32.4, 25.8], abs=1.5), got["paths"]["11"]
    # the drops' VERTICAL speed is the same (within 5 %) at 0, 25 and 60 km/h; their speed along the path is that over the cosine of
    # the angle (within 5 %); the streaks lengthen with the slant (the path speed) and stop at 1.6 times the calm length
    paths = got["paths"]
    for kmh in ("0", "11", "25", "60"):
        assert paths[kmh]["down"] == pytest.approx(1, rel=0.05), (kmh, paths[kmh])
        # (the mean over the drops, the layers in their shares: each at its own angle)
        shares = (0.45, 0.33, 0.22)
        expect = sum(w / math.cos(math.radians(abs(a))) for w, a in zip(shares, paths[kmh]["layers"]))
        assert paths[kmh]["path"] == pytest.approx(expect, rel=0.05), (kmh, paths[kmh], expect)
    assert paths["0"]["streak"] < paths["11"]["streak"] < paths["25"]["streak"] < paths["60"]["streak"], paths
    assert paths["60"]["streak"] <= paths["0"]["streak"] * 1.6 * 1.1 and paths["60"]["streak"] >= paths["0"]["streak"] * 1.6 * 0.9, paths   # (capped: 1.6 times, within pixels)
    # the rate they land at (drops per second over the window) is the same at every angle, within 10 % of each other (the same vertical
    # speed, the plain count of drops: no density scaling)
    landed = [paths[k]["landed"] for k in ("0", "11", "25", "60")]
    assert max(landed) <= min(landed) * 1.1, (landed, paths)
    assert len({paths[k]["drops"] for k in paths}) == 1, paths   # (the count is the budget's, whatever the slant)
    assert "const density" not in HTML and "density()" not in HTML


@needs_browser
async def test_the_rains_layers_are_drop_sizes_from_the_rain_rate_each_with_its_terminal_speed_lean_and_fall_and_the_bead_grows(server, station):
    got = await weather(server, "drops")
    assert got["errors"] == [], got["errors"]
    rates = {float(k): v for k, v in got["rates"].items()}
    # the median drop D0 = 0.9 R^0.21 mm (R clamped 0.5-100; none, a link's rain: the nominal 5)
    for rate, want in ((1, 0.9), (10, 1.46), (50, 2.05)):
        assert rates[rate]["d0"] == pytest.approx(want, abs=0.05), (rate, rates[rate])
    assert rates[0]["rate"] == 5 and rates[0]["d0"] == pytest.approx(0.9 * 5 ** 0.21, abs=1e-6), rates[0]
    assert rates[0.2]["rate"] == 0.5 and rates[500]["rate"] == 100, (rates[0.2], rates[500])
    # the layers are 0.6, 1.0 and 1.5 times it; each one's terminal speed v_t = 9.65 - 10.3 exp(-0.6 D) m/s (0.9 mm 3.65, 1.5 mm 5.46, 2.1 mm 6.73, 3 mm 7.95)
    for rate in (1, 5, 10, 50):
        r = rates[rate]
        for lay, size in zip(r["layers"], (0.6, 1.0, 1.5)):
            assert lay["D"] == pytest.approx(size * r["d0"], abs=1e-9) and lay["vt"] == pytest.approx(9.65 - 10.3 * math.exp(-0.6 * lay["D"]), abs=1e-9), (rate, lay)
    assert rates[1]["layers"][1]["vt"] == pytest.approx(3.65, abs=0.05) and rates[10]["layers"][1]["vt"] == pytest.approx(5.36, abs=0.05)
    assert rates[50]["layers"][1]["vt"] == pytest.approx(6.73, abs=0.1) and 9.65 - 10.3 * math.exp(-1.8) == pytest.approx(7.95, abs=0.01)
    # each layer leans atan(25 km/h / (v_t * 3.6)) (70 at most): at 5 mm/h far about 66, near about 48; a heavier rain leans less than drizzle, every layer
    for rate in (1, 5, 10, 50):
        for lay in rates[rate]["layers"]:
            assert lay["angleDeg"] == pytest.approx(min(70, math.degrees(math.atan(25 / (lay["vt"] * 3.6)))), abs=0.1), (rate, lay)
    far, near = rates[5]["layers"][0]["angleDeg"], rates[5]["layers"][2]["angleDeg"]
    assert 60 < far < 70 and 40 < near < 52 and far > near, (far, near)
    for k in range(3):
        assert rates[50]["layers"][k]["angleDeg"] < rates[1]["layers"][k]["angleDeg"], (k, rates[50], rates[1])
    # the fall speeds follow the v_t ratios (within 5 %), the mid layer at 230 px/s at 5 mm/h, the whole scaled with its v_t over that, 0.7 to 1.3 times
    for rate, r in rates.items():
        v = [lay["v"] for lay in r["layers"]]
        assert v[0] / v[1] == pytest.approx(r["layers"][0]["vt"] / r["layers"][1]["vt"], rel=0.05) and v[2] / v[1] == pytest.approx(r["layers"][2]["vt"] / r["layers"][1]["vt"], rel=0.05), (rate, v)
    assert rates[5]["layers"][1]["v"] == pytest.approx(230, abs=1)
    assert 230 * 0.7 - 1 <= rates[0.2]["layers"][1]["v"] <= 230 * 0.7 + 1 and rates[500]["layers"][1]["v"] == pytest.approx(230 * 1.3, abs=1), (rates[0.2], rates[500])
    assert rates[1]["layers"][1]["v"] < 230 < rates[10]["layers"][1]["v"], rates
    # the landing bead 2.6 + 0.37 clamp((D0 - 0.9) / 1.3, 0, 1): small in drizzle, large in a cloudburst (still the large class, 2.5 or more)
    assert rates[0.2]["beadR"] == pytest.approx(2.6) and rates[500]["beadR"] == pytest.approx(2.97), (rates[0.2], rates[500])
    assert rates[1]["beadR"] == pytest.approx(2.6, abs=1e-6) and 2.6 < rates[5]["beadR"] < rates[10]["beadR"] < rates[50]["beadR"] <= 2.97, rates
    assert "rainR()" in HTML and "RAIN_R" not in HTML


@needs_browser
async def test_new_eddies_rise_half_as_often_as_they_did(server, station):
    got = await weather(server, "eddies")
    assert got["errors"] == [], got["errors"]
    run = got["run"]
    # each slot's cycle is a life (5-9 s) then a rest (5-9 s): about 14 s, a rise per slot every 14 s, not every 7 (the old rate)
    per_slot_s = run["seconds"] * run["slots"] / max(run["rises"], 1)
    assert run["rises"] > 20 and per_slot_s == pytest.approx(14, rel=0.2), run
    # (so about half the slots show an eddy at a time)
    assert run["live"] == pytest.approx(run["slots"] / 2, abs=0.2 * run["slots"] + 0.5), run


@needs_browser
async def test_the_leaves_pace_follows_the_measured_wind_speed_eased_and_a_test_links_wind_keeps_the_usual_pace(server, station):
    got = await weather(server, "pace")
    assert got["errors"] == [] and got["linkErrors"] == [], got
    speeds = {int(k): v for k, v in got["speeds"].items()}
    # k = clamp(0.4 + 0.024 s, 0.5, 1.6): 0 km/h 0.5 (clamped), 15 0.76, 25 1.0, 40 1.36, 60 1.6
    for kmh, want in ((0, 0.5), (15, 0.76), (25, 1.0), (40, 1.36), (60, 1.6)):
        assert speeds[kmh]["k"] == pytest.approx(want, abs=0.03), (kmh, speeds[kmh])
    # the effective speed is the reading plus 0.3 of the gust over it: 10 with a gust of 40 is 19, k 0.856
    assert got["gust"]["speed"] == pytest.approx(19, abs=0.1) and got["gust"]["k"] == pytest.approx(0.4 + 0.024 * 19, abs=0.03), got["gust"]
    # the leaves' mean speed across scales with it (within 15 %) against what they do at 25 km/h
    for kmh in (15, 40, 60):
        assert speeds[kmh]["mean"] / speeds[25]["mean"] == pytest.approx(speeds[kmh]["k"], rel=0.15), (kmh, speeds)
    assert speeds[0]["mean"] < speeds[15]["mean"] < speeds[25]["mean"] < speeds[40]["mean"] < speeds[60]["mean"], speeds
    # eased like the rain's slant: no jump from 25 to 60 km/h, rising smoothly to 1.6
    eased = got["eased"]
    assert eased["first"] == pytest.approx(1.0, abs=0.01) and eased["last"] == pytest.approx(1.6, abs=0.03) and eased["monotonic"] and eased["maxStep"] < 0.05, eased
    # the test link's wind over a calmer real one: the pace it always had; a real wind above its 11 km/h rules
    assert got["link"]["k"] == pytest.approx(1.0, abs=0.01), got["link"]
    assert got["linkOver"]["k"] == pytest.approx(0.4 + 0.024 * 40, abs=0.03), got["linkOver"]


@needs_browser
async def test_the_fog_keeps_on_past_its_mark_down_to_85_percent_the_flood_has_no_hysteresis_and_a_page_load_starts_plain(server, station):
    got = await weather(server, "hysteresis")
    assert got["errors"] == [] and got["freshErrors"] == [], got
    # fog: on from 90 %, kept on to 85 % (84.9 off), back on at 90 again
    assert got["fog"] == [[91, True], [88, True], [86, True], [85, True], [84, False], [88, False], [91, True], [89.9, True], [85, True], [84.9, False]], got["fog"]
    # flood: the server's bucket depth, on from 0.05 and off below it (no hysteresis: the bucket is slow itself)
    assert got["flood"] == [[0.06, True], [0.05, True], [0.049, False], [0.06, True], [0.04, False], [0.2, True], [0.05, True], [0.04, False]], got["flood"]
    assert got["rateOnly"] == [0, 0, 0], got["rateOnly"]   # (a heavy rain rate alone: no flood)
    assert got["fresh"] == {"fog": 0, "flood": 0}, got["fresh"]


@needs_browser
async def test_the_floods_water_follows_the_buckets_depth_and_the_frog_swims_only_from_nine_tenths_full(server, station):
    got = await weather(server, "bucket")
    levels = got["levels"]
    assert [lv["want"] for lv in levels] == [0.04, 0.3, 0.6, 0.05, 0.2, 0.04, 0], levels
    for lv in levels:   # (the water's height: the depth fraction of its full height, none below 0.05)
        want = 0 if lv["want"] < 0.05 else lv["want"] * lv["full"]
        assert lv["depth"] == pytest.approx(want, abs=0.02), lv
        assert lv["level"] == (lv["want"] if lv["want"] >= 0.05 else 0), lv
    assert levels[1]["full"] == 64 and levels[3]["depth"] > 0, levels
    # (eased: a frame moves it no faster than full / RISE, 4 s from empty)
    ease = got["ease"]
    assert 0 < ease["step"] <= ease["full"] / 4 * ease["dt"] + 0.01, ease
    # a frog never in water 0.85 full however long (25 minutes of the weather's time, past his due time), one at 0.95
    assert got["shallow"]["frogs"] == 0 and got["shallow"]["since"] > got["shallow"]["nextFrog"] + 1000, got["shallow"]
    assert got["shallow"]["depth"] == pytest.approx(0.85 * got["shallow"]["full"], abs=0.1)
    assert got["deep"]["frogs"] >= 1 and got["deep"]["depth"] >= 0.9 * got["deep"]["full"], got["deep"]


@needs_browser
async def test_the_hazes_opacity_follows_the_pm25_continuously_by_beer_lambert_its_tint_between_the_bands_and_its_pattern_by_the_band(server, station):
    got = await weather(server, "pm25")
    # the curve 1 - exp(-0.01457 pm), at most 0.97: none below 9 (eased in from 8), 12 % at 9, 25 % at 20, 40 % at 35, 55 % at 55, 77 %
    # at 100, 89 % at 150, 97 % at 250, capped
    op = got["curve"]["opacity"]
    for pm, want in (("0", 0), ("5", 0), ("8", 0), ("9", 0.12), ("20", 0.25), ("35", 0.40), ("55", 0.55), ("100", 0.77), ("150", 0.89), ("250", 0.97), ("1000", 0.97)):
        assert op[pm] == pytest.approx(want, abs=0.03), (pm, op[pm])
    assert max(op.values()) <= 0.97 and 0 < op["8.5"] < op["9"], op
    # continuous (no jump over 0.02 per µg/m3 from 9 up, across every band's edge); its tint too (3 or less of 255 a channel per µg/m3);
    # the tint at the bands' midpoints, held beyond the first and last
    assert got["curve"]["worst"] <= 0.02 and got["curve"]["tint"] <= 3, got["curve"]
    assert got["curve"]["mids"] == pytest.approx([0, 1, 2, 3, 4], abs=1e-6)
    steady = got["curve"]["steady"]
    assert steady[0] == steady[1] and steady[2] == steady[3] and steady[0] != steady[2], steady
    # the page: below 9 no haze; read from data.pm25, low-passed, the opacity the curve's; the bands by the AQI as before
    assert got["below"]["effects"] == [] and got["below"]["opacity"] == 0 and got["below"]["mean"] == 0, got["below"]
    for pm, seen in got["points"].items():
        want = min(0.97, 1 - math.exp(-0.01457 * float(pm)))
        assert seen["effects"] == ["haze"] and seen["opacity"] == pytest.approx(want, abs=0.02) and seen["pm25"] == pytest.approx(float(pm), abs=0.1), (pm, seen)
    # ... the band's baked pattern (its cover) as ever: it changes at an edge, the opacity across it by no more than the curve's slope
    low, high = got["edge"]
    assert (low["level"], high["level"]) == (1, 2) and low["look"]["lo"] != high["look"]["lo"], got["edge"]
    assert abs(low["opacity"] - high["opacity"]) < 0.01 and low["tint"] == high["tint"], got["edge"]
    # ... within a band more PM2.5 is more opaque as drawn, as the curve says (0.77 / 0.58), the tint moving on
    same = got["sameBand"]
    assert same[0]["level"] == same[1]["level"] == 3 and same[0]["look"] == same[1]["look"], same
    assert same[1]["mean"] / same[0]["mean"] == pytest.approx((1 - math.exp(-1.457)) / (1 - math.exp(-0.01457 * 60)), abs=0.12), same
    assert same[0]["tint"] != same[1]["tint"], same
    # ... the tint between the bands' (a colour in each band's own at its midpoint, none the same two steps apart), at 20 µg/m3 the first band's
    assert got["points"]["20"]["tint"] != got["points"]["35"]["tint"] != got["points"]["50"]["tint"], got["points"]
    # no PM2.5 reading: the band's midpoint's opacity, the design look
    assert got["none"]["pm25"] == pytest.approx(90.4) and got["none"]["opacity"] == pytest.approx(0.732, abs=0.02), got["none"]
    # dithered with no grid (the least over several pans: under 400), cheap
    assert min(got["grids"]) < 400, got["grids"]
    assert got["cost"] < 4, got["cost"]


# ---------- the wind's direction ----------
@needs_browser
async def test_the_winds_side_follows_the_wind_direction_reading_east_leftward_west_rightward_north_and_south_hold_and_a_flip_eases(server, station):
    got = await weather(server, "direction")
    assert got["start"]["dir"] == got["start"]["target"] == -1 and got["start"]["flips"] == 0, got["start"]   # (leftward as the page opens)
    # 0 < deg < 180 (from the east side): leftward (-1); 180 < deg < 360: rightward (+1); exactly 0, 180, 360, or none: as it was
    assert got["readings"] == [[180, -1], [90, -1], [270, 1], [0, 1], [180, 1], [360, 1], [359.5, 1], [0.5, -1], [270, 1], [None, 1],
                               [180, 1], [45, -1], [315.5, 1], [360, 1], [179.5, -1], [180.5, 1]], got["readings"]
    # a flip: the factor from -1 to +1 over about 4 s, never more than a frame's smooth step, the side changing as it crosses 0 (once),
    # the leaves' mean speed across going from leftward to rightward (those in an eddy veering round, see the next test); and back again
    flip = got["flip"]
    assert flip["first"] == -1 and flip["last"] == 1 and flip["flips"] == 1 and flip["sides"] == [-1, 1] and flip["monotonic"], flip
    assert flip["maxStep"] < 0.03 and 3.5 <= flip["seconds"] <= 4.6, flip
    # (the leaves' mean speed across: leftward, then rightward: those in an eddy veer round rather than stop, so not calm in between)
    assert flip["vx"][0] < -10 and flip["vx"][2] > 10, flip
    assert got["flipBack"]["maxStep"] < 0.03 and got["flipBack"]["last"] == -1, got["flipBack"]
    # the wind alone, blowing rightward: the exact mirror of leftward, the streaks, the leaves in their sprites mirrored
    mirror = got["mirrorWind"]
    assert mirror["dirs"] == [-1, 1] and mirror["vx"][0] < -10 < 10 < mirror["vx"][1], mirror
    assert mirror["ink"] > 100 and mirror["wrong"] <= mirror["ink"] * 0.003, mirror
    # the haze drifts with it, the clouds and the motes
    haze = got["haze"]
    assert haze["east"]["moved"] < -3 and haze["west"]["moved"] > 3 and haze["east"]["slide"] > 3 and haze["west"]["slide"] < -3, haze
    assert haze["east"]["moved"] == pytest.approx(-haze["west"]["moved"], rel=0.15), haze
    assert haze["east"]["moved"] == pytest.approx(-haze["east"]["slide"], rel=0.1), haze   # (the motes carried at the pan's pace)
    # the rain slants with it: with the wind effect and without it (the reading alone), its drops' mean speed across, and its streaks
    # drawn mirrored
    rain = got["rain"]
    for way, sign in (("east", -1), ("west", 1)):
        for key in (way, f"{way}Read"):
            assert sign * rain[key]["vx"] > 20 and (rain[key]["mirrored"] > 0) == (sign > 0), (key, rain[key])
    assert rain["east"]["vx"] == pytest.approx(-rain["west"]["vx"], rel=0.25), rain
    # the frog comes in from upwind and swims downwind, its sprites the leftward ones exactly mirrored
    frog = got["frog"]
    assert frog["east"]["first"]["x"] > frog["east"]["width"] and frog["east"]["travelled"] < -10 and not frog["east"]["flipped"], frog["east"]
    assert frog["west"]["first"]["x"] < 0 and frog["west"]["travelled"] > 10 and frog["west"]["flipped"], frog["west"]
    assert frog["east"]["pics"] and frog["west"]["pics"], frog
    mirrored = {tuple(tuple(reversed(row.split(" "))) for row in pic) for pic in frog["east"]["pics"]}
    right = {tuple(tuple(row.split(" ")) for row in pic) for pic in frog["west"]["pics"]}
    assert len(right & mirrored) >= min(len(right), len(mirrored)) - 1 and any(pic != tuple(tuple(reversed(r)) for r in pic) for pic in right), (len(right), len(mirrored), len(right & mirrored))   # (the head lopsided; the poses seen one way seen mirrored the other, but for one the other swimmer was not sampled in)
    # the flood's waves run downwind faster than up, the surface carried along
    waves = got["waves"]
    assert waves["push"] == waves["PUSH"] > 0 and waves["still"]["right"] == pytest.approx(waves["still"]["left"], abs=2), waves
    assert waves["downRight"]["right"] > waves["downRight"]["left"] + 8 and waves["downLeft"]["left"] > waves["downLeft"]["right"] + 8, waves
    # a gust over 99 km/h: the cow comes in from upwind, flown downwind
    gust = got["gust"]
    assert gust["east"]["to"] == -1 and gust["east"]["x0"] > gust["east"]["width"] and gust["east"]["x1"] < gust["east"]["x0"], gust["east"]
    assert gust["west"]["to"] == 1 and gust["west"]["x0"] < 0 and gust["west"]["x1"] > gust["west"]["x0"], gust["west"]
    assert gust["chickenWest"]["to"] == 1 and gust["chickenWest"]["x1"] > gust["chickenWest"]["x0"], gust["chickenWest"]
    # everything on, the wind turning back and forth under it: no error, each effect still on
    assert got["busy"]["flips"] == 3 and got["busy"]["dir"] == 1 and {"haze", "rain", "wind", "flood"} <= set(got["busy"]["effects"]), got["busy"]


@needs_browser
async def test_when_the_wind_turns_its_eddies_swirl_on_undisturbed_and_the_leaves_in_them_veer_round_in_a_u_up_then_down_while_the_rest_reverse_along_their_line(server, station):
    got = await weather(server, "uturn")
    if os.environ.get("SKY_UTURN_DUMP"):   # (the paths, for a picture of them)
        Path(os.environ["SKY_UTURN_DUMP"]).write_text(json.dumps(got))
    # the eddy's swirl, through a turn (the wind from one side to the other): the same way round, the same strength and the same
    # velocity at each point, whichever side the wind has: the flip doesn't touch it (the turbulence off; and on, the eddy's eye the same way)
    swirl = got["swirl"]
    assert swirl["sides"] == [-1, 1] and swirl["factors"] == [-1, 1], swirl
    for name in ("east", "north"):
        assert 0.9 <= swirl[name]["ratio"][0] <= swirl[name]["ratio"][1] <= 1.1 and swirl[name]["drift"] < 0.1 and swirl[name]["v0"] > 30, swirl[name]
    assert swirl["sense"]["east"][0] == swirl["sense"]["east"][1] != 0 and swirl["sense"]["north"][0] == swirl["sense"]["north"][1] != 0, swirl["sense"]
    assert swirl["sense"]["eye"][0] == swirl["sense"]["eye"][1] != 0 and 0.9 <= swirl["sense"]["eyeRatio"][0] <= swirl["sense"]["eyeRatio"][1] <= 1.1, swirl["sense"]
    assert swirl["rough"][0] == swirl["rough"][1] != 0, swirl["rough"]
    # a leaf in the eddy, over three turns (left to right, back, and right again): its mean wind veers, its heading turning through 180
    # degrees, never slowing below 60 % of its speed, through up (the first turn), down (the second), up again (the third)
    assert [t["veerSide"] for t in got["leaf"]] == [1, -1, 1], got["leaf"]
    for turn, up in zip(got["leaf"], (True, False, True)):
        assert turn["eddy"][0] > 0.9, turn   # (in the eddy throughout)
        assert turn["speeds"][0] >= 0.6 * turn["speeds"][1] and turn["speeds"][2] <= 1.4 * turn["speeds"][1], turn
        assert turn["total"] == pytest.approx(math.pi, abs=0.1) and abs(turn["signed"]) == pytest.approx(math.pi, abs=0.1), turn   # (turning one way)
        assert (turn["pathY"][0] < -30) if up else (turn["pathY"][1] > 30), turn   # (its mean's path bulging up on screen, or down)
        assert turn["sides"] in ([-1, 1], [1, -1]) and turn["meanXs"][0] * turn["meanXs"][1] < 0, turn   # (and ending the other way)
    # a leaf in no eddy: its mean wind reverses along its line (no sideways), through calm, as before
    for turn, sign in zip(got["straight"], (1, -1)):
        assert turn["eddy"] == 0 and turn["ys"] < 1e-6 and sign * turn["xs"][0] < -10 and sign * turn["xs"][2] > 10 and abs(turn["xs"][1]) < 2, turn
    # a leaf in a weak eddy: half veering, half straight: the two blended by how much of an eddy it is in
    assert 0.35 < got["half"]["strength"] < 0.65, got["half"]
    assert sum(0.15 < r["w"] < 0.85 for r in got["blend"]) > 100, [r["w"] for r in got["blend"][::30]]
    for r in got["blend"]:
        for axis in (0, 1):
            assert r["mean"][axis] == pytest.approx(r["straight"][axis] + (r["veer"][axis] - r["straight"][axis]) * r["w"], abs=1e-6), r
    assert any(r["f"] == 1 and abs(r["mean"][0] - r["straight"][0]) > 0.5 for r in got["blend"]), "it veers while the straight part has finished"


def _hand_px(rows, right=True):
    return {(x, y) for y in range(SPRITE_H) for x in range(SPRITE_W) if (x > CX) == right and x != CX and y < 24 and rows[y][x] in "glv"
            and not (y < HEAD_H and HEAD_AT <= x < HEAD_AT + HEAD_W)}


def test_the_frogs_hands_are_bigger_than_before_webbed_with_the_fingertips_beyond_the_web_but_smaller_than_the_feet():
    OLD_HAND_PX = [71, 77, 82, 83, 86, 77, 73]   # (the old hands: three one-pixel fingers about 4-5 px out from the wrist; their pixels per frame)
    for n, rows in enumerate(FROG_FRAMES):
        tips = [(x, y) for x, y in _hand_px(rows) if rows[y][x] == "l" and y < 24 and not (y < HEAD_H and x < HEAD_AT + HEAD_W)]
        webs = [(x, y) for x, y in _hand_px(rows) if rows[y][x] == "v"]
        assert len(tips) == 4 and len(webs) >= 5, (n, tips, len(webs))   # (four fingers, a web between them)
        assert len(_hand_px(rows)) >= OLD_HAND_PX[n] + 10, (n, len(_hand_px(rows)))   # (bigger than the old three-finger hands)
        assert all(max(abs(a - c), abs(b - d)) >= 2 for i, (a, b) in enumerate(tips) for c, d in tips[i + 1:]), (n, tips)
        # the web ends short of the fingertips: no web pixel touches a tip, and the free end (tip plus the finger pixel behind it) is out beyond the web
        assert all(max(abs(x - a), abs(y - b)) >= 2 for x, y in tips for a, b in webs), (n, tips, webs)
        palm = (sum(x for x, _ in webs) / len(webs), sum(y for _, y in webs) / len(webs))
        assert max(math.dist(t, palm) for t in tips) >= 4, (n, tips, palm)
        # smaller than the feet: the widest finger spread is below the foot's widest toe spread (kick)
        spread = max(max(x for x, _ in tips) - min(x for x, _ in tips), max(y for _, y in tips) - min(y for _, y in tips))
        assert 5 <= spread <= 12, (n, spread)
        # (the hand stays off the head: no hand pixel but the arm's own is within a pixel of the head's outline)
        head = {(x, y) for y in range(HEAD_H) for x in range(HEAD_AT, HEAD_AT + HEAD_W) if rows[y][x] != "."}
        assert all(max(abs(x - a), abs(y - b)) >= 2 for x, y in tips for a, b in head), (n, tips)
    foot_tips = _toe_lengths(FROG_FRAMES[5], lambda x: x > CX)[0]
    foot_spread = max(x for x, _ in foot_tips) - min(x for x, _ in foot_tips)
    for n, rows in enumerate(FROG_FRAMES):   # (in every frame)
        t = [(x, y) for x, y in _hand_px(rows) if rows[y][x] == "l"]
        assert max(max(x for x, _ in t) - min(x for x, _ in t), max(y for _, y in t) - min(y for _, y in t)) < foot_spread + 4, n
    # the hands are web-coloured like the feet's, and both hands mirror
    assert all(_hand_px(r, True) == {(46 - x, y) for x, y in _hand_px(r, False)} for r in FROG_FRAMES)
