"""Moving the old files into ecobot.sqlite (lib/storage/migrate.py): old-format files built here by the old stores themselves, and the fake
station's whole archive, whose every day must come out as ecowitt.days.day_values worked it out from the old cache before the day
rows (kept here as the reference)."""

import shutil
import sys
import time
from datetime import date, timedelta, tzinfo
from statistics import fmean
from pathlib import Path

import pytest

from lib.airgradient.store import AirStore
from lib.ecowitt.api import UNITS
from lib.ecowitt.api import FINE_CYCLES
from lib.ecowitt.days import FIELDS
from lib.ecowitt.store import HistoryCache
from lib.snapshots import Snapshots
from lib.storage import FILE, Storage
from lib.storage import migrate as migration
from lib.storage.catalogue import BOT, CLOUD
from lib.timeutil import day_bounds, local_date
from tests.fakes import MAC, TZ, old_live_file, old_minutes_file

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
    old_live_file(folder / "ecowitt_live.sqlite", {"outdoor.temperature": {start + 600: (3, 9.0, 13.0), start + 900: (1, 11.0, 11.0)}})
    old_minutes_file(folder / "ecowitt_minutes.sqlite", {"outdoor": {"temperature": {"unit": "℃", "list": {str(t): "12.5" for t in range(now - 3600, now, 60)}}}})
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
    assert report.extra == {"outdoor.lightning": 1} and report.odd == {"rainfall.daily": 1}   # no column for it; a value that is no number
    assert report.differ == {} and "rainfall.daily 1 ('-')" in "\n".join(report.lines())
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
        assert r.meta("ecowitt_signature") and r.meta("migrated.station") == "ecowitt_cache.sqlite, ecowitt_live.sqlite, ecowitt_minutes.sqlite"
        assert r.meta("migrated.airgradient") == "airgradient_cache.sqlite" and r.meta("migrated.snapshots") == "conditions_cache.sqlite"
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
    assert not report.ok and "holds" in report.problem and "tried again at the next start" in report.lines()[0]
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


# ---------- each day as the old code worked it out, from the old cache: what the day rows are checked against ----------
def day_figures(cache: HistoryCache, mac: str, cycle: str, names: list[str], days: set[date], tz: tzinfo, start: int,
                end: int) -> dict[str, dict[date, float]]:
    """{reading: {day: its figure}} for each of `days`, from the cached readings between start and end (epochs): the day's highest
    high, lowest low or mean. Each field is read once, however many readings use it (temp_max, temp_min and temp_avg are one),
    and folded into days as it is read, one at a time, so a long period of 5-minute readings is not held whole."""
    readers: dict[tuple[str, str], list[str]] = {}
    for name in names:
        readers.setdefault(FIELDS[name][:2], []).append(name)
    out: dict[str, dict[date, float]] = {}
    for (group, field), wanted in readers.items():
        value, high, low = cache.slots(mac, cycle, group, [field, f"{field}_high", f"{field}_low"], start, end)
        for name in wanted:
            kind = FIELDS[name][2]
            points = value if kind == "mean" else {**value, **(high if kind == "high" else low)}   # a slot's own high or low, if it has one
            figures: dict[date, list[float]] = {}
            for ts, v in points.items():
                if (day := local_date(ts, tz)) in days:
                    got = figures.setdefault(day, [])
                    if kind == "mean" or not got:
                        got.append(v)
                    elif v > got[0] if kind == "high" else v < got[0]:
                        got[0] = v
            out[name] = {d: fmean(v) if kind == "mean" else v[0] for d, v in figures.items()}
    return out


def day_values(cache: HistoryCache, mac: str, tz: tzinfo, shown: list[str], days) -> tuple[dict[str, dict[date, float]], dict[date, str]]:
    """({reading: {day: its figure}}, {day: where it came from}) for these days, each from the best data the cache holds: exact days
    from the 5- or 30-minute readings where every group needed has them, else the daily buckets (source "daily": approximate)."""
    groups = sorted({FIELDS[n][0] for n in shown})
    windows = [(cycle, d) for cycle in FINE_CYCLES for d in days]
    source: dict[date, str] = {}
    for (cycle, d), gaps in zip(windows, cache.uncovered(mac, groups, [(cycle, *day_bounds(d, tz, True)) for cycle, d in windows])):
        if d not in source and not any(gaps.values()):
            source[d] = cycle
    rest = {d for d in days if d not in source}

    values: dict[str, dict[date, float]] = {n: {} for n in shown}
    for cycle in FINE_CYCLES:
        mine = {d for d, c in source.items() if c == cycle}
        if mine:
            for n, figures in day_figures(cache, mac, cycle, shown, mine, tz, day_bounds(min(mine), tz, True)[0],
                                          day_bounds(max(mine), tz, True)[1]).items():
                values[n].update(figures)
    if rest:  # daily buckets: labelled with the local date their 10am start falls on
        for n, figures in day_figures(cache, mac, "1day", shown, rest, tz, day_bounds(min(rest), tz, True)[0] - 86400,
                                      day_bounds(max(rest), tz, True)[1] + 86400).items():
            values[n].update(figures)
        for d in rest:
            if any(d in values[n] for n in shown):
                source[d] = "daily"
    return values, source


