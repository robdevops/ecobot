"""Moving the old files' history into ecobot.sqlite, once (docs/storage-plan.md, section 5), and checking it before it counts.

The old files are only read (opened read-only), never changed or deleted, so the previous version of the bot still runs on them. The new
file is built as ecobot.sqlite.part and renamed only once, for every sensor and resolution, the number of readings and their sum match the
old files', and so do the coverage, the ranges, the minutes, the AirGradient's readings and the snapshots. Otherwise it is deleted and the
report says why. Nothing is fetched: the history is the bot's to keep (Ecowitt's cloud keeps 90 days of 5-minute readings).

What goes where:
  - ecowitt_cache.sqlite: each (station, cycle, group) becomes a sensor's readings at that resolution, a field and its _low and _high one
    row's value, low and high, the text a REAL (a field the catalogue has no column for, or a value that is not a number, goes in `extra`);
    its coverage becomes the sensor's.
  - ecowitt_live.sqlite: the ranges (two readings or more) widen the 5-minute rows, its half hours the 30-minute rows.
  - ecowitt_minutes.sqlite: the 1min rows.
  - airgradient_cache.sqlite: the readings (the bot's own marked so, from `recorded`), at 5 minutes, or an hour on a day of hourly
    averages; the spreads their bands; the days fetched its coverage.
  - conditions_cache.sqlite: the snapshots and their meta, as they are.
Then the 30-minute bands are widened from the 5-minute readings, and every day row is worked out.
"""

import logging
import math
import os
import shutil
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, tzinfo
from pathlib import Path

from ..timeutil import day_bounds, local_date
from . import FILE, Sensor, Storage
from .catalogue import BOT, BY_NAME, CLOUD, HOME_ASSISTANT, KINDS, column_of, text
from .coverage import merge
from .schema import q
from .write import pivot

log = logging.getLogger(__name__)

OLD = ("ecowitt_cache.sqlite", "ecowitt_live.sqlite", "ecowitt_minutes.sqlite", "airgradient_cache.sqlite", "conditions_cache.sqlite")
HOURLY_MAX_READINGS = 30   # an AirGradient day with this few readings holds hourly averages (as airgradient.days)
SPARE = 20e6               # bytes of disk asked for beyond twice the old files


@dataclass
class Report:
    ok: bool = False
    skipped: str = ""                                         # why nothing was done
    problem: str = ""                                         # why it failed
    seconds: float = 0.0
    moved: Counter = field(default_factory=Counter)           # what was moved: "station", "minutes", "bands", "AirGradient", "snapshots"
    extra: Counter = field(default_factory=Counter)           # "group.field": values kept as they came, the catalogue having no column for them
    odd: Counter = field(default_factory=Counter)             # "group.field": values that are not numbers, kept as they came
    differ: Counter = field(default_factory=Counter)          # "group.field": values written back with other digits
    example: dict = field(default_factory=dict)               # "group.field": one of each of those (the old text, and the new)
    sizes: tuple[int, int] = (0, 0)                           # bytes: the old files, the new one

    def lines(self) -> list[str]:
        if self.skipped:
            return [f"Storage: {self.skipped}"]
        if not self.ok:
            return [f"Couldn't move the history to {FILE}: {self.problem}; the bot carries on with the old files"]
        moved = ", ".join(f"{n:,} {what}" for what, n in self.moved.items() if n)
        out = [f"Moved the history to {FILE} in {self.seconds:.1f}s: {moved}; {self.sizes[0] / 1e6:.1f} MB of old files, "
               f"{self.sizes[1] / 1e6:.1f} MB now"]
        if self.extra:
            out.append("No column for them yet, kept as they came: " + ", ".join(
                f"{k} {n:,} ({self.example[k][0]!r})" for k, n in sorted(self.extra.items())))
        if self.odd:
            out.append("Not numbers, kept as they came (no answer reads them): " + ", ".join(
                f"{k} {n:,} ({self.example[k][0]!r})" for k, n in sorted(self.odd.items())))
        if self.differ:
            out.append("Written back with other digits: " + ", ".join(
                f"{k} {n:,} ({self.example[k][0]!r} is now {self.example[k][1]!r})" for k, n in sorted(self.differ.items())))
        return out


