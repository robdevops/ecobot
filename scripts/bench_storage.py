"""How long the bot's typical requests take, and how many SQL statements and thread hops they make, on a copy of its caches: the
yardstick for the storage redesign (docs/storage-plan.md, step 0). Nothing is asked of Ecowitt or AirGradient (a request the cache
cannot answer is counted instead), and the caches themselves are only read: the copy is made with SQLite's backup, so the bot can keep
running. Run it where the bot runs, with its settings:

    sudo docker compose exec ecobot python scripts/bench_storage.py
    python scripts/bench_storage.py [--data /var/lib/private/ecobot] [--repeat 3]

and paste the table it prints.
"""

import asyncio
import dataclasses
import logging
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from _common import parser  # noqa: E402  (also puts the repo on sys.path)

from lib import counts  # noqa: E402
from lib.config import Config, data_dir, local_tz  # noqa: E402
from lib.tools import Turn  # noqa: E402

FILES = ("ecowitt_cache.sqlite", "ecowitt_live.sqlite", "ecowitt_minutes.sqlite", "airgradient_cache.sqlite")


class Offline:
    """Ecowitt's API with no network: it has nothing to give, and counts what it was asked (what the request would have fetched)."""
    empty_is_final = False   # (so nothing is recorded as fetched)
    fast_live = minute_lines = False

    def __init__(self):
        self.asked = 0

    async def history(self, *args, **kwargs) -> dict:
        self.asked += 1
        return {}

    async def realtime(self, *args, **kwargs) -> dict:
        self.asked += 1
        return {}

    async def devices(self) -> list:
        return []

    async def close(self):
        pass


def copy(source: Path, target: Path):
    """A consistent copy of a SQLite file, even while the bot writes to it."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def settings(folder: Path) -> Config:
    """The bot's settings with its files in `folder`, and nothing that would reach the network (Home Assistant, the sensor itself)."""
    os.environ["DATA_DIR"] = str(folder)
    for name in ("TELEGRAM_BOT_TOKEN", "ECOWITT_API_KEY", "ECOWITT_APP_KEY"):   # (never used: nothing is asked of them)
        os.environ.setdefault(name, "bench")
    cfg = Config.from_env()
    location = None
    air = folder / "airgradient_cache.sqlite"
    if air.exists():
        db = sqlite3.connect(air)
        found = db.execute("SELECT loc, COUNT(*) FROM readings GROUP BY loc ORDER BY 2 DESC LIMIT 1").fetchone()
        db.close()
        location = found[0] if found else None
    return dataclasses.replace(cfg, homeassistant_url="", homeassistant_token="", ecowitt_source="cloud", airgradient_local_url="",
                               airgradient_location=location or cfg.airgradient_location,
                               airgradient_token=cfg.airgradient_token or ("bench" if location and location != "local" else ""))


async def measured(work, repeat: int, before=None) -> tuple[float, counts.Count]:
    """(the median time in ms, the count of the last run) of `work()` run `repeat` times, each in a task of its own."""
    times, count = [], counts.Count()

    async def once():
        nonlocal count
        count = counts.start()
        await work()
    for _ in range(repeat):
        if before:
            before()
        started = time.perf_counter()
        await asyncio.create_task(once())
        times.append((time.perf_counter() - started) * 1000)
    return statistics.median(times), count


def period(days: float, now: datetime) -> dict:
    return {"start_date": f"{now - timedelta(days=days):%Y-%m-%d %H:%M:%S}", "end_date": f"{now:%Y-%m-%d %H:%M:%S}"}


