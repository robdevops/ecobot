"""Moving the old files' history into ecobot.sqlite, once (docs/storage-plan.md, section 5), and checking it before it counts.

The old files are only read (opened read-only), never changed or deleted, so the previous version of the bot still runs on them. What they
hold is moved in parts, each when its code moves to the new storage: the station's (step 2), the AirGradient's (step 3), the snapshots
(step 5); the file notes each part moved (meta `migrated.<part>`). A move is built as ecobot.sqlite.part and counts only once, for every
sensor and resolution, the number of readings and their sum match the old files', and so do the coverage, the ranges, the minutes, the
AirGradient's readings and the snapshots; otherwise it is deleted and the report says why. With no ecobot.sqlite yet the built file
becomes it; into one that is there (a part moved later than the others, or a move tried again after one that failed while the bot went on
with a new file) it is merged: the file's own readings win, the old ones fill in what it lacks, a band takes in both, and so does the
coverage. Nothing is fetched: the history is the bot's to keep (Ecowitt's cloud keeps 90 days of 5-minute readings).

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

PARTS = {"station": ("ecowitt_cache.sqlite", "ecowitt_live.sqlite", "ecowitt_minutes.sqlite"),   # the old files of each part, by name
         "airgradient": ("airgradient_cache.sqlite",), "snapshots": ("conditions_cache.sqlite",)}
OLD = tuple(name for names in PARTS.values() for name in names)
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
            return [f"Couldn't move the history to {FILE}: {self.problem}. The old files are as they were; it is tried again at the "
                    "next start (meanwhile the bot keeps what its sources still have)"]
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


def old_files(folder: Path) -> dict[str, tuple[Path, ...]]:
    """Each part's old files, as they are named in the data folder."""
    return {part: tuple(Path(folder) / name for name in names) for part, names in PARTS.items()}


def history_files(cfg) -> dict[str, tuple[Path, ...]]:
    """Each part's old files as the settings name them (the files from before ecobot.sqlite, moved into it once)."""
    return {"station": (cfg.cache_path, cfg.cache_path.with_name("ecowitt_live.sqlite"), cfg.cache_path.with_name("ecowitt_minutes.sqlite")),
            "airgradient": (cfg.air_cache_path,), "snapshots": (cfg.conditions_cache_path,)}


def moved(target: Path) -> set[str]:
    """The parts already moved into the file (none when there is no file)."""
    if not target.exists():
        return set()
    db = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        return {key.removeprefix("migrated.") for (key,) in db.execute("SELECT key FROM meta WHERE key LIKE 'migrated.%'")}
    except sqlite3.Error:   # (a file with no meta yet)
        return set()
    finally:
        db.close()


