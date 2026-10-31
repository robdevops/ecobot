import json
import math
import random
import time
from datetime import UTC, date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from lib.analysis import scan as correlate
from lib.timeutil import day_bounds
from lib.tools import Turn
from tests.test_compose import composer

ORIGIN = 1_700_000_000 // 1800 * 1800
DAYS = 90


def series(values):
    return {ORIGIN + i * 1800: float(v) for i, v in enumerate(values)}


def noise(rng, mean, sd, n):
    return [rng.gauss(mean, sd) for _ in range(n)]


def world(seed=0):
    """90 days of 30-minute slots: a daily cycle, smooth windy and calm spells, and readings that are pure noise."""
    rng = random.Random(seed)
    n = DAYS * 48
    slot = [i % 48 for i in range(n)]
    cycle = [math.sin(2 * math.pi * s / 48) for s in slot]
    gusty = [abs(v) for v in noise(rng, 15, 8, n)]
    wind = [sum(gusty[max(0, i - 6):i + 6]) / 12 for i in range(n)]    # a 6-hour running mean: spells, not noise
    return rng, n, slot, cycle, wind


def names(air, weather):
    return {k: (k, "µg/m³") for k in air}, {k: (k, "") for k in weather}


def test_a_planted_relationship_is_found_with_its_direction_and_nothing_else():
    rng, n, _, cycle, wind = world()
    air = {"pm2_5": series([20 - 0.5 * w + 3 * c + e for w, c, e in zip(wind, cycle, noise(rng, 0, 2, n))]),
           "co2": series([450 + 30 * c + e for c, e in zip(cycle, noise(rng, 0, 10, n))])}
    weather = {"wind": series(wind), "temperature": series([15 + 8 * c + e for c, e in zip(cycle, noise(rng, 0, 1, n))]),
               "noise": series(noise(rng, 0, 1, n))}
    result = correlate.scan(air, weather, None, None, ORIGIN, DAYS, UTC)
    kept = [(t["air"], t["weather"]) for t in result["tests"] if t["survives"]]
    assert kept == [("pm2_5", "wind")]                                   # the shared daily cycle and the noise are not links
    found = next(t for t in result["tests"] if t["weather"] == "wind" and t["air"] == "pm2_5")
    assert found["r_daily"] < -0.5 and found["r"] < -0.2 and found["p"] < 0.05
    out = correlate.summarise(result, *names(air, weather))
    assert out["verdict"] == "Yes - 1 relationship stands out" and "pm2_5 is lower when wind is higher" in out["findings"][0]
    assert out["strongest"]["weather"] == "wind"


def test_pure_noise_rarely_finds_anything_even_across_many_pairs():
    """18 pairs of noise at a time: something passes about as often as the 10% false discovery rate allows, no more."""
    found = 0
    for seed in range(10):
        rng, n, _, _, _ = world(100 + seed)
        air = {f"a{i}": series(noise(rng, 0, 1, n)) for i in range(3)}
        weather = {f"w{i}": series(noise(rng, 0, 1, n)) for i in range(6)}
        result = correlate.scan(air, weather, None, None, ORIGIN, DAYS, UTC)
        assert len(result["tests"]) == 18
        if any(t["survives"] for t in result["tests"]):
            found += 1
        else:
            out = correlate.summarise(result, *names(air, weather))
            assert out["verdict"].startswith("Nothing stands out") and out["findings"][0].startswith("Closest, probably chance")
    assert found <= 2


def test_unrelated_readings_that_each_wander_slowly_are_not_called_linked():
    """Two smooth, persistent series (as weather and air are) correlate by chance far more than noise does: the effective sample
    size must allow for it, or every slow drift would be reported as a link."""
    found = 0
    for seed in range(8):
        rng, n, _, _, _ = world(10 + seed)
        def drift():   # a spell of days at one level, then another, with the daily cycle and some noise on top
            level, out = 0.0, []
            for _ in range(DAYS):
                level = 0.8 * level + rng.gauss(0, 1)
                out += [level + math.sin(2 * math.pi * s / 48) + rng.gauss(0, 0.5) for s in range(48)]
            return out
        result = correlate.scan({"pm2_5": series(drift())}, {"pressure": series(drift())}, None, None, ORIGIN, DAYS, UTC)
        found += any(t["survives"] for t in result["tests"])
    assert found <= 2                                                     # about the 10% false discovery rate, not most of them


