import asyncio
import json
from datetime import datetime, timedelta

import pytest

from lib import intent
from lib.tools import Turn
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
    turn = Turn()
    out = json.loads(await eco.tools[1].handler(
        {"groups": "outdoor,indoor", "chart": True, "start_date": f"{day} 00:00:00", "end_date": f"{day} 23:59:59"}, turn))
    assert "chart" in out and len(turn.charts[0].panels) == 1 and {s.label for s in turn.charts[0].panels[0].lines} == {"Outdoor", "Indoor"}


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
    turn = Turn()
    out = await eco.tools[1].handler({**args, "chart": True}, turn)
    spec = turn.charts[0]
    after = monthly(out)
    assert len(after) >= 6 and all({"low_when", "low_date", "high_when", "high_date"} <= set(m) for m in after.values())
    assert "monthly_note" not in json.loads(out) and fake.calls == []
    assert "daily averages, range shaded" in spec.subtitle      # 200 days: a point a day from the cached 30-minute data
    line = spec.panels[0].lines[0]
    assert len(line.x) > 150 and len(line.low) == len(line.x)
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
    again = json.loads(await eco.tools[1].handler({"groups": "wind", "chart": True, "include_derived": [],   # a chart of direction alone
                                                   "start_date": f"{today - timedelta(days=3)} 00:00:00",
                                                   "end_date": f"{today - timedelta(days=2)} 23:59:59"}, Turn()))
    assert "wind.wind_direction" in again["series"]
    await eco.close()


async def test_the_wind_chart_carries_the_compass_beside_the_speed_line(tmp_path, archived_cache):
    from lib.charts import render
    eco, _ = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    args = {"groups": "wind", "chart": True, "start_date": f"{today - timedelta(days=6)} 00:00:00",
            "end_date": f"{today - timedelta(days=2)} 23:59:59"}
    turn = Turn()
    out = json.loads(await eco.tools[1].handler(args, turn))
    specs = turn.charts
    assert len(specs) == 1 and specs[0].compass and "compass" in out["chart"]     # one image: speed line and compass
    spec = specs[0]
    line = spec.panels[0].lines[0]
    assert spec.title == "Wind" and "shaded up to the gusts" in spec.subtitle and line.low
    assert "records marked" not in spec.subtitle and spec.compass.speeds is True
    rose = spec.compass.rose
    assert len(rose) == 16 and sum(rose[0]) == sum(map(sum, rose)) > 0                  # the fake wind swings 350, 0, 10: all in N
    assert sum(r[0] for r in rose) == 0 and sum(r[1] for r in rose) > 0 and sum(r[2] for r in rose) > 0   # 10 km/h and 25 km/h
    png = render(spec, eco.tz)
    assert png[:4] == b"\x89PNG"
    (tmp_path / "wind.png").write_bytes(png)
    await eco.close()


def test_calm_readings_are_reported_not_counted():
    from lib.ecowitt.direction import summarise
    from tests.fakes import TZ
    windy = [(1_780_000_000 + i * 300, 90.0, True) for i in range(80)]
    out = summarise(windy, TZ, by_day=False, calm=20)
    assert out["most_common"].startswith("E ") and out["calm"].startswith("20%")


async def test_a_chart_asked_for_in_the_persons_words_is_drawn_even_if_the_model_says_chart_false(tmp_path, archived_cache):
    eco, _ = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    short = {"groups": "wind", "chart": False, "start_date": f"{today - timedelta(days=3)} 00:00:00",
             "end_date": f"{today - timedelta(days=2)} 23:59:59"}   # two days
    long = {**short, "start_date": f"{today - timedelta(days=5)} 00:00:00"}   # four days: always a chart
    for args, asked, expected in ((short, False, 0), (short, True, 1), (long, False, 1)):     # one wind chart: line and compass
        turn = Turn(chart_asked=asked)
        await eco.tools[1].handler(args, turn)
        assert len(turn.charts) == expected
    await eco.close()


