"""How long the bot's typical requests take on a copy of its file, ecobot.sqlite: the yardstick for the storage redesign
(docs/storage-plan.md, step 0). Nothing is asked of Ecowitt or AirGradient (a request the copy cannot answer is counted instead), and the
bot's own file is only read: the copy is made with SQLite's backup, so the bot can keep running. Run it where the bot runs, with its
settings:

    sudo docker compose exec ecobot python scripts/bench_storage.py
    python scripts/bench_storage.py [--data /var/lib/private/ecobot] [--repeat 3]

and paste the table it prints.
"""

import asyncio
import dataclasses
import json
import logging
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from _common import parser  # noqa: E402  (also puts the repo on sys.path)

from lib.config import Config, data_dir, local_tz  # noqa: E402

FILES = ("ecobot.sqlite",)


class Offline:
    """Ecowitt's API with no network: it has nothing to give, and counts what it was asked (what the request would have fetched)."""
    empty_is_final = False   # (so nothing is recorded as fetched)

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
    for name in ("BOT_TELEGRAM_TOKEN", "MCP_ECOWITT_API_KEY", "MCP_ECOWITT_APP_KEY"):   # (never used: nothing is asked of them)
        os.environ.setdefault(name, "bench")
    cfg = Config.from_env(bot=False)   # (ecobot-mcp's settings: the sources)
    location = None
    if cfg.storage_path.exists():   # the air sensor the bot's file holds
        db = sqlite3.connect(f"file:{cfg.storage_path}?mode=ro", uri=True)
        found = db.execute("SELECT device FROM sensors WHERE kind = 'air' ORDER BY id LIMIT 1").fetchone()
        db.close()
        location = found[0] if found else None
    return dataclasses.replace(cfg, homeassistant_url="", homeassistant_token="", ecowitt_source="cloud", airgradient_local_url="",
                               airgradient_location=location or cfg.airgradient_location,
                               airgradient_token=cfg.airgradient_token or ("bench" if location and location != "local" else ""))


async def measured(work, repeat: int, asked, before=None) -> tuple[float, int]:
    """(the median time in ms, what the last run asked of the network: asked() is the total so far) of `work()` run `repeat` times, each
    in a task of its own."""
    times, ask = [], 0
    for _ in range(repeat):
        if before:
            before()
        started, first = time.perf_counter(), asked()
        await asyncio.create_task(work())
        times.append((time.perf_counter() - started) * 1000)
        ask = asked() - first
    return statistics.median(times), ask


def stations(storage) -> list[tuple[str, int, int]]:
    """(MAC, rows of readings, minutes) of each station in the file, the one with the most first."""
    from lib.storage import KINDS

    def count(r):
        found: dict[str, list[int]] = {}
        for sensor in r.sensors():
            if sensor.kind in KINDS and sensor.kind != "air":
                rows, minutes = r.db.execute(f"SELECT COUNT(*), SUM(res = '1min') FROM {sensor.kind} WHERE sensor = ?", (sensor.id,)).fetchone()
                got = found.setdefault(sensor.device, [0, 0])
                got[0], got[1] = got[0] + rows - (minutes or 0), got[1] + (minutes or 0)
        return sorted(((mac, n, m) for mac, (n, m) in found.items()), key=lambda x: -x[1])
    return storage.read_now(count)


def service(**sources):
    """The service's tools over these sources (lib/service), as the bot and the model call them."""
    from lib.service import Service
    from lib.service.context import Context
    return Service.of(Context(**sources, tz=next(iter(sources.values())).tz))


def asking(tools, name: str, args: dict):
    """A request: the service's `name` called with `args`, as the bot asks for its charts (as drawn)."""
    return lambda: tools[name].call(args, {"chart_points": "drawn"})


FOUR_YEARS = "last 4 years"


