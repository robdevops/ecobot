"""Find a spike in the bot's file (ecobot.sqlite) and change it. A dry run unless --apply: it shows what it would change.

    python scripts/edit_readings.py --list                                 # the readings' names, by sensor
    python scripts/edit_readings.py co2                                    # the 10 highest CO2 readings, to find the spike
    python scripts/edit_readings.py co2 --at 2026-10-27T13:25              # that one reading, with those either side of it
    python scripts/edit_readings.py co2 --at 2026-10-27T13:25 --apply      # changed to the mean of the readings either side
    python scripts/edit_readings.py co2 --at 2026-10-27T13:25 --set 425 --apply
    python scripts/edit_readings.py co2 --from 2026-10-27T13:00 --to 2026-10-27T14:00 --above 1000   # every one over 1000 then

What is matched: the reading at --at (each resolution held at that exact time: --res picks one), or those from --from up to --to, over
--above / under --below where given (by their value or their band's high or low); of every sensor of its kind, or --sensor's alone
(outdoor and indoor both have a temperature: --list names them). A dry run shows each match marked >> among the
readings either side of it (--context, 3 by default) with the value it would get, so you can see you caught the whole spike and
nothing more.

Each match gets the mean of the nearest readings either side of it that aren't matched (or --set), and loses its band (the lowest and
highest inside it). Its row is marked as edited by hand, which outranks every source: a later fetch of that time (the AirGradient's
backfill or gap filling, Ecowitt's archive, scripts/refill_airgradient.py) only fills in what the row lacks, never its values. The
other readings of that time stay as they are. The days it falls on are worked out again in the same transaction, so the day figures
and long charts follow.

Times are local (TZ, else the system's), or carry their own offset (2026-10-27T13:25+11:00). The bot may keep running: it reads the file
in WAL mode. A chart it drew before the change can be reused for a few minutes.
"""

from datetime import UTC, datetime
from pathlib import Path

from _common import parser  # noqa: E402  (also puts the repo on sys.path)
from lib.config import data_dir, local_tz  # noqa: E402

from lib.storage import FILE, Storage  # noqa: E402
from lib.storage.catalogue import BY_NAME, EDITED  # noqa: E402
from lib.storage.days import ANY  # noqa: E402
from lib.storage.schema import q  # noqa: E402

TOP = 10     # with nothing narrowed, how many of the highest are listed
SHOWN = 50   # the most matches listed (all are changed)
CONTEXT = 3  # the readings either side of each match shown in a dry run (--context)


def kind_of(reading: str) -> str:
    kind = next((k for k, columns in BY_NAME.items() if reading in columns), None)
    if kind is None:
        names = sorted(n for columns in BY_NAME.values() for n in columns)
        raise SystemExit(f"{reading!r} is not a reading in the file (--list shows them); one of: {', '.join(names)}")
    return kind


def epoch(text: str, tz) -> int:
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"{text!r} is not an ISO 8601 date and time, e.g. 2026-10-27T13:25") from None
    return int((t if t.tzinfo else t.replace(tzinfo=tz)).timestamp())


def _cells(reading: str) -> tuple[str, str, str, str]:
    v, lo, hi = q(reading), q(reading + "_lo"), q(reading + "_hi")
    return v, lo, hi, f"({v} IS NOT NULL OR {lo} IS NOT NULL OR {hi} IS NOT NULL)"


def matches(storage: Storage, kind: str, reading: str, start: int | None, end: int | None, above: float | None, below: float | None,
            top: int | None = None, res: str | None = None, sensor: int | None = None) -> list[tuple]:
    """(sensor, res, ts, value, lo, hi) of the readings matched, oldest first (the highest first with `top`)."""
    v, lo, hi, has = _cells(reading)
    where, args = [has], []
    for sql, value in (("ts >= ?", start), ("ts < ?", end), ("res = ?", res), ("sensor = ?", sensor),
                       (f"MAX(COALESCE({v}, {hi}), COALESCE({hi}, {v})) > ?", above),
                       (f"MIN(COALESCE({v}, {lo}), COALESCE({lo}, {v})) < ?", below)):
        if value is not None:
            where.append(sql)
            args.append(value)
    order = f"MAX(COALESCE({v}, {hi}), COALESCE({hi}, {v})) DESC LIMIT {top}" if top else "sensor, res, ts"
    sql = f"SELECT sensor, res, ts, {v}, {lo}, {hi} FROM {kind} WHERE {' AND '.join(where)} ORDER BY {order}"
    return storage.read_now(lambda r: r.db.execute(sql, args).fetchall())


