"""AirGradient readings kept on disk, so history is fetched once and survives restarts.

Only the six metrics the bot uses are stored, per reading, which keeps a year of data small.
Readings the bot takes itself from the sensor (local.py) are kept the same way, and noted in `recorded`, so a day that has some is completed by
adding only what is missing, never by replacing it. A day is recorded once it is over (and its data has had an hour to arrive), even if empty,
so days before the sensor existed aren't asked for again.
"""

import sqlite3
import threading
from datetime import date, tzinfo

from ..timeutil import day_bounds
from .metrics import STORED

COLUMNS = list(STORED)


class AirStore:
    def __init__(self, path, loc: str, tz: tzinfo):
        self.loc, self.tz = str(loc), tz
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(f"""
            CREATE TABLE IF NOT EXISTS readings (
                loc TEXT, ts INTEGER, {", ".join(f"{c} REAL" for c in COLUMNS)},
                PRIMARY KEY (loc, ts)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS days (loc TEXT, day TEXT, n INTEGER, PRIMARY KEY (loc, day)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS recorded (loc TEXT, ts INTEGER, PRIMARY KEY (loc, ts)) WITHOUT ROWID;
        """)
        have = {row[1] for row in self.db.execute("PRAGMA table_info(readings)")}
        if added := [c for c in COLUMNS if c not in have]:   # a file from before these columns: they are added, and every day is fetched
            for c in added:                                  # once more (the backfill) so that the earlier readings get them too
                self.db.execute(f"ALTER TABLE readings ADD COLUMN {c} REAL")
            self.db.execute("DELETE FROM days")
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    def counts(self, days: list[date]) -> dict[date, int | None]:
        """Readings stored for each of these finished days, in one query: None for a day that hasn't been fetched."""
        if not days:
            return {}
        with self._lock:
            found = dict(self.db.execute("SELECT day, n FROM days WHERE loc=? AND day BETWEEN ? AND ?",
                                         (self.loc, min(days).isoformat(), max(days).isoformat())).fetchall())
        return {d: found.get(d.isoformat()) for d in days}

    def save_day(self, day: date, rows: list[dict]) -> int:
        """Store one day's readings, in place of any it had; returns how many it now holds."""
        lo, hi = day_bounds(day, self.tz)
        rows = [r for r in rows if lo <= r["ts"] < hi]
        with self._lock:
            self.db.execute("DELETE FROM readings WHERE loc=? AND ts>=? AND ts<?", (self.loc, lo, hi))
            self.db.executemany(
                f"INSERT OR REPLACE INTO readings VALUES (?, ?, {', '.join('?' * len(COLUMNS))})",
                [(self.loc, r["ts"], *(r.get(c) for c in COLUMNS)) for r in rows])
            self.db.execute("INSERT OR REPLACE INTO days VALUES (?, ?, ?)", (self.loc, day.isoformat(), len(rows)))
            self.db.commit()
        return len(rows)

    def add_recorded(self, row: dict) -> None:
        """Store a reading the bot took itself from the sensor (a 5-minute average), in place of any the bucket had, and note that it did."""
        with self._lock:
            self.db.execute(f"INSERT OR REPLACE INTO readings VALUES (?, ?, {', '.join('?' * len(COLUMNS))})",
                            (self.loc, row["ts"], *(row.get(c) for c in COLUMNS)))
            self.db.execute("INSERT OR REPLACE INTO recorded VALUES (?, ?)", (self.loc, row["ts"]))
            self.db.commit()

    def merge(self, rows: list[dict]) -> int:
        """Store these readings where the store has none at that time (what it has already, recorded or fetched, is kept). Returns how many were new."""
        with self._lock:
            before = self.db.total_changes
            self.db.executemany(f"INSERT OR IGNORE INTO readings VALUES (?, ?, {', '.join('?' * len(COLUMNS))})",
                                [(self.loc, r["ts"], *(r.get(c) for c in COLUMNS)) for r in rows])
            self.db.commit()
            return self.db.total_changes - before

    def forget_days(self) -> None:
        """Note no day as fetched, so the next backfill fetches every finished day again (the readings stay until a day is replaced)."""
        with self._lock:
            self.db.execute("DELETE FROM days WHERE loc=?", (self.loc,))
            self.db.commit()

    def lacking(self) -> dict[str, int]:
        """How many stored readings have no value for each column, and \"all\": how many readings are stored."""
        with self._lock:
            total, *nulls = self.db.execute(
                f"SELECT COUNT(*), {', '.join(f'COUNT(*) - COUNT({c})' for c in COLUMNS)} FROM readings WHERE loc=?", (self.loc,)).fetchone()
        return {"all": total, **dict(zip(COLUMNS, nulls))}

    def has_recorded(self, day: date) -> bool:
        """Did the bot record any of this day's readings itself? (Such a day is completed by merging, never replaced.)"""
        lo, hi = day_bounds(day, self.tz)
        with self._lock:
            return self.db.execute("SELECT 1 FROM recorded WHERE loc=? AND ts>=? AND ts<? LIMIT 1", (self.loc, lo, hi)).fetchone() is not None

    def mark_day(self, day: date, n: int) -> None:
        """Note that this finished day is complete, with n readings."""
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO days VALUES (?, ?, ?)", (self.loc, day.isoformat(), n))
            self.db.commit()

    def load(self, start_ts: int, end_ts: int) -> list[dict]:
        """Readings with start_ts <= ts < end_ts, oldest first."""
        with self._lock:
            found = self.db.execute(
                f"SELECT ts, {', '.join(COLUMNS)} FROM readings WHERE loc=? AND ts>=? AND ts<? ORDER BY ts",
                (self.loc, start_ts, end_ts)).fetchall()
        return [{"ts": ts, **{c: v for c, v in zip(COLUMNS, vals) if v is not None}} for ts, *vals in found]

    def load_day(self, day: date) -> list[dict]:
        return self.load(*day_bounds(day, self.tz))
