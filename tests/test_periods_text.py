"""Periods in words (lib/periods_text.py): the table of docs/ecobot-mcp.md 3.2 read at fixed times, every answer worked out by hand.
Melbourne's clocks go back an hour at 3am on 5 Apr 2026 (2am to 3am happens twice) and forward at 2am on 4 Oct 2026 (2am to 3am
never happens)."""

from datetime import datetime

import pytest

from lib.periods_text import PeriodError, read_period
from tests.fakes import TZ


def at(*when, fold: int = 0) -> datetime:
    """A time on the station's clock."""
    return datetime(*when, tzinfo=TZ, fold=fold)


def read(words, now, start=None, end=None, **kw):
    return read_period(words, start, end, now=now, tz=TZ, default="today", **kw)


def hours(period) -> float:
    """The period's length in real hours (on one clock, aware datetimes subtract as wall times)."""
    return (period.end.timestamp() - period.start.timestamp()) / 3600


def check(now, words, start, end, text, **kw):
    """Each of the words (a "|" between the ones that mean the same) reads as start to end, written as text."""
    for said in words.split("|"):
        period = read(said.strip(), now, **kw)
        assert (period.asked, period.start, period.end, period.text) == (said.strip(), at(*start), at(*end), text), said


def rows(*table):
    return pytest.mark.parametrize("words, start, end, text", table, ids=[row[0].split("|")[0].strip() for row in table])


HELD = at(2024, 3, 1, 15, 20)   # the first reading held, for "on record"

# ---------- Tue 10 Nov 2026 14:05: the doc's examples, every row of the table ----------
TUESDAY = at(2026, 11, 10, 14, 5)
NOW = (2026, 11, 10, 14, 5)


