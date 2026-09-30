import asyncio
import json
from datetime import datetime, timedelta

import pytest

from lib.charts import CHART_REQUESTS
from lib.ecowitt import Ecowitt
from lib.ecowitt import api as ecowitt_api
from tests.fakes import MAC, archived_station, config, ecowitt_transport


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
    transport, fake = ecowitt_transport(history_days=60)
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    fetched, failed = await Archive(eco).run_once()
    assert failed == 0 and fetched > 40
    assert {c["cycle_type"] for c in fake.calls if "cycle_type" in c} == {"5min", "30min", "4hour", "1day"}
    fake.calls.clear()
    today = datetime.now(eco.tz).date()
    for days in (10, 30, 58):  # up to all of the fake station's 60 days
        out = await eco.tools[1].handler({"groups": "outdoor", "start_date": f"{today - timedelta(days=days)} 00:00:00",
                                          "end_date": f"{today - timedelta(days=2)} 23:59:59"})
        assert "Error" not in out[:20], out[:200]
    assert fake.calls == []  # nothing was fetched while answering
    await eco.close()


async def test_archive_only_refetches_the_newest_ranges_next_time(tmp_path, monkeypatch):
    from lib.ecowitt import Archive, archive as archive_mod
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    transport, fake = ecowitt_transport(history_days=60)
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
    assert "daily averages, range shaded" in spec["subtitle"]      # 200 days: a point a day from the cached 30-minute data
    assert len(spec["series"][0]["x"]) > 150 and len(spec["series"][0]["low"]) == len(spec["series"][0]["x"])
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
    transport, _ = ecowitt_transport(history_days=60)
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    arch = Archive(eco)
    assert arch.held() == "0/0/0/0 days (5min/30min/4h/1d)"
    await arch.run_once()
    held = {c: eco.cache.days_held(eco.mac, c, arch.groups) for c in ("5min", "30min", "4hour", "1day")}
    assert all(55 <= held[c] <= 62 for c in ("5min", "30min", "4hour", "1day"))
    assert arch.held().startswith(f"{held['5min']}/{held['30min']}/{held['4hour']}/{held['1day']} days")
    await eco.close()


async def test_archive_estimates_its_duration_from_the_pace(tmp_path, monkeypatch, caplog):
    from lib.ecowitt import Archive, archive as archive_mod
    caplog.set_level("INFO")
    monkeypatch.setattr(archive_mod, "PACE_SECONDS", 0)
    monkeypatch.setattr(archive_mod, "MIN_GAP_SECONDS", 2.0)  # as in production: 2s per range plus about 1s
    transport, _ = ecowitt_transport(history_days=60)
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


async def test_history_reports_direction_by_compass_point_not_a_range(tmp_path, archived_cache):
    eco, fake = await archived_station(tmp_path, archived_cache)
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


async def test_a_direction_chart_is_a_heatmap_over_time(tmp_path, archived_cache):
    from lib.charts import render
    eco, _ = await archived_station(tmp_path, archived_cache)
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
    spec = specs[1]
    assert spec["step"] == 3600 and spec["unit"] == "hour" and len(spec["columns"]) <= 130    # five days: hourly columns
    assert all(len(col) == 16 for col in spec["columns"]) and sum(map(sum, spec["columns"])) > 0
    north = sum(col[0] for col in spec["columns"])
    assert north == sum(map(sum, spec["columns"]))                # the fake wind swings 350, 0, 10: all in the N cell
    assert "left: share of readings per hour" in spec["subtitle"] and "right: whole period, by speed" in spec["subtitle"]
    rose = spec["rose"]
    assert len(rose) == 16 and sum(map(sum, rose)) == sum(map(sum, spec["columns"]))   # the same readings as the heatmap
    assert sum(r[0] for r in rose) == 0 and sum(r[1] for r in rose) > 0 and sum(r[2] for r in rose) > 0   # 10 km/h and 25 km/h
    assert sum(rose[0]) == sum(map(sum, rose))                                            # all of it from the N wedge
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


