#!/usr/bin/env python3
"""Print exactly what the report's fast path fetches (weather_now, air_quality and, if switched on, pollen_asthma and
weather_forecast), as the raw results the bot hands over. Paste the output to whoever is writing the report layout. Needs the
bot's environment, as the other scripts do (load your env file into the shell first; this script never reads it):

    set -a; . /etc/ecobot.env; set +a
    python scripts/report_inputs.py

It makes real requests (a few) and writes nothing.
"""

import asyncio
import json

from _common import parser  # noqa: E402  (also puts the repo on sys.path)

from ecobot import start_sources  # noqa: E402
from lib import intent  # noqa: E402
from lib.config import Config  # noqa: E402
from lib.tools import Tools  # noqa: E402


async def main():
    parser(__doc__).parse_args()
    cfg = Config.from_env()
    sources = await start_sources(cfg)
    try:
        tools = Tools([t for s in sources for t in s.tools])
        names = {s.name for s in sources}
        for name, args in intent.report_calls("Ecowitt" in names, "AirGradient" in names, "Pollen" in names, "Forecast" in names):
            print(f"=== {name} {json.dumps(args)}")
            print(await tools.call(name, json.dumps(args)))
            print()
    finally:
        for source in sources:
            await source.close()


if __name__ == "__main__":
    asyncio.run(main())
