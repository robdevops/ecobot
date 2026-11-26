from datetime import datetime, time as dtime, timedelta

import httpx
import pytest

from lib.forecast import Forecast
from lib.forecast import source
from lib.forecast.source import FLOOR_LADDER, SPREAD, WMO, amount_text, day_label, describe_day, floor_mm, quantile, rounded_mm, temps
from tests.fakes import TZ, config
from tests.test_pollen import started


def ensemble_body(per_day):
    """The ensemble's hourly rain: per_day = {day offset: [each member's daily total in mm, or None for a member with no data]};
    a member's total is put in the first hour of the day, the other 23 hours are dry."""
    today = datetime.now(TZ).date()
    members = max(len(totals) for totals in per_day.values())
    times, series = [], [[] for _ in range(members)]
    for offset, totals in sorted(per_day.items()):
        for hour in range(24):
            times.append(f"{today + timedelta(days=offset)}T{hour:02d}:00")
            for m in range(members):
                total = totals[m] if m < len(totals) else None
                series[m].append(None if total is None else (total if hour == 0 else 0.0))
    return {"hourly": {"time": times, "precipitation": series[0], **{f"precipitation_member{m:02d}": s for m, s in enumerate(series[1:], 1)}}}


ENSEMBLE_DAYS = {0: [0.0, 0.4, 2.0, 5.0],       # 2 of 4 members at 1 mm or more: 50%
                 1: [0.0, 0.0, 0.0, 0.2],       # none: 0%
                 2: [3.0, 4.0, 2.0, 0.9]}       # 3 of 4: 75%


