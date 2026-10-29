"""Moving the old files into ecobot.sqlite (lib/storage/migrate.py): old-format files built here by the old stores themselves, and the fake
station's whole archive, whose every day must come out as ecowitt.days.day_values works it out from the old cache."""

import shutil
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from lib.airgradient.store import AirStore
from lib.ecowitt.api import UNITS
from lib.ecowitt.days import day_values
from lib.ecowitt.live import LiveRanges
from lib.ecowitt.minutes import MinuteStore
from lib.ecowitt.store import HistoryCache
from lib.snapshots import Snapshots
from lib.storage import FILE, Storage
from lib.storage import migrate as migration
from lib.storage.catalogue import BOT, CLOUD
from lib.timeutil import day_bounds, local_date
from tests.fakes import MAC, TZ

DAY = 86400


def old_files(folder: Path, now: int) -> dict:
    """The five old files, written by the old stores: what they hold, for the checks."""
    folder.mkdir(parents=True, exist_ok=True)
    d = local_date(now, TZ) - timedelta(days=5)
    start, end = day_bounds(d, TZ, True)
    cache = HistoryCache(folder / "ecowitt_cache.sqlite", UNITS)
    five = {"outdoor": {"temperature": {"unit": "℃", "list": {str(t): f"{10 + (t - start) / 3600:.1f}" for t in range(start, end + 1, 300)}},
                        "humidity": {"unit": "%", "list": {str(t): "80" for t in range(start, end + 1, 300)}},
                        "lightning": {"unit": "km", "list": {str(start): "12"}}},
            "rainfall": {"daily": {"unit": "mm", "list": {str(start + 3600): "0.0", str(start + 3900): "-"}}}}
    cache.store(MAC, "5min", ["outdoor", "rainfall"], five, start, end)
    thirty = {"outdoor": {"temperature": {"unit": "℃", "list": {str(t): "15.0" for t in range(start, end + 1, 1800)}},
                          "temperature_low": {"unit": "℃", "list": {str(t): "14.0" for t in range(start, end + 1, 1800)}},
                          "temperature_high": {"unit": "℃", "list": {str(t): "16.0" for t in range(start, end + 1, 1800)}}}}
    cache.store(MAC, "30min", ["outdoor"], thirty, start - 7 * DAY, end)
    cache.db.execute("INSERT INTO series (mac, cycle, grp, field, unit) VALUES (?, '30min', 'outdoor', 'vpd', 'inHg')", (MAC,))
    vpd = cache.db.execute("SELECT id FROM series WHERE field = 'vpd'").fetchone()[0]
    cache.db.execute("INSERT INTO points VALUES (?, ?, '0.261')", (vpd, start))
    cache.db.execute("DELETE FROM meta WHERE key = 'unit_fixes'")   # as a cache from before the vpd's unit fix
    cache.db.commit()
    cache.close()
    live = LiveRanges(folder / "ecowitt_live.sqlite")
    live.put({"outdoor.temperature": {start + 600: (3, 9.0, 13.0), start + 900: (1, 11.0, 11.0)}})
    live.close()
    minutes = MinuteStore(folder / "ecowitt_minutes.sqlite")
    minutes.put({"outdoor": {"temperature": {"unit": "℃", "list": {str(t): "12.5" for t in range(now - 3600, now, 60)}}}}, now)
    minutes.close()
    air = AirStore(folder / "airgradient_cache.sqlite", "42", TZ)
    air.save_day(d, [{"ts": t, "pm2_5": 5.0, "co2": 420.0} for t in range(start, end + 1, 300)])
    hourly = d - timedelta(days=200)
    h_start = day_bounds(hourly, TZ)[0]
    air.save_day(hourly, [{"ts": h_start + 3600 * h, "pm2_5": 7.0} for h in range(24)])
    air.add_recorded({"ts": now - 900, "pm2_5": 9.0, "_range": {"pm2_5": (8.0, 12.0)}}, now)
    air.add_spreads({"pm2_5": {start: (4.0, 6.0)}})
    air.close()
    snaps = Snapshots(folder / "conditions_cache.sqlite")
    snaps.save("pollen", {"level": "Low"}, now=100)
    snaps.save("pollen", {"level": "High"}, now=200)
    snaps.put("forecast_baseline", "x")
    snaps.close()
    return {"day": d, "start": start, "end": end, "hourly": hourly}