@rows(
    ("today | Today | so far today", (2026, 11, 10), NOW, "today: Tue 10 Nov 2026"),
    ("yesterday", (2026, 11, 9), (2026, 11, 10), "yesterday: Mon 9 Nov 2026"),
    ("last 30 minutes | 30 min | 30min | 30 mins | the past 30 minutes", (2026, 11, 10, 13, 35), NOW,
     "the last 30 minutes: Tue 10 Nov 2026 1:35pm – 2:05pm"),
    ("last 6 hours | 6h | 6 hrs | the past six hours", (2026, 11, 10, 8, 5), NOW, "the last 6 hours: Tue 10 Nov 2026 8:05am – 2:05pm"),
    ("last 24 hours | 24h | 1d | last day | the past day | last 1 day | in the last 24 hours", (2026, 11, 9, 14, 5), NOW,
     "the last 24 hours: Mon 9 Nov 2:05pm – Tue 10 Nov 2026 2:05pm"),
    ("last hour | the past hour | 1h", (2026, 11, 10, 13, 5), NOW, "the last hour: Tue 10 Nov 2026 1:05pm – 2:05pm"),
    ("last 7 days | 7d | 7D | seven days | the past 7 days | over the last seven days", (2026, 11, 4), NOW,
     "the last 7 days: Wed 4 – Tue 10 Nov 2026"),
    ("this week | past week | the past week | the last week | week | 1w | in the last week | This Week", (2026, 11, 4), NOW,
     "the last 7 days: Wed 4 – Tue 10 Nov 2026"),
    ("last 2 weeks | 2w | two weeks", (2026, 10, 28), NOW, "the last 2 weeks: Wed 28 Oct – Tue 10 Nov 2026"),
    ("fortnight | the last fortnight", (2026, 10, 28), NOW, "the last 14 days: Wed 28 Oct – Tue 10 Nov 2026"),
    ("past month | the last month | the past month | month | 1m | 1M | last 30 days | last 1 month", (2026, 10, 12), NOW,
     "the last 30 days: Mon 12 Oct – Tue 10 Nov 2026"),
    ("last 3 months | 3m | 3 mo | 3M | past three months", (2026, 8, 12), NOW, "the last 3 months: Wed 12 Aug – Tue 10 Nov 2026"),
    ("5m | 5 months", (2026, 6, 12), NOW, "the last 5 months: Fri 12 Jun – Tue 10 Nov 2026"),   # round(5×365/12) = 152 days: never minutes
    ("past year | the last year | in the last year | year | 1y", (2025, 11, 11), NOW, "the last 365 days: Tue 11 Nov 2025 – Tue 10 Nov 2026"),
    ("last 12 months | last twelve months", (2025, 11, 11), NOW, "the last 12 months: Tue 11 Nov 2025 – Tue 10 Nov 2026"),
    ("last 2 years | 2y | past two years | the past 2 years", (2024, 11, 11), NOW, "the last 2 years: Mon 11 Nov 2024 – Tue 10 Nov 2026"),
    ("week to date", (2026, 11, 9), NOW, "week to date: Mon 9 – Tue 10 Nov 2026"),
    ("since Monday", (2026, 11, 9), NOW, "since Monday: Mon 9 – Tue 10 Nov 2026"),
    ("last week", (2026, 11, 2), (2026, 11, 9), "last week: Mon 2 – Sun 8 Nov 2026"),
    ("this month", (2026, 11, 1), NOW, "this month: Sun 1 – Tue 10 Nov 2026"),
    ("last month", (2026, 10, 1), (2026, 11, 1), "last month: Thu 1 – Sat 31 Oct 2026"),
    ("this year", (2026, 1, 1), NOW, "this year: Thu 1 Jan – Tue 10 Nov 2026"),
    ("last year", (2025, 1, 1), (2026, 1, 1), "last year: Wed 1 Jan – Wed 31 Dec 2025"),
    ("2025", (2025, 1, 1), (2026, 1, 1), "2025: Wed 1 Jan – Wed 31 Dec 2025"),
    ("2026", (2026, 1, 1), NOW, "2026: Thu 1 Jan – Tue 10 Nov 2026"),
    ("October 2025 | Oct 2025", (2025, 10, 1), (2025, 11, 1), "October 2025: Wed 1 – Fri 31 Oct 2025"),
    ("October | oct", (2026, 10, 1), (2026, 11, 1), "October 2026: Thu 1 – Sat 31 Oct 2026"),
    ("November", (2026, 11, 1), NOW, "November 2026: Sun 1 – Tue 10 Nov 2026"),
    ("December", (2025, 12, 1), (2026, 1, 1), "December 2025: Mon 1 – Wed 31 Dec 2025"),   # the latest December that has begun
    ("June to August 2026 | between June and August 2026 | from June to August 2026 | June - August 2026", (2026, 6, 1), (2026, 9, 1),
     "June 2026 to August 2026: Mon 1 Jun – Mon 31 Aug 2026"),
    ("December to February", (2025, 12, 1), (2026, 3, 1), "December 2025 to February 2026: Mon 1 Dec 2025 – Sat 28 Feb 2026"),
    ("5 to 10 Sep | 5 Sep to 10 Sep | Sat 5 – Thu 10 Sep 2026", (2026, 9, 5), (2026, 9, 11), "Sat 5 – Thu 10 Sep 2026"),
    ("Saturday to Sunday", (2026, 11, 7), (2026, 11, 9), "Saturday to Sunday: Sat 7 – Sun 8 Nov 2026"),
    ("Monday to now", (2026, 11, 9), NOW, "Monday to now: Mon 9 – Tue 10 Nov 2026"),
    ("2026-09-05 | 5 Sep | 5 September | Sat 5 Sep | Saturday 5 September 2026 | 5th of September | September 5 | on 5 Sep",
     (2026, 9, 5), (2026, 9, 6), "Sat 5 Sep 2026"),
    ("5 September 2025 | Sep 5, 2025", (2025, 9, 5), (2025, 9, 6), "Fri 5 Sep 2025"),
    ("29 Feb", (2024, 2, 29), (2024, 3, 1), "Thu 29 Feb 2024"),   # the latest that has been
    ("Monday | last Monday | on Monday", (2026, 11, 9), (2026, 11, 10), "Monday: Mon 9 Nov 2026"),
    ("Tuesday | tue", (2026, 11, 3), (2026, 11, 4), "Tuesday: Tue 3 Nov 2026"),   # the latest before today: never today
    ("last Saturday | Saturday | sat", (2026, 11, 7), (2026, 11, 8), "Saturday: Sat 7 Nov 2026"),
    ("Christmas Day | Christmas | christmas day 2025", (2025, 12, 25), (2025, 12, 26), "Christmas Day: Thu 25 Dec 2025"),
    ("Melbourne Cup | Melbourne Cup Day | the Melbourne Cup | cup day", (2026, 11, 3), (2026, 11, 4), "Melbourne Cup Day: Tue 3 Nov 2026"),
    ("Melbourne Cup 2025", (2025, 11, 4), (2025, 11, 5), "Melbourne Cup Day: Tue 4 Nov 2025"),
    ("Christmas to New Year's Day", (2025, 12, 25), (2026, 1, 2), "Christmas Day to New Year's Day: Thu 25 Dec 2025 – Thu 1 Jan 2026"),
    ("this morning", (2026, 11, 10, 6), (2026, 11, 10, 12), "this morning: Tue 10 Nov 2026 6am – 12pm"),
    ("this afternoon", (2026, 11, 10, 12), NOW, "this afternoon: Tue 10 Nov 2026 12pm – 2:05pm"),
    ("yesterday afternoon", (2026, 11, 9, 12), (2026, 11, 9, 18), "yesterday afternoon: Mon 9 Nov 2026 12pm – 6pm"),
    ("Tuesday evening", (2026, 11, 3, 18), (2026, 11, 4), "Tuesday evening: Tue 3 Nov 2026 6pm – midnight"),
    ("Saturday morning", (2026, 11, 7, 6), (2026, 11, 7, 12), "Saturday morning: Sat 7 Nov 2026 6am – 12pm"),
    ("last night | overnight", (2026, 11, 9, 22), (2026, 11, 10, 6), "last night: Mon 9 Nov 10pm – Tue 10 Nov 2026 6am"),
    ("Tuesday night", (2026, 11, 2, 22), (2026, 11, 3, 6), "Tuesday night: Mon 2 Nov 10pm – Tue 3 Nov 2026 6am"),
    ("since 6am | since 6 am | since 06:00", (2026, 11, 10, 6), NOW, "since 6am: Tue 10 Nov 2026 6am – 2:05pm"),
    ("since 3pm | since 15:00", (2026, 11, 9, 15), NOW, "since 3pm: Mon 9 Nov 3pm – Tue 10 Nov 2026 2:05pm"),   # the latest 3pm
    ("since midnight", (2026, 11, 10), NOW, "since midnight: Tue 10 Nov 2026 12am – 2:05pm"),
    ("since 5 Sep", (2026, 9, 5), NOW, "Sat 5 Sep – Tue 10 Nov 2026"),
    ("on record | all time | all-time | ever | of all time | the whole record", (2024, 3, 1), NOW, "on record: Fri 1 Mar 2024 – Tue 10 Nov 2026"),
)
def test_every_row_of_the_table_on_a_tuesday(words, start, end, text):
    check(TUESDAY, words, start, end, text, first_held=HELD)


