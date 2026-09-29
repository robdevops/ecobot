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
    assert fake.calls[-1]["call_back"] == "outdoor,indoor,pressure,wind,rainfall,rainfall_piezo"  # what the archive keeps too


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
    assert summary.startswith("Ecowitt ") and summary.endswith(" req")
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
    # ends yesterday, not at midnight today: the newest hours are still settling, and just after midnight
    # "today 00:00" is one of them (so the test would fail in the small hours, when it must ask again)
    args = {"groups": "outdoor", "start_date": f"{today - timedelta(days=201)} 00:00:00",
            "end_date": f"{today - timedelta(days=1)} 00:00:00"}

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


async def test_an_interrupted_archive_resumes_where_it_stopped(tmp_path, monkeypatch):
    import asyncio as aio
    from lib.ecowitt import Archive, archive as archive_mod
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0.005)
    transport, fake = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    fake.calls.clear()
    with pytest.raises(aio.TimeoutError):  # power cut / restart part-way through the backfill
        await aio.wait_for(Archive(eco).run_once(), timeout=0.6)
    first = {(c["cycle_type"], c["start_date"]) for c in fake.calls if "cycle_type" in c}
    assert 5 < len(first) < 150, len(first)
    fake.calls.clear()
    fetched, failed = await Archive(eco).run_once()  # the next start
    second = {(c["cycle_type"], c["start_date"]) for c in fake.calls if "cycle_type" in c}
    assert failed == 0 and second, "the rest was fetched"
    assert len(first & second) <= 1, "only the range in flight when it stopped is asked for again"
    fake.calls.clear()
    await Archive(eco).run_once()
    assert fake.calls == []  # complete: the archive only asks for settled data, and there is none left to fetch
    await eco.close()


async def test_archive_reports_how_much_history_is_held(tmp_path, monkeypatch):
    from lib.ecowitt import Archive, archive as archive_mod
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    arch = Archive(eco)
    assert arch.held() == "0/0/0/0 days (5min/30min/4h/1d)"
    await arch.run_once()
    held = {c: eco.cache.days_held(eco.mac, c, arch.groups) for c in ("5min", "30min", "4hour", "1day")}
    assert 85 <= held["5min"] <= 90 and 350 <= held["30min"] <= 365 and 700 <= held["4hour"] <= 730 and held["1day"] > 1400
    assert arch.held().startswith(f"{held['5min']}/{held['30min']}/{held['4hour']}/{held['1day']} days")
    await eco.close()


async def test_archive_estimates_its_duration_from_the_pace(tmp_path, monkeypatch, caplog):
    from lib.ecowitt import Archive, archive as archive_mod
    caplog.set_level("INFO")
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    monkeypatch.setattr(archive_mod, "MIN_GAP_SECONDS", 2.0)  # as in production: 2s per range plus about 1s
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    await Archive(eco).run_once()
    assert not any("readings stored" in r.message for r in caplog.records)
    caplog.clear()
    await Archive(eco).run_once()
    assert not any("to fetch" in r.message for r in caplog.records)  # nothing left: no estimate to give
    # a fresh cache has everything to fetch: about 3s each, shown in minutes once it is over 90s
    eco.cache.db.executescript("DELETE FROM coverage; DELETE FROM points;")
    caplog.clear()
    await Archive(eco).run_once()
    line = next(r.message for r in caplog.records if "to fetch" in r.message)
    n = int(line.split(": ")[1].split(" ")[0])
    assert f"{n} req to fetch (about {n * 3 / 60:.0f} min)" in line
    await eco.close()


