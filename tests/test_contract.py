"""What must not change however the code behind it is rebuilt: the shapes the model receives from every tool, and that
caches written by earlier versions (tests/data, generated once by the pre-recode code) still open and read the same."""

import json
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from lib.airgradient.store import AirStore
from lib.tools import Turn
from lib.ecowitt import api
from lib.ecowitt.store import HistoryCache
from tests.fakes import MAC, TZ, archived_station
from tests.test_compose import composer

DATA = Path(__file__).parent / "data"


def tables(path) -> dict[str, list[str]]:
    db = sqlite3.connect(path)
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {n: [r[1] for r in db.execute(f"PRAGMA table_info({n})")] for n in names}


def test_the_cache_schemas_are_unchanged():
    assert tables(DATA / "ecowitt_cache.sqlite") == {
        "coverage": ["mac", "cycle", "grp", "start", "end"],
        "fields": ["mac", "cycle", "grp", "field", "unit"],
        "meta": ["key", "value"],
        "points": ["mac", "cycle", "grp", "field", "ts", "value"]}
    assert tables(DATA / "airgradient_cache.sqlite") == {
        "days": ["loc", "day", "n"],
        "readings": ["loc", "ts", "pm2_5", "pm10", "pm1", "co2", "voc_index", "nox_index"]}


def test_a_cache_written_by_an_earlier_version_still_opens_and_reads(tmp_path):
    shutil.copy(DATA / "ecowitt_cache.sqlite", tmp_path / "eco.sqlite")
    shutil.copy(DATA / "airgradient_cache.sqlite", tmp_path / "air.sqlite")
    cache = HistoryCache(tmp_path / "eco.sqlite", api.UNITS)                       # the same units: nothing is cleared
    now = cache._query("SELECT MAX(ts) FROM points WHERE cycle = '30min'")[0][0]    # the fixture's own newest reading, so the test does not age
    for cycle, at_least in (("5min", 100), ("30min", 100), ("1day", 1)):
        temperature, = cache.slots(MAC, cycle, "outdoor", ["temperature"], now - 5 * 86400, now)
        assert len(temperature) >= at_least, cycle
    assert cache.days_held(MAC, "30min", ["outdoor"]) >= 3
    store = AirStore(tmp_path / "air.sqlite", "42", TZ)
    newest = store.db.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
    rows = store.load(newest - 5 * 86400, newest)
    assert len(rows) > 20 and {"ts", "pm2_5"} <= set(rows[0])
    cache.close()
    store.close()


async def test_every_tool_result_keeps_its_shape(tmp_path, archived_cache):
    eco, _ = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    day = lambda n: f"{today - timedelta(days=n)} 00:00:00"
    span = {"start_date": day(9), "end_date": f"{today - timedelta(days=2)} 23:59:59"}
    call = lambda i, args: eco.tools[i].handler(args)
    now = json.loads(await call(0, {"groups": "outdoor,indoor,pressure,wind,rainfall"}))
    assert set(now) - {"rain_outlook"} == {"time", "outdoor", "emoji"}                 # the outlook line only appears when the data suggests rain
    history = json.loads(await call(1, {"groups": "outdoor", **span}))
    assert set(history) - {"chart"} == {"period", "series"}                    # "chart": a hint that appears when one was drawn
    assert set(history["series"]["outdoor.temperature"]) == {
        "unit", "low", "low_time", "low_when", "low_date", "high", "high_time", "high_when", "high_date", "daily"}
    wind = json.loads(await call(1, {"groups": "wind", **span}))
    assert set(wind["series"]) == {"wind.wind_direction", "wind.wind_gust", "wind.wind_speed"}
    assert set(wind["series"]["wind.wind_direction"]) == {"unit", "most_common", "average_direction", "steadiness", "readings", "calm", "daily"}
    days = json.loads(await call(2, {"start_date": day(30), "end_date": f"{today - timedelta(days=2)} 23:59:59", "sort_by": "temp_max"}))
    assert set(days) == {"period", "days_checked", "matching_days", "units", "days"}
    assert set(days["days"][0]) == {"date", "temp_max", "temp_min", "rain", "source"}
    link = json.loads(await call(3, {"start_date": str(today - timedelta(days=20)), "end_date": str(today - timedelta(days=2))}))
    assert set(link) - {"chart"} == {"period", "driver", "resolution", "how_to_read", "slots", "rain_mm", "wet_slots", "verdict", "findings",
                         "by_change_before", "average_level", "correlation_change_vs_rain_next_3h", "rain_events", "days_with_data"}
    await eco.close()


async def test_the_cross_source_tools_keep_their_shape(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    turn = Turn()
    plot = json.loads(await comp.plot_chart({"panels": [{"series": "pm2_5"}, {"series": "rain"}]}, turn))
    link = json.loads(await comp.air_link({"chart": True}, turn))
    scan = json.loads(await comp.air_scan({"metric": "pm2_5", "chart": True}, turn))
    assert set(plot) == {"period", "panels", "chart"}
    assert {"period", "metric", "resolution", "how_to_read", "verdict", "findings", "events", "days_compared",
            "rank_correlation_daily_rain_vs_air", "slots", "wet_slots"} <= set(link)
    assert {"period", "resolution", "how_to_read", "verdict", "findings", "pairs_tested", "pairs_standing_out"} <= set(scan)
    await eco.close()