def _open_old(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return db


def _tables(db: sqlite3.Connection) -> set[str]:
    return {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def migrate(folder: Path, tz: tzinfo, target: Path | None = None, parts=tuple(PARTS), files: dict | None = None) -> Report:
    """Move these parts' history from the old files (`files`: {part: its files}; by default those named so in `folder`) into `target`
    (ecobot.sqlite in `folder`), those not moved already."""
    folder = Path(folder)
    target = Path(target) if target else folder / FILE
    files = {part: tuple(map(Path, paths)) for part, paths in (files or old_files(folder)).items()}
    report = Report()
    done = moved(target)
    due = [part for part in parts if part not in done and any(p.exists() for p in files.get(part, ()))]
    if not due:
        report.skipped = f"{target.name} is there already" if target.exists() else "no old files to move"
        return report
    old = [p for part in due for p in files[part] if p.exists()]
    size = sum(p.stat().st_size for p in old)
    if (free := shutil.disk_usage(target.parent).free) < 2 * size + SPARE:
        report.problem = f"{free / 1e6:.0f} MB free; it needs {(2 * size + SPARE) / 1e6:.0f} MB"
        return report
    log.info("Moving the history to %s (once): %s", target.name, ", ".join(p.name for p in old))
    part = target.with_name(target.name + ".part")
    for leftover in (part, Path(f"{part}-journal"), Path(f"{part}-wal"), Path(f"{part}-shm")):
        leftover.unlink(missing_ok=True)   # a run that was cut short
    started = time.monotonic()
    storage = Storage(part, tz, bulk=True)
    try:
        with storage.writer.transaction() as w:
            if "station" in due:
                cache, live, minutes = (files["station"] + (Path("/nonexistent"),) * 3)[:3]
                if mac := _ecowitt(w, cache, report):
                    _live(w, live, mac, report)
                    _minutes(w, minutes, mac, report)
            if "airgradient" in due:
                _air(w, files["airgradient"][0], tz, report)
            if "snapshots" in due:
                _snapshots(w, files["snapshots"][0], report)
            for sensor in w.all_sensors():   # the 30-minute bands, from the 5-minute readings now all in
                if sensor.kind in KINDS:
                    w.roll_up(sensor, 0, 2 ** 40)
            for name in due:
                w.set_meta(f"migrated.{name}", ", ".join(p.name for p in files[name] if p.exists()))
    except Exception as e:
        storage.close()
        part.unlink(missing_ok=True)
        report.problem = str(e) if isinstance(e, Mismatch) else f"{type(e).__name__}: {e}"
        return report
    storage.close()
    try:
        if target.exists():
            _merge(part, target, tz)
            part.unlink()
        else:
            db = sqlite3.connect(part)
            db.execute("PRAGMA journal_mode = WAL")   # as the bot keeps it
            db.close()
            os.replace(part, target)
    except Exception as e:   # (the file is as it was: the merge is one transaction)
        part.unlink(missing_ok=True)
        report.problem = f"{type(e).__name__}: {e}"
        return report
    report.ok, report.seconds, report.sizes = True, time.monotonic() - started, (size, target.stat().st_size)
    return report


def open_storage(target: Path, tz: tzinfo, files: dict[str, tuple[Path, ...]], parts=("station",)) -> Storage:
    """The bot's file at start, these parts of the old files' history moved into it first if they are not yet (logged). A move that fails
    leaves the file as it was, or none (the bot then starts it anew with what its sources still have), and is tried again next time."""
    report = migrate(Path(target).parent, tz, target, parts, files)
    for line in [] if report.skipped else report.lines():
        (log.info if report.ok else log.warning)("%s", line)
    return Storage(target, tz)


def _merge(part: Path, target: Path, tz: tzinfo):
    """A checked move (`part`) into the file the bot has been writing: its sensors found or added, the file's own readings kept and the old
    ones filling in what they lack, each band the wider of the two, the coverage of both, and the days of what came in worked out again."""
    storage = Storage(target, tz)
    db = storage.write_db
    try:
        db.execute("ATTACH DATABASE ? AS part", (str(part),))
        with storage.writer.transaction() as w:
            new = {sid: w.sensor(kind, role, label, device, external) for sid, kind, role, label, device, external
                   in db.execute("SELECT id, kind, role, label, device, external FROM part.sensors").fetchall()}
            db.execute("CREATE TEMP TABLE IF NOT EXISTS moved_ids (old INTEGER PRIMARY KEY, new INTEGER NOT NULL)")
            db.execute("DELETE FROM temp.moved_ids")
            db.executemany("INSERT INTO temp.moved_ids VALUES (?, ?)", [(sid, sensor.id) for sid, sensor in new.items()])
            for kind in KINDS:
                names = [c.name for c in KINDS[kind]]
                cells = [f"{c}{part}" for c in names for part in ("", "_lo", "_hi")]
                # (the file's own rows win, by the writer's rule: a final reading settles a provisional one, a row with only a band takes
                # the reading; a row is final once a final reading of it is in)
                sets = ["src = CASE WHEN excluded.final > final THEN excluded.src ELSE src END", "final = MAX(final, excluded.final)"]
                for c in names:
                    v, lo, hi = q(c), q(c + "_lo"), q(c + "_hi")
                    value = f"(CASE WHEN excluded.final > final THEN COALESCE(excluded.{v}, {v}) ELSE COALESCE({v}, excluded.{v}) END)"
                    sets += [f"{v} = {value}",
                             f"{lo} = MIN(COALESCE({lo}, excluded.{lo}), COALESCE(excluded.{lo}, {lo}), COALESCE({value}, {lo}, excluded.{lo}))",
                             f"{hi} = MAX(COALESCE({hi}, excluded.{hi}), COALESCE(excluded.{hi}, {hi}), COALESCE({value}, {hi}, excluded.{hi}))"]
                db.execute(f"INSERT INTO main.{kind} (sensor, res, ts, src, final, {', '.join(map(q, cells))}) "
                           f"SELECT i.new, p.res, p.ts, p.src, p.final, {', '.join('p.' + q(c) for c in cells)} FROM part.{kind} p "
                           f"JOIN temp.moved_ids i ON i.old = p.sensor WHERE true ON CONFLICT (sensor, res, ts) DO UPDATE SET {', '.join(sets)}")
                for sid, res, first, last in db.execute(f"SELECT sensor, res, MIN(ts), MAX(ts) FROM part.{kind} GROUP BY sensor, res").fetchall():
                    w.touch(new[sid], res, first, last)
                    if res in ("5min", "30min"):
                        w.roll_up(new[sid], first, last)
            for table in ("fields", "extra"):
                columns = [row[1] for row in db.execute(f"PRAGMA part.table_info({table})")][1:]
                db.execute(f"INSERT OR IGNORE INTO main.{table} SELECT i.new, {', '.join('p.' + q(c) for c in columns)} FROM part.{table} p "
                           "JOIN temp.moved_ids i ON i.old = p.sensor")
            for sid, res, start, end in db.execute('SELECT sensor, res, "start", "end" FROM part.coverage').fetchall():
                w.cover(new[sid], res, start, end)
            if not db.execute("SELECT 1 FROM main.snapshots LIMIT 1").fetchone():
                db.execute("INSERT INTO main.snapshots (kind, first_seen, last_seen, payload) SELECT kind, first_seen, last_seen, payload "
                           "FROM part.snapshots ORDER BY id")
            db.execute("INSERT OR IGNORE INTO main.snapshot_meta SELECT key, value FROM part.snapshot_meta")
            db.execute("INSERT OR REPLACE INTO main.meta SELECT key, value FROM part.meta WHERE key LIKE 'migrated.%'")
            db.execute("INSERT OR IGNORE INTO main.meta SELECT key, value FROM part.meta WHERE key NOT IN ('tz', 'schema')")
        db.execute("DETACH DATABASE part")
    finally:
        storage.close()


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


__all__ = ["OLD", "PARTS", "Report", "migrate", "moved", "old_files", "open_storage"]
