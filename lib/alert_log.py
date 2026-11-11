"""The alert log (docs/ecobot-mcp.md 3.6): every alert the service's rules decide, as an event a client reads and delivers.

An event is {id, kind, time, expires, title, text, data, [link: {label, url}], [chart]}:
  - id: monotonic (1, 2, ...), never reused, so a client resumes from the last one it handled;
  - kind: the alert type (lib/alerts/menu.py TYPES: rain, gusts, uv, temps, particulates, pollen_and_asthma, forecast);
  - time, expires: ISO local times with their offset; after `expires` the event is no longer news (a client back after a gap skips it);
  - title: a few words; text: ready to send as it is; data: the figures behind it;
  - link: the page that goes with it; chart: its chart as data (3.7), with the call that draws it again (`drawn_by`).

Kept in its own SQLite file in DATA_DIR (alerts.sqlite), not the readings file: the log exists whatever sources are configured (the pollen
and forecast alerts need no readings file), it is written from the event loop in tiny rows while the readings file is written in bulk from
worker threads, and it can be looked at or removed on its own. Events older than KEEP_SECONDS are dropped when it opens; the ids carry on.
A new file starts its ids at 1 again, so each file has a random `identity` (kept in its meta table) beside the ids: a reader that kept
the last id it handled keeps the identity with it, and starts afresh when the file is another (lib/alerts/notify.AlertDelivery).

A reader in the same process waits for new events with `wait(after)` (an asyncio notification on each append): the bot's delivery today,
the MCP listen of step 5."""

import asyncio
import json
import logging
import secrets
import sqlite3
import threading
import time as clock
from datetime import datetime, tzinfo

log = logging.getLogger(__name__)

KEEP_SECONDS = 30 * 86400

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    time REAL NOT NULL,
    expires REAL,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    data TEXT,
    link TEXT,
    chart TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _iso(ts: float | None, tz: tzinfo) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds")


class AlertLog:
    def __init__(self, path, tz: tzinfo, now=clock.time):
        """path: the log's file (made when there is none). tz: the zone its times are written in. now: the epoch clock (tests)."""
        self.tz, self.now = tz, now
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.execute("DELETE FROM alerts WHERE time < ?", (self.now() - KEEP_SECONDS,))
        self.db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('identity', ?)", (secrets.token_hex(8),))
        self.db.commit()
        self.identity: str = self.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0]
        self._new = asyncio.Event()

    def close(self):
        with self._lock:
            self.db.close()

    def append(self, kind: str, text: str, title: str, data: dict | None = None, expires: float | None = None,
               link: tuple[str, str] | None = None, chart: dict | None = None, time: float | None = None) -> dict:
        """Record an alert (expires: an epoch, None for never; link: (label, url); chart: the chart's data with its drawn_by) and wake the
        readers waiting. Returns the event."""
        at = self.now() if time is None else time
        with self._lock:
            cur = self.db.execute("INSERT INTO alerts (kind, time, expires, title, text, data, link, chart) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                  (kind, at, expires, title, text, json.dumps(data or {}, ensure_ascii=False),
                                   json.dumps({"label": link[0], "url": link[1]}, ensure_ascii=False) if link else None,
                                   json.dumps(chart, ensure_ascii=False) if chart else None))
            self.db.commit()
            event_id = cur.lastrowid
        woken, self._new = self._new, asyncio.Event()
        woken.set()
        log.info("Alert %d (%s): %s", event_id, kind, title)
        return self.get(event_id)

    def _event(self, row) -> dict:
        event_id, kind, at, expires, title, text, data, link, chart = row
        event = {"id": event_id, "kind": kind, "time": _iso(at, self.tz), "expires": _iso(expires, self.tz), "title": title, "text": text,
                 "data": json.loads(data) if data else {}}
        if link:
            event["link"] = json.loads(link)
        if chart:
            event["chart"] = json.loads(chart)
        return event

    def get(self, event_id: int) -> dict | None:
        """One event by its id (None when there is none, or it was dropped), expired or not."""
        with self._lock:
            row = self.db.execute("SELECT * FROM alerts WHERE id=?", (event_id,)).fetchone()
        return self._event(row) if row else None

    def since(self, after: int = 0, expired: bool = False) -> list[dict]:
        """The events after the id `after`, oldest first; only those not yet expired unless `expired`."""
        query, args = "SELECT * FROM alerts WHERE id > ?", [after]
        if not expired:
            query += " AND (expires IS NULL OR expires > ?)"
            args.append(self.now())
        with self._lock:
            rows = self.db.execute(query + " ORDER BY id", args).fetchall()
        return [self._event(row) for row in rows]

    def last_id(self) -> int:
        """The newest event's id (0 when there has been none): what a reader starting now has already seen."""
        with self._lock:
            row = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='alerts'").fetchone()
        return row[0] if row else 0

    async def wait(self, after: int, timeout: float | None = None) -> bool:
        """Until there is an event after the id `after` (True), or `timeout` seconds have passed (False)."""
        try:
            async with asyncio.timeout(timeout):
                while self.last_id() <= after:
                    await self._new.wait()
            return True
        except TimeoutError:
            return False
