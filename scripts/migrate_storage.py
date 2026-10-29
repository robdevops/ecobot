"""Move a copy of the bot's history into the new storage (ecobot.sqlite, docs/storage-plan.md) and say how it went: the check of every
count and sum, the fields kept as they came, the values written back with other digits, and how long the new storage's reads take. The
bot's own files are only copied (with SQLite's backup, so the bot can keep running) and never changed. The new file is made in a temporary
folder and deleted afterwards, unless --keep names where to put it. Nothing uses the new storage yet: this is to check it on real data.

    sudo docker compose exec ecobot python scripts/migrate_storage.py --tz Australia/Melbourne
    python scripts/migrate_storage.py [--data /var/lib/private/ecobot] [--keep /tmp/ecobot.sqlite]

and paste what it prints.
"""

import asyncio
import shutil
import statistics
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from _common import parser  # noqa: E402  (also puts the repo on sys.path)

from lib.config import data_dir, local_tz  # noqa: E402
from lib.storage import Storage  # noqa: E402
from lib.timeutil import local_date  # noqa: E402
from lib.storage.migrate import OLD, migrate  # noqa: E402

from bench_storage import copy  # noqa: E402


def reads(storage: Storage, tz) -> list[tuple[str, object]]:
    """(what, a read of the reader's) for the requests the benchmark times, as the new storage would serve them: the data alone (step 2
    builds the answers and charts on it)."""
    out = []
    now = int(time.time())
    today = local_date(now, tz)
    station = max((storage.station(s.device) for s in storage.sensors.all() if s.kind != "air"), key=len, default={})
    outdoor, rain = station.get("outdoor"), station.get("rainfall")
    if outdoor:
        o = [outdoor.id]
        out += [("Temperature line, 1D (5-minute rows)", lambda r: r.rows("climate", o, "5min", now - 86400, now, ["temperature"])),
                ("Temperature line, 7D (30-minute rows)", lambda r: r.rows("climate", o, "30min", now - 7 * 86400, now, ["temperature"])),
                ("Temperature line, 1M (2-hour buckets)", lambda r: r.buckets("climate", o, ("30min",), now - 30 * 86400, now, 7200,
                                                                               ["temperature"])),
                ("Temperature line, 3M (day rows)", lambda r: r.days("climate", o, today - timedelta(days=90), today, ["temperature"])),
                ("Temperature line, 1Y (day rows)", lambda r: r.days("climate", o, today - timedelta(days=365), today, ["temperature"]))]
    if outdoor and rain:
        out.append(("weather_days: 4 years of temperature and rain (day rows)", lambda r: (
            r.days("climate", [outdoor.id], today - timedelta(days=1460), today, ["temperature"]),
            r.days("rain", [rain.id], today - timedelta(days=1460), today, ["daily"]))))
    if station:
        jan = today.replace(month=1, day=1)
        out.append(("/dashboard: the year's day rows of every sensor", lambda r: [
            r.days(kind, [s.id for s in station.values() if s.kind == kind], jan, today) for kind in {s.kind for s in station.values()}
            if kind != "other"]))
    if air := next(iter(storage.sensors.all(kind="air")), None):
        out.append(("Air line (PM2.5), 1M (2-hour buckets)", lambda r: r.buckets("air", [air.id], ("5min", "1hour"), now - 30 * 86400, now,
                                                                                  7200, ["pm2_5"])))
    return out


async def timed(storage: Storage, fn, repeat: int = 3) -> float:
    times = []
    for _ in range(repeat):
        started = time.perf_counter()
        await asyncio.create_task(storage.read(fn))
        times.append((time.perf_counter() - started) * 1000)
    return statistics.median(times)


async def time_reads(storage: Storage, tz):
    print("\n| Read on the new file | Time |")
    print("|---|---|")
    for label, fn in reads(storage, tz):
        print(f"| {label} | {await timed(storage, fn):,.1f} ms |")
    print("\nThe data a request needs, in one hop; the answers and charts are built on it in step 2.")


def main():
    p = parser(__doc__)
    p.add_argument("--data", type=Path, default=data_dir(), help="the bot's data folder (default: DATA_DIR, as the bot finds it)")
    p.add_argument("--tz", help="the station's time zone, e.g. Australia/Melbourne (default: TZ, else the computer's); it sets the days")
    p.add_argument("--keep", type=Path, help="where to put the new file (default: it is deleted)")
    args = p.parse_args()
    found = [name for name in OLD if (args.data / name).exists()]
    if not found:
        sys.exit(f"No cache files in {args.data}: give the bot's data folder with --data")
    tz = ZoneInfo(args.tz) if args.tz else local_tz()
    with tempfile.TemporaryDirectory(prefix="ecobot-migrate-") as tmp:
        folder = Path(tmp)
        for name in found:
            copy(args.data / name, folder / name)
        print(f"Copied {', '.join(found)} from {args.data}; time zone {tz}")
        report = migrate(folder, tz)
        print("\n".join(report.lines()))
        if not report.ok:
            sys.exit(1)
        storage = Storage(folder / "ecobot.sqlite", tz)
        try:
            asyncio.run(time_reads(storage, tz))
        finally:
            storage.close()
        if args.keep:
            shutil.copy(folder / "ecobot.sqlite", args.keep)
            print(f"\nThe new file is in {args.keep}")


if __name__ == "__main__":
    main()