class Mismatch(Exception):
    """The new file does not hold what the old ones do."""


def old_files(folder: Path) -> list[Path]:
    return [folder / name for name in OLD if (folder / name).exists()]


def _open_old(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return db


def _tables(db: sqlite3.Connection) -> set[str]:
    return {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def migrate(folder: Path, tz: tzinfo, target: Path | None = None) -> Report:
    """Build `target` (ecobot.sqlite in `folder`) from the old files there, unless it exists already."""
    folder = Path(folder)
    target = Path(target) if target else folder / FILE
    report = Report()
    if target.exists():
        report.skipped = f"{target.name} is there already"
        return report
    old = old_files(folder)
    if not old:
        report.skipped = "no old files to move"
        return report
    size = sum(p.stat().st_size for p in old)
    if (free := shutil.disk_usage(target.parent).free) < 2 * size + SPARE:
        report.problem = f"{free / 1e6:.0f} MB free; it needs {(2 * size + SPARE) / 1e6:.0f} MB"
        return report
    part = target.with_name(target.name + ".part")
    for leftover in (part, Path(f"{part}-journal")):
        leftover.unlink(missing_ok=True)   # a run that was cut short
    started = time.monotonic()
    storage = Storage(part, tz, bulk=True)
    try:
        with storage.writer.transaction() as w:
            mac = _ecowitt(w, folder / OLD[0], report)
            if mac:
                _live(w, folder / OLD[1], mac, report)
                _minutes(w, folder / OLD[2], mac, report)
            _air(w, folder / OLD[3], tz, report)
            _snapshots(w, folder / OLD[4], report)
            for sensor in w.all_sensors():   # the 30-minute bands, from the 5-minute readings now all in
                if sensor.kind in KINDS:
                    w.roll_up(sensor, 0, 2 ** 40)
            w.set_meta("migrated", ", ".join(p.name for p in old))
    except Exception as e:
        storage.close()
        part.unlink(missing_ok=True)
        report.problem = str(e) if isinstance(e, Mismatch) else f"{type(e).__name__}: {e}"
        log.warning("%s", report.lines()[0])
        return report
    storage.close()
    db = sqlite3.connect(part)
    db.execute("PRAGMA journal_mode = WAL")   # as the bot keeps it
    db.close()
    os.replace(part, target)
    report.ok, report.seconds, report.sizes = True, time.monotonic() - started, (size, target.stat().st_size)
    return report


# ---------- the station ----------
def _ecowitt(w, path: Path, report: Report) -> str | None:
    """The station's readings, fields and coverage; returns the MAC with the most readings (whose the live ranges and minutes are)."""
    db = _open_old(path)
    if db is None:
        return None
    from ..ecowitt.api import UNIT_FIXES
    try:
        if not {"series", "points", "coverage"} <= _tables(db):
            return None
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall()) if "meta" in _tables(db) else {}
        if "signature" in meta:
            w.set_meta("ecowitt_signature", meta["signature"])
        fixed = meta.get("unit_fixes") == repr(sorted(UNIT_FIXES.items()))   # (else the vpd is still in inHg: converted, as HistoryCache does)
        expected: dict[tuple[Sensor, str, str], list[float]] = defaultdict(lambda: [0, 0.0])
        per_mac: Counter = Counter()
        for mac, cycle, group in db.execute("SELECT DISTINCT mac, cycle, grp FROM series ORDER BY mac, cycle, grp").fetchall():
            sensor = w.ecowitt_sensor(mac, group)
            plan: dict[int, tuple] = {}   # series id -> (its field, the row's key for it or None (extra), its decimals, a unit fix's factor)
            units = {}
            for sid, name, unit in db.execute("SELECT id, field, unit FROM series WHERE mac = ? AND cycle = ? AND grp = ?", (mac, cycle, group)):
                factor = None
                for (prefix, was), (now_unit, by) in UNIT_FIXES.items():
                    if not fixed and unit == was and (name == prefix or name.startswith(prefix + "_")):
                        factor, unit = by, now_unit
                units[name] = unit
                place = column_of(group, name)
                key = None if place is None else place[0] if place[1] == "value" else f"{place[0]}_{place[1]}"
                column = BY_NAME[sensor.kind][place[0]] if place else None
                plan[sid] = (name, key, (column.decimals, column.whole) if column else (0, False), factor)
            w.fields(sensor, cycle, units)
            rows: dict[int, dict] = {}
            extra = []
            seen: dict[tuple, tuple] = {}            # (text, decimals) -> (its number, written back the same): values repeat
            moved: Counter = Counter()
            totals: dict[int, float] = defaultdict(float)
            for sid, ts, raw in db.execute(f"SELECT series, ts, value FROM points WHERE series IN ({','.join('?' * len(plan))}) ORDER BY ts",
                                           tuple(plan)):
                name, key, decimals, factor = plan[sid]
                if factor is not None and (number := _number(raw)) is not None:
                    raw = f"{number * factor:.3f}"
                got = seen.get((raw, decimals))
                if got is None:
                    number = _number(raw)
                    got = seen[raw, decimals] = (number, number is None or text(number, *decimals) == str(raw))
                number, same = got
                if key is None or number is None:
                    extra.append((name, ts, str(raw)))
                    (report.extra if key is None else report.odd)[f"{group}.{name}"] += 1
                    report.example.setdefault(f"{group}.{name}", (str(raw), ""))
                    continue
                rows.setdefault(ts, {"ts": ts})[key] = number
                moved[sid] += 1
                totals[sid] += number
                if not same:
                    report.differ[f"{group}.{name}"] += 1
                    report.example.setdefault(f"{group}.{name}", (str(raw), text(number, *decimals)))
            for sid, n in moved.items():
                got = expected[sensor, cycle, plan[sid][1]]
                got[0], got[1] = got[0] + n, got[1] + totals[sid]
            w.put(sensor, cycle, [rows[t] for t in sorted(rows)], CLOUD, exact=True)
            w.extra(sensor, cycle, extra)
            per_mac[mac] += sum(moved.values()) + len(extra)
            report.moved["station readings"] += sum(moved.values()) + len(extra)
        _check_cells(w, expected, "the station")
        old_coverage: dict[tuple, list] = defaultdict(list)
        for mac, cycle, group, start, end in db.execute('SELECT mac, cycle, grp, start, "end" FROM coverage').fetchall():
            sensor = w.ecowitt_sensor(mac, group)
            w.cover(sensor, cycle, start, end)
            old_coverage[sensor.id, cycle].append((start, end))
        for (sensor, cycle), spans in old_coverage.items():
            if w.coverage_of(sensor, cycle) != merge(spans):
                raise Mismatch(f"the coverage of sensor {sensor} at {cycle} differs")
        return per_mac.most_common(1)[0][0] if per_mac else None
    finally:
        db.close()


def _number(raw) -> float | None:
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _check_cells(w, expected: dict[tuple[Sensor, str, str], list[float]], what: str):
    """Every (sensor, resolution, cell) holds as many values as were moved into it, with the same sum."""
    by_table: dict[tuple[Sensor, str], list] = defaultdict(list)
    for (sensor, res, key), want in expected.items():
        by_table[sensor, res].append((key, want))
    for (sensor, res), cells in by_table.items():
        found = w.db.execute(f"SELECT {', '.join(f'COUNT({q(k)}), TOTAL({q(k)})' for k, _ in cells)} FROM {sensor.kind} "
                             "WHERE sensor = ? AND res = ?", (sensor.id, res)).fetchone()
        for i, (key, (n, total)) in enumerate(cells):
            have_n, have_total = found[2 * i], found[2 * i + 1]
            if have_n != n or not math.isclose(have_total, total, rel_tol=1e-9, abs_tol=1e-6):
                raise Mismatch(f"{what}: {sensor.device} {sensor.external} {res} {key} holds {have_n} values summing {have_total:.6g}, "
                               f"not {n} summing {total:.6g}")


def _live(w, path: Path, mac: str, report: Report):
    """The bot's own ranges of the station's readings: each 5 minutes with two readings or more, and their half hours."""
    db = _open_old(path)
    if db is None:
        return
    try:
        tables = _tables(db)
        found: dict[str, dict[int, tuple]] = defaultdict(dict)
        halves: dict[tuple[str, str], dict] = defaultdict(dict)
        if "ranges" in tables:
            for series, ts, n, lo, hi in db.execute("SELECT series, ts, n, lo, hi FROM ranges").fetchall():
                found[series][ts] = (n, lo, hi)
        report.moved["bands"] += w.put_ranges(mac, found)
        if "slots" in tables:
            for series, ts, lo, hi in db.execute("SELECT series, ts, lo, hi FROM slots").fetchall():
                group, name = series.split(".", 1)
                if (place := column_of(group, name)) and place[1] == "value":
                    halves[group, place[0]][ts] = (lo, hi)
            for (group, column), bands in halves.items():
                report.moved["bands"] += w.widen(w.ecowitt_sensor(mac, group), "30min", {column: bands})
        _check_bands(w, mac, found, halves)
    finally:
        db.close()


def _check_bands(w, mac: str, found: dict, halves: dict):
    """Each range is inside its row's band."""
    for series, marks in found.items():
        group, name = series.split(".", 1)
        place = column_of(group, name)
        if place is None or place[1] != "value":
            continue
        sensor = w.ecowitt_sensor(mac, group)
        column = place[0]
        held = dict((ts, (lo, hi)) for ts, lo, hi in w.db.execute(
            f"SELECT ts, {q(column + '_lo')}, {q(column + '_hi')} FROM {sensor.kind} WHERE sensor = ? AND res = '5min'", (sensor.id,)))
        for ts, (n, lo, hi) in marks.items():
            if n >= 2 and not (ts in held and held[ts][0] <= lo and held[ts][1] >= hi):
                raise Mismatch(f"the range of {series} at {ts} is not in its row")
    for (group, column), bands in halves.items():
        sensor = w.ecowitt_sensor(mac, group)
        held = dict((ts, (lo, hi)) for ts, lo, hi in w.db.execute(
            f"SELECT ts, {q(column + '_lo')}, {q(column + '_hi')} FROM {sensor.kind} WHERE sensor = ? AND res = '30min'", (sensor.id,)))
        for ts, (lo, hi) in bands.items():
            if not (ts in held and held[ts][0] <= lo and held[ts][1] >= hi):
                raise Mismatch(f"the half hour of {group}.{column} at {ts} is not in its row")


def _minutes(w, path: Path, mac: str, report: Report):
    """Home Assistant's minutes of the station."""
    db = _open_old(path)
    if db is None:
        return
    try:
        if "minutes" not in _tables(db):
            return
        expected: dict[tuple[Sensor, str, str], list[float]] = defaultdict(lambda: [0, 0.0])
        by_group: dict[str, dict] = defaultdict(dict)
        for group, name, t, unit, value in db.execute("SELECT grp, field, t, unit, value FROM minutes ORDER BY t").fetchall():
            by_group[group].setdefault(name, {"unit": unit, "list": {}})["list"][str(t)] = value
        for group, fields in by_group.items():
            sensor = w.ecowitt_sensor(mac, group)
            rows, extra, units = pivot(group, fields, 0, 2 ** 40)
            w.fields(sensor, "1min", units)
            w.put(sensor, "1min", rows, HOME_ASSISTANT, exact=True)
            w.extra(sensor, "1min", extra)
            for row in rows:
                for key, v in row.items():
                    if key != "ts":
                        got = expected[sensor, "1min", key]
                        got[0], got[1] = got[0] + 1, got[1] + v
            report.moved["minutes"] += sum(len(r) - 1 for r in rows) + len(extra)
        _check_cells(w, expected, "the minutes")
    finally:
        db.close()


# ---------- the AirGradient ----------
def _air(w, path: Path, tz: tzinfo, report: Report):
    db = _open_old(path)
    if db is None:
        return
    from ..airgradient.metrics import STORED
    try:
        tables = _tables(db)
        if "readings" not in tables:
            return
        columns = [c for c in STORED if c in {row[1] for row in db.execute("PRAGMA table_info(readings)")}]
        expected: dict[tuple[Sensor, str, str], list[float]] = defaultdict(lambda: [0, 0.0])
        for (loc,) in db.execute("SELECT DISTINCT loc FROM readings").fetchall():
            sensor = w.air_sensor(loc)
            mine = {ts for (ts,) in db.execute("SELECT ts FROM recorded WHERE loc = ?", (loc,))} if "recorded" in tables else set()
            done = dict(db.execute("SELECT day, n FROM days WHERE loc = ?", (loc,)).fetchall()) if "days" in tables else {}
            found = db.execute(f"SELECT ts, {', '.join(columns)} FROM readings WHERE loc = ? ORDER BY ts", (loc,)).fetchall()
            by_res: dict[tuple[str, int], list[dict]] = defaultdict(list)
            res_of: dict[int, str] = {}
            for ts, *values in found:
                day = local_date(ts, tz)
                res = "1hour" if 0 < (done.get(day.isoformat()) or 0) <= HOURLY_MAX_READINGS else "5min"   # (a finished day of hourly averages)
                res_of[ts] = res
                row = {"ts": ts, "final": int(day.isoformat() in done or ts in mine)}
                for c, v in zip(columns, values):
                    if v is not None:
                        row[c] = v
                        got = expected[sensor, res, c]
                        got[0], got[1] = got[0] + 1, got[1] + v
                by_res[res, BOT if ts in mine else CLOUD].append(row)
            for (res, src), rows in by_res.items():
                w.put(sensor, res, rows, src, exact=True)
                report.moved["AirGradient readings"] += sum(len(r) - 2 for r in rows)
            w.fields(sensor, "5min", {c: STORED[c][1] for c in columns})
            _check_cells(w, expected, "the AirGradient")
            bot = w.db.execute("SELECT COUNT(*) FROM air WHERE sensor = ? AND src = ?", (sensor.id, BOT)).fetchone()[0]
            if bot != len(mine & set(res_of)):
                raise Mismatch(f"the AirGradient {loc}: {bot} of the bot's own readings, not {len(mine & set(res_of))}")
            if "spreads" in tables:
                bands: dict[tuple[str, str], dict] = defaultdict(dict)
                for ts, name, lo, hi in db.execute("SELECT ts, name, lo, hi FROM spreads WHERE loc = ?", (loc,)).fetchall():
                    if name in BY_NAME["air"]:
                        bands[res_of.get(ts, "5min"), name][ts] = (lo, hi)
                for (res, name), got in bands.items():
                    report.moved["bands"] += w.widen(sensor, res, {name: got})
            for day, n in done.items():
                start, end = day_bounds(date.fromisoformat(day), tz)
                w.cover(sensor, "1hour" if 0 < n <= HOURLY_MAX_READINGS else "5min", start, end - 1)
    finally:
        db.close()


# ---------- the pollen page and the forecast ----------
def _snapshots(w, path: Path, report: Report):
    db = _open_old(path)
    if db is None:
        return
    try:
        tables = _tables(db)
        if "snapshots" in tables:
            rows = db.execute("SELECT id, kind, first_seen, last_seen, payload FROM snapshots").fetchall()
            w.db.executemany("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)", rows)
            report.moved["snapshots"] += len(rows)
            if w.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] != len(rows):
                raise Mismatch("the snapshots differ")
        if "meta" in tables:
            w.db.executemany("INSERT INTO snapshot_meta VALUES (?, ?)", db.execute("SELECT key, value FROM meta").fetchall())
    finally:
        db.close()


__all__ = ["OLD", "Report", "migrate", "old_files"]
