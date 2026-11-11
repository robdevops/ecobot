import io
from datetime import date, datetime
from types import SimpleNamespace as NS

from PIL import Image
from telegram import InlineKeyboardMarkup

import json
import time

from lib import gauge_sheet, gauges, intent
from lib.bot import Bot
from lib.ecowitt import dashboard as dash
from lib.service import current as cur
from lib.service.context import Context
from lib.specs import Dashboard, Tile
from tests.fakes import TZ, StandInService, archived_station, gauges_result

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
    assert image.size == (3 * gauges.CELL_W + 2 * gauges.MARGIN, 2 * gauges.CELL_H + 2 * gauges.MARGIN)
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


class Tools:
    async def call(self, *a, **k):
        raise AssertionError("the dashboard is drawn in code")

    call_all = call


async def test_the_dashboard_command_sends_one_picture_with_no_period_buttons_and_asks_no_model(alert_state, monkeypatch):
    sent = []
    service = StandInService(current={"sensors": {"outdoor": {"humidity": {"value": 70, "unit": "%", "today": {"high": 87, "low": 60}}}}})

    class Agent:
        tools = Tools()

        async def run(self, *a, **k):
            raise AssertionError("no model for the dashboard")
    source = NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt")
    bot = Bot(NS(tz=TZ), Agent(), [source], alert_state, service=service)

    async def reply_photo(photo, caption=None, **kw):
        sent.append((photo[:4], caption, kw.get("reply_markup")))
        return NS(message_id=7)
    msg = NS(chat_id=1, message_id=50, message_thread_id=None, is_topic_message=False, reply_photo=reply_photo, text="Dashboard",
             chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)

    context = CONTEXT
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    await bot.respond(update, context, "Dashboard")
    assert service.calls == [("current", gauge_sheet.call())] and len(sent) == 1
    photo, caption, markup = sent[0]
    assert photo == b"\x89PNG" and caption.startswith("Weather station dashboard, ")
    assert not isinstance(markup, InlineKeyboardMarkup)                         # a chat whose keyboard is out of date gets that instead
    assert bot.charted.get(1, 7) is None
    bot.state.add_chat(1, "private")
    bot.state.set_keyboard(1, bot.keyboard_version)
    await bot.respond(update, context, "Dashboard")
    from lib import report_menu
    assert [b.callback_data for b in sent[-1][2].inline_keyboard[0]] == [report_menu.DASHBOARD]   # its 🔄, no period buttons


async def test_the_dashboards_refresh_draws_it_again_in_the_same_message_and_a_station_that_fails_leaves_it(alert_state):
    from lib import report_menu
    from lib.bot import STALE_BUTTON
    bot, now = report_bot(alert_state, three_gauges)
    edits = []

    class Query:
        data, message, toast = report_menu.DASHBOARD, NS(photo=[NS(file_id="x")]), "unset"

        async def answer(self, text=None, **k):
            self.toast = text

        async def edit_message_media(self, media, reply_markup=None):
            edits.append((media.media.input_file_content[:4], media.caption, reply_markup))
    query = Query()
    await bot.on_report_button(NS(callback_query=query), CONTEXT)
    (photo, caption, markup), = edits
    assert photo == b"\x89PNG" and caption.startswith("Weather station dashboard, ") and query.toast is None
    assert markup.inline_keyboard[0][0].callback_data == report_menu.DASHBOARD                               # the 🔄 stays

    def failing():
        raise RuntimeError("station down")
    now.gauges = failing
    query = Query()
    await bot.on_report_button(NS(callback_query=query), CONTEXT)
    assert len(edits) == 1 and "Couldn't refresh" in query.toast                                              # left as it was
    query = Query()
    query.message = NS(photo=None)
    await bot.on_report_button(NS(callback_query=query), CONTEXT)
    assert len(edits) == 1 and query.toast == STALE_BUTTON


def test_the_reports_gauges_are_temperature_humidity_and_rain_in_a_row():
    live = {"outdoor": {"temperature": reading(12.0), "humidity": reading(70)}, "rainfall": {"daily": reading(0.2)},
            "pressure": {"relative": reading(1020)}}
    sheet = gauge_sheet.sheet(gauges_result(live, {}, TODAY), dash.STATUS)
    assert [t.title for t in sheet.tiles] == ["Temperature", "Humidity", "Rain"] and sheet.per_row == 3
    image = Image.open(io.BytesIO(gauges.render(sheet)))
    assert image.size == (3 * gauges.CELL_W + 2 * gauges.MARGIN, gauges.CELL_H + 2 * gauges.MARGIN)   # one row


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


