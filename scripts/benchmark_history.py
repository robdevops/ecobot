"""Is detailed (30-minute) data worth it for a long period? Answers the same question twice from
the local cache, once with the 30-minute data and once with the daily fallback, and compares time,
size, chart cost and the answers themselves. No network, no API keys. Run it on the server:

    python scripts/benchmark_history.py [--days 120] [--groups outdoor,indoor] [--repeat 3]

--days must be over 93 (shorter periods always use detailed data). A period that isn't fully cached
is reported, not fetched.
"""

import asyncio
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from _common import ROOT, parser  # noqa: E402  (also puts the repo on sys.path)

from lib.charts import CHART_REQUESTS, render  # noqa: E402
from lib.ecowitt.api import EcowittError, UNITS  # noqa: E402
from lib.ecowitt.history import DETAILED_DAYS, Fetcher, HistoryQuery  # noqa: E402
from lib.ecowitt.store import HistoryCache, HotStore  # noqa: E402


class CacheOnly:
    """Stands in for the API: anything not in the cache is counted, not fetched."""

    def __init__(self):
        self.wanted = 0

    async def history(self, *args, **kwargs):
        self.wanted += 1
        raise EcowittError("not cached")


class Daily(HistoryQuery):
    async def _cached_locally(self) -> bool:  # pretend the 30-minute data isn't there
        return False


async def answer(cls, cache, mac, groups, tz, start, end):
    api = CacheOnly()
    fetcher = Fetcher(api, cache, HotStore(), mac, groups, tz)
    holder = []
    token = CHART_REQUESTS.set(holder)
    args = {"groups": ",".join(groups), "chart": True, "start_date": f"{start:%Y-%m-%d} 00:00:00",
            "end_date": f"{end:%Y-%m-%d %H:%M:%S}"}
    t0 = time.perf_counter()
    try:
        out = await cls(fetcher, args).run()
    finally:
        CHART_REQUESTS.reset(token)
    took = time.perf_counter() - t0
    t1 = time.perf_counter()
    png = render(holder[0], tz) if holder else b""
    return {"seconds": took, "chars": len(out), "result": json.loads(out) if out.startswith("{") else {},
            "missing_ranges": api.wanted, "chart_seconds": time.perf_counter() - t1, "chart_kb": len(png) / 1024,
            "chart_points": max((len(s["x"]) for s in holder[0]["series"]), default=0) if holder else 0}


def compare(a: dict, b: dict):
    """How the two answers differ for the first temperature series."""
    key = next((k for k in a["series"] if k.endswith(".temperature")), None)
    if not key or key not in b["series"]:
        return
    sa, sb = a["series"][key], b["series"][key]
    print(f"\nAnswers for {key}:")
    for want in ("low", "high"):
        print(f"  {want:4} detailed {sa[want]} {sa[f'{want}_when']} {sa[f'{want}_date']}"
              f"   |   daily {sb[want]} {sb[f'{want}_when']} {sb[f'{want}_date'] or '(spans two days)'}")
    ma, mb = sa.get("monthly", {}), sb.get("monthly", {})
    dated = sum(1 for m in ma.values() if m.get("low_date"))
    differ = sum(1 for k in ma if k in mb and any(str(ma[k].get(w)) != str(mb[k].get(w)) for w in ("low", "high")))
    print(f"  monthly figures: {len(ma)} months; detailed gives a date for {dated}, daily for "
          f"{sum(1 for m in mb.values() if m.get('low_date'))}; {differ} month(s) have a different low or high")


async def main():
    ap = parser(__doc__)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--groups", default="outdoor,indoor")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--cache", type=Path, default=ROOT / "ecowitt_cache.sqlite")
    args = ap.parse_args()
    if args.days <= DETAILED_DAYS:
        sys.exit(f"--days must be over {DETAILED_DAYS}: shorter periods always use detailed data")
    tz = ZoneInfo(os.getenv("TZ") or "Australia/Melbourne")
    macs = [m for (m,) in sqlite3.connect(f"file:{args.cache}?mode=ro", uri=True).execute("SELECT DISTINCT mac FROM coverage")]
    if not macs:
        sys.exit(f"{args.cache} has no cached history yet")
    cache = HistoryCache(args.cache, UNITS)
    today = datetime.now(tz).replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0)
    end = today - timedelta(seconds=1)  # through yesterday: today's readings are still settling
    start = today - timedelta(days=args.days)
    groups = args.groups.split(",")
    print(f"{args.days} days of {args.groups}, {start:%Y-%m-%d} to {end:%Y-%m-%d} (through yesterday), best of {args.repeat}, cache only\n")
    results = {}
    for name, cls in (("detailed (30-min)", HistoryQuery), ("daily fallback", Daily)):
        runs = [await answer(cls, cache, macs[0], groups, tz, start, end) for _ in range(args.repeat)]
        results[name] = min(runs, key=lambda r: r["seconds"])
    print(f"{'':20}{'answer s':>10}{'chart s':>9}{'chart pts':>10}{'chart KB':>9}{'answer chars':>13}{'not cached':>11}")
    for name, r in results.items():
        print(f"{name:20}{r['seconds']:>10.2f}{r['chart_seconds']:>9.2f}{r['chart_points']:>10}{r['chart_kb']:>9.0f}"
              f"{r['chars']:>13}{r['missing_ranges']:>11}")
    d, f = results["detailed (30-min)"], results["daily fallback"]
    if d["missing_ranges"]:
        print(f"\nNote: {d['missing_ranges']} range(s) weren't cached, so the detailed figures are incomplete; "
              "wait for the archive to finish (scripts/cache_status.py) and run again.")
    if d["result"].get("series") and f["result"].get("series"):
        compare(d["result"], f["result"])
    print(f"\nRough LLM cost of the answer: detailed ~{d['chars'] // 4} tokens, daily ~{f['chars'] // 4} tokens.")


if __name__ == "__main__":
    asyncio.run(main())
