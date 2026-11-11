#!/usr/bin/env python3
"""Print exactly what the report is written from: the service's calls the bot makes for it (current, and, if switched on, pollen and
forecast: lib/intent.report_calls), as the raw results the service gives. Paste the output to whoever is writing the report layout.
Needs the bot's environment, as the other scripts do (load your env file into the shell first; this script never reads it):

    set -a; . /etc/ecobot.env; set +a
    python scripts/report_inputs.py

It makes real requests (a few) and writes nothing.
"""

import asyncio

from _common import parser  # noqa: E402  (also puts the repo on sys.path)

from ecobot import start_sources  # noqa: E402
from lib import intent  # noqa: E402
from lib.config import Config  # noqa: E402
from lib.service import Service  # noqa: E402
from lib.service.context import Context  # noqa: E402


async def main():
    parser(__doc__).parse_args()
    cfg = Config.from_env(bot=False)   # (ecobot-mcp's settings: the sources)
    sources, cfg = await start_sources(cfg)
    try:
        by_name = {s.name: s for s in sources}
        service = Service.of(await Context.open(eco=by_name.get("Ecowitt"), air=by_name.get("AirGradient"), forecast=by_name.get("Forecast"),
                                                pollen=by_name.get("Pollen"), tz=cfg.tz))
        for name, args in intent.report_calls(by_name):
            print(f"=== {name} {args}")
            print(await service[name].call(args))
            print()
    finally:
        for source in sources:
            await source.close()


if __name__ == "__main__":
    asyncio.run(main())
