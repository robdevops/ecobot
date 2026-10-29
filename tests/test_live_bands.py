"""The bands of the station's 5-minute readings from the live readings the bot sees (source.LiveMarks, Writer.put_ranges), and the charts
shaded between them."""

from datetime import datetime, timedelta

from lib.ecowitt.live import BUCKET, readings_in
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