def test_a_period_hands_back_its_days_and_the_strings_todays_tools_take():
    week = read("last week", TUESDAY)
    assert (week.first_day, week.last_day, week.cut) == (at(2026, 11, 2).date(), at(2026, 11, 8).date(), None)
    assert week.as_naive_strings() == ("2026-11-02 00:00:00", "2026-11-08 23:59:59")
    assert read("today", TUESDAY).as_naive_strings() == ("2026-11-10 00:00:00", "2026-11-10 14:04:59")
    assert read("last night", TUESDAY).as_naive_strings() == ("2026-11-09 22:00:00", "2026-11-10 05:59:59")
    assert read_period(None, None, None, now=TUESDAY, tz=TZ, default="last 24 hours").asked == "last 24 hours"   # nothing asked: the default
    assert read_period("", " ", None, now=TUESDAY, tz=TZ, default="yesterday").text == "yesterday: Mon 9 Nov 2026"   # blanks aren't asking


# ---------- Mon 9 Nov 2026 10:00 ----------
MONDAY = at(2026, 11, 9, 10)


@rows(
    ("week to date", (2026, 11, 9), (2026, 11, 9, 10), "week to date: Mon 9 Nov 2026"),
    ("since Monday", (2026, 11, 9), (2026, 11, 9, 10), "since Monday: Mon 9 Nov 2026"),   # this Monday: today
    ("Monday | last Monday", (2026, 11, 2), (2026, 11, 3), "Monday: Mon 2 Nov 2026"),     # the latest before today
    ("Sunday", (2026, 11, 8), (2026, 11, 9), "Sunday: Sun 8 Nov 2026"),
    ("last week", (2026, 11, 2), (2026, 11, 9), "last week: Mon 2 – Sun 8 Nov 2026"),
    ("this week", (2026, 11, 3), (2026, 11, 9, 10), "the last 7 days: Tue 3 – Mon 9 Nov 2026"),
    ("Monday night", (2026, 11, 1, 22), (2026, 11, 2, 6), "Monday night: Sun 1 Nov 10pm – Mon 2 Nov 2026 6am"),
    ("Sunday evening", (2026, 11, 8, 18), (2026, 11, 9), "Sunday evening: Sun 8 Nov 2026 6pm – midnight"),
    ("Saturday to Sunday", (2026, 11, 7), (2026, 11, 9), "Saturday to Sunday: Sat 7 – Sun 8 Nov 2026"),
)
def test_on_a_monday(words, start, end, text):
    check(MONDAY, words, start, end, text)


