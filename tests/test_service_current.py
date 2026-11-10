"""The `current` tool (lib/service/current.py): the readings now from both sensors, every one or those named, with the gauges' highs and
lows beside them on request, and the bot's own words for them (its report, "weather now" and plain lookups), unchanged."""

import json
import re
import time
from datetime import date, datetime

import pytest

from tests import report_words as report   # (the bot's words before step 2: the oracle)
from lib.airgradient import AirGradient
from lib.ecowitt.days import day_values_on
from lib.intent import NOW_GROUPS
from lib.service import current as cur
from lib.service.common import FRESH
from lib.service.context import Context
from lib.service.current import CurrentTool, heading
from lib.service.names import AIR_DEVICE, STATION, Catalogue
from lib.timeutil import clock_text, to_local
from tests.fakes import TZ, air_transport, archived_station, config

WHEN = re.compile(r"^(at|around) \d{1,2}(:\d\d)?[ap]m$|^sometime between ")


def live(at: float, **changes) -> dict:
    """A live reading as Ecowitt's real_time gives it ({group: {field: {time, unit, value}}}): the station's own on a wet night (what
    tests/test_report.py's NOW was written from), with `changes` ("outdoor.temperature": "36.0", or None to leave a reading out)."""
    t = str(int(at))
    data = {"outdoor": {"temperature": ("12.3", "℃"), "feels_like": ("12.3", "℃"), "app_temp": ("12.7", "℃"), "dew_point": ("11.3", "℃"),
                        "vpd": ("0.085", "kPa"), "humidity": ("94", "%")},
            "indoor": {"temperature": ("18.3", "℃"), "humidity": ("63", "%"), "dew_point": ("11.1", "℃"), "feels_like": ("18.3", "℃"),
                       "app_tempin": ("18.7", "℃")},
            "solar_and_uvi": {"solar": ("0.0", "W/m²"), "uvi": ("0", "")},
            "rainfall": {"rain_rate": ("1.8", "mm/hr"), "daily": ("6.3", "mm"), "event": ("49.8", "mm"), "1_hour": ("1.3", "mm"),
                         "weekly": ("49.8", "mm"), "monthly": ("49.8", "mm"), "yearly": ("5105.7", "mm")},
            "wind": {"wind_speed": ("0.0", "km/h"), "wind_gust": ("0.0", "km/h"), "wind_direction": ("131", "º")},
            "pressure": {"relative": ("1025.0", "hPa"), "absolute": ("1009.5", "hPa")}}
    out = {g: {f: {"time": t, "unit": u, "value": v} for f, (v, u) in fields.items()} for g, fields in data.items()}
    for name, value in changes.items():
        group, field = name.split(".")
        if value is None:
            out[group].pop(field, None)
        else:
            out[group][field] = {**out[group].get(field, {"time": t, "unit": ""}), "value": value}
    return out


DAY = {"solar_and_uvi.solar": "612.5", "solar_and_uvi.uvi": "7", "wind.wind_speed": "14.0", "wind.wind_gust": "31.0"}   # sunny and breezy
HOT = {"outdoor.temperature": "36.0", "outdoor.humidity": "90", "indoor.temperature": "29.0", "indoor.humidity": "20",
       "pressure.relative": "1030.0", "rainfall.rain_rate": "2.0", "rainfall.daily": "3.0", "rainfall.monthly": "9.0",
       "solar_and_uvi.solar": "800.0", "solar_and_uvi.uvi": "10", "wind.wind_speed": "55.0", "wind.wind_gust": "70.0",
       "wind.wind_direction": "90"}   # every emoji there is (tests/test_report.py)
DRY = {"rainfall.rain_rate": "0.0", "rainfall.daily": "0.0", "rainfall.monthly": "12.0", "wind.wind_speed": "12.0",
       "wind.wind_gust": "20.0", "wind.wind_direction": "270"}


class Station:
    """The archived fake station, its live reading what the test says (a request counted, and whether it was urgent)."""

    def __init__(self, eco, data: dict | None):
        self.eco, self.data, self.asked = eco, data, []
        eco.api.realtime = self.realtime

    async def realtime(self, mac, groups, urgent=False):
        self.asked.append(urgent)
        if isinstance(self.data, Exception):
            raise self.data
        return json.loads(json.dumps(self.data))

    def reads(self, data):
        """A new live reading, asked for afresh (the kept one forgotten)."""
        self.data, self.eco._live = data, None


