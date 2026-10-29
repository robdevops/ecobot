"""Writing to the file, on the writer's thread, a transaction at a time: readings, their bands, what was fetched, and the day rows of the
days a write touches (worked out in the same transaction, so they are never behind the readings).

A reading replaces the one held for the same sensor, resolution and time when it is as final as that one (a provisional reading, newer than
its cycle's settle horizon, never replaces a final one) and from a source ranked as high (the bot's own over Home Assistant's over the
cloud's); otherwise it only fills in what the row lacks. A band is only ever widened: the lowest and highest of what was held and what is
written, with the value always inside it.
"""

import logging
import math
import time
from contextlib import contextmanager
from datetime import date

from ..counts import uncounted
from . import Sensor
from .catalogue import AIR, AIR_EXTERNAL, BY_NAME, CLOUD, HOME_ASSISTANT, KINDS, SLOT, column_of, kind_of, label_of
from .coverage import merge, subtract
from .days import ANY, days_of, work_out
from .schema import q

log = logging.getLogger(__name__)

GAP_FINAL_SECONDS = 2 * 86400   # readings missing for longer than this are missing for good (the station was off): their time counts as fetched
MINUTES_KEPT = 10 * 86400       # Home Assistant's minutes are kept this long (only a chart of a few hours draws them; docs/storage-plan.md 3.9)
DAY_RESOLUTIONS = ("5min", "30min", "4hour", "1day")   # what the day rows are worked out from
DAY_KINDS = tuple(k for k in KINDS if k != "air")      # the AirGradient's days need its spike filter first (docs/storage-plan.md, step 3)
MIN_READINGS = 2                # a 5 minutes of live readings with fewer than this has no band (as live.MIN_READINGS)


def cycle_of(name: str):
    """One of Ecowitt's cycles (api.CYCLES), imported here and not at the top: the station's package opens the storage."""
    from ..ecowitt.api import CYCLES
    return CYCLES[name]


def horizon(cycle: str, now: float) -> int:
    """The latest start of an Ecowitt reading of this cycle that is final (as ecowitt.store.horizon)."""
    c = cycle_of(cycle)
    return int(now - c.settle - c.seconds)


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def pivot(group: str, fields: dict, start: int, end: int) -> tuple[list[dict], list[tuple[str, int, str]], dict[str, str]]:
    """One Ecowitt group's fields ({field: {"unit", "list": {epoch: text}}}, the cloud's shape) between two epochs as (rows {"ts", column,
    column_lo, column_hi}, oldest first; [(field, ts, text)] for `extra`: a field the catalogue has no column for or a value that is not a
    number; {field: unit} of every field sent)."""
    rows: dict[int, dict] = {}
    extra, units = [], {}
    for field, obj in fields.items():
        if not isinstance(obj, dict) or not isinstance(obj.get("list"), dict):
            continue
        units[field] = str(obj.get("unit", ""))
        place = column_of(group, field)
        for ts, value in obj["list"].items():
            t = int(ts)
            if not start <= t <= end:
                continue
            number = _number(value)
            if place is None or number is None:
                extra.append((field, t, str(value)))
            else:
                column, part = place
                rows.setdefault(t, {"ts": t})[column if part == "value" else f"{column}_{part}"] = number
    return [rows[t] for t in sorted(rows)], extra, units


# ---------- the statements, generated once per kind ----------
_SQL: dict[tuple, str] = {}

REPLACE = "(excluded.final > final OR (excluded.final = final AND excluded.src >= src))"


def upsert_sql(kind: str) -> str:
    """A reading written (every column of its kind, null for what it lacks) by the rules above."""
    if (kind, "put") not in _SQL:
        names = [c.name for c in KINDS[kind]]
        cols = ["sensor", "res", "ts", "src", "final"] + [f"{c}{part}" for c in names for part in ("", "_lo", "_hi")]
        sets = [f"src = CASE WHEN {REPLACE} THEN excluded.src ELSE src END", f"final = CASE WHEN {REPLACE} THEN excluded.final ELSE final END"]
        for c in names:
            v, lo, hi = q(c), q(c + "_lo"), q(c + "_hi")
            new = f"(CASE WHEN {REPLACE} THEN COALESCE(excluded.{v}, {v}) ELSE COALESCE({v}, excluded.{v}) END)"
            sets += [f"{v} = {new}",
                     f"{lo} = MIN(COALESCE({lo}, excluded.{lo}), COALESCE(excluded.{lo}, {lo}), COALESCE({new}, {lo}, excluded.{lo}))",
                     f"{hi} = MAX(COALESCE({hi}, excluded.{hi}), COALESCE(excluded.{hi}, {hi}), COALESCE({new}, {hi}, excluded.{hi}))"]
        _SQL[kind, "put"] = (f"INSERT INTO {kind} ({', '.join(map(q, cols))}) VALUES ({', '.join('?' * len(cols))}) "
                             f"ON CONFLICT (sensor, res, ts) DO UPDATE SET {', '.join(sets)}")
    return _SQL[kind, "put"]


