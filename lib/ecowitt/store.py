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
from bisect import bisect_left

from ..timeutil import SLOT
from .api import UNIT_FIXES

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

type Interval = tuple[int, int]


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
        self._convert_units()
        self.db.commit()

    def _convert_units(self):
        """Readings stored before a unit fix (api.UNIT_FIXES) are converted once, in place (remembered in meta, so later opens
        do not look again)."""
        done = repr(sorted(UNIT_FIXES.items()))
        if (row := self.db.execute("SELECT value FROM meta WHERE key = 'unit_fixes'").fetchone()) and row[0] == done:
            return
        for (prefix, old), (unit, factor) in UNIT_FIXES.items():
            for key in self.db.execute("SELECT mac, cycle, grp, field FROM fields WHERE unit = ? AND (field = ? OR substr(field, 1, ?) = ?)",
                                       (old, prefix, len(prefix) + 1, prefix + "_")).fetchall():
                self.db.execute("UPDATE points SET value = printf('%.3f', CAST(value AS REAL) * ?) "
                                "WHERE mac=? AND cycle=? AND grp=? AND field=?", (factor, *key))
                self.db.execute("UPDATE fields SET unit = ? WHERE mac=? AND cycle=? AND grp=? AND field=?", (unit, *key))
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('unit_fixes', ?)", (done,))

    def close(self):
        with self._lock:
            self.db.close()

    def _query(self, sql: str, *args) -> list:
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def coverage(self, mac: str, cycle: str, grp: str) -> list[Interval]:
        """Every time range held for this group at this resolution (merged, oldest first)."""
        return merge(self._query("SELECT start, end FROM coverage WHERE mac=? AND cycle=? AND grp=?", mac, cycle, grp))

    def missing(self, mac: str, cycle: str, grp: str, start: int, end: int) -> list[Interval]:
        return subtract((start, end), self.coverage(mac, cycle, grp))

    def load_fields(self, mac: str, cycle: str, grp: str, fields: list[str] | None, start: int, end: int) -> dict:
        """A group's fields (all of them if `fields` is None) in Ecowitt's response shape: {field: {unit, list}}."""
        pick = f" AND field IN ({','.join('?' * len(fields))})" if fields is not None else ""
        args = (mac, cycle, grp, *(fields or ()))
        units = dict(self._query(f"SELECT field, unit FROM fields WHERE mac=? AND cycle=? AND grp=?{pick}", *args))
        out: dict = {}
        for field, ts, value in self._query(f"SELECT field, ts, value FROM points WHERE mac=? AND cycle=? AND grp=?{pick} "
                                            "AND ts BETWEEN ? AND ? ORDER BY ts", *args, start, end):
            out.setdefault(field, {"unit": units.get(field, ""), "list": {}})["list"][str(ts)] = value
        return out

    def slots(self, mac: str, cycle: str, grp: str, fields: list[str], start: int, end: int) -> list[dict[int, float]]:
        """Each field as {epoch: float}, in the order asked (empty for a field the cache lacks)."""
        got = self.load_fields(mac, cycle, grp, fields, start, end)
        return [{int(t): float(v) for t, v in got.get(f, {"list": {}})["list"].items()} for f in fields]

    def slot_ranges(self, mac: str, grp: str, field: str, values: dict[int, float], start: int, end: int) -> tuple[dict, dict]:
        """(lows, highs) for 30-minute readings that have no range of their own (dew point, feels-like, VPD, solar, UV,
        pressure ...): the lowest and highest of the cached 5-minute readings inside each slot (and the slot's own value).
        Only what the cache holds; slots with fewer than two 5-minute readings get none."""
        rows = self._query("SELECT ts, CAST(value AS REAL) FROM points WHERE mac=? AND cycle='5min' AND grp=? AND field=? "
                           "AND ts BETWEEN ? AND ? ORDER BY ts", mac, grp, field, start, end)
        times, fine = [ts for ts, _ in rows], [v for _, v in rows]
        lows, highs = {}, {}
        for t, v in values.items():
            inside = fine[bisect_left(times, t):bisect_left(times, t + SLOT)]
            if len(inside) >= 2:
                lows[t], highs[t] = min(inside + [v]), max(inside + [v])
        return lows, highs

    def days_held(self, mac: str, cycle: str, groups: list[str]) -> int:
        """Days of history stored at this resolution (the least any of the groups has)."""
        return min((sum(e - s + 1 for s, e in self.coverage(mac, cycle, grp)) // 86400 for grp in groups), default=0)

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
            grown = set()   # groups that now carry a reading the cache has never had (Ecowitt added a metric)
            for _, _, grp, field, _ in units:
                known = {r[0] for r in self.db.execute("SELECT field FROM fields WHERE mac=? AND cycle=? AND grp=?", (mac, cycle, grp))}
                if known and field not in known:
                    grown.add(grp)
            if grown:
                log.info("Ecowitt added a metric to %s (%s): its history is fetched again to fill it in", ", ".join(sorted(grown)), cycle)
            self.db.executemany("INSERT OR REPLACE INTO points VALUES (?,?,?,?,?,?)", rows)
            self.db.executemany("INSERT OR REPLACE INTO fields VALUES (?,?,?,?,?)", units)
            for grp, cov_end in covered:
                existing = [] if grp in grown else self.db.execute(   # the days held so far lack it: they are asked for again
                    "SELECT start, end FROM coverage WHERE mac=? AND cycle=? AND grp=?", (mac, cycle, grp)).fetchall()
                self.db.execute("DELETE FROM coverage WHERE mac=? AND cycle=? AND grp=?", (mac, cycle, grp))
                self.db.executemany("INSERT INTO coverage VALUES (?,?,?,?,?)",
                                    [(mac, cycle, grp, s, e) for s, e in merge(existing + [(start, cov_end)])])
            self.db.commit()

    def load(self, mac: str, cycle: str, groups: list[str], start: int, end: int) -> dict:
        """Cached readings in Ecowitt's response shape: {grp: {field: {unit, list}}}."""
        got = {grp: self.load_fields(mac, cycle, grp, None, start, end) for grp in groups}
        return {grp: fields for grp, fields in got.items() if fields}


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
