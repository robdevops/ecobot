import io
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import json
import time

from lib import gauge_sheet, gauges
from lib.bot import Bot
from lib.ecowitt import dashboard as dash
from lib.service import current as cur
from lib.service.context import Context
from lib.specs import Dashboard, Tile
from tests.fakes import TZ, StandInService, archived_station, gauges_result

TODAY = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 12, tzinfo=TZ)


def test_the_rows_cover_the_day_the_month_so_far_and_the_year_so_far():
    days = dash.periods_of(TODAY)
    assert days["Daily"] == [TODAY] and days["Monthly"][0] == date(2026, 10, 1) and days["Monthly"][-1] == TODAY
    assert days["Yearly"][0] == date(2026, 1, 1) and len(days["Yearly"]) == 300 and len(days["Monthly"]) == 27


def reading(value) -> dict:
    return {"value": str(value), "unit": ""}


def test_a_tile_has_the_live_reading_and_its_extremes_with_the_live_reading_counted_in():
    live = {"outdoor": {"temperature": reading(31.0), "humidity": reading(70)}, "rainfall": {"daily": reading(0.4), "rain_rate": reading(0)}}
    first, today = date(2026, 1, 5), TODAY
    values = {"temp_max": {first: 38.0, today: 25.0}, "temp_min": {first: -1.0, today: 9.0},
              "humidity_max": {today: 80.0}, "humidity_min": {today: 40.0},
              "rain": {first: 12.0, date(2026, 10, 3): 6.0, today: 0.4}, "rain_rate_max": {first: 30.0}}
    tiles = {t.title: t for t in gauge_sheet.sheet(gauges_result(live, values, today)).tiles}
    temperature = tiles["Temperature"]
    assert temperature.value == 31.0 and temperature.columns == ("Max", "Min")
    assert temperature.rows == {"Daily": (31.0, 9.0), "Monthly": (31.0, 9.0), "Yearly": (38.0, -1.0)}   # 31 is now: higher than today's 25
    rain = tiles["Rain"]
    assert rain.columns == ("Max", "Total") and rain.kind == "beaker"
    assert rain.rows == {"Daily": (None, 0.4), "Monthly": (6.0, 6.4), "Yearly": (12.0, 18.4)}            # no "max" for a single day; today's rain is the gauge's
    assert tiles["Rain rate"].rows["Yearly"] == (30.0,) and tiles["Humidity"].decimals == 0


def test_a_reading_the_station_never_had_is_left_out_and_one_it_lacks_just_now_is_dimmed():
    live = {"outdoor": {"temperature": reading(12.0)}}
    sheet = gauge_sheet.sheet(gauges_result(live, {"solar_max": {TODAY: 900.0}}, TODAY))
    titles = [t.title for t in sheet.tiles]
    assert titles == ["Temperature", "Solar radiation"] and sheet.tiles[1].value is None      # no indoor sensor, no wind
    assert sheet.tiles[1].rows["Daily"] == (900.0,)
    assert gauge_sheet.sheet(gauges_result({}, {}, TODAY)) is None


def test_the_gauges_are_drawn_as_a_png_of_the_size_their_tiles_make():
    tile = Tile("Humidity", "%", 70, "dial", 0, 100, ("#DFFBFF", "#2C7592"), ("Max", "Min"), {"Daily": (87, 48)}, (0, 50, 100), 0)
    dull = Tile("Rain", "mm", None, "beaker", 0, 100, ("#DFFBFF", "#2C7592"), ("Max", "Total"), {}, (100, 0))
    image = Image.open(io.BytesIO(gauges.render(Dashboard([tile, tile, tile, dull]))))
    assert image.size == (3 * gauges.CELL_W + 2 * gauges.MARGIN, 2 * gauges.TILE_H + 2 * gauges.MARGIN)   # (no tables under them)
    assert image.convert("RGB").getpixel((2, 2)) == (0x14, 0x19, 0x1D)