def around(storage: Storage, kind: str, reading: str, found: list[tuple], n: int) -> list[tuple]:
    """The matched readings with the `n` either side of each (same sensor and resolution), oldest first: (sensor, res, ts, value, lo,
    hi, matched)."""
    v, lo, hi, has = _cells(reading)
    picked = {(sensor, res, ts) for sensor, res, ts, *_ in found}
    rows = {(row[0], row[1], row[2]): row for row in found}

    def read(r):
        for sensor, res, ts in picked:
            for op, order in (("<", "DESC"), (">", "ASC")):
                for row in r.db.execute(f"SELECT sensor, res, ts, {v}, {lo}, {hi} FROM {kind} WHERE sensor = ? AND res = ? AND ts {op} ? "
                                        f"AND {has} ORDER BY ts {order} LIMIT ?", (sensor, res, ts, n)):
                    rows.setdefault((row[0], row[1], row[2]), row)
    if n > 0:
        storage.read_now(read)
    return [(*rows[k], k in picked) for k in sorted(rows)]


def new_values(storage: Storage, kind: str, reading: str, found: list[tuple], value: float | None = None) -> dict[tuple, float | None]:
    """{(sensor, res, ts): the value each match gets}: `value`, else the mean of the nearest readings before and after it that aren't
    matched (one side's alone at the edge of what is held; None when there are none)."""
    if value is not None:
        return {(s, res, ts): value for s, res, ts, *_ in found}
    v = q(reading)
    picked = {(s, res, ts) for s, res, ts, *_ in found}
    out = {}

    def read(r):
        for sensor, res, ts in picked:
            near = []
            for op, order in (("<", "DESC"), (">", "ASC")):
                for t, x in r.db.execute(f"SELECT ts, {v} FROM {kind} WHERE sensor = ? AND res = ? AND ts {op} ? AND {v} IS NOT NULL "
                                         f"ORDER BY ts {order}", (sensor, res, ts)):
                    if (sensor, res, t) not in picked:
                        near.append(x)
                        break
            out[sensor, res, ts] = round(sum(near) / len(near), 1) if near else None
    storage.read_now(read)
    return out


def change(storage: Storage, kind: str, reading: str, values: dict[tuple, float | None]):
    """Each match given its new value, its band taken off and its row marked as edited by hand; their days worked out again: one
    transaction."""
    sensors = {s.id: s for s in storage.sensors.all()}
    sql = (f"UPDATE {kind} SET {q(reading)} = ?, {q(reading + '_lo')} = NULL, {q(reading + '_hi')} = NULL, src = {EDITED}, final = 1 "
           "WHERE sensor = ? AND res = ? AND ts = ?")
    with storage.writer.transaction() as w:
        w.db.executemany(sql, [(value, sensor, res, ts) for (sensor, res, ts), value in values.items()])
        for sensor, _, ts in values:
            w.touch(sensors[sensor], ANY, ts, ts)


def listing(storage: Storage | None) -> list[str]:
    """The readings' names by kind of sensor, with the sensors the file has of each kind, by the name --sensor takes (none: a kind it
    holds nothing of yet)."""
    sensors = storage.sensors.all() if storage else []
    out = []
    for kind, columns in BY_NAME.items():
        held = sorted(f"{s.role} = {s.label} ({s.device})" for s in sensors if s.kind == kind)
        out.append(f"{kind}: {', '.join(held) if held else 'no sensor in the file'}")
        out.append(f"  {' '.join(columns)}")
    return out


def show(storage: Storage, rows: list[tuple], tz, values: dict | None = None):
    """Each reading: its local time, resolution, value and band, sensor; with `values` (rows from around), the matched ones marked >>
    with the value they get, and a blank line between sensors and resolutions."""
    names = {s.id: f"{s.label} ({s.device})" for s in storage.sensors.all()}
    limit = SHOWN * (1 + 2 * CONTEXT) if values is not None else SHOWN
    last = None
    fmt = lambda x: "-" if x is None else f"{x:g}"
    for row in rows[:limit]:
        sensor, res, ts, value, lo, hi = row[:6]
        if values is not None and last is not None and last != (sensor, res):
            print()
        last = (sensor, res)
        band = f"  band {lo:g} to {hi:g}" if lo is not None and hi is not None else ""
        flag, to = "", ""
        if values is not None:
            flag = ">> " if row[6] else "   "
            to = f"  -> {fmt(values.get((sensor, res, ts)))}" if row[6] else ""
        print(f"  {flag}{datetime.fromtimestamp(ts, UTC).astimezone(tz).isoformat()}  {res:6} {fmt(value):>9}{band}{to}  "
              f"{names.get(sensor, sensor)}")
    if len(rows) > limit:
        print(f"  ... and {len(rows) - limit} more")


