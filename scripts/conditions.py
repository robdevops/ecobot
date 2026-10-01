#!/usr/bin/env python3
"""Fetch the pollen / thunderstorm asthma page and the forecast once, the way the bot does, and print what the report would show.
Use it to check the real sites before switching POLLEN=on / FORECAST=on.

    python scripts/conditions.py --lat -37.8 --lon 144.9 [--district Central] [--raw]

--raw also prints what was parsed from the pollen page. The sync hours (6 am - 6 pm) don't apply: it always fetches.
"""

import argparse
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config import _tz  # noqa: E402
from lib.forecast import Forecast  # noqa: E402
from lib.pollen import Pollen  # noqa: E402


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--district", default="Central")
    ap.add_argument("--raw", action="store_true")
    args = ap.parse_args()
    cfg = SimpleNamespace(tz=_tz(), pollen_district=args.district, forecast_lat=args.lat, forecast_lon=args.lon)

    pollen = Pollen(cfg)
    try:
        await pollen.warm(True)
        print("Pollen & asthma")
        print("\n".join(f"• {line}" for line in pollen.lines()) or "• (no levels found: has the page changed?)")
        if args.raw:
            print(pollen.data)
    except Exception as e:
        print(f"Pollen: error: {e}")
    finally:
        await pollen.close()

    if args.lat is None or args.lon is None:
        print("\nForecast: pass --lat and --lon")
        return
    forecast = Forecast(cfg)
    try:
        await forecast.warm(True)
        print(f"\nForecast ({forecast.source})")
        print("\n".join(f"• {line}" for line in forecast.lines()))
    except Exception as e:
        print(f"\nForecast: error: {e}")
    finally:
        await forecast.close()


if __name__ == "__main__":
    asyncio.run(main())
