import json
from datetime import datetime, timedelta

from lib.airgradient import AirGradient, pm25_aqi, rating
from lib.tools import Turn
from tests.fakes import TZ, air_transport, config


async def make(tmp_path):
    transport, calls = air_transport()
    air = AirGradient(config(tmp_path), transport=transport)
    await air.start()
    return air, calls


def test_aqi_and_ratings_follow_the_us_scale():
    assert pm25_aqi(9.0) == (50, "good")
    assert pm25_aqi(55.5)[0] == 151 and pm25_aqi(55.5)[1] == "unhealthy"
    assert rating("pm2_5", 5).endswith("good") and rating("pm2_5", 55.5).endswith("very poor")


async def test_current_reading_with_ratings(tmp_path):
    air, _ = await make(tmp_path)
    reading = json.loads(await air.handle({}))
    assert reading["pm2_5"]["value"] == 12.0 and reading["pm2_5"]["band"] == "moderate"
    assert "_time_utc" not in reading and reading["pm2_5"]["rating"].endswith("poor")
    await air.close()


async def test_history_summary_and_chart(tmp_path):
    air, _ = await make(tmp_path)
    day = datetime.now(TZ).date() - timedelta(days=2)
    turn = Turn()
    out = json.loads(await air.handle({"start_date": f"{day} 00:00:00", "end_date": f"{day} 23:59:59", "chart": True,
                                       "metrics": ["pm2_5", "co2"]}, turn))
    assert out["pm2_5"]["high"] >= out["pm2_5"]["low"] and "high_aqi_us" in out["pm2_5"]
    (chart,) = turn.charts
    assert chart.title == "Air quality" and [p.label for p in chart.panels] == ["PM2.5", "CO₂"]   # the metrics asked for, in panel order
    await air.close()


async def test_finished_days_are_fetched_once(tmp_path):
    air, calls = await make(tmp_path)
    await air.warm(True)
    n = len(calls)
    day = datetime.now(TZ).date() - timedelta(days=3)
    await air.handle({"start_date": f"{day} 00:00:00", "end_date": f"{day} 23:59:59"})
    assert len(calls) == n  # already warmed
    await air.close()


async def test_warm_summary_and_wants(tmp_path):
    air, _ = await make(tmp_path)
    assert (await air.warm(True)).startswith("AirGradient ") and (await air.warm(True)).endswith(" req")
    assert air.wants("how's the air?") and not air.wants("what's the temperature")
    await air.close()


async def test_history_is_capped_at_14_days(tmp_path):
    air, _ = await make(tmp_path)
    out = json.loads(await air.handle({"start_date": "2026-01-01 00:00:00", "chart": False, "end_date": "2026-03-01 00:00:00"}))
    assert "note_period" in out or out.get("error") is None
    await air.close()


# ---------- the whole history is cached ----------
from datetime import timezone  # noqa: E402

from lib.airgradient import source  # noqa: E402
import httpx  # noqa: E402


async def test_finished_days_survive_a_restart_and_need_no_requests(tmp_path):
    air, calls = await make(tmp_path)
    day = datetime.now(TZ).date() - timedelta(days=3)
    args = {"start_date": f"{day} 00:00:00", "end_date": f"{day} 23:59:59"}
    first = json.loads(await air.handle(args))
    assert calls.count("past") == 1
    await air.close()
    dead = httpx.MockTransport(lambda r: httpx.Response(500))  # AirGradient is unreachable now
    again = AirGradient(config(tmp_path), transport=dead)
    assert json.loads(await again.handle(args)) == first
    assert again.requests == 0
    await again.close()


