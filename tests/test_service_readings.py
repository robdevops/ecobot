"""The service's `readings` tool (lib/service/readings.py): one reading's values at an interval, over a period in words, with the rows
the bot's readings tool (lib/history.py) gives for the same span."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from lib.history import INTERVALS, MAX_ROWS
from lib.service import readings
from lib.service.common import FRESH
from lib.service.context import Context
from lib.service.names import Catalogue
from lib.storage import Storage
from tests.old_tools import History
from lib.storage.catalogue import CLOUD
from lib.timeutil import local_epoch
from lib.tools import Tools, Turn
from tests.fakes import TZ

MAC, LOC = "M", "42"
NOW = local_epoch(datetime(2026, 11, 9, 1, 47), TZ)   # Mon 9 Nov 2026 1:47am, as lib/history.py's tests ask
SENT = {"outdoor": {"temperature": "℃", "temperature_low": "℃", "humidity": "%"}, "indoor": {"temperature": "℃", "humidity": "%"},
        "wind": {"wind_speed": "km/h", "wind_gust": "km/h", "wind_direction": "º"}, "rainfall": {"daily": "mm", "rain_rate": "mm/hr"}}


def at(day: int, hour: int, minute: int = 0) -> int:
    return local_epoch(datetime(2026, 11, day, hour, minute), TZ)


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "ecobot.sqlite", TZ)
    yield s
    s.close()


def put(storage, sensor: str, res: str, rows: list[dict]):
    """Readings written as a source writes them: an Ecowitt group's, or the air sensor's ("air")."""
    def write(w):
        s = w.air_sensor(LOC) if sensor == "air" else w.ecowitt_sensor(MAC, sensor)
        w.put(s, res, rows, CLOUD)
    storage.writer.run(write, ())


class AirStore:
    """The air sensor's readings in the file, as AirGradient keeps them (lib/airgradient/readings.py): its sensor is a property."""

    def __init__(self, storage):
        self.storage = storage

    @property
    def sensor(self):
        return self.storage.air_sensor(LOC)


def sources(storage):
    """(the station, the air sensor) as the tools read them: the file, the station's MAC, the air sensor's place in the file."""
    return SimpleNamespace(storage=storage, mac=MAC, tz=TZ), SimpleNamespace(store=AirStore(storage), tz=TZ)


def tool(storage):
    station, air = sources(storage)
    (made,) = readings.tools(Context(eco=station, air=air, catalogue=Catalogue.of(SENT), tz=TZ, clock=lambda: NOW))
    return made


async def ask(storage, meta: dict | None = None, **args) -> dict:
    return json.loads(await tool(storage).call(args, meta))


async def old(storage, **args) -> dict:
    """The bot's readings tool (lib/history.py) over the same file, at the same time (it calls the air sensor's `sensor`)."""
    station = SimpleNamespace(storage=storage, mac=MAC, tz=TZ)
    air = SimpleNamespace(store=SimpleNamespace(storage=storage, sensor=lambda: storage.air_sensor(LOC)), tz=TZ)
    return json.loads(await History(station, air, SENT, now=lambda: NOW).handle(args, Turn()))


ROWS = ("columns", "rows", "from", "missing", "note_coarser", "fits", "note_minutes", "note_daily", "note_last", "note")


def same_rows(new: dict, before: dict) -> bool:
    return {k: new.get(k) for k in ROWS} == {k: before.get(k) for k in ROWS} and new["value_is"] == before["value_is"]


# ---------- what it takes ----------
def test_it_takes_one_reading_a_period_in_words_and_an_interval(storage):
    made = tool(storage)
    props = made.parameters["properties"]
    assert made.name == "readings" and made.parameters["required"] == ["reading", "interval"]
    assert set(props) == {"reading", "period", "start", "end", "interval"} and props["interval"]["enum"] == list(INTERVALS)
    assert "Default: last 24 hours" in props["period"]["description"] and "this week" in props["period"]["description"]
    assert "outdoor.humidity" in props["reading"]["description"] and "ONE reading" in props["reading"]["description"]
    assert len(made.description) < 1500 and all(f"({tool})" in made.description for tool in ("summary", "days", "current"))
    assert 'a record ("hottest ever", even "use 5 minute data": summary, period "on record")' in made.description
    assert '"past week" (the last 7 days) is never sent as "last week"' in props["period"]["description"]
    assert made.description.count("{reading:") >= 3 and "`value_is`" in made.description and "`fits`" in made.description
    assert made.annotations == {"readOnlyHint": True, "openWorldHint": False}   # it reads the file only