async def current_of(eco, args: dict) -> dict:
    """The service's current over this station, called with these arguments."""
    return json.loads(await cur.CurrentTool(Context(eco=eco)).tool().call(args, None))


async def test_the_station_gives_its_gauges_from_the_live_reading_and_the_cache(tmp_path, daytime_archive):
    eco, _ = await archived_station(tmp_path, daytime_archive)
    sheet = gauge_sheet.sheet(await current_of(eco, gauge_sheet.call()))
    temperature = next(t for t in sheet.tiles if t.title == "Temperature")
    assert temperature.value == 12.3                                       # the fake station's live reading
    high, low = temperature.rows["Yearly"]
    assert high > 12.3 > low and len(temperature.rows) == 3
    assert gauges.render(sheet)[:4] == b"\x89PNG"
    await eco.close()


async def test_the_old_dashboards_and_reports_refresh_are_out_of_date_and_change_nothing(serve, alert_state):
    """The Telegram dashboard is no more (the gauges' live page is ecobot-mcp's /dashboard), nor the report written in code (step 6c):
    the 🔄 under one sent before is out of date, and its message stays as it was."""
    from lib.bot import STALE_BUTTON
    bot, _ = await report_bot(serve, alert_state, three_gauges)
    edits = []

    class Query:
        def __init__(self, data):
            self.data, self.toast, self.from_user = data, "unset", NS(id=7)
            self.message = NS(photo=[NS(file_id="x")], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄", callback_data=data)]]),
                              chat=NS(id=1, type="private"), chat_id=1)

        async def answer(self, text=None, **k):
            self.toast = text

        async def edit_message_media(self, *a, **k):
            edits.append(a)

        async def edit_message_caption(self, *a, **k):
            edits.append(a)
    for data in ("rp:dashboard", "rp:refresh"):
        query = Query(data)
        await bot.on_button(NS(callback_query=query, effective_chat=query.message.chat, effective_user=query.from_user,
                               effective_message=query.message), CONTEXT)
        assert query.toast == STALE_BUTTON and edits == [], data


def test_the_reports_gauges_are_temperature_humidity_and_rain_in_a_row():
    live = {"outdoor": {"temperature": reading(12.0), "humidity": reading(70)}, "rainfall": {"daily": reading(0.2)},
            "pressure": {"relative": reading(1020)}}
    sheet = gauge_sheet.sheet(gauges_result(live, {}, TODAY), dash.STATUS)
    assert [t.title for t in sheet.tiles] == ["Temperature", "Humidity", "Rain"] and sheet.per_row == 3
    image = Image.open(io.BytesIO(gauges.render(sheet)))
    assert image.size == (3 * gauges.CELL_W + 2 * gauges.MARGIN, gauges.TILE_H + 2 * gauges.MARGIN)   # one row


async def test_the_reports_gauges_read_the_years_day_rows_in_one_go_and_only_what_its_three_show(tmp_path, archived_cache):
    from tests.test_service_current import Station, live
    eco, _ = await archived_station(tmp_path, archived_cache)
    Station(eco, live(time.time()))
    reads, read = [], eco.storage.read

    async def counted(fn, *args):
        reads.append((fn, args))
        return await read(fn, *args)
    eco.storage.read = counted
    out = await current_of(eco, gauge_sheet.call(dash.STATUS))
    (mac, names, days), = [args for fn, args in reads if fn is cur.context_on]                 # one read, the year
    today = Context(eco=eco).now().date()
    assert names == ["humidity_max", "humidity_min", "rain", "temp_max", "temp_min"] and (days[0], days[-1]) == (date(today.year, 1, 1), today)
    assert [t.title for t in gauge_sheet.sheet(out, dash.STATUS).tiles] == list(dash.STATUS)
    await eco.close()


