"""A question's period as days (lib/ecowitt/period.py): its whole days from their day rows, the part-days at either end from their own
readings, and its records over them, each a true figure or one from averaged readings."""

import time
from datetime import date, timedelta

import pytest

from lib.ecowitt.period import period_on
from lib.storage import Storage
from lib.storage.days import utc_days
from lib.timeutil import day_bounds, local_date
from tests.fakes import MAC, TZ

DAY = 86400


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "ecobot.sqlite", TZ)
    yield s
    s.close()


def days_ago(n: int) -> date:
    return local_date(int(time.time()), TZ) - timedelta(days=n)


def at(d: date, hour: float) -> int:
    return day_bounds(d, TZ, True)[0] + int(hour * 3600)


def put(storage, cycle: str, group: str, fields: dict[str, dict[int, str]], start: int, end: int):
    data = {group: {name: {"unit": "", "list": {str(t): v for t, v in values.items()}} for name, values in fields.items()}}
    storage.writer.run(lambda w: w.put_ecowitt(MAC, cycle, [group], data, start, end), ())


def period(storage, groups: list[str], start: int, end: int, reaches_now: bool = False, minutes: bool = False):
    return storage.reader.run(lambda r: period_on(r, MAC, groups, start, end, reaches_now, minutes), ())


def test_whole_days_come_from_their_rows_and_the_parts_at_either_end_from_their_own_readings(storage):
    d1, d2, d3 = days_ago(22), days_ago(21), days_ago(20)
    values = {t: "15.0" for t in range(at(d1, 0), at(d3, 24), 300)}
    values |= {at(d1, 6): "2.0", at(d2, 15): "25.0", at(d3, 3): "5.0", at(d3, 10): "30.0"}   # the 2.0 and the 30.0 outside the period
    put(storage, "5min", "outdoor", {"temperature": values}, at(d1, 0), at(d3, 24) - 1)
    got = period(storage, ["outdoor"], at(d1, 12), at(d3, 6))["outdoor.temperature"]
    assert [(d.day, d.source) for d in got.days] == [(d1, "5min"), (d2, "5min"), (d3, "5min")]
    assert (got.days[0].lo, got.days[2].hi) == (15.0, 15.0)                          # the parts hold only their own readings
    assert (got.low.value, got.low.raw, got.low.ts, got.low.cycle, got.low.exact) == (5.0, "5.0", at(d3, 3), "5min", True)
    assert (got.high.value, got.high.ts) == (25.0, at(d2, 15))
    assert got.column.name == "temperature"


def test_the_last_day_is_its_row_when_the_period_reaches_now(storage):
    d = days_ago(20)
    put(storage, "5min", "outdoor", {"temperature": {t: "15.0" if t < at(d, 18) else "20.0" for t in range(at(d, 0), at(d, 24), 300)}},
        at(d, 0), at(d, 24) - 1)
    assert period(storage, ["outdoor"], at(d, 0), at(d, 12))["outdoor.temperature"].high.value == 15.0
    assert period(storage, ["outdoor"], at(d, 0), at(d, 12), reaches_now=True)["outdoor.temperature"].high.value == 20.0   # (today's)


def test_a_tie_goes_to_the_first_day_that_reaches_it(storage):
    d1, d2 = days_ago(21), days_ago(20)
    values = {t: "15.0" for t in range(at(d1, 0), at(d2, 24), 300)} | {at(d1, 16): "25.0", at(d2, 9): "25.0", at(d2, 14): "25.0"}
    put(storage, "5min", "outdoor", {"temperature": values}, at(d1, 0), at(d2, 24) - 1)
    got = period(storage, ["outdoor"], at(d1, 0), at(d2, 24) - 1)["outdoor.temperature"]
    assert got.high.ts == at(d1, 16) and [d.hi_ts for d in got.days] == [at(d1, 16), at(d2, 9)]


def test_a_part_with_nothing_finer_than_ecowitt_s_daily_record_takes_the_day_s_row(storage):
    d = days_ago(500)
    first, _ = utc_days(d, TZ)
    with storage.writer.transaction() as w:
        w.put(w.ecowitt_sensor(MAC, "outdoor"), "1day", [{"ts": first, "temperature": 15.0, "temperature_lo": 8.0, "temperature_hi": 22.0}])
    got = period(storage, ["outdoor"], at(d, 14), at(d, 18))["outdoor.temperature"]
    assert [(x.day, x.source) for x in got.days] == [(d, "1day")]
    assert (got.low.value, got.high.value, got.high.cycle, got.high.exact) == (8.0, 22.0, "1day", True)   # the record's own highest


def test_a_figure_from_averaged_readings_is_marked_so(storage):
    d = days_ago(200)
    start, end = at(d, 0), at(d, 24) - 1
    put(storage, "30min", "solar_and_uvi", {"solar": {t: "100.0" for t in range(start, end, 1800)} | {at(d, 13): "700.0"}}, start, end)
    put(storage, "30min", "wind", {"wind_gust": {t: "10.0" for t in range(start, end, 1800)} | {at(d, 15): "40.0"},
                                   "wind_speed": {t: "5.0" for t in range(start, end, 1800)}}, start, end)
    put(storage, "30min", "rainfall", {"daily": {t: "0.0" if t < at(d, 9) else "3.2" for t in range(start, end, 1800)},
                                       "rain_rate": {t: "0.0" for t in range(start, end, 1800)} | {at(d, 9): "6.4"}}, start, end)
    got = period(storage, ["solar_and_uvi", "wind", "rainfall"], start, end)
    assert not got["solar_and_uvi.solar"].high.exact and got["solar_and_uvi.solar"].high.cycle == "30min"   # a half hour's mean
    assert got["wind.wind_gust"].high.exact and not got["wind.wind_gust"].low.exact   # a gust's highest is the half hour's own
    assert got["rainfall.daily"].high.exact and not got["rainfall.rain_rate"].high.exact   # a counter is a reading; a rate is a mean
    put(storage, "30min", "solar_and_uvi", {"solar": {at(d, 13): "700.0"}, "solar_high": {at(d, 13): "850.0"}}, at(d, 13), at(d, 13))
    solar = period(storage, ["solar_and_uvi"], start, end)["solar_and_uvi.solar"].high
    assert (solar.value, solar.exact) == (850.0, True)                                     # Ecowitt's own highest of the half hour
