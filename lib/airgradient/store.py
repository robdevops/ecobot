"""AirGradient readings kept on disk, so history is fetched once and survives restarts.

Only the six metrics the bot uses are stored, per reading, which keeps a year of data small.
A day is recorded once it is over (and its data has had an hour to arrive), even if empty,
so days before the sensor existed aren't asked for again.
"""

import sqlite3
import threading
from datetime import date, datetime, timedelta, tzinfo

from .metrics import METRICS

COLUMNS = list(METRICS)


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
        """)
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    def _bounds(self, day: date) -> tuple[int, int]:
        start = datetime.combine(day, datetime.min.time()).replace(tzinfo=self.tz)
        return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())

    def day_count(self, day: date) -> int | None:
        """Readings stored for this finished day, or None if it hasn't been fetched."""
        with self._lock:
            row = self.db.execute("SELECT n FROM days WHERE loc=? AND day=?", (self.loc, day.isoformat())).fetchone()
        return row[0] if row else None

    def save_day(self, day: date, rows: list[dict]):
        lo, hi = self._bounds(day)
        rows = [r for r in rows if lo <= r["ts"] < hi]
        with self._lock:
            self.db.execute("DELETE FROM readings WHERE loc=? AND ts>=? AND ts<?", (self.loc, lo, hi))
            self.db.executemany(
                f"INSERT OR REPLACE INTO readings VALUES (?, ?, {', '.join('?' * len(COLUMNS))})",
                [(self.loc, r["ts"], *(r.get(c) for c in COLUMNS)) for r in rows])
            self.db.execute("INSERT OR REPLACE INTO days VALUES (?, ?, ?)", (self.loc, day.isoformat(), len(rows)))
            self.db.commit()

    def load(self, start_ts: int, end_ts: int) -> list[dict]:
        """Readings with start_ts <= ts < end_ts, oldest first."""
        with self._lock:
            found = self.db.execute(
                f"SELECT ts, {', '.join(COLUMNS)} FROM readings WHERE loc=? AND ts>=? AND ts<? ORDER BY ts",
                (self.loc, start_ts, end_ts)).fetchall()
        return [{"ts": ts, **{c: v for c, v in zip(COLUMNS, vals) if v is not None}} for ts, *vals in found]

    def load_day(self, day: date) -> list[dict]:
        return self.load(*self._bounds(day))