class ReportAgent:
    """The model, answering every question with the report it is asked for (Status: the `report` prompt's question)."""

    def __init__(self):
        self.asked = []

    async def run(self, messages, system, effort, **k):
        self.asked.append(messages[-1]["content"])
        messages.append({"role": "assistant", "content": "Weather station\n• Outdoor: 12.3 °C"})   # (as lib/llm.py's Agent does)
        return messages[-1]["content"]


async def report_bot(serve, alert_state, gauges_now):
    """A bot whose service gives the report's readings now (its `reading`, the outdoor temperature, which a test may change) and, with
    context, its gauges (`gauges`: current's result with context, or raises; gauges_now to begin with); its model a ReportAgent."""
    now = NS(reading="12.3 °C", gauges=gauges_now)

    def current(args, meta):
        if args.get("context"):
            assert args.get("readings") in (gauge_sheet.call(dash.STATUS)["readings"], None)   # the report's three gauges, or all (none named)
            return now.gauges()
        return {"lines": {"station": [f"• Outdoor: {now.reading}"], "air": ["• PM10: 3.0 µg/m³ 🟢 good"]},
                "headings": {"station": "Weather station", "air": "Air quality"}}
    service = StandInService(current=current)
    bot = Bot(NS(tz=TZ), ReportAgent(), alert_state, service=await serve(tools=service, sources=["Ecowitt", "AirGradient"]))
    alert_state.add_chat(1, "Rob")
    alert_state.set_keyboard(1, bot.keyboard_version)
    return bot, now


def three_gauges():
    """current's result with context for the report's three gauges."""
    return {"sensors": {"outdoor": {"temperature": {"value": 12.3, "unit": "°C"}, "humidity": {"value": 70, "unit": "%"}},
                        "rainfall": {"rain": {"value": 0.2, "unit": "mm"}}}}


def chat(sent):
    async def reply_photo(photo, caption=None, **kw):
        sent.append(("photo", photo[:4], caption, kw))
        return NS(message_id=7)

    async def reply_text(text, **kw):
        sent.append(("text", None, text, kw))
    msg = NS(chat_id=1, message_id=50, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo, reply_text=reply_text,
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
    return msg, NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))


async def no_action(*a, **k):
    pass


CONTEXT = NS(bot=NS(send_chat_action=no_action, send_message_draft=no_action, id=99))   # a Telegram bot that shows "typing..." and takes drafts


async def test_status_is_its_gauges_then_the_report_prompts_answer_from_the_model(serve, alert_state):
    """Status (the button, /status): the three gauges' picture at once, then ecobot-mcp's `report` prompt asked of the model as a typed
    question, its answer a message of its own (step 6c: the report written in code, its caption and its 🔄 are gone)."""
    from lib.mcp_server import prompt
    bot, tools = await report_bot(serve, alert_state, three_gauges)
    sent = []
    msg, update = chat(sent)
    await bot.status(update, CONTEXT)
    question = prompt("report").messages[0].content.text
    assert bot.agent.asked == [question]
    assert [(k, photo, text) for k, photo, text, _ in sent] == [("photo", b"\x89PNG", None), ("text", None, "Weather station\n• Outdoor: 12.3 °C")]
    assert "reply_markup" not in sent[0][3] and sent[1][3].get("reply_markup") is None                       # no 🔄, no period buttons
    history = bot.chats[(1, None)].history
    assert history[-2] == {"role": "user", "content": question} and history[-1]["content"] == "Weather station\n• Outdoor: 12.3 °C"


async def test_status_without_the_model_sends_its_gauges_and_the_line_saying_so(serve, alert_state):
    from lib.bot import NO_MODEL
    bot, _ = await report_bot(serve, alert_state, three_gauges)
    bot.agent = NS(ready=False)
    sent = []
    msg, update = chat(sent)
    await bot.status(update, CONTEXT)
    assert [(k, photo, text) for k, photo, text, _ in sent] == [("photo", b"\x89PNG", None), ("text", None, NO_MODEL)]


