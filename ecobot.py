"""Telegram bot for a personal weather station (Ecowitt) and air-quality sensor (AirGradient),
answered by xAI Grok, with proactive alerts. Settings come from environment variables
(see lib/config.py); run it under systemd with the unit's EnvironmentFile set.
"""

import asyncio
import contextlib
import logging
import signal
import subprocess
import time

from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import Application, Defaults

from lib import intent
from lib.airgradient import AirGradient
from lib.alerts import AIR_CHECK_SECONDS, AirMonitor, AlertState, ForecastMonitor, Notifier, PollenMonitor, WeatherMonitor
from lib.alerts.menu import available_kinds
from lib.bot import Bot, polling_error
from lib.config import ROOT, Config
from lib.ecowitt import Archive, Ecowitt
from lib.forecast import Forecast
from lib.llm import Agent
from lib.pollen import Pollen
from lib.compose import Composer
from lib.tools import Tools
from lib.warm import every, safely

logging.basicConfig(format="%(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)  # "Application started" etc.
log = logging.getLogger("ecobot")


async def start_sources(cfg: Config) -> list:
    """Every configured data source that starts: the weather station and the air sensor, then the optional website sources
    (pollen, forecast), which are off unless switched on. The forecast uses the station's location unless one is configured."""
    sources = []

    async def start(source):
        try:
            await source.start()
            sources.append(source)
        except Exception:
            log.exception("%s couldn't start - continuing without it", source.name)
            await source.close()
    for source in ([Ecowitt(cfg)] if cfg.ecowitt else []) + ([AirGradient(cfg)] if cfg.airgradient else []):
        await start(source)
    if cfg.pollen:
        await start(Pollen(cfg))
    if cfg.forecast:
        eco = next((s for s in sources if isinstance(s, Ecowitt)), None)
        await start(Forecast(cfg, (eco.latitude, eco.longitude) if eco and eco.latitude is not None else None))
    return sources


def version() -> str:
    """The checked-out commit, so the log shows which code is running."""
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=5, check=True).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


async def main():
    log.info("Starting ecobot %s", version())
    cfg = Config.from_env()
    intent.FEELS_LIKE_IN_ALL = cfg.chart_all_feels_like
    intent.VPD_IN_ALL = cfg.chart_all_vpd
    sources = await start_sources(cfg)
    if not sources:
        raise SystemExit("No data source is working - nothing to talk about")
    eco = next((s for s in sources if isinstance(s, Ecowitt)), None)
    air = next((s for s in sources if isinstance(s, AirGradient)), None)
    pollen = next((s for s in sources if isinstance(s, Pollen)), None)
    forecast = next((s for s in sources if isinstance(s, Forecast)), None)

    composer = Composer(eco, air) if eco and air else None  # charts and comparisons across the two sources
    tools = Tools([t for s in sources for t in s.tools] + (composer.tools if composer else []))
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
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False,
                                            error_callback=polling_error)   # messages sent while starting are answered (lib.bot.PENDING_MAX_SECONDS)
            try:  # any startup failure below still runs the shutdown steps (and shows the real error)
                log.info("@%s + %s (sources: %s)", app.bot.username, cfg.xai_model,
                         ", ".join(s.name for s in sources))
                notify = Notifier(app.bot, state, available_kinds({s.name for s in sources}))
                await bot.refresh_keyboards(app.bot)  # chats whose buttons are out of date are told, with the new ones

                # Alerts
                kinds = []
                if eco:
                    monitor = WeatherMonitor(eco, state, notify, cfg.alert_cooldown_minutes * 60, cfg.rain_quiet_hours)
                    kinds += ["rain", "rain likely", "temp. crossing"]
                if air:
                    air_monitor = AirMonitor(air, state, notify)
                    tasks.append(asyncio.create_task(every(AIR_CHECK_SECONDS, air_monitor.check)))
                    kinds.append("air")
                if forecast:
                    forecast.warmer.after.append(ForecastMonitor(forecast, state, notify).check)   # after each refresh (in the day)
                    kinds.append("forecast changes")
                if pollen:
                    pollen.warmer.after.append(PollenMonitor(pollen, state, notify).check)  # after each refresh (in the day)
                    kinds += ["pollen", "asthma"]
                log.info("Alerts: %s: %d chats", ", ".join(kinds) or "none", len(state.alert_chats()))

                # Keeping warm: everything questions need, refreshed before they arrive
                tasks.extend(asyncio.create_task(s.warmer.run()) for s in sources)
                log.info("Keeping warm: %s", ", ".join(f"{s.name} {s.warmer.interval:.0f}s" for s in sources))

                async def startup_warmup():
                    """Fetch everything once, together, and log one summary line."""
                    started, parts = time.monotonic(), []

                    async def ecowitt_part():
                        # One request at a time (Ecowitt rate-limits): warm first, so the alerts'
                        # own look at the last 3 hours is answered from what was just fetched
                        # A failed first refresh must not stop the alerts or the archive: safely() logs it
                        parts.append(await safely(eco.warm, True) or "Ecowitt failed")
                        try:
                            await monitor.init()
                            eco.warmer.after.append(monitor.check)  # checked on every fresh refresh
                            await monitor.check()
                        except Exception:
                            log.exception("Weather alerts couldn't start")
                        # The full history is copied into the cache in the background (a few minutes
                        # the first time), so questions rarely need Ecowitt inline
                        tasks.append(asyncio.create_task(archive.loop()))

                    async def air_part():
                        parts.append(await safely(air.warm, True) or "AirGradient failed")
                        tasks.append(asyncio.create_task(safely(air_backfill)))  # even if the sensor was unreachable just now

                    async def air_backfill():
                        started = time.monotonic()
                        fetched, failed, total = await air.backfill()
                        log.info("AirGradient archive: %d day(s) cached, %d failed, %d day(s) held, %.0fs",
                                 fetched, failed, total, time.monotonic() - started)

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