async def test_backfill_walks_back_to_the_start_of_the_sensors_data(tmp_path):
    transport, calls = air_transport(oldest=datetime.now(timezone.utc) - timedelta(days=10))
    air = AirGradient(config(tmp_path), transport=transport)
    fetched, failed, total = await air.backfill(pace=0, empty_stop=3)
    assert failed == 0 and fetched >= 10 and 10 <= total <= fetched
    assert calls.count("past") <= 3  # nine days per request, not one
    n = len(calls)
    assert await air.backfill(pace=0, empty_stop=3) == (0, 0, total)  # nothing left to fetch
    assert len(calls) == n
    # and a long question is now answered entirely from the cache
    out = json.loads(await air.handle({"start_date": (datetime.now(TZ) - timedelta(days=9)).strftime("%Y-%m-%d %H:%M:%S"),
                                       "end_date": (datetime.now(TZ) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")}))
    assert out["readings"] > 100 and len(calls) == n and "note_missing" not in out
    await air.close()


async def test_backfill_stops_after_repeated_failures(tmp_path):
    air = AirGradient(config(tmp_path), transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    fetched, failed, total = await air.backfill(pace=0)
    assert (fetched, total) == (0, 0)
    # each failed request covers nine days; eight in the small hours, when yesterday is not finished yet
    assert source.BACKFILL_FAIL_STOP * (source.MAX_REQUEST_DAYS - 1) <= failed <= source.BACKFILL_FAIL_STOP * source.MAX_REQUEST_DAYS
    await air.close()


async def test_missing_days_are_fetched_inline_only_up_to_a_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(source, "MAX_INLINE_DAYS", 3)
    air, calls = await make(tmp_path)
    out = json.loads(await air.handle({"start_date": (datetime.now(TZ) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"),
                                       "end_date": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")}))
    assert calls.count("past") <= 3 and "note_missing" in out and out["readings"] > 0  # one window, plus today (and yesterday, in the small hours)
    await air.close()


async def test_long_charts_are_averaged_but_keep_the_true_peak(tmp_path):
    air, _ = await make(tmp_path)
    ts = list(range(1_780_000_000, 1_780_000_000 + 40 * 86400, 60))  # 40 days at 1-minute readings
    rows = [{"ts": t, "pm2_5": 5.0 + (300.0 if t == ts[5000] else 0.0)} for t in ts]
    line = air._chart(["pm2_5"], rows, "period").panels[0].lines[0]
    assert len(line.x) <= 500
    assert line.records["high"] == (ts[5000], 305.0) and "low" not in line.records          # PM's lowest is often 0: not marked
    rows = [{"ts": t, "co2": 450.0 + (300.0 if t == ts[5000] else 0.0)} for t in ts]
    assert air._chart(["co2"], rows, "period").panels[0].lines[0].records["low"][1] == 450.0   # CO2's lowest is
    await air.close()


async def test_bucketed_air_lines_carry_a_band_and_the_peak_label_sits_on_its_top(tmp_path):
    from lib.charts import _extreme
    air, _ = await make(tmp_path)
    ts = list(range(1_780_000_000, 1_780_000_000 + 40 * 86400, 60))
    rows = [{"ts": t, "pm2_5": 5.0 + (300.0 if t == ts[5000] else 0.0)} for t in ts]
    line = air._chart(["pm2_5"], rows, "period").panels[0].lines[0]          # 40 days: 4-hour buckets
    assert line.low is not None and max(line.y) < 305.0 and max(line.high) == 305.0
    assert _extreme(line, "high", range(len(line.x)))[1] == 305.0   # the label is the true peak, at the band's top
    five = [{"ts": ts[0] + i * 300, "pm2_5": 5.0 + i % 7, "co2": 450.0 + i % 11} for i in range(288)]   # a day of 5-minute readings
    assert air._chart(["pm2_5"], five, "period").panels[0].lines[0].low is None                            # the readings themselves: no band
    rows = [dict(r, co2=450.0 + (t - ts[7000]) // 60 % 97) for r, t in zip(rows, ts)]
    co2 = air._chart(["co2"], rows, "period").panels[0].lines[0]
    assert co2.low is not None and _extreme(co2, "low", range(len(co2.x)))[1] == min(co2.low)
    await air.close()


def await_days(air, days_ago):
    """Readings stored for the local day this many days ago (0 if none or not fetched)."""
    day = datetime.now(TZ).date() - timedelta(days=days_ago)
    return air.store.day_count(day) or 0


# ---------- what the API's docs promise ----------
def longest_request(transport) -> timedelta:
    return max(end - start for start, end in transport.spans)


async def test_requests_never_ask_for_more_than_ten_days(tmp_path):
    transport, calls = air_transport(oldest=datetime.now(timezone.utc) - timedelta(days=60))
    air = AirGradient(config(tmp_path), transport=transport)
    await air.backfill(pace=0, empty_stop=3)
    assert 2 <= calls.count("past") <= 10 and longest_request(transport) <= timedelta(days=10)
    day = datetime.now(TZ) - timedelta(days=200)
    out = json.loads(await air.handle({"start_date": day.strftime("%Y-%m-%d %H:%M:%S"),
                                       "end_date": (day + timedelta(days=25)).strftime("%Y-%m-%d %H:%M:%S")}))
    assert "error" not in out and longest_request(transport) <= timedelta(days=10)  # 422s would have failed the answer
    await air.close()


async def test_no_data_available_is_an_empty_period_not_a_failure(tmp_path):
    transport, calls = air_transport(oldest=datetime.now(timezone.utc) + timedelta(days=5))  # every request gets a 404
    air = AirGradient(config(tmp_path), transport=transport)
    fetched, failed, total = await air.backfill(pace=0, empty_stop=5, empty_before_data=5)
    assert failed == 0 and fetched >= 5 and total == 5
    day = datetime.now(TZ).date() - timedelta(days=3)
    out = json.loads(await air.handle({"start_date": f"{day} 00:00:00", "end_date": f"{day} 23:59:59"}))
    assert out["note"] == "No readings for this period." and "error" not in out
    await air.close()


async def test_answers_say_when_old_days_are_hourly_averages(tmp_path):
    transport, _ = air_transport(rows_per_day=288, hourly_before=datetime.now(timezone.utc) - timedelta(days=4))
    air = AirGradient(config(tmp_path), transport=transport)
    now = datetime.now(TZ)
    fmt = "%Y-%m-%d %H:%M:%S"
    out = json.loads(await air.handle({"start_date": (now - timedelta(days=9)).strftime(fmt),
                                       "end_date": (now - timedelta(days=1)).strftime(fmt)}))
    assert "hourly averages" in out["note_resolution"] and out["note_resolution"].startswith(("4 ", "5 ", "6 "))
    recent = json.loads(await air.handle({"start_date": (now - timedelta(days=3)).strftime(fmt),
                                          "end_date": (now - timedelta(days=1)).strftime(fmt)}))
    assert "note_resolution" not in recent
    await air.close()



async def test_a_recent_outage_does_not_hide_older_history(tmp_path):
    """The sensor was offline for the last 40 days: the empty run comes before any data, so the backfill
    keeps looking instead of concluding that the history starts here."""
    now = datetime.now(timezone.utc)
    transport, calls = air_transport(oldest=now - timedelta(days=100), outage=(now - timedelta(days=40), now + timedelta(days=1)))
    air = AirGradient(config(tmp_path), transport=transport)
    fetched, failed, total = await air.backfill(pace=0, empty_stop=10, empty_before_data=80)
    assert failed == 0
    with_data = [d for d in range(1, 110) if (await_days(air, d)) > 0]
    assert len(with_data) >= 50 and max(with_data) >= 95      # the days before the outage were found and cached
    await air.close()


async def test_a_gap_in_the_middle_shorter_than_the_limit_does_not_stop_the_backfill(tmp_path):
    now = datetime.now(timezone.utc)
    transport, _ = air_transport(oldest=now - timedelta(days=100), outage=(now - timedelta(days=50), now - timedelta(days=15)))
    air = AirGradient(config(tmp_path), transport=transport)
    await air.backfill(pace=0, empty_stop=40, empty_before_data=80)      # a 35-day gap, under the limit
    with_data = [d for d in range(1, 110) if await_days(air, d) > 0]
    assert max(with_data) >= 95
    await air.close()


async def test_a_gap_after_data_longer_than_the_limit_is_taken_as_the_start(tmp_path):
    now = datetime.now(timezone.utc)
    transport, _ = air_transport(oldest=now - timedelta(days=100), outage=(now - timedelta(days=50), now - timedelta(days=15)))
    air = AirGradient(config(tmp_path), transport=transport)
    await air.backfill(pace=0, empty_stop=20, empty_before_data=80)      # a 35-day gap, over the limit
    with_data = [d for d in range(1, 110) if await_days(air, d) > 0]
    assert max(with_data) < 20                                            # stopped at the gap
    await air.close()


async def test_air_charts_too_long_for_the_point_budget_are_bucketed_and_carry_a_range(tmp_path):
    air, _ = await make(tmp_path)
    now = datetime.now(TZ)
    fmt = "%Y-%m-%d %H:%M:%S"

    async def specs(days_back, metrics):
        turn = Turn()
        await air.handle({"start_date": (now - timedelta(days=days_back)).strftime(fmt), "end_date": now.strftime(fmt),
                          "chart": True, "metrics": metrics}, turn)
        return turn.charts
    short = (await specs(3, ["pm2_5"]))[0].panels[0].lines[0]
    assert len(short.x) > 20 and short.low is None                                # a few days: the readings themselves, no band
    week = (await specs(30, ["pm2_5"]))[0]
    line = week.panels[0].lines[0]
    assert "-hour averages" in week.subtitle and "range shaded" in week.subtitle and 150 <= len(line.x) <= 500
    assert line.low is not None and set(line.records) == {"high"}
    base = int(now.timestamp()) // 3600 * 3600
    hourly = [{"ts": base - i * 3600, "pm2_5": 5 + (i % 24) / 2} for i in range(120 * 24, 0, -1)]   # 120 days of hourly readings
    season = air._chart(["pm2_5"], hourly, "the period")
    line = season.panels[0].lines[0]
    assert "daily averages, range shaded" in season.subtitle and 110 <= len(line.x) <= 122
    assert all(lo <= y <= hi for lo, y, hi in zip(line.low, line.y, line.high)) and set(line.records) == {"high"}
    panels = (await specs(30, ["pm2_5", "co2"]))[0]
    assert len(panels.panels) == 2 and all(p.lines[0].records for p in panels.panels)
    from lib.charts import render
    assert render(panels, TZ)[:4] == b"\x89PNG" and render(week, TZ)[:4] == b"\x89PNG"
    await air.close()
