"""Is everything cached? Reads the two SQLite caches (no network, no API keys) and reports, per
resolution, what is stored and any gaps. Run it on the server, from the repo directory:

    python scripts/cache_status.py [--ecowitt ecowitt_cache.sqlite] [--airgradient airgradient_cache.sqlite]

The newest day or two is not reported as a gap (recent readings are still settling). Any other gap
means the archive hasn't finished or a range failed; it fills in on the next start or nightly run.
"""

import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from _common import ROOT, parser  # noqa: E402  (also puts the repo on sys.path)

from lib.ecowitt.api import GROUPS, RETENTION  # noqa: E402
from lib.ecowitt.store import horizon, subtract  # noqa: E402


def day(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def ecowitt(path: Path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    for (mac,) in db.execute("SELECT DISTINCT mac FROM coverage").fetchall():
        first = db.execute("SELECT MIN(ts) FROM points WHERE mac=?", (mac,)).fetchone()[0]
        rows = db.execute("SELECT COUNT(*) FROM points WHERE mac=?", (mac,)).fetchone()[0]
        print(f"Ecowitt {mac}: {rows:,} readings, station data from {day(first) if first else 'n/a'}")
        now = time.time()
        for cycle in ("5min", "30min", "4hour", "1day"):
            start = max(now - (RETENTION[cycle] - 2) * 86400, (first or now) - 86400)
            end = horizon(cycle)
            for group in GROUPS:
                cov = db.execute("SELECT start, end FROM coverage WHERE mac=? AND cycle=? AND grp=?",
                                 (mac, cycle, group)).fetchall()
                if not cov:
                    print(f"  {cycle:6} {group:15} nothing cached")
                    continue
                gaps = subtract((int(start), int(end)), cov)
                if gaps and gaps[-1][1] >= end - 1 and gaps[-1][0] >= end - 2 * 86400:
                    gaps.pop()  # the newest day or two: still settling, filled in by the next archive run
                stored = db.execute("SELECT COUNT(*) FROM points WHERE mac=? AND cycle=? AND grp=?",
                                    (mac, cycle, group)).fetchone()[0]
                span = f"{day(min(c[0] for c in cov))} to {day(max(c[1] for c in cov))}"
                if not gaps:
                    print(f"  {cycle:6} {group:15} complete, {span}, {stored:,} readings")
                else:
                    missing = sum(b - a + 1 for a, b in gaps) / 86400
                    print(f"  {cycle:6} {group:15} {len(gaps)} gap(s), {missing:.1f} day(s) missing "
                          f"(first {day(gaps[0][0])}, last {day(gaps[-1][1])}), have {span}")


def airgradient(path: Path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    for (loc,) in db.execute("SELECT DISTINCT loc FROM days").fetchall():
        days = {date.fromisoformat(d): n for d, n in db.execute("SELECT day, n FROM days WHERE loc=?", (loc,))}
        readings = db.execute("SELECT COUNT(*) FROM readings WHERE loc=?", (loc,)).fetchone()[0]
        with_data = sorted(d for d, n in days.items() if n)
        print(f"AirGradient {loc}: {readings:,} readings over {len(with_data)} day(s) with data "
              f"({len(days) - len(with_data)} empty day(s) recorded)")
        if not with_data:
            continue
        expected = (with_data[-1] - with_data[0]).days + 1
        missing = [with_data[0] + timedelta(days=i) for i in range(expected) if with_data[0] + timedelta(days=i) not in days]
        print(f"  from {with_data[0]} to {with_data[-1]}: "
              + ("complete" if not missing else f"{len(missing)} day(s) never fetched, e.g. {missing[0]}"))


if __name__ == "__main__":
    ap = parser(__doc__)
    ap.add_argument("--ecowitt", type=Path, default=ROOT / "ecowitt_cache.sqlite")
    ap.add_argument("--airgradient", type=Path, default=ROOT / "airgradient_cache.sqlite")
    args = ap.parse_args()
    for path, report in ((args.ecowitt, ecowitt), (args.airgradient, airgradient)):
        if path.exists():
            report(path)
        else:
            print(f"{path}: not found")