# ---------- Sun 1 Nov 2026 14:05: the 1st of a month ----------
FIRST = at(2026, 11, 1, 14, 5)
FIRST_NOW = (2026, 11, 1, 14, 5)


@rows(
    ("this month", (2026, 11, 1), FIRST_NOW, "this month: Sun 1 Nov 2026"),
    ("last month", (2026, 10, 1), (2026, 11, 1), "last month: Thu 1 – Sat 31 Oct 2026"),
    ("November", (2026, 11, 1), FIRST_NOW, "November 2026: Sun 1 Nov 2026"),
    ("October", (2026, 10, 1), (2026, 11, 1), "October 2026: Thu 1 – Sat 31 Oct 2026"),
    ("December", (2025, 12, 1), (2026, 1, 1), "December 2025: Mon 1 – Wed 31 Dec 2025"),
    ("this week", (2026, 10, 26), FIRST_NOW, "the last 7 days: Mon 26 Oct – Sun 1 Nov 2026"),
    ("last week", (2026, 10, 19), (2026, 10, 26), "last week: Mon 19 – Sun 25 Oct 2026"),
    ("week to date", (2026, 10, 26), FIRST_NOW, "week to date: Mon 26 Oct – Sun 1 Nov 2026"),
    ("past month", (2026, 10, 3), FIRST_NOW, "the last 30 days: Sat 3 Oct – Sun 1 Nov 2026"),
    ("1 Nov", (2026, 11, 1), FIRST_NOW, "Sun 1 Nov 2026"),
    ("Sunday", (2026, 10, 25), (2026, 10, 26), "Sunday: Sun 25 Oct 2026"),
    ("since Sunday", (2026, 11, 1), FIRST_NOW, "since Sunday: Sun 1 Nov 2026"),   # since a weekday: today, if it is that day
    ("yesterday", (2026, 10, 31), (2026, 11, 1), "yesterday: Sat 31 Oct 2026"),
    ("October to November", (2026, 10, 1), FIRST_NOW, "October 2026 to November 2026: Thu 1 Oct – Sun 1 Nov 2026"),
)
def test_on_the_first_of_a_month(words, start, end, text):
    check(FIRST, words, start, end, text)


# ---------- Fri 1 Jan 2027 09:00 ----------
NEW_YEAR = at(2027, 1, 1, 9)
NEW_YEAR_NOW = (2027, 1, 1, 9)


@rows(
    ("this year", (2027, 1, 1), NEW_YEAR_NOW, "this year: Fri 1 Jan 2027"),
    ("last year", (2026, 1, 1), (2027, 1, 1), "last year: Thu 1 Jan – Thu 31 Dec 2026"),
    ("2026", (2026, 1, 1), (2027, 1, 1), "2026: Thu 1 Jan – Thu 31 Dec 2026"),
    ("this month", (2027, 1, 1), NEW_YEAR_NOW, "this month: Fri 1 Jan 2027"),
    ("last month", (2026, 12, 1), (2027, 1, 1), "last month: Tue 1 – Thu 31 Dec 2026"),
    ("December", (2026, 12, 1), (2027, 1, 1), "December 2026: Tue 1 – Thu 31 Dec 2026"),
    ("January", (2027, 1, 1), NEW_YEAR_NOW, "January 2027: Fri 1 Jan 2027"),
    ("February", (2026, 2, 1), (2026, 3, 1), "February 2026: Sun 1 – Sat 28 Feb 2026"),
    ("Christmas Day", (2026, 12, 25), (2026, 12, 26), "Christmas Day: Fri 25 Dec 2026"),
    ("New Year's Day | new years day", (2027, 1, 1), NEW_YEAR_NOW, "New Year's Day: Fri 1 Jan 2027"),
    ("last week", (2026, 12, 21), (2026, 12, 28), "last week: Mon 21 – Sun 27 Dec 2026"),
    ("this week", (2026, 12, 26), NEW_YEAR_NOW, "the last 7 days: Sat 26 Dec 2026 – Fri 1 Jan 2027"),
    ("31 Dec", (2026, 12, 31), (2027, 1, 1), "Thu 31 Dec 2026"),
    ("5 Jan", (2026, 1, 5), (2026, 1, 6), "Mon 5 Jan 2026"),
    ("past year", (2026, 1, 2), NEW_YEAR_NOW, "the last 365 days: Fri 2 Jan 2026 – Fri 1 Jan 2027"),
    ("December to January", (2026, 12, 1), NEW_YEAR_NOW, "December 2026 to January 2027: Tue 1 Dec 2026 – Fri 1 Jan 2027"),
    ("yesterday", (2026, 12, 31), (2027, 1, 1), "yesterday: Thu 31 Dec 2026"),
)
def test_on_new_years_day(words, start, end, text):
    check(NEW_YEAR, words, start, end, text)