async def world(tmp_path, archived_cache, data=None, *, air=True, station=True, catalogue=None):
    """(the tool, the Station, the AirGradient or None): the archived station reading `data` now (default: the wet night), the fake air
    sensor's cloud beside it in the same file. catalogue "file": the readings the file holds (Catalogue.open); None: every reading."""
    eco, _ = await archived_station(tmp_path, archived_cache)
    stand_in = Station(eco, live(time.time()) if data is None else data)
    sensor = None
    if air:
        transport, _ = air_transport()
        sensor = AirGradient(config(tmp_path), transport=transport, storage=eco.storage, owned=False)
        await sensor.start()
    ctx = Context(eco=eco if station else None, air=sensor)
    if catalogue == "file":
        ctx.catalogue = await Catalogue.open(ctx.eco, ctx.air)
    tool = CurrentTool(ctx)
    tool.opened = [s for s in (sensor, eco) if s is not None]   # (what close() closes: the station too, when the tool isn't given it)
    return tool, stand_in, sensor


async def close(tool):
    for source in tool.opened:
        await source.close()


async def call(tool, meta=None, **args) -> dict:
    return json.loads(await tool.tool().call(args, meta))


# ---------- the tool ----------
def test_the_description_says_what_it_answers_with_an_example_each_and_where_the_rest_goes():
    d = cur.DESCRIPTION
    assert len(d) < 1500
    for said in ("{}", "{readings: [\"temperature\"]}", "{readings: [\"rain\"]}", "{context: true}", "report, status or sitrep",
                 "forecast", "pollen", "will it rain (outlook)", "(summary)", "(readings)", "lines", "missing", "unavailable"):
        assert said in d, said
    schema = cur.parameters("Readings, as `sensor.reading`.")
    assert set(schema["properties"]) == {"readings", "context"} and "required" not in schema
    assert schema["properties"]["readings"]["description"].endswith("Default: every reading the station and the air sensor have now.")


async def test_it_is_offered_with_a_sensor_and_says_it_asks_the_devices(tmp_path, archived_cache):
    assert cur.tools(Context()) == []
    tool, _, _ = await world(tmp_path, archived_cache)
    (made,) = cur.tools(tool.ctx)
    assert made.name == "current" and made.annotations == {"readOnlyHint": True, "openWorldHint": True}
    assert "outdoor.temperature" in made.parameters["properties"]["readings"]["description"]
    await close(tool)


# ---------- the readings now ----------
async def test_every_reading_now_with_its_value_unit_words_and_emoji(tmp_path, archived_cache):
    tool, station, _ = await world(tmp_path, archived_cache)
    got = await call(tool)
    assert "period" not in got and got["fresh"] == FRESH and "charts" not in got and "missing" not in got
    s = got["sensors"]
    assert s["outdoor"]["temperature"] == {"value": 12.3, "unit": "°C", "text": "12.3 °C", "emoji": "🧥"}
    assert s["outdoor"]["humidity"] == {"value": 94, "unit": "%", "text": "94 %", "emoji": "💦"}
    assert s["indoor"]["app_temp"]["text"] == "18.7 °C"                                   # (the station calls it app_tempin)
    assert s["outdoor"]["vpd"] == {"value": 0.085, "unit": "kPa", "text": "0.085 kPa"}       # nothing to notice: no emoji
    assert s["rainfall"]["rain"] == {"value": 6.3, "unit": "mm", "text": "6.3 mm", "emoji": "☔"}   # today's rain so far
    assert s["rainfall"]["rain_rate"]["text"] == "1.8 mm/h" and s["rainfall"]["rain_rate"]["emoji"] == "🌧️"
    assert s["wind"]["wind_direction"] == {"value": 131, "unit": "°", "text": "131°", "compass": "SE"}
    assert s["solar_and_uvi"]["uvi"] == {"value": 0, "unit": "", "text": "0"}
    assert s["pressure"]["absolute"]["text"] == "1009.5 hPa"
    assert "rainfall_piezo" not in s                                     # every reading the sensors have now: this station has no piezo gauge
    pm = s["air"]["pm2_5"]
    assert pm["value"] == 12.0 and pm["unit"] == "µg/m³" and pm["text"] == "12.0 µg/m³" and 51 <= pm["aqi_us"] <= 100 and pm["band"] == "moderate"
    assert pm["rating"].endswith(("good", "poor"))
    assert s["air"]["co2"]["text"] == "450 ppm" and s["air"]["nox_index"]["text"] == "1.0" and s["air"]["voc_index"]["text"] == "100"
    assert s["air"]["temperature"]["unit"] == "°C" and "rating" not in s["air"]["temperature"]   # the sensor's own: no rating
    newest = datetime.fromisoformat(got["as_of"]["time"])
    assert abs(newest.timestamp() - time.time()) < 60 and got["as_of"]["text"].endswith(clock_text(newest))
    assert station.asked == [True]                                     # someone is waiting: the station's request goes first
    await close(tool)


