"""The readings tool (lib/history.py): one reading's stored values at an interval, each with its time."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from lib.history import MAX_ROWS, History, interval_starts
from lib.storage import Storage
from lib.storage.catalogue import CLOUD
from lib.timeutil import local_epoch, when
from lib.tools import Turn
from tests.fakes import TZ

MAC, LOC = "M", "42"
NOW = local_epoch(datetime(2026, 11, 9, 1, 47), TZ)   # Mon 9 Nov 2026 1:47am, as in the report that asked for this tool
SENT = {"outdoor": {"temperature": "℃", "temperature_low": "℃", "humidity": "%"}, "indoor": {"temperature": "℃", "app_tempin": "℃"},
        "wind": {"wind_speed": "km/h", "wind_gust": "km/h"}, "rainfall": {"daily": "mm", "rain_rate": "mm/hr", "event": "mm"}}


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


def tool(storage, eco: bool = True, air: bool = True, sent: dict | None = SENT) -> History:
    station = SimpleNamespace(storage=storage, mac=MAC, tz=TZ) if eco else None
    sensor = SimpleNamespace(store=SimpleNamespace(storage=storage, sensor=lambda: storage.air_sensor(LOC)), tz=TZ) if air else None
    return History(station, sensor, sent if eco else None, now=lambda: NOW)


async def ask(history: History, **args) -> dict:
    return json.loads(await history.handle(args, Turn()))


def offered(history: History, turn: Turn | None = None) -> list[str]:
    return history.tools[0].schema(turn)["function"]["parameters"]["properties"]["metric"]["enum"]


def test_each_sensor_offers_what_it_has_sent_and_rain_as_the_mm_that_fell(storage):
    names = offered(tool(storage))
    assert {"outdoor.temperature", "outdoor.humidity", "indoor.temperature", "indoor.app_temp", "wind.wind_gust", "rainfall.rain",
            "rainfall.rain_rate", "air.pm2_5", "air.co2"} <= set(names)
    assert not {"outdoor.dew_point", "indoor.humidity", "rainfall.event", "rainfall.daily", "air.air_temperature"} & set(names)
    assert {"rainfall_piezo.rain", "solar_and_uvi.uvi"} <= set(names)   # (groups with nothing recorded yet offer their whole kind)
    assert "air.air_temperature" in offered(tool(storage), Turn(air_extra=True))   # the air sensor's own: only when the words name it
    assert [n for n in offered(tool(storage, eco=False)) if not n.startswith("air.")] == []
    assert [n for n in offered(tool(storage, air=False)) if n.startswith("air.")] == []


async def test_stored_five_minute_readings_come_back_as_they_are_with_their_bands(storage):
    put(storage, "outdoor", "5min", [{"ts": at(9, 1, 5 * i), "humidity": 80 + i, "humidity_lo": 79 + i, "humidity_hi": 81 + i} for i in range(9)])
    out = await ask(tool(storage), metric="outdoor.humidity", start="2026-11-09 01:00", end="2026-11-09 01:40", interval="5min")
    assert out["columns"] == ["time", "value", "low", "high"] and out["unit"] == "%" and out["from"] == ["5min"]
    assert out["rows"][0] == ["Mon 9 Nov 2026 1:00am", 80, 79, 81] and out["rows"][-1] == ["Mon 9 Nov 2026 1:40am", 88, 87, 89]
    assert len(out["rows"]) == 9 and "missing" not in out


async def test_an_hour_is_worked_out_from_the_finer_readings_and_the_last_one_is_still_filling(storage):
    put(storage, "outdoor", "30min", [{"ts": at(8, 20) + 1800 * i, "humidity": 60 + 10 * (i // 2)} for i in range(4)])   # 8pm to 9:30pm
    put(storage, "outdoor", "5min", [{"ts": at(8, 22) + 300 * i, "humidity": 70 + (i // 12)} for i in range(12 * 4 - 2)])   # 10pm to 1:45am
    h = tool(storage)
    out = await ask(h, metric="humidity", start="2026-11-08 20:47", interval="1hour")   # "humidity each hour for the last 5 hours"
    assert [r[0] for r in out["rows"]] == [f"{d} {t}" for d, t in [("Sun 8 Nov 2026", "8:00pm"), ("Sun 8 Nov 2026", "9:00pm"),
                                                                   ("Sun 8 Nov 2026", "10:00pm"), ("Sun 8 Nov 2026", "11:00pm"),
                                                                   ("Mon 9 Nov 2026", "12:00am"), ("Mon 9 Nov 2026", "1:00am")]]
    assert [r[1] for r in out["rows"]] == [60, 70, 70, 71, 72, 73] and out["from"] == ["5min", "30min"]
    assert "note_last" in out and out["metric"] == "outdoor.humidity"


def test_intervals_are_blocks_of_local_time_across_a_clock_change():
    four = interval_starts(at(3, 9), at(4, 9), 4 * 3600, TZ)
    assert [when(t, TZ).split(" ", 4)[-1] for t in four] == ["8:00am", "12:00pm", "4:00pm", "8:00pm", "12:00am", "4:00am", "8:00am"]
    spring = interval_starts(local_epoch(datetime(2026, 10, 4, 0, 30), TZ), local_epoch(datetime(2026, 10, 4, 4, 0), TZ), 3600, TZ)
    assert [when(t, TZ).split(" ", 4)[-1] for t in spring] == ["12:00am", "1:00am", "3:00am", "4:00am"]   # (2am never happened)


async def test_too_many_rows_are_refused_with_the_interval_that_fits(storage):
    h = tool(storage)
    out = await ask(h, metric="outdoor.temperature", start="2026-11-07 01:47", interval="5min")
    assert out["error"] == "too many rows" and out["rows"] > MAX_ROWS and out["fits"] == "30min"
    out = await ask(h, metric="outdoor.temperature", start="2024-01-01 00:00", interval="1day")
    assert out["error"] == "too many rows" and out["fits"] is None


async def test_what_it_cannot_read_is_said(storage):
    h = tool(storage)
    assert (await ask(h, metric="outdoor.humidity", start="2026-11-09 00:00", interval="1hour"))["note"] == "Nothing is stored for this sensor yet."
    assert "error" in await ask(h, metric="outdoor.soil", start="2026-11-09 00:00", interval="1hour")
    assert "error" in await ask(h, metric="outdoor.humidity", start="2026-11-09 00:00", interval="fortnightly")
    assert "error" in await ask(h, metric="outdoor.humidity", start="2026-11-09 01:00", end="2026-11-09 00:00", interval="5min")
    assert "error" in await ask(h, metric="outdoor.humidity", start="yesterday", interval="5min")
    assert (await ask(h, metric="outdoor.humidity", start="2026-11-09 00:00", interval="hourly"))["interval"] == "1hour"


async def test_minutes_older_than_are_kept_say_so_and_a_coarser_interval_is_offered(storage):
    put(storage, "outdoor", "5min", [{"ts": at(7, 12), "temperature": 12.0}])   # the sensor has readings, none of them here
    h = tool(storage)
    out = await ask(h, metric="outdoor.temperature", start="2026-11-09 01:00", end="2026-11-09 01:10", interval="1min")
    assert out["missing"] == 11 and "note_minutes" in out and "note" in out and all(r[1] is None for r in out["rows"])
    put(storage, "outdoor", "5min", [{"ts": at(9, 1, 5 * i), "temperature": 12.0} for i in range(3)])
    out = await ask(h, metric="outdoor.temperature", start="2026-11-09 01:00", end="2026-11-09 01:10", interval="1min")
    assert out["fits"] == "5min" and "note_coarser" in out


async def test_gusts_take_the_highest_in_each_interval(storage):
    put(storage, "wind", "5min", [{"ts": at(8, 22) + 300 * i, "wind_speed": 5.0, "wind_gust": 10.0 + i} for i in range(24)])
    out = await ask(tool(storage), metric="wind.wind_gust", start="2026-11-08 22:00", end="2026-11-08 23:59", interval="1hour")
    assert out["columns"] == ["time", "value"] and [r[1] for r in out["rows"]] == [21.0, 33.0]


async def test_rain_is_the_mm_that_fell_in_each_interval_across_the_midnight_reset(storage):
    put(storage, "rainfall", "5min", [{"ts": at(8, 22, 30), "daily": 0.0}, {"ts": at(8, 23), "daily": 0.0}, {"ts": at(8, 23, 30), "daily": 1.0},
                                      {"ts": at(8, 23, 55), "daily": 2.0}, {"ts": at(9, 0), "daily": 0.0}, {"ts": at(9, 0, 30), "daily": 0.5}])
    out = await ask(tool(storage), metric="rainfall.rain", start="2026-11-08 23:00", end="2026-11-09 00:59", interval="1hour")
    assert out["unit"] == "mm" and out["columns"] == ["time", "value"] and [r[1] for r in out["rows"]] == [2.0, 0.5]


async def test_an_air_glitch_is_taken_out_as_air_quality_takes_it_out(storage):
    rows = [{"ts": at(8, 22) + 300 * i, "pm2_5": 5.0, "pm10": 8.0, "pm1": 3.0} for i in range(6)]
    rows[3] |= {"pm2_5": 50.0, "pm10": 60.0, "pm1": 40.0}   # one reading far above both sides: the sensor, not the air
    put(storage, "air", "5min", rows)
    out = await ask(tool(storage), metric="air.pm2_5", start="2026-11-08 22:00", end="2026-11-08 22:29", interval="30min")
    assert out["rows"] == [["Sun 8 Nov 2026 10:00pm", 5.0, 5.0, 5.0]] and out["unit"] == "µg/m³"


async def test_the_air_sensors_older_hourly_history_is_read_hourly_never_split(storage):
    put(storage, "air", "1hour", [{"ts": at(8, 20 + i), "pm2_5": 4.0 + i} for i in range(3)])
    h = tool(storage, eco=False)
    out = await ask(h, metric="air.pm2_5", start="2026-11-08 20:00", end="2026-11-08 22:59", interval="30min")
    assert out["missing"] == 6 and out["fits"] == "1hour" and "note_coarser" in out
    out = await ask(h, metric="air.pm2_5", start="2026-11-08 20:00", end="2026-11-08 22:59", interval="1hour")
    assert [r[1] for r in out["rows"]] == [4.0, 5.0, 6.0] and out["from"] == ["1hour"]


async def test_days_come_from_the_day_rows_with_when_their_extremes_happened(storage):
    first = at(6, 0)
    put(storage, "outdoor", "5min", [{"ts": first + 300 * i, "temperature": 10.0 + (i % 288 == 180) * 10} for i in range(288 * 2)])
    out = await ask(tool(storage), metric="outdoor.temperature", start="2026-11-06 00:00", end="2026-11-07 23:59", interval="1day")
    assert out["columns"] == ["day", "value", "low", "low_time", "high", "high_time"]
    (day, mean, low, low_time, high, high_time), _ = out["rows"]
    assert day == "Fri 6 Nov 2026" and low == 10.0 and high == 20.0 and high_time == "3:00pm" and mean == 10.0   # (10.03: as the station writes it)
