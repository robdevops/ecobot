"""Each sensor's local days, worked out once from its readings and kept as day rows (`<kind>_day`), so the dashboard, `weather_days`, a
period's records and a long chart read a few hundred day rows instead of every reading.

A day's figures come from the finest readings that hold the whole local day, as `ecowitt.days.day_values` chose them: the 5-minute ones
where the day is covered at 5 minutes, else the 30-minute ones, else Ecowitt's daily record once it is final (10am to 10am in Melbourne,
labelled with the local date its start falls on: approximate). A day none of those hold (today, the edge of what was fetched) is worked
out from what there is (the newest readings, still settling, too), marked incomplete. Each reading's figures:
  - its mean: of each half hour's mean, so a stretch with readings missing counts for its time, not its readings (as lines._summarise);
  - its lowest and highest: of each reading's own range where it has one (Ecowitt's lows and highs, the bands of the bot's and Home
    Assistant's readings), else of its value; and when they first happened. A day of 5-minute readings takes in its 30-minute readings'
    bands too (Ecowitt's lows and highs from its finer readings, which its 5-minute ones miss), timed by the 5-minute reading lowest or
    highest in that half hour (where refining an extreme found it); its mean stays the 5-minute readings'.
A day from a daily record takes the times of its lowest and highest from the 4-hour readings, where those are held ("sometime between 2pm
and 6pm", as refining an extreme finds it); its figures stay the record's, as day_values has them.

Done in SQL: Python only chooses each day's resolution and window (it knows the coverage and the time zone) and puts them in a temporary
table, and one INSERT ... SELECT per kind of sensor works out all of them.
"""

import sqlite3
from collections.abc import Callable
from datetime import date, timedelta, tzinfo

from ..timeutil import day_bounds, local_date
from .catalogue import KINDS, SLOT
from .schema import day_columns, q, some_value

DAY = 86400
ANY = "*"   # what is held changed: a day may come from another resolution now
WANT = ('CREATE TEMP TABLE IF NOT EXISTS want (sensor INTEGER, day TEXT, "start" INTEGER, "end" INTEGER, res TEXT, complete INTEGER, '
        "PRIMARY KEY (sensor, day))")
MISSING = "SELECT w.sensor, w.day FROM temp.want w LEFT JOIN {kind}_day d ON d.sensor = w.sensor AND d.day = w.day WHERE d.sensor IS NULL"

type Held = Callable[[int, str, int, int], bool]


def _with(kind: str) -> str:
    """The figures of temp.want's days (`figures`), from the rows of their windows (only those with a value: a row holding only a band,
    for a reading not yet fetched, is no reading) and, for a day of 5-minute readings, the bands of its 30-minute ones (Ecowitt's own lows
    and highs, from readings finer than its 5 minutes: they count in the day's lowest and highest, never in its mean). Worked out by half
    hours: their mean, lowest and highest, and when the day's lowest and highest happened, the first half hour that holds them (a window
    over the day's half hours, not its readings)."""
    names = [c.name for c in KINDS[kind]]
    ranged = [c.name for c in KINDS[kind] if c.ranged]
    columns = ", ".join(f"r.{q(c)} AS {q(c + '~v')}, CASE WHEN r.{q(c)} IS NOT NULL THEN COALESCE(r.{q(c + '_lo')}, r.{q(c)}) END AS {q(c + '~l')}, "
                        f"CASE WHEN r.{q(c)} IS NOT NULL THEN COALESCE(r.{q(c + '_hi')}, r.{q(c)}) END AS {q(c + '~h')}" for c in names)
    bands = ", ".join(f"NULL AS {q(c + '~v')}, " + (f"r.{q(c + '_lo')} AS {q(c + '~l')}, r.{q(c + '_hi')} AS {q(c + '~h')}" if c in ranged
                      else f"NULL AS {q(c + '~l')}, NULL AS {q(c + '~h')}") for c in names)
    within = 'r.sensor = w.sensor AND r.res = {} AND r.ts BETWEEN w."start" AND w."end"'   # (want first: it is a few rows, the readings many)
    halves = (f"UNION ALL SELECT w.sensor, w.day, r.ts, r.ts / {SLOT} AS slot, 0 AS own, {bands} FROM temp.want w CROSS JOIN {kind} r "
              f"ON {within.format(repr('30min'))} WHERE w.res = '5min' "
              f"AND {some_value([c + part for c in ranged for part in ('_lo', '_hi')], 'r.')}") if ranged else ""
    slots = ", ".join(f"AVG({q(c + '~v')}) AS {q(c + '~m')}, MIN({q(c + '~l')}) AS {q(c + '~lo')}, MAX({q(c + '~h')}) AS {q(c + '~hi')}"
                      for c in names)
    window = ", ".join(f"MIN({q(c + '~lo')}) OVER whole AS {q(c + '~dl')}, MAX({q(c + '~hi')}) OVER whole AS {q(c + '~dh')}" for c in names)
    figures = ", ".join(f"AVG({q(c + '~m')}) AS {q(c + '~m')}, MIN({q(c + '~lo')}) AS {q(c + '~lo')}, "
                        f"MIN(CASE WHEN {q(c + '~lo')} = {q(c + '~dl')} THEN t END) AS {q(c + '~lt')}, MAX({q(c + '~hi')}) AS {q(c + '~hi')}, "
                        f"MIN(CASE WHEN {q(c + '~hi')} = {q(c + '~dh')} THEN t END) AS {q(c + '~ht')}" for c in names)
    return (f"WITH src AS (SELECT w.sensor, w.day, r.ts, r.ts / {SLOT} AS slot, 1 AS own, {columns} FROM temp.want w CROSS JOIN {kind} r "
            f"ON {within.format('w.res')} WHERE {some_value(names, 'r.')} "
            f"AND (r.final OR w.res IN ('1min', '5min', '30min')) {halves}), "   # (a daily record, or its 4 hours, once final)
            f"slots AS (SELECT sensor, day, slot, SUM(own) AS n, MIN(ts) AS t, {slots} FROM src GROUP BY sensor, day, slot), "
            f"marked AS (SELECT *, {window} FROM slots WINDOW whole AS (PARTITION BY sensor, day)), "
            f"figures AS (SELECT m.sensor, m.day, w.res, w.complete, SUM(m.n) AS n, {figures} FROM marked m JOIN temp.want w "
            f"ON w.sensor = m.sensor AND w.day = m.day GROUP BY m.sensor, m.day HAVING SUM(m.n) > 0) ")


