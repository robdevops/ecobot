"""What the service's tools stand on (lib/service): the request's metadata, answers and refusals, periods read from words, the chart
budget, the reading names and the words for them, and a day's look."""

import json
from datetime import date, datetime

import pytest

from lib import intent
from lib.periods_text import PeriodError
from lib.prompt import PROMPT
from lib.service import common, daylook, names
from lib.service.common import (CHART_NOTE, FRESH, Refusal, ServiceTool, answer, charts_field, period_properties, read_period, refuse,
                                refuse_period, request_meta, with_charts)
from lib.service.daylook import LOOKS, clear_sky_mean, day_look
from lib.service.names import CATALOGUE, Catalogue, UnknownReading, resolve
from lib.specs import Chart, Line, Panel, from_json
from lib.storage import Storage
from lib.storage.catalogue import CLOUD
from lib.tools import Tools, Turn
from tests.fakes import TZ

NOW = datetime(2026, 11, 10, 14, 5, tzinfo=TZ)   # Tue 10 Nov 2026 2:05pm, as the plan's periods table is asked
MELBOURNE = -37.8


def got(text: str) -> dict:
    return json.loads(text)


def named(*words, default="temperature", catalogue: Catalogue | None = None) -> list[str]:
    return [r.name for r in resolve(list(words), default=default, catalogue=catalogue)]


# ---------- the request's metadata ----------
def test_the_metadata_a_handler_reads_has_its_defaults_and_is_checked():
    assert request_meta() == request_meta({}) == {"chart_points": 150, "fetch": True, "priority": "normal"}
    assert request_meta({"chart_points": 500})["chart_points"] == 500 and request_meta({"chart_points": 10000})["chart_points"] == 500
    assert request_meta({"chart_points": 1})["chart_points"] == 2 and request_meta({"chart_points": "300"})["chart_points"] == 300
    assert request_meta({"chart_points": "lots"})["chart_points"] == request_meta({"chart_points": True})["chart_points"] == 150
    assert request_meta({"fetch": False})["fetch"] is False and request_meta({"fetch": "false"})["fetch"] is False
    assert request_meta({"fetch": None})["fetch"] is True and request_meta({"fetch": True})["fetch"] is True
    assert request_meta({"priority": "Background"})["priority"] == "background" and request_meta({"priority": "soon"})["priority"] == "normal"
    assert request_meta({"progressToken": 7})["progressToken"] == 7   # what it doesn't read is passed on as it came


def test_a_turn_carries_its_own_metadata():
    a, b = Turn(), Turn()
    a.meta["fetch"] = False
    assert b.meta == {}


async def test_a_service_tool_is_sent_its_arguments_and_the_requests_metadata_and_nothing_else():
    seen = []

    async def handler(args, meta):
        seen.append((args, meta))
        return answer(period=None, as_of=None, ok=True)
    tool = ServiceTool("probe", "A probe.", {"type": "object", "properties": {}}, handler, open_world=True)
    assert tool.annotations == {"readOnlyHint": True, "openWorldHint": True}
    tools = Tools([tool.tool()])
    assert got(await tools.call("probe", '{"x": 1}', Turn(text="plot it", chart_asked=True, meta={"chart_points": 500}))) == {"ok": True}
    assert seen[-1] == ({"x": 1}, {"chart_points": 500, "fetch": True, "priority": "normal"})   # the Turn's words never reach it
    await tools.call("probe", "{}", Turn(ahead=True))                       # a chart drawn ahead is a background request
    assert seen[-1][1]["priority"] == "background"
    await tools.call("probe", "{}", Turn(ahead=True, meta={"priority": "normal"}))   # (the request's own metadata wins)
    assert seen[-1][1]["priority"] == "normal"
    await tools.call("probe", "{}")
    assert seen[-1] == ({}, {"chart_points": 150, "fetch": True, "priority": "normal"})
    assert tools.schemas[0]["function"]["parameters"] == {"type": "object", "properties": {}}   # (no `source` argument: charts name theirs)


