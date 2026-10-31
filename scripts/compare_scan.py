#!/usr/bin/env python3
"""TEMPORARY: compare the air-quality scan's two versions before numpy goes: the old one (lib/analysis/scan_numpy.py, day shuffles)
and the new one (lib/analysis/scan.py, Fisher's z with an effective sample size). Deleted once they have been compared.

    python scripts/compare_scan.py --synthetic      # made-up data: how often each finds a link that isn't there, and finds one that is
    set -a; . /etc/ecobot.env; set +a               # then, with the bot's settings, the real readings:
    python scripts/compare_scan.py                  # the last 30, 90 and 365 days
    sudo docker exec ecobot python scripts/compare_scan.py   # (in Docker the settings are already there)

The real comparison reads the stored history through the bot's own sources (a few requests to fill any gap) and writes nothing.
"""

import asyncio
import math
import random
import time
from datetime import UTC, date, timedelta

from _common import parser  # noqa: E402  (also puts the repo on sys.path)

from lib.analysis import scan as new  # noqa: E402
from lib.analysis import scan_numpy as old  # noqa: E402

DAYS = 90
ORIGIN = 1_700_000_000 // 1800 * 1800
N = DAYS * 48


def series(values):
    return {ORIGIN + i * 1800: float(v) for i, v in enumerate(values)}


def unrelated(rng, kind):
    if kind == "showers":   # dry most of the time, showers most likely in the afternoon
        out = []
        for _ in range(DAYS):
            wet = rng.random() < 0.3
            out += [rng.expovariate(2) if wet and rng.random() < 0.15 + 0.25 * (24 <= s < 36) else 0.0 for s in range(48)]
        return out
    if kind == "noise":
        return [rng.gauss(0, 1) for _ in range(N)]
    if kind == "smooth":   # each day its own slow swings: smooth within the day, unrelated across
        out = []
        for _ in range(DAYS):
            waves = [(rng.gauss(0, 1), rng.uniform(4, 16), rng.uniform(0, 2 * math.pi)) for _ in range(3)]
            out += [sum(a * math.sin(2 * math.pi * s / (2 * h) + ph) for a, h, ph in waves) + rng.gauss(0, 0.2) for s in range(48)]
        return out
    out, level = [], 0.0
    for _ in range(DAYS):   # spells of days, a daily cycle and noise: what weather and air look like
        level = 0.8 * level + rng.gauss(0, 1)
        out += [level + math.sin(2 * math.pi * s / 48) + rng.gauss(0, 0.5) for s in range(48)]
    return out


def planted(rng, kind):
    """(air, weather, direction, speed) with one real link of this kind."""
    spells = unrelated(rng, "spells")
    if kind == "within the day, 3 h later":
        wind = [abs(v) * 4 for v in spells]
        later = wind[-6:] + wind[:-6]
        return {"pm2_5": series([20 - 0.3 * w + rng.gauss(0, 3) for w in later])}, {"wind": series(wind)}, None, None
    if kind == "day to day":
        days = [0.0]
        for _ in range(DAYS - 1):
            days.append(0.7 * days[-1] + rng.gauss(0, 1))
        temp = [d + rng.gauss(0, 1.5) for d in days for _ in range(48)]
        return {"pm2_5": series([10 + 0.8 * d + rng.gauss(0, 2) for d in days for _ in range(48)])}, {"temperature": series(temp)}, None, None
    direction = [rng.choice((45.0, 135.0, 225.0, 315.0)) for _ in range(DAYS) for _ in range(48)]
    air = [10 + 2.5 * (d == 45.0) + s * 0.5 + rng.gauss(0, 2) for d, s in zip(direction, spells)]
    return {"pm2_5": series(air)}, {"wind": series(spells)}, series(direction), series([15.0] * N)


def synthetic(runs: int):
    print(f"Made-up data, {runs} runs each, 90 days. Unrelated readings (any link found is false; about 10% of runs is the target):")
    for kind in ("noise", "spells", "smooth", "showers"):
        hits = {"old": 0, "new": 0}
        for seed in range(runs):
            rng = random.Random(seed)
            air = {f"a{i}": series(unrelated(rng, kind)) for i in range(3)}
            weather = {f"w{i}": series(unrelated(rng, kind)) for i in range(6)}
            for name, mod in (("old", old), ("new", new)):
                result = mod.scan(air, weather, None, None, ORIGIN, DAYS, UTC)
                hits[name] += any(t["survives"] for t in result["tests"])
        print(f"  {kind:8s} old {hits['old']:2d}/{runs}  new {hits['new']:2d}/{runs}")
    hits = {"old": 0, "new": 0}
    for seed in range(runs):   # wind that holds for days, beside air that drifts in spells: no link between them
        rng = random.Random(500 + seed)
        air = {f"a{i}": series(unrelated(rng, "spells")) for i in range(5)}
        days, d = [], rng.randrange(8)
        for _ in range(DAYS):
            d = d if rng.random() < 0.7 else rng.randrange(8)
            days.append(d * 45.0 + rng.uniform(-15, 15))
        args = (air, {"w": series(unrelated(rng, "spells"))}, series([x for x in days for _ in range(48)]), series([15.0] * N))
        for name, mod in (("old", old), ("new", new)):
            hits[name] += any(t["survives"] for t in mod.scan(*args, ORIGIN, DAYS, UTC)["directions"])
    print(f"  {'wind direction':8s} old {hits['old']:2d}/{runs}  new {hits['new']:2d}/{runs}")
    print("One planted link (found is right):")
    for kind in ("within the day, 3 h later", "day to day", "wind direction"):
        hits = {"old": 0, "new": 0}
        for seed in range(runs):
            args = planted(random.Random(100 + seed), kind)
            for name, mod in (("old", old), ("new", new)):
                result = mod.scan(*args, ORIGIN, DAYS, UTC)
                found = any(t["survives"] for t in [*result["tests"], *result["directions"]])
                hits[name] += found
        print(f"  {kind:26s} old {hits['old']:2d}/{runs}  new {hits['new']:2d}/{runs}")