async def test_status_without_its_gauges_is_the_report_alone(serve, alert_state, caplog):
    def broken():
        raise RuntimeError("no cache")
    bot, _ = await report_bot(serve, alert_state, broken)
    sent = []
    msg, update = chat(sent)
    await bot.status(update, CONTEXT)
    assert [(k, text) for k, _, text, _ in sent] == [("text", "Weather station\n• Outdoor: 12.3 °C")]
    assert "The report's gauges failed (MCPError: Internal server error)" in caplog.text   # (refused by ecobot-mcp, which logs why:)
    assert "RuntimeError: no cache" in caplog.text


async def test_the_bands_the_bot_saw_widen_the_gauges_figures_and_the_year_takes_in_the_month(tmp_path):
    """Between its 5-minute readings the bot (and Home Assistant) saw lower and higher: the day rows take those bands in, so the gauges'
    Daily and Monthly highs and lows do, and the year's."""
    from lib.ecowitt.days import day_values_on
    from lib.storage import Storage
    from lib.timeutil import day_bounds
    from tests.fakes import MAC
    storage = Storage(tmp_path / "ecobot.sqlite", TZ)
    earlier = date(2026, 10, 9)

    def day(w, d, low, high, band):
        start, end = day_bounds(d, TZ, True)
        sensor = w.ecowitt_sensor(MAC, "outdoor")
        w.put(sensor, "5min", [{"ts": t, "temperature": low if t < start + 43200 else high} for t in range(start, end, 300)])
        w.cover(sensor, "5min", start, end)
        w.put_ranges(MAC, {"outdoor.temperature": {start + 43200: (4, *band)}})
    storage.writer.run(lambda w: (day(w, earlier, 5.0, 28.0, (3.5, 30.0)), day(w, TODAY, 15.0, 20.0, (14.0, 21.5))), ())
    live = {"outdoor": {"temperature": reading(18.0)}}
    values, _ = await storage.read(day_values_on, MAC, ["temp_max", "temp_min"], dash.periods_of(TODAY)["Yearly"])
    tile = gauge_sheet.sheet(gauges_result(live, values, TODAY), ("Temperature",)).tiles[0]
    assert tile.rows == {"Daily": (21.5, 14.0), "Monthly": (30.0, 3.5), "Yearly": (30.0, 3.5)}
    values = {"temp_max": {earlier: 28.0, TODAY: 20.0}, "temp_min": {earlier: 5.0, TODAY: 15.0}}
    plain = gauge_sheet.sheet(gauges_result(live, values, TODAY), ("Temperature",)).tiles[0]
    assert plain.rows == {"Daily": (20.0, 15.0), "Monthly": (28.0, 5.0), "Yearly": (28.0, 5.0)}   # the readings alone
    storage.close()


# ---------- the gauges from the service's current, with context (docs/ecobot-mcp.md 3.1, 6 step 2) ----------
STATUS_TITLES = ("Temperature", "Humidity", "Rain")
GAUGE_RESULT = {
    "sensors": {
        "outdoor": {"temperature": {"value": 12.3, "unit": "°C", "text": "12.3 °C",
                                    "today": {"high": 14.0, "low": 9.5}, "month": {"high": 21.0, "low": 4.0},
                                    "year": {"high": 38.0, "low": -1.0}},
                    "humidity": {"value": 94, "unit": "%", "text": "94 %", "today": {"high": 94, "low": 60}}},
        "rainfall": {"rain": {"value": 6.3, "unit": "mm", "text": "6.3 mm", "today": {"total": 6.3},
                              "month": {"total": 49.8, "wettest_day": {"rain": 20.1}}, "year": {"total": 400.2, "wettest_day": {"rain": 41.0}}}},
        "wind": {"wind_speed": {"value": 14, "unit": "km/h", "today": {"high_gust": 31.0}}},
        "indoor": {"temperature": {"value": None, "unit": "°C", "year": {"high": 30.0, "low": 12.0}}},   # no reading now: dimmed
        "solar_and_uvi": {"uvi": {"value": None, "unit": ""}}},                                          # nothing at all: left out
    "lines": {"station": []}, "headings": {"station": "Weather station"}}


