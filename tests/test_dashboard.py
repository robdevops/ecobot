import io
from datetime import date, datetime
from types import SimpleNamespace as NS

import pytest
from PIL import Image
from telegram import InlineKeyboardMarkup

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

    context = CONTEXT
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await bot.respond(update, context, "Dashboard")
    assert asked == [1] and len(sent) == 1
    photo, caption, markup = sent[0]
    assert photo == b"\x89PNG" and caption.startswith("Weather station dashboard, ")
    assert not isinstance(markup, InlineKeyboardMarkup)                         # the chat's own keyboard may ride on it, never the period buttons
    assert bot.charted.get(1, 7) is None


def test_the_reports_gauges_are_temperature_humidity_and_rain_in_a_row():
    live = {"outdoor": {"temperature": reading(12.0), "humidity": reading(70)}, "rainfall": {"daily": reading(0.2)},
            "pressure": {"relative": reading(1020)}}
    sheet = dash.sheet(live, {}, TODAY, dash.STATUS)
    assert [t.title for t in sheet.tiles] == ["Temperature", "Humidity", "Rain"] and sheet.per_row == 3
    image = Image.open(io.BytesIO(gauges.render(sheet)))
    assert image.size == (3 * gauges.CELL_W + 2 * gauges.MARGIN, gauges.CELL_H + 2 * gauges.MARGIN)   # one row


async def test_the_dashboard_reads_the_years_day_rows_in_one_go_and_only_what_its_gauges_show(monkeypatch):
    calls = []

    def day_values_on(r, mac, names, days):
        calls.append((tuple(names), min(days), max(days)))
        return {n: {d: 1.0 for d in days} for n in names}, {}
    monkeypatch.setattr(dash, "day_values_on", day_values_on)

    class Storage:
        async def read(self, fn, *args):
            return fn(None, *args)
    sheet = await dash.dashboard({"outdoor": {"temperature": reading(12.0)}}, Storage(), "mac", TODAY, dash.STATUS)
    assert calls == [(("humidity_max", "humidity_min", "rain", "temp_max", "temp_min"), date(2026, 1, 1), TODAY)]   # one read, the year
    assert [t.title for t in sheet.tiles] == list(dash.STATUS)


def report_bot(alert_state, sheet):
    """A bot whose station gives the report's tool results and, through dashboard(), its gauges (`sheet()`: a Dashboard, or raises)."""
    import json

    from tests.fakes import StandInTools

    class Tools(StandInTools):
        reading = "12.3 ℃"

        async def call(self, name, raw, turn=None):
            return {"weather_now": json.dumps({"outdoor": {"temperature": self.reading}}),
                    "air_quality": json.dumps({"pm10": {"value": 3.0, "unit": "µg/m³", "rating": "🟢 good"}})}[name]

    class Agent:
        tools = Tools()

    async def dashboard(titles=None):
        assert titles == dash.STATUS
        return sheet()
    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt", dashboard=dashboard),
               NS(name="AirGradient", wants=lambda t: True, poke=lambda: None, describe=lambda: "AirGradient")]
    bot = Bot(NS(tz=TZ), Agent(), sources, alert_state)
    alert_state.add_chat(1, "Rob")
    alert_state.set_keyboard(1, bot.keyboard_version)
    return bot, Agent.tools


def three_tiles():
    tile = Tile("Humidity", "%", 70, "dial", 0, 100, ("#DFFBFF", "#2C7592"), ("Max",), {"Daily": (87,)}, (0, 100), 0)
    return Dashboard([tile, tile, tile])


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