def test_wind_that_holds_for_days_beside_air_that_drifts_is_not_called_a_direction_effect():
    """Days of one wind and days of one air level line up by chance: counted half-hour by half-hour they look like a strong effect."""
    found = 0
    for seed in range(8):
        rng = random.Random(500 + seed)
        level, air, days, d = 0.0, [], [], rng.randrange(8)
        for _ in range(DAYS):
            level = 0.8 * level + rng.gauss(0, 1)
            air += [level + rng.gauss(0, 0.5) for _ in range(48)]
            d = d if rng.random() < 0.7 else rng.randrange(8)
            days.append(d * 45.0 + rng.uniform(-15, 15))
        direction = series([x for x in days for _ in range(48)])
        result = correlate.scan({"pm2_5": series(air)}, {"w": series(noise(rng, 0, 1, DAYS * 48))}, direction,
                                series([15.0] * DAYS * 48), ORIGIN, DAYS, UTC)
        found += any(t["survives"] for t in result["directions"])
    assert found <= 2


def test_a_lagged_relationship_is_found_at_its_lag():
    rng, n, _, cycle, wind = world(2)
    lagged = wind[-6:] + wind[:-6]                                      # the air answers the wind three hours later
    air = {"pm2_5": series([20 - 0.5 * w + e for w, e in zip(lagged, noise(rng, 0, 1, n))])}
    result = correlate.scan(air, {"wind": series(wind)}, None, None, ORIGIN, DAYS, UTC)
    test = result["tests"][0]
    assert test["survives"] and test["lag_hours"] == 3.0 and test["r"] < -0.3


