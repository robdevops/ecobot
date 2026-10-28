import json
from datetime import timedelta

from lib.ecowitt import Ecowitt
from lib.ecowitt.api import MINUTE, cycle_seconds
from lib.tools import Turn
from tests.fakes import config, ecowitt_transport, home_assistant_transport


async def chart(eco, hours, groups="outdoor"):
    now = eco.now()
    turn = Turn()
    out = json.loads(await eco.tools[1].handler({"groups": groups, "chart": True, "start_date": f"{now - timedelta(hours=hours):%Y-%m-%d %H:%M:%S}",
                                                 "end_date": f"{now:%Y-%m-%d %H:%M:%S}"}, turn))
    return out, turn


async def home(tmp_path, cloud=False):
    kw = {"homeassistant_url": "http://ha", "homeassistant_token": "tok"}
    if not cloud:
        kw.update(ecowitt_api_key="", ecowitt_app_key="")
    ct, cloud_fake = ecowitt_transport()
    ht, ha = home_assistant_transport()
    eco = Ecowitt(config(tmp_path, **kw), ct if cloud else None, ht)
    await eco.start()
    return eco, ha, cloud_fake


def test_a_minute_is_sixty_seconds_and_the_others_are_the_clouds():
    assert cycle_seconds(MINUTE) == 60 and cycle_seconds("5min") == 300 and cycle_seconds("1day") == 86400


async def test_a_few_hours_are_drawn_a_minute_at_a_time_from_home_assistant(tmp_path):
    eco, ha, _ = await home(tmp_path)
    out, turn = await chart(eco, 3)
    (line,) = turn.charts[0].panels[0].lines
    assert 150 <= len(line.x) <= 185 and all(b - a == 60 for a, b in zip(line.x, line.x[1:]))        # three hours of minutes
    await eco.close()


async def test_a_day_is_still_five_minutes_and_the_minutes_are_not_cached(tmp_path):
    eco, ha, _ = await home(tmp_path)
    _, turn = await chart(eco, 20)
    (line,) = turn.charts[0].panels[0].lines
    assert all(b - a == 300 for a, b in zip(line.x, line.x[1:]))                                    # beyond the 8 hours: the 5-minute readings
    await eco.close()


async def test_a_cloud_only_station_has_no_minute_lines(tmp_path):
    transport, _ = ecowitt_transport()
    eco = Ecowitt(config(tmp_path), transport)
    await eco.start()
    _, turn = await chart(eco, 3)
    (line,) = turn.charts[0].panels[0].lines
    assert all(b - a == 300 for a, b in zip(line.x, line.x[1:]))
    await eco.close()


async def test_a_home_assistant_that_is_down_leaves_the_five_minute_line_from_the_cloud(tmp_path):
    eco, ha, _ = await home(tmp_path, cloud=True)
    ha.off = True
    _, turn = await chart(eco, 3)
    (line,) = turn.charts[0].panels[0].lines
    assert len(line.x) >= 20 and all(b - a == 300 for a, b in zip(line.x, line.x[1:]))
    await eco.close()
