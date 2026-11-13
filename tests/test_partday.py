"""Part of each day (lib/partday.py): a sensor's figures over a stretch of each day, checked against stretches worked out by hand on a
small file, and for the whole day against the day rows."""

import random
from datetime import date, datetime, time, timedelta

import pytest

from lib.partday import CLOCK_CHANGE, NOT_HELD, NOT_OVER, SECONDS, Stretch, over, parse_stretch, stretch_figures
from lib.storage import Storage
from lib.storage.catalogue import CLOUD, RAIN_COUNTERS
from lib.storage.days import utc_days
from lib.storage.read import DAY_PARTS
from lib.timeutil import day_bounds, local_epoch
from tests.fakes import TZ

MAC, LOC = "M", "42"
NOW = local_epoch(datetime(2026, 11, 10, 14, 5), TZ)   # Tue 10 Nov 2026 2:05pm
DAY = timedelta(days=1)


def at(d: date, hour: int, minute: int = 0) -> int:
    """The epoch of a local time (the first, in the hour that happens twice)."""
    return local_epoch(datetime.combine(d, time(hour, minute)), TZ)


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "ecobot.sqlite", TZ)
    yield s
    s.close()


def put(storage, group: str, res: str, rows: list[dict], start: int, end: int):
    """Readings written as a source writes them (an Ecowitt group's, or the air sensor's: "air"), [start, end] held at `res`."""
    def write(w):
        s = w.air_sensor(LOC) if group == "air" else w.ecowitt_sensor(MAC, group)
        w.put(s, res, rows, CLOUD)
        w.cover(s, res, start, end)
        return s
    return storage.writer.run(write, ())


def figures(storage, sensor, first: date, last: date, stretch: str, columns: list[str] | None = None, now: int = NOW):
    return storage.reader.run(lambda r: stretch_figures(r, sensor.kind, sensor, first, last, stretch, columns, TZ, now), ())


# ---------- the stretches ----------
def test_a_stretch_is_named_two_times_on_the_half_hour_or_a_moment_and_anything_else_says_what_is_allowed():
    assert parse_stretch(" Night ") == Stretch(22 * 60, 6 * 60, "night") and parse_stretch("night").overnight
    assert [(parse_stretch(n).start, parse_stretch(n).end) for n in ("morning", "afternoon", "evening", "daytime")] == [
        (360, 720), (720, 1080), (1080, 1440), (360, 1080)]
    assert parse_stretch("22:30-06:00") == Stretch(1350, 360) and parse_stretch("22:30-06:00").overnight
    assert parse_stretch("18:00-00:00") == parse_stretch("18:00-24:00") == Stretch(1080, 1440)   # to midnight, not the evening before
    assert parse_stretch("15:00") == parse_stretch("15:00-15:05") == Stretch(900, 905) and parse_stretch("15:00").moment
    assert parse_stretch("night").text == "night (22:00–06:00)" and parse_stretch("15:30").text == "15:30"
    for wrong in ("15:10", "15:00-15:10", "15:00-15:07", "3pm", "24:00-02:00", "06:00-06:00", "15:60", "25:00", "dawn", ""):
        with pytest.raises(ValueError, match=r"night \(22:00-06:00\).*a moment on the hour or half hour"):
            parse_stretch(wrong)


def test_today_counts_once_its_stretch_is_over(storage):
    today = date(2026, 11, 10)
    night, three = parse_stretch("night"), parse_stretch("15:00")
    assert not over(night, today, TZ, at(today, 5, 59)) and over(night, today, TZ, at(today, 6))
    assert not over(three, today, TZ, at(today, 15, 4)) and over(three, today, TZ, at(today, 15, 5))
    seven = at(today, 7)
    s = put(storage, "outdoor", "5min", [{"ts": t, "temperature": 9.0} for t in range(at(today - DAY, 12), seven, 300)],
            at(today - DAY, 12), seven - 1)
    assert figures(storage, s, today, today, "night", now=seven).days.day == [today]   # last night, asked this morning
    assert figures(storage, s, today, today, "morning", now=seven).left_out == {today: NOT_OVER}


