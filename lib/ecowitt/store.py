"""Where Ecowitt readings are kept between questions.

HistoryCache (SQLite, survives restarts) holds settled readings per
(mac, cycle, group, field, timestamp), plus which time ranges have been fetched per
(mac, cycle, group). Coverage is recorded even when Ecowitt returns nothing (e.g. before
the station existed), so those ranges aren't asked for again. The newest readings may
still change (late uploads), so only buckets older than a per-cycle horizon are stored.

HotStore (memory) holds the newest response per (mac, cycle, group) for a few minutes, so
that unsettled tail is fetched by the keep-warm refresh rather than by each question.
"""

import asyncio
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

HOT_TTL_SECONDS = 300   # recent readings are reused for this long
HOT_SLACK_SECONDS = 60  # a response ending within this of its fetch time "reaches the present"

Interval = tuple[int, int]


def horizon(cycle: str, now: float | None = None) -> int:
    """Latest bucket start whose data is final."""
    now = time.time() if now is None else now
    return int(now - SETTLE_SECONDS[cycle] - BUCKET_SECONDS[cycle])


def subtract(span: Interval, covered: list[Interval]) -> list[Interval]:
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


def merge(intervals: list[Interval]) -> list[Interval]:
    out: list[list[int]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


class HistoryCache:
    def __init__(self, path, units: dict):
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

    def close(self):
        with self._lock:
            self.db.close()

    def missing(self, mac: str, cycle: str, grp: str, start: int, end: int) -> list[Interval]:
        with self._lock:
            rows = self.db.execute(
                "SELECT start, end FROM coverage WHERE mac=? AND cycle=? AND grp=? AND end>=? AND start<=?",
                (mac, cycle, grp, start, end)).fetchall()
        return subtract((start, end), rows)

    def coverage(self, mac: str, cycle: str, grp: str) -> list[Interval]:
        """Every time range held for this group at this resolution (merged, oldest first)."""
        with self._lock:
            rows = self.db.execute("SELECT start, end FROM coverage WHERE mac=? AND cycle=? AND grp=?",
                                   (mac, cycle, grp)).fetchall()
        return merge(rows)

    def load_fields(self, mac: str, cycle: str, grp: str, fields: list[str], start: int, end: int) -> dict:
        """Just some fields of a group, in Ecowitt's response shape: {field: {unit, list}}."""
        out: dict = {}
        marks = ",".join("?" * len(fields))
        with self._lock:
            units = dict(self.db.execute(
                f"SELECT field, unit FROM fields WHERE mac=? AND cycle=? AND grp=? AND field IN ({marks})",
                (mac, cycle, grp, *fields)).fetchall())
            for field, ts, value in self.db.execute(
                    f"SELECT field, ts, value FROM points WHERE mac=? AND cycle=? AND grp=? AND field IN ({marks}) "
                    "AND ts BETWEEN ? AND ?", (mac, cycle, grp, *fields, start, end)):
                out.setdefault(field, {"unit": units.get(field, ""), "list": {}})["list"][str(ts)] = value
        return out

    def days_held(self, mac: str, cycle: str, groups: list[str]) -> int:
        """Days of history stored at this resolution (the least any of the groups has)."""
        held = []
        with self._lock:
            for grp in groups:
                rows = self.db.execute("SELECT start, end FROM coverage WHERE mac=? AND cycle=? AND grp=?",
                                       (mac, cycle, grp)).fetchall()
                held.append(sum(e - s + 1 for s, e in merge(rows)) // 86400)
        return min(held, default=0)

    def store(self, mac: str, cycle: str, groups: list[str], data: dict, start: int, end: int):
        """Save the final readings of one response and mark what was fetched, per group.

        Readings are only stored up to the settle horizon. The fetched range is marked only
        up to the last reading Ecowitt actually returned, so if Ecowitt is running late the
        missing stretch is asked for again next time. A range entirely older than two days is
        marked in full, since missing data there is really missing. A group with no readings
        while others have some is a sensor the station doesn't have, and is marked as far as
        the others."""
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
        newest = max((t for t in last_by_group.values() if t is not None), default=None)
        covered = []
        for grp, last in last_by_group.items():
            reached = last if last is not None else newest
            if old_enough:
                covered.append((grp, limit))
            elif reached is not None:
                covered.append((grp, min(limit, reached + BUCKET_SECONDS[cycle] - 1)))
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


class HotStore:
    """Fresh responses per (mac, cycle, group): the fields, the range they cover, and when
    they were fetched. Only the latest response per key is kept."""

    def __init__(self):
        self._entries: dict = {}
        self._locks: dict = {}

    def lock(self, mac: str, cycle: str) -> asyncio.Lock:
        """One fetch at a time per mac/cycle, so a question waits for an in-flight
        refresh instead of asking Ecowitt for the same thing."""
        return self._locks.setdefault((mac, cycle), asyncio.Lock())

    def get(self, mac: str, cycle: str, group: str, start: int, end: int) -> dict | None:
        """Group's fields if a fresh-enough response covers the whole of [start, end], else
        None. A response fetched up to the present covers anything up to now (readings newer
        than that are at most HOT_TTL_SECONDS stale, by design)."""
        entry = self._entries.get((mac, cycle, group))
        if not entry or time.time() - entry["fetched_at"] > HOT_TTL_SECONDS or entry["start"] > start:
            return None
        reaches_present = entry["end"] >= entry["fetched_at"] - HOT_SLACK_SECONDS
        return entry["fields"] if end <= entry["end"] or reaches_present else None

    def put(self, mac: str, cycle: str, group: str, start: int, end: int, fields: dict):
        self._entries[(mac, cycle, group)] = {"start": start, "end": end, "fields": fields,
                                              "fetched_at": time.time()}