# ---------- Tue 29 Feb 2028 14:05 ----------
LEAP = at(2028, 2, 29, 14, 5)
LEAP_NOW = (2028, 2, 29, 14, 5)


@rows(
    ("29 Feb | 29 February 2028", (2028, 2, 29), LEAP_NOW, "Tue 29 Feb 2028"),
    ("28 Feb", (2028, 2, 28), (2028, 2, 29), "Mon 28 Feb 2028"),
    ("this month", (2028, 2, 1), LEAP_NOW, "this month: Tue 1 – Tue 29 Feb 2028"),
    ("February", (2028, 2, 1), LEAP_NOW, "February 2028: Tue 1 – Tue 29 Feb 2028"),
    ("last month", (2028, 1, 1), (2028, 2, 1), "last month: Sat 1 – Mon 31 Jan 2028"),
    ("last year", (2027, 1, 1), (2028, 1, 1), "last year: Fri 1 Jan – Fri 31 Dec 2027"),
    ("past year | 1y", (2027, 3, 2), LEAP_NOW, "the last 365 days: Tue 2 Mar 2027 – Tue 29 Feb 2028"),
    ("3m", (2027, 12, 1), LEAP_NOW, "the last 3 months: Wed 1 Dec 2027 – Tue 29 Feb 2028"),
    ("February 2027", (2027, 2, 1), (2027, 3, 1), "February 2027: Mon 1 – Sun 28 Feb 2027"),
)
def test_on_29_february(words, start, end, text):
    check(LEAP, words, start, end, text)


def test_29_february_in_a_year_without_one_is_no_date():
    with pytest.raises(PeriodError, match="29 February 2027 isn't a date"):
        read("29 Feb 2027", LEAP)
    with pytest.raises(PeriodError, match="isn't a date"):
        read("31 Sep", LEAP)


# ---------- Tue 10 Nov 2026 00:30 ----------
LATE = at(2026, 11, 10, 0, 30)
LATE_NOW = (2026, 11, 10, 0, 30)


@rows(
    ("today", (2026, 11, 10), LATE_NOW, "today: Tue 10 Nov 2026"),
    ("yesterday", (2026, 11, 9), (2026, 11, 10), "yesterday: Mon 9 Nov 2026"),
    ("last 2 hours", (2026, 11, 9, 22, 30), LATE_NOW, "the last 2 hours: Mon 9 Nov 10:30pm – Tue 10 Nov 2026 12:30am"),
    ("since 6am", (2026, 11, 9, 6), LATE_NOW, "since 6am: Mon 9 Nov 6am – Tue 10 Nov 2026 12:30am"),
    ("last night | overnight", (2026, 11, 9, 22), LATE_NOW, "last night: Mon 9 Nov 10pm – Tue 10 Nov 2026 12:30am"),   # still going
    ("yesterday evening", (2026, 11, 9, 18), (2026, 11, 10), "yesterday evening: Mon 9 Nov 2026 6pm – midnight"),
    ("last 7 days", (2026, 11, 4), LATE_NOW, "the last 7 days: Wed 4 – Tue 10 Nov 2026"),
)
def test_at_half_past_midnight(words, start, end, text):
    check(LATE, words, start, end, text)


def test_at_half_past_midnight_this_morning_is_still_ahead():
    with pytest.raises(PeriodError, match="in the future .*use forecast"):
        read("this morning", LATE)