# ---------- the rows ----------
async def test_the_rows_are_the_bots_readings_over_the_period_the_words_name(storage):
    put(storage, "outdoor", "30min", [{"ts": at(8, 20) + 1800 * i, "humidity": 60 + 10 * (i // 2)} for i in range(4)])   # 8pm to 9:30pm
    put(storage, "outdoor", "5min", [{"ts": at(8, 22) + 300 * i, "humidity": 70 + (i // 12)} for i in range(12 * 4 - 2)])   # 10pm on
    out = await ask(storage, reading="outdoor.humidity", period="last 5 hours", interval="1hour")   # "humidity each hour for the last 5 hours"
    assert list(out)[:3] == ["period", "as_of", "fresh"] and out["fresh"] == FRESH
    assert out["period"]["asked"] == "last 5 hours" and out["period"]["text"] == "the last 5 hours: Sun 8 Nov 8:47pm – Mon 9 Nov 2026 1:47am"
    assert out["as_of"] == {"text": "Mon 9 Nov 2026 1:47am", "time": "2026-11-09T01:47:00+11:00"}
    assert list(out)[3:8] == ["reading", "label", "unit", "interval", "value_is"]
    assert (out["reading"], out["label"], out["unit"], out["interval"]) == ("outdoor.humidity", "Outdoor humidity", "%", "1hour")
    assert [r[1] for r in out["rows"]] == [60, 70, 70, 71, 72, 73] and out["from"] == ["5min", "30min"] and "note_last" in out
    assert same_rows(out, await old(storage, metric="outdoor.humidity", start="2026-11-08 20:47", interval="1hour"))


async def test_with_no_period_it_reads_the_last_24_hours(storage):
    put(storage, "outdoor", "5min", [{"ts": at(8, 1) + 300 * i, "temperature": 10.0 + i % 12} for i in range(24 * 12 + 9)])
    out = await ask(storage, reading="temperature", interval="1hour")
    assert out["period"]["asked"] == "last 24 hours" and out["period"]["start"] == "2026-11-08T01:47:00+11:00"
    assert len(out["rows"]) == 25 and out["rows"][0][0] == "Sun 8 Nov 2026 1:00am" and out["rows"][-1][0] == "Mon 9 Nov 2026 1:00am"
    assert same_rows(out, await old(storage, metric="outdoor.temperature", start="2026-11-08 01:47", interval="1hour"))


async def test_rain_gusts_and_days_are_read_as_the_bot_reads_them(storage):
    put(storage, "rainfall", "5min", [{"ts": at(8, 22, 30), "daily": 0.0}, {"ts": at(8, 23), "daily": 0.0}, {"ts": at(8, 23, 30), "daily": 1.0},
                                      {"ts": at(8, 23, 55), "daily": 2.0}, {"ts": at(9, 0), "daily": 0.0}, {"ts": at(9, 0, 30), "daily": 0.5}])
    out = await ask(storage, reading="rain", start="2026-11-08 23:00", end="2026-11-09 00:59", interval="hourly")
    assert out["reading"] == "rainfall.rain" and out["interval"] == "1hour" and [r[1] for r in out["rows"]] == [2.0, 0.5]   # the midnight reset
    assert out["value_is"] == "the mm of rain that fell in the interval" and out["columns"] == ["time", "value"]
    assert same_rows(out, await old(storage, metric="rainfall.rain", start="2026-11-08 23:00", end="2026-11-09 00:59", interval="1hour"))
    put(storage, "wind", "5min", [{"ts": at(8, 22) + 300 * i, "wind_speed": 5.0, "wind_gust": 10.0 + i} for i in range(24)])
    out = await ask(storage, reading="gust", start="2026-11-08 22:00", end="2026-11-08 23:59", interval="1hour")
    assert out["reading"] == "wind.wind_gust" and [r[1] for r in out["rows"]] == [21.0, 33.0]
    put(storage, "outdoor", "5min", [{"ts": at(6, 0) + 300 * i, "temperature": 10.0 + (i % 288 == 180) * 10} for i in range(288 * 2)])
    out = await ask(storage, reading="outdoor.temperature", start="2026-11-06", end="2026-11-07", interval="1day")   # dates: whole days
    assert out["columns"] == ["day", "value", "low", "low_time", "high", "high_time"] and out["rows"][0][:5] == ["Fri 6 Nov 2026", 10.0, 10.0, "12:00am", 20.0]
    assert same_rows(out, await old(storage, metric="outdoor.temperature", start="2026-11-06 00:00", end="2026-11-07 23:59", interval="1day"))


async def test_the_air_sensors_readings_and_its_own_temperature(storage):
    rows = [{"ts": at(8, 22) + 300 * i, "pm2_5": 5.0, "pm10": 8.0, "pm1": 3.0, "air_temperature": 18.0 + i} for i in range(6)]
    rows[3] |= {"pm2_5": 50.0, "pm10": 60.0, "pm1": 40.0}   # a glitch of the sensor, taken out as air_quality takes it out
    put(storage, "air", "5min", rows)
    out = await ask(storage, reading="pm2.5", start="2026-11-08 22:00", end="2026-11-08 22:29", interval="30min")
    assert out["reading"] == "air.pm2_5" and out["unit"] == "µg/m³" and out["rows"] == [["Sun 8 Nov 2026 10:00pm", 5.0, 5.0, 5.0]]
    out = await ask(storage, reading="airgradient temperature", start="2026-11-08 22:00", end="2026-11-08 22:29", interval="30min")
    assert (out["reading"], out["label"], out["unit"]) == ("air.temperature", "AirGradient temperature", "°C") and out["rows"][0][1:] == [20.5, 18.0, 23.0]


async def test_a_word_for_several_readings_reads_the_first_and_names_the_others(storage):
    put(storage, "outdoor", "5min", [{"ts": at(9, 1), "humidity": 80}])
    out = await ask(storage, reading="humidity", period="last 30 minutes", interval="5min")
    assert out["reading"] == "outdoor.humidity" and "indoor.humidity" in out["note_others"] and "call it again" in out["note_others"]
    out = await ask(storage, reading="wind", period="last 30 minutes", interval="5min")
    assert out["reading"] == "wind.wind_speed" and "note_others" not in out   # wind is its speed: its direction goes with it only on a chart
    put(storage, "wind", "5min", [{"ts": at(9, 1, 5 * i), "wind_speed": 5.0, "wind_direction": 90 + i} for i in range(9)])
    out = await ask(storage, reading="wind direction", period="last 30 minutes", interval="5min")
    assert out["reading"] == "wind.wind_direction" and out["columns"] == ["time", "value"] and out["rows"][-2:] == [["Mon 9 Nov 2026 1:40am", 98], ["Mon 9 Nov 2026 1:45am", None]]


# ---------- refusals that teach ----------
async def test_a_reading_or_an_interval_it_cant_read_lists_those_it_can(storage):
    out = await ask(storage, reading="soil moisture", interval="1hour")
    assert "isn't a reading" in out["error"] and "outdoor.humidity" in out["names"] and out["words"]["pressure"] == ["pressure.relative"]
    out = await ask(storage, interval="1hour")
    assert out["error"].startswith("Send `reading`") and "rainfall.rain" in out["names"]
    out = await ask(storage, reading="humidity")
    assert out["error"].startswith("Send `interval`") and out["intervals"] == list(INTERVALS)
    out = await ask(storage, reading="humidity", interval="fortnightly")
    assert "'fortnightly' isn't one" in out["error"] and out["intervals"] == list(INTERVALS)


async def test_a_period_it_cant_read_says_what_it_can(storage):
    out = await ask(storage, reading="humidity", period="the other day", interval="1hour")
    assert "isn't a period" in out["error"] and any(form.startswith("last N hours") for form in out["forms"])
    assert "It is now Mon 9 Nov 2026 1:47am." in out["error"]
    assert "use forecast" in (await ask(storage, reading="humidity", period="tomorrow", interval="1hour"))["error"]
    both = await ask(storage, reading="humidity", period="today", start="2026-11-08", interval="1hour")   # two spans: refused
    assert "`period` 'today' is Mon 9 Nov 2026 but `start`/`end` say Sun 8 Nov 2026 – now: send one of them" in both["error"]
    junk = await ask(storage, reading="humidity", period="today", start="count_only", interval="1hour")   # junk: the period, said so
    assert "error" not in junk and junk["period"]["asked"] == "today" and "were ignored" in junk["period"]["note"]
    out = await ask(storage, reading="humidity", period="on record", interval="1day")
    assert "Nothing is held yet" in out["error"]


async def test_too_many_rows_are_refused_with_the_interval_that_fits_and_that_one_is_accepted(storage):
    out = await ask(storage, reading="outdoor.temperature", period="last 3 days", interval="5min")
    assert out["error"].startswith("Too many rows: the last 3 days") and out["rows"] > MAX_ROWS and out["max_rows"] == MAX_ROWS
    assert out["fits"] == "30min" and "Ask again with interval 30min." in out["error"]
    assert "error" not in await ask(storage, reading="outdoor.temperature", period="last 3 days", interval=out["fits"])
    out = await ask(storage, reading="outdoor.temperature", period="last 2 years", interval="1day")
    assert out["fits"] is None and "summary" in out["error"] and "days" in out["error"]


async def test_on_record_starts_at_the_first_day_held(storage):
    put(storage, "outdoor", "5min", [{"ts": at(6, 12) + 300 * i, "temperature": 12.0} for i in range(12)])
    out = await ask(storage, reading="outdoor.temperature", period="on record", interval="1day")
    assert out["period"]["start"] == "2026-11-06T00:00:00+11:00" and [r[0] for r in out["rows"]] == \
        ["Fri 6 Nov 2026", "Sat 7 Nov 2026", "Sun 8 Nov 2026", "Mon 9 Nov 2026"] and out["missing"] == 3


async def test_what_isnt_held_is_said_never_filled(storage):
    out = await ask(storage, reading="outdoor.humidity", period="last 2 hours", interval="1hour")
    assert out["rows"] == [] and out["note"] == "Nothing is stored for this sensor yet."
    put(storage, "outdoor", "5min", [{"ts": at(7, 12), "temperature": 12.0}])   # the sensor has readings, none of them here
    out = await ask(storage, reading="outdoor.temperature", start="2026-11-09 01:00", end="2026-11-09 01:10", interval="1min")
    assert out["missing"] == 10 and "note_minutes" in out and "never fill the gap" in out["note"]   # (an end with a time stops at it)
    put(storage, "outdoor", "5min", [{"ts": at(9, 1, 5 * i), "temperature": 12.0} for i in range(3)])
    out = await ask(storage, reading="outdoor.temperature", start="2026-11-09 01:00", end="2026-11-09 01:10", interval="1min")
    assert out["fits"] == "5min" and "note_coarser" in out


# ---------- only its arguments reach it ----------
async def test_only_the_arguments_reach_it_and_its_metadata_changes_nothing(storage):
    put(storage, "outdoor", "5min", [{"ts": at(9, 1, 5 * i), "humidity": 80 + i} for i in range(9)])
    args = {"reading": "outdoor.humidity", "period": "last hour", "interval": "5min"}
    direct = await tool(storage).call(args)
    via_bot = await Tools([tool(storage).tool()]).call("readings", json.dumps(args), Turn(per_day=True))
    assert via_bot == direct and json.loads(direct)["interval"] == "5min"
    assert await tool(storage).call(args, {"fetch": False, "priority": "background"}) == direct   # nothing is fetched here anyway