async def test_a_group_the_station_lacks_is_dropped_for_the_warm_up_too(tmp_path, monkeypatch):
    import httpx
    from lib.ecowitt import Archive, archive as archive_mod
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    real = ecowitt_transport()[1]

    def handler(request):  # this station has no piezo rain gauge
        if "rainfall_piezo" in request.url.params.get("call_back", ""):
            return httpx.Response(200, json={"code": 40000, "msg": "Invalid call_back"})
        return real(request)
    eco = Ecowitt(config(tmp_path), transport=httpx.MockTransport(handler))
    await eco.start()
    assert "rainfall_piezo" in eco.groups
    fetched, failed = await Archive(eco).run_once()
    assert failed == 0 and fetched > 100 and "rainfall_piezo" not in eco.groups
    real.calls.clear()
    await eco.warm(True)  # would fail on the missing group if it still asked for it
    assert real.calls and all("rainfall_piezo" not in c["call_back"] for c in real.calls if "call_back" in c)
    await eco.close()


async def test_the_archive_leaves_the_still_settling_newest_data_to_the_warm_up(tmp_path, monkeypatch):
    from lib.ecowitt import Archive
    transport, fake = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    now = eco.now()
    for cycle, start, end in Archive(eco)._work():
        assert end <= now - timedelta(hours=1), (cycle, end)  # nothing that is still changing
    await eco.close()


async def test_a_passing_outage_does_not_drop_a_group_and_the_ranges_are_retried(tmp_path, monkeypatch):
    import httpx
    from lib.ecowitt import Archive, archive as archive_mod
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    real = ecowitt_transport()[1]
    down = {"on": True}

    def handler(request):  # the network to Ecowitt drops whenever the piezo group is asked for
        if down["on"] and "rainfall_piezo" in request.url.params.get("call_back", ""):
            raise httpx.ConnectTimeout("timed out")
        return real(request)
    eco = Ecowitt(config(tmp_path), transport=httpx.MockTransport(handler))
    await eco.start()
    arch = Archive(eco)
    first_three = list(arch._work())[:3]
    monkeypatch.setattr(Archive, "_work", lambda self: iter(first_three))
    fetched, failed = await arch.run_once()
    assert (fetched, failed) == (0, 3) and "rainfall_piezo" in eco.groups   # kept: a timeout says nothing about the group
    down["on"] = False
    fetched, failed = await arch.run_once()                                 # the outage is over: they are retried
    assert (fetched, failed) == (3, 0) and "rainfall_piezo" in eco.groups
    await eco.close()


async def test_transient_and_refused_requests_are_told_apart(tmp_path):
    import httpx
    from lib.ecowitt import api as ecowitt_api
    calls = []

    def handler(request):  # the first request is refused outright; every later one gets "busy"
        calls.append(1)
        return httpx.Response(200, json={"code": 40000, "msg": "Invalid call_back"} if len(calls) == 1
                              else {"code": -1, "msg": "System is busy."})
    api = ecowitt_api.EcowittAPI("a", "b", httpx.MockTransport(handler))
    import asyncio as aio
    orig, aio.sleep = aio.sleep, (lambda s: orig(0))
    try:
        with pytest.raises(ecowitt_api.EcowittError) as refused:
            await api.devices()
        assert refused.value.transient is False
        with pytest.raises(ecowitt_api.EcowittError) as busy:      # still busy after every retry
            await api.devices()
        assert busy.value.transient is True
    finally:
        aio.sleep = orig
    await api.close()


async def test_http_errors_are_refused_only_when_ecowitt_answers_with_a_reason(tmp_path):
    import httpx
    from lib.ecowitt import api as ecowitt_api
    replies = {
        "400 with reason": httpx.Response(400, json={"code": 40010, "msg": "Invalid mac"}),
        "401 no body": httpx.Response(401, text="Unauthorized"),
        "502 gateway": httpx.Response(502, text="<html>Bad gateway</html>"),
        "429 with reason": httpx.Response(429, json={"code": 429, "msg": "Rate limited"}),
        "500 with reason": httpx.Response(500, json={"code": 500, "msg": "Internal error"}),
    }
    expected = {"400 with reason": False, "401 no body": True, "502 gateway": True,
                "429 with reason": True, "500 with reason": True}
    import asyncio as aio
    orig, aio.sleep = aio.sleep, (lambda s: orig(0))
    try:
        for name, reply in replies.items():
            api = ecowitt_api.EcowittAPI("a", "b", httpx.MockTransport(lambda r, reply=reply: reply))
            with pytest.raises(ecowitt_api.EcowittError) as err:
                await api.devices()
            assert err.value.transient is expected[name], name   # a bad key or gateway must never drop a group
            await api.close()
    finally:
        aio.sleep = orig


