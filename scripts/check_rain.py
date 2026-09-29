"""Can the rain totals the bot reports be trusted at each resolution? Compares, from the cache alone
(no network, no keys), each day's rain total worked out three ways for the days where the cache
holds 5-minute data (the reference: the counter's last reading of the day):

  - from the 30-minute data   (what weather_days uses for days before the 5-minute archive began)
  - from the daily buckets    (what it uses for older days; a 10am-10am window, so some difference is expected)

and which rain fields Ecowitt actually gives at each resolution. Run it on the server:

    python scripts/check_rain.py [--days 88] [--cache ecowitt_cache.sqlite]

What to look for: "lower" days at 30-minute resolution mean the totals are averages, not the day's total,
so days just over the 1 mm line can be missed.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.ecowitt import days as day_tool  # noqa: E402
from lib.ecowitt.api import UNITS  # noqa: E402
from lib.ecowitt.store import HistoryCache, subtract  # noqa: E402

TOLERANCE = 0.05  # mm: equal within this
RAINY = 1.0       # mm: the bot's line between a trace and a rainy day


def fields_held(cache: HistoryCache, mac: str) -> dict[str, list[str]]:
    """Which rainfall fields are stored at each resolution."""
    with cache._lock:
        rows = cache.db.execute("SELECT cycle, field FROM fields WHERE mac=? AND grp='rainfall' ORDER BY cycle, field", (mac,)).fetchall()
    out: dict[str, list[str]] = {}
    for cycle, field in rows:
        out.setdefault(cycle, []).append(field)
    return out


def totals(cache: HistoryCache, mac: str, tz, cycle: str, days: list) -> dict:
    """{day: rain total} the way weather_days works it out at this resolution."""
    if not days:
        return {}
    lo, hi = day_tool._bounds(min(days), tz)[0], day_tool._bounds(max(days), tz)[1]
    if cycle == "1day":
        lo, hi = lo - 86400, hi + 86400
    store = day_tool._load(cache, mac, cycle, ["rain"], lo, hi)
    return day_tool._per_day(store, "rain", set(days), tz)


def compare_rain(cache: HistoryCache, mac: str, tz, today, days_back: int = 88) -> dict:
    cover = cache.coverage(mac, "5min", "rainfall")
    days = [today - timedelta(days=n) for n in range(days_back, 0, -1)]
    days = [d for d in days if not subtract(day_tool._bounds(d, tz), cover)]
    reference = totals(cache, mac, tz, "5min", days)
    result = {"days": len(days), "fields": fields_held(cache, mac), "against": {}}
    for cycle in ("30min", "1day"):
        other = totals(cache, mac, tz, cycle, days)
        pairs = [(reference.get(d, 0.0), other.get(d, 0.0), d) for d in days]
        rainy = [p for p in pairs if p[0] > 0]
        lower = [p for p in rainy if p[1] < p[0] - TOLERANCE]
        result["against"][cycle] = {
            "rainy_days": len(rainy),
            "same": sum(1 for r, o, _ in rainy if abs(r - o) <= TOLERANCE),
            "lower": len(lower),
            "higher": sum(1 for r, o, _ in rainy if o > r + TOLERANCE),
            "no_data": sum(1 for d in days if d not in other),
            "missed_rainy": sum(1 for r, o, _ in pairs if r >= RAINY > o),      # a rainy day the bot would not list
            "invented_rainy": sum(1 for r, o, _ in pairs if o >= RAINY > r),    # a dry-ish day it would call rainy
            "median_ratio": sorted(o / r for r, o, _ in rainy if r)[len(rainy) // 2] if rainy else None,
            "worst": sorted(lower, key=lambda p: p[1] - p[0])[:3],
        }
    return result


def report(res: dict):
    print(f"Compared {res['days']} day(s) that have 5-minute rain data (the reference)\n")
    print("Rain fields stored per resolution:")
    for cycle, names in res["fields"].items():
        print(f"  {cycle:6} {', '.join(names)}")
    print("\nDay totals against the 5-minute reference (days with rain):")
    for cycle, r in res["against"].items():
        label = {"30min": "30-minute data", "1day": "daily buckets (10am-10am)"}[cycle]
        ratio = f"{r['median_ratio']:.2f}" if r["median_ratio"] is not None else "n/a"
        print(f"  {label}: {r['rainy_days']} rainy day(s): {r['same']} the same, {r['lower']} lower, {r['higher']} higher, "
              f"{r['no_data']} without data; median ratio {ratio}")
        print(f"      a day of 1 mm or more the bot would miss: {r['missed_rainy']};  "
              f"one it would wrongly list: {r['invented_rainy']}")
        for ref, got, day in r["worst"]:
            print(f"      e.g. {day}: 5-minute {ref:.1f} mm, here {got:.1f} mm")
    print()
    thirty, daily = res["against"]["30min"], res["against"]["1day"]
    if thirty["rainy_days"] and thirty["lower"] == 0 and thirty["higher"] == 0:
        print("30-minute rain totals match the 5-minute ones: no correction needed there.")
    elif thirty["lower"]:
        print(f"30-minute totals are LOWER than the real total on {thirty['lower']} of {thirty['rainy_days']} rainy days "
              "(they look like averages): the bot's 'exact' label is too generous for rain on those days.")
    if daily["rainy_days"] and daily["median_ratio"] is not None:
        print(f"Daily buckets read about {daily['median_ratio']:.0%} of the 5-minute total on a typical rainy day "
              "(they also cover a different 24 hours, so some difference is normal).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=88)
    ap.add_argument("--cache", type=Path, default=ROOT / "ecowitt_cache.sqlite")
    args = ap.parse_args()
    tz = ZoneInfo(os.getenv("TZ") or "Australia/Melbourne")
    macs = [m for (m,) in sqlite3.connect(f"file:{args.cache}?mode=ro", uri=True).execute("SELECT DISTINCT mac FROM coverage")]
    if not macs:
        sys.exit(f"{args.cache} has no cached history")
    res = compare_rain(HistoryCache(args.cache, UNITS), macs[0], tz, datetime.now(tz).date(), args.days)
    if not res["days"]:
        sys.exit("No days have 5-minute rain data in the cache yet")
    report(res)


if __name__ == "__main__":
    main()