def test_every_day_of_the_archive_comes_out_as_day_values_works_it_out(tmp_path, old_archived_cache):
    """The fake station's whole archive (90 days at 5 minutes, 365 at 30, 548 at 4 hours and daily records): each day's figures from the
    new day rows are the old code's, from the same readings, and so is where each came from."""
    shutil.copy(old_archived_cache, tmp_path / "ecowitt_cache.sqlite")
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


def test_a_part_moved_later_is_merged_into_the_file_the_bot_has_been_writing(tmp_path):
    """Step 2 moves the station; the AirGradient moves when its code does (step 3), into the file the station has been written to since."""
    now = int(time.time())
    held = old_files(tmp_path, now)
    assert migration.migrate(tmp_path, TZ, parts=("station",)).ok
    s = Storage(tmp_path / FILE, TZ)
    outdoor = s.sensors.find(MAC, "outdoor")
    assert s.air_sensor("42") is None and s.reader.meta("migrated.station") and s.reader.meta("migrated.airgradient") is None
    s.close()
    report = migration.migrate(tmp_path, TZ, parts=("station", "airgradient"))
    assert report.ok and report.moved["AirGradient readings"] and not report.moved["station readings"]   # (the station is not moved twice)
    s = Storage(tmp_path / FILE, TZ)
    try:
        air = s.air_sensor("42")
        assert s.sensors.find(MAC, "outdoor") == outdoor and air.id != outdoor.id
        assert s.write_db.execute("SELECT COUNT(*) FROM air WHERE sensor = ?", (air.id,)).fetchone()[0] == 24 + 289
        assert s.coverage.held(air.id, "5min", *day_bounds(held["day"], TZ, True)) and s.reader.meta("migrated.airgradient")
        assert s.write_db.execute("SELECT COUNT(*) FROM climate WHERE sensor = ? AND res = '5min'", (outdoor.id,)).fetchone()[0] == 288
    finally:
        s.close()
    assert migration.migrate(tmp_path, TZ, parts=("station", "airgradient")).skipped == f"{FILE} is there already"


def test_a_move_tried_again_after_one_that_failed_keeps_what_the_bot_wrote_meanwhile(tmp_path, monkeypatch, caplog):
    """A move that fails leaves no file; the bot goes on with a new one, and the next start moves the old history into it: the old readings
    fill in what it lacks, its own stay, a band takes in both, and the coverage and days are of both."""
    import logging
    now = int(time.time())
    held = old_files(tmp_path, now)
    start, d = held["start"], held["day"]
    files = migration.old_files(tmp_path)
    monkeypatch.setattr(migration, "_minutes", lambda *a: (_ for _ in ()).throw(migration.Mismatch("the minutes differ")))
    with caplog.at_level(logging.INFO, logger="lib.storage.migrate"):
        s = migration.open_storage(tmp_path / FILE, TZ, files)
    assert "Moving the history to ecobot.sqlite (once)" in caplog.text and "Couldn't move the history" in caplog.text
    outdoor = s.writer.run(lambda w: w.ecowitt_sensor(MAC, "outdoor"), ())
    mine = [{"ts": start, "temperature": 50.0}, {"ts": start + 300, "humidity": 1.0, "temperature_lo": -5.0, "temperature_hi": 99.0}]
    s.writer.run(lambda w: w.put(outdoor, "5min", mine), ())   # what the bot fetched meanwhile (no temperature at start + 300)
    s.writer.run(lambda w: w.cover(outdoor, "5min", start - DAY, start - 1), ())
    s.close()
    monkeypatch.undo()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lib.storage.migrate"):
        s = migration.open_storage(tmp_path / FILE, TZ, files)
    try:
        assert "Moved the history to ecobot.sqlite" in caplog.text and s.reader.meta("migrated.station")
        five = s.reader.rows("climate", [outdoor.id], "5min", start, start + 600)[outdoor.id]
        assert five.values["temperature"][:3] == [50.0, 10.1, 10.2]               # its own reading stays; the old ones fill in the rest
        assert five.values["humidity"][:2] == [80.0, 1.0] and (five.lo["temperature"][1], five.hi["temperature"][1]) == (-5.0, 99.0)
        assert s.coverage.held(outdoor.id, "5min", start - DAY, held["end"])          # the coverage of both
        day = s.reader.days("climate", [outdoor.id], d, d)[outdoor.id]
        assert day.hi["temperature"] == [99.0] and day.lo["temperature"] == [-5.0]       # its days worked out again
        assert s.reader.rows("climate", [outdoor.id], "1min", now - 3600, now)[outdoor.id].ts   # the minutes, this time
    finally:
        s.close()