async def bench(folder: Path, repeat: int, tz) -> list[tuple[str, float, counts.Count, int]]:
    from lib.airgradient import AirGradient
    from lib.ecowitt import Ecowitt
    from lib.ecowitt.archive import Archive
    cfg = settings(folder)
    rows: list[tuple[str, float, counts.Count, int]] = []
    if (folder / "ecowitt_cache.sqlite").exists():
        eco = Ecowitt(cfg)
        eco.api, eco.tz = Offline(), tz
        held = eco.cache._query("SELECT s.mac, COUNT(*) FROM points p JOIN series s ON s.id = p.series GROUP BY s.mac ORDER BY 2 DESC")
        eco.mac = held[0][0] if held else ""
        print(f"Ecowitt {eco.mac}: {held[0][1] if held else 0:,} readings, {Archive(eco).held()}")
        now = eco.now()
        history, days, link = eco.tools[1].handler, eco.tools[2].handler, eco.tools[3].handler
        chart = lambda field, d, groups="outdoor": lambda: history({"groups": groups, "chart": True, "chart_field": field, **period(d, now)}, Turn())
        work = [(f"Temperature chart, {label}", chart("temperature", d)) for label, d in (("1D", 1), ("7D", 7), ("1M", 30), ("3M", 90), ("1Y", 365))]
        work += [("Rain chart, 3M", chart("rain", 90, "rainfall")),
                 ("History of outdoor and indoor, 7 days, no chart", lambda: history({"groups": "outdoor,indoor", **period(7, now)}, Turn())),
                 ("weather_days: days over 30°C, 4 years", lambda: days({"condition": "temp_max > 30", "count_only": True,
                                                                         "start_date": f"{now - timedelta(days=1460):%Y-%m-%d}",
                                                                         "end_date": f"{now:%Y-%m-%d}"}, Turn())),
                 ("weather_days: rain per month, a year", lambda: days({"stat": "sum", "of": "rain", "group_by": "month",
                                                                       "start_date": f"{now - timedelta(days=365):%Y-%m-%d}",
                                                                       "end_date": f"{now:%Y-%m-%d}"}, Turn())),
                 ("weather_link: pressure and rain, 90 days, chart", lambda: link({"driver": "pressure", "chart": True}, Turn())),
                 ("The alerts' last 3 hours", lambda: eco.recent(3))]
        for label, run in work:
            eco.api.asked = 0
            ms, count = await measured(run, repeat)
            rows.append((label, ms, count, eco.api.asked // repeat))
        for label, before in (("/dashboard, first time in a day", eco._days_memo.clear), ("/dashboard, Refresh", None)):
            eco.api.asked = 0
            ms, count = await measured(eco.dashboard, repeat, before)
            rows.append((label, ms, count, eco.api.asked // repeat))
        await eco.close()
    if (folder / "airgradient_cache.sqlite").exists() and cfg.airgradient_location:
        asked = [0]

        def cloud(request):   # what the cache lacks is counted, and there is none of it
            asked[0] += 1
            return httpx.Response(200, json=[]) if request.url.path.endswith("/past") else httpx.Response(503)
        air = AirGradient(cfg, transport=httpx.MockTransport(cloud))
        air.tz = tz
        n = air.store.db.execute("SELECT COUNT(*) FROM readings WHERE loc=?", (air.loc,)).fetchone()[0]
        print(f"AirGradient {air.loc}: {n:,} readings")
        now = datetime.now(tz).replace(tzinfo=None)
        work = [(f"Air chart (PM2.5), {label}", lambda d=d: air.handle({"chart": True, "metrics": ["pm2_5"], **period(d, now)}, Turn()))
                for label, d in (("1D", 1), ("7D", 7), ("1M", 30))]
        work.append(("air_days: days PM2.5 over 25, 4 years", lambda: air.tools[1].handler(
            {"condition": "pm2_5_max > 25", "count_only": True, "start_date": f"{now - timedelta(days=1460):%Y-%m-%d}",
             "end_date": f"{now:%Y-%m-%d}"}, Turn())))
        for label, run in work:
            asked[0] = 0
            ms, count = await measured(run, repeat)
            rows.append((label, ms, count, asked[0] // repeat))
        await air.close()
    return rows


def main():
    p = parser(__doc__)
    p.add_argument("--data", type=Path, default=data_dir(), help="the bot's data folder (default: DATA_DIR, as the bot finds it)")
    p.add_argument("--repeat", type=int, default=3, help="runs of each request; the time is their median (default 3)")
    p.add_argument("--tz", help="the station's time zone, e.g. Australia/Melbourne (default: TZ, else the computer's); it sets the days")
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    found = [name for name in FILES if (args.data / name).exists()]
    if not found:
        sys.exit(f"No cache files in {args.data}: give the bot's data folder with --data")
    tz = ZoneInfo(args.tz) if args.tz else local_tz()
    counts.install()
    with tempfile.TemporaryDirectory(prefix="ecobot-bench-") as tmp:
        folder = Path(tmp)
        for name in found:
            copy(args.data / name, folder / name)
        print(f"Caches in {args.data}: " + ", ".join(f"{name} {(args.data / name).stat().st_size / 1e6:.1f} MB" for name in found))
        print(f"Time zone {tz}, each request run {args.repeat} times (the median shown)\n")
        rows = asyncio.run(bench(folder, args.repeat, tz))
    print("| Request | SQL statements | Thread hops | Asked of the network | Time |")
    print("|---|---|---|---|---|")
    for label, ms, count, asked in rows:
        print(f"| {label} | {count.sql} | {count.hops} | {asked} | {ms:,.0f} ms |")
    print("\nAsked of the network: what the copy could not answer. For a chart that is the newest hour or so, still settling, which the "
          "running bot holds in memory; here it is asked of nothing and answered at once.")


if __name__ == "__main__":
    main()