# ---------- the figures ----------
def test_a_night_starts_the_evening_before_and_belongs_to_the_day_it_ends_on(storage):
    mon, tue = date(2026, 11, 2), date(2026, 11, 3)
    coldest = at(tue, 4, 30)
    temp = lambda t: round(2 + abs(t - coldest) / 3600, 2)   # 2° at 4:30am, a degree warmer for each hour either side
    s = put(storage, "outdoor", "5min", [{"ts": t, "temperature": temp(t)} for t in range(at(mon, 0), at(tue, 12), 300)],
            at(mon, 0), at(tue, 12) - 1)
    got = figures(storage, s, mon, tue, "night", ["temperature"])
    assert got.left_out == {mon: NOT_HELD}            # (Monday's night began on Sunday evening, which isn't held)
    assert got.windows == {tue: (at(mon, 22), at(tue, 6))} and got.missing == []
    days, night = got.days, [temp(t) for t in range(at(mon, 22), at(tue, 6), 300)]
    assert (days.day, days.source, days.complete, days.n) == ([tue], ["5min"], [True], [96])
    assert (days.lo["temperature"], days.lo_ts["temperature"]) == ([2.0], [coldest])
    assert (days.hi["temperature"], days.hi_ts["temperature"]) == ([8.5], [at(mon, 22)])   # not 9:55pm's 8.58: before the night
    assert days.mean["temperature"] == [pytest.approx(sum(night) / len(night))]


def test_the_clock_changes_make_a_night_9_or_7_hours_and_the_hour_october_skips_is_left_out(storage):
    april, october = date(2026, 4, 5), date(2026, 10, 4)   # Melbourne: 2am happens twice, then 2am is 3am
    for d in (april, october):
        start, end = at(d - DAY, 0), at(d + DAY, 0)
        s = put(storage, "outdoor", "5min", [{"ts": t, "temperature": 10.0 + (t - start) / 3600} for t in range(start, end, 300)],
                start, end - 1)
    night = figures(storage, s, april, october, "night")
    assert night.days.day == [april, october] and night.days.n == [9 * 12, 7 * 12]
    assert set(night.left_out.values()) == {NOT_HELD}
    skipped, twice = (figures(storage, s, d, d, "02:00-03:00") for d in (october, april))
    assert (skipped.left_out, skipped.days.day) == ({october: CLOCK_CHANGE}, [])
    assert twice.days.n == [24] and twice.windows[april] == (at(april, 2), at(april, 3))   # 2:00 AEDT to 3:00 AEST: both of its 2ams
    part = figures(storage, s, october, october, "01:30-02:30")    # what is left of it: 1:30 to the jump
    assert part.days.n == [6] and part.windows[october] == (at(october, 1, 30), at(october, 3))
    assert figures(storage, s, october, october, "02:30").left_out == {october: CLOCK_CHANGE}
    first = figures(storage, s, april, april, "02:30")             # the first 2:30 of the two
    assert first.windows[april] == (at(april, 2, 30), at(april, 2, 30) + 300) and first.days.n == [1]


def test_a_moment_is_the_5_minute_reading_at_that_time_or_on_a_coarser_day_the_half_hour_or_hour_that_holds_it(storage):
    coarse, fine = date(2026, 11, 2), date(2026, 11, 3)
    a, b = day_bounds(fine, TZ, True)
    s = put(storage, "outdoor", "5min", [{"ts": t, "temperature": 10 + (t - a) / 30000} for t in range(a, b, 300)], a, b)
    a, b = day_bounds(coarse, TZ, True)
    put(storage, "outdoor", "30min", [{"ts": t, "temperature": 20 + i, "temperature_lo": 19.5 + i, "temperature_hi": 21 + i}
                                      for i, t in enumerate(range(a, b, 1800))], a, b)
    got = figures(storage, s, coarse, fine, "15:00")
    days = got.days
    assert days.day == [coarse, fine] and days.source == ["30min", "5min"] and days.n == [1, 1]
    assert got.windows == {coarse: (at(coarse, 15), at(coarse, 15, 30)), fine: (at(fine, 15), at(fine, 15, 5))}
    assert days.mean["temperature"] == [50.0, pytest.approx(10 + 15 * 3600 / 30000)]            # (not 3:05's)
    assert days.lo["temperature"] == [49.5, pytest.approx(10 + 15 * 3600 / 30000)]
    assert days.hi["temperature"] == [51.0, pytest.approx(10 + 15 * 3600 / 30000)]               # the half hour's own lowest and highest
    assert days.hi_ts["temperature"] == [at(coarse, 15), at(fine, 15)]
    assert got.note == "15:00 is the 5-minute reading at that time; on 1 day held only as 30-minute averages, the half hour from it."
    a, b = day_bounds(coarse, TZ, True)
    air = put(storage, "air", "1hour", [{"ts": t, "pm2_5": float(i)} for i, t in enumerate(range(a, b, 3600))], a, b)
    hourly = figures(storage, air, coarse, coarse, "15:30")
    assert hourly.windows == {coarse: (at(coarse, 15), at(coarse, 16))} and hourly.days.mean["pm2_5"] == [15.0]
    assert "on 1 day held only as hourly averages, the hour that holds it" in hourly.note
    assert figures(storage, s, fine, fine, "15:00").note is None