def report_bot(alert_state, gauges_now):
    """A bot whose service gives the report's readings now (its `reading`, the outdoor temperature, which a test may change) and, with
    context, its gauges (`gauges`: current's result with context, or raises; gauges_now to begin with)."""
    now = NS(reading="12.3 °C", gauges=gauges_now)

    def current(args, meta):
        if args.get("context"):
            assert args.get("readings") in (gauge_sheet.call(dash.STATUS)["readings"], gauge_sheet.call()["readings"])   # the report's three gauges, or all
            return now.gauges()
        return {"lines": {"station": [f"• Outdoor: {now.reading}"], "air": ["• PM10: 3.0 µg/m³ 🟢 good"]},
                "headings": {"station": "Weather station", "air": "Air quality"}}
    service = StandInService(current=current)

    class Agent:
        pass

    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt"),
               NS(name="AirGradient", wants=lambda t: True, poke=lambda: None, describe=lambda: "AirGradient")]
    bot = Bot(NS(tz=TZ), Agent(), sources, alert_state, service=service)
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


async def test_the_report_is_its_gauges_with_the_report_as_their_caption_and_refresh_redraws_both(alert_state, monkeypatch):
    from lib import report_menu
    bot, tools = report_bot(alert_state, three_gauges)
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
    tools.reading = "13.0 °C"
    await bot.on_report_button(NS(callback_query=Query()), CONTEXT)
    what, caption, entities, markup = edits[-1]
    assert what == "media" and "13.0 °C" in caption and [e.type for e in entities] == ["bold", "bold"]
    assert markup.inline_keyboard[0][0].callback_data == report_menu.REFRESH

    def failing():
        raise RuntimeError("cache gone")
    tools.gauges = failing
    tools.reading = "14.0 °C"
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
    assert "Service tool current failed: RuntimeError: no cache" in caplog.text


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


async def test_the_dashboard_and_the_reports_gauges_are_the_services_current_with_context(alert_state, monkeypatch):
    """The bot draws its gauges from current {context: true} (the station's own dashboard() is not asked): the whole dashboard from
    every gauge's reading, the report's from its three."""
    from tests.fakes import StandInService
    drawn = []
    monkeypatch.setattr(gauges, "render", lambda sheet: drawn.append(sheet) or b"\x89PNG")

    def current(args, meta):
        if args.get("context"):
            return GAUGE_RESULT
        return {"lines": {"station": ["• Outdoor: 12.3 °C"]}, "headings": {"station": "Weather station"}}
    service = StandInService(current=current)

    async def no_dashboard(titles=None):
        raise AssertionError("the gauges come from the service's current")
    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt", dashboard=no_dashboard)]
    bot = Bot(NS(tz=TZ), NS(), sources, alert_state, service=service)
    sent = []
    msg, update = chat(sent)
    await bot.respond(update, CONTEXT, "dashboard")
    assert [k for k, *_ in sent] == ["photo"] and sent[0][2].startswith("Weather station dashboard, ")
    from lib import gauge_sheet
    assert drawn[-1] == gauge_sheet.sheet(GAUGE_RESULT)
    assert ("current", {"readings": [g.reading for g in dash.GAUGES], "context": True}) in service.calls   # every gauge's: the air isn't asked
    await bot.respond(update, CONTEXT, "report")
    assert sent[-1][0] == "photo" and [t.title for t in drawn[-1].tiles] == list(STATUS_TITLES)
    assert ("current", {"readings": ["outdoor.temperature", "outdoor.humidity", "rainfall.rain"], "context": True}) in service.calls


async def test_a_station_that_gives_nothing_is_not_answering_and_its_report_goes_without_gauges(alert_state):
    from tests.fakes import StandInService
    down = {"sensors": {}, "lines": {"station": ["• not available right now"]}, "headings": {"station": "Weather station"},
            "unavailable": {"station": "The weather station isn't answering right now."}}
    service = StandInService(current=down)
    sources = [NS(name="Ecowitt", wants=lambda t: True, poke=lambda: None, describe=lambda: "Ecowitt")]
    bot = Bot(NS(tz=TZ), NS(), sources, alert_state, service=service)
    sent = []
    msg, update = chat(sent)
    await bot.respond(update, CONTEXT, "dashboard")
    assert [(k, text) for k, _, text, _ in sent] == [("text", "The weather station isn't answering right now.")]
    await bot.respond(update, CONTEXT, "report")
    assert sent[-1][0] == "text"                                                                 # the report as text alone
