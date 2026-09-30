from datetime import datetime


from lib.lines import build_line, slot_readings
from tests.fakes import TZ

T0 = int(datetime(2026, 1, 1, tzinfo=TZ).timestamp())


def readings(n, step, seconds, low=None, high=None):
    return [(T0 + i * step, float(i % 7), low, high, seconds) for i in range(n)]


def test_readings_that_fit_the_budget_are_drawn_as_they_are():
    line = build_line(readings(100, 1800, 1800), TZ, 100 * 1800)
    assert line.raw and len(line.x) == 100 and line.low is None and line.name == "30-minute readings"


def test_too_many_readings_are_bucketed_and_a_band_only_comes_with_days():
    week = build_line(readings(48 * 20, 1800, 1800), TZ, 20 * 86400)
    assert not week.raw and week.width == 3600 and week.low is None and week.name == "hourly averages"
    year = build_line(readings(48 * 120, 1800, 1800, 0.0, 9.0), TZ, 120 * 86400)
    assert year.width == 86400 and year.low is not None and year.name == "daily averages"


def test_a_native_band_is_kept_at_every_width():
    raw = build_line(slot_readings({T0 + i * 1800: 5.0 for i in range(20)}, highs={T0 + i * 1800: 8.0 for i in range(20)}), TZ,
                     20 * 1800, native_band=True)
    assert raw.raw and raw.high == [8.0] * 20
    hourly = build_line(readings(48 * 20, 1800, 1800, 1.0, 9.0), TZ, 20 * 86400, native_band=True)
    assert hourly.width == 3600 and hourly.high is not None


def test_a_slow_five_minute_line_is_smoothed_but_ends_on_the_latest_reading():
    rs = [(T0 + i * 300, 10.0 + (i % 2), None, None, 300) for i in range(50)]
    plain, smooth = build_line(rs, TZ, 50 * 300), build_line(rs, TZ, 50 * 300, smooth=True)
    assert not plain.smoothed and smooth.smoothed
    assert smooth.y[-1] == rs[-1][1] and max(smooth.y[5:-2]) - min(smooth.y[5:-2]) < 1.0


def test_force_daily_draws_a_point_a_day_and_until_leaves_out_the_unfinished_day():
    rs = readings(48 * 3, 1800, 1800)
    assert len(build_line(rs, TZ, 3 * 86400, force_daily=True).x) == 3
    assert len(build_line(rs, TZ, 3 * 86400, force_daily=True, until=datetime(2026, 1, 3).date()).x) == 2


def test_daily_records_sit_at_ten_am_next_to_exact_days_and_carry_their_own_range():
    records = [(T0 + d * 86400 + 10 * 3600, 20.0 + d, 15.0 + d, 25.0 + d, 86400) for d in range(-3, 0)]
    fine = readings(48, 1800, 1800, 12.0, 30.0)
    line = build_line(records + fine, TZ, 4 * 86400)
    assert line.width == 86400 and len(line.x) == 4 and line.low[0] == 15.0 - 3 and (line.low[-1], line.high[-1]) == (12.0, 30.0)


def test_too_little_gives_no_line():
    assert build_line(readings(1, 1800, 1800), TZ, 1800) is None
    assert build_line([], TZ, 1800) is None