async def test_named_readings_alone_each_once_in_the_order_named(tmp_path, archived_cache):
    tool, _, _ = await world(tmp_path, archived_cache)
    got = await call(tool, readings=["rain", "temperature", "pm2.5", "gust"])
    assert [(g, list(r)) for g, r in got["sensors"].items()] == [("rainfall", ["rain"]), ("outdoor", ["temperature"]),
                                                                 ("indoor", ["temperature"]), ("wind", ["wind_gust"]), ("air", ["pm2_5"])]
    assert set(got["lines"]) == {STATION, AIR_DEVICE} and got["lines"][AIR_DEVICE] == ["• PM2.5: " + got["sensors"]["air"]["pm2_5"]["text"]
                                                                                      + f" {got['sensors']['air']['pm2_5']['rating']} (AQI {got['sensors']['air']['pm2_5']['aqi_us']})"]
    assert (await call(tool, readings="indoor temperature, outdoor humidity"))["sensors"] == {
        "indoor": {"temperature": {"value": 18.3, "unit": "°C", "text": "18.3 °C", "emoji": "🧥"}},
        "outdoor": {"humidity": {"value": 94, "unit": "%", "text": "94 %", "emoji": "💦"}}}   # (a string of names will do)
    only_air = await call(tool, readings=["air"])
    assert list(only_air["sensors"]) == ["air"] and list(only_air["sensors"]["air"]) == ["pm2_5", "pm10", "pm1", "co2", "voc_index",
                                                                                         "nox_index"]
    assert set(only_air["lines"]) == {AIR_DEVICE}
    await close(tool)


async def test_a_reading_named_that_isnt_being_read_now_is_missing(tmp_path, archived_cache):
    tool, station, _ = await world(tmp_path, archived_cache, live(time.time(), **{"indoor.temperature": None}))
    got = await call(tool, readings=["indoor temperature", "pressure", "piezo rain"])
    assert got["missing"] == ["indoor.temperature", "rainfall_piezo.rain"] and list(got["sensors"]) == ["pressure"]
    with_context = await call(tool, readings=["indoor temperature"], context=True)   # a gauge with no reading now: its figures, dimmed
    entry = with_context["sensors"]["indoor"]["temperature"]
    assert entry["value"] is None and entry["unit"] == "°C" and entry["year"]["high"] > entry["year"]["low"]
    assert with_context["missing"] == ["indoor.temperature"]
    await close(tool)


async def test_the_readings_listed_are_those_the_file_holds(tmp_path, archived_cache):
    tool, _, _ = await world(tmp_path, archived_cache, catalogue="file")   # the fake station's history has no indoor humidity
    got = await call(tool)
    assert list(got["sensors"]["indoor"]) == ["temperature"] and "• Indoor: 🧥 18.3 °C, 63 %" in got["lines"][STATION]   # (the report's words: as today)
    await close(tool)


