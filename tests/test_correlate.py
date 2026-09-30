import json
import time

import numpy as np

from lib import correlate
from lib.charts import CHART_REQUESTS
from tests.fakes import TZ
from tests.test_compose import composer

ORIGIN = 1_700_000_000 // 1800 * 1800
DAYS = 90


def series(values):
    return {ORIGIN + i * 1800: float(v) for i, v in enumerate(values)}


def world(seed=0):
    """90 days of 30-minute slots: a daily cycle, smooth windy and calm spells, and readings that are pure noise."""
    rng = np.random.default_rng(seed)
    n = DAYS * 48
    slot = np.arange(n) % 48
    cycle = np.sin(2 * np.pi * slot / 48)
    wind = np.convolve(np.abs(rng.normal(15, 8, n)), np.ones(12) / 12, mode="same")
    return rng, n, slot, cycle, wind


def names(air, weather):
    return {k: (k, "µg/m³") for k in air}, {k: (k, "") for k in weather}


def test_a_planted_relationship_is_found_with_its_direction_and_nothing_else():
    rng, n, _, cycle, wind = world()
    air = {"pm2_5": series(20 - 0.5 * wind + 3 * cycle + rng.normal(0, 2, n)), "co2": series(450 + 30 * cycle + rng.normal(0, 10, n))}
    weather = {"wind": series(wind), "temperature": series(15 + 8 * cycle + rng.normal(0, 1, n)), "noise": series(rng.normal(0, 1, n))}
    result = correlate.scan(air, weather, None, None, ORIGIN, DAYS)
    kept = [(t["air"], t["weather"]) for t in result["tests"] if t["survives"]]
    assert kept == [("pm2_5", "wind")]                                   # the shared daily cycle and the noise are not links
    found = next(t for t in result["tests"] if t["weather"] == "wind" and t["air"] == "pm2_5")
    assert found["r_daily"] < -0.5 and found["r"] < -0.2 and found["p"] < 0.05
    out = correlate.summarise(result, *names(air, weather))
    assert out["verdict"] == "Yes - 1 relationship stands out" and "pm2_5 is lower when wind is higher" in out["findings"][0]
    assert out["strongest"]["weather"] == "wind"


def test_pure_noise_finds_nothing_even_across_many_pairs():
    rng, n, _, _, _ = world(1)
    air = {f"a{i}": series(rng.normal(0, 1, n)) for i in range(3)}
    weather = {f"w{i}": series(rng.normal(0, 1, n)) for i in range(6)}
    result = correlate.scan(air, weather, None, None, ORIGIN, DAYS, shuffles=100)
    assert not any(t["survives"] for t in result["tests"]) and len(result["tests"]) == 18
    out = correlate.summarise(result, *names(air, weather))
    assert out["verdict"].startswith("Nothing stands out") and out["findings"][0].startswith("Closest, probably chance")


def test_a_lagged_relationship_is_found_at_its_lag():
    rng, n, _, cycle, wind = world(2)
    lagged = np.roll(wind, 6)                                           # the air answers the wind three hours later
    air = {"pm2_5": series(20 - 0.5 * lagged + rng.normal(0, 1, n))}
    result = correlate.scan(air, {"wind": series(wind)}, None, None, ORIGIN, DAYS)
    test = result["tests"][0]
    assert test["survives"] and test["lag_hours"] == 3.0 and test["r"] < -0.3


def test_the_air_by_wind_direction_finds_the_sector_it_is_worst_from():
    rng, n, _, _, wind = world(3)
    days = np.arange(n) // 48
    direction = np.where(days % 3 == 0, 45.0, np.where(days % 3 == 1, 225.0, 135.0))    # north-east, south-west or south-east days
    air = {"pm2_5": series(10 + 8 * (direction == 45.0) + rng.normal(0, 1, n))}
    result = correlate.scan(air, {"wind": series(wind)}, series(direction), series(np.full(n, 20.0)), ORIGIN, DAYS)
    d = result["directions"][0]
    assert d["survives"] and d["highest"] == "NE" and d["lowest"] in ("SW", "SE") and d["highest_by"] > 3
    out = correlate.summarise(result, *names(air, {"wind": 1}))
    assert any("highest with winds from the NE" in f for f in out["findings"])


def test_too_few_days_says_so():
    rng, n, _, _, wind = world(4)
    short = {t: v for t, v in series(rng.normal(0, 1, n)).items() if t < ORIGIN + 10 * 86400}
    result = correlate.scan({"pm2_5": short}, {"wind": series(wind)}, None, None, ORIGIN, DAYS)
    assert result["tests"] == [] and "at least 21" in result["note"]
    assert correlate.summarise(result, {"pm2_5": ("PM2.5", "")}, {})["verdict"] == "Not enough data to say"


def test_the_helpers_rank_with_ties_allow_for_many_tests_and_word_strength():
    assert list(correlate.rank(np.array([3.0, 1.0, 1.0, np.nan, 2.0]))[[0, 1, 2, 4]]) == [4.0, 1.5, 1.5, 3.0]
    assert correlate.benjamini_hochberg([0.001, 0.02, 0.5, 0.8], 0.1) == [True, True, False, False]
    assert correlate.benjamini_hochberg([0.04, 0.3, 0.5, 0.9], 0.1) == [False, False, False, False]      # 0.04 alone is not enough of 4
    assert [correlate.strength_word(r) for r in (0.1, 0.3, -0.6)] == ["weak", "moderate", "strong"]


def test_a_full_size_scan_of_every_pair_completes():
    rng, n, _, cycle, wind = world(5)
    air = {m: series(rng.normal(0, 1, n) + cycle) for m in ("pm2_5", "pm10", "co2", "voc_index", "nox_index")}
    weather = {w: series(rng.normal(0, 1, n) + cycle) for w in ("temperature", "humidity", "dew_point", "pressure", "wind_speed", "wind_gust", "pressure_change", "rain")}
    started = time.time()
    result = correlate.scan(air, weather, series(rng.uniform(0, 360, n)), series(np.full(n, 20.0)), ORIGIN, DAYS, shuffles=60)
    assert len(result["tests"]) == 40 and len(result["directions"]) == 5 and time.time() - started < 30


async def test_air_scan_answers_with_a_verdict_and_can_chart_the_strongest_pair(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    token = CHART_REQUESTS.set([])
    try:
        out = json.loads(await comp.air_scan({"metric": "pm2_5", "chart": True}))
        specs = CHART_REQUESTS.get()
    finally:
        CHART_REQUESTS.reset(token)
    assert out["verdict"] and out["findings"] and out["pairs_tested"] >= 6 and "how_to_read" in out
    assert all(len(s["panels"]) == 2 for s in specs)                     # a chart only when a relationship stood out
    assert "error" in json.loads(await comp.air_scan({"metric": "pm1"}))
    await eco.close()