async def test_a_chart_asked_for_in_the_persons_words_is_drawn_even_if_the_model_says_chart_false(tmp_path, archived_cache):
    from lib.charts import CHART_ASKED
    eco, _ = await archived_station(tmp_path, archived_cache)
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
    few = await spec_for(4)                                        # four days: 5- or 30-minute readings, each day's range shaded
    line = few["series"][0]
    assert "6-hour range shaded" in few["subtitle"] and len(line["x"]) > 100
    assert all(lo <= y <= hi for lo, y, hi in zip(line["low"], line["y"], line["high"]))
    assert len(set(line["high"])) > 20                                            # a ribbon that follows the line, not flat blocks
    await eco.close()


async def test_a_multi_year_chart_uses_cached_30_minute_data_for_the_newest_year(tmp_path, archived_cache):
    eco, fake = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    token = CHART_REQUESTS.set([])
    try:
        await eco.tools[1].handler({"groups": "outdoor", "chart": True,
                                    "start_date": f"{today - timedelta(days=540)} 00:00:00",
                                    "end_date": f"{today - timedelta(days=1)} 00:00:00"})   # settled: no request in the small hours
        spec = CHART_REQUESTS.get()[0]
    finally:
        CHART_REQUESTS.reset(token)
    line = spec["series"][0]
    assert fake.calls == [], fake.calls                          # all from the cache
    assert len(line["x"]) >= 500 and len(line["low"]) == len(line["x"])   # one point a day across the whole period
    assert line["x"] == sorted(line["x"]) and len(set(line["x"])) == len(line["x"])
    days_seen = {datetime.fromtimestamp(t, eco.tz).date() for t in line["x"]}
    assert len(days_seen) == len(line["x"])                      # never two points for one day
    assert all(lo <= y <= hi for lo, y, hi in zip(line["low"], line["y"], line["high"]))
    await eco.close()


async def test_averages_come_with_the_answer(tmp_path, archived_cache):
    eco, _ = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    span = {"groups": "outdoor", "start_date": f"{today - timedelta(days=9)} 00:00:00", "end_date": f"{today - timedelta(days=2)} 23:59:59"}
    plain = json.loads(await eco.tools[1].handler(span))
    assert "average" not in plain["series"]["outdoor.temperature"]         # highs and lows are the default
    week = json.loads(await eco.tools[1].handler({**span, "average": True}))
    temp = week["series"]["outdoor.temperature"]
    days = list(temp["daily"].values())
    assert float(temp["average"]) == pytest.approx(sum(float(d["avg"]) for d in days) / len(days), abs=0.1)
    assert float(temp["low"]) <= float(temp["average"]) <= float(temp["high"])
    assert all(float(d["low"]) <= float(d["avg"]) <= float(d["high"]) and "_sum" not in d for d in days)
    year = json.loads(await eco.tools[1].handler({"groups": "outdoor", "start_date": f"{today - timedelta(days=200)} 00:00:00",
                                                  "end_date": f"{today - timedelta(days=2)} 23:59:59", "average": True}))
    months = year["series"]["outdoor.temperature"]["monthly"]
    assert all("avg" in m and "_sum" not in m for m in months.values()) and "average" in year["series"]["outdoor.temperature"]
    await eco.close()


