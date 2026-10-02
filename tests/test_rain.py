

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