def test_the_gauges_sheet_is_drawn_from_currents_readings_and_their_context():
    from lib import gauge_sheet
    sheet = gauge_sheet.sheet(GAUGE_RESULT)
    tiles = {t.title: t for t in sheet.tiles}
    assert list(tiles) == ["Temperature", "Humidity", "Wind", "Rain", "Indoor temperature"]   # the dashboard's order (GAUGES)
    t = tiles["Temperature"]
    assert (t.value, t.unit, t.kind, t.columns) == (12.3, "°C", "thermometer", ("Max", "Min"))
    assert t.rows == {"Daily": (14.0, 9.5), "Monthly": (21.0, 4.0), "Yearly": (38.0, -1.0)}
    assert tiles["Humidity"].value == 94.0 and isinstance(tiles["Humidity"].value, float)       # as the station's reading was read
    assert tiles["Humidity"].rows["Monthly"] == (None, None)
    assert tiles["Wind"].rows == {"Daily": (31.0,), "Monthly": (None,), "Yearly": (None,)}      # its highest gust
    assert tiles["Rain"].rows == {"Daily": (None, 6.3), "Monthly": (20.1, 49.8), "Yearly": (41.0, 400.2)}
    assert tiles["Indoor temperature"].value is None and tiles["Indoor temperature"].rows["Yearly"] == (30.0, 12.0)
    status = gauge_sheet.sheet(GAUGE_RESULT, STATUS_TITLES)
    assert [t.title for t in status.tiles] == list(STATUS_TITLES)
    assert gauge_sheet.sheet({"sensors": {}}) is None and gauge_sheet.sheet({"error": "x"}) is None
    assert gauge_sheet.sheet({**GAUGE_RESULT, "unavailable": {"station": "The weather station isn't answering right now."}}) is None


async def test_the_reports_gauges_are_the_services_current_with_context(serve, alert_state, monkeypatch):
    """Status's gauges are drawn from current {context: true} (the station's own dashboard() is not asked), its three readings
    named, by the service (lib/status.py)."""
    from tests.fakes import StandInService
    drawn = []
    monkeypatch.setattr(gauges, "render", lambda sheet: drawn.append(sheet) or b"\x89PNG")

    def current(args, meta):
        if args.get("context"):
            return GAUGE_RESULT
        return {"lines": {"station": ["• Outdoor: 12.3 °C"]}, "headings": {"station": "Weather station"}}
    service = StandInService(current=current)
    bot = Bot(NS(tz=TZ), ReportAgent(), alert_state, service=await serve(tools=service, sources=["Ecowitt"]))
    sent = []
    msg, update = chat(sent)
    await bot.status(update, CONTEXT)
    assert sent[0][0] == "photo" and [t.title for t in drawn[-1].tiles] == list(STATUS_TITLES)
    assert ("current", {"readings": ["outdoor.temperature", "outdoor.humidity", "rainfall.rain"], "context": True}) in service.calls


async def test_a_station_that_gives_nothing_sends_its_report_without_gauges(serve, alert_state):
    from tests.fakes import StandInService
    down = {"sensors": {}, "lines": {"station": ["• not available right now"]}, "headings": {"station": "Weather station"},
            "unavailable": {"station": "The weather station isn't answering right now."}}
    service = StandInService(current=down)
    bot = Bot(NS(tz=TZ), ReportAgent(), alert_state, service=await serve(tools=service, sources=["Ecowitt"]))
    sent = []
    msg, update = chat(sent)
    await bot.status(update, CONTEXT)
    assert [k for k, *_ in sent] == ["text"]                                                     # the report as text alone


