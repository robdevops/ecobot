"""Local time across daylight saving: Melbourne skips 2am to 3am on 4 Oct 2026 and has 2am to 3am twice on 5 Apr 2026."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from lib.timeutil import local_epoch, naive_local, parse_local, seconds_until

TZ = ZoneInfo("Australia/Melbourne")


def test_seconds_until_a_clock_time_are_real_seconds_across_a_change():
    assert seconds_until(18, 0, TZ, datetime(2026, 10, 3, 18, 0)) == 23 * 3600            # the day the clocks go forward
    assert seconds_until(1, 30, TZ, datetime(2026, 10, 4, 1, 40)) == (22 * 60 + 50) * 60  # the archive's nightly run
    assert seconds_until(18, 0, TZ, datetime(2026, 4, 4, 18, 0)) == 25 * 3600             # the day they go back
    assert seconds_until(18, 0, TZ, datetime(2026, 9, 8, 17, 0)) == 3600                  # an ordinary day


def test_a_naive_local_time_keeps_which_of_the_twice_happening_hours_it_is():
    second = int(datetime(2026, 4, 5, 2, 30, fold=1, tzinfo=TZ).timestamp())
    assert local_epoch(naive_local(second, TZ), TZ) == second


def test_a_date_with_an_offset_is_read_as_the_local_time_it_is():
    assert parse_local("2026-10-01T00:00:00Z", TZ) == datetime(2026, 10, 1, 10, 0)        # UTC midnight: 10am in Melbourne
    assert parse_local("2026-12-01T00:00:00+00:00", TZ) == datetime(2026, 12, 1, 11, 0)   # 11am in daylight saving
    assert parse_local("2026-10-01 14:30:00", TZ) == datetime(2026, 10, 1, 14, 30) and parse_local(date(2026, 10, 1), TZ) == datetime(2026, 10, 1)


def test_ecowitts_daily_records_say_when_they_run_in_local_time():
    from lib.ecowitt.api import daily_run
    assert daily_run(TZ, date(2026, 6, 1), date(2026, 8, 31)) == "10am to 10am"
    assert daily_run(TZ, date(2026, 12, 1), date(2027, 2, 28)) == "11am to 11am"
    assert daily_run(TZ, date(2026, 8, 1), date(2026, 11, 30)) == "10am to 10am (11am to 11am in daylight saving)"


async def test_the_last_hours_are_real_hours_the_night_the_clocks_go_forward(tmp_path):
    from lib.ecowitt import Ecowitt
    from tests.fakes import config
    eco = Ecowitt(config(tmp_path))
    asked = []

    async def readings(cycle, start, end, groups=None):
        asked.append((start, end))
        return []
    eco.readings, eco.now = readings, (lambda: datetime(2026, 10, 4, 4, 0))
    await eco.recent(3)
    await eco.close()
    assert asked == [(datetime(2026, 10, 4, 0, 0), datetime(2026, 10, 4, 4, 0))]   # three real hours: 2am to 3am never happened