async def real(periods: list[int]):
    from ecobot import start_sources
    from lib import compose
    from lib.compose import Composer
    from lib.config import Config
    cfg = Config.from_env()
    sources, cfg = await start_sources(cfg)
    by = {s.name: s for s in sources}
    if "Ecowitt" not in by or "AirGradient" not in by:
        raise SystemExit("needs both the weather station and the air sensor")
    captured = []
    real_scan = compose.scan.scan
    compose.scan.scan = lambda *a, **k: (captured.append(a), real_scan(*a, **k))[1]
    comp = Composer(by["Ecowitt"], by["AirGradient"])
    try:
        for days in periods:
            captured.clear()
            start = date.today() - timedelta(days=days)
            await comp.air_scan({"start_date": f"{start} 00:00:00"})
            if not captured:
                print(f"\nLast {days} days: no scan was run (too little data)")
                continue
            args = captured[-1]
            out = {}
            for name, mod in (("old", old), ("new", new)):
                started = time.perf_counter()
                out[name] = (mod.scan(*args), time.perf_counter() - started)
            report(days, out, args)
            self_check(args)
    finally:
        compose.scan.scan = real_scan
        for s in sources:
            await s.close()


def report(days: int, out: dict, args):
    (o, to), (n, tn) = out["old"], out["new"]
    print(f"\nLast {days} days ({args[5]} days scanned): old {to:.1f} s, new {tn:.2f} s")
    if "note" in o or "note" in n:
        print("  ", o.get("note"), "|", n.get("note"))
        return
    key = lambda t: (t["air"], t.get("weather", "direction"))
    old_by, new_by = {key(t): t for t in [*o["tests"], *o["directions"]]}, {key(t): t for t in [*n["tests"], *n["directions"]]}
    agree = sum(old_by[k]["survives"] == new_by[k]["survives"] for k in old_by if k in new_by)
    print(f"  pairs: {len(old_by)}; found by old {sum(t['survives'] for t in old_by.values())}, by new "
          f"{sum(t['survives'] for t in new_by.values())}; the same call on {agree} of {len(old_by)}")
    for k in sorted(old_by, key=lambda k: min(old_by[k]["p"], new_by.get(k, old_by[k])["p"])):
        a, b = old_by[k], new_by.get(k)
        if b is None or not (a["survives"] or b["survives"]) and min(a["p"], b["p"]) > 0.05:
            continue
        r = lambda t: (f"r {t['r']:+.2f}" if t.get("r") is not None else "") + (f" daily {t['r_daily']:+.2f}" if t.get("r_daily") is not None else "")
        mark = "  " if a["survives"] == b["survives"] else "≠ "
        print(f"  {mark}{k[0]} ~ {k[1]}: old p {a['p']:.3f} {'FOUND' if a['survives'] else '-'} | new p {b['p']:.4f} "
              f"{'FOUND' if b['survives'] else '-'}  {r(b)}")


def shifted(series_: dict[int, float], origin: int, days: int, by_days: int) -> dict[int, float]:
    """The readings moved `by_days` later, the end wrapping round to the start: the same readings, the same daily cycle and
    persistence, but no longer lined up with the weather they went with."""
    span = days * 86400
    return {origin + (t - origin + by_days * 86400) % span: v for t, v in series_.items()}


def self_check(args):
    """Both scans with the air moved by whole weeks against the weather: any link they still find is a false one, on these very
    readings. A sound scan finds about none (a few at most over all the shifts)."""
    air, weather, direction, speed, origin, days, tz = args
    shifts = [k for k in (7, 14, 21, 28) if k < days - 3]
    found, total = {"old": [], "new": []}, 0
    for k in shifts:
        moved = {name: shifted(v, origin, days, k) for name, v in air.items()}
        for name, mod in (("old", old), ("new", new)):
            result = mod.scan(moved, weather, direction, speed, origin, days, tz)
            if "note" in result:
                continue
            tests = [*result["tests"], *result["directions"]]
            total += len(tests) if name == "new" else 0
            found[name] += [(k, t) for t in tests if t["survives"]]
    print(f"  self-check, the air moved by {', '.join(map(str, shifts))} days (no real link left), {total} pairs tested: "
          f"false finds old {len(found['old'])}, new {len(found['new'])}")
    for k, t in sorted(found["new"], key=lambda f: f[1]["p"])[:8]:
        if "weather" in t:
            view = "within the day" if t["p_within"] <= (t["p_daily"] if t["p_daily"] is not None else 1) else "day to day"
            print(f"    new, moved {k} days: {t['air']} ~ {t['weather']}  p {t['p']:.4f} ({view}: within p {t['p_within']:.4f} r "
                  f"{t['r'] if t['r'] is not None else float('nan'):+.2f}, daily p {t['p_daily'] if t['p_daily'] is not None else float('nan'):.4f} "
                  f"r {t['r_daily'] if t['r_daily'] is not None else float('nan'):+.2f})")
        else:
            print(f"    new, moved {k} days: {t['air']} ~ wind direction  p {t['p']:.4f}")


def main():
    p = parser(__doc__)
    p.add_argument("--synthetic", action="store_true", help="made-up data instead of the real readings")
    p.add_argument("--runs", type=int, default=20)
    p.add_argument("--days", type=int, nargs="*", default=[30, 90, 365])
    a = p.parse_args()
    if a.synthetic:
        synthetic(a.runs)
    else:
        asyncio.run(real(a.days))


if __name__ == "__main__":
    main()
