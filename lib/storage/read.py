"""Reading the file, on the reader's thread: what a request needs in as few queries as it takes, as columns of numbers (a list per reading)
rather than a dict per reading. The database does what is the same for every row: finding them (a range of the primary key), bucketing a
long line, picking the records; Python is left the time zones and what is drawn and said.
"""

from dataclasses import dataclass, field
from datetime import date
from .catalogue import KINDS, SLOT
from .days import windows_select
from .schema import q, some_value

FINE = ("1min", "5min")   # readings finer than a half hour: in a half hour that has them they make its mean (lines._summarise)


@dataclass
class Rows:
    """A sensor's readings at one resolution, oldest first. A reading the sensor never had in the period is left out."""
    ts: list[int] = field(default_factory=list)
    final: list[int] = field(default_factory=list)
    values: dict[str, list] = field(default_factory=dict)
    lo: dict[str, list] = field(default_factory=dict)
    hi: dict[str, list] = field(default_factory=dict)


@dataclass
class Buckets:
    """A sensor's readings in buckets of a width: each one's start, and per reading the bucket's mean, lowest and highest (None where the
    bucket has none of that reading)."""
    ts: list[int] = field(default_factory=list)
    mean: dict[str, list] = field(default_factory=dict)
    lo: dict[str, list] = field(default_factory=dict)
    hi: dict[str, list] = field(default_factory=dict)


@dataclass
class Days:
    """A sensor's day rows, oldest first (days.py says what each holds)."""
    day: list[date] = field(default_factory=list)
    source: list[str] = field(default_factory=list)
    timing: list[str] = field(default_factory=list)
    complete: list[bool] = field(default_factory=list)
    n: list[int] = field(default_factory=list)
    mean: dict[str, list] = field(default_factory=dict)
    lo: dict[str, list] = field(default_factory=dict)
    lo_ts: dict[str, list] = field(default_factory=dict)
    hi: dict[str, list] = field(default_factory=dict)
    hi_ts: dict[str, list] = field(default_factory=dict)


def _names(kind: str, columns: list[str] | None) -> list[str]:
    known = [c.name for c in KINDS[kind]]
    return known if columns is None else [c for c in columns if c in known]


def _marks(n: int) -> str:
    return ", ".join("?" * n)


DAY_PARTS = ("mean", "lo", "lo_ts", "hi", "hi_ts")   # a reading's figures in a day row, in its order


def _add_day(out: dict[int, "Days"], names: list[str], sensor: int, day: str, source: str, timing: str, complete: bool, n: int,
             figures: list, at: list[int]):
    """One day's row into its sensor's Days: each reading's figures from figures[5 * at[i]:] (its place in the row)."""
    got = out.get(sensor)
    if got is None:
        got = out[sensor] = Days(**{a: {c: [] for c in names} for a in DAY_PARTS})
    got.day.append(date.fromisoformat(day))
    got.source.append(source)
    got.timing.append(timing)
    got.complete.append(complete)
    got.n.append(n)
    for c, i in zip(names, at):
        for j, a in enumerate(DAY_PARTS):
            getattr(got, a)[c].append(figures[5 * i + j])


def _drop_empty(*tables: dict[str, list], keep: list[str] | None = None) -> None:
    """Leave out of these the readings with nothing in any of them (unless asked for by name)."""
    for name in list(tables[0]):
        if (keep is None or name not in keep) and all(all(v is None for v in t[name]) for t in tables):
            for t in tables:
                del t[name]