async def test_a_refusal_raised_in_a_handler_is_the_tools_answer_and_a_plain_function_will_do():
    def handler(args, meta):
        if "reading" not in args:
            raise Refusal("Which reading?", names=["outdoor.temperature"])
        return refuse("not yet")
    tool = ServiceTool("probe", "A probe.", {"type": "object", "properties": {}}, handler)
    assert got(await tool.call({})) == {"error": "Which reading?", "names": ["outdoor.temperature"]}
    assert got(await tool.call({"reading": "x"})) == {"error": "not yet"}
    assert "error" in got(await tool.call(["not", "an", "object"]))
    assert tool.annotations == {"readOnlyHint": True, "openWorldHint": False}


# ---------- answers and refusals ----------
def test_an_answer_starts_with_its_period_and_the_time_of_its_figures():
    period = read_period({"period": "this week"}, tz=TZ, now=NOW, default="today")
    out = got(answer(period=period, as_of=NOW, series={"outdoor.temperature": {}}, missing=[]))
    assert list(out) == ["period", "as_of", "fresh", "series", "missing"]
    assert out["period"] == {"asked": "this week", "text": "the last 7 days: Wed 4 – Tue 10 Nov 2026",
                             "start": "2026-11-04T00:00:00+11:00", "end": "2026-11-10T14:05:00+11:00"}
    assert out["as_of"] == {"text": "Tue 10 Nov 2026 2:05pm", "time": "2026-11-10T14:05:00+11:00"}
    assert out["fresh"] == FRESH and "as_of" in FRESH and "called again" in FRESH
    cut = got(answer(period=read_period({"period": "this week"}, tz=TZ, now=NOW, default="on record", whole_days=True), as_of=NOW))
    assert cut["period"]["cut"] == "to Mon 9 Nov: today isn't over" and cut["period"]["end"] == "2026-11-10T00:00:00+11:00"
    assert got(answer(period=None, as_of=None, verdict="likely")) == {"verdict": "likely"}   # no period (outlook), no figures (about)
    assert "°" in answer(period=None, as_of=None, unit="°C") and " " not in answer(period=None, as_of=None, a=1, b=2)   # compact, unescaped
    finished = got(answer(period=None, as_of=NOW, series={}, **common.complete("Pressure, the last 7 days: …")))
    assert list(finished)[-2:] == ["reply", "complete"] and finished["complete"] is True   # a chart that is the whole answer (3.7)
    assert common.iso(int(NOW.timestamp()), TZ) == common.iso(NOW) == "2026-11-10T14:05:00+11:00"
    with pytest.raises(ValueError):
        answer(period=None, as_of=NOW, fresh="mine")
    with pytest.raises(ValueError):
        answer(period=None, as_of=datetime(2026, 11, 10, 14, 5))   # a time without its zone can't be written


def test_a_refusal_is_its_error_and_what_teaches_the_right_call():
    assert got(refuse("too many rows", fits="1hour", max_rows=300)) == {"error": "too many rows", "fits": "1hour", "max_rows": 300}
    assert list(got(refuse("x", a=1))) == ["error", "a"]
    with pytest.raises(ValueError):
        refuse("x", error="y")
    plain = got(refuse_period(PeriodError("Not now.")))
    assert plain == {"error": "Not now."}


# ---------- periods ----------
def test_a_period_is_read_from_the_words_or_dates_or_else_the_default():
    assert read_period({}, tz=TZ, now=NOW, default="last 24 hours").text == "the last 24 hours: Mon 9 Nov 2:05pm – Tue 10 Nov 2026 2:05pm"
    assert read_period({"period": "  "}, tz=TZ, now=NOW, default="today").asked == "today"
    assert read_period({"period": "in the last year"}, tz=TZ, now=NOW, default="today").text == \
        "the last 365 days: Tue 11 Nov 2025 – Tue 10 Nov 2026"
    dated = read_period({"start": "2026-11-01", "end": "2026-11-02"}, tz=TZ, now=NOW, default="today")
    assert dated.asked == "start/end" and (dated.start.day, dated.end.day) == (1, 3)
    first = read_period({"period": "on record"}, tz=TZ, now=NOW, default="today", first_held=date(2024, 3, 1))
    assert first.start.date() == date(2024, 3, 1)


