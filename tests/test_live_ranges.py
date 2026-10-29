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
    found = {"outdoor.temperature": {mark: (4, 10.0, 11.0) for mark in range((now - 2 * 86400) // BUCKET * BUCKET, now, BUCKET)}}
    await eco.storage.write(lambda w: w.put_ranges(eco.mac, found))   # the bot has watched the whole time
    shaded = await chart_of_the_last_day(eco)
    assert shaded.low and len(shaded.low) == len(shaded.x) == len(shaded.high)
    assert all(lo <= y <= hi for lo, y, hi in zip(shaded.low, shaded.y, shaded.high))
    assert any(hi - lo >= 1.0 for lo, hi in zip(shaded.low, shaded.high))
    await eco.close()


async def test_the_station_keeps_the_live_readings_it_is_asked_for_and_a_failure_to_keep_them_changes_nothing(tmp_path, archived_cache, caplog):
    eco, _ = await archived_station(tmp_path, archived_cache)
    assert (await eco._live_data(["outdoor"]))["outdoor"]                         # a question's own request: the fake station's one temperature
    (mark,) = eco.live_marks.marks["outdoor.temperature"].values()
    assert mark[0] == 1                                                          # counted; one reading is no band yet

    def broken(data, now=None):
        raise RuntimeError("disk full")
    eco.live_marks.add = broken
    assert (await eco._live_data(["outdoor"]))["outdoor"] and "disk full" in caplog.text
    await eco.close()


def test_the_ranges_roll_up_into_wider_slots_and_whole_periods(tmp_path):
    ranges = LiveRanges(tmp_path / "live.sqlite")
    slot = T // 1800 * 1800                                                     # a half hour; its first stretch, then the next half hour's
    stretches = [(10.0, 11.0), (9.0, 12.0), (10.5, 10.9), (8.0, 14.0), (13.0, 13.5), (9.5, 10.0), (20.0, 25.0)]   # i = 0..5: the first half hour, 6: the next
    ranges.put({"s": {slot + 300 * i: (4, lo, hi) for i, (lo, hi) in enumerate(stretches)} | {slot + 3000: (1, -50.0, 99.0)}})   # (one reading only: no range)
    assert ranges.rolled("s", slot, slot + 3600, 1800) == {slot: (8.0, 14.0), slot + 1800: (20.0, 25.0)}
    assert ranges.extremes("s", slot, slot + 3600) == (8.0, 25.0) and ranges.extremes("s", slot + 4000, slot + 5000) is None
    assert ranges.rolled("s", slot + 4000, slot + 5000, 1800) == {}
    ranges.close()


async def test_a_chart_of_a_week_has_the_true_extremes_of_each_half_hour_in_its_band(tmp_path, archived_cache):
    eco, _ = await archived_station(tmp_path, archived_cache)
    now = datetime.now(eco.tz)
    args = {"groups": "outdoor", "start_date": f"{(now - timedelta(days=5)):%Y-%m-%d} 00:00:00", "end_date": f"{(now - timedelta(days=2)):%Y-%m-%d} 23:59:59",
            "chart": True}

    async def line():
        turn = Turn()
        await eco.tools[1].handler(args, turn)
        return turn.charts[0].panels[0].lines[0]
    plain = await line()
    slot = plain.x[len(plain.x) // 2]                                           # a half hour in the middle of the period
    base = dict(zip(plain.x, zip(plain.low, plain.y, plain.high)))[slot]
    found = {"outdoor.temperature": {slot // 1800 * 1800 + 300 * k: (5, base[0] - 4.0, base[2] + 6.0) for k in range(6)}}
    await eco.storage.write(lambda w: w.put_ranges(eco.mac, found))   # a spike and a dip between its 5-minute marks
    widened = await line()
    low, value, high = dict(zip(widened.x, zip(widened.low, widened.y, widened.high)))[slot]
    assert low == base[0] - 4.0 and high == base[2] + 6.0 and value == base[1]    # the range is taken out; the line is not moved
    other = [(a, b) for a, b in zip(plain.low, widened.low) if a != b]
    assert len(other) <= 2                                                       # only the slot(s) the ranges fall in
    await eco.close()


def test_the_half_hours_are_kept_rolled_up_as_ranges_come_in_and_a_file_from_before_them_is_rolled_up_once(tmp_path):
    path = tmp_path / "live.sqlite"
    ranges = LiveRanges(path)
    slot = T // 1800 * 1800
    ranges.add(live(12.0, slot + 60), now=slot + 60)
    ranges.add(live(15.0, slot + 120), now=slot + 120)                           # two readings in a stretch: it has a range, and the half hour has it
    ranges.add(live(9.0, slot + 640), now=slot + 640)
    ranges.add(live(11.0, slot + 700), now=slot + 700)                           # ... and a second stretch, lower
    assert ranges.rolled("outdoor.temperature", slot, slot, 1800) == {slot: (9.0, 15.0)}
    assert ranges.db.execute("SELECT COUNT(*) FROM slots").fetchone() == (1,)
    ranges.close()
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("DROP TABLE slots")                                               # as a file from before this was kept
    db.commit()
    db.close()
    again = LiveRanges(path)
    assert again.rolled("outdoor.temperature", slot, slot, 1800) == {slot: (9.0, 15.0)}
    again.close()