# ---------- the air sensor's gauges, on US standards ----------
def test_the_air_gauges_scales_and_bands_are_the_epas_and_the_us_co2_guidance():
    from lib.airgradient.dashboard import AIR_GAUGES, EPA, EPA_NAMES
    g = {x.title: x for x in AIR_GAUGES}
    assert list(g) == ["US AQI", "PM2.5", "PM10", "PM1", "CO₂", "VOC index", "NOx index"]               # no own temperature
    assert (g["PM1"].bounds, g["PM1"].colours, g["PM1"].names) == (g["PM2.5"].bounds, g["PM2.5"].colours, g["PM2.5"].names)   # PM2.5's bands
    assert EPA == ("#00E400", "#FFFF00", "#FF7E00", "#FF0000", "#8F3F97", "#7E0023")
    assert EPA_NAMES == ("Good", "Moderate", "Unhealthy for Sensitive Groups", "Unhealthy", "Very Unhealthy", "Hazardous")
    assert g["US AQI"].bounds == (0, 50, 100, 150, 200, 300, 500) and g["US AQI"].colours == EPA and g["US AQI"].aqi
    assert g["PM2.5"].bounds == (0, 9.0, 35.4, 55.4, 125.4, 225.4, 325.4) and g["PM2.5"].colours == EPA   # the 2024 breakpoints
    assert g["PM10"].bounds == (0, 54, 154, 254, 354, 424, 604) and g["PM10"].colours == EPA
    assert g["CO₂"].bounds == (400, 800, 1000, 1500, 2000, 2500)
    for title, readings in {
            "US AQI": ((0, 0), (50, 0), (51, 1), (100, 1), (101, 2), (150, 2), (151, 3), (200, 3), (201, 4), (300, 4), (301, 5), (480, 5)),
            "PM2.5": ((0, 0), (9.0, 0), (9.1, 1), (35.4, 1), (35.5, 2), (55.4, 2), (55.5, 3), (125.4, 3), (125.5, 4), (225.4, 4),
                      (225.5, 5), (500, 5)),
            "PM10": ((54, 0), (55, 1), (154, 1), (155, 2), (254, 2), (255, 3), (354, 3), (355, 4), (424, 4), (425, 5), (700, 5))}.items():
        for value, band in readings:
            assert g[title].band(value) == EPA_NAMES[band], (title, value)
    co2 = g["CO₂"]
    assert [co2.band(v) for v in (420, 799, 800, 999, 1000, 1499, 1500, 1999, 2000, 3000)] == \
        ["Good", "Good", "Fair", "Fair", "Poor", "Poor", "Bad", "Bad", "Very bad", "Very bad"]
    assert [g["VOC index"].band(v) for v in (1, 99, 100, 150, 250, 400)] == ["Low", "Low", "Average", "Elevated", "High", "Very high"]
    assert [g["NOx index"].band(v) for v in (1, 20, 21, 150, 151, 300, 301)] == ["Low", "Low", "Elevated", "Elevated", "High", "High",
                                                                                  "Very high"]
    assert g["PM2.5"].band(None) is None


def air_result(latest: dict | None, air_values: dict | None = None) -> dict:
    live = {"outdoor": {"temperature": reading(12.0)}}
    return gauges_result(live, {}, TODAY, latest, air_values)