def test_words_that_are_no_form_are_refused_with_every_form_and_what_it_means_now():
    with pytest.raises(Refusal) as e:
        read_period({"period": "the other week"}, tz=TZ, now=NOW, default="today")
    out = got(e.value.text)
    assert "'the other week' isn't a period" in out["error"] and out["error"].endswith("It is now Tue 10 Nov 2026 2:05pm.")
    assert out["forms"]["today"] == "today: Tue 10 Nov 2026"
    assert out["forms"]["this week, past week, the last week, week, 1w"] == "the last 7 days: Wed 4 – Tue 10 Nov 2026"
    assert "since 6am, since Monday" in out["forms"]
    with pytest.raises(Refusal) as e:
        read_period({"period": "next week"}, tz=TZ, now=NOW, default="today")
    assert "use forecast" in e.value.message and "forms" not in got(e.value.text)
    with pytest.raises(Refusal) as e:
        read_period({"period": "this week", "start": "2026-11-01"}, tz=TZ, now=NOW, default="today")
    assert "not both" in e.value.message


def test_a_whole_day_tool_refuses_what_leaves_no_whole_day_and_points_elsewhere():
    for asked in ({"period": "today"}, {"period": "last 5 hours"}, {"period": "this morning"}, {"start": "2026-11-09 06:00"}):
        with pytest.raises(Refusal) as e:
            read_period(asked, tz=TZ, now=NOW, default="on record", whole_days=True, first_held=date(2024, 1, 1))
        assert "use summary or readings" in e.value.message, asked
    with pytest.raises(Refusal) as e:
        read_period({"period": "fortnightly"}, tz=TZ, now=NOW, default="on record", whole_days=True)
    assert "today" not in got(e.value.text)["forms"] and "yesterday" in got(e.value.text)["forms"]   # only the forms it takes
    week = read_period({"period": "last 7 days"}, tz=TZ, now=NOW, default="on record", whole_days=True)
    assert week.text == "the last 7 days: Tue 3 – Mon 9 Nov 2026" and week.cut == "to Mon 9 Nov: today isn't over"


def test_the_period_properties_tell_a_model_the_forms_the_dates_and_the_default():
    props = period_properties("last 24 hours")
    assert list(props) == ["period", "start", "end"] and all(p["type"] == "string" for p in props.values())
    words = props["period"]["description"]
    for form in ("today", "yesterday", "last N hours/days/weeks/months/years", "this week", "last week", "week to date", "this month",
                 "last month", "this year", "last year", "a month or a year", "5 Sep", "a weekday", "this morning",
                 "yesterday afternoon", "last night", "since 6am", "Christmas Day", "on record", "A to B"):
        assert form in words, form
    assert "own words" in words and "unchanged" in words and words.endswith("Default: last 24 hours.")
    assert "'YYYY-MM-DD HH:MM'" in props["start"]["description"] and "never with it" in props["start"]["description"]
    assert "Default: now." in props["end"]["description"]
    whole = period_properties("on record", whole_days=True)
    assert "yesterday" in whole["period"]["description"] and whole["period"]["description"].endswith("Default: on record.")
    assert not any(form in whole["period"]["description"] for form in ("today,", "hours", "this morning", "since 6am"))
    assert "HH:MM" not in whole["start"]["description"] and "Default: yesterday." in whole["end"]["description"]