def transport(calls, ok=True, ensemble=ENSEMBLE_DAYS):
    """Open-Meteo: three days from today; its ensemble answers with `ensemble` (None: it is down)."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if not ok or (request.url.host.startswith("ensemble") and ensemble is None):
            return httpx.Response(503)
        if request.url.host.startswith("ensemble"):
            calls.append(dict(request.url.params))
            return httpx.Response(200, json=ensemble_body(ensemble))
        today = datetime.now(TZ).date()
        return httpx.Response(200, json={"daily": {
            "time": [str(today + timedelta(days=n)) for n in range(3)], "temperature_2m_min": [10.5, 12, 9],
            "temperature_2m_max": [16, 19.5, 14.2], "precipitation_probability_max": [40, 0, 100], "weather_code": [63, 0, 95]}})
    return httpx.MockTransport(handler)


def make(tmp_path, ok=True, hour=12, ensemble=ENSEMBLE_DAYS, **over):
    calls = []
    forecast = Forecast(config(tmp_path, forecast=True, forecast_lat=-37.8, forecast_lon=144.9, **over), None, transport(calls, ok, ensemble))
    forecast.now = lambda: datetime.combine(datetime.now(TZ).date(), dtime(hour, 0))
    return forecast, calls


def test_every_summary_is_an_emoji_and_a_phrase_ending_in_a_full_stop():
    import re
    assert all(re.fullmatch(r"\S+ [A-Z][a-z ]+\.", summary) for summary in WMO.values()), WMO
    assert WMO[0] == "☀️ Clear." and WMO[63] == "🌧️ Rain." and WMO[80] == "🌦️ Showers." and WMO[95] == "⛈️ Thunderstorm."


def test_a_day_reads_like_the_report_line():
    assert describe_day({"summary": "🌧️ Rain.", "min_c": 11, "max_c": 17, "rain_chance_pct": 90}) == "🌧️ Rain. 11–17°C, 90% chance of rain"
    assert describe_day({"summary": "⛈️ Thunderstorm and hail.", "min_c": 11, "max_c": 18, "rain_chance_pct": 95}) == \
        "⛈️ Thunderstorm and hail. 11–18°C, 95% chance of rain"
    assert describe_day({"summary": "☀️ Clear.", "min_c": 9.5, "max_c": None, "rain_chance_pct": None}) == "☀️ Clear. min 10°C"
    assert describe_day({}) == "no data"


def test_temperatures_are_whole_degrees_with_halves_rounded_up():
    assert temps({"min_c": 10.5, "max_c": 19.5}) == "11–20°C" and temps({"min_c": -1.5, "max_c": -0.5}) == "-1–0°C"
    assert temps({"min_c": 12.4, "max_c": 16.6}) == "12–17°C" and temps({"max_c": 20.5}) == "max 21°C" and temps({}) is None


def test_days_after_today_are_named_by_their_weekday():
    today = datetime(2026, 10, 1).date()               # a Thursday
    assert [day_label(today + timedelta(days=n), today) for n in range(4)] == ["Today", "Friday", "Saturday", "Sunday"]


async def test_today_and_the_next_days_come_from_open_meteo_by_local_date(tmp_path):
    forecast, calls = make(tmp_path)
    await started(forecast)
    today = datetime.now(TZ).date()
    assert forecast.lines() == ["Today: 🌧️ Rain. 11–16°C, 50% chance of at least 1 mm",
                                f"{today + timedelta(days=1):%A}: ☀️ Clear. 12–20°C, 0% chance of at least 1 mm"]
    lines = forecast.lines(3)
    assert len(lines) == 3 and lines[2] == f"{today + timedelta(days=2):%A}: ⛈️ Thunderstorm. 9–14°C, 75% chance of at least 1 mm"
    assert forecast.place == "Melbourne" and "Tomorrow" not in " ".join(lines)
    assert {c for c in calls if isinstance(c, str)} == {"api.open-meteo.com", "ensemble-api.open-meteo.com"}
    await forecast.close()


async def test_the_place_is_the_configured_name(tmp_path):
    forecast, _ = make(tmp_path, place="Geelong")
    await started(forecast)
    assert forecast.place == "Geelong"
    assert forecast.fetched_at                                                           # when it was fetched, for the text's heading
    await forecast.close()


async def test_the_forecast_is_fetched_only_in_the_day_and_not_again_while_young(tmp_path):
    forecast, calls = make(tmp_path, hour=22)
    await started(forecast)
    n = len(calls)
    assert n > 0
    await forecast.warm(True)
    await forecast.warm(False)
    assert len(calls) == n                                  # night: the cached forecast is used
    forecast.now = lambda: datetime.combine(datetime.now(TZ).date(), dtime(9, 0))
    await forecast.warm(False)
    assert len(calls) == n                                  # young enough for a question
    await forecast.warm(True)
    assert len(calls) > n                                   # the hourly timer
    await forecast.close()


async def test_the_station_location_is_used_when_none_is_configured_and_without_one_the_source_does_not_start(tmp_path, caplog):
    cfg = config(tmp_path, forecast=True)
    assert Forecast(cfg, (-37.8, 144.9)).location == (-37.8, 144.9)
    half = config(tmp_path, forecast=True, forecast_lat=-33.9)                       # one half alone is not a place: not paired with the station's
    assert Forecast(half, (-37.8, 144.9)).location == (-37.8, 144.9) and "MCP_FORECAST_LAT and MCP_FORECAST_LON go together" in caplog.text
    assert Forecast(config(tmp_path, forecast=True, forecast_lat=-33.9, forecast_lon=151.2), (-37.8, 144.9)).location == (-33.9, 151.2)
    with pytest.raises(RuntimeError, match="MCP_FORECAST_LAT"):
        await Forecast(cfg).start()


async def test_the_lines_are_every_day_open_meteo_gave_unless_fewer_are_asked_for(tmp_path):
    forecast, _ = make(tmp_path)
    await started(forecast)
    assert len(forecast.lines(7)) == 3                                                   # every day Open-Meteo gave (the test data has three)
    assert len(forecast.lines(2)) == 2
    assert len(forecast.lines(30)) == 3
    await forecast.close()


async def test_the_chance_of_at_least_1_mm_is_the_share_of_ensemble_members_to_the_nearest_5_and_the_request_is_for_the_ensemble(tmp_path):
    odd = {0: [0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0] + [1.0] * 4,    # 6 of 12: 50
           1: [1.0] + [0.0] * 6,                                        # 1 of 7: 14.3 -> 15
           2: [None, None, 5.0, 5.0]}                                   # members with no data are not counted: 2 of 2 -> 100
    forecast, calls = make(tmp_path, ensemble=odd)
    await started(forecast)
    assert [d["rain_1mm_pct"] for d in forecast.days] == [50, 15, 100]
    asked = next(c for c in calls if isinstance(c, dict))
    assert asked["hourly"] == "precipitation" and asked["models"] == "ecmwf_ifs025" and asked["latitude"] == "-37.8"
    await forecast.close()


async def test_a_day_the_ensemble_has_too_few_hours_for_keeps_open_meteos_own_figure(tmp_path):
    forecast, _ = make(tmp_path, ensemble={0: [0.0, 2.0]})               # only today; the other two days have no members
    await started(forecast)
    today, tomorrow = forecast.days[0], forecast.days[1]
    assert today["rain_1mm_pct"] == 50 and "rain_1mm_pct" not in tomorrow
    assert describe_day(tomorrow).endswith("0% chance of rain")
    await forecast.close()


async def test_when_the_ensemble_is_down_the_forecast_still_works_with_open_meteos_own_figure(tmp_path):
    forecast, _ = make(tmp_path, ensemble=None)
    await started(forecast)
    assert forecast.lines()[0] == "Today: 🌧️ Rain. 11–16°C, 40% chance of rain"
    await forecast.close()


SHOWERS = {"summary": "🌦️ Showers.", "min_c": 19, "max_c": 23}


def test_a_heavy_day_gives_its_total_called_heavy_with_the_chance():
    assert describe_day({**SHOWERS, "rain_mm": 40.3, "rain_1mm_pct": 100, "rain_chance_pct": 100}) == \
        "🌦️ Showers. 19–23°C, heavy: about 40 mm (100% chance of at least 1 mm)"   # (no spread: no floor)


def test_a_showery_day_gives_its_total_to_the_half_millimetre():
    assert describe_day({**SHOWERS, "rain_mm": 3.2, "rain_1mm_pct": 80}) == "🌦️ Showers. 19–23°C, about 3 mm (80% chance of at least 1 mm)"
    assert amount_text({"rain_mm": 3.3}) == "about 3.5 mm"


def test_a_drizzle_day_is_under_1_mm_and_a_dry_day_has_no_amount():
    assert describe_day({**SHOWERS, "rain_mm": 0.6, "rain_1mm_pct": 10}) == "🌦️ Showers. 19–23°C, under 1 mm (10% chance of at least 1 mm)"
    assert describe_day({**SHOWERS, "rain_mm": 0.6, "rain_1mm_pct": 0}) == "🌦️ Showers. 19–23°C, 0% chance of at least 1 mm"
    assert describe_day({**SHOWERS, "rain_mm": 0.0, "rain_1mm_pct": 0}) == "🌦️ Showers. 19–23°C, 0% chance of at least 1 mm"
    assert describe_day({**SHOWERS, "rain_mm": 0.1, "rain_1mm_pct": 5}) == "🌦️ Showers. 19–23°C, 5% chance of at least 1 mm"


def test_a_day_without_a_total_keeps_the_old_wording():
    assert describe_day({**SHOWERS, "rain_1mm_pct": 100}) == "🌦️ Showers. 19–23°C, 100% chance of at least 1 mm"
    assert describe_day({**SHOWERS, "rain_mm": None, "rain_chance_pct": 60}) == "🌦️ Showers. 19–23°C, 60% chance of rain"


def test_a_total_with_no_chance_stands_alone():
    assert describe_day({**SHOWERS, "rain_mm": 12.4}) == "🌦️ Showers. 19–23°C, about 12 mm"


def test_the_ensemble_spread_is_a_range_when_it_is_wider_than_the_rounding():
    day = {**SHOWERS, "rain_mm": 6, "rain_mm_lo": 0.2, "rain_mm_hi": 14, "rain_1mm_pct": 90}
    assert describe_day(day) == "🌦️ Showers. 19–23°C, about 6 mm, likely 0–14 mm (90% chance of at least 1 mm)"
    assert amount_text({"rain_mm": 8, "rain_mm_lo": 7.6, "rain_mm_hi": 8.4}) == "about 8 mm"                 # the same once rounded
    assert amount_text({"rain_mm": 6, "rain_mm_lo": 0.2, "rain_mm_hi": 14}) == "about 6 mm, likely 0–14 mm"
    assert amount_text({"rain_mm": 50, "rain_mm_lo": 2, "rain_mm_hi": 30}) == "heavy: about 50 mm, likely 2–50 mm"   # the total is inside it


def test_rain_is_rounded_to_half_a_millimetre_under_5_a_millimetre_under_20_and_5_mm_from_20():
    assert [rounded_mm(x) for x in (1.24, 1.25, 4.74, 4.75, 5.4, 5.5, 19.4, 19.5, 22.4, 22.5, 41, 47.6)] == \
        [1.0, 1.5, 4.5, 5.0, 5.0, 6.0, 19.0, 20.0, 20.0, 25.0, 40, 50]
    assert [amount_text({"rain_mm": x}) for x in (0.99, 1, 29.9, 27.4)] == ["under 1 mm", "about 1 mm", "heavy: about 30 mm", "about 25 mm"]


def test_the_quantile_is_interpolated():
    assert quantile([0, 10], 0.5) == 5 and quantile([3], 0.9) == 3 and quantile([4, 0, 2, 8, 6], 0.25) == 2


async def test_the_total_and_the_spread_are_kept_from_open_meteo_and_the_ensemble(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        today = datetime.now(TZ).date()
        if request.url.host.startswith("ensemble"):
            return httpx.Response(200, json=ensemble_body({0: [10.0, 20.0, 30.0, 60.0, 40.0]}))
        calls.append(request.url.params["daily"])
        return httpx.Response(200, json={"daily": {"time": [str(today)], "temperature_2m_min": [19], "temperature_2m_max": [23],
                                                   "precipitation_probability_max": [90], "precipitation_sum": [40.3], "weather_code": [80]}})
    forecast = Forecast(config(tmp_path, forecast=True, forecast_lat=-37.8, forecast_lon=144.9), None, httpx.MockTransport(handler))
    forecast.now = lambda: datetime.combine(datetime.now(TZ).date(), dtime(12, 0))
    await started(forecast)
    assert "precipitation_sum" in calls[0]
    assert forecast.lines(1)[0].endswith("🌦️ Showers. 19–23°C, heavy: about 40 mm, at least 10 mm very likely (90%)")
    await forecast.close()


def lo_hi(mm, lo, hi, pct=100):
    return {**SHOWERS, "rain_mm": mm, "rain_mm_lo": lo, "rain_mm_hi": hi, "rain_1mm_pct": pct}


def test_a_heavy_day_states_a_floor_in_place_of_the_range_and_the_chance():
    line = describe_day(lo_hi(40.3, 15.2, 62))
    assert line == "🌦️ Showers. 19–23°C, heavy: about 40 mm, at least 15 mm very likely (90%)"
    assert "chance of at least 1 mm" not in line and "likely 1" not in line.replace("very likely", "")
    assert amount_text(lo_hi(95, 52, 130)) == "heavy: about 95 mm, at least 50 mm very likely (90%)"
    assert describe_day(lo_hi(12, 6.4, 20)).endswith("about 12 mm, at least 5 mm very likely (90%)")
    assert floor_mm(lo_hi(40.3, 15.2, 62)) == 15 and floor_mm(lo_hi(12, 6.4, 20)) == 5


def test_a_showery_day_and_a_day_without_spread_or_ensemble_keep_the_old_wording():
    assert describe_day(lo_hi(3.2, 1.0, 3.5, 80)) == "🌦️ Showers. 19–23°C, about 3 mm, likely 1–3.5 mm (80% chance of at least 1 mm)"
    assert floor_mm(lo_hi(3.2, 1.0, 3.5, 80)) is None and floor_mm(lo_hi(3.2, 1.9, 3.5, 80)) is None
    assert floor_mm({**SHOWERS, "rain_mm": 40.3, "rain_1mm_pct": 100}) is None                       # no spread
    assert floor_mm({**SHOWERS, "rain_mm": 40.3, "rain_mm_lo": 15.2, "rain_mm_hi": 62}) is None      # no ensemble chance
    assert floor_mm(lo_hi(None, 15, 62)) is None and floor_mm(lo_hi(0.0, 0, 0, 0)) is None
    assert describe_day(lo_hi(0.6, 0, 1, 10)).endswith("under 1 mm (10% chance of at least 1 mm)")


def test_the_floor_never_reaches_the_rounded_total():
    assert floor_mm(lo_hi(5, 5.2, 9)) is None                 # the floor would equal the total
    assert floor_mm(lo_hi(4.6, 5.2, 9)) is None               # (lo above the total: the total stays)
    assert floor_mm(lo_hi(6, 5.2, 9)) == 5
    assert describe_day(lo_hi(5, 5.2, 9)).endswith("about 5 mm, likely 5–9 mm (100% chance of at least 1 mm)")


def test_the_floor_is_the_ladder_step_at_or_below_the_quantile():
    table = [(1.9, None), (2, 2), (2.9, 2), (3, 3), (4.9, 3), (5, 5), (9.9, 5), (10, 10), (14.9, 10), (15.2, 15), (19.9, 15), (20, 20),
             (29.9, 25), (30, 30), (45, 40), (52, 50), (80, 75), (120, 100), (250, 100)]
    assert FLOOR_LADDER == (2, 3, 5, 10, 15, 20, 25, 30, 40, 50, 75, 100)
    for lo, want in table:
        assert floor_mm(lo_hi(500, lo, 600)) == want, lo


def test_a_floor_above_1_mm_comes_with_at_least_90_percent_of_the_runs_at_1_mm():
    """A 10th percentile above 1 mm puts 90% or more of the members at 1 mm or more: the ensemble's chance is then 90% or 95% or 100%."""
    members = [2.1, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 8.0, 9.0, 12.0, 20.0]
    lo = quantile(members, SPREAD[0])
    chance = 5 * round(100 * sum(m >= 1.0 for m in members) / len(members) / 5)
    assert lo > 1 and chance >= 100 * (1 - SPREAD[0])
    day = lo_hi(12, lo, 20, chance)
    assert floor_mm(day) == 2 and "chance of at least 1 mm" not in describe_day(day)


def test_the_percentage_in_the_floor_follows_the_spread(monkeypatch):
    monkeypatch.setattr(source, "SPREAD", (0.2, 0.8))
    assert describe_day(lo_hi(40.3, 15.2, 62)) == "🌦️ Showers. 19–23°C, heavy: about 40 mm, at least 15 mm likely (80%)"
    monkeypatch.setattr(source, "SPREAD", (0.05, 0.95))
    assert describe_day(lo_hi(40.3, 15.2, 62)).endswith("at least 15 mm very likely (95%)")
