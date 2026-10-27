"""The station's lowest and highest in each 5-minute stretch, from the live readings the bot takes itself.

Ecowitt's own 5-minute history gives one number per reading and no range (its lows and highs come only with the 30-minute readings and
longer). Home Assistant gets a shaded range on its recent charts by keeping every reading the station reports and, every 5 minutes, boiling
them down to a mean, a lowest and a highest. This does the same: each live reading (about one a minute: Ecowitt's real_time, which
the keep-warm already fetches) is folded into the 5-minute stretch it falls in, and a chart of 5-minute readings is shaded between each
stretch's lowest and highest. A reading is counted in the stretch around the nearest 5-minute mark; a stretch with fewer than
MIN_READINGS has no range. Nothing here is ever asked of Ecowitt: it only has the readings the bot saw.
"""

import sqlite3
import threading
import time

from ..lines import RANGE_KEEP_DAYS
from ..series import derives_range

BUCKET = 300
MIN_READINGS = 2           # a stretch with fewer live readings than this has no range
PRUNE_EVERY = 3600         # seconds between clean-ups of what is older


def readings_in(data: dict) -> list[tuple[str, int | None, float]]:
    """(series "group.field", when it was read (epoch, None when the response does not say), value) for each reading of a real_time
    response that a chart can shade (series.derives_range; not the rain)."""
    found = []
    for group, fields in data.items():
        for field, obj in (fields.items() if isinstance(fields, dict) else ()):
            if group == "rainfall" or not (isinstance(obj, dict) and derives_range(field)):   # (rain is a running count: bars, not a line)
                continue
            try:
                value = float(obj["value"])
            except (KeyError, TypeError, ValueError):
                continue
            try:
                when = int(obj.get("time") or 0) or None
            except (TypeError, ValueError):
                when = None
            found.append((f"{group}.{field}", when, value))
    return found


class LiveRanges:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._seen: dict[str, int] = {}       # series -> the reading time last counted: the same reading, asked for again, is not another one
        self._pruned = 0.0
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS ranges (
                           series TEXT, ts INTEGER, n INTEGER, lo REAL, hi REAL, PRIMARY KEY (series, ts)) WITHOUT ROWID""")
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    def add(self, data: dict, now: float | None = None) -> int:
        """Count the readings of a real_time response into their 5-minute stretches. Returns how many were new."""
        now = time.time() if now is None else now
        counted = 0
        with self._lock:
            for series, when, value in readings_in(data):
                when = when or int(now)
                if self._seen.get(series) == when:
                    continue
                self._seen[series] = when
                self.db.execute("INSERT INTO ranges VALUES (?, ?, 1, ?, ?) ON CONFLICT (series, ts) DO UPDATE SET "
                                "n = n + 1, lo = MIN(lo, excluded.lo), hi = MAX(hi, excluded.hi)",
                                (series, round(when / BUCKET) * BUCKET, value, value))
                counted += 1
            if now - self._pruned > PRUNE_EVERY:
                self._pruned = now
                self.db.execute("DELETE FROM ranges WHERE ts < ?", (int(now) - RANGE_KEEP_DAYS * 86400,))
            self.db.commit()
        return counted

    def between(self, series: str, start: int, end: int) -> dict[int, tuple[float, float]]:
        """{the 5-minute mark: (lowest, highest)} for the stretches with a range, in [start, end]."""
        with self._lock:
            rows = self.db.execute("SELECT ts, lo, hi FROM ranges WHERE series=? AND ts BETWEEN ? AND ? AND n >= ?",
                                   (series, start, end, MIN_READINGS)).fetchall()
        return {ts: (lo, hi) for ts, lo, hi in rows}
