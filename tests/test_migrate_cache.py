"""scripts/migrate_cache.py: the old Ecowitt cache layout (readings keyed by mac, cycle, group, field) becomes the series layout."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import migrate_cache  # noqa: E402
from lib.ecowitt import api  # noqa: E402
from lib.ecowitt.store import HistoryCache  # noqa: E402

ROWS = [("M", "5min", "outdoor", "temperature", 100 + 300 * i, f"{10 + i / 10:.1f}") for i in range(5)] + [
    ("M", "5min", "outdoor", "humidity", 100, "94"), ("M", "1day", "rainfall", "daily", 86400, "0.0"),
    ("M", "5min", "wind", "gust", 100, "-")]       # a field with no `fields` row, and a value that is not a number


def old_cache(path: Path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE points (mac TEXT, cycle TEXT, grp TEXT, field TEXT, ts INTEGER, value TEXT,
                             PRIMARY KEY (mac, cycle, grp, field, ts)) WITHOUT ROWID;
        CREATE TABLE fields (mac TEXT, cycle TEXT, grp TEXT, field TEXT, unit TEXT, PRIMARY KEY (mac, cycle, grp, field)) WITHOUT ROWID;
        CREATE TABLE coverage (mac TEXT, cycle TEXT, grp TEXT, start INTEGER, end INTEGER);""")
    db.executemany("INSERT INTO points VALUES (?,?,?,?,?,?)", ROWS)
    db.executemany("INSERT INTO fields VALUES (?,?,?,?,?)", [("M", "5min", "outdoor", "temperature", "℃"), ("M", "5min", "outdoor", "humidity", "%"),
                                                              ("M", "1day", "rainfall", "daily", "mm")])
    db.executemany("INSERT INTO coverage VALUES (?,?,?,?,?)", [("M", "5min", "outdoor", 0, 1000), ("M", "1day", "rainfall", 0, 200000)])
    db.commit()
    db.close()


def contents(path: Path):
    db = sqlite3.connect(path)
    points = sorted(db.execute("SELECT s.mac, s.cycle, s.grp, s.field, p.ts, p.value FROM points p JOIN series s ON s.id = p.series").fetchall())
    return points, sorted(db.execute("SELECT mac, cycle, grp, field, unit FROM series").fetchall()), sorted(db.execute("SELECT * FROM coverage").fetchall())


def test_every_value_string_unit_and_coverage_row_survives_and_the_original_is_kept(tmp_path):
    path = tmp_path / "eco.sqlite"
    old_cache(path)
    rows, series, before, after = migrate_cache.migrate(path)
    assert rows == len(ROWS) and series == 4
    points, units, coverage = contents(path)
    assert points == sorted(ROWS) and coverage == [("M", "1day", "rainfall", 0, 200000), ("M", "5min", "outdoor", 0, 1000)]
    assert ("M", "5min", "outdoor", "temperature", "℃") in units and ("M", "5min", "wind", "gust", "") in units       # the unmatched point gets a series
    assert (tmp_path / "eco.sqlite.old").exists()
    assert {r[0] for r in sqlite3.connect(tmp_path / "eco.sqlite.old").execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {"points", "fields"}
    cache = HistoryCache(path, {})                                                    # and the bot reads the result
    assert cache.load("M", "5min", ["outdoor"], 0, 10_000)["outdoor"]["humidity"] == {"unit": "%", "list": {"100": "94"}}
    cache.close()


def test_a_second_run_changes_nothing_and_no_backup_leaves_none(tmp_path):
    path = tmp_path / "eco.sqlite"
    old_cache(path)
    migrate_cache.migrate(path, backup=False)
    assert not (tmp_path / "eco.sqlite.old").exists()
    first = contents(path)
    assert migrate_cache.migrate(path)[0] == -1 and contents(path) == first and not (tmp_path / "eco.sqlite.old").exists()


def test_a_failure_part_way_leaves_the_original_tables(tmp_path, monkeypatch):
    path = tmp_path / "eco.sqlite"
    old_cache(path)
    real = sqlite3.connect

    class Failing(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("DROP TABLE points_old"):
                raise sqlite3.OperationalError("disk full")
            return super().execute(sql, *args)
    monkeypatch.setattr(migrate_cache.sqlite3, "connect", lambda *a, **k: real(*a, factory=Failing, **k))
    with pytest.raises(sqlite3.OperationalError):
        migrate_cache.migrate(path, backup=False)
    db = sqlite3.connect(path)
    assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {"points", "fields"}
    assert db.execute("SELECT COUNT(*) FROM points").fetchone()[0] == len(ROWS) and "mac" in [r[1] for r in db.execute("PRAGMA table_info(points)")]


def test_the_stored_unit_matches_what_the_bot_expects(tmp_path):
    path = tmp_path / "eco.sqlite"
    old_cache(path)
    migrate_cache.migrate(path, backup=False)
    HistoryCache(path, api.UNITS).close()                                              # the signature step runs on the converted file without error