async def test_the_chart_plots_the_field_the_question_is_about(tmp_path):
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    today = datetime.now(eco.tz).date()
    base = {"groups": "outdoor", "start_date": f"{today - timedelta(days=8)} 00:00:00",
            "end_date": f"{today - timedelta(days=2)} 23:59:59"}
    async def chart_of(asked_field=None, **extra):
        turn = Turn(chart_field=asked_field)                                   # asked_field: from the person's words
        await eco.tools[1].handler({**base, **extra}, turn)
        return turn.charts[0]
    assert (await chart_of()).title == "Temperature"                       # the default
    assert (await chart_of(chart_field="wind_gust", groups="wind")).title == "Wind"
    assert (await chart_of(chart_field="Nonsense")).title == "Temperature"  # unknown: fall back, never fail
    assert (await chart_of("wind_gust", groups="outdoor,wind", chart_field="temperature")).title == "Wind"   # the words beat the model
    await eco.close()


async def test_a_chart_is_bucketed_to_the_point_budget_and_every_bucket_carries_a_range(tmp_path):
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    today = datetime.now(eco.tz).date()

    async def spec_for(days_back):
        turn = Turn()
        await eco.tools[1].handler({"groups": "outdoor", "chart": True,
                                    "start_date": f"{today - timedelta(days=days_back)} 00:00:00",
                                    "end_date": f"{today - timedelta(days=1)} 23:59:59"}, turn)
        return turn.charts[0]
    ten = await spec_for(9)                                        # nine days of 30-minute readings fit the point budget: the readings, each with its own low and high
    assert "range shaded" in ten.subtitle and ten.panels[0].lines[0].low is not None
    fortnight = await spec_for(20)                                 # twenty days: hourly averages, with their range
    line = fortnight.panels[0].lines[0]
    assert "hourly averages, range shaded" in fortnight.subtitle
    assert 300 < len(line.x) <= 500 and all(lo <= y <= hi for lo, y, hi in zip(line.low, line.y, line.high))
    season = (await spec_for(90)).panels[0].lines[0]               # ninety days: a point a day, each with its low and high
    assert 80 <= len(season.x) <= 92 and len(season.low) == len(season.high) == len(season.y)
    assert all(lo <= y <= hi for lo, y, hi in zip(season.low, season.y, season.high))
    few = await spec_for(4)                                        # four days: the readings themselves, each with its own low and high
    line = few.panels[0].lines[0]
    assert "range shaded" in few.subtitle and len(line.x) > 100 and line.low is not None
    await eco.close()


async def test_a_multi_year_chart_uses_cached_30_minute_data_for_the_newest_year(tmp_path, archived_cache):
    eco, fake = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    turn = Turn()
    await eco.tools[1].handler({"groups": "outdoor", "chart": True,
                                "start_date": f"{today - timedelta(days=540)} 00:00:00",
                                "end_date": f"{today - timedelta(days=1)} 00:00:00"}, turn)   # settled: no request in the small hours
    spec = turn.charts[0]
    line = spec.panels[0].lines[0]
    assert fake.calls == [], fake.calls                          # all from the cache
    assert len(line.x) >= 500 and len(line.low) == len(line.x)   # one point a day across the whole period
    assert line.x == sorted(line.x) and len(set(line.x)) == len(line.x)
    days_seen = {datetime.fromtimestamp(t, eco.tz).date() for t in line.x}
    assert len(days_seen) == len(line.x)                         # never two points for one day
    assert all(lo <= y <= hi for lo, y, hi in zip(line.low, line.y, line.high))
    await eco.close()


async def test_the_wind_chart_is_the_average_speed_shaded_up_to_the_gusts(tmp_path, archived_cache):
    eco, _ = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()

    async def wind_chart(days, **extra):
        turn = Turn()
        await eco.tools[1].handler({"groups": "wind", "chart": True, "chart_field": "wind_gust",
                                    "start_date": f"{today - timedelta(days=days)} 00:00:00",
                                    "end_date": f"{today - timedelta(days=1)} 23:59:59", **extra}, turn)
        return turn.charts[0]
    raw = await wind_chart(4)                                       # readings themselves: the band is speed up to gust
    line = raw.panels[0].lines[0]
    assert raw.title == "Wind" and line.label == "Wind" and "shaded up to the gusts" in raw.subtitle
    assert all(lo <= y <= hi for lo, y, hi in zip(line.low, line.y, line.high))
    assert any(hi > y + 0.5 for y, hi in zip(line.y, line.high)) and set(line.records) == {"high"}   # gusts above the speed
    season = (await wind_chart(90)).panels[0].lines[0]              # daily: the day's lull to its peak gust
    assert len(season.low) == len(season.high) == len(season.y) and all(lo <= y <= hi for lo, y, hi in zip(season.low, season.y, season.high))
    assert max(season.high) >= max(line.high) * 0.9
    await eco.close()