# ---------- the words: the bot's, unchanged ----------
@pytest.mark.parametrize("changes", [{}, DAY, HOT, DRY], ids=["wet-night", "sunny", "every-emoji", "dry"])
async def test_the_lines_are_the_reports_and_weather_nows_words(tmp_path, archived_cache, changes):
    tool, _, air = await world(tmp_path, archived_cache, live(time.time(), **changes))
    got = await call(tool)
    eco, today = tool.ctx.eco, tool.ctx.now().date()
    weather_now, air_quality = await eco._realtime({"groups": NOW_GROUPS}), await air.handle({})   # today's tools, on the same readings
    written = "\n\n".join(f"{got['headings'][d]}\n" + "\n".join(got["lines"][d]) for d in (STATION, AIR_DEVICE))
    assert written == report.report({"weather_now": weather_now, "air_quality": air_quality}, today)
    assert "\n".join(got["lines"][STATION]) == report.weather_now(weather_now)
    assert got["lines"][AIR_DEVICE] == report.air_lines(json.loads(air_quality))
    await close(tool)


# (the bot's plain lookup: the readings the words name, and the sides) -> what a model sends for the same question
LOOKUPS = [(["temperature"], ["outdoor", "indoor"], ["temperature"]),            # "how hot is it"
           (["temperature"], ["indoor"], ["indoor temperature"]),                 # "indoor temperature"
           (["humidity"], ["outdoor"], ["outdoor humidity"]),
           (["rain"], ["outdoor", "indoor"], ["rain"]),                           # "is it raining"
           (["wind"], ["outdoor", "indoor"], ["wind"]),
           (["uv"], ["outdoor", "indoor"], ["uv"]),
           (["pressure"], ["outdoor", "indoor"], ["pressure"]),
           (["solar"], ["outdoor", "indoor"], ["solar"]),
           (["dew_point"], ["outdoor", "indoor"], ["dew point"]),
           (["dew_point"], ["indoor"], ["indoor dew point"]),
           (["feels_like"], ["outdoor", "indoor"], ["feels like"]),
           (["temperature", "rain"], ["outdoor", "indoor"], ["temperature", "rain"])]


@pytest.mark.parametrize("changes", [{}, DAY, DRY], ids=["wet-night", "sunny", "dry"])
async def test_named_readings_are_worded_as_the_bots_plain_lookups(tmp_path, archived_cache, changes):
    tool, _, air = await world(tmp_path, archived_cache, live(time.time(), **changes))
    eco, today = tool.ctx.eco, tool.ctx.now().date()
    weather_now = await eco._realtime({"groups": NOW_GROUPS})
    for names, sides, asked in LOOKUPS:
        got = await call(tool, readings=asked)
        assert "\n".join(got["lines"][STATION]) == report.lookup("reading", weather_now, names, sides, today), asked
    air_quality = await air.handle({})
    for names, asked in ((["pm10"], ["pm10"]), (["co2", "pm2_5"], ["co2", "pm2.5"]), ([], ["air"]), ([], ["airgradient"])):
        got = await call(tool, readings=asked)
        assert "\n".join(got["lines"][AIR_DEVICE]) == report.lookup("air", air_quality, names, [], today), asked
    await close(tool)


async def test_readings_the_bot_never_worded_have_a_line_of_their_own(tmp_path, archived_cache):
    tool, _, _ = await world(tmp_path, archived_cache, live(time.time(), **DAY))
    got = await call(tool, readings=["gust", "wind direction", "rain rate", "absolute pressure", "airgradient temperature"])
    assert got["lines"][STATION] == ["• Wind gust: 🌬️ 31.0 km/h", "• Wind direction: 131°", "• Rain rate: 🌧️ 1.8 mm/h",
                                     "• Absolute pressure: 1009.5 hPa"]
    assert got["lines"][AIR_DEVICE] == [f"• AirGradient temperature: {got['sensors']['air']['temperature']['text']}"]
    wind = await call(tool, readings=["wind", "gust"])          # the gusts are in the wind's line already
    assert wind["lines"][STATION] == ["• Wind: 14.0 km/h from 131°, gust 🌬️ 31.0 km/h"]
    await close(tool)


