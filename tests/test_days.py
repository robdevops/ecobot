"""weather_days: rank, filter and count days from the cache, checked against answers computed
independently from the fake station's own functions."""

import json
from datetime import datetime, timedelta

import pytest

from lib.ecowitt import Archive, Ecowitt, archive as archive_mod
from tests.fakes import TZ, config, ecowitt_transport, rain_day, temp

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
async def station(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    transport, fake = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    await Archive(eco).run_once()
    fake.calls.clear()
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
    first, last = dates(eco, 700, 1)   # reaches past the year of 30-minute data
    out = await ask(eco, start_date=str(first), end_date=str(last), where=[{"field": "temp_max", "op": ">", "value": 0}], limit=20)
    assert out["days_checked"] > 600 and "note_daily" in out and out["exact_from"]
    cutoff = datetime.now(eco.tz).date() - timedelta(days=365)
    exact_from = datetime.strptime(out["exact_from"], "%a %d %b %Y").date()
    assert cutoff - timedelta(days=3) <= exact_from <= cutoff + timedelta(days=3)  # about where 30-minute data begins
    old = await ask(eco, start_date=str(first), end_date=str(first + timedelta(days=30)),
                    where=[{"field": "temp_max", "op": ">", "value": 0}], limit=5)
    assert old["days"] and all(r["source"] == "daily" for r in old["days"]) and "note_daily" in old
    assert fake.calls == []


async def test_a_longer_archive_makes_more_of_the_record_exact_without_code_changes(tmp_path, monkeypatch):
    """The exact range follows what the cache holds, not a fixed cutoff."""
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    await Archive(eco).run_once()
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