def test_the_air_by_wind_direction_finds_the_sector_it_is_worst_from():
    rng, n, _, _, wind = world(3)
    direction = [45.0 if (i // 48) % 3 == 0 else 225.0 if (i // 48) % 3 == 1 else 135.0 for i in range(n)]   # NE, SW or SE days
    air = {"pm2_5": series([10 + 8 * (d == 45.0) + e for d, e in zip(direction, noise(rng, 0, 1, n))])}
    result = correlate.scan(air, {"wind": series(wind)}, series(direction), series([20.0] * n), ORIGIN, DAYS, UTC)
    d = result["directions"][0]
    assert d["survives"] and d["highest"] == "NE" and d["lowest"] in ("SW", "SE") and d["highest_by"] > 3
    out = correlate.summarise(result, *names(air, {"wind": 1}))
    assert any("highest with winds from the NE" in f for f in out["findings"])


def test_too_few_days_says_so():
    rng, n, _, _, wind = world(4)
    short = {t: v for t, v in series(noise(rng, 0, 1, n)).items() if t < ORIGIN + 10 * 86400}
    result = correlate.scan({"pm2_5": short}, {"wind": series(wind)}, None, None, ORIGIN, DAYS, UTC)
    assert result["tests"] == [] and "at least 21" in result["note"]
    assert correlate.summarise(result, {"pm2_5": ("PM2.5", "")}, {})["verdict"] == "Not enough data to say"


def test_the_helpers_rank_with_ties_allow_for_many_tests_and_word_strength():
    assert correlate.rank([3.0, 1.0, 1.0, None, 2.0]) == [4.0, 1.5, 1.5, None, 3.0]
    assert correlate.benjamini_hochberg([0.001, 0.02, 0.5, 0.8], 0.1) == [True, True, False, False]
    assert correlate.benjamini_hochberg([0.04, 0.3, 0.5, 0.9], 0.1) == [False, False, False, False]      # 0.04 alone is not enough of 4
    assert [correlate.strength_word(r) for r in (0.1, 0.3, -0.6)] == ["weak", "moderate", "strong"]


def test_the_formulas_match_their_tables():
    assert abs(correlate.p_value(0.3, 43) - 0.0491) < 0.002                    # r 0.3 from 43 pairs: just under 5%
    assert correlate.p_value(0.0, 100) == 1.0 and correlate.p_value(0.9, 3) == 1.0
    assert abs(correlate.chi2_sf(14.067, 7) - 0.05) < 1e-3 and abs(correlate.chi2_sf(3.841, 1) - 0.05) < 1e-3
    assert abs(correlate.chi2_sf(2.0, 7) - 0.9598) < 1e-3
    assert correlate.effective(1000, 0.9, 0.9) < 120 and correlate.effective(1000, 0.5, -0.5) == 1000
    assert abs(correlate.sidak(0.01, 13) - 0.1225) < 1e-3


def test_a_full_size_scan_of_every_pair_completes_quickly():
    rng, n, _, cycle, wind = world(5)
    air = {m: series([e + c for e, c in zip(noise(rng, 0, 1, n), cycle)]) for m in ("pm2_5", "pm10", "co2", "voc_index", "nox_index")}
    weather = {w: series([e + c for e, c in zip(noise(rng, 0, 1, n), cycle)])
               for w in ("temperature", "humidity", "dew_point", "pressure", "wind_speed", "wind_gust", "pressure_change", "rain")}
    started = time.time()
    result = correlate.scan(air, weather, series([rng.uniform(0, 360) for _ in range(n)]), series([20.0] * n), ORIGIN, DAYS, UTC)
    assert len(result["tests"]) == 40 and len(result["directions"]) == 5 and time.time() - started < 10


async def test_air_scan_answers_with_a_verdict_and_can_chart_the_strongest_pair(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    turn = Turn()
    out = json.loads(await comp.air_scan({"metric": "pm2_5", "chart": True}, turn))
    specs = turn.charts
    assert out["verdict"] and out["findings"] and out["pairs_tested"] >= 6 and "how_to_read" in out
    assert all(len(s.panels) == 2 or s.panels[0].bars for s in specs)    # two readings (rain behind the other): only when a relationship stood out
    assert "error" in json.loads(await comp.air_scan({"metric": "pm1"}))
    await eco.close()


def test_a_time_of_day_keeps_its_slot_across_a_daylight_saving_change():
    tz = ZoneInfo("Australia/Melbourne")
    for first in (date(2026, 4, 3), date(2026, 10, 2)):                 # 5 April gains an hour, 4 October loses one
        origin = day_bounds(first, tz)[0]
        nine = {int(datetime.combine(first + timedelta(days=d), dtime(9), tz).timestamp()): 1.0 for d in range(4)}
        grid = correlate.to_grid(nine, origin, 4, correlate.clock_slots(origin, 4, tz))
        assert [i for i, v in enumerate(grid) if v is not None] == [18 + 48 * d for d in range(4)]     # 9 am every day, not 8 or 10


def test_no_weather_readings_say_there_was_nothing_to_compare_not_that_nothing_stands_out():
    rng, n, _, _, _ = world(6)
    result = correlate.scan({"pm2_5": series(noise(rng, 0, 1, n))}, {}, None, None, ORIGIN, DAYS, UTC)
    assert result["tests"] == [] and "No weather readings" in result["note"]
    assert correlate.summarise(result, {"pm2_5": ("PM2.5", "")}, {})["verdict"] == "Not enough data to say"


async def test_a_scan_of_years_covers_its_latest_400_days_and_says_so(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    out = json.loads(await comp.air_scan({"start_date": "2020-01-01"}))
    first, last = (date.fromisoformat(d) for d in out["period"].split(" to "))
    assert (last - first).days == 399 and "Only the last 400 days" in out["notes"]["period"]
    await eco.close()