async def bench(folder: Path, repeat: int, tz) -> list[tuple[str, float, int]]:
    from lib.airgradient import AirGradient
    from lib.ecowitt import Ecowitt
    from lib.ecowitt.archive import Archive
    cfg = settings(folder)
    rows: list[tuple[str, float, int]] = []
    if cfg.storage_path.exists():
        started = time.monotonic()
        eco = Ecowitt(cfg)
        opened = time.monotonic() - started
        eco.api, eco.tz = Offline(), tz
        held = stations(eco.storage)
        eco.mac = held[0][0] if held else ""
        eco.has_minutes = any(n for _, _, n in held)   # Home Assistant's minutes, which the bot draws a chart of a few hours from
        print(f"Ecowitt {eco.mac}: {held[0][1] if held else 0:,} rows of readings, {held[0][2] if held else 0:,} minutes, {Archive(eco).held()} "
              f"(opened in {opened:.1f} s)")
        tools = service(eco=eco)
        chart = lambda reading, period: asking(tools, "summary", {"readings": [reading], "period": period, "chart": True})
        work = [(f"Temperature chart, {label}", chart("outdoor.temperature", period))
                for label, period in (("3 hours", "last 3 hours"), ("1D", "1d"), ("7D", "7d"), ("1M", "1m"), ("3M", "3m"), ("1Y", "1y"))]
        work += [("Rain chart, 3M", chart("rain", "3m")),
                 ("Summary of outdoor and indoor, 7 days, no chart", asking(tools, "summary", {"period": "7d", "chart": False})),
                 ("days: days over 30°C, 4 years", asking(tools, "days", {"where": [{"field": "temp_max", "op": ">", "value": 30}],
                                                                           "count_only": True, "period": FOUR_YEARS})),
                 ("days: rain per month, a year", asking(tools, "days", {"stat": "sum", "of": "rain", "group_by": "month", "period": "1y"})),
                 ("relate: pressure and rain, 90 days, chart", asking(tools, "relate", {"reading": "pressure", "chart": True})),
                 ("The alerts' last 3 hours", lambda: eco.recent(3))]
        for label, run in work:
            rows.append((label, *await measured(run, repeat, lambda: eco.api.asked)))
        from lib.gauge_sheet import call as gauges_call, sheet
        from lib.service.context import Context
        from lib.service.current import CurrentTool
        current = CurrentTool(Context(eco=eco)).tool()   # (the bot's gauges: current with context, laid out bot-side)

        async def dashboard():
            return sheet(json.loads(await current.call(gauges_call(), None)))
        rows.append(("/dashboard", *await measured(dashboard, repeat, lambda: eco.api.asked)))
        await eco.close()
    if cfg.airgradient_location and cfg.storage_path.exists():
        asked = [0]

        def cloud(request):   # what the cache lacks is counted, and there is none of it
            asked[0] += 1
            return httpx.Response(200, json=[]) if request.url.path.endswith("/past") else httpx.Response(503)
        air = AirGradient(cfg, transport=httpx.MockTransport(cloud))
        air.tz = tz
        n = (await air.store.lacking())["all"]
        print(f"AirGradient {air.loc}: {n:,} readings")
        tools = service(air=air)
        work = [(f"Air chart (PM2.5), {label}", asking(tools, "summary", {"readings": ["pm2.5"], "period": period, "chart": True}))
                for label, period in (("1D", "1d"), ("7D", "7d"), ("1M", "1m"))]
        work.append(("days: days PM2.5 over 25, 4 years", asking(tools, "days", {"where": [{"field": "pm2_5_max", "op": ">", "value": 25}],
                                                                                 "count_only": True, "period": FOUR_YEARS})))
        for label, run in work:
            rows.append((label, *await measured(run, repeat, lambda: asked[0])))
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
        sys.exit(f"No history files in {args.data}: give the bot's data folder with --data")
    tz = ZoneInfo(args.tz) if args.tz else local_tz()
    with tempfile.TemporaryDirectory(prefix="ecobot-bench-") as tmp:
        folder = Path(tmp)
        for name in found:
            copy(args.data / name, folder / name)
        print(f"Files in {args.data}: " + ", ".join(f"{name} {(args.data / name).stat().st_size / 1e6:.1f} MB" for name in found))
        print(f"Time zone {tz}, each request run {args.repeat} times (the median shown)\n")
        rows = asyncio.run(bench(folder, args.repeat, tz))
    print("| Request | Asked of the network | Time |")
    print("|---|---|---|")
    for label, ms, asked in rows:
        print(f"| {label} | {asked} | {ms:,.0f} ms |")
    print("\nAsked of the network: what the copy could not answer. For a chart that is the newest hour or so, still settling, which the "
          "running bot has fetched lately; here it is asked of nothing and answered at once. The 3-hour chart is drawn from the minutes where "
          "the file holds them (Home Assistant's), else from the 5-minute readings.")


if __name__ == "__main__":
    main()
