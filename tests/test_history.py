"""lib/history.py, the engine of the service's `readings` tool (lib/service/readings.py): one reading's stored values at an interval,
each with its time. Driven through that tool (until step 6c through the old readings tool's handler, tests/old_tools.py, deleted)."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from lib.history import MAX_ROWS, finest_fit, interval_starts
from lib.service import readings
from lib.service.context import Context
from lib.service.names import Catalogue
from lib.storage import Storage
from lib.storage.catalogue import CLOUD
from lib.timeutil import local_epoch, when
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


class AirStore:
    """The air sensor's store as the readings tool reads it: its file, and its sensor a property (AirReadings.sensor)."""

    def __init__(self, storage):
        self.storage = storage

    @property
    def sensor(self):
        return self.storage.air_sensor(LOC)


def put(storage, sensor: str, res: str, rows: list[dict]):
    """Readings written as a source writes them: an Ecowitt group's, or the air sensor's ("air")."""
    def write(w):
        s = w.air_sensor(LOC) if sensor == "air" else w.ecowitt_sensor(MAC, sensor)
        w.put(s, res, rows, CLOUD)
    storage.writer.run(write, ())


def tool(storage, eco: bool = True, air: bool = True, sent: dict | None = SENT):
    """The service's readings tool over the file: the station's sensors (those `sent`) and the air sensor, at NOW."""
    station = SimpleNamespace(storage=storage, mac=MAC, tz=TZ) if eco else None
    sensor = SimpleNamespace(store=AirStore(storage), tz=TZ) if air else None
    catalogue = Catalogue.of(sent if eco else None, station=eco, air=air)
    (made,) = readings.tools(Context(eco=station, air=sensor, catalogue=catalogue, tz=TZ, clock=lambda: NOW))
    made.catalogue = catalogue
    return made


async def ask(made, metric: str, **args) -> dict:
    """The tool's answer for `metric` (its reading) and these arguments (start and end: local times)."""
    return json.loads(await made.call({"reading": metric, **args}, None))


def offered(made) -> list[str]:
    return made.catalogue.names


def test_each_sensor_offers_what_it_has_sent_and_rain_as_the_mm_that_fell(storage):
    names = offered(tool(storage))
    assert {"outdoor.temperature", "outdoor.humidity", "indoor.temperature", "indoor.app_temp", "wind.wind_gust", "rainfall.rain",
            "rainfall.rain_rate", "air.pm2_5", "air.co2"} <= set(names)
    assert not {"outdoor.dew_point", "indoor.humidity", "rainfall.event", "rainfall.daily"} & set(names)
    assert {"rainfall_piezo.rain", "solar_and_uvi.uvi", "air.temperature"} <= set(names)   # (groups with nothing recorded yet: their whole kind)
    assert [n for n in offered(tool(storage, eco=False)) if not n.startswith("air.")] == []
    assert [n for n in offered(tool(storage, air=False)) if n.startswith("air.")] == []


async def test_stored_five_minute_readings_come_back_as_they_are_with_their_bands(storage):
    put(storage, "outdoor", "5min", [{"ts": at(9, 1, 5 * i), "humidity": 80 + i, "humidity_lo": 79 + i, "humidity_hi": 81 + i} for i in range(9)])
    out = await ask(tool(storage), metric="outdoor.humidity", start="2026-11-09 01:00", end="2026-11-09 01:41", interval="5min")   # (the end not included)
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
    assert "note_last" in out and out["reading"] == "outdoor.humidity"


def test_intervals_are_blocks_of_local_time_across_a_clock_change_and_four_hours_are_the_stations_own():
    four = interval_starts(at(3, 9), at(4, 9), 4 * 3600, TZ)   # Ecowitt's 4-hour records are cut on UTC hours: 3am, 7am ... in AEDT
    assert [when(t, TZ).split(" ", 4)[-1] for t in four] == ["7:00am", "11:00am", "3:00pm", "7:00pm", "11:00pm", "3:00am", "7:00am"]
    days = interval_starts(at(3, 9), at(5, 9), 86400, TZ)
    assert [when(t, TZ).split(" ", 4)[-1] for t in days] == ["12:00am"] * 3
    spring = interval_starts(local_epoch(datetime(2026, 10, 4, 0, 30), TZ), local_epoch(datetime(2026, 10, 4, 4, 0), TZ), 3600, TZ)
    assert [when(t, TZ).split(" ", 4)[-1] for t in spring] == ["12:00am", "1:00am", "3:00am", "4:00am"]   # (2am never happened)


async def test_too_many_rows_are_refused_with_the_interval_that_fits(storage):
    h = tool(storage)
    out = await ask(h, metric="outdoor.temperature", start="2026-11-07 01:47", interval="5min")
    assert out["error"].startswith("Too many rows: ") and out["rows"] > MAX_ROWS and out["fits"] == "30min"
    out = await ask(h, metric="outdoor.temperature", start="2024-01-01 00:00", interval="1day")
    assert out["error"].startswith("Too many rows: ") and out["fits"] is None


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
    out = await ask(h, metric="outdoor.temperature", start="2026-11-09 01:00", end="2026-11-09 01:11", interval="1min")   # (the end not included)
    assert out["missing"] == 11 and "note_minutes" in out and "note" in out and all(r[1] is None for r in out["rows"])
    put(storage, "outdoor", "5min", [{"ts": at(9, 1, 5 * i), "temperature": 12.0} for i in range(3)])
    out = await ask(h, metric="outdoor.temperature", start="2026-11-09 01:00", end="2026-11-09 01:11", interval="1min")
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