# ---------- charts ----------
def a_chart(points: int) -> Chart:
    start = int(NOW.timestamp()) - points * 60
    x = [start + 60 * i for i in range(points)]
    return Chart("Temperature", "", [Panel("Temperature", "°C", [Line("Outdoor", x, [15 + (i % 50) / 10 for i in range(points)])],
                                           reading="temperature")])


def test_a_chart_is_sent_as_data_within_the_requests_budget():
    chart = a_chart(1200)
    lengths = lambda out: [len(s["points"]) for c in out for p in c["panels"] for s in p["series"]]
    assert max(lengths(charts_field([chart], TZ))) <= 150
    assert 150 < max(lengths(charts_field([chart], TZ, {"chart_points": 500}))) <= 500
    assert max(lengths(charts_field([chart], TZ, {"chart_points": 5000}))) <= 500   # (at most today's budget)
    assert lengths(charts_field([a_chart(40)], TZ)) == [40]                        # a short line as it is
    sent = charts_field([chart], TZ)[0]
    assert sent["panels"][0]["series"][0]["records"]["high"][1] == 19.9            # the true extremes stay
    assert sent["panels"][0]["series"][0]["points"][0][0].endswith("+11:00")      # local times with their offset
    from_json(sent)                                                               # and it reads back as a chart
    assert with_charts([], TZ) == {}
    fields = with_charts([chart], TZ)
    assert list(fields) == ["chart_note", "charts"] and fields["chart_note"] == CHART_NOTE and len(fields["charts"]) == 1
    assert "draw it" in CHART_NOTE and "describe" in CHART_NOTE


# ---------- reading names ----------
def test_every_word_stands_for_the_readings_it_lists():
    for word, targets in names.ALIASES.items():
        expected = [n for n in (sum((names.BARE[r] for r in names.in_all()), ()) if targets == names.ALL else targets)]
        assert named(word) == expected, word


def test_the_words_people_use():
    assert named("pressure") == named("barometer") == ["pressure.relative"]
    assert named("uv") == named("UV index") == ["solar_and_uvi.uvi"] and named("solar") == ["solar_and_uvi.solar"]
    assert named("rain") == ["rainfall.rain"] and named("rain rate") == named("rain_rate") == ["rainfall.rain_rate"]
    assert named("gust") == named("gusts") == named("Wind Gusts") == ["wind.wind_gust"]
    assert named("dew point") == named("dew_point") == named("dewpoint") == ["outdoor.dew_point"]
    assert named("feels like") == named("feels-like") == ["outdoor.feels_like"] and named("vpd") == ["outdoor.vpd"]
    for word in ("pm2.5", "pm25", "PM 2.5", "pm2_5", "air.pm2.5", "ag pm2.5"):
        assert named(word) == ["air.pm2_5"], word
    assert named("pm10") == ["air.pm10"] and named("pm1") == ["air.pm1"] and named("PM1.0") == ["air.pm1"]
    assert named("co2") == named("CO₂") == ["air.co2"] and named("voc") == ["air.voc_index"] and named("nox") == ["air.nox_index"]
    assert named("outdoor humidity") == named("outside humidity") == named("outdoor.humidity") == ["outdoor.humidity"]
    assert named("indoor temp") == named("inside temperature") == ["indoor.temperature"]
    assert named("rainfall.daily") == ["rainfall.rain"] and named("air_humidity") == ["air.humidity"]   # today's names


def test_wind_is_its_speed_shaded_up_to_the_gusts_with_its_direction():
    speed, direction = resolve("wind", default="all")
    assert (speed.name, speed.band, speed.drawn) == ("wind.wind_speed", "wind.wind_gust", "line")
    assert (direction.name, direction.drawn, direction.combine) == ("wind.wind_direction", "rose", "last")
    assert CATALOGUE.get("wind.wind_gust").combine == "max" and CATALOGUE.get("wind.wind_gust").unit == "km/h"
    assert Catalogue.of({"wind": {"wind_speed": "km/h"}}, air=False).get("wind.wind_speed").band is None   # no gusts held: no band


