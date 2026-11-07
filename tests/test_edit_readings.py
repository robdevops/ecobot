"""scripts/edit_readings.py: finds a spike, shows it among the readings either side in a dry run, and changes just that reading, for
good: the day's figures follow, and a fetch of that time again doesn't bring it back."""

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from lib.storage import Storage
from lib.storage.catalogue import CLOUD, BOT
from tests.fakes import TZ

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import edit_readings  # noqa: E402

DAY = date(2026, 10, 27)
BASE = int(datetime(2026, 10, 27, tzinfo=TZ).timestamp())
SPIKE = BASE + 161 * 300   # 1:25pm, breathed on


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "ecobot.sqlite", TZ)
    with s.writer.transaction() as w:
        air = w.air_sensor("42")
        w.put(air, "5min", [{"ts": BASE + 300 * i, "co2": 400.0 + i / 10, "pm2_5": 3.0} for i in range(288)], BOT)
        w.put(air, "5min", [{"ts": SPIKE, "co2": 1604.9, "co2_lo": 420.0, "co2_hi": 5202.0, "pm2_5": 3.0}], BOT)
        w.cover(air, "5min", BASE, BASE + 86399)
    yield s
    s.close()


def co2(s: Storage, ts: int):
    rows = s.read_now(lambda r: r.rows("air", [1], "5min", ts, ts))[1]
    return rows.values.get("co2", [None])[0], rows.lo.get("co2", [None])[0], rows.hi.get("co2", [None])[0], rows.values["pm2_5"][0]


def high(s: Storage) -> float:
    return s.read_now(lambda r: r.days("air", [1], DAY, DAY))[1].hi["co2"][0]


def test_the_highest_are_listed_first_and_one_reading_is_matched_by_its_time_or_a_window(storage):
    top = edit_readings.matches(storage, "air", "co2", None, None, None, None, top=3)
    assert top[0][2] == SPIKE and top[0][3:] == (1604.9, 420.0, 5202.0)
    assert [f[2] for f in edit_readings.matches(storage, "air", "co2", SPIKE, SPIKE + 1, None, None)] == [SPIKE]   # --at
    assert [f[2] for f in edit_readings.matches(storage, "air", "co2", BASE, BASE + 86400, 1000.0, None)] == [SPIKE]
    assert edit_readings.matches(storage, "air", "co2", BASE, SPIKE, 1000.0, None) == []   # the end is not included
    assert edit_readings.matches(storage, "air", "co2", SPIKE, SPIKE + 1, None, None, res="1hour") == []


def test_a_dry_run_shows_the_readings_either_side_of_each_match(storage):
    found = edit_readings.matches(storage, "air", "co2", SPIKE, SPIKE + 1, None, None)
    rows = edit_readings.around(storage, "air", "co2", found, 2)
    assert [(r[2] - SPIKE) // 300 for r in rows] == [-2, -1, 0, 1, 2] and [r[6] for r in rows] == [False, False, True, False, False]


def test_a_spike_gets_the_mean_of_the_readings_either_side_and_keeps_it_when_fetched_again(storage):
    found = edit_readings.matches(storage, "air", "co2", SPIKE, SPIKE + 1, None, None)
    values = edit_readings.new_values(storage, "air", "co2", found)
    assert values == {(1, "5min", SPIKE): 416.1}                                     # 416.0 before, 416.2 after
    assert high(storage) == 1604.9
    edit_readings.change(storage, "air", "co2", values)
    assert co2(storage, SPIKE) == (416.1, None, None, 3.0)                          # its band gone, the other readings as they were
    assert high(storage) == 428.7                                                   # the day follows (its last reading the highest now)
    with storage.writer.transaction() as w:                                         # the backfill, or the gap filling, fetches it again
        w.put(w.air_sensor("42"), "5min", [{"ts": SPIKE, "co2": 1604.9, "pm2_5": 9.0}], CLOUD)
        w.put(w.air_sensor("42"), "5min", [{"ts": SPIKE, "co2": 1604.9}], BOT)
    assert co2(storage, SPIKE) == (416.1, None, None, 3.0) and high(storage) == 428.7


def test_a_run_of_readings_each_gets_the_readings_beyond_the_run_and_set_gives_them_one(storage):
    found = edit_readings.matches(storage, "air", "co2", SPIKE - 300, SPIKE + 600, None, None)   # 1:20 to 1:30
    values = edit_readings.new_values(storage, "air", "co2", found)
    assert set(values.values()) == {416.1}                                          # 1:15 (415.9) and 1:35 (416.3), whatever lies between
    assert set(edit_readings.new_values(storage, "air", "co2", found, 400.0).values()) == {400.0}


def test_a_reading_is_named_as_the_file_has_it():
    assert edit_readings.kind_of("co2") == "air" and edit_readings.kind_of("temperature") == "climate"
    with pytest.raises(SystemExit, match="co2"):
        edit_readings.kind_of("co3")


def test_the_list_gives_each_kinds_readings_and_the_sensors_the_file_has_of_it(storage):
    lines = edit_readings.listing(storage)
    at = lines.index("air: air = AirGradient (42)")
    assert "co2" in lines[at + 1].split() and "climate: no sensor in the file" in lines


def test_a_reading_of_one_sensor_alone(tmp_path):
    s = Storage(tmp_path / "e.sqlite", TZ)
    with s.writer.transaction() as w:
        for group, value in (("outdoor", 12.0), ("indoor", 21.0)):
            w.put(w.ecowitt_sensor("mac", group), "5min", [{"ts": SPIKE, "temperature": value}])
    outdoor = s.sensors.find("mac", "outdoor").id
    assert len(edit_readings.matches(s, "climate", "temperature", SPIKE, SPIKE + 1, None, None)) == 2   # both have one
    assert [f[3] for f in edit_readings.matches(s, "climate", "temperature", SPIKE, SPIKE + 1, None, None, sensor=outdoor)] == [12.0]
    s.close()


def test_the_file_is_in_data_dir_when_it_is_set_else_the_services(monkeypatch, tmp_path):
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    assert edit_readings.default_file() == Path("/var/lib/private/ecobot/ecobot.sqlite")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert edit_readings.default_file() == tmp_path / "ecobot.sqlite"
