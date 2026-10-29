"""The bot's one file of readings (docs/storage-plan.md): `ecobot.sqlite` in the data folder.

Storage opens it, makes or upgrades its tables (schema.py, from the catalogue), and keeps two connections, each on a thread of its own:
the writer's (the archive, the refreshes, the live readings, the migration) and the reader's (the questions, the reports, the alerts). In
WAL mode a read never waits for a write. A request runs all its queries in one hop to the reader's thread, in one read transaction:

    rows, days = await storage.read(lambda r: (r.rows("climate", [s.id], "30min", start, end), r.days("climate", [s.id], first, last)))

and a write is one transaction on the writer's thread (`await storage.write(lambda w: w.put_ecowitt(...))`). The sensors and the coverage
are kept in memory as well, so neither is ever a query.
"""

import asyncio
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import tzinfo
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import schema
from .catalogue import AIR, AIR_EXTERNAL, KINDS
from .coverage import Coverage

log = logging.getLogger(__name__)

FILE = "ecobot.sqlite"


class Sensor(NamedTuple):
    id: int
    kind: str
    role: str
    label: str
    device: str
    external: str


class Sensors:
    """The sensors, by id and by (device, external), shared by the reader and the writer; a write's new sensors are seen once it is
    committed."""

    def __init__(self, rows: list[tuple]):
        self._by_id: dict[int, Sensor] = {}
        self._by_key: dict[tuple[str, str], Sensor] = {}
        self._lock = threading.Lock()
        self.publish([Sensor(*row) for row in rows])

    def find(self, device: str, external: str) -> Sensor | None:
        with self._lock:
            return self._by_key.get((device, external))

    def all(self, device: str | None = None, kind: str | None = None) -> list[Sensor]:
        with self._lock:
            return [s for s in self._by_id.values() if (device is None or s.device == device) and (kind is None or s.kind == kind)]

    def publish(self, new: list[Sensor]):
        with self._lock:
            for s in new:
                self._by_id[s.id] = s
                self._by_key[s.device, s.external] = s


def zone_named(name: str, default: tzinfo) -> tzinfo:
    """The zone a file names, else (a name this Python does not know) `default`."""
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        log.warning("Storage: the file's time zone %r is not one this Python knows; using %s", name, default)
        return default


def connect(path: Path, bulk: bool = False) -> sqlite3.Connection:
    """A connection to the file. bulk (the migration, into a file of its own): no WAL and no syncing, as fast as it goes, since a failure
    deletes the file."""
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    db.execute("PRAGMA busy_timeout = 5000")
    db.execute("PRAGMA temp_store = MEMORY")
    if bulk:
        db.execute("PRAGMA journal_mode = MEMORY")
        db.execute("PRAGMA synchronous = OFF")
    else:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")   # in WAL mode a power cut may lose the last commits, never the file
    return db


class Storage:
    def __init__(self, path, tz: tzinfo, bulk: bool = False):
        """tz: the zone a new file's days are worked out in; a file keeps its own (the station's, see set_timezone)."""
        from .read import Reader
        from .write import Writer
        self.path, self.tz, self.bulk = Path(path), tz, bulk
        self.write_db = connect(self.path, bulk)
        added = schema.create(self.write_db)
        if added:
            log.info("Storage: new readings in the catalogue: %s", ", ".join(added))
        self.sensors = Sensors(self.write_db.execute("SELECT id, kind, role, label, device, external FROM sensors").fetchall())
        self.coverage = Coverage(self.write_db.execute('SELECT sensor, res, "start", "end" FROM coverage').fetchall())
        self.read_db = connect(self.path, bulk)
        self.writer, self.reader = Writer(self), Reader(self)
        self._write_pool = ThreadPoolExecutor(1, thread_name_prefix="storage-write")
        self._read_pool = ThreadPoolExecutor(1, thread_name_prefix="storage-read")
        if (zone := self.reader.meta("tz")) is None:
            with self.writer.transaction():
                self.writer.set_meta("tz", str(getattr(tz, "key", tz)))
        else:
            self.tz = self.writer.tz = zone_named(zone, tz)

    async def set_timezone(self, tz: tzinfo):
        """The station's days are local to it: a zone other than the file's works them all out again."""
        if str(getattr(tz, "key", tz)) != str(getattr(self.tz, "key", self.tz)):
            try:
                await self.write(lambda w: w.set_timezone(tz))
            except BaseException:   # (rolled back: the file and its days are still in the old zone)
                self.writer.tz = self.tz
                raise
            self.tz = tz

    # ---------- the hops ----------
    async def read(self, fn, *args):
        """fn(reader, *args) on the reader's thread, in one read transaction (its queries see one moment of the file)."""
        return await self._hop(self._read_pool, self.reader.run, fn, args)

    async def write(self, fn, *args):
        """fn(writer, *args) on the writer's thread, in one transaction."""
        return await self._hop(self._write_pool, self.writer.run, fn, args)

    def read_now(self, fn, *args):
        """read() for code that is not async: the calling thread waits for the reader's (never call it on the reader's own thread)."""
        return self._read_pool.submit(self.reader.run, fn, args).result()

    @staticmethod
    async def _hop(pool: ThreadPoolExecutor, run, fn, args):
        return await asyncio.get_running_loop().run_in_executor(pool, run, fn, args)

    # ---------- the sensors ----------
    def air_sensor(self, location: str) -> Sensor | None:
        return self.sensors.find(str(location), AIR_EXTERNAL)

    def station(self, mac: str) -> dict[str, Sensor]:
        """{Ecowitt group: sensor} of one station."""
        return {s.external: s for s in self.sensors.all(device=mac) if s.kind != AIR[0]}

    def close(self):
        self._write_pool.shutdown(wait=True)
        self._read_pool.shutdown(wait=True)
        if not self.bulk:
            self.write_db.execute("PRAGMA optimize")
        self.read_db.close()
        self.write_db.close()


__all__ = ["FILE", "KINDS", "Sensor", "Sensors", "Storage", "connect"]