def test_a_bare_temperature_or_humidity_is_the_stations_outdoor_and_indoor_never_the_air_sensors():
    assert named("temperature") == named("temp") == ["outdoor.temperature", "indoor.temperature"]
    assert named("humidity") == ["outdoor.humidity", "indoor.humidity"]
    assert named("air.temperature") == named("airgradient temperature") == named("ag temp") == named("air.temp") == ["air.temperature"]
    assert named("air gradient humidity") == ["air.humidity"]
    with pytest.raises(UnknownReading) as e:
        resolve("air temperature", default="all")
    assert "outdoor.temperature" in e.value.message and "air.temperature" in e.value.message


def test_a_sensors_name_is_its_usual_set():
    assert named("outdoor") == ["outdoor.temperature", "outdoor.humidity", "outdoor.dew_point", "outdoor.feels_like"]
    assert named("indoor") == named("inside") == ["indoor.temperature", "indoor.humidity"]
    assert named("wind") == ["wind.wind_speed", "wind.wind_direction"]
    assert named("rainfall") == ["rainfall.rain", "rainfall.rain_rate"]
    six = ["air.pm2_5", "air.pm10", "air.pm1", "air.co2", "air.voc_index", "air.nox_index"]
    assert named("air") == named("air quality") == six
    assert named("ag") == named("airgradient") == named("air gradient") == six   # the devices' nicknames
    assert named("ecowitt") == named("weather station") == named("all")


def test_all_is_every_station_reading_in_the_usual_chart_order_with_the_settings(monkeypatch):
    usual = ["outdoor.temperature", "indoor.temperature", "outdoor.humidity", "indoor.humidity", "solar_and_uvi.solar", "pressure.relative",
             "rainfall.rain", "outdoor.dew_point", "wind.wind_speed", "wind.wind_direction"]
    assert named("all") == usual and not any(n.startswith("air.") for n in named("all"))
    monkeypatch.setattr(names, "FEELS_LIKE_IN_ALL", True)
    monkeypatch.setattr(names, "VPD_IN_ALL", True)
    assert named("all") == [*usual[:8], "outdoor.feels_like", "outdoor.vpd", *usual[8:]]
    assert intent.chart_fields("weather all week") == ["temperature", "humidity", "solar", "pressure", "rain", "dew_point", "feels_like",
                                                       "vpd", "wind"]   # the bot's "weather all week" reads the same settings
    held = Catalogue.of({"outdoor": {"temperature": "℃"}, "indoor": {"temperature": "℃"}, "pressure": {"relative": "hPa"},
                         "wind": {"wind_speed": "km/h"}, "rainfall": {"daily": "mm"}, "rainfall_piezo": {"daily": "mm"},
                         "solar_and_uvi": {"uvi": ""}}, air=False)
    assert named("weather station", catalogue=held) == ["outdoor.temperature", "indoor.temperature", "pressure.relative", "rainfall.rain",
                                                        "wind.wind_speed"]   # what is held of it


def test_names_come_in_the_order_asked_each_once_and_none_is_the_default():
    assert named("rain, temperature and pressure") == ["rainfall.rain", "outdoor.temperature", "indoor.temperature", "pressure.relative"]
    assert named("outdoor.temperature", "temperature") == ["outdoor.temperature", "indoor.temperature"]
    assert named(default="outdoor") == [r.name for r in resolve(None, default="outdoor")] == named("outdoor")
    assert named() == named("temperature")
    assert [r.name for r in resolve("", default=["temperature", "humidity"])] == named("temperature", "humidity")


def test_an_unknown_name_is_refused_with_the_names_and_the_words():
    with pytest.raises(UnknownReading) as e:
        resolve(["temperature", "sunniness"], default="all")
    out = got(e.value.text)
    assert "'sunniness' isn't a reading" in out["error"]
    assert out["names"] == CATALOGUE.names and "air.temperature" in out["names"]
    assert out["words"]["temperature"] == ["outdoor.temperature", "indoor.temperature"] and out["words"]["pm2.5"] == ["air.pm2_5"]
    assert isinstance(e.value, Refusal)   # (so a tool answers with it)