# ---------- the days the clocks change ----------
def test_the_day_the_clocks_go_back_has_25_hours_and_its_hours_are_real():
    noon = at(2026, 4, 5, 12)
    assert hours(read("today", noon)) == 13                                        # midnight was summer time
    assert read("last 24 hours", noon).start == at(2026, 4, 4, 13) and hours(read("last 24 hours", noon)) == 24
    night = read("last night", noon)
    assert (night.start, night.end, hours(night)) == (at(2026, 4, 4, 22), at(2026, 4, 5, 6), 9)
    assert hours(read("yesterday", at(2026, 4, 6, 12))) == 25
    since = read("since 2am", noon)                                                # the second 2am: the latest
    assert (since.start.fold, since.start.timestamp(), hours(since)) == (1, at(2026, 4, 5, 2, fold=1).timestamp(), 10)
    early = read("last 3 hours", at(2026, 4, 5, 4))                                # 4am back to the first 2am
    assert (early.start.fold, hours(early), early.text) == (0, 3, "the last 3 hours: Sun 5 Apr 2026 2am – 4am")
    assert read("Easter Sunday", noon).text == "Easter Sunday: Sun 5 Apr 2026"
    two = read("last 2 days", at(2026, 4, 6, 12), whole_days=True)
    assert (two.start, two.end, hours(two), two.text) == (at(2026, 4, 4), at(2026, 4, 6), 49, "the last 2 days: Sat 4 – Sun 5 Apr 2026")


def test_the_day_the_clocks_go_forward_has_23_hours_and_its_hours_are_real():
    noon = at(2026, 10, 4, 12)
    assert hours(read("today", noon)) == 11
    assert read("last 24 hours", noon).start == at(2026, 10, 3, 11) and hours(read("last 24 hours", noon)) == 24
    night = read("last night", noon)
    assert (night.start, night.end, hours(night)) == (at(2026, 10, 3, 22), at(2026, 10, 4, 6), 7)
    assert hours(read("yesterday", at(2026, 10, 5, 12))) == 23
    early = read("last 3 hours", at(2026, 10, 4, 4))                               # three real hours: 2am to 3am never happened
    assert (early.start, early.text) == (at(2026, 10, 4), "the last 3 hours: Sun 4 Oct 2026 12am – 4am")
    skipped = read("since 2:30am", noon)                                           # a time that never happened: when the clocks jump
    assert (skipped.start, hours(skipped)) == (at(2026, 10, 4, 3), 9)
    assert read(None, noon, start="2026-10-04 02:30").start == at(2026, 10, 4, 3)


# ---------- whole-day tools ----------
CUT = "to Mon 9 Nov: today isn't over"
WHOLE_DAYS = [   # (words, start, end, text, cut) at TUESDAY
    ("last 7 days", (2026, 11, 3), (2026, 11, 10), "the last 7 days: Tue 3 – Mon 9 Nov 2026", CUT),   # that many whole days to yesterday
    ("this week", (2026, 11, 3), (2026, 11, 10), "the last 7 days: Tue 3 – Mon 9 Nov 2026", CUT),
    ("3m", (2026, 8, 11), (2026, 11, 10), "the last 3 months: Tue 11 Aug – Mon 9 Nov 2026", CUT),
    ("past year", (2025, 11, 10), (2026, 11, 10), "the last 365 days: Mon 10 Nov 2025 – Mon 9 Nov 2026", CUT),
    ("1d", (2026, 11, 9), (2026, 11, 10), "the last day: Mon 9 Nov 2026", CUT),
    ("last 48 hours", (2026, 11, 8), (2026, 11, 10), "the last 2 days: Sun 8 – Mon 9 Nov 2026", CUT),
    ("this month", (2026, 11, 1), (2026, 11, 10), "this month: Sun 1 – Mon 9 Nov 2026", CUT),        # a calendar one runs to yesterday
    ("this year", (2026, 1, 1), (2026, 11, 10), "this year: Thu 1 Jan – Mon 9 Nov 2026", CUT),
    ("since Monday", (2026, 11, 9), (2026, 11, 10), "since Monday: Mon 9 Nov 2026", CUT),
    ("on record", (2024, 3, 1), (2026, 11, 10), "on record: Fri 1 Mar 2024 – Mon 9 Nov 2026", CUT),
    ("yesterday", (2026, 11, 9), (2026, 11, 10), "yesterday: Mon 9 Nov 2026", None),                # nothing of today: not cut
    ("last week", (2026, 11, 2), (2026, 11, 9), "last week: Mon 2 – Sun 8 Nov 2026", None),
    ("October", (2026, 10, 1), (2026, 11, 1), "October 2026: Thu 1 – Sat 31 Oct 2026", None),
    ("Christmas Day", (2025, 12, 25), (2025, 12, 26), "Christmas Day: Thu 25 Dec 2025", None),
]


@pytest.mark.parametrize("words, start, end, text, cut", WHOLE_DAYS, ids=[row[0] for row in WHOLE_DAYS])
def test_whole_day_tools_take_whole_days_to_yesterday(words, start, end, text, cut):
    period = read(words, TUESDAY, whole_days=True, first_held=HELD)
    assert (period.start, period.end, period.text, period.cut) == (at(*start), at(*end), text, cut)