def test_a_day_held_only_as_daily_records_or_in_part_is_left_out_and_one_held_with_no_readings_is_missing(storage):
    old, off, part = date(2025, 3, 10), date(2025, 3, 11), date(2025, 3, 12)
    first, last = utc_days(old, TZ)
    with storage.writer.transaction() as w:
        s = w.ecowitt_sensor(MAC, "outdoor")
        w.put(s, "1day", [{"ts": first, "temperature": 15.0, "temperature_lo": 8.0, "temperature_hi": 22.0}])
        w.put(s, "4hour", [{"ts": first + 4 * 3600 * i, "temperature": 15.0} for i in range(6)])
        w.cover(s, "1day", first, last + 86400 - 1)
        w.cover(s, "4hour", first, last + 86400 - 1)
        w.cover(s, "5min", *day_bounds(off, TZ, True))                       # the station was off
        w.put(s, "5min", [{"ts": t, "temperature": 11.0} for t in range(at(part, 0), at(part, 12), 300)])
        w.cover(s, "5min", at(part, 0), at(part, 12) - 1)                    # the morning held, the afternoon not
    assert storage.reader.run(lambda r: r.days("climate", [s.id], old, old)[s.id].source, ()) == ["1day"]   # (a day row, all the same)
    got = figures(storage, s, old, part, "00:00-24:00")
    assert got.left_out == {old: NOT_HELD, part: NOT_HELD} and got.missing == [off] and got.days.day == []
    assert figures(storage, s, part, part, "morning").days.n == [72]


def test_rain_over_a_stretch_is_what_fell_in_it_not_the_running_total_since_midnight(storage):
    mon, tue, wed = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)
    fell = {at(mon, 18) + 300 * i: 0.2 for i in range(30)}                 # 6 mm on Monday evening, then dry all night
    fell |= {at(tue, 15) + 300 * i: 0.2 for i in range(5)}                 # 1 mm on Tuesday afternoon
    fell |= {at(tue, 23) + 300 * i: 0.1 for i in range(5)}                 # 0.5 mm late on Tuesday evening ...
    fell |= {at(wed, 2) + 300 * i: 0.1 for i in range(3)}                  # ... and 0.3 after midnight
    start, end = at(mon, 0), at(wed, 12)
    rows, total = [], 0.0
    for t in range(start, end, 300):
        total = (0.0 if t in (at(tue, 0), at(wed, 0)) else total) + fell.get(t, 0.0)   # the daily total, from 0 at midnight
        rows.append({"ts": t, "daily": round(total, 1), "rain_rate": 2.4 if t in fell else 0.0})
    s = put(storage, "rainfall", "5min", rows, start, end - 1)
    nights = figures(storage, s, tue, wed, "night", ["daily", "rain_rate"]).days
    assert nights.hi["daily"] == [0.0, pytest.approx(0.8)]                 # not Monday's 6 mm, held all night as the day's total
    assert nights.hi_ts["daily"] == [None, at(wed, 2, 10)] and nights.mean["daily"] == nights.lo["daily"] == [None, None]
    assert nights.hi["rain_rate"] == [0.0, 2.4]                            # (the rest as a day row has them)
    window = (s.id, tue, at(mon, 22), at(tue, 6) - 1, "5min")
    assert storage.reader.run(lambda r: r.part_days("rain", [window], ["daily"])[s.id].hi["daily"], ()) == [6.0]   # (what it isn't)
    assert figures(storage, s, mon, tue, "evening", ["daily"]).days.hi["daily"] == [pytest.approx(6.0), pytest.approx(0.5)]
    assert figures(storage, s, mon, tue, "afternoon", ["daily"]).days.hi["daily"] == [0.0, pytest.approx(1.0)]


def test_the_reading_before_a_stretch_may_be_at_the_other_resolution(storage):
    """A 5-minute day after one held only at 30 minutes: the rain in its first 5 minutes is the rise from the half hour before."""
    mon, tue = date(2026, 11, 2), date(2026, 11, 3)
    a, b = day_bounds(mon, TZ, True)
    put(storage, "rainfall", "30min", [{"ts": t, "daily": 3.0} for t in range(a, b, 1800)], a, b)
    a, b = day_bounds(tue, TZ, True)
    s = put(storage, "rainfall", "5min", [{"ts": t, "daily": 0.4} for t in range(a, b, 300)], a, b)   # 0.4 mm in its first 5 minutes
    assert figures(storage, s, tue, tue, "00:00-24:00", ["daily"]).days.hi["daily"] == [0.4]