async def test_an_average_question_gets_a_caption_that_leads_with_the_average_and_a_daily_range_chart(tmp_path, archived_cache):
    from lib.charts import AVERAGE_ASKED, AVERAGE_CHART_HINT
    eco, _ = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    args = {"groups": "outdoor,indoor", "start_date": f"{today - timedelta(days=90)} 00:00:00",
            "end_date": f"{today - timedelta(days=2)} 23:59:59"}
    hints = {}
    for asked in (False, True):
        chart, avg = CHART_REQUESTS.set([]), AVERAGE_ASKED.set(asked)
        try:
            out = json.loads(await eco.tools[1].handler(args))
            spec = CHART_REQUESTS.get()[0]
        finally:
            AVERAGE_ASKED.reset(avg)
            CHART_REQUESTS.reset(chart)
        hints[asked] = out["chart"]
        assert ("average" in out["series"]["outdoor.temperature"]) is asked      # highs and lows by default
        assert "daily averages, range shaded" in spec["subtitle"] and len(spec["series"][0]["x"]) > 80   # 90 days: a point a day
    assert hints[True] == AVERAGE_CHART_HINT and hints[False] != AVERAGE_CHART_HINT
    await eco.close()


async def test_asking_for_an_average_uses_daily_points_even_for_a_short_period(tmp_path, archived_cache):
    from lib.charts import AVERAGE_ASKED
    eco, _ = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    args = {"groups": "outdoor", "start_date": f"{today - timedelta(days=6)} 00:00:00",
            "end_date": f"{today - timedelta(days=2)} 23:59:59", "chart": True}   # five days: intraday unless an average was asked
    subtitles = {}
    for asked in (False, True):
        chart, avg = CHART_REQUESTS.set([]), AVERAGE_ASKED.set(asked)
        try:
            await eco.tools[1].handler(args)
            subtitles[asked] = CHART_REQUESTS.get()[0]["subtitle"]
        finally:
            AVERAGE_ASKED.reset(avg)
            CHART_REQUESTS.reset(chart)
    assert "daily averages" not in subtitles[False] and "daily averages, range shaded" in subtitles[True]
    await eco.close()


async def test_resolution_follows_the_length_of_the_period(tmp_path):
    """Nothing cached, so every request shows what the planner chose. Longer periods use daily records, not weeks of 5-minute data."""
    fmt = "%Y-%m-%d %H:%M:%S"
    seen = {}
    for days in (5, 20, 60, 300):
        transport, fake = ecowitt_transport()
        (tmp_path / str(days)).mkdir()
        eco = Ecowitt(config(tmp_path / str(days)), transport=transport)
        await eco.start()
        today = datetime.now(eco.tz).date()
        await eco.tools[1].handler({"groups": "outdoor", "start_date": f"{today - timedelta(days=days)} 00:00:00",
                                    "end_date": f"{today - timedelta(days=2)} 23:59:59"})
        calls = [c for c in fake.calls if c.get("path") == "history"]
        seen[days] = {c["cycle_type"]: sum(1 for x in calls if x["cycle_type"] == c["cycle_type"]) for c in calls}
        long_five = [c for c in calls if c["cycle_type"] == "5min"
                     and datetime.strptime(c["end_date"], fmt) - datetime.strptime(c["start_date"], fmt) > timedelta(days=1)]
        assert not long_five                                           # 5-minute data only ever a day at a time
        await eco.close()
    assert set(seen[20]) <= {"30min", "5min"} and "1day" not in seen[20]  # a month or less: 30-minute weeks
    assert "1day" in seen[60] and seen[60].get("30min", 0) <= 3        # longer: daily records (+ the newest days, + refinements)
    assert seen[300]["1day"] >= 1 and seen[300].get("30min", 0) <= 6   # not a year of 30-minute weeks


def test_the_heatmap_steps_follow_the_length_of_the_period_and_never_wrap_at_north():
    from lib.ecowitt.direction import grid, sector
    from tests.fakes import TZ
    first = datetime(2026, 1, 1, 0, 0)
    for days, unit, step in ((2, "hour", 3600), (20, "6 hours", 21600), (100, "day", 86400), (300, "week", 604800)):
        g = grid([], TZ, first, first + timedelta(days=days))
        assert g["unit"] == unit and g["step"] == step and 1 <= len(g["columns"]) <= 130, days
    assert sector(359) == sector(1) == sector(0) == 0 and sector(11.2) == 0 and sector(11.3) == 1 and sector(348.8) == 0
    base = int(datetime(2026, 1, 1, 12, tzinfo=TZ).timestamp())
    g = grid([(base, 359.0, True), (base + 60, 1.0, True), (base + 7200, 180.0, True)], TZ, first, first + timedelta(days=2))
    assert g["columns"][12][0] == 2 and g["columns"][14][8] == 1 and sum(map(sum, g["columns"])) == 3