def test_whole_day_tools_cut_on_other_days_too():
    monday = read("this week", MONDAY, whole_days=True)
    assert (monday.text, monday.cut) == ("the last 7 days: Mon 2 – Sun 8 Nov 2026", "to Sun 8 Nov: today isn't over")
    leap = read("this month", LEAP, whole_days=True)
    assert (leap.text, leap.cut) == ("this month: Tue 1 – Mon 28 Feb 2028", "to Mon 28 Feb: today isn't over")
    assert read("1y", LEAP, whole_days=True).text == "the last 365 days: Mon 1 Mar 2027 – Mon 28 Feb 2028"
    assert read("past year", NEW_YEAR, whole_days=True).text == "the last 365 days: Thu 1 Jan – Thu 31 Dec 2026"
    assert read("this year", FIRST, whole_days=True).text == "this year: Thu 1 Jan – Sat 31 Oct 2026"


@pytest.mark.parametrize("words, now", [
    ("today", TUESDAY), ("last 5 hours", TUESDAY), ("30 min", TUESDAY), ("this month", FIRST), ("November", FIRST),
    ("week to date", MONDAY), ("since Monday", MONDAY), ("this year", NEW_YEAR), ("New Year's Day", NEW_YEAR), ("29 Feb", LEAP),
])
def test_a_whole_day_tool_refuses_what_leaves_no_whole_day(words, now):
    with pytest.raises(PeriodError, match=r"leaves no whole day.*use summary or readings") as caught:
        read(words, now, whole_days=True)
    assert caught.value.forms == ()


@pytest.mark.parametrize("words", ["this morning", "yesterday afternoon", "last night", "Tuesday night", "since 6am"])
def test_a_whole_day_tool_refuses_part_of_a_day(words):
    with pytest.raises(PeriodError, match=r"is part of a day.*use summary or readings"):
        read(words, TUESDAY, whole_days=True)


def test_a_whole_day_tool_takes_dates_only_and_cuts_them_to_yesterday():
    period = read(None, TUESDAY, start="2026-11-01", whole_days=True)
    assert (period.start, period.end, period.text, period.cut) == (at(2026, 11, 1), at(2026, 11, 10), "Sun 1 – Mon 9 Nov 2026", CUT)
    period = read(None, TUESDAY, start="2026-10-01", end="2026-10-31", whole_days=True)
    assert (period.start, period.end, period.cut) == (at(2026, 10, 1), at(2026, 11, 1), None)
    with pytest.raises(PeriodError, match=r"has a time.*use summary or readings"):
        read(None, TUESDAY, start="2026-11-01 06:00", whole_days=True)
    with pytest.raises(PeriodError, match="leaves no whole day"):
        read(None, TUESDAY, start="2026-11-10", whole_days=True)


# ---------- start and end ----------
def test_start_and_end_are_the_stations_clock_and_a_date_end_includes_the_day():
    period = read(None, TUESDAY, start="2026-11-01", end="2026-11-03")
    assert (period.asked, period.start, period.end, period.text) == ("start/end", at(2026, 11, 1), at(2026, 11, 4), "Sun 1 – Tue 3 Nov 2026")
    assert (period.first_day, period.last_day, period.as_naive_strings()) == (
        at(2026, 11, 1).date(), at(2026, 11, 3).date(), ("2026-11-01 00:00:00", "2026-11-03 23:59:59"))
    period = read(None, TUESDAY, start="2026-11-01 06:00", end="2026-11-01 09:30")   # an end with a time stops at it
    assert (period.start, period.end, period.text) == (at(2026, 11, 1, 6), at(2026, 11, 1, 9, 30), "Sun 1 Nov 2026 6am – 9:30am")
    assert read(None, TUESDAY, start="2026-11-01").end == TUESDAY                       # no end: to now
    assert read(None, TUESDAY, start="2026-11-01", end="2026-12-25").end == TUESDAY     # nor past it


def test_a_start_or_end_with_an_offset_is_moved_to_the_stations_clock():
    assert read(None, TUESDAY, start="2026-11-09T20:00:00Z").start == at(2026, 11, 10, 7)    # UTC: 7am in daylight saving
    period = read(None, TUESDAY, start="2026-11-01T00:00:00+00:00", end="2026-11-01T06:00:00+00:00")
    assert (period.start, period.end) == (at(2026, 11, 1, 11), at(2026, 11, 1, 17))
    assert read(None, at(2026, 6, 10, 12), start="2026-06-01T00:00:00Z").start == at(2026, 6, 1, 10)   # standard time


