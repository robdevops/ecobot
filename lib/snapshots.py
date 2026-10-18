"""The pollen page and the forecast kept on disk, so a restart (even at night, when nothing is fetched) starts with the last
fetch, and what the sites said is kept as a history.

One row per distinct result: a fetch that finds the same content only moves the row's last_seen forward, so a day of polling
adds a handful of rows, not hundreds. Rows older than a year are dropped."""

import json
import sqlite3
import threading
import time

KEEP_SECONDS = 400 * 86400


class Snapshots:
    """One source's connection to the file: the pollen and forecast sources each open their own (the file is in WAL mode,
    so two connections take turns to write) and close it when they stop."""

    def __init__(self, path):
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY, kind TEXT, first_seen REAL, last_seen REAL, payload TEXT);
            CREATE INDEX IF NOT EXISTS snapshots_kind ON snapshots (kind, id);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
        """)
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    def save(self, kind: str, payload: dict, now: float | None = None) -> bool:
        """Record a fetch; True if it differs from the last one stored (a new row), False if it only confirmed it."""
        now = time.time() if now is None else now
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        with self._lock:
            last = self.db.execute("SELECT id, payload FROM snapshots WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
            changed = last is None or last[1] != text
            if changed:
                self.db.execute("INSERT INTO snapshots (kind, first_seen, last_seen, payload) VALUES (?, ?, ?, ?)",
                                (kind, now, now, text))
            else:
                self.db.execute("UPDATE snapshots SET last_seen=? WHERE id=?", (now, last[0]))
            self.db.execute("DELETE FROM snapshots WHERE kind=? AND last_seen<?", (kind, now - KEEP_SECONDS))
            self.db.commit()
        return changed

    def latest(self, kind: str) -> tuple[float, dict] | None:
        """(when it was last fetched, the payload) of the newest row."""
        with self._lock:
            row = self.db.execute("SELECT last_seen, payload FROM snapshots WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            self.db.commit()
