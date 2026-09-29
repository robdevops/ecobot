"""Telegram bot for a personal weather station (Ecowitt) and air-quality sensor (AirGradient),
answered by xAI Grok, with proactive alerts. Settings come from environment variables
(see lib/config.py); run it under systemd with the unit's EnvironmentFile set.
"""

import asyncio
import contextlib
import logging
import signal
import time

from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import Application, Defaults

from lib.airgradient import AirGradient
from lib.alerts import AIR_CHECK_SECONDS, AirMonitor, AlertState, Notifier, WeatherMonitor
from lib.bot import Bot, polling_error
from lib.config import Config
from lib.ecowitt import Archive, Ecowitt
from lib.llm import Agent
from lib.tools import Tools
from lib.warm import REFRESH_SECONDS, every

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)  # "Application started" etc.
log = logging.getLogger("envirobot")


async def start_sources(cfg: Config) -> list:
    """Every configured data source that starts. Ecowitt and AirGradient are treated alike."""
    sources = []
    for source in ([Ecowitt(cfg)] if cfg.ecowitt else []) + ([AirGradient(cfg)] if cfg.airgradient else []):
        try:
            await source.start()
            sources.append(source)
        except Exception:
            log.exception("%s couldn't start - continuing without it", source.name)
            await source.close()
    return sources


async def main():
    cfg = Config.from_env()
    sources = await start_sources(cfg)
    if not sources:
        raise SystemExit("No data source is working - nothing to talk about")
    eco = next((s for s in sources if isinstance(s, Ecowitt)), None)
    air = next((s for s in sources if isinstance(s, AirGradient)), None)

    tools = Tools([t for s in sources for t in s.tools])
    agent = Agent(AsyncOpenAI(api_key=cfg.xai_api_key, base_url=cfg.xai_base_url), cfg.xai_model, tools)
    state = AlertState(cfg.state_path)
    bot = Bot(cfg, agent, sources, state)

    app = (Application.builder().token(cfg.telegram_token).concurrent_updates(True)
           .defaults(Defaults(disable_notification=True))  # silent messages
           .connect_timeout(10).read_timeout(15).write_timeout(15)  # default 5s is tight
           .build())
    bot.register(app)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks: list[asyncio.Task] = []
    archive = Archive(eco) if eco else None
    try:
        async with app:  # one task runs everything, so connections open and close cleanly
            await app.start()
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True,
                                            error_callback=polling_error)
            try:  # any startup failure below still runs the shutdown steps (and shows the real error)
                log.info("Bot @%s running with model %s (sources: %s)", app.bot.username, cfg.xai_model,
                         ", ".join(s.name for s in sources))
                notify = Notifier(app.bot, state)

                # Alerts
                kinds = []
                if eco:
                    monitor = WeatherMonitor(eco, state, notify)
                    kinds += ["rain", "rain likely", "temperature crossing"]
                if air:
                    air_monitor = AirMonitor(air, state, notify)
                    tasks.append(asyncio.create_task(every(AIR_CHECK_SECONDS, air_monitor.check)))
                    kinds.append(f"air quality (every {AIR_CHECK_SECONDS // 60} min)")
                log.info("Alerts: %s, to %d chat(s)", ", ".join(kinds) or "none", len(state.alert_chats()))

                # Keeping warm: everything questions need, refreshed before they arrive
                tasks.extend(asyncio.create_task(s.warmer.run()) for s in sources)
                log.info("Keeping warm every %ds: %s", REFRESH_SECONDS, " + ".join(s.name for s in sources))
                if archive:
                    tasks.append(asyncio.create_task(archive.loop()))

                async def startup_warmup():
                    """Fetch everything once, together, and log one summary line."""
                    started, parts = time.monotonic(), []

                    async def ecowitt_part():
                        # One request at a time (Ecowitt rate-limits): warm first, so the alerts'
                        # own look at the last 3 hours is answered from what was just fetched
                        parts.append(await eco.warm(True))
                        try:
                            await monitor.init()
                            eco.warmer.after.append(monitor.check)  # checked on every fresh refresh
                            await monitor.check()
                        except Exception:
                            log.exception("Weather alerts couldn't start")
                        added, failed = await archive.run_once()  # backfill days Ecowitt still has, oldest first
                        parts.append(f"5-min archive +{added} day(s)" + (f", {failed} failed" if failed else ""))

                    async def air_part():
                        parts.append(await air.warm(True))

                    try:
                        await asyncio.gather(*([ecowitt_part()] if eco else []), *([air_part()] if air else []))
                    except Exception:
                        log.exception("Startup warm-up failed (the regular refreshes will catch up)")
                    log.info("Startup warm-up: %s, %.1fs", ", ".join(parts) or "nothing to fetch",
                             time.monotonic() - started)

                tasks.append(asyncio.create_task(startup_warmup()))
                await stop.wait()
            finally:
                for task in tasks:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                await app.updater.stop()
                await app.stop()
    finally:
        for source in sources:
            await source.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