if __name__ == "__main__":
    ap = parser(__doc__)
    ap.add_argument("reading", nargs="?", help="the reading's name in the file: co2, pm2_5, temperature, humidity, wind_gust ... (--list)")
    ap.add_argument("--list", action="store_true", help="list the readings' names, by sensor, and stop")
    ap.add_argument("--at", help="just the reading at this time (ISO 8601, local unless it has an offset)")
    ap.add_argument("--from", dest="start", help="the first time")
    ap.add_argument("--to", dest="end", help="the end (not included)")
    ap.add_argument("--above", type=float, help="only readings (or their band's high) over this")
    ap.add_argument("--below", type=float, help="only readings (or their band's low) under this")
    ap.add_argument("--sensor", help="only this sensor, by the name --list gives it: outdoor, indoor, air ... (default: every one of the kind)")
    ap.add_argument("--res", help="only this resolution: 1min, 5min, 30min, 1hour, 4hour, 1day")
    ap.add_argument("--set", type=float, dest="value", help="the value to give them (default: the mean of the readings either side)")
    ap.add_argument("--context", type=int, default=CONTEXT, help=f"readings shown either side of each match (default {CONTEXT})")
    ap.add_argument("--apply", action="store_true", help="make the change (without it: a dry run)")
    ap.add_argument("--file", type=Path, default=data_dir() / FILE, help="the bot's file (default: ecobot.sqlite in DATA_DIR)")
    args = ap.parse_args()
    if args.list:
        storage = Storage(args.file, local_tz()) if args.file.exists() else None
        print("\n".join(listing(storage)))
        if storage:
            storage.close()
        raise SystemExit(0)
    if not args.reading:
        ap.error("name a reading (--list shows them)")
    if not args.file.exists():
        raise SystemExit(f"{args.file}: not found")
    if args.at and (args.start or args.end):
        raise SystemExit("--at is one time: give it, or --from/--to, not both")
    tz = local_tz()
    kind = kind_of(args.reading)
    if args.at:
        start = epoch(args.at, tz)
        end = start + 1
    else:
        start, end = (epoch(t, tz) if t else None for t in (args.start, args.end))
    narrowed = any(x is not None for x in (start, end, args.above, args.below))
    if args.apply and not narrowed:
        raise SystemExit("--apply needs --at, --from/--to, --above or --below: it won't change every reading")
    storage = Storage(args.file, tz)
    try:
        sensor = None
        if args.sensor:
            sensor = next((s.id for s in storage.sensors.all() if s.kind == kind and args.sensor.lower() in (s.role.lower(), s.label.lower())), None)
            if sensor is None:
                raise SystemExit(f"no {kind} sensor called {args.sensor!r} in the file (--list shows them)")
        found = matches(storage, kind, args.reading, start, end, args.above, args.below, None if narrowed else TOP, args.res, sensor)
        if not narrowed:
            print(f"The {TOP} highest {args.reading} readings (pick one with --at to change it):")
            show(storage, found, tz)
        elif not found:
            print("Nothing matches." + (" (--at needs the reading's own time: 13:25, not 13:27)" if args.at else ""))
        else:
            values = new_values(storage, kind, args.reading, found, args.value)
            if not args.apply:
                print(f"Dry run: {len(found)} {args.reading} reading(s) would change, marked >> with their new value, among the "
                      f"{args.context} either side (add --apply to change them):")
                show(storage, around(storage, kind, args.reading, found, args.context), tz, values)
            else:
                change(storage, kind, args.reading, values)
                print(f"Changed {len(found)} {args.reading} reading(s), marked as edited by hand, and worked their days out again:")
                show(storage, around(storage, kind, args.reading, found, args.context), tz, values)
    finally:
        storage.close()