def widen_sql(kind: str, column: str) -> str:
    """One reading's band widened (a row with only the band if the reading has none yet: provisional, from the cloud's rank, so the
    reading fills it in when it comes)."""
    if (kind, "widen", column) not in _SQL:
        v, lo, hi = q(column), q(column + "_lo"), q(column + "_hi")
        _SQL[kind, "widen", column] = (
            f"INSERT INTO {kind} (sensor, res, ts, src, final, {lo}, {hi}) VALUES (?, ?, ?, {CLOUD}, 0, ?, ?) ON CONFLICT (sensor, res, ts) DO UPDATE "
            f"SET {lo} = MIN(COALESCE({lo}, excluded.{lo}), excluded.{lo}, COALESCE({v}, excluded.{lo})), "
            f"{hi} = MAX(COALESCE({hi}, excluded.{hi}), excluded.{hi}, COALESCE({v}, excluded.{hi}))")
    return _SQL[kind, "widen", column]


def rollup_sql(kind: str) -> str:
    """The bands of the 30-minute readings whose half hours start in [:start, :end], widened by the 5-minute readings inside them: by their
    bands, and by their values where a half hour has two or more."""
    if (kind, "rollup") not in _SQL:
        names = [c.name for c in KINDS[kind] if c.ranged]
        found = ", ".join(f"COUNT({q(c)}) AS {q(c + '~n')}, MIN({q(c)}) AS {q(c + '~min')}, MAX({q(c)}) AS {q(c + '~max')}, "
                          f"MIN({q(c + '_lo')}) AS {q(c + '~lo')}, MAX({q(c + '_hi')}) AS {q(c + '~hi')}" for c in names)
        sets = []
        for c in names:
            v, own_lo, own_hi = f"{kind}.{q(c)}", f"{kind}.{q(c + '_lo')}", f"{kind}.{q(c + '_hi')}"
            v_lo, v_hi = f"(CASE WHEN f.{q(c + '~n')} >= 2 THEN f.{q(c + '~min')} END)", f"(CASE WHEN f.{q(c + '~n')} >= 2 THEN f.{q(c + '~max')} END)"
            b_lo, b_hi = f"f.{q(c + '~lo')}", f"f.{q(c + '~hi')}"
            lo = f"MIN(COALESCE({own_lo}, {v_lo}, {b_lo}), COALESCE({v_lo}, {own_lo}, {b_lo}), COALESCE({b_lo}, {own_lo}, {v_lo}))"
            hi = f"MAX(COALESCE({own_hi}, {v_hi}, {b_hi}), COALESCE({v_hi}, {own_hi}, {b_hi}), COALESCE({b_hi}, {own_hi}, {v_hi}))"
            sets += [f"{q(c + '_lo')} = MIN({lo}, COALESCE({v}, {lo}))", f"{q(c + '_hi')} = MAX({hi}, COALESCE({v}, {hi}))"]
        _SQL[kind, "rollup"] = (
            f"WITH f AS (SELECT ts / {SLOT} * {SLOT} AS slot, {found} FROM {kind} WHERE sensor = :sensor AND res = '5min' "
            f"AND ts BETWEEN :start AND :end + {SLOT - 1} GROUP BY slot) "
            f"UPDATE {kind} SET {', '.join(sets)} FROM f WHERE {kind}.sensor = :sensor AND {kind}.res = '30min' AND {kind}.ts = f.slot")
    return _SQL[kind, "rollup"]


