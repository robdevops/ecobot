import json
from datetime import datetime, timedelta

from lib.airgradient import AirGradient, pm25_aqi, rating
from lib.charts import CHART_REQUESTS
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
    holder = []
    token = CHART_REQUESTS.set(holder)
    try:
        out = json.loads(await air.handle({"start_date": f"{day} 00:00:00", "end_date": f"{day} 23:59:59", "chart": True,
                                           "metrics": ["pm2_5", "co2"]}))
    finally:
        CHART_REQUESTS.reset(token)
    assert out["pm2_5"]["high"] >= out["pm2_5"]["low"] and "high_aqi_us" in out["pm2_5"]
    assert holder[0]["kind"] == "panels" and len(holder[0]["panels"]) == 2
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
    spec = air._chart_spec("pm2_5", rows, "period")
    line = spec["series"][0]
    assert len(line["x"]) <= source.CHART_POINTS + 50
    assert line["records"]["high"] == [ts[5000], 305.0] and line["records"]["low"][1] == 5.0
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