async def test_a_four_hour_record_is_its_own_row_and_a_reading_straddling_two_rows_is_left_out(storage):
    put(storage, "outdoor", "4hour", [{"ts": at(5, 11), "temperature": 20.0, "temperature_lo": 18.0, "temperature_hi": 35.0}])  # 11am-3pm
    put(storage, "outdoor", "5min", [{"ts": at(5, 8) + 300 * i, "temperature": 15.0} for i in range(36)])   # 8am to 10:55am
    out = await ask(tool(storage), metric="outdoor.temperature", start="2026-11-05 08:00", end="2026-11-05 14:59", interval="4hour")
    assert out["rows"] == [["Thu 5 Nov 2026 7:00am", 15.0, 15.0, 15.0], ["Thu 5 Nov 2026 11:00am", 20.0, 18.0, 35.0]]
    put(storage, "outdoor", "30min", [{"ts": at(5, 23, 30), "temperature": 9.0, "temperature_lo": 1.0, "temperature_hi": 9.0}])
    out = await ask(tool(storage), metric="outdoor.temperature", start="2026-11-05 23:00", end="2026-11-05 23:59", interval="5min")
    assert all(r[1] is None for r in out["rows"]) and out["fits"] == "30min"   # (a 30-minute reading is not split into 5-minute rows)


async def test_rain_from_two_resolutions_never_reads_their_seam_as_the_midnight_reset(storage):
    put(storage, "rainfall", "5min", [{"ts": at(8, 20) + 300 * i, "daily": 10.0 + (0.25 if i >= 12 else 0)} for i in range(13)])  # 8pm-9pm
    put(storage, "rainfall", "1min", [{"ts": at(8, 21, 2) + 60 * i, "daily": 10.1 + 0.05 * i} for i in range(28)])   # 9:02pm on
    out = await ask(tool(storage), metric="rainfall.rain", start="2026-11-08 20:00", end="2026-11-08 21:29", interval="1hour")
    (_, eight), (_, nine) = out["rows"]
    assert eight == 0.0 and 1.4 <= nine <= 1.5   # 9:02pm to 9:29pm (never the day's whole total again: about 11 mm)


async def test_gusts_take_the_records_own_highest(storage):
    put(storage, "wind", "30min", [{"ts": at(7, 10) + 1800 * i, "wind_gust": 20.0, "wind_gust_hi": 35.0 + i} for i in range(4)])
    out = await ask(tool(storage), metric="wind.wind_gust", start="2026-11-07 10:00", end="2026-11-07 11:59", interval="1hour")
    assert [r[1] for r in out["rows"]] == [36.0, 38.0]


async def test_the_interval_still_filling_is_not_a_gap_to_ask_coarser_for(storage):
    put(storage, "outdoor", "5min", [{"ts": at(9, 0) + 300 * i, "temperature": 12.0} for i in range(21)])   # midnight to 1:40am
    put(storage, "outdoor", "30min", [{"ts": at(9, 0) + 1800 * i, "temperature": 12.0} for i in range(4)])
    out = await ask(tool(storage), metric="outdoor.temperature", start="2026-11-09 01:00", interval="5min")
    assert out["missing"] == 1 and "fits" not in out and "note_coarser" not in out and "note_last" in out


async def test_a_period_held_only_as_days_points_to_days(storage):
    def write(w):
        utc_day = int(datetime(2026, 11, 2, tzinfo=UTC).timestamp())   # (Ecowitt's daily records are cut on UTC days)
        w.put(w.ecowitt_sensor(MAC, "outdoor"), "1day", [{"ts": utc_day, "temperature": 14.0, "temperature_lo": 8.0, "temperature_hi": 21.0}])
    storage.writer.run(write, ())
    out = await ask(tool(storage), metric="outdoor.temperature", start="2026-11-02 00:00", end="2026-11-02 23:59", interval="4hour")
    assert out["fits"] == "1day" and "ask again with interval 1day" in out["note"]


async def test_an_air_glitch_at_the_edge_of_the_window_is_still_taken_out(storage):
    put(storage, "air", "5min", [{"ts": at(8, 21, 30) + 300 * i, "pm2_5": 50.0 if i == 6 else 5.0, "pm10": 8.0, "pm1": 3.0} for i in range(12)])
    out = await ask(tool(storage), metric="air.pm2_5", start="2026-11-08 22:00", end="2026-11-08 22:29", interval="30min")
    assert out["rows"] == [["Sun 8 Nov 2026 10:00pm", 5.0, 5.0, 5.0]]


async def test_a_time_with_an_offset_is_moved_into_local_time(storage):
    h = tool(storage)
    put(storage, "outdoor", "5min", [{"ts": at(9, 1), "temperature": 12.0}])
    out = await ask(h, metric="outdoor.temperature", start="2026-11-08T14:00:00Z", end="2026-11-08T14:05:00Z", interval="5min")
    assert out["rows"][0] == ["Mon 9 Nov 2026 1:00am", 12.0, 12.0, 12.0]


async def test_the_interval_offered_instead_is_one_that_is_then_accepted(storage):
    h = tool(storage)
    for start in ("2026-08-15 07:15", "2026-09-20 22:00", "2026-10-28 01:47", "2026-01-14 00:00"):   # (the third: 300 hours; the last: 300 days)
        refused = await ask(h, metric="outdoor.temperature", start=start, end="2026-11-09 01:47", interval="1min")
        assert refused["error"].startswith("Too many rows: ") and refused["fits"], start
        assert "error" not in await ask(h, metric="outdoor.temperature", start=start, end="2026-11-09 01:47", interval=refused["fits"]), start
    assert finest_fit(local_epoch(datetime(2026, 1, 14), TZ), NOW, TZ) == "1day"