def test_the_direction_heatmap_renders_for_two_days_and_a_year():
    from lib.charts import render
    from lib.ecowitt.direction import grid
    from tests.fakes import TZ
    first = datetime(2026, 1, 1)
    for days in (2, 92, 365):
        last = first + timedelta(days=days)
        readings = [(int((first + timedelta(minutes=30 * i)).replace(tzinfo=TZ).timestamp()), (i * 37) % 360, True)
                    for i in range(days * 48)]
        spec = {"kind": "direction", "title": "Wind direction", "subtitle": "test", **grid(readings, TZ, first, last)}
        assert render(spec, TZ)[:4] == b"\x89PNG"


def test_the_rose_counts_by_wind_speed_and_wraps_at_north():
    from lib.ecowitt.direction import rose
    readings = [(1, 359.0, True, 5.0), (2, 1.0, True, 15.0), (3, 0.0, True, 40.0), (4, 180.0, True, None), (5, 90.0, True, 10.0)]
    out = rose(readings)
    assert out[0] == [1, 1, 1] and out[8] == [1, 0, 0] and out[4] == [0, 1, 0] and sum(map(sum, out)) == 5


def test_the_heatmap_and_rose_render_with_and_without_speed():
    from lib.charts import render
    from lib.ecowitt.direction import grid, rose
    from tests.fakes import TZ
    first = datetime(2026, 1, 1)
    readings = [(int((first + timedelta(minutes=30 * i)).replace(tzinfo=TZ).timestamp()), (i * 37) % 360, True) for i in range(96 * 4)]
    base = {"kind": "direction", "title": "Wind direction", "subtitle": "test", **grid(readings, TZ, first, first + timedelta(days=4))}
    with_speed = {**base, "speeds": True, "speed_steps": [10, 20],
                  "rose": rose([(t, d, x, (t % 30)) for t, d, x in readings])}
    without = {**base, "speeds": False, "rose": rose([(t, d, x, None) for t, d, x in readings])}
    for spec in (with_speed, without, base):                       # a spec with no rose is still drawn (heatmap only)
        assert render(spec, TZ)[:4] == b"\x89PNG"


def test_the_rolling_range_is_the_lowest_and_highest_within_the_window():
    from lib.timeutil import rolling_range
    x = [0, 1800, 3600, 5400, 7200, 9000]                      # 30-minute readings
    low = [10, 12, 9, 15, 20, 21]
    high = [11, 13, 10, 16, 21, 22]
    lo, hi = rolling_range(x, x, low, high, 3600)              # one hour either side
    assert lo == [9, 9, 9, 9, 9, 15] and hi == [13, 16, 21, 22, 22, 22]
    lo, hi = rolling_range([3600], x, low, high, 0)            # no window: just that reading
    assert (lo, hi) == ([9], [10])
    lo, hi = rolling_range([100000], x, low, high, 1800)       # a time with no readings near it still returns something sane
    assert len(lo) == len(hi) == 1


# ---------- weather_link: rain against pressure, read together ----------
def test_rain_per_slot_comes_from_the_rise_in_the_daily_total_and_its_midnight_reset():
    from lib.ecowitt.link import rain_slots
    daily = {0: 0.0, 1800: 0.4, 3600: 0.4, 5400: 1.0, 7200: 0.2, 20000: 0.7}     # 7200 is after the reset; 20000 follows a hole
    assert rain_slots(daily) == {1800: 0.4, 3600: 0.0, 5400: 0.6, 7200: 0.2}