# ---------- wind direction is circular ----------
def test_direction_summary_crosses_north_without_a_wide_swing():
    from lib.ecowitt.direction import point, summarise, vector_mean
    from tests.fakes import TZ
    readings = [(1_780_000_000 + i * 300, d, True) for i, d in enumerate([350, 0, 10] * 20)]
    out = summarise(readings, TZ, by_day=True)
    assert out["most_common"].startswith("N ") and out["average_direction"].startswith("N (") and out["steadiness"] == "steady"
    assert "low" not in out and "high" not in out and "note" not in out
    mean = vector_mean([359, 1])[0]
    assert mean > 359 or mean < 1   # north, not 180
    assert point(359) == point(1) == "N" and point(90) == "E" and point(202) == "SSW"
    assert len(out["daily"]) >= 1


def test_direction_summary_of_a_variable_wind_and_averaged_data():
    from lib.ecowitt.direction import summarise
    from tests.fakes import TZ
    spread = [(1_780_000_000 + i * 300, (i * 47) % 360, i % 2 == 0) for i in range(200)]
    out = summarise(spread, TZ, by_day=False)
    assert out["steadiness"] == "variable" and "averaged data" in out["note"] and "daily" not in out
    assert summarise([], TZ, by_day=False) == {}


async def test_history_reports_direction_by_compass_point_not_a_range(tmp_path, monkeypatch):
    from lib.ecowitt import Archive, archive as archive_mod
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    transport, fake = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    await Archive(eco).run_once()
    today = datetime.now(eco.tz).date()
    out = json.loads(await eco.tools[1].handler({"groups": "wind", "start_date": f"{today - timedelta(days=7)} 00:00:00",
                                                 "end_date": f"{today - timedelta(days=2)} 23:59:59"}))
    direction = out["series"]["wind.wind_direction"]
    assert direction["most_common"].startswith("N ") and "low" not in direction and "high" not in direction
    assert direction["average_direction"].startswith("N (") and "note" not in direction   # 5-minute readings
    assert direction["calm"].startswith("25%")                                             # midnight to 6am had no wind
    assert len(direction["daily"]) == 6 and all(v.startswith("N ") for v in direction["daily"].values())
    assert "high" in out["series"]["wind.wind_gust"]                                       # speeds are unchanged
    holder = CHART_REQUESTS.set([])                                                        # asking for a chart of direction alone
    try:
        again = json.loads(await eco.tools[1].handler({"groups": "wind", "chart": True, "include_derived": [],
                                                       "start_date": f"{today - timedelta(days=3)} 00:00:00",
                                                       "end_date": f"{today - timedelta(days=2)} 23:59:59"}))
    finally:
        CHART_REQUESTS.reset(holder)
    assert "wind.wind_direction" in again["series"]
    await eco.close()


async def test_a_direction_chart_is_a_scatter_by_hour_of_day(tmp_path, monkeypatch):
    from lib.charts import render
    from lib.ecowitt import Archive, archive as archive_mod
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    await Archive(eco).run_once()
    today = datetime.now(eco.tz).date()
    args = {"groups": "wind", "chart": True, "start_date": f"{today - timedelta(days=6)} 00:00:00",
            "end_date": f"{today - timedelta(days=2)} 23:59:59"}
    token = CHART_REQUESTS.set([])
    try:
        out = json.loads(await eco.tools[1].handler(args))
        specs = CHART_REQUESTS.get()
    finally:
        CHART_REQUESTS.reset(token)
    kinds = [s["kind"] for s in specs]
    assert kinds == ["line", "direction"] and "wind direction" in out["chart"]     # gusts still get their line
    points = specs[1]["points"]
    assert 0 < len(points) <= 4000 and all(0 <= h < 24 and 0 <= d < 360 for h, d in points)
    png = render(specs[1], eco.tz)
    assert png[:4] == b"\x89PNG"
    (tmp_path / "direction.png").write_bytes(png)
    await eco.close()