def _figures(kind: str) -> str:
    """Each reading's figures over the day, in the day table's order: mean, lowest, its time, highest, its time. In a day of 5-minute
    readings, the time is the first of the half hour's own readings that is lowest or highest in it (where refining an extreme found it;
    the half hour's, where it has none of its own): a look at a few rows, for the one half hour."""
    out = []
    for c in KINDS[kind]:
        v, lo, hi = q(c.name), q(c.name + "_lo"), q(c.name + "_hi")
        lt, ht = q(c.name + "~lt"), q(c.name + "~ht")
        first = (f"(SELECT r.ts FROM {kind} r WHERE r.sensor = f.sensor AND r.res = '5min' AND r.ts BETWEEN f.{{0}} - f.{{0}} % {SLOT} "
                 f"AND f.{{0}} - f.{{0}} % {SLOT} + {SLOT - 1} AND r.{v} IS NOT NULL ORDER BY {{1}}, r.ts LIMIT 1)")
        when = lambda t, order: f"CASE WHEN f.res = '5min' THEN COALESCE({first.format(t, order)}, f.{t}) ELSE f.{t} END"
        out.append(f"f.{q(c.name + '~m')}, f.{q(c.name + '~lo')}, {when(lt, f'COALESCE(r.{lo}, r.{v})')}, f.{q(c.name + '~hi')}, "
                   f"{when(ht, f'COALESCE(r.{hi}, r.{v}) DESC')}")
    return ", ".join(out)


_SQL: dict[str, tuple[str, str]] = {}


def statements(kind: str) -> tuple[str, str]:
    """(the INSERT of the day rows of temp.want's windows, the SELECT of their figures, for reading back): generated once per kind."""
    if kind not in _SQL:
        names = ", ".join(q(n) for n, _ in day_columns(kind))
        insert = (f"{_with(kind)}INSERT INTO {kind}_day (sensor, day, source, timing, complete, n, {names}) "
                  f"SELECT f.sensor, f.day, f.res, f.res, f.complete, f.n, {_figures(kind)} FROM figures f")
        select = f"{_with(kind)}SELECT f.sensor, f.day, f.n, {_figures(kind)} FROM figures f"
        _SQL[kind] = insert, select
    return _SQL[kind]


def utc_days(day: date, tz: tzinfo) -> tuple[int, int] | None:
    """(the first, the last) of Ecowitt's daily records labelled with this local day: the UTC midnights the local day holds (a 25-hour day
    can hold two, a 23-hour one none). None for none."""
    start, end = day_bounds(day, tz, True)
    found = [t for t in range(start - start % DAY, end + 1, DAY) if t >= start]
    return (found[0], found[-1]) if found else None


