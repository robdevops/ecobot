"""Periods in words: the person's own words for a stretch of time ("this week", "3m", "yesterday afternoon"), read by one table in
the station's time zone, so a model never works out a date (docs/mcp-service.md 3.2). Every tool that takes a period reads it here,
and its result begins with how it was read.

  - `period` must be one of the table's forms as a whole phrase, case aside; N is a number in digits or a word up to twelve. There is
    no partial match and no falling back to a default: anything else is a PeriodError listing the forms with what each means now.
  - Or `start` (and `end`), on the station's clock: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'; one with an offset or Z is moved into local
    time. An end that is a date includes that day; no end is now.
  - Whole-day tools (days, relate, scan) take whole days up to yesterday, because today is still settling.
  - A period in the future is an error that points to the forecast.

Times are worked out as instants (UTC) and handed back on the station's clock, so an hour is a real hour across a clock change and
a day runs midnight to midnight, 23 or 25 hours on the days the clocks change.
"""

import re
from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from functools import lru_cache

from .ecowitt.calendar import PublicHolidays
from .timeutil import clock_text, day_text

DAY = timedelta(days=1)
MAX_DAYS = 3650                  # ten years: longer than anything held
PARTS = {"morning": (6, 12), "afternoon": (12, 18), "evening": (18, 24)}   # the parts of a day, in hours
NIGHT = (22, 6)                  # a night runs from 22:00 the evening before to 06:00
FOR_PART_OF_A_DAY = "use summary or readings"
FOR_WHAT_IS_AHEAD = "use forecast"

