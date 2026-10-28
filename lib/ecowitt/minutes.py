"""The last few hours of minute readings from Home Assistant, kept on disk so a short chart is drawn without asking it.

A chart of up to MINUTE_HOURS is drawn a reading a minute (Ecowitt's cloud has no such cycle). Rather than ask Home Assistant for them at every
chart, the bot takes them as they come (at start for the last KEEP_HOURS, then at each new live reading) and a chart reads them here. They are
kept only as long as a chart can use them, and in the shape the history is told in, {group: {field: {"unit", "list": {epoch: value}}}}.
"""

import sqlite3
import threading

MINUTE_HOURS = 8                     # a chart of up to this long is drawn from minutes
KEEP_HOURS = MINUTE_HOURS + 1        # ... and may end an hour ago: what is older is no use
SLACK_SECONDS = 300                  # the first minute held may be this much after a chart's start (the chart's first point is then a little late)


class MinuteStore:
    def __init__(self, path):
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS minutes (
                           grp TEXT, field TEXT, t INTEGER, unit TEXT, value TEXT, PRIMARY KEY (grp, field, t)) WITHOUT ROWID""")
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    def put(self, data: dict, now: float, keep_seconds: int = KEEP_HOURS * 3600) -> int:
        """Keep these minutes (a minute held already is replaced: the last one is told as far as it had gone) and drop what is older than a
        chart can use. Returns how many were kept."""
        rows = [(group, field, int(t), obj.get("unit", ""), str(v)) for group, fields in data.items() for field, obj in fields.items()
                for t, v in obj["list"].items() if int(t) >= now - keep_seconds]
        with self._lock:
            self.db.executemany("INSERT OR REPLACE INTO minutes VALUES (?, ?, ?, ?, ?)", rows)
            self.db.execute("DELETE FROM minutes WHERE t < ?", (int(now - keep_seconds),))
            self.db.commit()
        return len(rows)

    def newest(self) -> int | None:
        """The time of the newest minute held (the one the next pull starts from), None when it holds none."""
        with self._lock:
            return self.db.execute("SELECT MAX(t) FROM minutes").fetchone()[0]

    def get(self, groups: list[str], start: int, end: int) -> dict:
        """The minutes in [start, end] for these groups; {} when it does not reach back to the start (a chart then uses the 5-minute
        readings), or holds none."""
        marks = ",".join("?" * len(groups))
        with self._lock:
            rows = self.db.execute(f"SELECT grp, field, t, unit, value FROM minutes WHERE grp IN ({marks}) AND t BETWEEN ? AND ? ORDER BY t",
                                   (*groups, start, end)).fetchall()
        if not rows or rows[0][2] > start + SLACK_SECONDS:
            return {}
        out: dict = {}
        for group, field, t, unit, value in rows:
            out.setdefault(group, {}).setdefault(field, {"unit": unit, "list": {}})["list"][str(t)] = value
        return out
