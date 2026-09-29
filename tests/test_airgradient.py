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
    assert "request(s)" in await air.warm(True)
    assert air.wants("how's the air?") and not air.wants("what's the temperature")
    await air.close()


async def test_history_is_capped_at_14_days(tmp_path):
    air, _ = await make(tmp_path)
    out = json.loads(await air.handle({"start_date": "2026-01-01 00:00:00", "chart": False, "end_date": "2026-03-01 00:00:00"}))
    assert "note_period" in out or out.get("error") is None
    await air.close()