def synthetic_days(falling_rain: bool):
    """Ten days of 30-minute slots: pressure falls 4 hPa over 6 hours then recovers; rain falls in the last hours of
    each fall (or, when falling_rain is False, in the recovery)."""
    driver, daily, total = {}, {}, 0.0
    for i in range(10 * 48):
        t = 1_780_000_000 + i * 1800
        phase = i % 24                                       # a 12-hour cycle
        driver[t] = 1015 - (phase * 4 / 12 if phase < 12 else (24 - phase) * 4 / 12)
        wet = (8 <= phase < 12) if falling_rain else (16 <= phase < 20)
        if i % 48 == 0:
            total = 0.0
        total += 0.6 if wet else 0.0
        daily[t] = total
    return driver, daily


def test_rain_that_comes_with_a_pressure_fall_is_found_and_rain_that_does_not_is_not():
    from lib.ecowitt.link import analyse, rain_slots
    from tests.fakes import TZ
    driver, daily = synthetic_days(True)
    out = analyse(driver, rain_slots(daily), 1.0, TZ)
    fell = out["by_change_before"]["falling"]
    fell_correlation = out["correlation_change_vs_rain_next_3h"]
    assert int(fell["share_of_rain"].rstrip("%")) > 60 and fell_correlation < -0.2
    assert out["rain_events"]["count"] >= 8 and out["rain_events"]["started_after_a_fall"] >= 8
    assert out["average_level"]["during_rain"] < out["average_level"]["dry"]
    driver, daily = synthetic_days(False)                      # rain in the recovery: pressure is rising when it falls
    out = analyse(driver, rain_slots(daily), 1.0, TZ)
    assert (int(out["by_change_before"]["rising"]["share_of_rain"].rstrip("%")) >
            int(out["by_change_before"]["falling"]["share_of_rain"].rstrip("%")))     # the mirror image: rain in the recovery
    assert out["correlation_change_vs_rain_next_3h"] > fell_correlation and analyse({}, {}, 1.0, TZ) == {}


async def test_weather_link_reads_the_cache_only_and_charts_rain_under_the_reading(tmp_path, archived_cache):
    from lib.charts import render
    eco, fake = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    token = CHART_REQUESTS.set([])
    try:
        out = json.loads(await eco.tools[3].handler({"start_date": f"{today - timedelta(days=20)}",
                                                     "end_date": f"{today - timedelta(days=2)}"}))
        specs = CHART_REQUESTS.get()
    finally:
        CHART_REQUESTS.reset(token)
    assert fake.calls == [] and out["resolution"].startswith("30-minute") and out["slots"] > 500
    assert set(out["by_change_before"]) == {"falling", "steady", "rising"} and "chart" in out
    assert len(specs) == 1 and specs[0]["kind"] == "pair" and specs[0]["bottom"]["width"] == 6 * 3600
    assert render(specs[0], eco.tz)[:4] == b"\x89PNG"
    assert "error" in json.loads(await eco.tools[3].handler({"driver": "nonsense"}))
    await eco.close()


def test_the_pair_chart_renders_for_a_day_a_month_and_a_year():
    from lib.charts import render
    from lib.ecowitt.link import chart_spec
    from tests.fakes import TZ
    for days in (2, 30, 92, 365):
        first = (datetime(2026, 1, 1)).date()
        last = first + timedelta(days=days - 1)
        base = int(datetime(2026, 1, 1, tzinfo=TZ).timestamp())
        driver = {base + i * 1800: 1015 + 6 * ((i / 200) % 2 - 1) for i in range(days * 48)}
        rain = {t: (0.4 if (i // 30) % 5 == 0 else 0.0) for i, t in enumerate(driver)}
        spec = chart_spec(driver, rain, TZ, first, last, "pressure", "hPa")
        assert spec["bottom"]["width"] in (3600, 6 * 3600, 86400) and render(spec, TZ)[:4] == b"\x89PNG"
