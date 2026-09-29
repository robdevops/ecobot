"""On-disk cache of Ecowitt history, so past data is only fetched once.

Stores individual readings per (mac, cycle, group, field, timestamp) plus which time
ranges have been fetched per (mac, cycle, group). Coverage is recorded even when
Ecowitt returns nothing (e.g. before the station existed), so those ranges aren't
asked for again. Recent readings may still change (late uploads), so only buckets
older than a per-cycle horizon are stored; newer ones are always fetched fresh.
"""

import json
import logging
import sqlite3
import threading
import time

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
BUCKET_SECONDS = {"5min": 300, "30min": 1800, "4hour": 14400, "1day": 86400}
# How long after a bucket ends before its data is treated as final
SETTLE_SECONDS = {"5min": 3600, "30min": 7200, "4hour": 8 * 3600, "1day": 86400}
# Missing readings older than this are accepted as genuinely missing (station offline,
# before it existed). Newer gaps may just be Ecowitt running late, so they're re-asked.
GAP_FINAL_SECONDS = 2 * 86400


def horizon(cycle: str, now: float | None = None) -> int:
    """Latest bucket start whose data is final."""
    now = time.time() if now is None else now
    return int(now - SETTLE_SECONDS[cycle] - BUCKET_SECONDS[cycle])


def subtract(span: tuple[int, int], covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Parts of inclusive [start, end] not inside any covered interval."""
    start, end = span
    gaps, cursor = [], start
    for c_start, c_end in sorted(covered):
        if c_end < cursor:
            continue
        if c_start > end:
            break
        if c_start > cursor:
            gaps.append((cursor, c_start - 1))
        cursor = max(cursor, c_end + 1)
        if cursor > end:
            break
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


def merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [tuple(x) for x in out]


class HistoryCache:
    def __init__(self, path: str, units: dict):
        self.path = path
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS points (
                mac TEXT, cycle TEXT, grp TEXT, field TEXT, ts INTEGER, value TEXT,
                PRIMARY KEY (mac, cycle, grp, field, ts)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS fields (
                mac TEXT, cycle TEXT, grp TEXT, field TEXT, unit TEXT,
                PRIMARY KEY (mac, cycle, grp, field)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS coverage (mac TEXT, cycle TEXT, grp TEXT, start INTEGER, end INTEGER);
            CREATE INDEX IF NOT EXISTS coverage_key ON coverage (mac, cycle, grp);
        """)
        # Readings are stored in the units requested; a different setup starts fresh
        signature = json.dumps({"schema": SCHEMA_VERSION, "units": units}, sort_keys=True)
        row = self.db.execute("SELECT value FROM meta WHERE key = 'signature'").fetchone()
        if row and row[0] != signature:
            log.warning("History cache settings changed - clearing %s", path)
            self.db.executescript("DELETE FROM points; DELETE FROM fields; DELETE FROM coverage;")
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('signature', ?)", (signature,))
        self.db.commit()
        n = self.db.execute("SELECT COUNT(*) FROM points").fetchone()[0]
        log.info("History cache %s: %d readings stored", path, n)

    def close(self):
        with self._lock:
            self.db.close()

    def missing(self, mac: str, cycle: str, grp: str, start: int, end: int) -> list[tuple[int, int]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT start, end FROM coverage WHERE mac=? AND cycle=? AND grp=? AND end>=? AND start<=?",
                (mac, cycle, grp, start, end)).fetchall()
        return subtract((start, end), rows)

    def store(self, mac: str, cycle: str, groups: list[str], data: dict, start: int, end: int):
        """Save final readings from one response and mark what was fetched, per group.

        Readings are only stored up to the settle horizon. The fetched range is marked
        only up to the last reading Ecowitt actually returned, so if Ecowitt is running
        late the missing stretch is asked for again next time. A range entirely older
        than two days is marked in full, since missing data there is really missing.
        A group with no readings while others have some is a sensor the station doesn't
        have, and is marked as far as the others."""
        limit = min(end, horizon(cycle))
        if start > limit:
            return
        old_enough = limit < time.time() - GAP_FINAL_SECONDS
        rows, units, last_by_group = [], [], {}
        for grp in groups:
            last = None
            for field, obj in (data.get(grp) or {}).items():
                if not isinstance(obj, dict) or not isinstance(obj.get("list"), dict):
                    continue
                units.append((mac, cycle, grp, field, obj.get("unit", "")))
                for ts, v in obj["list"].items():
                    if start <= int(ts) <= limit:
                        rows.append((mac, cycle, grp, field, int(ts), str(v)))
                        last = int(ts) if last is None else max(last, int(ts))
            last_by_group[grp] = last
        # A late Ecowitt stops every group at the same point; a group that's empty while
        # others have data is a sensor the station doesn't have, so it's covered as far
        # as the others are. Nothing returned at all for a recent range: ask again next time.
        newest = max((t for t in last_by_group.values() if t is not None), default=None)
        covered = []
        for grp, last in last_by_group.items():
            if old_enough:
                covered.append((grp, limit))
            elif (last if last is not None else newest) is not None:
                covered.append((grp, min(limit, (last if last is not None else newest) + BUCKET_SECONDS[cycle] - 1)))
        with self._lock:
            self.db.executemany("INSERT OR REPLACE INTO points VALUES (?,?,?,?,?,?)", rows)
            self.db.executemany("INSERT OR REPLACE INTO fields VALUES (?,?,?,?,?)", units)
            for grp, cov_end in covered:
                existing = self.db.execute(
                    "SELECT start, end FROM coverage WHERE mac=? AND cycle=? AND grp=?", (mac, cycle, grp)).fetchall()
                self.db.execute("DELETE FROM coverage WHERE mac=? AND cycle=? AND grp=?", (mac, cycle, grp))
                self.db.executemany("INSERT INTO coverage VALUES (?,?,?,?,?)",
                                    [(mac, cycle, grp, s, e) for s, e in merge(existing + [(start, cov_end)])])
            self.db.commit()

    def load(self, mac: str, cycle: str, groups: list[str], start: int, end: int) -> dict:
        """Cached readings in Ecowitt's response shape: {grp: {field: {unit, list}}}."""
        out: dict = {}
        with self._lock:
            for grp in groups:
                units = dict(self.db.execute(
                    "SELECT field, unit FROM fields WHERE mac=? AND cycle=? AND grp=?", (mac, cycle, grp)).fetchall())
                for field, ts, value in self.db.execute(
                        "SELECT field, ts, value FROM points WHERE mac=? AND cycle=? AND grp=? AND ts BETWEEN ? AND ? "
                        "ORDER BY ts", (mac, cycle, grp, start, end)):
                    entry = out.setdefault(grp, {}).setdefault(field, {"unit": units.get(field, ""), "list": {}})
                    entry["list"][str(ts)] = value
        return out