def test_the_air_sensors_tiles_come_after_the_stations_with_their_aqi_bands_and_figures():
    from lib.airgradient.metrics import pm25_aqi
    latest = {"pm2_5": {"value": 40.0, "unit": "µg/m³"}, "pm10": {"value": 60.0, "unit": "µg/m³"}, "pm1": {"value": 20.0, "unit": "µg/m³"},
              "co2": {"value": 1200, "unit": "ppm"}}
    values = {"pm2_5_max": {date(2026, 10, 2): 70.0, TODAY: 30.0}, "pm2_5_min": {TODAY: 5.0}, "co2_max": {TODAY: 900}, "co2_min": {TODAY: 410}}
    sheet = gauge_sheet.sheet(air_result(latest, values))
    tiles = {t.title: t for t in sheet.tiles}
    assert list(tiles) == ["Temperature", "US AQI", "PM2.5", "PM10", "PM1", "CO₂"]              # no VOC nor NOx sent: left out
    aqi = tiles["US AQI"]
    assert (aqi.value, aqi.band, aqi.kind, aqi.low, aqi.high, aqi.columns) == (pm25_aqi(40.0)[0], "Unhealthy for Sensitive Groups", "dial",
                                                                               0, 500, ("Max", "Min"))
    assert aqi.rows["Daily"] == (pm25_aqi(40.0)[0], pm25_aqi(5.0)[0]) and aqi.rows["Monthly"][0] == pm25_aqi(70.0)[0]
    assert aqi.ticks == aqi.bounds == (0, 50, 100, 150, 200, 300, 500)
    pm = tiles["PM2.5"]
    assert (pm.value, pm.band, pm.decimals, pm.rows["Monthly"]) == (40.0, "Unhealthy for Sensitive Groups", 1, (70.0, 5.0))
    assert (tiles["PM10"].band, tiles["CO₂"].band, tiles["CO₂"].rows["Daily"]) == ("Moderate", "Poor", (1200.0, 410.0))
    assert [t.title for t in gauge_sheet.sheet(air_result(None)).tiles] == ["Temperature"]   # no air sensor: no air gauges


def test_the_aqi_and_pm25_tiles_name_the_band_their_needle_is_in_on_a_rise():
    """The service rates PM2.5 (its aqi_us and band) on the lower of the last two readings; the dials show the reading now, so their
    numbers and names are of the reading now: a rise from 8 to 40 µg/m³ is Unhealthy for Sensitive Groups on both, not Good."""
    from lib.airgradient.dashboard import EPA_NAMES
    from lib.airgradient.metrics import pm25_aqi
    from lib.gauge_look import share
    latest = {"pm2_5": {"value": 40.0, "unit": "µg/m³", "aqi_us": pm25_aqi(8.0)[0], "band": "good"}}   # (as latest() gives it after 8.0)
    tiles = {t.title: t for t in gauge_sheet.sheet(air_result(latest)).tiles}
    aqi, pm = tiles["US AQI"], tiles["PM2.5"]
    assert (aqi.value, aqi.band, pm.value, pm.band) == (pm25_aqi(40.0)[0], "Unhealthy for Sensitive Groups", 40.0,
                                                        "Unhealthy for Sensitive Groups")
    assert 2 / 6 <= share(pm, pm.value) < 3 / 6 and 2 / 6 <= share(aqi, aqi.value) < 3 / 6   # both needles in the orange band
    for value in (4.0, 9.1, 20.0, 35.5, 45.0, 55.5, 90.0, 125.5, 175.0, 225.5, 300.0):   # each band, and just over each edge
        tiles = {t.title: t for t in gauge_sheet.sheet(air_result({"pm2_5": {"value": value, "unit": "µg/m³"}})).tiles}
        pm, aqi = tiles["PM2.5"], tiles["US AQI"]
        assert pm.band == aqi.band == EPA_NAMES[min(int(share(pm, value) * 6), 5)] == EPA_NAMES[min(int(share(aqi, aqi.value) * 6), 5)]


