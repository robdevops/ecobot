"""What must not change however the code behind it is rebuilt: the shapes the model receives from every tool."""

import json
from datetime import datetime, timedelta

from lib.tools import Turn
from tests.fakes import archived_station
from tests.test_compose import composer



async def test_every_tool_result_keeps_its_shape(tmp_path, archived_cache):
    eco, _ = await archived_station(tmp_path, archived_cache)
    today = datetime.now(eco.tz).date()
    day = lambda n: f"{today - timedelta(days=n)} 00:00:00"
    span = {"start_date": day(9), "end_date": f"{today - timedelta(days=2)} 23:59:59"}
    call = lambda i, args: eco.tools[i].handler(args)
    now = json.loads(await call(0, {"groups": "outdoor,indoor,pressure,wind,rainfall"}))
    assert set(now) == {"time", "outdoor", "emoji"}
    history = json.loads(await call(1, {"groups": "outdoor", **span}))
    assert set(history) - {"chart"} == {"period", "series"}                    # "chart": a hint that appears when one was drawn
    assert set(history["series"]["outdoor.temperature"]) == {
        "unit", "low", "low_time", "low_when", "low_date", "high", "high_time", "high_when", "high_date", "daily"}
    wind = json.loads(await call(1, {"groups": "wind", **span}))
    assert set(wind["series"]) == {"wind.wind_direction", "wind.wind_gust", "wind.wind_speed"}
    assert set(wind["series"]["wind.wind_direction"]) == {"unit", "most_common", "average_direction", "steadiness", "readings", "calm", "daily"}
    days = json.loads(await call(2, {"start_date": day(30), "end_date": f"{today - timedelta(days=2)} 23:59:59", "sort_by": "temp_max"}))
    assert set(days) == {"period", "days_checked", "matching_days", "units", "days"}
    assert set(days["days"][0]) == {"date", "temp_max", "temp_min", "rain", "source"}
    link = json.loads(await call(3, {"start_date": str(today - timedelta(days=20)), "end_date": str(today - timedelta(days=2))}))
    assert set(link) - {"chart"} == {"period", "driver", "resolution", "how_to_read", "slots", "rain_mm", "wet_slots", "verdict", "findings",
                         "by_change_before", "average_level", "correlation_change_vs_rain_next_3h", "rain_events", "days_with_data"}
    await eco.close()


async def test_the_cross_source_tools_keep_their_shape(tmp_path, archived_cache):
    comp, eco = await composer(tmp_path, archived_cache)
    turn = Turn()
    plot = json.loads(await comp.plot_chart({"panels": [{"series": "pm2_5"}, {"series": "rain"}]}, turn))
    link = json.loads(await comp.air_link({"chart": True}, turn))
    scan = json.loads(await comp.air_scan({"metric": "pm2_5", "chart": True}, turn))
    assert set(plot) == {"period", "panels", "chart"}
    assert {"period", "metric", "resolution", "how_to_read", "verdict", "findings", "events", "days_compared",
            "rank_correlation_daily_rain_vs_air", "slots", "wet_slots"} <= set(link)
    assert {"period", "resolution", "how_to_read", "verdict", "findings", "pairs_tested", "pairs_standing_out"} <= set(scan)
    await eco.close()
