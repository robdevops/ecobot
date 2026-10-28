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


async def test_days_home_assistant_has_forgotten_keep_their_bands_in_a_chart_of_twenty_days(tmp_path):
    kw = {"ecowitt_api_key": "", "ecowitt_app_key": "", "homeassistant_url": "http://ha", "homeassistant_token": "tok"}
    long_memory, _ = home_assistant_transport(history_days=20)
    eco = Ecowitt(config(tmp_path, **kw), None, long_memory)
    await eco.start()
    now = eco.now()
    await eco.fetcher(["outdoor"]).get("30min", now - timedelta(days=19), now, load=False)      # what the archive copies in
    await eco.close()

    short_memory, ha = home_assistant_transport(history_days=10)                                 # ten days on, Home Assistant has forgotten the rest
    eco = Ecowitt(config(tmp_path, **kw), None, short_memory)
    await eco.start()
    _, turn = await chart(eco, 19 * 24)
    (line,) = turn.charts[0].panels[0].lines
    old = [i for i, t in enumerate(line.x) if t < eco.now().timestamp() - 12 * 86400]
    assert len(old) > 100 and line.low is not None and line.high is not None
    assert all(line.low[i] <= line.y[i] <= line.high[i] for i in old) and any(line.low[i] < line.high[i] for i in old)
    await eco.close()


async def test_home_assistant_alone_is_kept_warm_and_backfilled_like_the_cloud(tmp_path):
    from lib.ecowitt import Archive
    eco, ha, _ = await home(tmp_path)
    assert (await eco.warm(True)).startswith("Ecowitt ")
    asked = len(ha.calls)
    await eco.warm(False)
    assert len(ha.calls) == asked                                                              # warm: the newest is in memory, nothing re-asked
    fetched, failed = await Archive(eco, eco.archive_pace).run_once()
    assert fetched > 10 and not failed                                                          # a day at a time, 5-minute, then the coarser
    for cycle in ("5min", "30min", "1day"):
        assert eco.cache.days_held(eco.mac, cycle, eco.groups) >= 8, cycle                       # (the fake has ten days)
    again = len(ha.calls)
    fetched, failed = await Archive(eco, 0).run_once()                                          # what is held is not asked again; the one day before
    assert fetched <= 1 and not failed and len(ha.calls) == again                               # it began is tried (and costs no request: it has nothing)
    await eco.close()