class Writer:
    def __init__(self, storage):
        self.storage = storage
        self.db = storage.write_db
        self.tz = storage.tz
        self._depth = 0
        self._coverage: dict[tuple[int, str], list] = {}   # this transaction's coverage, by (sensor, res)
        self._sensors: list[Sensor] = []                    # ... its new sensors
        self._days: dict[str, dict[int, dict[date, set[str]]]] = {}   # ... and the days to work out again, by kind and sensor, with what
                                                                     # changed: the resolutions written, ANY for what is held

    # ---------- transactions ----------
    def run(self, fn, args):
        with self.transaction():
            return fn(self, *args)

    @contextmanager
    def transaction(self):
        """One transaction (a transaction inside one is part of it): the day rows of the days it touched are worked out before it is
        committed, and its coverage and sensors are seen by the readers once it is."""
        if self._depth:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        uncounted(self.db, "BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self
            for kind, wanted in self._days.items():
                work_out(self.db, kind, wanted, self.held, self.tz)
            uncounted(self.db, "COMMIT")
        except BaseException:
            uncounted(self.db, "ROLLBACK")
            raise
        finally:
            self._depth = 0
            coverage, sensors = self._coverage, self._sensors
            self._coverage, self._sensors, self._days = {}, [], {}
        self.storage.coverage.publish(coverage)
        self.storage.sensors.publish(sensors)

    # ---------- sensors and what they send ----------
    def sensor(self, kind: str, role: str, label: str, device: str, external: str) -> Sensor:
        """The sensor, added if new."""
        found = self.storage.sensors.find(device, external) or next(
            (s for s in self._sensors if (s.device, s.external) == (device, external)), None)
        if found:
            return found
        cur = self.db.execute("INSERT INTO sensors (kind, role, label, device, external) VALUES (?, ?, ?, ?, ?)",
                              (kind, role, label, device, external))
        new = Sensor(cur.lastrowid, kind, role, label, device, external)
        self._sensors.append(new)
        return new

    def ecowitt_sensor(self, mac: str, group: str) -> Sensor:
        return self.sensor(kind_of(group), group, label_of(group), mac, group)

    def air_sensor(self, location: str) -> Sensor:
        return self.sensor(*AIR, str(location), AIR_EXTERNAL)

    def all_sensors(self) -> list[Sensor]:
        """Every sensor, this transaction's new ones too."""
        return [*self.storage.sensors.all(), *self._sensors]

    def known(self, sensor: Sensor, res: str) -> set[str]:
        """The fields this sensor has sent at this resolution (as the source names them: "temperature_low")."""
        return {name for (name,) in self.db.execute("SELECT name FROM fields WHERE sensor = ? AND res = ?", (sensor.id, res))}

    def fields(self, sensor: Sensor, res: str, units: dict[str, str]):
        """Note the fields a response had and their units (a unit follows what the source says now)."""
        self.db.executemany("INSERT INTO fields VALUES (?, ?, ?, ?) ON CONFLICT (sensor, res, name) DO UPDATE SET unit = excluded.unit",
                            [(sensor.id, res, name, unit) for name, unit in units.items()])

    def extra(self, sensor: Sensor, res: str, rows: list[tuple[str, int, str]]):
        self.db.executemany("INSERT OR REPLACE INTO extra VALUES (?, ?, ?, ?, ?)", [(sensor.id, res, f, ts, v) for f, ts, v in rows])

    def set_meta(self, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    # ---------- readings ----------
    def put(self, sensor: Sensor, res: str, rows: list[dict], src: int = CLOUD, final: bool = True, exact: bool = False) -> int:
        """Write a sensor's readings at one resolution: rows of {"ts": epoch, column: value, column_lo, column_hi, and "final" where it
        differs from `final`}. exact (the migration): rows the file does not have yet, as they are (a row it has already is an error), the
        30-minute bands not yet widened from the 5-minute readings (roll_up does that once they are all in). Returns how many."""
        if not rows:
            return 0
        if exact:   # rows new to the file: only the cells they have
            keys = sorted({k for row in rows for k in row} - {"ts", "final"})
            self.db.executemany(f"INSERT INTO {sensor.kind} (sensor, res, ts, src, final, {', '.join(map(q, keys))}) "
                                f"VALUES ({', '.join('?' * (5 + len(keys)))})",
                                [(sensor.id, res, row["ts"], src, int(row.get("final", final)), *map(row.get, keys)) for row in rows])
        else:
            names = [c.name for c in KINDS[sensor.kind]]
            args = []
            for row in rows:
                values = []
                for c in names:
                    v, lo, hi = row.get(c), row.get(c + "_lo"), row.get(c + "_hi")
                    if v is not None:   # the value inside its band
                        lo, hi = (None if lo is None else min(lo, v)), (None if hi is None else max(hi, v))
                    values += (v, lo, hi)
                args.append((sensor.id, res, row["ts"], src, int(row.get("final", final)), *values))
            self.db.executemany(upsert_sql(sensor.kind), args)
        first, last = min(row["ts"] for row in rows), max(row["ts"] for row in rows)
        self.touch(sensor, res, first, last)
        if res in ("5min", "30min") and not exact:
            self.roll_up(sensor, first, last)
        return len(rows)

    def widen(self, sensor: Sensor, res: str, bands: dict[str, dict[int, tuple[float, float]]]) -> int:
        """Widen these readings' bands ({column: {ts: (lowest, highest)}}: what the bot's live readings and Home Assistant's give for each 5
        minutes). Returns how many."""
        times = []
        for column, found in bands.items():
            self.db.executemany(widen_sql(sensor.kind, column), [(sensor.id, res, ts, lo, hi) for ts, (lo, hi) in found.items()])
            times += found
        if times:
            self.touch(sensor, res, min(times), max(times))
            if res == "5min":
                self.roll_up(sensor, min(times), max(times))
        return len(times)

    def roll_up(self, sensor: Sensor, start: int, end: int):
        """Widen the bands of the 30-minute readings from start to end by the 5-minute readings inside them (what the old cache's
        slot_ranges and the live ranges' half hours gave at read time)."""
        if any(c.ranged for c in KINDS[sensor.kind]):
            self.db.execute(rollup_sql(sensor.kind), {"sensor": sensor.id, "start": start - start % SLOT, "end": end})

    def prune_minutes(self, before: int) -> int:
        """Drop the minutes older than `before` (epoch): a run of rows at the front of each sensor's minutes. Returns how many."""
        gone = 0
        for s in self.all_sensors():
            if s.kind in KINDS:
                gone += self.db.execute(f"DELETE FROM {s.kind} WHERE sensor = ? AND res = '1min' AND ts < ?", (s.id, before)).rowcount
        return gone

    # ---------- coverage ----------
    def coverage_of(self, sensor: int, res: str) -> list:
        return self._coverage.get((sensor, res), self.storage.coverage.of(sensor, res))

    def held(self, sensor: int, res: str, start: int, end: int) -> bool:
        """Is [start, end] held, as this transaction leaves it."""
        return not subtract((start, end), self.coverage_of(sensor, res))

    def cover(self, sensor: Sensor, res: str, start: int, end: int):
        """[start, end] (epochs) was fetched at this resolution, readings or none."""
        self._set_coverage(sensor, res, merge([*self.coverage_of(sensor.id, res), (start, end)]))
        self.touch(sensor, res, start, end)   # (a day held whole at this resolution now takes its figures from it)

    def uncover(self, sensor: Sensor, res: str):
        """Forget what was fetched at this resolution (it is all asked for again)."""
        for start, end in self.coverage_of(sensor.id, res):
            self.touch(sensor, ANY, start, end)   # (a day's figures may come from another resolution now)
        self._set_coverage(sensor, res, [])

    def _set_coverage(self, sensor: Sensor, res: str, spans: list):
        self.db.execute("DELETE FROM coverage WHERE sensor = ? AND res = ?", (sensor.id, res))
        self.db.executemany("INSERT INTO coverage VALUES (?, ?, ?, ?)", [(sensor.id, res, s, e) for s, e in spans])
        self._coverage[sensor.id, res] = spans

    # ---------- days ----------
    def touch(self, sensor: Sensor, res: str, start: int, end: int):
        """Note that readings of [start, end] at this resolution changed (ANY: what is held there changed): their days are worked out again
        when the transaction is committed, those that can come out otherwise (days.work_out)."""
        if sensor.kind in DAY_KINDS and (res in DAY_RESOLUTIONS or res == ANY):
            days = self._days.setdefault(sensor.kind, {}).setdefault(sensor.id, {})
            for d in days_of(start, end, self.tz, utc=res in ("4hour", "1day", ANY)):
                days.setdefault(d, set()).add(res)

    def set_timezone(self, tz):
        """Work out every day in this zone from now on (and every day held, again)."""
        zone = str(getattr(tz, "key", tz))
        log.info("Storage: the time zone is now %s (was %s): working out every day again", zone, getattr(self.tz, "key", self.tz))
        self.tz = self.storage.tz = tz
        self.all_days()
        self.set_meta("tz", zone)

    def all_days(self):
        """Work out every day of every sensor again (a new time zone)."""
        for s in self.all_sensors():
            if s.kind in DAY_KINDS:
                first, last = self.db.execute(f"SELECT MIN(ts), MAX(ts) FROM {s.kind} WHERE sensor = ?", (s.id,)).fetchone()
                if first is not None:
                    self.touch(s, ANY, first, last)

    # ---------- the sources' own shapes ----------
    def put_ecowitt(self, mac: str, cycle: str, groups: list[str], data: dict, start: int, end: int, now: float | None = None,
                    src: int = CLOUD):
        """One of Ecowitt's history responses (or Home Assistant's, told as Ecowitt's) for [start, end] at this cycle, by the rules of
        HistoryCache.store: what is newer than the cycle's settle horizon is kept as provisional (replaced by the next fetch of it), and what
        was fetched is marked only up to the last final reading Ecowitt returned, so a stretch it is late with is asked for again; one
        entirely older than two days is marked in full (what is missing there is missing). A group with no readings while others have some is
        a sensor the station lacks, marked as far as the others. A group that sends a field it never sent at this cycle has all of it asked
        for again, to fill the field in."""
        now = time.time() if now is None else now
        limit = min(end, horizon(cycle, now))
        settled: dict[str, int | None] = {}
        sensors: dict[str, Sensor] = {}
        grown = []
        for group in groups:
            sensors[group] = sensor = self.ecowitt_sensor(mac, group)
            rows, extra, units = pivot(group, data.get(group) or {}, start, end)
            known = self.known(sensor, cycle)
            if known and set(units) - known:
                grown.append(group)
                self.uncover(sensor, cycle)
            self.fields(sensor, cycle, units)
            for row in rows:
                row["final"] = int(row["ts"] <= limit)
            self.put(sensor, cycle, rows, src)
            self.extra(sensor, cycle, extra)
            settled[group] = max((r["ts"] for r in rows if r["ts"] <= limit), default=None)
        if grown:
            log.info("Ecowitt added a metric to %s (%s): its history is fetched again to fill it in", ", ".join(sorted(grown)), cycle)
        if start > limit:
            return
        newest = max((t for t in settled.values() if t is not None), default=None)
        old_enough = limit < now - GAP_FINAL_SECONDS
        for group, last in settled.items():
            reached = last if last is not None else newest
            if old_enough:
                self.cover(sensors[group], cycle, start, limit)
            elif reached is not None:
                self.cover(sensors[group], cycle, start, min(limit, reached + cycle_of(cycle).seconds - 1))

    def put_minutes(self, mac: str, data: dict, now: float | None = None, kept: int = MINUTES_KEPT) -> int:
        """Home Assistant's minutes ({group: {field: {"unit", "list"}}}); those older than `kept` seconds are dropped. Returns how many."""
        now = time.time() if now is None else now
        written = 0
        for group, fields in data.items():
            sensor = self.ecowitt_sensor(mac, group)
            rows, extra, units = pivot(group, fields, int(now - kept), int(now) + 3600)
            self.fields(sensor, "1min", units)
            written += self.put(sensor, "1min", rows, HOME_ASSISTANT)
            self.extra(sensor, "1min", extra)
        self.prune_minutes(int(now - kept))
        return written

    def put_ranges(self, mac: str, found: dict[str, dict[int, tuple[int, float, float]]]) -> int:
        """The lowest and highest of each 5 minutes of the station's readings ({"group.field": {5-minute mark: (readings, lowest,
        highest)}}: the bot's live readings, Home Assistant's): a mark with fewer than MIN_READINGS has none. Returns how many."""
        widened = 0
        for series, marks in found.items():
            group, field = series.split(".", 1)
            if (place := column_of(group, field)) is None or place[1] != "value" or not BY_NAME[kind_of(group)][place[0]].ranged:
                continue   # (a compass point and the rain counters take no band)
            bands = {ts: (lo, hi) for ts, (n, lo, hi) in marks.items() if n >= MIN_READINGS}
            widened += self.widen(self.ecowitt_sensor(mac, group), "5min", {place[0]: bands})
        return widened


__all__ = ["DAY_KINDS", "GAP_FINAL_SECONDS", "MINUTES_KEPT", "Writer", "horizon", "pivot"]
