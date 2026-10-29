"""The file's tables, generated from the catalogue, and its versions.

The readings of each kind of sensor are one table keyed by (sensor, res, ts), WITHOUT ROWID: a sensor's readings at one resolution are a run
of pages in time order, and a new reading goes on the end of its run. Each reading has its value and its lowest and highest (`_lo`, `_hi`);
`src` says where the values came from and `final` whether they may still change. Each kind's day rows are `<kind>_day`, keyed by
(sensor, local date). A reading added to the catalogue later is added to its tables at the next start (ALTER TABLE ... ADD COLUMN, at once).
"""

import sqlite3

from .catalogue import KINDS

SCHEMA_VERSION = 1

FIXED = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sensors (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,       -- climate, pressure, wind, rain, light, air (catalogue.KINDS), or other
    role TEXT NOT NULL,       -- the name the code and the model use: outdoor, indoor, rainfall_piezo, air ...
    label TEXT NOT NULL,      -- shown on charts and in answers
    device TEXT NOT NULL,     -- the station's MAC, the AirGradient's location
    external TEXT NOT NULL,   -- the source's name for it: Ecowitt's group, "air"
    UNIQUE (device, external));
CREATE TABLE IF NOT EXISTS fields (   -- the readings a sensor has at a resolution, and their units as the source writes them
    sensor INTEGER NOT NULL, res TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL,
    PRIMARY KEY (sensor, res, name)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS coverage (   -- the times fetched, merged (readings or none: nothing to fetch again)
    sensor INTEGER NOT NULL, res TEXT NOT NULL, "start" INTEGER NOT NULL, "end" INTEGER NOT NULL,
    PRIMARY KEY (sensor, res, "start")) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS extra (   -- a field the catalogue has no column for, or a value that is not a number: kept as it came
    sensor INTEGER NOT NULL, res TEXT NOT NULL, field TEXT NOT NULL, ts INTEGER NOT NULL, value TEXT,
    PRIMARY KEY (sensor, res, field, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, kind TEXT, first_seen REAL, last_seen REAL, payload TEXT);
CREATE INDEX IF NOT EXISTS snapshots_kind ON snapshots (kind, id);
CREATE TABLE IF NOT EXISTS snapshot_meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
"""


def q(name: str) -> str:
    """A name quoted for SQL: the readings' names may start with a digit ("1_hour")."""
    return '"' + name.replace('"', '""') + '"'


def some_value(names: list[str], table: str = "") -> str:
    """SQL that is true for a row with a value of any of these readings (one that holds only a band is not a reading)."""
    columns = [f"{table}{q(n)}" for n in names]
    return f"{columns[0]} IS NOT NULL" if len(columns) == 1 else f"COALESCE({', '.join(columns)}) IS NOT NULL"


def reading_columns(kind: str) -> list[tuple[str, str]]:
    """(name, type) of a readings table's reading columns, in order."""
    return [(f"{c.name}{part}", "REAL") for c in KINDS[kind] for part in ("", "_lo", "_hi")]


def day_columns(kind: str) -> list[tuple[str, str]]:
    return [(f"{c.name}{part}", kind_) for c in KINDS[kind]
            for part, kind_ in (("_mean", "REAL"), ("_lo", "REAL"), ("_lo_ts", "INTEGER"), ("_hi", "REAL"), ("_hi_ts", "INTEGER"))]


def readings_table(kind: str) -> str:
    columns = ", ".join(f"{q(name)} {type_}" for name, type_ in reading_columns(kind))
    return (f"CREATE TABLE IF NOT EXISTS {kind} (sensor INTEGER NOT NULL, res TEXT NOT NULL, ts INTEGER NOT NULL, "
            f"src INTEGER NOT NULL DEFAULT 0, final INTEGER NOT NULL DEFAULT 1, {columns}, PRIMARY KEY (sensor, res, ts)) WITHOUT ROWID")


def days_table(kind: str) -> str:
    columns = ", ".join(f"{q(name)} {type_}" for name, type_ in day_columns(kind))
    return (f"CREATE TABLE IF NOT EXISTS {kind}_day (sensor INTEGER NOT NULL, day TEXT NOT NULL, "
            "source TEXT NOT NULL,      -- the resolution the figures come from: 5min, 30min, 1day (Ecowitt's daily record), 1hour\n"
            "timing TEXT NOT NULL,      -- the resolution the times of the lowest and highest come from\n"
            "complete INTEGER NOT NULL, -- the source holds the whole local day\n"
            f"n INTEGER NOT NULL, {columns}, PRIMARY KEY (sensor, day)) WITHOUT ROWID")


class NewerFile(RuntimeError):
    """The file was written by a newer version of the bot: this one leaves it alone."""


def create(db: sqlite3.Connection) -> list[str]:
    """Make the tables the file lacks, and the reading columns the catalogue has gained (each "table.column"). Refuses a file from a newer
    schema."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise NewerFile(f"its schema is version {version}; this bot knows {SCHEMA_VERSION}")
    db.executescript(FIXED)
    for kind in KINDS:
        db.execute(readings_table(kind))
        db.execute(days_table(kind))
    added = []
    for kind in KINDS:
        for table, wanted in ((kind, reading_columns(kind)), (f"{kind}_day", day_columns(kind))):
            have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            for name, type_ in wanted:
                if name not in have:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {q(name)} {type_}")
                    added.append(f"{table}.{name}")
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
    return added


__all__ = ["NewerFile", "SCHEMA_VERSION", "create", "day_columns", "days_table", "q", "reading_columns", "readings_table", "some_value"]
