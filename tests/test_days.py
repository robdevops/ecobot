"""weather_days: rank, filter and count days from the cache, checked against answers computed
independently from the fake station's own functions."""

import json
from datetime import datetime, timedelta

import pytest

from lib.ecowitt import Ecowitt
from tests.fakes import TZ, archived_station, config, ecowitt_transport, rain_day, temp

pytestmark = pytest.mark.asyncio


def local_day(d):
    return int(datetime.combine(d, datetime.min.time()).replace(tzinfo=TZ).timestamp())


def truth(day):
    """(temp_max, rain) for one local day, worked out from the fake's functions alone."""
    ticks = range(local_day(day), local_day(day) + 86400, 300)
    return max(temp(t) for t in ticks), max(rain_day(t) for t in ticks)


def label(d):
    return f"{d:%a} {d.day} {d:%b %Y}"


@pytest.fixture
async def station(tmp_path, archived_cache):
    eco, fake = await archived_station(tmp_path, archived_cache)
    yield eco, fake
    await eco.close()


async def ask(eco, **args):
    return json.loads(await eco.tools[2].handler(args))


def dates(eco, first_ago, last_ago):
    """(first, last) local days, this many days ago. Never later than the day before yesterday: in the
    small hours yesterday is not settled yet, so it isn't cached (and isn't counted) until it is."""
    today = datetime.now(eco.tz).date()
    return today - timedelta(days=first_ago), today - timedelta(days=max(last_ago, 2))


async def test_the_hottest_day_that_also_rained(station):
    eco, fake = station
    first, last = dates(eco, 80, 1)
    out = await ask(eco, start_date=str(first), end_date=str(last),
                    where=[{"field": "rain", "op": ">", "value": 0}], sort_by="temp_max", order="desc", limit=3)
    days = [first + timedelta(days=k) for k in range((last - first).days + 1)]
    rainy = sorted((d for d in days if truth(d)[1] > 0), key=lambda d: -truth(d)[0])
    assert out["matching_days"] == len(rainy) and out["days_checked"] == len(days)
    assert [r["date"] for r in out["days"]] == [label(d) for d in rainy[:3]]
    top = out["days"][0]
    assert top["temp_max"] == round(truth(rainy[0])[0], 1) and top["rain"] == truth(rainy[0])[1] and top["source"] == "exact"
    assert fake.calls == []  # answered from the cache alone


