"""What the bot's file holds, and what it lacks. Reads ecobot.sqlite (no network, no API keys) and reports, per sensor and resolution,
what is stored and any gaps. Run it on the server, from the repo directory:

    python scripts/cache_status.py [--file /var/lib/ecobot/ecobot.sqlite]

The newest day or two is not reported as a gap (recent readings are still settling). Any other gap of the station's means the archive
hasn't finished or a range failed; it fills in on the next start or nightly run. A day of the air sensor's never fetched fills in at the
next start too (the backfill), unless the sensor's history starts after it.
"""

import time
from datetime import date, datetime, timedelta, UTC
from pathlib import Path

from _common import parser  # noqa: E402  (also puts the repo on sys.path)
from lib.config import data_dir, local_tz  # noqa: E402

from lib.ecowitt.api import CYCLES, kept_from  # noqa: E402
from lib.storage import FILE, Storage  # noqa: E402
from lib.storage.catalogue import KINDS  # noqa: E402
from lib.storage.coverage import merge, subtract  # noqa: E402
from lib.storage.schema import some_value  # noqa: E402
from lib.storage.write import horizon  # noqa: E402
from lib.timeutil import local_date  # noqa: E402

AIR_RESOLUTIONS = ("5min", "1hour")


def day(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def readings(storage: Storage, sensor, res: str) -> int:
    """How many readings the sensor has at this resolution (a row holding only a band is not one)."""
    names = [c.name for c in KINDS[sensor.kind]]
    return storage.read_now(lambda r: r.db.execute(f"SELECT COUNT(*) FROM {sensor.kind} WHERE sensor = ? AND res = ? AND {some_value(names)}",
                                                   (sensor.id, res)).fetchone()[0])


def station(storage: Storage, mac: str):
    sensors = storage.station(mac)
    held = [span for s in sensors.values() for cycle in CYCLES for span in storage.coverage.of(s.id, cycle)]
    first = min((start for start, _ in held), default=None)
    total = sum(readings(storage, s, cycle) for s in sensors.values() for cycle in CYCLES)
    print(f"Ecowitt {mac}: {total:,} readings, station data from {day(first) if first else 'n/a'}")
    now = time.time()
    for cycle in CYCLES:
        start = max(datetime.combine(kept_from(cycle, date.today()), datetime.min.time()).timestamp(), (first or now) - 86400)
        end = horizon(cycle, now)
        for group, sensor in sensors.items():
            cov = storage.coverage.of(sensor.id, cycle)
            if not cov:
                print(f"  {cycle:6} {group:15} nothing held")
                continue
            gaps = subtract((int(start), int(end)), cov)
            if gaps and gaps[-1][1] >= end - 1 and gaps[-1][0] >= end - 2 * 86400:
                gaps.pop()  # the newest day or two: still settling, filled in by the next archive run
            stored = readings(storage, sensor, cycle)
            span = f"{day(cov[0][0])} to {day(cov[-1][1])}"
            if not gaps:
                print(f"  {cycle:6} {group:15} complete, {span}, {stored:,} readings")
            else:
                missing = sum(b - a + 1 for a, b in gaps) / 86400
                print(f"  {cycle:6} {group:15} {len(gaps)} gap(s), {missing:.1f} day(s) missing "
                      f"(first {day(gaps[0][0])}, last {day(gaps[-1][1])}), have {span}")


def airgradient(storage: Storage, sensor, tz):
    held = {res: storage.coverage.of(sensor.id, res) for res in AIR_RESOLUTIONS}
    total = sum(readings(storage, sensor, res) for res in AIR_RESOLUTIONS)
    spans = merge([span for found in held.values() for span in found])
    days = {res: storage.coverage.days(sensor.id, res) for res in AIR_RESOLUTIONS}
    print(f"AirGradient {sensor.device}: {total:,} readings over {sum(days.values())} day(s) held "
          f"({days['5min']} at 5 minutes, {days['1hour']} hourly)")
    if not spans:
        return
    first, last = local_date(spans[0][0], tz), local_date(spans[-1][1], tz)
    never = [local_date(a, tz) + timedelta(days=n) for a, b in subtract((spans[0][0], spans[-1][1]), spans)
             for n in range((b - a + 1) // 86400)]
    print(f"  from {first} to {last}: " + ("complete" if not never else f"{len(never)} day(s) never fetched, e.g. {never[0]}"))


if __name__ == "__main__":
    ap = parser(__doc__)
    ap.add_argument("--file", type=Path, default=data_dir() / FILE, help="the bot's file (default: ecobot.sqlite in DATA_DIR)")
    args = ap.parse_args()
    if not args.file.exists():
        raise SystemExit(f"{args.file}: not found")
    tz = local_tz()
    storage = Storage(args.file, tz)
    try:
        for mac in sorted({s.device for s in storage.sensors.all() if s.kind != "air"}):
            station(storage, mac)
        for sensor in storage.sensors.all(kind="air"):
            airgradient(storage, sensor, tz)
        if not storage.sensors.all():
            print(f"{args.file}: no sensors yet")
    finally:
        storage.close()