def test_a_tile_with_no_reading_is_dimmed_at_its_floor_in_its_json():
    from lib.gauge_look import floor, share, shown, tile_json
    latest = {"pm2_5": {"value": 12.0, "unit": "µg/m³"}}
    values = {"co2_max": {TODAY: 900}, "co2_min": {TODAY: 410}}
    sheet = gauge_sheet.sheet(gauges_result({"outdoor": {"temperature": reading(12.0)}, "pressure": {"relative": reading(1012)}},
                                            {"humidity_max": {TODAY: 80.0}, "humidity_min": {TODAY: 50.0}, "pressure_max": {TODAY: 1020.0}},
                                            TODAY, latest, values))
    tiles = {t.title: t for t in sheet.tiles}
    humidity, co2, aqi = tiles["Humidity"], tiles["CO₂"], tiles["US AQI"]
    assert humidity.value is None and co2.value is None and co2.band is None                    # figures, no reading now
    assert (floor(humidity), floor(co2), floor(tiles["Atmospheric pressure"]), floor(aqi)) == (0, 400, 950, 0)
    assert shown(co2) == 400 and share(co2, shown(co2)) == 0 and share(humidity, 0) == 0
    got = tile_json(co2)
    assert (got["value"], got["faded"], got["floor"], got["rows"]["Daily"]) == (None, True, 400, [900, 410])
    assert tile_json(aqi)["faded"] is False and tile_json(tiles["Atmospheric pressure"])["floor"] == 950


def test_a_band_scale_gives_each_band_an_equal_share_of_the_dial():
    from lib.gauge_look import share
    pm = gauge_sheet.sheet(air_result({"pm2_5": {"value": 9.0, "unit": "µg/m³"}})).tiles[2]
    assert pm.title == "PM2.5"
    assert [round(share(pm, v), 4) for v in (0, 9.0, 35.4, 55.4, 325.4, 400)] == [0, round(1 / 6, 4), round(2 / 6, 4), 0.5, 1, 1]
    assert share(pm, 22.2) == pytest.approx((1 + (22.2 - 9) / (35.4 - 9)) / 6) and share(pm, -1) == 0


def test_the_png_draws_a_tile_with_no_reading_as_its_floor_dimmed():
    """The dimmed tile is the same gauge at its floor, every colour FADE of the way from the card: the needle at the dial's start, the
    mercury at the scale's low end, the beaker empty, the number its floor."""
    from PIL import ImageChops
    from lib.gauge_look import BG, FADE
    for tile in (Tile("Atmospheric pressure", "hPa", None, "dial", 950, 1050, dash.PRESSURE, ("Max", "Min"), {"Daily": (1020.0, 1001.0)},
                      (950, 1000, 1050)),
                 Tile("Temperature", "°C", None, "thermometer", 0, 40, dash.COLD_TO_HOT[:4], ("Max", "Min"), {"Daily": (20.0, 8.0)}, dash.DEGREES),
                 Tile("Rain", "mm", None, "beaker", 0, 100, dash.WATER, ("Max", "Total"), {"Monthly": (6.0, 9.0)}, (100, 0)),
                 Tile("CO₂", "ppm", None, "dial", 400, 2500, ("#00E400",) * 5, ("Max", "Min"), {"Daily": (900, 410)},
                      (400, 800, 1000, 1500, 2000, 2500), 0, (400, 800, 1000, 1500, 2000, 2500), "Good")):
        from lib.gauge_look import floor
        dimmed = Image.open(io.BytesIO(gauges.render(Dashboard([tile])))).convert("RGB")
        at_floor = Tile(**{**tile.__dict__, "value": floor(tile), "band": None})
        bright = Image.open(io.BytesIO(gauges.render(Dashboard([at_floor])))).convert("RGB")
        expected = Image.blend(bright, Image.new("RGB", bright.size, BG), 1 - FADE)
        off = ImageChops.difference(dimmed, expected).convert("L").histogram()
        mean, worst = sum(i * n for i, n in enumerate(off)) / sum(off), max(i for i, n in enumerate(off) if n)
        assert mean < 0.15 and worst <= 20, (tile.title, mean, worst)   # (but the edges' anti-aliasing; the gauge anywhere else: 80+)
        assert bright.getdata() != dimmed.getdata()