def test_the_heading_has_the_time_of_the_reading_with_its_day_when_that_isnt_today():
    today = date(2026, 11, 10)
    assert heading(STATION, datetime(2026, 11, 10, 2, 21, tzinfo=TZ), today) == "Weather station (2:21am)"
    assert heading(AIR_DEVICE, datetime(2026, 11, 9, 23, 45, tzinfo=TZ), today) == "Air quality (Mon 9 Nov 11:45pm)"
    assert heading(STATION, None, today) == "Weather station"
    old = json.dumps({"time": "Mon 9 Nov 2026 11:45pm", "pm2_5": {"value": 1.0, "unit": "µg/m³"}})
    assert report.report({"air_quality": old}, today).splitlines()[0] == "Air quality (Mon 9 Nov 11:45pm)"   # (as the report writes it)


# ---------- context: the gauges' figures ----------
async def gauge_rows(eco, data: dict) -> dict:
    """{title: {row: figures}}: each gauge's rows as the station's own dashboard drew them before step 2 (dashboard.figures over the
    year's day rows, the live reading counted in; today's rain the gauge's own reading)."""
    from lib.ecowitt.dashboard import GAUGES, PERIODS, figures, live_value, periods_of
    days = periods_of(eco.now().date())
    names = sorted({n for g in GAUGES for n in (g.highest, g.lowest) if n})
    values, _ = await eco.storage.read(day_values_on, eco.mac, names, days["Yearly"])
    out = {}
    for g in GAUGES:
        now = live_value(data, g.live)
        out[g.title] = {row: figures(g, values, days[row], None if g.total else now) for row in PERIODS}
        if g.total and now is not None:
            out[g.title]["Daily"] = (None, now)
    return out


async def test_context_is_the_gauges_figures_with_when_each_happened(tmp_path, archived_cache):
    data = live(time.time(), **DAY)
    tool, _, _ = await world(tmp_path, archived_cache, data)
    got = await call(tool, context=True)
    tiles = await gauge_rows(tool.ctx.eco, data)
    s = got["sensors"]
    for title, (sensor, reading) in (("Temperature", ("outdoor", "temperature")), ("Humidity", ("outdoor", "humidity")),
                                     ("Atmospheric pressure", ("pressure", "relative")), ("Dew point", ("outdoor", "dew_point")),
                                     ("Indoor temperature", ("indoor", "temperature"))):
        entry = s[sensor][reading]
        for row, key in cur.PERIODS:
            assert (entry[key]["high"], entry[key]["low"]) == tiles[title][row], (title, row)
            assert WHEN.match(entry[key]["high_when"]) and WHEN.match(entry[key]["low_when"]), entry[key]
            assert ("high_date" in entry[key]) is (key != "today")
    assert s["outdoor"]["temperature"]["year"]["high_text"] == f"{s['outdoor']['temperature']['year']['high']:.1f} °C"
    assert s["outdoor"]["humidity"]["year"]["low_text"] == f"{s['outdoor']['humidity']['year']['low']:.0f} %"
    wind = s["wind"]["wind_speed"]
    assert [wind[k]["high_gust"] for _, k in cur.PERIODS] == [tiles["Wind"][row][0] for row, _ in cur.PERIODS]
    assert s["wind"]["wind_gust"]["year"]["high"] == wind["year"]["high_gust"]           # the gusts' own: the same figure
    rain = s["rainfall"]["rain"]
    assert rain["today"] == {"total": 6.3, "total_text": "6.3 mm"}                       # the gauge's own reading
    for row, key in cur.PERIODS[1:]:
        wettest, total = tiles["Rain"][row]
        assert rain[key]["total"] == total and rain[key]["wettest_day"]["rain"] == wettest
    assert s["solar_and_uvi"]["uvi"]["year"]["high_text"] == "11"                         # written as the station writes it
    assert "today" not in s["outdoor"]["vpd"] and "today" not in s["air"]["pm2_5"]       # no gauge, no context
    await close(tool)