def test_calm_readings_are_reported_not_counted():
    from lib.ecowitt.direction import summarise
    from tests.fakes import TZ
    windy = [(1_780_000_000 + i * 300, 90.0, True) for i in range(80)]
    out = summarise(windy, TZ, by_day=False, calm=20)
    assert out["most_common"].startswith("E ") and out["calm"].startswith("20%")


async def test_a_chart_asked_for_in_the_persons_words_is_drawn_even_if_the_model_says_chart_false(tmp_path, monkeypatch):
    from lib.charts import CHART_ASKED
    from lib.ecowitt import Archive, archive as archive_mod
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    await Archive(eco).run_once()
    today = datetime.now(eco.tz).date()
    short = {"groups": "wind", "chart": False, "start_date": f"{today - timedelta(days=3)} 00:00:00",
             "end_date": f"{today - timedelta(days=2)} 23:59:59"}   # two days
    long = {**short, "start_date": f"{today - timedelta(days=5)} 00:00:00"}   # four days: always a chart
    for args, asked, expected in ((short, False, 0), (short, True, 2), (long, False, 2)):
        holder, asked_token = CHART_REQUESTS.set([]), CHART_ASKED.set(asked)
        try:
            await eco.tools[1].handler(args)
            assert len(CHART_REQUESTS.get()) == expected
        finally:
            CHART_ASKED.reset(asked_token)
            CHART_REQUESTS.reset(holder)
    await eco.close()


async def test_the_chart_plots_the_field_the_question_is_about(tmp_path):
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    today = datetime.now(eco.tz).date()
    base = {"groups": "outdoor", "start_date": f"{today - timedelta(days=8)} 00:00:00",
            "end_date": f"{today - timedelta(days=2)} 23:59:59"}
    async def chart_of(**extra):
        token = CHART_REQUESTS.set([])
        try:
            await eco.tools[1].handler({**base, **extra})
            return CHART_REQUESTS.get()[0]
        finally:
            CHART_REQUESTS.reset(token)
    assert (await chart_of())["title"] == "Temperature"                       # the default
    assert (await chart_of(chart_field="wind_gust", groups="wind"))["title"] == "Wind gust"
    assert (await chart_of(chart_field="Nonsense"))["title"] == "Temperature"  # unknown: fall back, never fail
    from lib.charts import CHART_FIELD
    token = CHART_FIELD.set("wind_gust")                                       # from the person's words: beats the model's choice
    try:
        assert (await chart_of(groups="outdoor,wind", chart_field="temperature"))["title"] == "Wind gust"
    finally:
        CHART_FIELD.reset(token)
    await eco.close()


async def test_charts_of_bucketed_data_carry_each_buckets_range(tmp_path):
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    today = datetime.now(eco.tz).date()

    async def spec_for(days_back):
        token = CHART_REQUESTS.set([])
        try:
            await eco.tools[1].handler({"groups": "outdoor", "chart": True,
                                        "start_date": f"{today - timedelta(days=days_back)} 00:00:00",
                                        "end_date": f"{today - timedelta(days=1)} 23:59:59"})
            return CHART_REQUESTS.get()[0]
        finally:
            CHART_REQUESTS.reset(token)
    week = (await spec_for(8))["series"][0]                       # 30-minute data: each has a low and a high
    assert len(week["low"]) == len(week["high"]) == len(week["y"])
    assert all(lo <= y <= hi for lo, y, hi in zip(week["low"], week["y"], week["high"]))
    assert "range shaded" in (await spec_for(8))["subtitle"]
    await eco.close()
