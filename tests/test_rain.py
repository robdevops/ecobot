

def test_rain_bars_include_the_dry_stretches_so_the_chart_covers_the_whole_period():
    from datetime import date
    from zoneinfo import ZoneInfo
    from lib.rain import rain_bars
    from lib.timeutil import day_bounds
    tz = ZoneInfo("Australia/Melbourne")
    start = day_bounds(date(2026, 9, 27), tz)[0]
    bars = rain_bars({start + 2 * 86400 + 3600: 1.5}, tz, date(2026, 9, 27), date(2026, 10, 3))
    assert bars.per == "6 hours" and len(bars.x) == 7 * 4 and sum(bars.y) == 1.5 and bars.x[0] == start
    assert rain_bars({}, tz, date(2026, 9, 27), date(2026, 9, 27)).y == [0.0] * 24
    assert len(rain_bars({}, tz, date(2026, 9, 27), date(2026, 10, 3), until=start + 86400).x) == 4


def test_rain_bars_by_month_or_year_are_one_bar_each_with_dry_ones_as_0():
    from datetime import date
    from zoneinfo import ZoneInfo
    from lib.rain import rain_bars
    from lib.timeutil import day_bounds
    tz = ZoneInfo("Australia/Melbourne")
    t = lambda d: day_bounds(d, tz)[0] + 3600
    rain = {t(date(2025, 1, 5)): 2.0, t(date(2025, 1, 20)): 3.0, t(date(2025, 3, 2)): 4.5}
    months = rain_bars(rain, tz, date(2025, 1, 1), date(2025, 4, 30), by="month")
    assert months.y == [5.0, 0.0, 4.5, 0.0] and months.per == "month" and months.values
    years = rain_bars(rain, tz, date(2024, 6, 1), date(2025, 12, 31), by="year")
    assert years.y == [0.0, 9.5] and years.per == "year"
    assert rain_bars(rain, tz, date(2025, 1, 1), date(2025, 4, 30)).per == "day"          # without by: the usual layout
