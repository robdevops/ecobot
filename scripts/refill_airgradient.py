"""Fetch every day of the AirGradient history from the cloud again, so the local store holds all of it (including the columns added
since a day was first cached, such as the sensor's own temperature and humidity). Run it with the bot stopped, with the bot's settings:

    set -a; . /etc/ecobot.env; set +a
    python scripts/refill_airgradient.py [--days 1460] [--pace 1.0]

It forgets which days were fetched, then runs the same backfill the bot runs at start: newest first, up to 9 days per request, stopping when
the sensor's history starts. A day the bot recorded itself is only completed, never replaced. It is safe to stop and run again (or to start
the bot, which carries on with the days not yet fetched). At the end it says how many stored readings still lack each value.
"""

import asyncio
import logging
import time

from _common import parser  # noqa: E402  (also puts the repo on sys.path)
from lib.airgradient import AirGradient  # noqa: E402
from lib.airgradient.source import BACKFILL_MAX_DAYS, BACKFILL_PACE  # noqa: E402
from lib.config import Config  # noqa: E402


async def main(days: int, pace: float):
    cfg = Config.from_env()
    if not cfg.airgradient_cloud:
        raise SystemExit("Set AIRGRADIENT_API_TOKEN and AIRGRADIENT_LOCATION_ID: the history comes from the cloud")
    air = AirGradient(cfg)
    try:
        before = await air.store.lacking()
        await air.store.forget_days()
        started = time.monotonic()
        fetched, failed, total = await air.backfill(pace=pace, max_days=days)
        after = await air.store.lacking()
        print(f"{fetched} days fetched, {failed} failed, {total} held, {time.monotonic() - started:.0f}s; "
              f"{after['all']:,} readings stored (was {before['all']:,})")
        for column, n in after.items():
            if column != "all":
                print(f"  {column:16} {n:,} without a value (was {before[column]:,})")
        if failed:
            print("Some days failed: run it again, or start the bot, to carry on.")
    finally:
        await air.close()


if __name__ == "__main__":
    args = parser(__doc__)
    args.add_argument("--days", type=int, default=BACKFILL_MAX_DAYS, help="how far back to look at most")
    args.add_argument("--pace", type=float, default=BACKFILL_PACE, help="seconds between requests")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    given = args.parse_args()
    asyncio.run(main(given.days, given.pace))
