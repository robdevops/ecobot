"""Convert the Ecowitt history cache to the smaller layout, in place. Run it ONCE, with the bot stopped:

    python scripts/migrate_cache.py /var/lib/ecobot/ecowitt_cache.sqlite [--no-backup]

The old layout repeated (mac, cycle, group, field) as text in the key of every reading; the new one keeps each series once in a `series`
table and keys a reading by that small id and its time. Every value string and unit is carried over as it is, so nothing is fetched again
(Ecowitt only keeps 90 days of 5-minute readings, so the cache cannot be rebuilt). The conversion is one transaction; the original is kept as
<file>.old unless --no-backup, and the file is compacted at the end. A file already in the new layout is left alone.
"""

import shutil
import sqlite3
from pathlib import Path

from _common import parser


def converted(db: sqlite3.Connection) -> bool:
    return "series" in {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def migrate(path: Path, backup: bool = True) -> tuple[int, int, int, int]:
    """(rows, series, bytes before, bytes after); rows is -1 when the file was already converted."""
    before = path.stat().st_size
    db = sqlite3.connect(path, isolation_level=None)
    try:
        if converted(db):
            return -1, 0, before, before
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if backup:
            if shutil.disk_usage(path.parent).free < 2 * before:
                raise SystemExit(f"Not enough free disk for the backup and the conversion (need about {2 * before / 1e6:.0f} MB)")
            shutil.copy2(path, path.with_name(path.name + ".old"))
        db.execute("BEGIN")   # one statement at a time: executescript would commit first
        for statement in (
                "ALTER TABLE points RENAME TO points_old",
                "ALTER TABLE fields RENAME TO fields_old",
                """CREATE TABLE series (id INTEGER PRIMARY KEY, mac TEXT, cycle TEXT, grp TEXT, field TEXT, unit TEXT,
                                       UNIQUE (mac, cycle, grp, field))""",
                "CREATE TABLE points (series INTEGER, ts INTEGER, value TEXT, PRIMARY KEY (series, ts)) WITHOUT ROWID",
                "INSERT INTO series (mac, cycle, grp, field, unit) SELECT mac, cycle, grp, field, unit FROM fields_old ORDER BY mac, cycle, grp, field",
                "INSERT OR IGNORE INTO series (mac, cycle, grp, field, unit) SELECT DISTINCT mac, cycle, grp, field, '' FROM points_old",
                """INSERT INTO points SELECT s.id, p.ts, p.value FROM points_old p
                   JOIN series s ON s.mac = p.mac AND s.cycle = p.cycle AND s.grp = p.grp AND s.field = p.field ORDER BY s.id, p.ts"""):
            db.execute(statement)
        rows = db.execute("SELECT COUNT(*) FROM points").fetchone()[0]
        if rows != db.execute("SELECT COUNT(*) FROM points_old").fetchone()[0]:
            raise SystemExit("The converted row count differs from the original; nothing was changed")
        series = db.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        db.execute("DROP TABLE points_old")
        db.execute("DROP TABLE fields_old")
        db.execute("COMMIT")
        db.execute("VACUUM")
        return rows, series, before, path.stat().st_size
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    ap = parser(__doc__)
    ap.add_argument("cache", type=Path, help="the Ecowitt history cache (ecowitt_cache.sqlite)")
    ap.add_argument("--no-backup", action="store_true", help="do not keep <file>.old")
    args = ap.parse_args()
    rows, series, before, after = migrate(args.cache, backup=not args.no_backup)
    if rows < 0:
        print(f"{args.cache} is already in the new layout; nothing to do.")
    else:
        print(f"Converted {rows:,} readings in {series} series: {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB"
              + ("" if args.no_backup else f" (the original is {args.cache.name}.old)"))
