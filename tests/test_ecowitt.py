import asyncio
import json
from datetime import datetime, timedelta

import pytest

from lib.charts import CHART_REQUESTS
from lib.ecowitt import Ecowitt
from lib.ecowitt import api as ecowitt_api
from tests.fakes import MAC, config, ecowitt_transport


@pytest.fixture
async def station(tmp_path):
    transport, fake = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    yield eco, fake
    await eco.close()


async def test_finds_the_station(station):
    eco, _ = station
    assert eco.mac == MAC and eco.station_name == "Fairleigh" and eco.longitude == 145.0
    assert "Fairleigh" in eco.describe() and "created" in eco.describe()


@pytest.mark.parametrize("msg", ["System is busy.", "Operation too frequent"])
async def test_busy_and_too_frequent_are_retried(monkeypatch, msg):
    import httpx
    slept, replies = [], [{"code": -1, "msg": msg}, {"code": 0, "data": {"list": [{"mac": "x"}]}}]

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(ecowitt_api.asyncio, "sleep", fake_sleep)
    api = ecowitt_api.EcowittAPI("a", "b", httpx.MockTransport(lambda r: httpx.Response(200, json=replies.pop(0))))
    assert len(await api.devices()) == 1 and slept == [3]
    await api.close()


async def test_requests_are_spaced_out(monkeypatch):
    import httpx
    monkeypatch.setattr(ecowitt_api, "MIN_GAP_SECONDS", 0.2)
    stamps = []

    def handler(request):
        stamps.append(asyncio.get_running_loop().time())
        return httpx.Response(200, json={"code": 0, "data": {}})
    api = ecowitt_api.EcowittAPI("a", "b", httpx.MockTransport(handler))
    await asyncio.gather(api.devices(), api.devices(), api.devices())
    assert min(b - a for a, b in zip(stamps, stamps[1:])) >= 0.19
    await api.close()


async def test_errors_surface(tmp_path):
    import httpx
    api = ecowitt_api.EcowittAPI("a", "b", httpx.MockTransport(
        lambda r: httpx.Response(200, json={"code": 40010, "msg": "Invalid key"})))
    with pytest.raises(ecowitt_api.EcowittError, match="Invalid key"):
        await api.devices()
    await api.close()


async def test_requests_use_metric_unit_ids(station):
    eco, fake = station
    await eco.recent(1)
    assert fake.calls[-1]["temp_unitid"] == "1" and fake.calls[-1]["rainfall_unitid"] == "12"
    assert fake.calls[-1]["call_back"] == "outdoor,indoor,rainfall,pressure,wind"


async def test_realtime_is_compact_and_local(station):
    eco, _ = station
    out = json.loads(await eco.tools[0].handler({"groups": "outdoor"}))
    assert out["outdoor"]["temperature"] == "12.3 ℃" and out["time"]


async def test_history_gives_extremes_with_times_and_days(station):
    eco, fake = station
    today = datetime.now(eco.tz).date()
    start, end = today - timedelta(days=3), today - timedelta(days=1)
    result = json.loads(await eco.tools[1].handler(
        {"groups": "outdoor", "start_date": f"{start} 00:00:00", "end_date": f"{end} 23:59:59"}))
    s = result["series"]["outdoor.temperature"]
    assert set(s) >= {"low", "high", "low_when", "high_when", "low_date", "high_date", "daily"}
    assert len(s["daily"]) == 3
    assert float(s["high"]) > float(s["low"])
    assert s["high_when"].startswith(("at ", "around "))  # refined to a 5-minute or 30-minute reading


async def test_second_question_is_served_from_the_cache(station):
    eco, fake = station
    today = datetime.now(eco.tz).date()
    args = {"groups": "outdoor", "start_date": f"{today - timedelta(days=4)} 00:00:00",
            "end_date": f"{today - timedelta(days=2)} 23:59:59"}
    first = await eco.tools[1].handler(args)
    fake.calls.clear()
    assert await eco.tools[1].handler(args) == first
    assert fake.calls == []


async def test_long_range_uses_daily_data_and_monthly_figures(station):
    eco, fake = station
    today = datetime.now(eco.tz).date()
    result = json.loads(await eco.tools[1].handler(
        {"groups": "outdoor", "start_date": f"{today - timedelta(days=200)} 00:00:00", "end_date": f"{today} 00:00:00"}))
    s = result["series"]["outdoor.temperature"]
    assert "monthly" in s and "daily" not in s and result["monthly_note"]
    assert {c["cycle_type"] for c in fake.calls if "cycle_type" in c} >= {"1day"}