async def test_with_context_every_gauge_the_station_sends_is_there_though_its_file_hasnt_recorded_it_yet(tmp_path, archived_cache):
    """The catalogue is what the file has recorded; with context, a gauge's reading the station sends now but the file doesn't hold yet
    (the archived fake has no feels like, rain rate nor indoor humidity) is there too, with its figures (the reading now alone), as the
    gauges show every reading the station sends. Without context, or with readings named, only the catalogue's."""
    tool, _, _ = await world(tmp_path, archived_cache, catalogue="file")
    held = tool.ctx.readings()
    assert not {"outdoor.feels_like", "rainfall.rain_rate", "indoor.humidity"} & set(held.names)
    s = (await call(tool, context=True))["sensors"]
    feels, rate, inside = s["outdoor"]["feels_like"], s["rainfall"]["rain_rate"], s["indoor"]["humidity"]
    assert (feels["value"], feels["text"]) == (12.3, "12.3 °C") and rate["value"] == 1.8 and inside["value"] == 63
    assert feels["today"]["high"] == feels["today"]["low"] == 12.3 and rate["year"]["high"] == 1.8
    assert "app_temp" not in s["outdoor"] and "vpd" not in s["outdoor"]             # (no gauge: only the catalogue's)
    assert "feels_like" not in (await call(tool))["sensors"]["outdoor"]
    named = (await call(tool, readings=["outdoor temperature"], context=True))["sensors"]
    assert list(named) == ["outdoor"] and list(named["outdoor"]) == ["temperature"]   # named: just those
    await close(tool)


async def test_a_high_or_low_is_the_day_rows_with_its_day_or_the_live_reading_when_it_beats_them(tmp_path, archived_cache):
    tool, station, _ = await world(tmp_path, archived_cache)
    got = await call(tool, readings=["outdoor temperature"], context=True)
    year = got["sensors"]["outdoor"]["temperature"]["year"]
    on = datetime.strptime(year["high_date"], "%a %d %b %Y").date()
    eco = tool.ctx.eco
    values, _ = await eco.storage.read(day_values_on, eco.mac, ["temp_max"], [on])
    assert values["temp_max"][on] == year["high"]                                       # the day whose row has it
    at = time.time()
    station.reads(live(at, **{"outdoor.temperature": "45.0"}))                          # hotter than any day held
    hot = (await call(tool, readings=["outdoor temperature"], context=True))["sensors"]["outdoor"]["temperature"]
    clock = f"at {clock_text(to_local(int(at), TZ), whole_hours=True)}"
    for key in ("today", "month", "year"):
        assert hot[key]["high"] == 45.0 and hot[key]["high_when"] == clock and hot[key]["high_text"] == "45.0 °C"
    assert hot["month"]["high_date"] == f"{to_local(int(at), TZ):%a} {to_local(int(at), TZ).day} {to_local(int(at), TZ):%b %Y}"
    await close(tool)


# ---------- refusals that teach, and devices that don't answer ----------
async def test_a_wrong_call_is_refused_with_what_fixes_it(tmp_path, archived_cache):
    tool, _, _ = await world(tmp_path, archived_cache)
    then = await call(tool, period="yesterday")
    assert "summary" in then["error"] and "readings" in then["error"] and then["arguments"] == ["readings", "context"]
    other = await call(tool, groups="outdoor")
    assert other["error"] == "'groups' is not an argument of current." and other["arguments"] == ["readings", "context"]
    unknown = await call(tool, readings=["lightning"])
    assert "isn't a reading" in unknown["error"] and "outdoor.temperature" in unknown["names"] and "rain" in unknown["words"]
    assert "true or false" in (await call(tool, context="maybe"))["error"]
    assert (await call(tool, context="true"))["sensors"]["outdoor"]["temperature"]["today"]   # (true written as a word will do)
    await close(tool)

    (tmp_path / "station").mkdir()
    tool, _, _ = await world(tmp_path / "station", archived_cache, air=False)
    no_air = await call(tool, readings=["pm2.5"])
    assert no_air["error"] == "air.pm2_5 is the air sensor's, and this service has no air sensor." and "air.pm2_5" not in no_air["names"]
    assert "air" not in (await call(tool))["sensors"]                                     # unnamed: just the sensors there are
    await close(tool)


