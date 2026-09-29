"""scripts/check_rain.py: reports honestly on healthy data, and catches rain totals that are averages."""

import sys
from datetime import datetime
from pathlib import Path

import pytest

from lib.ecowitt import Archive, Ecowitt, archive as archive_mod
from tests.fakes import config, ecowitt_transport

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_rain  # noqa: E402


@pytest.fixture
async def station(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    await Archive(eco).run_once()
    yield eco
    await eco.close()


def run(eco):
    return check_rain.compare_rain(eco.cache, eco.mac, eco.tz, datetime.now(eco.tz).date(), 60)


async def test_healthy_data_is_reported_as_matching(station, capsys):
    res = run(station)
    assert res["days"] == 60 and "daily_high" in res["fields"]["30min"] and "daily" in res["fields"]["5min"]
    thirty = res["against"]["30min"]
    assert thirty["rainy_days"] > 5 and thirty["lower"] == 0 and thirty["missed_rainy"] == 0
    check_rain.report(res)
    out = capsys.readouterr().out
    assert "30-minute rain totals match the 5-minute ones" in out and "Compared 60 day(s)" in out


def average_the_thirty_minute_rain(eco, factor):
    """What averages look like: no daily_high field, and values a fraction of the real total."""
    with eco.cache._lock:
        eco.cache.db.execute("DELETE FROM points WHERE cycle='30min' AND grp='rainfall' AND field IN ('daily_high','daily_low')")
        eco.cache.db.execute("UPDATE points SET value = CAST(value AS REAL) * ? WHERE cycle='30min' AND grp='rainfall' AND field='daily'",
                             (factor,))
        eco.cache.db.commit()


async def test_thirty_minute_totals_that_are_averages_are_caught(station, capsys):
    average_the_thirty_minute_rain(station, 0.6)
    thirty = run(station)["against"]["30min"]
    assert thirty["lower"] == thirty["rainy_days"] > 5 and 0.55 < thirty["median_ratio"] < 0.65
    assert thirty["worst"] and thirty["worst"][0][1] < thirty["worst"][0][0]
    assert thirty["missed_rainy"] == 0      # a 5 mm day still reads 3 mm: understated, but not flipped
    check_rain.report(run(station))
    out = capsys.readouterr().out
    assert "LOWER than the real total" in out and "'exact' label is too generous" in out


async def test_days_that_fall_under_the_line_are_counted_as_missed(station):
    """The failure the review described: a rainy day whose averaged total reads under 1 mm is not listed."""
    baseline = run(station)["against"]["30min"]
    assert baseline["missed_rainy"] == 0
    average_the_thirty_minute_rain(station, 0.15)    # 5 mm -> 0.75 mm
    thirty = run(station)["against"]["30min"]
    assert 0 < thirty["missed_rainy"] < thirty["rainy_days"]   # the wet days (5 mm) flip; the 0.5 mm traces were never listed
    assert thirty["invented_rainy"] == 0
