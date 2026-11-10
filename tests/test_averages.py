"""Averages weighted by time (docs/mcp-service.md 3.3): weather_history's and air_quality's average of a period count each day for the time of
it the period holds (lines.time_mean), so the part of a day at either end counts for its hours, not as a whole day, and an air day held only
as hourly averages counts for its 24 hours, not for its 24 readings against a 5-minute day's 288. Rain rate has an average; the rain
counters, which are totals, have none."""

import json
from datetime import date, datetime

from lib.airgradient import AirGradient
from lib.ecowitt.query import HistoryQuery
from lib.storage.catalogue import RAIN_COUNTERS
from lib.timeutil import local_epoch
from lib.tools import Turn
from tests.fakes import TZ, air_transport, config, fetcher_on, storage_with

NOW = datetime(2026, 10, 23, 12, 0)   # Fri 23 Oct 2026, noon: the days below are over (and none of them changes the clock)


def at(day: int, hour: float = 0, month: int = 10) -> int:
    """An epoch on a day of October 2026 (`month`), `hour` hours after its midnight."""
    return local_epoch(datetime(2026, month, day), TZ) + int(hour * 3600)


def every(seconds: int, *stretches: tuple[int, int, float]) -> dict[str, str]:
    """{epoch: value}, a reading every `seconds` through each (start, end, value) stretch [start, end), as Ecowitt's lists have them."""
    return {str(t): f"{v}" for a, b, v in stretches for t in range(a, b, seconds)}


async def test_a_part_day_at_either_end_counts_for_its_hours_and_rain_rate_has_an_average(tmp_path):
    temperature = every(300, (at(20), at(20, 18), 20.0), (at(20, 18), at(21), 10.0), (at(21), at(22), 16.0), (at(22), at(22, 6), 4.0),
                        (at(22, 6), at(23), 30.0))   # (the 20.0 and the 30.0 outside the period)
    rain_rate = every(300, (at(20), at(21), 0.0), (at(21), at(22), 1.2), (at(22), at(23), 0.0))
    rainfall = {"rain_rate": {"unit": "mm/hr", "list": rain_rate},
                **{name: {"unit": "mm", "list": every(300, (at(20), at(23), 0.0))} for name in RAIN_COUNTERS}}
    storage = storage_with(tmp_path, ("5min", ["outdoor", "rainfall"], {"outdoor": {"temperature": {"unit": "℃", "list": temperature}},
                                                                        "rainfall": rainfall}, at(20), at(23) - 1))
    q = HistoryQuery(fetcher_on(storage, None, ["outdoor", "rainfall"]),
                     {"start_date": "2026-10-20 18:00:00", "end_date": "2026-10-22 05:59:59", "average": True}, Turn())
    q.now = NOW
    series = json.loads(await q.run())["series"]
    temp = series["outdoor.temperature"]
    assert {day: row["avg"] for day, row in temp["daily"].items()} == {"Tue 20 Oct": "10.0", "Wed 21 Oct": "16.0", "Thu 22 Oct": "4.0"}
    assert temp["average"] == "13.0"   # (6 h × 10.0 + 24 h × 16.0 + 6 h × 4.0) / 36 h; the mean of the three days' means would be 10.0
    assert series["rainfall.rain_rate"]["average"] == "0.8"   # 1.2 mm/hr through the 24 of the period's 36 hours that are Wednesday's
    assert not [name for name in RAIN_COUNTERS if "average" in series[f"rainfall.{name}"]]   # a total, not a mean
    storage.close()


def test_a_months_average_is_over_its_time_too():
    q = HistoryQuery.__new__(HistoryQuery)
    q.f, q.tz = fetcher_on(None), TZ
    q.start, q.end = datetime(2026, 9, 29, 18), datetime(2026, 10, 31, 23, 59, 59)
    q.span = q.end - q.start   # (over a month: by month)
    entry = {"monthly": {"Sep 2026": {"low": "6.0", "high": "30.0"}, "Oct 2026": {"low": "12.0", "high": "12.0"}}}
    means = {date(2026, 9, 29): 30.0, date(2026, 9, 30): 6.0} | {date(2026, 10, d): 12.0 for d in range(1, 32)}
    q._add_averages(entry, means)
    assert entry["monthly"]["Sep 2026"]["avg"] == "10.8"   # (6 h × 30.0 + 24 h × 6.0) / 30 h, not (30.0 + 6.0) / 2
    assert entry["monthly"]["Oct 2026"]["avg"] == "12.0"
    assert entry["average"] == "12.0"   # 9,240 / 773 hours (4 October has 23): 11.95; the mean of the 33 days' means would be 12.4


async def test_an_air_day_held_only_as_hourly_averages_counts_for_its_hours_and_a_day_s_average_is_its_day_row_s(tmp_path):
    transport, _ = air_transport()
    air = AirGradient(config(tmp_path), transport=transport)
    await air.start()
    air.now = lambda: NOW
    await air.store.save_day(date(2026, 10, 20), [{"ts": t, "pm2_5": 8.0} for t in range(at(20), at(21), 3600)])   # the cloud's older history
    await air.store.save_day(date(2026, 10, 21), [{"ts": t, "pm2_5": 20.0} for t in range(at(21), at(22), 300)])
    both = await air.history("2026-10-20 00:00:00", "2026-10-21 23:59:59")
    assert both["readings"] == 24 + 288 and "hourly averages" in both["note_resolution"]
    assert both["pm2_5"]["average"] == 14.0   # a day each: (8.0 + 20.0) / 2; the mean of the 312 readings would be 19.1
    parts = await air.history("2026-10-20 12:00:00", "2026-10-21 05:59:59")
    assert parts["pm2_5"]["average"] == 12.0  # (12 h × 8.0 + 6 h × 20.0) / 18 h; the readings' mean would be 18.3

    sparse = [{"ts": t, "pm2_5": 10.0} for t in range(at(22), at(22, 12), 300)] + [{"ts": t, "pm2_5": 16.0} for t in range(at(22, 12), at(23), 1800)]
    await air.store.save_day(date(2026, 10, 22), sparse)   # the afternoon held at one reading a half hour
    day = await air.history("2026-10-22 00:00:00", "2026-10-22 23:59:59")
    row = (await air.store.day_figures(date(2026, 10, 22), date(2026, 10, 22), ["pm2_5_avg"])).values["pm2_5_avg"][date(2026, 10, 22)]
    assert day["pm2_5"]["average"] == round(row, 1) == 13.0   # each half hour's mean, then theirs; the readings' mean would be 10.9
    await air.close()