async def test_derived_readings_hidden_unless_asked(tmp_path):
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    orig = eco.api.history

    async def with_dew(*a, **k):
        data = await orig(*a, **k)
        data["outdoor"]["dew_point"] = data["outdoor"]["temperature"]
        return data
    eco.api.history = with_dew
    day = datetime.now(eco.tz).date() - timedelta(days=2)
    args = {"groups": "outdoor", "start_date": f"{day} 00:00:00", "end_date": f"{day} 23:59:59"}
    assert "outdoor.dew_point" not in json.loads(await eco.tools[1].handler(args))["series"]
    assert "outdoor.dew_point" in json.loads(await eco.tools[1].handler({**args, "include_derived": ["dew_point"]}))["series"]
    await eco.close()


async def test_chart_request_adds_a_spec(station):
    eco, _ = station
    day = datetime.now(eco.tz).date() - timedelta(days=2)
    holder = []
    token = CHART_REQUESTS.set(holder)
    try:
        out = json.loads(await eco.tools[1].handler(
            {"groups": "outdoor,indoor", "chart": True, "start_date": f"{day} 00:00:00", "end_date": f"{day} 23:59:59"}))
    finally:
        CHART_REQUESTS.reset(token)
    assert "chart" in out and holder[0]["kind"] == "line" and {s["label"] for s in holder[0]["series"]} == {"Outdoor", "Indoor"}


async def test_bad_dates_are_reported(station):
    eco, _ = station
    assert (await eco.tools[1].handler({"start_date": "nonsense", "end_date": "x"})).startswith("Error: bad date")


async def test_warm_then_question_needs_no_new_requests_for_the_tail(station):
    eco, fake = station
    summary = await eco.warm(True)
    assert "request(s)" in summary
    fake.calls.clear()
    now = eco.now()
    await eco.fetcher(["outdoor"]).get("5min", datetime.combine(now.date(), datetime.min.time()), now)
    assert fake.calls == []  # recent tail came from memory


# ---------- the whole history is cached ----------
async def test_archive_caches_every_cycle_so_long_questions_need_no_requests(tmp_path, monkeypatch):
    from lib.ecowitt import Archive, archive as archive_mod
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    transport, fake = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    fetched, failed = await Archive(eco).run_once()
    assert failed == 0 and fetched > 100
    assert {c["cycle_type"] for c in fake.calls if "cycle_type" in c} == {"5min", "30min", "4hour", "1day"}
    fake.calls.clear()
    today = datetime.now(eco.tz).date()
    for days in (30, 200, 700):  # a month, most of a year, nearly two years
        out = await eco.tools[1].handler({"groups": "outdoor", "start_date": f"{today - timedelta(days=days)} 00:00:00",
                                          "end_date": f"{today - timedelta(days=2)} 23:59:59"})
        assert "Error" not in out[:20], out[:200]
    assert fake.calls == []  # nothing was fetched while answering
    await eco.close()


async def test_archive_only_refetches_the_newest_ranges_next_time(tmp_path, monkeypatch):
    from lib.ecowitt import Archive, archive as archive_mod
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    transport, fake = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    arch = Archive(eco)
    await arch.run_once()
    fake.calls.clear()
    await arch.run_once()
    assert len(fake.calls) <= 4  # the unsettled tail of each cycle
    await eco.close()


async def test_archive_skips_time_before_the_station_existed(tmp_path, monkeypatch):
    from lib.ecowitt import Archive, archive as archive_mod
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    transport, fake = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    eco.created = datetime.now(eco.tz) - timedelta(days=20)
    await Archive(eco).run_once()
    starts = [c["start_date"][:10] for c in fake.calls if "start_date" in c]
    assert min(starts) >= str((eco.created - timedelta(days=1)).date())
    assert not any(c["cycle_type"] == "5min" and c["start_date"][:10] < str(eco.created.date() - timedelta(days=1))
                   for c in fake.calls if "cycle_type" in c)
    await eco.close()


async def test_year_long_questions_get_dated_monthly_figures_once_the_history_is_cached(tmp_path, monkeypatch):
    from lib.ecowitt import Archive, archive as archive_mod
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    transport, fake = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    today = datetime.now(eco.tz).date()
    args = {"groups": "outdoor", "start_date": f"{today - timedelta(days=200)} 00:00:00", "end_date": f"{today} 00:00:00"}

    def monthly(out):
        return json.loads(out)["series"]["outdoor.temperature"]["monthly"]

    before = monthly(await eco.tools[1].handler(args))          # nothing cached: daily figures, values only
    assert all("low_when" not in m for m in before.values())

    await Archive(eco).run_once()
    fake.calls.clear()
    token = CHART_REQUESTS.set([])
    try:
        out = await eco.tools[1].handler({**args, "chart": True})
        spec = CHART_REQUESTS.get()[0]
    finally:
        CHART_REQUESTS.reset(token)
    after = monthly(out)
    assert len(after) >= 6 and all({"low_when", "low_date", "high_when", "high_date"} <= set(m) for m in after.values())
    assert "monthly_note" not in json.loads(out) and fake.calls == []
    assert "30-minute" in spec["subtitle"] or "5-minute" in spec["subtitle"]
    await eco.close()