async def test_a_device_that_doesnt_answer_is_said_so_and_the_other_still_answers(tmp_path, archived_cache):
    tool, station, air = await world(tmp_path, archived_cache, RuntimeError("Ecowitt: System is busy."))
    got = await call(tool)
    assert got["unavailable"] == {STATION: "The weather station isn't answering right now."}
    assert got["lines"][STATION] == ["• not available right now"] and got["headings"][STATION] == "Weather station"
    assert list(got["sensors"]) == ["air"] and got["as_of"]
    written = "\n\n".join(f"{got['headings'][d]}\n" + "\n".join(got["lines"][d]) for d in (STATION, AIR_DEVICE))
    assert written == report.report({"weather_now": "Error calling tool: busy", "air_quality": await air.handle({})},
                                    tool.ctx.now().date())                                 # the report's words for it

    async def down(extras=()):
        raise RuntimeError("AirGradient current failed: HTTP 500")
    air.latest = down
    station.reads(live(time.time()))
    got = await call(tool)
    assert got["unavailable"] == {AIR_DEVICE: "The air sensor isn't answering right now."} and "air" not in got["sensors"]
    assert f"{got['headings'][AIR_DEVICE]}\n{got['lines'][AIR_DEVICE][0]}" == "Air quality\n• not available right now"
    neither = RuntimeError("down")
    station.reads(neither)
    nothing = await call(tool)
    assert set(nothing["unavailable"]) == {STATION, AIR_DEVICE} and nothing["sensors"] == {} and "as_of" not in nothing
    await close(tool)


# ---------- the request's metadata ----------
async def test_without_fetching_it_answers_from_the_reading_held(tmp_path, archived_cache):
    tool, station, _ = await world(tmp_path, archived_cache)
    nothing = await call(tool, {"fetch": False}, readings=["temperature"])
    assert nothing["unavailable"] == {STATION: "No reading of the weather station is held (and `fetch` is off)."} and station.asked == []
    await call(tool, readings=["temperature"])
    station.data = live(time.time(), **{"outdoor.temperature": "20.0"})
    tool.ctx.eco._live = (time.time() - 3600, tool.ctx.eco._live[1])                    # an hour old: a question would ask again
    held = await call(tool, {"fetch": False}, readings=["outdoor temperature"])
    assert held["sensors"]["outdoor"]["temperature"]["value"] == 12.3 and station.asked == [True]
    assert (await call(tool, readings=["outdoor temperature"]))["sensors"]["outdoor"]["temperature"]["value"] == 20.0
    await close(tool)


async def test_the_kept_reading_answers_while_fresh_and_a_group_it_lacks_is_asked_for_when_named(tmp_path, archived_cache):
    tool, station, _ = await world(tmp_path, archived_cache, air=False, catalogue="file")
    assert "rainfall_piezo.rain" in tool.ctx.readings()         # (a group that never sent anything is offered whole)
    await call(tool)
    await call(tool, context=True)
    await call(tool, readings=["temperature", "rain"])
    assert station.asked == [True]                               # one request: the reading the station keeps answers the rest
    await call(tool, readings=["piezo rain"])
    assert station.asked == [True, True]                         # a group "weather now" doesn't show: asked for when named
    await close(tool)


async def test_a_background_request_asks_the_station_behind_those_someone_waits_for(tmp_path, archived_cache):
    tool, station, _ = await world(tmp_path, archived_cache, air=False)
    await call(tool, {"priority": "background"})
    station.reads(live(time.time()))
    await call(tool)
    assert station.asked == [False, True]
    await close(tool)


async def test_the_air_sensor_alone_and_its_own_temperature(tmp_path, archived_cache):
    tool, station, _ = await world(tmp_path, archived_cache, station=False)
    got = await call(tool, readings=["airgradient temperature", "airgradient humidity"])
    t, h = got["sensors"]["air"]["temperature"], got["sensors"]["air"]["humidity"]
    assert t["unit"] == "°C" and t["text"] == f"{t['value']:.1f} °C" and h["text"] == f"{h['value']:.0f} %"
    assert got["lines"] == {AIR_DEVICE: [f"• AirGradient temperature: {t['text']}", f"• AirGradient humidity: {h['text']}"]}
    everything = await call(tool)
    assert list(everything["sensors"]) == ["air"] and list(everything["lines"]) == [AIR_DEVICE] and station.asked == []
    assert everything["headings"][AIR_DEVICE].startswith("Air quality (")
    await close(tool)


def test_the_module_says_what_it_offers():
    assert all(hasattr(cur, name) for name in cur.__all__)
