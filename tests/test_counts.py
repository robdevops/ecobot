"""The count of SQL statements and thread hops a request makes (lib/counts.py), and the benchmark that reports them."""
import asyncio
import shutil
import sqlite3
import sys
from pathlib import Path

from lib import counts
from lib.bot import chart_times

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bench_storage  # noqa: E402


async def test_a_request_counts_its_own_statements_and_hops_and_a_task_it_detaches_counts_nothing(monkeypatch):
    monkeypatch.setattr(asyncio, "to_thread", asyncio.to_thread)   # (put back after the test: install wraps it)
    counts.install()
    counts.install()                                                 # once, however often it is called
    db = counts.connect(":memory:")

    async def request():
        count = counts.start()
        await asyncio.to_thread(db.execute, "SELECT 1")
        await asyncio.to_thread(lambda: (db.execute("SELECT 2"), db.execute("SELECT 3")))

        async def background():
            counts.detach()
            await asyncio.to_thread(db.execute, "SELECT 4")
        await asyncio.create_task(background())
        return count
    count = await asyncio.create_task(request())
    assert (count.sql, count.hops) == (3, 2) and str(count) == "3 sql, 2 hops"
    db.execute("SELECT 5")                                           # outside any request: counted nowhere
    assert counts.CURRENT.get() is None
    db.close()


def test_the_timing_line_carries_the_count():
    line = chart_times({"data": 0.012, "draw": 0.098, "png": 0.041}, 0.015, [b"x" * 2048], 0.812, counts.Count(22, 17))
    assert line == "data 12, 22 sql, 17 hops, calc 3, draw 98, PNG 41 (2 KB), send 812"


async def test_the_benchmark_reads_a_copy_of_the_caches_and_asks_nothing_of_the_network(tmp_path, archived_cache, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(archived_cache, data / "ecowitt_cache.sqlite")
    before = (data / "ecowitt_cache.sqlite").read_bytes()
    copy = tmp_path / "copy"
    copy.mkdir()
    bench_storage.copy(data / "ecowitt_cache.sqlite", copy / "ecowitt_cache.sqlite")
    for name in ("TELEGRAM_BOT_TOKEN", "ECOWITT_API_KEY", "ECOWITT_APP_KEY"):   # (the benchmark sets them where they are missing)
        monkeypatch.setenv(name, "t")
    monkeypatch.setenv("DATA_DIR", str(copy))
    monkeypatch.setattr(asyncio, "to_thread", asyncio.to_thread)
    counts.install()
    from tests.fakes import TZ
    rows = await bench_storage.bench(copy, 1, TZ)
    labels = [r[0] for r in rows]
    assert "Temperature chart, 1Y" in labels and "/dashboard, first time in a day" in labels and len(rows) >= 12
    assert all(r[2].sql > 0 for r in rows if not r[0].startswith("weather_link")) and all(r[3] <= 1 for r in rows)
    assert (data / "ecowitt_cache.sqlite").read_bytes() == before                       # the bot's own file is never written


async def test_the_benchmark_s_own_cache_is_as_old_as_asked_and_its_short_chart_is_drawn_from_the_minutes(tmp_path, monkeypatch):
    from lib.ecowitt.api import GROUPS, UNITS
    from lib.ecowitt.store import HistoryCache
    from tests import bench_cache
    from tests.fakes import MAC, TZ
    days = {"5min": 3, "30min": 9, "4hour": 30, "1day": 60}
    bench_cache.build(tmp_path, days=days, air_days=3, live_days=1, minute_hours=4)
    cache = HistoryCache(tmp_path / "ecowitt_cache.sqlite", UNITS)
    held = {cycle: cache.days_held(MAC, cycle, GROUPS) for cycle in days}
    cache.close()
    assert all(n - 2 <= held[cycle] <= n for cycle, n in days.items()), held   # (the newest hours are still settling)
    for name in ("TELEGRAM_BOT_TOKEN", "ECOWITT_API_KEY", "ECOWITT_APP_KEY"):
        monkeypatch.setenv(name, "t")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(asyncio, "to_thread", asyncio.to_thread)
    counts.install()
    rows = {r[0]: r for r in await bench_storage.bench(tmp_path, 1, TZ)}
    assert rows["Temperature chart, 3 hours"][3] == 0 and rows["Temperature chart, 3 hours"][2].sql <= 3   # the minutes, in a read or two
    assert rows["Temperature chart, 1D"][3] == 1                                                            # its newest hour is settling
    assert "air_days: days PM2.5 over 25, 4 years" in rows