async def test_weather_now_carries_the_rain_outlook_when_it_is_raining_or_likely(tmp_path, monkeypatch):
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    now = int(datetime.now(eco.tz).timestamp())
    wet = [(now - 300 * i, {"rainfall.rain_rate": 1.5, "rainfall.daily": 2.0}) for i in range(2, -1, -1)]
    dry = [(now - 300 * i, {"rainfall.rain_rate": 0.0, "rainfall.daily": 2.0}) for i in range(2, -1, -1)]
    rows = {"value": wet}

    async def recent(hours):
        return rows["value"]
    monkeypatch.setattr(eco, "recent", recent)
    out = json.loads(await eco.tools[0].handler({"groups": "outdoor,rainfall"}))
    assert out["rain_outlook"] == "raining now (1.5 mm/h)"
    rows["value"] = dry
    assert "rain_outlook" not in json.loads(await eco.tools[0].handler({"groups": "outdoor,rainfall"}))
    rows["value"] = wet
    assert "rain_outlook" not in json.loads(await eco.tools[0].handler({"groups": "outdoor"}))     # no rain group asked for
    await eco.close()


def test_the_rolling_mean_smooths_a_staircase_without_moving_timestamps_or_crossing_gaps():
    from lib.timeutil import rolling_mean
    assert rolling_mean({0: 5.0, 300: 5.0, 600: 5.0}, 450) == {0: 5.0, 300: 5.0, 600: 5.0}
    assert rolling_mean({0: 1.0, 300: float('nan'), 600: 3.0}, 450) == {0: 1.0, 600: 3.0}   # a bad reading is left out, it does not poison the rest
    stairs = {i * 300: 10 + 0.1 * (i // 3) + (0.1 if i % 2 else 0.0) for i in range(30)}                 # 0.1-degree steps with jitter
    out = rolling_mean(stairs, 450)
    assert list(out) == list(stairs)                                       # the same timestamps
    turns = lambda d: sum(1 for a, b, c in zip(list(d.values()), list(d.values())[1:], list(d.values())[2:]) if (b - a) * (c - b) < 0)
    assert turns(out) < turns(stairs)
    assert out[0] == (stairs[0] + stairs[300]) / 2                         # the ends use the shorter window
    gap = rolling_mean({0: 0.0, 300: 0.0, 3600: 10.0, 3900: 10.0}, 450)         # an hour apart: neither side pulls on the other
    assert gap == {0: 0.0, 300: 0.0, 3600: 10.0, 3900: 10.0}


async def test_a_5_minute_temperature_line_is_lightly_smoothed_and_its_records_stay_raw(tmp_path, archived_cache, monkeypatch):
    from lib.ecowitt import query as history
    eco, _ = await archived_station(tmp_path, archived_cache)
    day = datetime.now(eco.tz).date() - timedelta(days=2)
    args = {"groups": "outdoor", "chart": True, "start_date": f"{day} 00:00:00", "end_date": f"{day} 23:59:59"}

    async def chart():
        turn = Turn()
        out = json.loads(await eco.tools[1].handler(args, turn))
        return out, turn.charts[0]
    out, smooth = await chart()
    monkeypatch.setattr(history, "SMOOTH_SERIES", set())
    _, raw = await chart()
    a, b = smooth.panels[0].lines[0], raw.panels[0].lines[0]
    assert "5-minute" in smooth.subtitle and a.x == b.x and a.y != b.y
    assert max(a.y) <= max(b.y) + 1e-6 and min(a.y) >= min(b.y) - 1e-6         # smoothing never goes past the readings
    assert a.smoothed and not b.smoothed and a.y[-1] == b.y[-1]          # the end dot is the latest reading, as it was
    assert a.records == b.records and a.records["high"][1] == float(out["series"]["outdoor.temperature"]["high"].split()[0])
    await eco.close()


async def test_a_long_period_is_read_from_the_cache_at_30_minutes_when_it_is_held_and_fits_the_row_budget(tmp_path, archived_cache, monkeypatch):
    from lib.ecowitt import query as history
    seen = []
    real = history.HistoryQuery._chunks

    async def spy(self, cycle, t, until):
        seen.append(cycle)
        await real(self, cycle, t, until)
    monkeypatch.setattr(history.HistoryQuery, "_chunks", spy)
    eco, fake = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    args = {"groups": "outdoor", "start_date": f"{today - timedelta(days=200)} 00:00:00", "end_date": f"{today - timedelta(days=1)} 00:00:00"}
    await eco.tools[1].handler(args)
    assert set(seen) == {"30min"} and fake.calls == []            # held: no daily records, no requests
    seen.clear()
    monkeypatch.setattr(history, "MAX_ROWS", 1000)                # too big to load: back to daily records
    await eco.tools[1].handler(args)
    assert "1day" in seen
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
    from lib.charts import AVERAGE_CHART_HINT
    eco, _ = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    args = {"groups": "outdoor,indoor", "start_date": f"{today - timedelta(days=90)} 00:00:00",
            "end_date": f"{today - timedelta(days=2)} 23:59:59"}
    hints = {}
    for asked in (False, True):
        turn = Turn(average_asked=asked)
        out = json.loads(await eco.tools[1].handler(args, turn))
        spec = turn.charts[0]
        hints[asked] = out["chart"]
        assert ("average" in out["series"]["outdoor.temperature"]) is asked      # highs and lows by default
        assert "daily averages, range shaded" in spec.subtitle and len(spec.panels[0].lines[0].x) > 80   # 90 days: a point a day
    assert hints[True] == AVERAGE_CHART_HINT and hints[False] != AVERAGE_CHART_HINT
    await eco.close()


async def test_asking_for_an_average_uses_daily_points_even_for_a_short_period(tmp_path, archived_cache):
    eco, _ = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    args = {"groups": "outdoor", "start_date": f"{today - timedelta(days=6)} 00:00:00",
            "end_date": f"{today - timedelta(days=2)} 23:59:59", "chart": True}   # five days: intraday unless an average was asked
    subtitles = {}
    for asked in (False, True):
        turn = Turn(average_asked=asked)
        await eco.tools[1].handler(args, turn)
        subtitles[asked] = turn.charts[0].subtitle
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


def test_the_rose_counts_by_wind_speed_and_wraps_at_north():
    from lib.ecowitt.direction import rose, sector
    assert sector(359) == sector(1) == sector(0) == 0 and sector(11.2) == 0 and sector(11.3) == 1 and sector(348.8) == 0
    readings = [(1, 359.0, True, 5.0), (2, 1.0, True, 15.0), (3, 0.0, True, 40.0), (4, 180.0, True, None), (5, 90.0, True, 10.0)]
    out = rose(readings)
    assert out[0] == [1, 1, 1] and out[8] == [1, 0, 0] and out[4] == [0, 1, 0] and sum(map(sum, out)) == 5


def test_the_wind_line_and_compass_render_with_and_without_speed():
    from lib.charts import render
    from lib.ecowitt.direction import rose
    from lib.specs import Chart, Compass, Line, Panel
    from tests.fakes import TZ
    first = datetime(2026, 1, 1)
    readings = [(int((first + timedelta(minutes=30 * i)).replace(tzinfo=TZ).timestamp()), (i * 37) % 360, True) for i in range(96 * 4)]
    x = [t for t, _, _ in readings]
    y = [10 + 5 * ((i % 48) / 48) for i in range(len(x))]
    def chart(compass=None):
        wind = Line("Wind", x, y, y, [v + 8 for v in y], records={"high": (x[5], y[5] + 8)})
        return Chart("Wind", "test", [Panel("Wind", "km/h", [wind])], compass)
    with_speed = chart(Compass(rose([(t, d, e, (t % 30)) for t, d, e in readings]), True, (10, 20)))
    without = chart(Compass(rose([(t, d, e, None) for t, d, e in readings]), False))
    for spec in (with_speed, without, chart()):                    # a chart with no compass is just the line
        assert render(spec, TZ)[:4] == b"\x89PNG"


# ---------- weather_link: rain against pressure, read together ----------
def test_rain_per_slot_comes_from_the_rise_in_the_daily_total_and_its_midnight_reset():
    from lib.rain import rain_slots
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


def test_the_verdict_is_a_plain_call_from_moving_against_steady():
    from lib.analysis.pairs import verdict
    assert verdict(1.4, 0.6).startswith("Yes - a moderate") and verdict(1.8, 0.4).startswith("Yes - a clear")
    assert verdict(1.0, 1.0).startswith("No") and verdict(1.3, 0.95).startswith("Weak") and verdict(None, 0.5) is None


def test_rain_that_comes_with_a_pressure_fall_is_found_and_rain_that_does_not_is_not():
    from lib.analysis.pairs import analyse
    from lib.rain import rain_slots
    from tests.fakes import TZ
    driver, daily = synthetic_days(True)
    out = analyse(driver, rain_slots(daily), 1.0, TZ)
    fell = out["by_change_before"]["falling"]
    fell_correlation = out["correlation_change_vs_rain_next_3h"]
    assert int(fell["share_of_rain"].rstrip("%")) > 60 and fell_correlation < -0.2
    assert out["rain_events"]["count"] >= 8 and out["rain_events"]["started_after_a_fall"] >= 8
    assert out["average_level"]["during_rain"] < out["average_level"]["dry"]
    assert float(fell["rain_vs_fair_share"].rstrip("x")) > 1.5                # the ratio the answer leads with
    assert any("lower level" in f or "during rain vs" in f for f in out["findings"]) and \
        any("began after a fall" in f for f in out["findings"]) and any("fair share" in f for f in out["findings"])
    assert any(f.startswith("Moving either way") and "x; steady:" in f for f in out["findings"])
    assert any("the biggest was" in f for f in out["findings"]) and "raining in" in out["findings"][0]
    assert out["verdict"].startswith("Yes")
    driver, daily = synthetic_days(False)                      # rain in the recovery: pressure is rising when it falls
    out = analyse(driver, rain_slots(daily), 1.0, TZ)
    assert (int(out["by_change_before"]["rising"]["share_of_rain"].rstrip("%")) >
            int(out["by_change_before"]["falling"]["share_of_rain"].rstrip("%")))     # the mirror image: rain in the recovery
    assert out["correlation_change_vs_rain_next_3h"] > fell_correlation and analyse({}, {}, 1.0, TZ) == {}


async def test_weather_link_reads_the_cache_only_and_charts_rain_under_the_reading(tmp_path, archived_cache):
    from lib.charts import render
    eco, fake = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    turn = Turn()
    out = json.loads(await eco.tools[3].handler({"start_date": f"{today - timedelta(days=20)}",
                                                 "end_date": f"{today - timedelta(days=2)}"}, turn))
    specs = turn.charts
    assert fake.calls == [] and out["resolution"].startswith("30-minute") and out["slots"] > 500
    assert set(out["by_change_before"]) == {"falling", "steady", "rising"} and "chart" in out
    assert len(specs) == 1 and specs[0].title == "Pressure and Rain" and specs[0].panels[0].bars.width == 6 * 3600
    assert render(specs[0], eco.tz)[:4] == b"\x89PNG"
    assert "error" in json.loads(await eco.tools[3].handler({"driver": "nonsense"}))
    await eco.close()


def test_the_pair_chart_band_is_ecowitts_own_lows_and_highs_where_the_cache_has_them():
    from lib.ecowitt.link import driver_series
    from tests.fakes import TZ
    first, last = datetime(2026, 1, 1).date(), datetime(2026, 4, 15).date()      # 105 days: a point a day
    base = int(datetime(2026, 1, 1, tzinfo=TZ).timestamp())
    driver = {base + i * 1800: 1015.0 for i in range(105 * 48)}
    lows = {t: v - 2.5 for t, v in driver.items()}
    highs = {t: v + 1.5 for t, v in driver.items()}
    with_true = driver_series(driver, TZ, first, last, "Pressure", lows, highs)
    assert with_true.low[0] == 1012.5 and with_true.high[0] == 1016.5 and with_true.y[0] == 1015.0
    without = driver_series(driver, TZ, first, last, "Pressure")               # no lows or highs cached: the readings' own range
    assert without.low[0] == without.high[0] == 1015.0


def test_a_days_mean_counts_a_stretch_held_at_5_minutes_no_more_than_the_same_stretch_at_30():
    from lib.lines import build_line
    from tests.fakes import TZ
    day = (datetime.now(TZ) - timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
    t0 = int(day.timestamp())
    pts = {t0 + i * 1800: {"cycle": "30min", "value": (10.0, "10"), "low": (8.0, "8"), "high": (12.0, "12")} for i in range(48)}
    pts.update({t0 + 20 * 3600 + i * 300: {"cycle": "5min", "value": (20.0, "20")} for i in range(48)})   # the last 4 hours also at 5 minutes
    readings = [(t, r["value"][0], r["value"][0] if "low" not in r else r["low"][0], r["value"][0] if "high" not in r else r["high"][0],
                 300 if r["cycle"] == "5min" else 1800) for t, r in pts.items()]
    line = build_line(readings + [(t0 - 86400, 5.0, 4.0, 6.0, 1800), (t0 + 86400, 5.0, 4.0, 6.0, 1800)], TZ, 3 * 86400, force_daily=True)
    assert line.y[1] == pytest.approx((40 * 10 + 8 * 20) / 48) and (line.low[1], line.high[1]) == (8.0, 20.0)


def test_the_bucket_widens_as_the_period_grows_and_never_goes_below_the_readings():
    from lib.timeutil import bucket_width
    day = 86400
    assert [bucket_width(d * day, 300) for d in (1, 1.7, 2)] == [300, 300, 1800]      # 5-minute readings, until they exceed the budget
    assert [bucket_width(d * day, 1800) for d in (2, 10, 11, 20, 21, 41, 42, 83, 84, 1460)] == [
        1800, 1800, 3600, 3600, 7200, 7200, 14400, 14400, 86400, 86400]
    assert bucket_width(2 * day, 3600) == 3600 and bucket_width(day, 86400) == 86400   # hourly or daily source: never finer


def test_bucket_summary_is_the_mean_and_true_extremes_per_epoch_aligned_bucket():
    from lib.timeutil import bucket_summary, bucketed
    from tests.fakes import TZ
    t0 = int(datetime(2026, 5, 1, tzinfo=TZ).timestamp()) // 7200 * 7200
    points = [(t0 + i * 1800, float(i), i - 0.5, i + 0.5, False) for i in range(8)]    # two 2-hour buckets of four 30-minute readings
    out = bucket_summary(points, 7200)
    assert out == {t0: (1.5, -0.5, 3.5), t0 + 7200: (5.5, 3.5, 7.5)}
    xs, mean, low, high = bucketed(points, TZ, 7200)
    assert xs == [t0 + 3600, t0 + 10800] and mean == [1.5, 5.5] and low == [-0.5, 3.5] and high == [3.5, 7.5]


def test_daily_summary_is_one_mean_and_true_extremes_per_local_day():
    from lib.timeutil import daily_summary
    from tests.fakes import TZ
    t0 = int(datetime(2026, 5, 1, tzinfo=TZ).timestamp())
    day1 = [(t0 + i * 1800, 10.0, 8.0, 12.0, False) for i in range(48)]
    day1 += [(t0 + 20 * 3600 + i * 300, 20.0, 20.0, 20.0, True) for i in range(48)]       # the last 4 hours also at 5 minutes
    day2 = [(t0 + 86400 + i * 1800, 5.0, 5.0, 5.0, False) for i in range(48)]              # no separate lows/highs: the value
    out = daily_summary(day1 + day2, TZ)
    assert set(out) == {datetime(2026, 5, 1).date(), datetime(2026, 5, 2).date()}
    assert out[datetime(2026, 5, 1).date()] == pytest.approx(((40 * 10 + 8 * 20) / 48, 8.0, 20.0))
    assert out[datetime(2026, 5, 2).date()] == (5.0, 5.0, 5.0) and daily_summary([], TZ) == {}


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
        assert spec.panels[0].bars.width in (3600, 6 * 3600, 86400) and render(spec, TZ)[:4] == b"\x89PNG"


async def test_weather_link_says_which_series_is_missing(tmp_path):
    transport, fake = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)          # nothing cached at all
    await eco.start()
    today = datetime.now(eco.tz).date()
    out = json.loads(await eco.tools[3].handler({"start_date": str(today - timedelta(days=9)), "end_date": str(today - timedelta(days=2))}))
    assert "pressure (pressure.relative) or rainfall (rainfall.daily)" in out["note"] and "chart" not in out
    await eco.close()


async def test_any_readings_can_be_plotted_together_one_panel_each(tmp_path, archived_cache):
    from lib.charts import render
    eco, fake = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    args = {"groups": "outdoor", "start_date": f"{today - timedelta(days=20)} 00:00:00",
            "end_date": f"{today - timedelta(days=2)} 23:59:59", "chart": True}

    async def ask_chart(asked_stack=(), **extra):
        turn = Turn(chart_fields=list(asked_stack))                                  # asked_stack: from the person's words
        out = json.loads(await eco.tools[1].handler({**args, **extra}, turn))
        return out, turn.charts
    out, specs = await ask_chart(chart_fields=["temperature", "rain"])              # the model's choice; rain's group is added for it
    assert len(specs) == 1 and specs[0].title == "Temperature and Rain" and [p.label for p in specs[0].panels] == ["Temperature"]
    assert specs[0].panels[0].bars and out["rain_total_mm"] > 0 and "rain_total_mm" in out["chart"]     # the rain is behind the line
    assert render(specs[0], eco.tz)[:4] == b"\x89PNG" and fake.calls == []            # cache only
    out, specs = await ask_chart(chart_fields=["pressure", "humidity", "wind", "rain", "temperature"], groups="outdoor,indoor")
    assert [p.label for p in specs[0].panels][0] == "Pressure" and len(specs[0].panels) >= 3
    assert specs[0].panels[0].bars                                                     # rain goes behind the first line
    temperature = next(p for p in specs[0].panels if p.label == "Temperature")
    assert [s.label for s in temperature.lines] == ["Indoor", "Outdoor"]              # both lines in one panel
    assert render(specs[0], eco.tz)[:4] == b"\x89PNG"
    out, specs = await ask_chart(["rain", "wind"])
    assert specs[0].title == "Rain and Wind" and [p.label for p in specs[0].panels] == ["Wind"] and specs[0].panels[0].bars
    out, specs = await ask_chart(chart_fields=["temperature"])                        # one reading: the usual chart
    assert len(specs[0].panels) == 1 and not specs[0].panels[0].bars
    await eco.close()


def test_current_readings_get_a_hot_cold_wet_windy_emoji_from_their_values():
    from lib.ecowitt.glance import glance
    assert [glance("outdoor", "temperature", t) for t in (38, 31, 24, 16, 9, 3, -2)] == ["🔥", "🥵", "😎", "🙂", "🧥", "🥶", "🧊"]
    assert glance("outdoor", "temperature", 15.9) == "🧥" and glance("outdoor", "temperature", 16) == "🙂"       # happy from 16 outside
    assert glance("indoor", "temperature", 19.9) == "🧥" and glance("indoor", "temperature", 20) == "🙂"          # and from 20 inside
    assert [glance("indoor", "temperature", t) for t in (36, 29, 25, 21, 17, 12, 8)] == ["🔥", "🥵", "😎", "🙂", "🧥", "🥶", "🧊"]
    assert glance("outdoor", "humidity", 90) == "💦" and glance("outdoor", "humidity", 20) == "🏜️" and glance("outdoor", "humidity", 55) == ""
    assert [glance("wind", "wind_speed", v) for v in (60, 35, 20, 5)] == ["🌪️", "💨", "🍃", ""]
    assert glance("rainfall", "rain_rate", 1.2) == "🌧️" and glance("rainfall", "rain_rate", 0) == ""
    assert glance("rainfall", "daily", 3.0) == "☔" and glance("rainfall", "daily", 0) == "" and glance("pressure", "relative", 1010) == ""


async def test_weather_now_carries_the_emoji_next_to_the_readings(tmp_path):
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    out = json.loads(await eco.tools[0].handler({"groups": "outdoor"}))
    assert out["emoji"] == {"outdoor.temperature": "🧥"}                       # the fake station reports 12.3 degrees
    await eco.close()


async def test_rain_on_its_own_is_drawn_as_daily_columns_not_as_the_running_counter(tmp_path):
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    today = datetime.now(eco.tz).date()
    turn = Turn()
    out = json.loads(await eco.tools[1].handler({"groups": "rainfall", "chart": True,
                                                 "start_date": f"{today - timedelta(days=9)} 00:00:00",
                                                 "end_date": f"{today - timedelta(days=1)} 23:59:59"}, turn))
    (chart,) = turn.charts
    assert chart.title == "Rain" and chart.panels[0].bars and not chart.panels[0].lines and "rain per" in chart.subtitle
    assert "rain_total_mm" in out
    await eco.close()


async def test_a_dew_point_chart_plots_the_dew_point_and_never_quietly_a_temperature(tmp_path):
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    today = datetime.now(eco.tz).date()
    turn = Turn(chart_field="dew_point")
    out = json.loads(await eco.tools[1].handler({"groups": "outdoor", "chart": True,
                                                 "start_date": f"{today - timedelta(days=9)} 00:00:00",
                                                 "end_date": f"{today - timedelta(days=1)} 23:59:59"}, turn))
    assert turn.charts and turn.charts[0].title == "Dew point" and "outdoor.dew_point" in out["series"]
    await eco.close()


async def test_weather_all_week_is_a_stack_of_every_reading_the_station_has(tmp_path):
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport=transport)
    await eco.start()
    today = datetime.now(eco.tz).date()
    turn = Turn(chart_asked=True, chart_fields=intent.chart_fields("weather all week"))
    await eco.tools[1].handler({"groups": "outdoor,indoor", "chart": True, "start_date": f"{today - timedelta(days=7)} 00:00:00",
                                "end_date": f"{today - timedelta(days=1)} 23:59:59"}, turn)
    chart = turn.charts[0]
    assert len(chart.panels) >= 3 and {"Temperature", "Dew point"} <= {p.label for p in chart.panels}
    await eco.close()


def test_a_reading_named_as_a_group_becomes_its_group_and_an_unknown_name_is_dropped():
    from lib.ecowitt.station import parse_groups
    assert parse_groups("pressure,humidity") == ["pressure", "outdoor"]
    assert parse_groups("rain, dew_point, wind, outdoor") == ["rainfall", "outdoor", "wind"]
    assert parse_groups("outdoor.temperature,bogus") == ["outdoor"] and parse_groups("bogus") == ["outdoor", "indoor"]
    assert parse_groups(["indoor", "rainfall_piezo"]) == ["indoor", "rainfall_piezo"]


async def test_a_job_stops_asking_once_ecowitt_has_refused_twice(tmp_path):
    from lib.ecowitt.api import EcowittError
    from lib.ecowitt.fetch import Fetcher
    from tests.fakes import TZ
    from lib.ecowitt.store import HistoryCache, HotStore

    class Refusing:
        calls = 0

        async def history(self, *a):
            Refusing.calls += 1
            raise EcowittError("humidity is invalid", transient=False)

    cache = HistoryCache(tmp_path / "c.sqlite", {})
    f = Fetcher(Refusing(), cache, HotStore(), "AA:BB", ["pressure"], TZ)
    for day in range(6):
        await f.get("30min", datetime(2026, 9, 1 + day), datetime(2026, 9, 1 + day, 23, 59))
    assert Refusing.calls == 2 and f.rejected == 2 and len(f.errors) == 2