async def test_the_report_is_its_gauges_with_the_report_as_their_caption_and_refresh_redraws_both(alert_state, monkeypatch):
    from lib import report_menu
    bot, tools = report_bot(alert_state, three_tiles)
    sent = []
    msg, update = chat(sent)
    await bot.respond(update, CONTEXT, "report")
    assert len(sent) == 1
    kind, photo, caption, kw = sent[0]
    assert kind == "photo" and photo == b"\x89PNG" and caption.startswith("Weather station\n• Outdoor: 12.3 °C")
    assert [e.type for e in kw["caption_entities"]] == ["bold", "bold"]                                        # the headings, as in the text report
    assert [b.callback_data for b in kw["reply_markup"].inline_keyboard[0]] == [report_menu.REFRESH]         # Refresh, no period buttons
    assert bot.charted.get(1, 7) is None

    edits = []

    class Query:
        data, message = report_menu.REFRESH, NS(photo=[NS(file_id="x")])

        async def answer(self, *a, **k):
            pass

        async def edit_message_media(self, media, reply_markup=None):
            edits.append(("media", media.caption, media.caption_entities, reply_markup))

        async def edit_message_caption(self, caption=None, caption_entities=None, reply_markup=None):
            edits.append(("caption", caption, caption_entities, reply_markup))
    tools.reading = "13.0 ℃"
    await bot.on_report_button(NS(callback_query=Query()), CONTEXT)
    what, caption, entities, markup = edits[-1]
    assert what == "media" and "13.0 °C" in caption and [e.type for e in entities] == ["bold", "bold"]
    assert markup.inline_keyboard[0][0].callback_data == report_menu.REFRESH

    async def failing(titles=None):
        raise RuntimeError("cache gone")
    bot.by_name["Ecowitt"].dashboard = failing
    tools.reading = "14.0 ℃"
    await bot.on_report_button(NS(callback_query=Query()), CONTEXT)
    assert edits[-1][0] == "caption" and "14.0 °C" in edits[-1][1]                                             # no new picture: the caption alone


async def test_a_report_without_its_gauges_is_the_text_report_as_before(alert_state, caplog):
    from lib import report_menu

    def broken():
        raise RuntimeError("no cache")
    bot, _ = report_bot(alert_state, broken)
    sent = []
    msg, update = chat(sent)
    await bot.respond(update, CONTEXT, "report")
    assert [k for k, *_ in sent] == ["text"] and sent[0][3]["reply_markup"].inline_keyboard[0][0].callback_data == report_menu.REFRESH
    assert "gauges failed" in caplog.text


async def test_a_report_too_long_for_a_caption_goes_after_its_gauges_with_the_refresh_button_on_the_text():
    from lib.bot import deliver
    from lib import report_menu
    sent = []
    msg, _ = chat(sent)
    text = "Weather station\n" + "\n".join(f"• line {i} " + "x" * 40 for i in range(30))
    await deliver(msg, text, [b"\x89PNG"], markup=report_menu.keyboard(), titles=[""], bold_headings=True, photos_first=True)
    assert [k for k, *_ in sent] == ["photo", "text"]
    assert "reply_markup" not in sent[0][3] and sent[1][3]["reply_markup"].inline_keyboard[0][0].callback_data == report_menu.REFRESH


def test_a_refreshed_report_too_long_for_its_caption_keeps_its_first_sections():
    from lib.bot import CAPTION_LIMIT, report_caption
    from lib.entities import utf16_len
    first = "Weather station (2:21am)\n• Outdoor: 12.3 °C"
    long = first + "\n\nAir quality\n" + "\n".join(f"• line {i} 🟢" + "x" * 30 for i in range(40))
    body, entities = report_caption(long)
    assert body == first and [e.type for e in entities] == ["bold"]
    body, _ = report_caption(first + "\n\nForecast\n• Today: fine")
    assert body.endswith("• Today: fine")                                                                  # it fits: all of it
    body, _ = report_caption("Weather station\n" + "\n".join(f"• {i} " + "y" * 60 for i in range(40)))
    assert utf16_len(body) <= CAPTION_LIMIT and body.startswith("Weather station\n• 0 ")                    # one long section: its first lines


async def test_the_bands_the_bot_saw_widen_the_gauges_figures_and_the_year_takes_in_the_month(tmp_path):
    """Between its 5-minute readings the bot (and Home Assistant) saw lower and higher: the day rows take those bands in, so the gauges'
    Daily and Monthly highs and lows do, and the year's."""
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
    tile = (await dash.dashboard(live, storage, MAC, TODAY, ("Temperature",))).tiles[0]
    assert tile.rows == {"Daily": (21.5, 14.0), "Monthly": (30.0, 3.5), "Yearly": (30.0, 3.5)}
    values = {"temp_max": {earlier: 28.0, TODAY: 20.0}, "temp_min": {earlier: 5.0, TODAY: 15.0}}
    plain = dash.sheet(live, values, TODAY, ("Temperature",)).tiles[0]
    assert plain.rows == {"Daily": (20.0, 15.0), "Monthly": (28.0, 5.0), "Yearly": (28.0, 5.0)}   # the readings alone
    storage.close()