def test_the_old_files_move_whole_and_are_not_touched(tmp_path):
    now = int(time.time())
    held = old_files(tmp_path, now)
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.sqlite")}
    report = migration.migrate(tmp_path, TZ)
    assert report.ok, report.problem
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.sqlite") if p.name != FILE} == before   # the old files, as they were
    assert not (tmp_path / f"{FILE}.part").exists() and report.moved["station readings"] == 288 * 2 + 3 * 48 + 4   # (and the lightning, two rain values, the vpd)
    assert report.extra == {"outdoor.lightning": 1, "rainfall.daily": 1}       # no column for it; a value that is no number
    assert report.differ == {}
    s = Storage(tmp_path / FILE, TZ)
    try:
        r, start, d = s.reader, held["start"], held["day"]
        outdoor, air = s.sensors.find(MAC, "outdoor"), s.air_sensor("42")
        assert s.write_db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        five = r.rows("climate", [outdoor.id], "5min", start, held["end"])[outdoor.id]
        assert len(five.ts) == 288 and five.values["temperature"][0] == 10.0 and five.values["humidity"][0] == 80.0
        assert (five.lo["temperature"][2], five.hi["temperature"][2]) == (9.0, 13.0)          # the bot's own range of those 5 minutes
        assert five.lo["temperature"][3] is None                                              # one reading is no range
        thirty = r.rows("climate", [outdoor.id], "30min", start, held["end"])[outdoor.id]
        assert thirty.lo["temperature"][0] == 9.0 and thirty.hi["temperature"][0] == 16.0      # Ecowitt's own, widened by the 5 minutes'
        assert thirty.values["vpd"][0] == pytest.approx(0.884)                                # converted to kPa, as HistoryCache does
        assert s.coverage.held(outdoor.id, "5min", start, held["end"]) and s.coverage.held(outdoor.id, "30min", start - 7 * DAY, held["end"])
        minutes = r.rows("climate", [outdoor.id], "1min", now - 3600, now)[outdoor.id]
        assert len(minutes.ts) == 60 and set(minutes.values) == {"temperature"}
        day = r.days("climate", [outdoor.id], d, d)[outdoor.id]
        assert day.source == ["5min"] and day.hi["temperature"] == [33.9] and day.lo["temperature"] == [9.0]   # the bot's band counts
        found = s.write_db.execute("SELECT res, COUNT(*), SUM(src = ?), SUM(src = ?) FROM air WHERE sensor = ? GROUP BY res ORDER BY res",
                                   (BOT, CLOUD, air.id)).fetchall()
        assert found == [("1hour", 24, 0, 24), ("5min", 289, 1, 288)]
        assert s.write_db.execute("SELECT pm2_5_lo, pm2_5_hi FROM air WHERE ts = ?", (start,)).fetchone() == (4.0, 6.0)
        assert s.coverage.held(air.id, "5min", *day_bounds(d, TZ, True)) and s.coverage.held(air.id, "1hour", *day_bounds(held["hourly"], TZ, True))
        assert s.write_db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2
        assert s.write_db.execute("SELECT value FROM snapshot_meta WHERE key = 'forecast_baseline'").fetchone()[0] == "x"
        assert r.meta("ecowitt_signature") and "ecowitt_cache.sqlite" in r.meta("migrated")
    finally:
        s.close()
    assert migration.migrate(tmp_path, TZ).skipped == f"{FILE} is there already"


def test_a_move_that_does_not_add_up_leaves_no_new_file(tmp_path, monkeypatch):
    old_files(tmp_path, int(time.time()))
    real = migration.Storage

    class Lossy(real):
        """A storage whose writer loses one reading of each batch."""
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            put = self.writer.put
            self.writer.put = lambda sensor, res, rows, *a, **k: put(sensor, res, rows[:-1], *a, **k)
    monkeypatch.setattr(migration, "Storage", Lossy)
    report = migration.migrate(tmp_path, TZ)
    assert not report.ok and "holds" in report.problem and "carries on with the old files" in report.lines()[0]
    assert not (tmp_path / FILE).exists() and not (tmp_path / f"{FILE}.part").exists()


def test_without_room_on_the_disk_nothing_is_moved_and_a_run_cut_short_is_started_again(tmp_path, monkeypatch):
    old_files(tmp_path, int(time.time()))
    monkeypatch.setattr(migration.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10 ** 9, 10 ** 9, 1000))
    report = migration.migrate(tmp_path, TZ)
    assert not report.ok and "MB free" in report.problem and not (tmp_path / FILE).exists()
    monkeypatch.undo()
    (tmp_path / f"{FILE}.part").write_bytes(b"left by a run that was cut short")
    assert migration.migrate(tmp_path, TZ).ok and not (tmp_path / f"{FILE}.part").exists()