class Reader:
    def __init__(self, storage):
        self.storage = storage
        self.db = storage.read_db

    def run(self, fn, args):
        """fn(self, *args) in one read transaction: all its queries see the file as it was when the first one ran."""
        self.db.execute("BEGIN")
        try:
            return fn(self, *args)
        finally:
            self.db.execute("COMMIT")

    # ---------- what is there ----------
    def sensors(self, device: str | None = None, kind: str | None = None):
        return self.storage.sensors.all(device, kind)

    def sensor(self, device: str, external: str):
        return self.storage.sensors.find(device, external)

    def fields(self, sensor: int, res: str | None = None) -> dict[str, str]:
        """{field: unit} the sensor has sent (at this resolution, or any), as the source names them ("temperature_low")."""
        rows = self.db.execute("SELECT name, unit FROM fields WHERE sensor = ?" + (" AND res = ?" if res else ""), (sensor, res) if res else (sensor,))
        return dict(rows.fetchall())

    def held(self, sensor: int, res: str, start: int, end: int) -> bool:
        return self.storage.coverage.held(sensor, res, start, end)

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    # ---------- readings ----------
    def rows(self, kind: str, sensors: list[int], res: str, start: int, end: int, columns: list[str] | None = None,
             srcs: tuple[int, ...] | None = None) -> dict[int, Rows]:
        """{sensor: its readings at this resolution from start to end (epochs)}: one query, whatever the sensors. A row holding only a band
        (a reading not fetched yet) is left out; srcs: only the rows these sources wrote (catalogue.CLOUD ...)."""
        names = _names(kind, columns)
        if not sensors or not names:
            return {}
        cols = ", ".join(f"{q(c)}, {q(c + '_lo')}, {q(c + '_hi')}" for c in names)
        by_src = f" AND src IN ({_marks(len(srcs))})" if srcs else ""
        found = self.db.execute(f"SELECT sensor, ts, final, {cols} FROM {kind} WHERE sensor IN ({_marks(len(sensors))}) AND res = ? "
                                f"AND ts BETWEEN ? AND ? AND {some_value(names)}{by_src} ORDER BY sensor, ts",
                                (*sensors, res, start, end, *(srcs or ())))
        out: dict[int, Rows] = {}
        for sensor, ts, final, *values in found:
            got = out.get(sensor)
            if got is None:
                got = out[sensor] = Rows(values={c: [] for c in names}, lo={c: [] for c in names}, hi={c: [] for c in names})
            got.ts.append(ts)
            got.final.append(final)
            for i, c in enumerate(names):
                got.values[c].append(values[3 * i])
                got.lo[c].append(values[3 * i + 1])
                got.hi[c].append(values[3 * i + 2])
        for got in out.values():
            _drop_empty(got.values, got.lo, got.hi, keep=columns)
        return out

    def buckets(self, kind: str, sensors: list[int], res: tuple[str, ...], start: int, end: int, width: int,
                columns: list[str] | None = None) -> dict[int, Buckets]:
        """{sensor: its readings at these resolutions from start to end in epoch-aligned buckets of `width` seconds}, worked out by the
        database as lines.bucket_summary does: a bucket's mean is the mean of its half hours' means, where a half hour that has readings
        finer than itself takes its mean from those alone; its lowest and highest are of all its readings' own ranges (else their values)."""
        names = _names(kind, columns)
        if not sensors or not names:
            return {}
        fine = f"res IN ({', '.join(repr(r) for r in FINE)})"
        per_slot = ", ".join(
            f"COALESCE(AVG(CASE WHEN {fine} THEN {q(c)} END), AVG(CASE WHEN NOT {fine} THEN {q(c)} END)) AS {q(c + '~m')}, "
            f"MIN(CASE WHEN {q(c)} IS NOT NULL THEN COALESCE({q(c + '_lo')}, {q(c)}) END) AS {q(c + '~l')}, "
            f"MAX(CASE WHEN {q(c)} IS NOT NULL THEN COALESCE({q(c + '_hi')}, {q(c)}) END) AS {q(c + '~h')}" for c in names)
        per_bucket = ", ".join(f"AVG({q(c + '~m')}), MIN({q(c + '~l')}), MAX({q(c + '~h')})" for c in names)
        found = self.db.execute(
            f"WITH s AS (SELECT sensor, ts / :w * :w AS b, ts / {SLOT} AS slot, {per_slot} FROM {kind} "
            f"WHERE sensor IN ({', '.join(f':s{i}' for i in range(len(sensors)))}) AND res IN ({', '.join(f':r{i}' for i in range(len(res)))}) "
            f"AND ts BETWEEN :start AND :end GROUP BY sensor, b, slot) "
            f"SELECT sensor, b, {per_bucket} FROM s GROUP BY sensor, b ORDER BY sensor, b",
            {"w": width, "start": start, "end": end, **{f"s{i}": s for i, s in enumerate(sensors)}, **{f"r{i}": r for i, r in enumerate(res)}})
        out: dict[int, Buckets] = {}
        for sensor, b, *figures in found:
            got = out.get(sensor)
            if got is None:
                got = out[sensor] = Buckets(mean={c: [] for c in names}, lo={c: [] for c in names}, hi={c: [] for c in names})
            if all(v is None for v in figures):
                continue
            got.ts.append(b)
            for i, c in enumerate(names):
                got.mean[c].append(figures[3 * i])
                got.lo[c].append(figures[3 * i + 1])
                got.hi[c].append(figures[3 * i + 2])
        for got in out.values():
            _drop_empty(got.mean, got.lo, got.hi, keep=columns)
        return out

    # ---------- days ----------
    def days(self, kind: str, sensors: list[int], first: date, last: date, columns: list[str] | None = None) -> dict[int, Days]:
        """{sensor: its day rows from first to last}: one query, whatever the sensors."""
        names = _names(kind, columns)
        if not sensors or not names:
            return {}
        cols = ", ".join(q(f"{c}_{p}") for c in names for p in DAY_PARTS)
        found = self.db.execute(f"SELECT sensor, day, source, timing, complete, n, {cols} FROM {kind}_day WHERE sensor IN ({_marks(len(sensors))}) "
                                "AND day BETWEEN ? AND ? ORDER BY sensor, day", (*sensors, first.isoformat(), last.isoformat()))
        out: dict[int, Days] = {}
        for sensor, day, source, timing, complete, n, *figures in found:
            _add_day(out, names, sensor, day, source, timing, bool(complete), n, figures, list(range(len(names))))
        for got in out.values():
            _drop_empty(got.mean, got.lo, got.lo_ts, got.hi, got.hi_ts, keep=columns)
        return out

    def part_days(self, kind: str, windows: list[tuple[int, date, int, int, str]], columns: list[str] | None = None) -> dict[int, Days]:
        """{sensor: the figures of these parts of days, each (sensor, its day, start, end, the resolution to read), worked out from those
        readings as a day row is from a whole day's (days.py): one query, whatever the windows. A part with no readings is left out; one
        a day per sensor."""
        names = _names(kind, columns)
        if not windows or not names:
            return {}
        res_of = {(s, d.isoformat()): res for s, d, _, _, res in windows}
        at = [[c.name for c in KINDS[kind]].index(c) for c in names]
        out: dict[int, Days] = {}
        found = self.db.execute(windows_select(kind, len(windows)), [x for s, d, a, b, res in windows for x in (s, d.isoformat(), a, b, res)])
        for sensor, day, n, *figures in found:
            _add_day(out, names, sensor, day, res_of[sensor, day], res_of[sensor, day], False, n, figures, at)
        for got in out.values():
            _drop_empty(got.mean, got.lo, got.lo_ts, got.hi, got.hi_ts, keep=columns)
        return out

    def banded(self, kind: str, sensor: int, column: str, want: str, res: str, start: int, end: int) -> bool:
        """Does a reading of this resolution from start to end carry its own lowest ("lo") or highest ("hi") of the column: a figure from
        it is a true extreme, not an average."""
        return self.db.execute(f"SELECT 1 FROM {kind} WHERE sensor = ? AND res = ? AND ts BETWEEN ? AND ? AND {q(f'{column}_{want}')} "
                               "IS NOT NULL LIMIT 1", (sensor, res, start, end)).fetchone() is not None



__all__ = ["Buckets", "Days", "Reader", "Rows"]