def test_an_air_glitch_inside_the_stretch_is_taken_out_and_a_rise_that_lasts_is_not(storage):
    d = date(2026, 11, 3)
    start, end = at(d - DAY, 12), at(d, 12)
    pm = {t: 5.0 for t in range(start, end, 300)}
    pm[at(d - DAY, 22)] = pm[at(d, 2)] = 60.0           # one reading far above those either side: a glitch (at the night's edge too)
    pm[at(d, 4)] = pm[at(d, 4, 5)] = 30.0               # two in a row: the air
    s = put(storage, "air", "5min", [{"ts": t, "pm2_5": v} for t, v in pm.items()], start, end - 1)
    days = figures(storage, s, d, d, "night", ["pm2_5"]).days
    assert (days.hi["pm2_5"], days.hi_ts["pm2_5"]) == ([30.0], [at(d, 4)])
    assert days.n == [96 - 2]                           # (the two glitches' readings held nothing else)
    assert figures(storage, s, d, d, "02:00-03:00", ["pm2_5"]).days.hi["pm2_5"] == [5.0]


# ---------- the whole day ----------
def _same(got: list, row: list) -> bool:
    return all(g == pytest.approx(r) if isinstance(r, float) else g == r for g, r in zip(got, row, strict=True))


def _running(times: range, rng: random.Random):
    """(ts, the day's running rain total) at each reading: from 0 at local midnight, a shower now and then after the day's first hour."""
    total = 0.0
    for t in times:
        local = datetime.fromtimestamp(t, TZ)
        if local.time() == time(0):
            total = 0.0
        elif local.hour >= 1:
            total += rng.choice([0.0] * 6 + [0.2, 0.6])
        yield t, round(total, 1)


@pytest.mark.parametrize("group, res", [("outdoor", "5min"), ("outdoor", "30min"), ("air", "5min"), ("air", "1hour")])
def test_the_whole_day_has_its_day_rows_figures_wherever_it_is_held_whole(storage, group, res):
    """"00:00-24:00" over days held whole, a clock-change day among those at 5 minutes: each reading's figures, n and the resolution are
    its day row's (the rain counters' total too: the day's starts at midnight)."""
    rng = random.Random(f"{group} {res}")
    first, last = (date(2026, 4, 4), date(2026, 4, 6)) if res == "5min" else (date(2026, 2, 27), date(2026, 3, 1))
    start, end = at(first, 0), at(last + DAY, 0)
    times = range(start, end, SECONDS[res])
    if group == "air":   # a glitch now and then
        rows = [{"ts": t, "pm2_5": 60.0 if rng.random() < 0.05 else rng.uniform(3, 9), "co2": rng.uniform(400, 900)} for t in times]
    else:
        rows = [{"ts": t, "temperature": round(rng.uniform(5, 25), 1), "humidity": float(round(rng.uniform(40, 99)))} for t in times]
        if res == "30min":
            rows = [row | {"temperature_lo": row["temperature"] - rng.uniform(0, 2), "temperature_hi": row["temperature"] + rng.uniform(0, 2)}
                    for row in rows]
    sensors = [put(storage, group, res, rows, start, end - 1)]
    if group == "outdoor":
        if res == "5min":   # the half hours' own lows and highs, which a day of 5-minute readings takes in
            put(storage, "outdoor", "30min", [{"ts": t, "temperature": 15.0, "temperature_lo": rng.uniform(1, 5),
                                               "temperature_hi": rng.uniform(26, 30)} for t in range(start, end, 1800)], start, end - 1)
        sensors.append(put(storage, "rainfall", res, [{"ts": t, "daily": v} for t, v in _running(times, rng)], start, end - 1))
    for s in sensors:
        got = figures(storage, s, first, last, "00:00-24:00").days
        row = storage.reader.run(lambda r: r.days(s.kind, [s.id], first, last)[s.id], ())
        assert got.day == row.day == [first, first + DAY, last] and got.source == row.source == [res] * 3
        assert (got.n, got.complete, set(got.mean)) == (row.n, row.complete, set(row.mean))
        for c in row.mean:
            for part in ("hi", "hi_ts") if c in RAIN_COUNTERS else DAY_PARTS:
                assert _same(getattr(got, part)[c], getattr(row, part)[c]), (c, part)
        if res == "5min":
            assert got.n[1] == 25 * 12   # (the day the clocks went back)