def test_the_catalogue_is_what_the_file_holds():
    station = Catalogue.of({"outdoor": {"temperature": "℃", "humidity": "%"}, "rainfall": {"daily": "mm"}}, air=False)
    assert station.names[:3] == ["outdoor.temperature", "outdoor.humidity", "indoor.temperature"]   # (indoor sent nothing: all its kind)
    assert "outdoor.dew_point" not in station and "air.pm2_5" not in station
    assert [r.name for r in station.resolve("rainfall", default="all")] == ["rainfall.rain"]   # (no rain rate held)
    with pytest.raises(UnknownReading) as e:
        station.resolve("pm2.5", default="all")
    assert "doesn't hold" in e.value.message
    air = Catalogue.of(station=False)
    assert all(r.device == "air" for r in air)
    with pytest.raises(UnknownReading):
        air.resolve("humidity", default="all")   # a bare humidity is never the air sensor's
    assert "pm2.5" not in names.names_description(station) and "ecowitt" not in names.names_description(air)


async def test_the_catalogue_reads_the_stations_fields_from_the_file(tmp_path):
    from types import SimpleNamespace
    storage = Storage(tmp_path / "ecobot.sqlite", TZ)
    try:
        def write(w):
            sensor = w.ecowitt_sensor("M", "outdoor")
            w.fields(sensor, "5min", {"temperature": "℃", "humidity": "%"})
            w.put(sensor, "5min", [{"ts": int(NOW.timestamp()) // 300 * 300, "temperature": 15.0, "humidity": 50.0}], CLOUD)
        storage.writer.run(write, ())
        found = await Catalogue.open(SimpleNamespace(storage=storage, mac="M"), None)
        assert [n for n in found.names if n.startswith("outdoor.")] == ["outdoor.temperature", "outdoor.humidity"]
        assert not any(n.startswith("air.") for n in found.names)
    finally:
        storage.close()


def test_each_reading_says_what_it_is_and_how_it_is_read():
    t = CATALOGUE.get("outdoor.temperature")
    assert (t.label, t.unit, t.device, t.kind, t.column, t.combine, t.series, t.sensor, t.reading) == \
        ("Outdoor temperature", "°C", "station", "climate", "temperature", "mean", "temperature", "outdoor", "temperature")
    rain = CATALOGUE.get("rainfall.rain")
    assert (rain.label, rain.unit, rain.kind, rain.column, rain.combine, rain.drawn, rain.series) == \
        ("Rain", "mm", "rain", "daily", "rain", "bars", "rain")
    own = CATALOGUE.get("air.temperature")
    assert (own.label, own.unit, own.device, own.column, own.series) == ("AirGradient temperature", "°C", "air", "air_temperature",
                                                                         "air_temperature")
    assert CATALOGUE.get("air.voc_index").unit == "" and CATALOGUE.get("solar_and_uvi.uvi").label == "UV index"
    assert CATALOGUE.get("pressure.relative").metric.name == "pressure.relative"   # (what lib/history.py reads it with)
    assert set(names.BARE) == set(names.ALL_ORDER) and set(names.SENSOR_SETS) <= {*(r.sensor for r in CATALOGUE)}


def test_the_names_description_lists_the_names_the_words_and_the_rule_for_bare_names():
    text = names.names_description()
    assert all(name in text for name in CATALOGUE.names)
    for words in ("temperature and humidity are outdoor and indoor", "only ever air.temperature and air.humidity", "pressure is relative",
                  "rain is the main gauge", "pm2.5", "co2", "wind (its speed shaded up to the gusts", "usual set", "all is every",
                  "ecowitt", "weather station", "airgradient", "air gradient"):
        assert words in text, words


# ---------- a day's look ----------
SUMMER = date(2026, 12, 21)


def look(day=SUMMER, latitude=MELBOURNE, **figures) -> str | None:
    found = day_look({"rain": 0.0, "wind_gust": 20.0, "temp_max": 24.0, "temp_min": 12.0} | figures, latitude=latitude, day=day)
    return found and f"{found['word']} {found['emoji']}"


def test_a_days_look_takes_the_first_of_its_rules_that_holds():
    clear = clear_sky_mean(SUMMER, MELBOURNE)
    assert look(rain=12.0, wind_gust=65.0, solar_avg=clear) == "stormy ⛈️"
    assert look(rain=12.0, wind_gust=40.0) == look(rain=5.0) == "wet 🌧️"           # heavy rain with no strong gust is wet
    assert look(rain=1.0, solar_avg=clear) == look(rain=4.9, wind_gust=70.0) == "showery 🌦️"
    assert look(rain=0.9, wind_gust=50.0) == "windy 💨"                             # a trace is no rainy day
    assert look(temp_max=35.0, solar_avg=clear) == "very hot 🥵"
    assert look(day=date(2026, 6, 21), temp_max=9.5) == look(temp_min=0.0, solar_avg=clear) == "very cold 🥶"
    assert look(solar_avg=0.9 * clear) == "sunny ☀️" and look(solar_avg=0.7 * clear) == "mostly sunny 🌤️"
    assert look(solar_avg=0.4 * clear) == "partly cloudy ⛅" and look(solar_avg=0.2 * clear) == "overcast ☁️"
    winter = clear_sky_mean(date(2026, 6, 21), MELBOURNE)
    assert look(day=date(2026, 6, 21), temp_max=14.0, temp_min=4.0, solar_avg=0.9 * winter) == "sunny ☀️"   # set against its own date's sky


def test_a_day_with_no_solar_radiation_has_no_cloud_step():
    assert look() is None and look(latitude=None, solar_avg=300.0) is None
    assert look(rain=6.0) == "wet 🌧️"                                   # (the other steps still hold)
    assert day_look({}, latitude=MELBOURNE, day=SUMMER) is None
    assert day_look({"rain": None, "solar_avg": "350.0"}, latitude=MELBOURNE, day=SUMMER) == {"word": "sunny", "emoji": "☀️"}
    today = {"rain": 0.0, "temp_max": 24.0, "solar_avg": 350.0}
    assert day_look(today, latitude=MELBOURNE, day=SUMMER, so_far=True) is None   # a day not over: its solar mean is no whole day's
    assert day_look(today | {"temp_max": 36.0}, latitude=MELBOURNE, day=SUMMER, so_far=True)["word"] == "very hot"


def test_a_days_look_uses_only_the_prompts_weather_emojis():
    allowed = next(line for line in PROMPT.splitlines() if "Use only these" in line)
    assert all(emoji in allowed for emoji in LOOKS.values())
    assert daylook.FIGURES == ("rain", "wind_gust", "temp_max", "temp_min", "solar_avg")   # the days tool's names


def test_a_clear_sky_follows_the_season_and_the_latitude():
    summer, winter = clear_sky_mean(SUMMER, MELBOURNE), clear_sky_mean(date(2026, 6, 21), MELBOURNE)
    assert 29 < summer * 86400 / 1e6 < 34 and 8 < winter * 86400 / 1e6 < 12   # MJ/m² a day: Melbourne's clear days, about 32 and 10
    assert clear_sky_mean(date(2026, 6, 21), -80.0) == 0.0                  # the polar night
    assert clear_sky_mean(date(2026, 6, 21), 37.8) == pytest.approx(summer, rel=0.02)   # the other hemisphere's summer
    assert day_look({"solar_avg": 10.0}, latitude=-80.0, day=date(2026, 6, 21)) is None


def test_the_service_modules_say_what_they_offer():
    for module in (common, names, daylook):
        assert all(hasattr(module, name) for name in module.__all__), module.__name__
