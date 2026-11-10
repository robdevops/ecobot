"""scripts/bench_storage.py: the benchmark of typical requests on a copy of the bot's file."""
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tests.clocks import pin
from tests.fakes import TZ

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bench_storage  # noqa: E402


@pytest.mark.parametrize("when", [datetime(2026, 11, 10, 14, 5, tzinfo=TZ), datetime(2026, 11, 10, 23, 59, tzinfo=TZ)],
                         ids=["2:05pm", "11:59pm"])
async def test_the_benchmark_reads_a_copy_of_the_file_and_asks_nothing_of_the_network(tmp_path, pinned_archive, monkeypatch, when):
    """At 11:59pm its "Temperature chart, 3 hours" has the day row's figures but no readings in the window: summary's chart failed on
    it (a KeyError) until the reading was left out of the chart."""
    cache = pinned_archive(when)
    pin(monkeypatch, when)
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(cache, data / "ecobot.sqlite")
    before = (data / "ecobot.sqlite").read_bytes()
    copy = tmp_path / "copy"
    copy.mkdir()
    bench_storage.copy(data / "ecobot.sqlite", copy / "ecobot.sqlite")
    for name in ("TELEGRAM_BOT_TOKEN", "ECOWITT_API_KEY", "ECOWITT_APP_KEY"):   # (the benchmark sets them where they are missing)
        monkeypatch.setenv(name, "t")
    monkeypatch.setenv("DATA_DIR", str(copy))
    rows = await bench_storage.bench(copy, 1, TZ)
    labels = [r[0] for r in rows]
    assert "Temperature chart, 1Y" in labels and "/dashboard" in labels and len(rows) >= 12
    assert all(r[2] <= 1 for r in rows)
    assert (data / "ecobot.sqlite").read_bytes() == before                       # the bot's own file is never written


async def test_the_benchmark_s_own_cache_is_as_old_as_asked_and_its_short_chart_is_drawn_from_the_minutes(tmp_path, monkeypatch):
    from lib.ecowitt.api import GROUPS
    from lib.storage import Storage
    from tests import bench_cache
    from tests.fakes import MAC, TZ
    days = {"5min": 3, "30min": 9, "4hour": 30, "1day": 60}
    bench_cache.build(tmp_path, days=days, air_days=3, live_days=1, minute_hours=4)
    storage = Storage(tmp_path / "ecobot.sqlite", TZ)
    sensors = [storage.sensors.find(MAC, g) for g in GROUPS if storage.sensors.find(MAC, g)]
    held = {cycle: min(storage.coverage.days(s.id, cycle) for s in sensors) for cycle in days}
    storage.close()
    assert all(n - 2 <= held[cycle] <= n for cycle, n in days.items()), held   # (the newest hours are still settling)
    for name in ("TELEGRAM_BOT_TOKEN", "ECOWITT_API_KEY", "ECOWITT_APP_KEY"):
        monkeypatch.setenv(name, "t")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    rows = {r[0]: r for r in await bench_storage.bench(tmp_path, 1, TZ)}
    short = rows["Temperature chart, 3 hours"]
    assert short[2] == 0
    assert rows["Temperature chart, 1D"][2] == 1                                                            # its newest hour is settling
    assert "days: days PM2.5 over 25, 4 years" in rows
