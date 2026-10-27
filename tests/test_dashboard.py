import io
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest
from PIL import Image
from telegram import InlineKeyboardMarkup

from lib import bot as botmod
from lib import gauges, intent
from lib.bot import Bot
from lib.ecowitt import dashboard as dash
from lib.specs import Dashboard, Tile
from tests.fakes import TZ, archived_station

TODAY = date(2026, 10, 27)
NOW = datetime(2026, 10, 27, 12, tzinfo=TZ)


def test_a_dashboard_is_asked_for_in_a_word_or_two_and_is_not_the_report():
    for said in ("dashboard", "Dashboard", "show me the dashboard", "gauges", "weather dashboard", "dashboard now"):
        read = intent.read(said, NOW)
        assert read.dashboard and not read.report, said
    for said in ("report", "status", "dashboard for last week", "how do I use the dashboard on my phone", "weather"):
        assert not intent.read(said, NOW).dashboard, said
    assert intent.read("report", NOW).report
    assert not intent.read("dashboard", NOW, ("AirGradient",)).dashboard          # the station's gauges need the station


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
    tiles = {t.title: t for t in dash.sheet(live, values, today).tiles}
    temperature = tiles["Temperature"]
    assert temperature.value == 31.0 and temperature.columns == ("Max", "Min")
    assert temperature.rows == {"Daily": (31.0, 9.0), "Monthly": (31.0, 9.0), "Yearly": (38.0, -1.0)}   # 31 is now: higher than today's 25
    rain = tiles["Rain"]
    assert rain.columns == ("Max", "Total") and rain.kind == "beaker"
    assert rain.rows == {"Daily": (None, 0.4), "Monthly": (6.0, 6.4), "Yearly": (12.0, 18.4)}            # no "max" for a single day; today's rain is the gauge's
    assert tiles["Rain rate"].rows["Yearly"] == (30.0,) and tiles["Humidity"].decimals == 0


def test_a_reading_the_station_never_had_is_left_out_and_one_it_lacks_just_now_is_dimmed():
    live = {"outdoor": {"temperature": reading(12.0)}}
    sheet = dash.sheet(live, {"solar_max": {TODAY: 900.0}}, TODAY)
    titles = [t.title for t in sheet.tiles]
    assert titles == ["Temperature", "Solar radiation"] and sheet.tiles[1].value is None      # no indoor sensor, no wind
    assert sheet.tiles[1].rows["Daily"] == (900.0,)
    assert dash.sheet({}, {}, TODAY) is None


def test_the_gauges_are_drawn_as_a_png_of_the_size_their_tiles_make():
    tile = Tile("Humidity", "%", 70, "dial", 0, 100, ("#DFFBFF", "#2C7592"), ("Max", "Min"), {"Daily": (87, 48)}, (0, 50, 100), 0)
    dull = Tile("Rain", "mm", None, "beaker", 0, 100, ("#DFFBFF", "#2C7592"), ("Max", "Total"), {}, (100, 0))
    image = Image.open(io.BytesIO(gauges.render(Dashboard([tile, tile, tile, dull]))))
    assert image.size == (3 * gauges.CELL_W + 2 * gauges.MARGIN, 2 * gauges.CELL_H + 2 * gauges.MARGIN)
    assert image.convert("RGB").getpixel((2, 2)) == (0x14, 0x19, 0x1D)
    with pytest.raises(ValueError):
        Tile("x", "", 1, "gauge", 0, 1, ("#fff",))
    with pytest.raises(ValueError):
        Tile("x", "", 1, "dial", 0, 1, ("#fff",), ("Max",), {"Daily": (1.0, 2.0)})


async def test_the_station_gives_its_gauges_from_the_live_reading_and_the_cache(tmp_path, archived_cache):
    eco, _ = await archived_station(tmp_path, archived_cache)
    sheet = await eco.dashboard()
    temperature = next(t for t in sheet.tiles if t.title == "Temperature")
    assert temperature.value == 12.3                                       # the fake station's live reading
    high, low = temperature.rows["Yearly"]
    assert high > 12.3 > low and len(temperature.rows) == 3
    assert gauges.render(sheet)[:4] == b"\x89PNG"
    await eco.close()


class Tools:
    async def call(self, *a, **k):
        raise AssertionError("the dashboard is drawn in code")

    call_all = call


async def test_the_dashboard_command_sends_one_picture_with_no_period_buttons_and_asks_no_model(alert_state, monkeypatch):
    sent, asked = [], []

    async def sheet():
        asked.append(1)
        return Dashboard([Tile("Humidity", "%", 70, "dial", 0, 100, ("#DFFBFF", "#2C7592"), ("Max",), {"Daily": (87,)}, (0, 100), 0)])

    class Agent:
        tools = Tools()

        async def run(self, *a, **k):
            raise AssertionError("no model for the dashboard")
    source = NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt", dashboard=sheet)
    bot = Bot(NS(tz=TZ), Agent(), [source], alert_state)

    async def reply_photo(photo, caption=None, **kw):
        sent.append((photo[:4], caption, kw.get("reply_markup")))
        return NS(message_id=7)
    msg = NS(chat_id=1, message_id=50, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo, text="Dashboard",
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)

    async def no_action(*a, **k):
        pass
    context = NS(bot=NS(send_chat_action=no_action, id=99))
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await bot.respond(update, context, "Dashboard")
    assert asked == [1] and len(sent) == 1
    photo, caption, markup = sent[0]
    assert photo == b"\x89PNG" and caption.startswith("Weather station dashboard, ")
    assert not isinstance(markup, InlineKeyboardMarkup)                         # the chat's own keyboard may ride on it, never the period buttons
    assert bot.charted.get(1, 7) is None