NUMBER_WORDS = {w: i for i, w in enumerate("one two three four five six seven eight nine ten eleven twelve".split(), 1)}
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
_WEEKDAYS = {name.lower(): i for i, name in enumerate(WEEKDAY_NAMES)} | {
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
_MONTHS = {name.lower(): i for i, name in enumerate(MONTH_NAMES, 1)} | {name[:3].lower(): i for i, name in enumerate(MONTH_NAMES, 1)} | {"sept": 9}
_UNITS = {"minutes": ("min", "mins", "minute", "minutes"), "hours": ("h", "hr", "hrs", "hour", "hours"), "days": ("d", "day", "days"),
          "weeks": ("w", "wk", "wks", "week", "weeks"), "months": ("m", "mo", "mos", "month", "months"),   # "m" is months, never minutes
          "years": ("y", "yr", "yrs", "year", "years")}
_UNIT = {word: unit for unit, words in _UNITS.items() for word in words}


def _either(words) -> str:
    return "|".join(sorted(words, key=len, reverse=True))


_WD, _MON, _YEAR = _either(_WEEKDAYS), _either(_MONTHS), r"(?:19|20)\d\d"
ON_RECORD = re.compile(r"on record|(?:of )?all[- ]time|ever(?: recorded)?|(?:the )?(?:full|whole|entire|complete|all(?: the)?) (?:history|record|data)")
ROLLING = (   # (words, how they read, days, or hours when it is a number of hours)
    (re.compile(r"this week|(?:the )?past week|the last week|week"), "the last 7 days", 7, 0),
    (re.compile(r"(?:(?:the )?(?:last|past) )?fortnight"), "the last 14 days", 14, 0),
    (re.compile(r"(?:the )?past month|the last month|month"), "the last 30 days", 30, 0),
    (re.compile(r"(?:the )?past year|the last year|year"), "the last 365 days", 365, 0),
    (re.compile(r"(?:the )?(?:last|past) day"), "the last 24 hours", 0, 24),
    (re.compile(r"(?:the )?(?:last|past) hour"), "the last hour", 0, 1),
)
NUMBERED = re.compile(rf"(?:(?:the )?(?:last|past) )?(?P<n>\d+|{_either(NUMBER_WORDS)}) ?(?P<unit>{_either(_UNIT)})")
YEAR = re.compile(rf"(?P<y>{_YEAR})")
MONTH = re.compile(rf"(?P<mon>{_MON})\.?(?:,? (?P<y>{_YEAR}))?")
DAYS = (   # "2026-09-05", "Sat 5 Sep", "5th of September 2025", "September 5, 2025"
    re.compile(rf"(?P<y>{_YEAR})-(?P<m>\d\d)-(?P<d>\d\d)"),
    re.compile(rf"(?:(?P<wd>{_WD}),? )?(?:the )?(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?: of)? (?P<mon>{_MON})\.?(?:,? (?P<y>{_YEAR}))?"),
    re.compile(rf"(?:(?P<wd>{_WD}),? )?(?P<mon>{_MON})\.? (?:the )?(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?:,? (?P<y>{_YEAR}))?"),
)
BARE_DAY = re.compile(rf"(?:(?P<wd>{_WD}),? )?(?:the )?(?P<d>\d{{1,2}})(?:st|nd|rd|th)?")   # the "5" of "5 to 10 Sep"
WEEKDAY = re.compile(rf"(?:last )?(?P<wd>{_WD})")
PART = re.compile(rf"(?P<day>this|yesterday|(?:last )?(?P<wd>{_WD})) (?P<part>morning|afternoon|evening)")
NIGHT_OF = re.compile(rf"(?:last )?(?P<wd>{_WD}) night")
SINCE = re.compile(r"since (?P<what>.+)")
CLOCK = re.compile(r"(?P<h>\d{1,2})(?:[:.](?P<mi>\d\d))? ?(?P<ap>am|pm|a\.m\.|p\.m\.)|(?P<h24>\d{1,2}):(?P<mi24>\d\d)|noon|midday|midnight")
RANGE_SEP = re.compile(r" (?:to|until|till|through|thru) | ?– ?| - ")
LEAD = re.compile(r"(?:in|over|during|for|within|across|throughout|on) (?P<rest>.+)")   # "in the last year", "over the past week"
FUTURE = re.compile(r"(?:the )?(?:day after )?tomorrow(?: .+)?|(?:the |this )?(?:next|coming|upcoming|following) .+|later(?: today| on)?")
HOLIDAY_ALIASES = {"cup day": "melbourne cup day", "grand final friday": "friday before the afl grand final", "easter": "easter sunday",
                   "new year": "new years day"}

# The forms a PeriodError lists, each with an example read at the time asked; `timed` ones are left out for whole-day tools
FORMS = (   # (form, example, timed)
    ("today", "today", True),
    ("yesterday", "yesterday", False),
    ("last N minutes, N min", "last 30 minutes", True),
    ("last N hours, Nh, last 24 hours, 1d, last day", "last 24 hours", True),
    ("last N days, Nd (N from 2)", "last 7 days", False),
    ("this week, past week, the last week, week, 1w", "this week", False),
    ("last N weeks, Nw, fortnight", "last 2 weeks", False),
    ("past month, the last month, month, 1m, last 30 days", "past month", False),
    ("last N months, Nm, N mo (m is months, never minutes)", "3m", False),
    ("past year, the last year, year, 1y, last 12 months", "past year", False),
    ("last N years, Ny", "2y", False),
    ("week to date, since Monday", "week to date", False),
    ("last week (the previous Monday to Sunday)", "last week", False),
    ("this month", "this month", False),
    ("last month", "last month", False),
    ("this year", "this year", False),
    ("last year", "last year", False),
    ("a year, such as 2025", "{last_year}", False),
    ("a month, such as October 2025, or October (the latest that has begun)", "October", False),
    ("A to B, between A and B, such as June to August 2026", "June to August", False),
    ("a day, such as 2026-09-05, 5 Sep, 5 September 2025, Sat 5 Sep (with no year, the latest not after today)", "5 Sep", False),
    ("Monday … Sunday, last Saturday (the latest before today)", "Saturday", False),
    ("a public holiday by name, with or without a year, such as Christmas Day or Melbourne Cup", "Christmas Day", False),
    ("this morning, yesterday afternoon, Tuesday evening (06:00-12:00, 12:00-18:00, 18:00-24:00)", "yesterday afternoon", True),
    ("last night, Tuesday night, overnight (22:00 the evening before to 06:00)", "last night", True),
    ("since 6am, since Monday", "since 6am", True),
    ("on record, all time, ever (from the first reading held)", "on record", False),
)


@dataclass(frozen=True)
class Period:
    """A period as read: the words asked (or "start/end"), its start and end on the station's clock (the end not included), how it
    was read, ready to pass on, and, when a whole-day tool cut it to yesterday, a note saying so."""
    asked: str
    start: datetime
    end: datetime
    text: str
    cut: str | None = None

    @property
    def first_day(self) -> date:
        return self.start.date()

    @property
    def last_day(self) -> date:
        """The day of its last moment: the day before an end at midnight."""
        return (self.end - timedelta(microseconds=1)).date()

    def as_naive_strings(self) -> tuple[str, str]:
        """Start and end as today's tools take them: 'YYYY-MM-DD HH:MM:SS' on the station's clock, the end its last second."""
        return f"{self.start:%Y-%m-%d %H:%M:%S}", f"{self.end - timedelta(seconds=1):%Y-%m-%d %H:%M:%S}"


class PeriodError(ValueError):
    """A period that can't be read, saying what to send instead. When the words aren't a form at all, `forms` holds each form with
    what it means now, so the error also tells a model today's date."""

    def __init__(self, message: str, forms: tuple[tuple[str, str], ...] = ()):
        super().__init__(message + "".join(f"\n- {form} → {means}" for form, means in forms))
        self.message, self.forms = message, forms


class _NotAForm(Exception):
    """Words that match no form (read_period lists the forms)."""


@dataclass(frozen=True)
class _Read:
    """Words read, before the cut to now and to whole days: kind is "calendar" (named days, cut at yesterday by a whole-day tool),
    "days" (the last `days` days), "hours" (a length of real time, `days` its whole days) or "time" (part of a day)."""
    label: str | None
    start: datetime
    end: datetime
    kind: str
    days: int = 0


@dataclass(frozen=True)
class _Clock:
    """The time asked, as an instant and on the station's clock."""
    at: datetime
    local: datetime
    tz: tzinfo

    @classmethod
    def of(cls, now: datetime, tz: tzinfo) -> "_Clock":
        local = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
        return cls(local.astimezone(UTC), local, tz)

    @property
    def today(self) -> date:
        return self.local.date()

    def wall(self, day: date, at: time = time(), fold: int = 0) -> datetime:
        """The instant the station's clock shows `at` on `day`. In the hour the clocks go back, `fold` picks the first (0) or the
        second (1); a time the clocks skip is read as the moment they jump."""
        local = datetime.combine(day, at, tzinfo=self.tz).replace(fold=fold)
        instant = local.astimezone(UTC)
        if instant.astimezone(self.tz).replace(tzinfo=None) == local.replace(tzinfo=None):
            return instant
        instant = min(local.replace(fold=0).astimezone(UTC), local.replace(fold=1).astimezone(UTC))
        while instant.astimezone(self.tz).replace(tzinfo=None) < local.replace(tzinfo=None):
            instant += timedelta(minutes=1)
        return instant

    def midnight(self, day: date) -> datetime:
        return self.wall(day)

    def error(self, message: str, forms: tuple[tuple[str, str], ...] = ()) -> PeriodError:
        return PeriodError(f"{message} It is now {day_text(self.local)} {clock_text(self.local)}.", forms)


def read_period(period: str | None, start: str | None, end: str | None, *, now: datetime, tz: tzinfo, default: str,
                whole_days: bool = False, first_held: date | datetime | None = None) -> Period:
    """The period a tool was asked for: `period` in words, or `start` (and `end`), or else `default` (words). `now` is the time asked
    (aware; naive is taken as the station's clock), `first_held` the first reading held for the readings asked about ("on record").
    whole_days: the tool works on whole days, up to yesterday. PeriodError says what to send instead."""
    clock = _Clock.of(now, tz)
    try:
        return _read(period, start, end, clock, default, whole_days, first_held)[0]
    except _NotAForm as e:
        dates = "as dates ('YYYY-MM-DD'): this tool takes whole days, up to yesterday" if whole_days else \
            "('YYYY-MM-DD' or 'YYYY-MM-DD HH:MM', the station's local time)"
        raise clock.error(f"{e.args[0]!r} isn't a period this tool can read. Send `period` as one of these forms, as a whole phrase "
                          f"(N is a number, in digits or a word up to twelve), or send `start` and `end` {dates}.",
                          _forms(clock, whole_days, first_held)) from None


def _read(period, start, end, clock: _Clock, default: str, whole_days: bool, first_held) -> tuple[Period, str | None]:
    """The Period and the label it was read as (None for plain dates)."""
    period, start, end = (str(x).strip() if x is not None and str(x).strip() else None for x in (period, start, end))
    if period and (start or end):
        raise clock.error(f"Send `period` or `start` (with `end`), not both: `period` {period!r}, `start` {start!r}, `end` {end!r}.")
    if end and not start:
        raise clock.error(f"`end` {end!r} needs a `start`: send `start` too, or a `period` instead.")
    if start:
        asked, what, read = "start/end", f"`start` {start!r}", _dates(start, end, clock, whole_days)
    else:
        asked = period or default
        what, read = repr(asked), _words(asked, clock, first_held)
    if read.start > clock.at:
        raise clock.error(f"{what} is in the future ({_text(read, clock)}). This tool reads what has been measured: for what's ahead, "
                          f"{FOR_WHAT_IS_AHEAD}.")
    read = replace(read, end=min(read.end, clock.at))
    cut = None
    if whole_days:
        read, cut = _whole_days(read, what, clock)
    return Period(asked, read.start.astimezone(clock.tz), read.end.astimezone(clock.tz), _text(read, clock), cut), read.label


def _whole_days(read: _Read, what: str, clock: _Clock) -> tuple[_Read, str | None]:
    """The period in whole days up to yesterday, and the note when today was cut from it."""
    today, yesterday = clock.midnight(clock.today), clock.today - DAY
    cut = f"to {day_text(yesterday, year=False)}: today isn't over"
    if read.kind == "time":
        raise clock.error(f"{what} is part of a day: this tool takes whole days, up to yesterday ({day_text(yesterday)}). For part of a "
                          f"day, {FOR_PART_OF_A_DAY}.")
    nothing = clock.error(f"{what} leaves no whole day: this tool takes whole days up to yesterday ({day_text(yesterday)}), as today "
                          f"isn't over. For today or part of a day, {FOR_PART_OF_A_DAY}.")
    if read.kind in ("days", "hours"):
        if not read.days:
            raise nothing
        label = read.label if read.kind == "days" else "the last day" if read.days == 1 else f"the last {read.days} days"
        return _Read(label, clock.midnight(clock.today - read.days * DAY), today, "days", read.days), cut
    if read.end <= today:
        return read, None
    if read.start >= today:
        raise nothing
    return replace(read, end=today), cut


# ---------- start and end ----------
def _dates(start: str, end: str | None, clock: _Clock, whole_days: bool) -> _Read:
    first, _ = _moment("start", start, clock, whole_days)
    if end is None:
        return _Read(None, first, clock.at, "calendar")
    last, is_date = _moment("end", end, clock, whole_days)
    if is_date:   # a date includes that whole day
        last = clock.midnight(last.astimezone(clock.tz).date() + DAY)
    if first >= last:
        raise clock.error(f"`start` {start!r} must be before `end` {end!r}.")
    return _Read(None, first, last, "calendar")


def _moment(name: str, text: str, clock: _Clock, whole_days: bool) -> tuple[datetime, bool]:
    """A start or end as an instant, and whether it was a date alone."""
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise clock.error(f"`{name}` {text!r} isn't a date or time: send 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM', the station's local "
                          f"time.") from None
    is_date = bool(re.fullmatch(r"\d{4}-\d\d-\d\d", text))
    if whole_days and not is_date:
        raise clock.error(f"`{name}` {text!r} has a time: this tool takes whole days ('YYYY-MM-DD'), up to yesterday "
                          f"({day_text(clock.today - DAY)}). For part of a day, {FOR_PART_OF_A_DAY}.")
    return (dt.astimezone(UTC) if dt.tzinfo else clock.wall(dt.date(), dt.time())), is_date


# ---------- words ----------
def _words(asked: str, clock: _Clock, first_held) -> _Read:
    p = " ".join(asked.replace("’", "'").replace("—", "–").lower().split())
    lead = LEAD.fullmatch(p)
    read = _phrase(p, clock, first_held) or (lead and _phrase(lead["rest"], clock, first_held))
    if read:
        return read
    if FUTURE.fullmatch(p) or (lead and FUTURE.fullmatch(lead["rest"])):
        raise clock.error(f"{asked!r} is in the future. This tool reads what has been measured: for what's ahead, {FOR_WHAT_IS_AHEAD}.")
    raise _NotAForm(asked)


def _phrase(p: str, clock: _Clock, first_held) -> _Read | None:
    """The words as one of the forms, or None."""
    if ON_RECORD.fullmatch(p):
        return _on_record(clock, first_held)
    for words, label, days, hours in ROLLING:
        if words.fullmatch(p):
            return _last_days(label, days, clock) if days else _last_time(label, timedelta(hours=hours), clock)
    if m := NUMBERED.fullmatch(p):
        return _numbered(p, m, clock)
    if p in ("last night", "overnight"):
        return _night("last night", clock.today, clock)
    if m := NIGHT_OF.fullmatch(p):
        wd = _WEEKDAYS[m["wd"]]
        return _night(f"{WEEKDAY_NAMES[wd]} night", _latest_weekday(wd, clock.today, strict=True), clock)
    if m := PART.fullmatch(p):
        return _part(m, clock)
    if m := SINCE.fullmatch(p):
        return _since(m["what"], clock)
    if read := _range(p, clock):
        return read
    return _calendar(p, clock, clock.today, strict=True)


def _on_record(clock: _Clock, first_held) -> _Read:
    if first_held is None:
        raise clock.error("Nothing is held yet for these readings, so 'on record' has no start.")
    if isinstance(first_held, datetime):
        first_held = (first_held.astimezone(clock.tz) if first_held.tzinfo else first_held).date()
    return _Read("on record", clock.midnight(first_held), clock.at, "calendar")


def _last_days(label: str, days: int, clock: _Clock) -> _Read:
    """Today and the days-1 whole days before it, to now."""
    return _Read(label, clock.midnight(clock.today - (days - 1) * DAY), clock.at, "days", days)


def _last_time(label: str, length: timedelta, clock: _Clock) -> _Read:
    """The last `length` of real time, to now."""
    return _Read(label, clock.at - length, clock.at, "hours", length // DAY)


def _numbered(p: str, m: re.Match, clock: _Clock) -> _Read | None:
    """"last 7 days", "3m", "past two years", "45 min": a rolling period to now. One day is the last 24 hours; a month is
    round(N×365/12) days, a year 365."""
    if len(m["n"]) > 6 or not (n := int(m["n"]) if m["n"].isdigit() else NUMBER_WORDS[m["n"]]):
        return None
    unit = _UNIT[m["unit"]]
    if unit in ("minutes", "hours") or (unit == "days" and n == 1):
        length = timedelta(minutes=n) if unit == "minutes" else timedelta(hours=24 if unit == "days" else n)
        if length > MAX_DAYS * DAY:
            raise clock.error(f"{p!r} is longer than ten years, more than anything held: for all of it, send 'on record'.")
        hours = length // timedelta(hours=1)
        label = (f"the last {n} minutes" if n > 1 else "the last minute") if unit == "minutes" else \
            f"the last {hours} hours" if hours > 1 else "the last hour"
        return _last_time(label, length, clock)
    days = {"days": n, "weeks": 7 * n, "months": round(n * 365 / 12), "years": 365 * n}[unit]
    if days > MAX_DAYS:
        raise clock.error(f"{p!r} is longer than ten years, more than anything held: for all of it, send 'on record'.")
    return _last_days(f"the last {days} days" if n == 1 or unit == "days" else f"the last {n} {unit}", days, clock)


def _night(label: str, day: date, clock: _Clock) -> _Read:
    """The night that ends on `day`: 22:00 the evening before to 06:00."""
    return _Read(label, clock.wall(day - DAY, time(NIGHT[0])), clock.wall(day, time(NIGHT[1])), "time")


def _part(m: re.Match, clock: _Clock) -> _Read:
    """"this morning", "yesterday afternoon", "Tuesday evening" (the latest Tuesday before today)."""
    if m["wd"]:
        wd = _WEEKDAYS[m["wd"]]
        day, said = _latest_weekday(wd, clock.today, strict=True), WEEKDAY_NAMES[wd]
    else:
        day, said = (clock.today, "this") if m["day"] == "this" else (clock.today - DAY, "yesterday")
    first, last = PARTS[m["part"]]
    end = clock.midnight(day + DAY) if last == 24 else clock.wall(day, time(last))
    return _Read(f"{said} {m['part']}", clock.wall(day, time(first)), end, "time")


def _since(what: str, clock: _Clock) -> _Read | None:
    """"since 6am": from the latest such time; "since Monday", "since 5 Sep": from that day's 00:00 (a weekday may be today)."""
    if (m := CLOCK.fullmatch(what)) and (at := _time_of_day(m)) is not None:
        days = (clock.today, clock.today - DAY)
        start = max(t for t in (clock.wall(d, at, fold) for d in days for fold in (0, 1)) if t <= clock.at)
        said = "midnight" if at == time() else "noon" if at == time(12) else clock_text(datetime.combine(clock.today, at), whole_hours=True)
        return _Read(f"since {said}", start, clock.at, "time")
    if anchor := _calendar(what, clock, clock.today, strict=False):
        return _Read(f"since {anchor.label}" if anchor.label else None, anchor.start, clock.at, "calendar")
    return None


def _time_of_day(m: re.Match) -> time | None:
    if m[0] in ("noon", "midday", "midnight"):
        return time(12) if m[0] != "midnight" else time()
    hour, minute = int(m["h"] or m["h24"]), int(m["mi"] or m["mi24"] or 0)
    if m["ap"]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if m["ap"][0] == "p" else 0)
    return time(hour, minute) if hour < 24 and minute < 60 else None


def _range(p: str, clock: _Clock) -> _Read | None:
    """"A to B", "between A and B", "from A until B": the start of A to the end of B. B is read as it would be alone; A, when it
    names no year (or is a weekday), is the latest not after B, so "December to February" is the summer just gone."""
    between = p.startswith("between ")
    body = p.removeprefix("between ") if between else p.removeprefix("from ")
    for sep in re.finditer(" and " if between else RANGE_SEP, body):
        a, b = body[:sep.start()], body[sep.end():]
        last = _Read("now", clock.at, clock.at, "calendar") if b == "now" else _calendar(b, clock, clock.today, strict=True)
        if last is None:
            continue
        b_day = last.start.astimezone(clock.tz).date()
        if BARE_DAY.fullmatch(a) and any(d.fullmatch(b) for d in DAYS):   # "5 to 10 Sep": the 5th of B's month
            a = f"{a} {MONTH_NAMES[b_day.month - 1].lower()} {b_day.year}"
        if (first := _calendar(a, clock, b_day, strict=False)) is None:
            continue
        if first.start >= last.end:
            raise clock.error(f"{p!r} starts after it ends.")
        return _Read(f"{first.label} to {last.label}" if first.label and last.label else None, first.start, last.end, "calendar")
    return None


def _calendar(p: str, clock: _Clock, ref: date, strict: bool) -> _Read | None:
    """A stretch of whole days with a name: today, yesterday, last week, this or last month or year, a year, a month, a day, a
    weekday, a public holiday. A month, day or holiday with no year is the latest not after `ref`; a weekday the latest before it
    (strict) or not after it."""
    if named := _named(p, clock.today):
        label, first, end = named
        return _Read(label, clock.midnight(first), clock.midnight(end), "calendar")
    if m := YEAR.fullmatch(p):
        year = int(m["y"])
        return _Read(m["y"], clock.midnight(date(year, 1, 1)), clock.midnight(date(year + 1, 1, 1)), "calendar")
    if m := MONTH.fullmatch(p):
        month = _MONTHS[m["mon"]]
        year = int(m["y"]) if m["y"] else ref.year if month <= ref.month else ref.year - 1
        first = date(year, month, 1)
        end = date(year + month // 12, month % 12 + 1, 1)
        return _Read(f"{MONTH_NAMES[month - 1]} {year}", clock.midnight(first), clock.midnight(end), "calendar")
    for pattern in DAYS:
        if m := pattern.fullmatch(p):
            return _one_day(None, _day(m, ref, clock), clock)
    if m := WEEKDAY.fullmatch(p):
        wd = _WEEKDAYS[m["wd"]]
        return _one_day(WEEKDAY_NAMES[wd], _latest_weekday(wd, ref, strict), clock)
    if holiday := _holiday(p, ref, clock):
        return _one_day(*holiday, clock)
    return None


def _named(p: str, today: date) -> tuple[str, date, date] | None:
    """The calendar's named stretches as (label, first day, the day after the last)."""
    monday, month = today - timedelta(days=today.weekday()), today.replace(day=1)
    previous = (month - DAY).replace(day=1)
    following = (month + 31 * DAY).replace(day=1)
    return {"today": ("today", today, today + DAY), "so far today": ("today", today, today + DAY),
            "yesterday": ("yesterday", today - DAY, today),
            "week to date": ("week to date", monday, today + DAY),
            "last week": ("last week", monday - 7 * DAY, monday),
            "this month": ("this month", month, following), "last month": ("last month", previous, month),
            "this year": ("this year", date(today.year, 1, 1), date(today.year + 1, 1, 1)),
            "last year": ("last year", date(today.year - 1, 1, 1), date(today.year, 1, 1))}.get(p)


def _one_day(label: str | None, day: date, clock: _Clock) -> _Read:
    return _Read(label, clock.midnight(day), clock.midnight(day + DAY), "calendar")


def _day(m: re.Match, ref: date, clock: _Clock) -> date:
    """The day a date names: with no year, the latest not after `ref`. A weekday that doesn't match it is an error."""
    g = m.groupdict()   # (the ISO form has no weekday or month name)
    month, day = int(g["m"]) if g.get("m") else _MONTHS[g["mon"]], int(g["d"])
    if not 1 <= month <= 12 or not 1 <= day <= monthrange(2000, month)[1]:   # (2000: a leap year, so 29 Feb is a date)
        raise clock.error(f"{m[0]!r} isn't a date.")
    if g["y"]:
        try:
            found = date(int(g["y"]), month, day)
        except ValueError:
            raise clock.error(f"{day} {MONTH_NAMES[month - 1]} {g['y']} isn't a date.") from None
    else:
        found = next(d for year in range(ref.year, ref.year - 9, -1) if (d := _date_or_none(year, month, day)) and d <= ref)
    if g.get("wd") and (wanted := _WEEKDAYS[g["wd"]]) != found.weekday():
        raise clock.error(f"{found.day} {found:%B %Y} is a {found:%A}, not a {WEEKDAY_NAMES[wanted]}: send '{day_text(found)}', "
                          f"or the date alone.")
    return found


def _date_or_none(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:   # 29 Feb in a year that has none
        return None


def _latest_weekday(wd: int, ref: date, strict: bool) -> date:
    """The latest such weekday not after `ref`, or before it if strict."""
    back = (ref.weekday() - wd) % 7
    return ref - timedelta(days=back or (7 if strict else 0))


# ---------- public holidays ----------
def _holiday(p: str, ref: date, clock: _Clock) -> tuple[str, date] | None:
    """A public holiday by name, from the station's holiday calendar ("Christmas Day", "Melbourne Cup 2025"): (its name, its day),
    with no year the latest not after `ref`."""
    m = re.fullmatch(rf"(?:the )?(?P<name>.+?)(?: (?P<y>{_YEAR}))?", p)
    key = _holiday_key(m["name"])
    key = HOLIDAY_ALIASES.get(key, key)
    years = (int(m["y"]),) if m["y"] else (ref.year, ref.year - 1)
    found = [(day, name) for year in years for day, name in _holidays(clock.tz, year).get(key, ()) if m["y"] or day <= ref]
    return max(found, key=lambda found: found[0])[::-1] if found else None


def _holiday_key(name: str) -> str:
    return " ".join(name.lower().replace("'", "").replace("’", "").split())


@lru_cache(maxsize=None)
def _holidays(tz: tzinfo, year: int) -> dict[str, tuple[tuple[date, str], ...]]:
    """The year's public holidays by the words that name them: the name, and the name without "Day" ("Christmas", "Melbourne Cup")."""
    calendar, found = PublicHolidays(tz), {}
    day = date(year, 1, 1)
    while calendar.available and day.year == year:
        for name in (calendar.name(day) or "").split("; "):
            if name and "observed" not in name.lower():
                key = _holiday_key(name)
                for words in {key, key.removesuffix(" day")}:
                    found[words] = found.get(words, ()) + ((day, name),)
        day += DAY
    return found


# ---------- in words ----------
def _text(read: _Read, clock: _Clock) -> str:
    """How the period was read: "the last 7 days: Wed 4 – Tue 10 Nov 2026"; plain dates without the label. Days run from midnight
    to midnight, the last of them today for a period to now; a length of time or part of a day shows its times."""
    start, end = read.start.astimezone(clock.tz), read.end.astimezone(clock.tz)
    as_days = read.kind in ("calendar", "days") and start.time() == time() and (end.time() == time() or read.end == clock.at)
    span = _days_text(start.date(), max(start.date(), end.date() - DAY if end.time() == time() else end.date())) if as_days \
        else _times_text(start, end)
    return f"{read.label}: {span}" if read.label else span


def _times_text(start: datetime, end: datetime) -> str:
    """"Tue 10 Nov 2026 6am – 12pm", "Tue 3 Nov 2026 6pm – midnight", "Mon 9 Nov 10pm – Tue 10 Nov 2026 6am"."""
    midnight = time()
    if start.date() == end.date():
        return f"{day_text(start)} {_clock(start)} – {_clock(end)}"
    if end.time() == midnight and end.date() == start.date() + DAY:
        return f"{day_text(start)} {_clock(start)} – midnight"
    return f"{day_text(start, year=start.year != end.year)} {_clock(start)} – {day_text(end)} {_clock(end)}"


def _days_text(first: date, last: date) -> str:
    """"Tue 10 Nov 2026", "Wed 4 – Tue 10 Nov 2026", "Wed 12 Aug – Tue 10 Nov 2026", "Mon 10 Nov 2025 – Tue 10 Nov 2026"."""
    if first == last:
        return day_text(first)
    if (first.year, first.month) == (last.year, last.month):
        return f"{first:%a} {first.day} – {day_text(last)}"
    return f"{day_text(first, year=first.year != last.year)} – {day_text(last)}"


def _clock(t: datetime) -> str:
    return clock_text(t, whole_hours=True)


def _forms(clock: _Clock, whole_days: bool, first_held) -> tuple[tuple[str, str], ...]:
    """Each form with what its example means now (a whole-day tool's leaves out the ones it refuses)."""
    forms = []
    for form, example, timed in FORMS:
        if whole_days and timed:
            continue
        example = example.format(last_year=clock.today.year - 1)
        if example == "on record" and first_held is None:
            forms.append((form, "from the first reading held, for the readings asked about, to now"))
            continue
        try:
            period, label = _read(example, None, None, clock, example, whole_days, first_held)
        except (PeriodError, _NotAForm):   # (no holiday calendar here, or not on this day: "this month" on the 1st, for whole days)
            continue
        forms.append((form, period.text if label else f"{example}: {period.text}"))
    return tuple(forms)