@pytest.mark.parametrize("period, start, end, says", [
    ("today", "2026-11-01", None, "not both"),
    ("today", None, "2026-11-03", "not both"),
    (None, None, "2026-11-03", "needs a `start`"),
    (None, "1 Nov", None, "isn't a date or time"),
    (None, "2026-11-05", "2026-11-03", "must be before"),
    (None, "2026-11-11", None, "in the future.*use forecast"),
])
def test_start_and_end_errors_say_what_to_send(period, start, end, says):
    with pytest.raises(PeriodError, match=says) as caught:
        read(period, TUESDAY, start=start, end=end)
    assert "It is now Tue 10 Nov 2026 2:05pm." in str(caught.value)


# ---------- errors that teach ----------
@pytest.mark.parametrize("words", ["yesterday afternoon and", "June, July", "last summer", "the last 7 days please", "rain this week",
                                   "0 days", "last thirteen days", "m", "5", "tonight"])
def test_words_outside_the_table_list_the_forms_with_what_each_means_now(words):
    with pytest.raises(PeriodError) as caught:
        read(words, TUESDAY, first_held=HELD)
    error = caught.value
    assert f"{words!r} isn't a period this tool can read" in error.message and "It is now Tue 10 Nov 2026 2:05pm." in error.message
    forms = dict(error.forms)
    assert forms["today"] == "today: Tue 10 Nov 2026"
    assert forms["last N days, Nd (N from 2)"] == "the last 7 days: Wed 4 – Tue 10 Nov 2026"
    assert forms["last N months, Nm, N mo (m is months, never minutes)"] == "the last 3 months: Wed 12 Aug – Tue 10 Nov 2026"
    assert forms["last week (the previous Monday to Sunday)"] == "last week: Mon 2 – Sun 8 Nov 2026"
    assert forms["a year, such as 2025"] == "2025: Wed 1 Jan – Wed 31 Dec 2025"
    assert forms["a day, such as 2026-09-05, 5 Sep, 5 September 2025, Sat 5 Sep (with no year, the latest not after today)"] == \
        "5 Sep: Sat 5 Sep 2026"
    assert forms["on record, all time, ever (from the first reading held)"] == "on record: Fri 1 Mar 2024 – Tue 10 Nov 2026"
    assert "- last N days, Nd (N from 2) → the last 7 days: Wed 4 – Tue 10 Nov 2026" in str(error)
    assert len(forms) == 27


def test_a_whole_day_tools_forms_are_its_own():
    with pytest.raises(PeriodError) as caught:
        read("last summer", TUESDAY, whole_days=True)
    forms = dict(caught.value.forms)
    assert "as dates ('YYYY-MM-DD')" in caught.value.message
    assert forms["last N days, Nd (N from 2)"] == "the last 7 days: Tue 3 – Mon 9 Nov 2026"
    assert not {"today", "last N minutes, N min", "since 6am, since Monday"} & set(forms)
    assert forms["on record, all time, ever (from the first reading held)"] == "from the first reading held, for the readings asked about, to now"


@pytest.mark.parametrize("words", ["tomorrow", "tomorrow morning", "next week", "the next 3 days", "December 2026", "2027",
                                   "Christmas Day 2026", "5 Dec 2026", "this evening"])
def test_a_future_period_points_to_the_forecast(words):
    with pytest.raises(PeriodError, match=r"is in the future.*for what's ahead, use forecast") as caught:
        read(words, TUESDAY)
    assert caught.value.forms == () and "It is now Tue 10 Nov 2026 2:05pm." in str(caught.value)


def test_a_weekday_that_does_not_match_the_date_is_named():
    with pytest.raises(PeriodError, match=r"5 September 2026 is a Saturday, not a Friday: send 'Sat 5 Sep 2026'"):
        read("Fri 5 Sep", TUESDAY)


def test_longer_than_anything_held_points_to_on_record():
    with pytest.raises(PeriodError, match="longer than ten years.*'on record'"):
        read("last 20 years", TUESDAY)
    assert read("last 10 years", TUESDAY).start == at(2016, 11, 13)   # 3650 days: today and the 3649 before


def test_on_record_needs_the_first_reading_held():
    with pytest.raises(PeriodError, match="Nothing is held yet"):
        read("on record", TUESDAY)
    assert read("on record", TUESDAY, first_held=at(2025, 6, 1).date()).start == at(2025, 6, 1)