def test_with_no_old_files_there_is_nothing_to_move(tmp_path):
    assert migration.migrate(tmp_path, TZ).skipped == "no old files to move"


def test_every_day_of_the_archive_comes_out_as_day_values_works_it_out(tmp_path, archived_cache):
    """The fake station's whole archive (88 days at 5 minutes, 363 at 30, 548 at 4 hours, 546 daily records): each day's figures from the new
    day rows are the old code's, from the same readings, and so is where each came from."""
    shutil.copy(archived_cache, tmp_path / "ecowitt_cache.sqlite")
    report = migration.migrate(tmp_path, TZ)
    assert report.ok, report.problem
    old = HistoryCache(tmp_path / "ecowitt_cache.sqlite", UNITS)
    today = local_date(int(time.time()), TZ)
    days = [today - timedelta(days=n) for n in range(2, 550)]
    names = ["temp_max", "temp_min", "temp_avg", "humidity_max", "humidity_min", "rain", "wind_gust", "pressure_max", "pressure_min", "uv_max"]
    values, source = day_values(old, MAC, TZ, names, days)
    old.close()
    s = Storage(tmp_path / FILE, TZ)
    try:
        sensor = {g: s.sensors.find(MAC, g).id for g in ("outdoor", "rainfall", "wind", "pressure", "solar_and_uvi")}
        read = {(kind, group): s.reader.days(kind, [sensor[group]], days[-1], days[0])[sensor[group]]
                for kind, group in (("climate", "outdoor"), ("rain", "rainfall"), ("wind", "wind"), ("pressure", "pressure"), ("light", "solar_and_uvi"))}
    finally:
        s.close()
    where = {"temp_max": ("climate", "outdoor", "temperature", "hi"), "temp_min": ("climate", "outdoor", "temperature", "lo"),
             "temp_avg": ("climate", "outdoor", "temperature", "mean"), "humidity_max": ("climate", "outdoor", "humidity", "hi"),
             "humidity_min": ("climate", "outdoor", "humidity", "lo"), "rain": ("rain", "rainfall", "daily", "hi"),
             "wind_gust": ("wind", "wind", "wind_gust", "hi"), "pressure_max": ("pressure", "pressure", "relative", "hi"),
             "pressure_min": ("pressure", "pressure", "relative", "lo"), "uv_max": ("light", "solar_and_uvi", "uvi", "hi")}
    compared = 0
    for name, (kind, group, column, part) in where.items():
        got = read[kind, group]
        new = dict(zip(got.day, getattr(got, part)[column]))
        assert set(new) == set(values[name]), name
        for day, figure in values[name].items():
            assert new[day] == pytest.approx(figure, abs=1e-9), (name, day)
            compared += 1
    outdoor = read["climate", "outdoor"]
    kinds = {"5min": "5min", "30min": "30min", "daily": "1day"}
    assert {d: kinds[s] for d, s in source.items()} == dict(zip(outdoor.day, outdoor.source))
    assert {"5min", "30min", "1day"} == set(outdoor.source) and compared > 5000
    assert {t for s, t in zip(outdoor.source, outdoor.timing) if s == "1day"} == {"4hour"}   # the daily records' times from the 4-hour readings


def test_the_script_moves_a_copy_and_times_the_reads(tmp_path, monkeypatch, capsys):
    from tests import bench_cache
    bench_cache.build(tmp_path / "data", days={"5min": 3, "30min": 9, "4hour": 30, "1day": 60}, air_days=3, live_days=1, minute_hours=2)
    before = {p.name: p.read_bytes() for p in (tmp_path / "data").glob("*.sqlite")}
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import migrate_storage
    monkeypatch.setattr(sys, "argv", ["migrate_storage.py", "--data", str(tmp_path / "data"), "--tz", "Australia/Melbourne",
                                      "--keep", str(tmp_path / "kept.sqlite")])
    import asyncio
    monkeypatch.setattr(asyncio, "to_thread", asyncio.to_thread)
    migrate_storage.main()
    out = capsys.readouterr().out
    assert "Moved the history to ecobot.sqlite" in out and "| Temperature line, 1Y (day rows) | 1 | 1 |" in out
    assert (tmp_path / "kept.sqlite").exists() and not (tmp_path / "data" / FILE).exists()
    assert {p.name: p.read_bytes() for p in (tmp_path / "data").glob("*.sqlite")} == before
