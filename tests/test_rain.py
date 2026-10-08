

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


def test_rain_bars_of_a_rolling_period_start_at_the_bar_holding_its_start_not_at_midnight():
    """The last 24 hours, asked at 7:40pm: there were 19 hours of empty bars left of the data (which stretched every panel's axis)."""
    from datetime import date
    from zoneinfo import ZoneInfo
    from lib.rain import rain_bars
    from lib.timeutil import day_bounds
    tz = ZoneInfo("Australia/Melbourne")
    midnight = day_bounds(date(2026, 10, 7), tz)[0]
    since, until = midnight + 19 * 3600 + 40 * 60, midnight + 43 * 3600 + 40 * 60            # Wed 7:40pm to Thu 7:40pm
    bars = rain_bars({midnight + 20 * 3600 + 600: 0.4}, tz, date(2026, 10, 7), date(2026, 10, 8), until=until, since=since)
    assert bars.per == "hour" and bars.x[0] == midnight + 19 * 3600 and bars.x[-1] == midnight + 43 * 3600 and len(bars.x) == 25
    assert bars.y[1] == 0.4 and sum(bars.y) == 0.4                                            # the 8pm hour holds the rain
    assert rain_bars({}, tz, date(2026, 10, 7), date(2026, 10, 8), until=until).x[0] == midnight          # without `since`: from midnight, as before
    assert rain_bars({}, tz, date(2026, 10, 7), date(2026, 10, 8), until=until, since=midnight - 3600).x[0] == midnight   # never before the origin
    assert rain_bars({}, tz, date(2026, 10, 7), date(2026, 10, 8), until=until, since=midnight).x[0] == midnight
    weeks = rain_bars({}, tz, date(2026, 9, 27), date(2026, 10, 3), since=day_bounds(date(2026, 9, 27), tz)[0] + 7 * 3600)   # 6-hourly: the bar holding 7am
    assert weeks.per == "6 hours" and weeks.x[0] == day_bounds(date(2026, 9, 27), tz)[0] + 6 * 3600 and len(weeks.x) == 7 * 4 - 1
    by_month = rain_bars({}, tz, date(2025, 1, 1), date(2025, 4, 30), by="month", since=day_bounds(date(2025, 2, 10), tz)[0])
    assert len(by_month.x) == 4                                                               # a figure per month ignores it


def test_a_stacked_chart_over_a_rolling_day_with_no_rain_has_no_axis_left_of_its_data():
    """The chart in the report: the x range is what the lines and bars draw, and the bars no longer reach back to midnight."""
    from datetime import date
    from zoneinfo import ZoneInfo
    from lib.rain import rain_bars
    from lib.specs import Line, Panel
    from lib.timeutil import day_bounds
    tz = ZoneInfo("Australia/Melbourne")
    midnight = day_bounds(date(2026, 10, 7), tz)[0]
    since, until = midnight + 19 * 3600 + 40 * 60, midnight + 43 * 3600 + 40 * 60
    xs = list(range(since, until, 1800))
    bars = rain_bars({}, tz, date(2026, 10, 7), date(2026, 10, 8), until=until, since=since)
    panel = Panel("Humidity", "%", [Line("Outdoor", xs, [60.0 + (i % 5) for i in range(len(xs))], reading="humidity")], bars=bars, reading="humidity")
    assert min(panel.xs) == bars.x[0] and since - min(panel.xs) < 3600                       # at most the hour the data starts in
