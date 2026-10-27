import json
from datetime import datetime, timedelta

from lib.ecowitt.live import BUCKET, LiveRanges, readings_in
from lib.lines import RANGE_KEEP_DAYS
from lib.tools import Turn
from tests.fakes import archived_station

T = 1_800_000_000 // BUCKET * BUCKET


def live(temperature, when, humidity=None) -> dict:
    data = {"outdoor": {"temperature": {"value": str(temperature), "unit": "℃", "time": str(when)}},
            "rainfall": {"rain_rate": {"value": "0.0", "time": str(when)}, "daily": {"value": "1.0", "time": str(when)}},
            "wind": {"wind_direction": {"value": "90", "time": str(when)}}}
    if humidity is not None:
        data["outdoor"]["humidity"] = {"value": str(humidity), "unit": "%", "time": str(when)}
    return data


def test_only_the_readings_a_chart_can_shade_are_taken_from_a_live_response():
    got = readings_in(live(12.5, T, humidity=70))
    assert sorted(got) == [("outdoor.humidity", T, 70.0), ("outdoor.temperature", T, 12.5)]      # no rain rate, rain counter or wind direction
    assert readings_in({"outdoor": {"temperature": {"value": "n/a"}}, "indoor": "x"}) == []


def test_readings_fold_into_their_five_minutes_and_a_range_needs_two_of_them(tmp_path):
    ranges = LiveRanges(tmp_path / "live.sqlite")
    assert ranges.add(live(12.0, T - 100)) == 1 and ranges.add(live(12.0, T - 100)) == 0          # the same reading again is not another
    assert ranges.between("outdoor.temperature", T - BUCKET, T + BUCKET) == {}                    # one reading: no range yet
    ranges.add(live(13.5, T + 60))
    ranges.add(live(11.0, T + 120))
    assert ranges.between("outdoor.temperature", T - BUCKET, T + BUCKET) == {T: (11.0, 13.5)}      # 100 s before and 60, 120 s after the mark
    ranges.add(live(20.0, T + 400))                                                               # nearer the next mark
    assert ranges.between("outdoor.temperature", T, T) == {T: (11.0, 13.5)} and ranges.between("outdoor.temperature", T + BUCKET, T + BUCKET) == {}
    ranges.add(live(21.0, T + 420))
    assert ranges.between("outdoor.temperature", T + BUCKET, T + BUCKET) == {T + BUCKET: (20.0, 21.0)}
    ranges.close()


def test_what_is_older_than_the_chart_ranges_reach_is_dropped_and_the_ranges_survive_a_restart(tmp_path):
    path = tmp_path / "live.sqlite"
    ranges = LiveRanges(path)
    old = T - (RANGE_KEEP_DAYS + 1) * 86400
    ranges.add(live(5.0, old), now=old)
    ranges.add(live(6.0, old + 60), now=old + 60)
    assert ranges.between("outdoor.temperature", old, old + 600)
    ranges.add(live(1.0, T), now=T)
    ranges.add(live(2.0, T + 60), now=T + 60)
    assert not ranges.between("outdoor.temperature", old, old + 600) and ranges.between("outdoor.temperature", T, T)
    ranges.close()
    assert LiveRanges(path).between("outdoor.temperature", T, T) == {T: (1.0, 2.0)}


async def chart_of_the_last_day(eco):
    now = datetime.now(eco.tz)
    args = {"groups": "outdoor", "start_date": f"{(now - timedelta(days=1)):%Y-%m-%d %H:%M:%S}", "end_date": f"{now:%Y-%m-%d %H:%M:%S}", "chart": True}
    turn = Turn()
    await eco.tools[1].handler(args, turn)
    return turn.charts[0].panels[0].lines[0]


async def test_a_chart_of_5_minute_readings_is_shaded_between_the_lows_and_highs_the_bot_saw(tmp_path, archived_cache):
    eco, _ = await archived_station(tmp_path, archived_cache)
    bare = await chart_of_the_last_day(eco)
    assert bare.low is None                                                       # nothing was recorded: a clean line, as before
    now = int(datetime.now(eco.tz).timestamp())
    for mark in range((now - 2 * 86400) // BUCKET * BUCKET, now, BUCKET):         # the bot has watched the whole time
        eco.live.db.execute("INSERT INTO ranges VALUES ('outdoor.temperature', ?, 4, 10.0, 11.0)", (mark,))
    eco.live.db.commit()
    shaded = await chart_of_the_last_day(eco)
    assert shaded.low and len(shaded.low) == len(shaded.x) == len(shaded.high)
    assert all(lo <= y <= hi for lo, y, hi in zip(shaded.low, shaded.y, shaded.high))
    assert any(hi - lo >= 1.0 for lo, hi in zip(shaded.low, shaded.high))
    await eco.close()


async def test_the_station_keeps_the_live_readings_it_is_asked_for_and_a_failure_to_keep_them_changes_nothing(tmp_path, archived_cache, caplog):
    eco, _ = await archived_station(tmp_path, archived_cache)
    assert (await eco._live_data(["outdoor"]))["outdoor"]                         # a question's own request: the fake station's one temperature
    assert eco.live.db.execute("SELECT COUNT(*), MAX(n) FROM ranges").fetchone() == (1, 1)

    def broken(data, now=None):
        raise RuntimeError("disk full")
    eco.live.add = broken
    assert (await eco._live_data(["outdoor"]))["outdoor"] and "disk full" in caplog.text
    await eco.close()
