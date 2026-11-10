"""What must not change however the code behind it is rebuilt: the shapes the model receives from every tool."""

import json
from datetime import datetime, timedelta

from lib.tools import Turn
from tests.fakes import archived_station
from tests.test_compose import composer



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