async def test_counting_days_over_a_threshold_with_and_without_rain(station):
    eco, _ = station
    first, last = dates(eco, 60, 1)
    days = [first + timedelta(days=k) for k in range((last - first).days + 1)]
    cut = sorted(truth(d)[0] for d in days)[len(days) // 2]  # the median day's high
    hot = [d for d in days if round(truth(d)[0], 1) > cut]
    out = await ask(eco, start_date=str(first), end_date=str(last), where=[{"field": "temp_max", "op": ">", "value": cut}])
    assert out["matching_days"] == len(hot)
    both = await ask(eco, start_date=str(first), end_date=str(last),
                     where=[{"field": "temp_max", "op": ">", "value": cut}, {"field": "rain", "op": ">", "value": 0}])
    assert both["matching_days"] == len([d for d in hot if truth(d)[1] > 0])
    dry = await ask(eco, start_date=str(first), end_date=str(last), where=[{"field": "rain", "op": "=", "value": 0}], limit=1)
    assert dry["matching_days"] == len([d for d in days if truth(d)[1] == 0])


async def test_ascending_order_and_the_limit(station):
    eco, _ = station
    first, last = dates(eco, 30, 1)
    out = await ask(eco, start_date=str(first), end_date=str(last), sort_by="temp_min", order="asc", limit=2)
    assert len(out["days"]) == 2 and out["days"][0]["temp_min"] <= out["days"][1]["temp_min"]
    assert out["matching_days"] == out["days_checked"] == (last - first).days + 1   # no conditions: every day matches
    many = await ask(eco, start_date=str(first), end_date=str(last), limit=500)
    assert len(many["days"]) == 20                                  # capped


async def test_old_days_come_from_daily_buckets_and_say_so(station):
    eco, fake = station
    first, last = dates(eco, 540, 1)   # reaches past the year of 30-minute data
    out = await ask(eco, start_date=str(first), end_date=str(last), where=[{"field": "temp_max", "op": ">", "value": 0}], limit=20)
    assert out["days_checked"] > 500 and "note_daily" in out and out["exact_from"]
    cutoff = datetime.now(eco.tz).date() - timedelta(days=365)
    exact_from = datetime.strptime(out["exact_from"], "%a %d %b %Y").date()
    assert cutoff - timedelta(days=3) <= exact_from <= cutoff + timedelta(days=3)  # about where 30-minute data begins
    old = await ask(eco, start_date=str(first), end_date=str(first + timedelta(days=30)),
                    where=[{"field": "temp_max", "op": ">", "value": 0}], limit=5)
    assert old["days"] and all(r["source"] == "daily" for r in old["days"]) and "note_daily" in old
    assert fake.calls == []


async def test_a_longer_archive_makes_more_of_the_record_exact_without_code_changes(tmp_path, archived_cache):
    """The exact range follows what the cache holds, not a fixed cutoff."""
    eco, _ = await archived_station(tmp_path, archived_cache)
    first, last = dates(eco, 500, 300)   # 300-500 days ago: daily buckets only, for now
    args = dict(start_date=str(first), end_date=str(last), where=[{"field": "temp_max", "op": ">", "value": 0}], limit=1)
    assert (await ask(eco, **args))["days"][0]["source"] == "daily"
    # later, the archive has kept 30-minute data for that period too (stored the way the archive stores it)
    for (mac, grp, start, end) in [(eco.mac, g, local_day(first), local_day(last) + 86399) for g in ("outdoor", "rainfall")]:
        data = await eco.api.history(mac, "30min", datetime.fromtimestamp(start, TZ).replace(tzinfo=None),
                                     datetime.fromtimestamp(end, TZ).replace(tzinfo=None), grp)
        eco.cache.store(mac, "30min", [grp], data, start, end)
    assert (await ask(eco, **args))["days"][0]["source"] == "exact"
    await eco.close()


async def test_nothing_cached_is_reported_not_guessed(tmp_path):
    transport, fake = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    first, last = dates(eco, 30, 1)
    out = await ask(eco, start_date=str(first), end_date=str(last), where=[{"field": "rain", "op": ">", "value": 0}])
    assert out["days_checked"] == 0 and out["matching_days"] == 0 and "No cached readings" in out["note"]
    assert not any(c.get("path") == "history" for c in fake.calls)  # it never fetches
    await eco.close()


async def test_bad_input_gets_a_clear_error(station):
    eco, _ = station
    assert "bad date" in (await ask(eco, start_date="soon", end_date="later"))["error"]
    today = datetime.now(eco.tz).date()
    assert "before end_date" in (await ask(eco, start_date=str(today), end_date=str(today)))["error"]
    first, last = dates(eco, 10, 1)
    assert "unusable" in (await ask(eco, start_date=str(first), end_date=str(last),
                                    where=[{"field": "mood", "op": ">", "value": 1}]))["error"]
    assert "sort_by" in (await ask(eco, start_date=str(first), end_date=str(last), sort_by="mood"))["error"]


async def test_a_hotter_trace_rain_day_is_returned_for_a_footnote(station):
    eco, _ = station
    first, last = dates(eco, 80, 1)
    days = [first + timedelta(days=k) for k in range((last - first).days + 1)]
    wet = sorted((d for d in days if truth(d)[1] >= 1), key=lambda d: -truth(d)[0])
    trace_only = [d for d in days if 0 < truth(d)[1] < 1]
    assert wet and trace_only
    out = await ask(eco, start_date=str(first), end_date=str(last), where=[{"field": "rain", "op": ">=", "value": 1}],
                    sort_by="temp_max", limit=1)
    assert out["days"][0]["date"] == label(wet[0]) and out["matching_days"] == len(wet)
    hotter = [d for d in sorted(trace_only, key=lambda d: -truth(d)[0]) if truth(d)[0] > truth(wet[0])[0]][:2]
    if hotter:
        assert [r["date"] for r in out["trace_rain_days"]] == [label(d) for d in hotter][:len(out["trace_rain_days"])]
        assert all(0 < r["rain"] < 1 for r in out["trace_rain_days"]) and "footnote" in out["note_trace"]
    else:
        assert "trace_rain_days" not in out


async def test_trace_days_are_reported_even_when_nothing_reaches_the_threshold(station):
    eco, _ = station
    first, last = dates(eco, 80, 1)
    out = await ask(eco, start_date=str(first), end_date=str(last), where=[{"field": "rain", "op": ">=", "value": 50}],
                    sort_by="temp_max")
    assert out["matching_days"] == 0 and out["days"] == []
    assert len(out["trace_rain_days"]) == 2 and "less than 50 mm" in out["note_trace"]
    top = max((d for d in [first + timedelta(days=k) for k in range((last - first).days + 1)] if truth(d)[1] > 0),
              key=lambda d: truth(d)[0])
    assert out["trace_rain_days"][0]["date"] == label(top)


async def test_no_footnote_without_a_rain_threshold_or_when_nothing_ranks_higher(station):
    eco, _ = station
    first, last = dates(eco, 80, 1)
    assert "trace_rain_days" not in await ask(eco, start_date=str(first), end_date=str(last),
                                              where=[{"field": "temp_max", "op": ">", "value": 0}])
    assert "trace_rain_days" not in await ask(eco, start_date=str(first), end_date=str(last),
                                              where=[{"field": "rain", "op": ">", "value": 0}])   # any rain: nothing is hidden
    assert "trace_rain_days" not in await ask(eco, start_date=str(first), end_date=str(last),
                                              where=[{"field": "rain", "op": "<", "value": 1}])   # not a "rained" question


async def test_a_tie_is_not_reported_as_a_hotter_day(station):
    eco, _ = station
    first, last = dates(eco, 80, 1)
    out = await ask(eco, start_date=str(first), end_date=str(last), where=[{"field": "rain", "op": ">=", "value": 1}],
                    sort_by="temp_max", limit=1)
    top = out["days"][0]["temp_max"]
    assert all(r["temp_max"] > top for r in out.get("trace_rain_days", []))   # strictly hotter, never equal


async def test_public_holidays_are_the_local_ones_not_the_models_guess(station):
    import holidays
    eco, _ = station
    out = await ask(eco, start_date="2025-11-01", end_date="2026-02-28", only="public_holiday", sort_by="temp_max", limit=20)
    vic = holidays.country_holidays("AU", subdiv="VIC")
    expected = {d for d in (datetime(2025, 11, 1).date() + timedelta(days=k) for k in range(120)) if d in vic}
    assert out["days_checked"] == out["matching_days"] == len(expected) and len(expected) == 5   # Cup Day, Christmas, Boxing Day, New Year, Australia Day
    assert {r["date"] for r in out["days"]} == {label(d) for d in expected}
    names = {r["holiday"] for r in out["days"]}
    assert {"Melbourne Cup Day", "Christmas Day", "Boxing Day", "New Year's Day", "Australia Day"} <= names
    temps = [r["temp_max"] for r in out["days"]]
    assert temps == sorted(temps, reverse=True)


async def test_weekends_and_unknown_places(station):
    from zoneinfo import ZoneInfo

    from lib.ecowitt import days
    eco, _ = station
    out = await ask(eco, start_date="2026-08-03", end_date="2026-08-16", only="weekend", limit=10)   # two weekends
    assert out["days_checked"] == 4 and all(r["date"].startswith(("Sat", "Sun")) for r in out["days"])
    assert "error" in await ask(eco, start_date="2026-08-03", end_date="2026-08-16", only="bank_holiday")
    elsewhere = days.find_days(eco.cache, eco.mac, ZoneInfo("Europe/Paris"),
                               {"start_date": "2026-08-03", "end_date": "2026-08-16", "only": "public_holiday"},
                               datetime.now(ZoneInfo("Europe/Paris")).replace(tzinfo=None))
    assert "aren't known for this time zone" in elsewhere["error"]      # says so instead of guessing


async def test_a_missing_holidays_package_is_explained_not_guessed(station, monkeypatch):
    from lib.ecowitt import calendar
    eco, _ = station
    monkeypatch.setattr(calendar, "holidays", None)
    out = await ask(eco, start_date="2026-01-01", end_date="2026-02-01", only="public_holiday")
    assert "pip install holidays" in out["error"]


async def test_one_known_day_can_be_looked_up_with_no_conditions(station):
    """'Did it rain on Sat 5 Sep?': the day's own figures, not a search of the month that says nothing about it."""
    eco, _ = station
    day = datetime.now(eco.tz).date() - timedelta(days=6)
    out = await ask(eco, start_date=str(day), end_date=str(day))
    assert out["days_checked"] == out["matching_days"] == 1 and len(out["days"]) == 1
    row = out["days"][0]
    assert row["date"] == label(day) and {"temp_max", "temp_min", "rain"} <= set(row)
    assert row["rain"] == truth(day)[1] and row["temp_max"] == round(truth(day)[0], 1)


async def test_days_can_be_counted_by_any_reading_not_only_temperature_rain_and_gusts(station):
    eco, fake = station
    first, last = dates(eco, 10, 1)
    start, end = local_day(first), local_day(last) + 86399
    hot = {first + timedelta(days=k) for k in (2, 5, 7)}          # the days UV reaches 9
    uvi = {str(t): ("9.5" if datetime.fromtimestamp(t, TZ).date() in hot and 8 <= datetime.fromtimestamp(t, TZ).hour <= 14 else "3.0")
           for t in range(start, end, 300)}
    humidity = {str(t): "40" if datetime.fromtimestamp(t, TZ).date() in hot else "80" for t in range(start, end, 300)}
    eco.cache.store(eco.mac, "5min", ["solar_and_uvi"], {"solar_and_uvi": {"uvi": {"unit": "", "list": uvi}}}, start, end)
    eco.cache.store(eco.mac, "5min", ["outdoor"], {"outdoor": {"humidity": {"unit": "%", "list": humidity}}}, start, end)
    out = await ask(eco, start_date=str(first), end_date=str(last), where=[{"field": "uv_max", "op": ">=", "value": 9}], limit=5)
    assert out["matching_days"] == 3 and out["units"]["uv_max"] == "" and {r["uv_max"] for r in out["days"]} == {9.5}
    assert all(r["source"] == "exact" for r in out["days"]) and "note_averaged" not in out   # 5-minute readings: the peak is real
    dry = await ask(eco, start_date=str(first), end_date=str(last), where=[{"field": "humidity_min", "op": "<", "value": 50}])
    assert dry["matching_days"] == 3 and dry["units"]["humidity_min"] == "%"
    bad = await ask(eco, start_date=str(first), end_date=str(last), where=[{"field": "uvi", "op": ">=", "value": 9}])
    assert "unusable condition" in bad["error"] and "uv_max" in bad["error"]
    assert fake.calls == []


async def test_days_from_30_minute_data_say_their_peaks_are_averages(station):
    eco, _ = station
    first, last = dates(eco, 150, 145)   # older than the 90 days kept at 5 minutes: 30-minute readings
    start, end = local_day(first), local_day(last) + 86399
    uvi = {str(t): "9.2" for t in range(start, end, 1800)}
    eco.cache.store(eco.mac, "30min", ["solar_and_uvi"], {"solar_and_uvi": {"uvi": {"unit": "", "list": uvi}}}, start, end)
    out = await ask(eco, start_date=str(first), end_date=str(last), where=[{"field": "uv_max", "op": ">=", "value": 9}])
    assert out["matching_days"] == out["days_checked"] > 0 and "average" in out["note_averaged"]


async def test_count_only_gives_just_the_counts_and_group_by_month_gives_each_month(station):
    eco, _ = station
    first, last = dates(eco, 70, 1)
    where = [{"field": "temp_max", "op": ">", "value": 0}]
    full = await ask(eco, start_date=str(first), end_date=str(last), where=where, limit=5)
    counted = await ask(eco, start_date=str(first), end_date=str(last), where=where, count_only=True, group_by="month")
    assert "days" not in counted and counted["matching_days"] == full["matching_days"] and counted["days_checked"] == full["days_checked"]
    months = counted["by_month"]
    assert sum(months.values()) == counted["matching_days"] and list(months) == sorted(months) and len(months) >= 2
    assert "by_year" in await ask(eco, start_date=str(first), end_date=str(last), where=where, count_only=True, group_by="year")


async def test_counts_per_month_or_year_are_drawn_as_a_bar_chart_with_a_title_that_says_the_condition(station):
    from lib.tools import Turn
    eco, _ = station
    first, last = dates(eco, 70, 1)
    where = [{"field": "temp_max", "op": ">=", "value": 0}]
    turn = Turn()
    out = json.loads(await eco.tools[2].handler(dict(start_date=str(first), end_date=str(last), where=where, count_only=True, group_by="month"), turn))
    assert len(turn.charts) == 1 and "chart" in out and "caption" in out["chart"]
    chart = turn.charts[0]
    bars = chart.panels[0].bars
    assert chart.title == "Days with temperature ≥ 0 °C" and bars.per == "month" and bars.values
    assert [int(v) for v in bars.y] == list(out["by_month"].values()) and len(bars.x) == len(out["by_month"])
    assert f"{out['matching_days']:,} of {out['days_checked']:,} days" in chart.subtitle
    from lib.charts import render
    assert render(chart, eco.tz)[:4] == b"\x89PNG"
    plain = Turn()                                                  # no group_by: a count, no chart
    await eco.tools[2].handler(dict(start_date=str(first), end_date=str(last), where=where, count_only=True), plain)
    assert plain.charts == []


async def test_a_total_highest_or_average_per_month_is_a_bar_chart_of_those_figures(station):
    from collections import defaultdict
    from lib.tools import Turn
    eco, _ = station
    first, last = dates(eco, 70, 1)
    days = [first + timedelta(days=k) for k in range((last - first).days + 1)]
    args = dict(start_date=str(first), end_date=str(last), count_only=True, group_by="month")

    rain = defaultdict(float)
    top = {}
    for d in days:
        rain[f"{d:%Y-%m}"] += truth(d)[1]
        top[f"{d:%Y-%m}"] = max(top.get(f"{d:%Y-%m}", -1e9), truth(d)[0])
    turn = Turn()
    out = json.loads(await eco.tools[2].handler({**args, "stat": "sum", "of": "rain"}, turn))
    assert out["stat"]["what"] == "Total rain" and out["stat"]["unit"] == "mm" and "days" not in out
    assert {k: v for k, v in out["by_month"].items()} == {k: round(v, 1) for k, v in sorted(rain.items())}
    bars = turn.charts[0].panels[0].bars
    assert turn.charts[0].title == "Total rain" and bars.unit == "mm" and bars.per == "month" and bars.y == list(out["by_month"].values())

    hottest = json.loads(await eco.tools[2].handler({**args, "stat": "max", "of": "temp_max"}, Turn()))
    assert hottest["stat"]["what"] == "Highest temperature" and hottest["by_month"] == {k: round(v, 1) for k, v in sorted(top.items())}

    mean = json.loads(await eco.tools[2].handler({**args, "stat": "avg", "of": "temp_avg"}, Turn()))
    assert mean["stat"]["what"] == "Average temperature" and all(v is not None for v in mean["by_month"].values())
    overall = json.loads(await eco.tools[2].handler(dict(start_date=str(first), end_date=str(last), stat="max", of="temp_max", count_only=True), Turn()))
    assert overall["value"] == round(max(top.values()), 1) and "by_month" not in overall
    bad = json.loads(await eco.tools[2].handler(dict(start_date=str(first), end_date=str(last), stat="max"), Turn()))
    assert "needs `of`" in bad["error"]


async def test_a_holiday_filter_the_person_never_asked_for_is_dropped_but_a_requested_one_is_kept(station):
    from lib.tools import Turn
    eco, _ = station
    first, last = dates(eco, 60, 1)
    args = dict(start_date=str(first), end_date=str(last), where=[{"field": "temp_max", "op": ">", "value": 0}], count_only=True)
    plain = json.loads(await eco.tools[2].handler(args, Turn(text="days over 0 degrees")))
    strayed = json.loads(await eco.tools[2].handler({**args, "only": "weekend"}, Turn(text="days over 0 degrees")))
    assert strayed["days_checked"] == plain["days_checked"] > 40            # the weekend filter was dropped: every day counted
    asked = json.loads(await eco.tools[2].handler({**args, "only": "weekend"}, Turn(text="how many weekend days over 0 degrees")))
    assert 0 < asked["days_checked"] < 20                                    # kept: only Saturdays and Sundays
    direct = json.loads(await eco.tools[2].handler({**args, "only": "weekend"}))   # no question to check against (a direct call)
    assert direct["days_checked"] == asked["days_checked"]


async def test_a_chart_of_holiday_or_weekend_days_says_so_in_its_subtitle(station):
    from lib.tools import Turn
    eco, _ = station
    first, last = dates(eco, 60, 1)
    turn = Turn(text="how many weekend days over 0 degrees per month")
    await eco.tools[2].handler(dict(start_date=str(first), end_date=str(last), where=[{"field": "temp_max", "op": ">", "value": 0}],
                                    count_only=True, group_by="month", only="weekend"), turn)
    assert turn.charts[0].subtitle.endswith("weekends only  ·  per month") or "weekends only" in turn.charts[0].subtitle