def work_out(db: sqlite3.Connection, kind: str, wanted: dict[int, dict[date, set[str]]], held: Held, tz: tzinfo):
    """Work out these sensors' days again ({sensor: {day: the resolutions written that day, or ANY}}): their rows replaced, or gone when no
    readings are left for them. A day held whole at a resolution is left as it is when nothing of that resolution, nor what is held, changed
    (a day of 5-minute readings has nothing to take from its 30-minute or daily ones: the archive writes every day at each resolution)."""
    insert, select = statements(kind)
    db.execute(WANT)
    complete, records, rest, again = [], [], set(), []
    for sensor, days in wanted.items():
        for d, changed in days.items():
            start, end = day_bounds(d, tz, True)
            res = next((r for r in ("5min", "30min") if held(sensor, r, start, end)), None)
            if res and not changed & {res, ANY}:
                continue
            again.append((sensor, d.isoformat()))
            if res:   # held whole: that resolution, even if it has no readings (the station was off), as day_values does
                complete.append((sensor, d.isoformat(), start, end, res, 1))
            elif (utc := utc_days(d, tz)) is not None:
                records.append((sensor, d.isoformat(), *utc, "1day", 0))
            else:
                rest.add((sensor, d))
    db.executemany(f"DELETE FROM {kind}_day WHERE sensor = ? AND day = ?", again)
    _insert(db, insert, complete)
    if records:
        _insert(db, insert, records)
        rest |= {(s, date.fromisoformat(d)) for s, d in db.execute(MISSING.format(kind=kind)).fetchall()}
        _time_from_four_hours(db, kind, select, records, held)
    for res in ("5min", "30min"):   # neither held whole nor in a daily record: what there is, marked incomplete
        if not rest:
            break
        _insert(db, insert, [(s, d.isoformat(), *day_bounds(d, tz, True), res, 0) for s, d in sorted(rest)])
        rest = {(s, date.fromisoformat(d)) for s, d in db.execute(MISSING.format(kind=kind)).fetchall()}


def _insert(db: sqlite3.Connection, insert: str, windows: list[tuple]):
    db.execute("DELETE FROM temp.want")
    if windows:
        db.executemany("INSERT INTO temp.want VALUES (?, ?, ?, ?, ?, ?)", windows)
        db.execute(insert)


def _time_from_four_hours(db: sqlite3.Connection, kind: str, select: str, records: list[tuple], held: Held):
    """The days from daily records: when their lowest and highest happened, from the 4-hour readings of the record's UTC day where those
    are held (the 4 hours with the lowest low, the highest high)."""
    windows = [(s, day, first, last + DAY - 1, "4hour", 0) for s, day, first, last, _, _ in records if held(s, "4hour", first, last + DAY - 1)]
    db.execute("DELETE FROM temp.want")
    if not windows:
        return
    db.executemany("INSERT INTO temp.want VALUES (?, ?, ?, ?, ?, ?)", windows)
    names = [c.name for c in KINDS[kind]]
    for sensor, day, _, *figures in db.execute(select).fetchall():
        sets, args = ["timing = '4hour'"], []
        for i, c in enumerate(names):
            _, lo, lo_ts, hi, hi_ts = figures[5 * i: 5 * i + 5]
            if lo is not None:
                sets.append(f"{q(c + '_lo_ts')} = CASE WHEN {q(c + '_lo')} IS NOT NULL THEN ? END")
                args.append(lo_ts)
            if hi is not None:
                sets.append(f"{q(c + '_hi_ts')} = CASE WHEN {q(c + '_hi')} IS NOT NULL THEN ? END")
                args.append(hi_ts)
        db.execute(f"UPDATE {kind}_day SET {', '.join(sets)} WHERE sensor = ? AND day = ?", (*args, sensor, day))


def days_of(start: int, end: int, tz: tzinfo, utc: bool = True) -> set[date]:
    """The local days [start, end] (epochs) touches, and (utc) the day of each daily record it touches (a daily record and its 4-hour
    readings belong to the local day their UTC day starts on)."""
    first, last = local_date(start, tz), local_date(end, tz)
    if utc:
        first = min(first, local_date(start - start % DAY, tz))
    return {first + timedelta(days=n) for n in range((last - first).days + 1)}


__all__ = ["ANY", "days_of", "statements", "utc_days", "work_out"]
