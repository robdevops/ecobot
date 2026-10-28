"""Telegram bot for a personal weather station (Ecowitt) and air-quality sensor (AirGradient),
answered by a model through OpenRouter, with proactive alerts. Settings come from environment variables
(see lib/config.py); run it under systemd with the unit's EnvironmentFile set.
"""

import asyncio
import contextlib
import dataclasses
import logging
import os
import signal
import time
from collections.abc import Coroutine

from lib.config import ROOT, Config, data_dir

# Matplotlib keeps its font cache here: a service user without a home directory would otherwise get a throw-away /tmp one on every start.
# Set before anything imports Matplotlib; an MPLCONFIGDIR in the environment wins.
os.environ.setdefault("MPLCONFIGDIR", str(data_dir() / "matplotlib"))

from openai import AsyncOpenAI, Timeout
from telegram import Update
from telegram.error import InvalidToken
from telegram.ext import Application, Defaults

from lib import intent
from lib.airgradient import AirGradient
from lib.airgradient import metrics as air_metrics
from lib.alerts import (PARTICULATES_CHECK_SECONDS, AlertState, ForecastMonitor, IrrigationMonitor, Notifier, ParticulatesMonitor,
                        PollenAndAsthmaMonitor, WeatherMonitor)
from lib.alerts.menu import LABELS, available_kinds
from lib.bot import TURN_SECONDS, Bot, polling_error
from lib.ecowitt import Archive, Ecowitt
from lib.ecowitt.source import HA_IMPORT_SECONDS
from lib.forecast import Forecast
from lib.irrigation import Tuya
from lib.irrigation.watchdog import CHECK_SECONDS as VALVE_CHECK_SECONDS
from lib.llm import Agent
from lib.logs import LogFormat
from lib.pollen import Pollen
from lib.compose import Composer
from lib.sources import Source
from lib.tools import Tools
from lib.warm import every, safely


_handler = logging.StreamHandler()
_handler.setFormatter(LogFormat("%(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)  # "Application started" etc.
log = logging.getLogger("ecobot")


async def start_sources(cfg: Config) -> tuple[list[Source], Config]:
    """Every configured data source that starts, and the settings in the time zone they run in. The weather station starts
    first: it names its own time zone, which wins over TZ, so the air sensor and the optional website sources (pollen,
    forecast, off unless switched on), and everything built after, use the station's. The forecast uses the station's
    location unless one is configured."""
    sources: list[Source] = []

    async def start(kind: type[Source], *args):
        """A source made and started; one that fails either way is logged and left out."""
        source = None
        try:
            source = kind(*args)
            await source.start()
            sources.append(source)
        except Exception:
            log.exception("%s couldn't start - continuing without it", kind.name)
            if source is not None:
                await safely(source.close)
    if cfg.ecowitt:
        await start(Ecowitt, cfg)
    eco = sources[0] if sources else None
    if eco and str(eco.tz) != str(cfg.tz):
        if os.getenv("TZ"):
            log.warning("TZ=%s is not the weather station's time zone; using the station's, %s", os.getenv("TZ"), eco.tz)
        cfg = dataclasses.replace(cfg, tz=eco.tz)
    if not getattr(cfg.tz, "key", None):
        log.warning("The time zone has no name, only an offset (%s), so daylight saving will put the times out: set TZ, "
                    "e.g. TZ=Australia/Melbourne", cfg.tz)
    if cfg.airgradient:
        await start(AirGradient, cfg)
    if cfg.pollen:
        await start(Pollen, cfg)
    if cfg.forecast:
        await start(Forecast, cfg, (eco.latitude, eco.longitude) if eco and None not in (eco.latitude, eco.longitude) else None)
    return sources, cfg


class Tasks:
    """The tasks the bot runs in the background: one that stops with an error is logged as it does, and close() cancels them."""

    def __init__(self):
        self.running: set[asyncio.Task] = set()

    def start(self, work: Coroutine) -> asyncio.Task:
        task = asyncio.create_task(work, name=getattr(work, "__qualname__", None))
        self.running.add(task)
        task.add_done_callback(self._ended)
        return task

    def _ended(self, task: asyncio.Task):
        self.running.discard(task)
        if not task.cancelled() and (error := task.exception()):
            log.error("%s stopped: %s: %s", task.get_name(), type(error).__name__, error, exc_info=error)

    async def close(self):
        for task in self.running:
            task.cancel()
        await asyncio.gather(*self.running, return_exceptions=True)


def start_alerts(cfg: Config, bot: Bot, notify: Notifier, tasks: Tasks) -> tuple[list[str], WeatherMonitor | None]:
    """Hook up the alert monitor of every source that started and of the irrigation controller. The weather alerts start in
    warm_up, once the station has been read. Returns the alert types now running, and the weather monitor."""
    eco, air, pollen, forecast = (bot.by_name.get(name) for name in ("Ecowitt", "AirGradient", "Pollen", "Forecast"))
    running: list[str] = []
    weather = WeatherMonitor(eco, bot.state, notify, cfg.alert_cooldown_minutes * 60, cfg.rain_quiet_hours) if eco else None
    if air:
        tasks.start(every(PARTICULATES_CHECK_SECONDS, ParticulatesMonitor(air, bot.state, notify).check))
        running += ParticulatesMonitor.KINDS
    if forecast:
        forecast.warmer.after.append(ForecastMonitor(forecast, bot.state, notify).check)   # after each refresh (in the day)
        running += ForecastMonitor.KINDS
    if pollen:
        pollen.warmer.after.append(PollenAndAsthmaMonitor(pollen, bot.state, notify).check)  # after each refresh (in the day)
        running += PollenAndAsthmaMonitor.KINDS
    if bot.irrigation:   # the daily check (rain delay, battery) and the watchdog that closes the valve after a run
        tasks.start(IrrigationMonitor(bot.irrigation, bot.state, notify, cfg.tz, cfg.irrigation_check_hour, eco, forecast).run())
        tasks.start(every(VALVE_CHECK_SECONDS, bot.watchdog.check))
        running += IrrigationMonitor.KINDS
    return running, weather


async def warm_up(bot: Bot, weather: WeatherMonitor | None, running: list[str], tasks: Tasks):
    """Fetch everything once, together, and log one summary line; then start what waited for it: the weather alerts, the
    archives (copying each source's history into its cache)."""
    eco, air = bot.by_name.get("Ecowitt"), bot.by_name.get("AirGradient")

    async def ecowitt_part():
        # One request at a time (Ecowitt rate-limits): warm first, so the alerts' own look at the last 3 hours is answered
        # from what was just fetched. A failed first refresh must not stop the alerts or the archive: safely() logs it
        await safely(eco.warm, True)
        try:
            await weather.init()
            eco.warmer.after.append(weather.check)  # checked on every fresh refresh
            running[:0] = WeatherMonitor.KINDS
            await weather.check()
            if eco.fast is not None:   # Home Assistant: the rain alert looks at each new reading as it comes in, not only at the minute's refresh
                eco.on_live.append(weather.check_rain)
        except Exception:
            log.exception("Weather alerts couldn't start")
        if eco.fast is not None:
            tasks.start(eco.watch_live())
        # The full history is copied into the cache in the background (a few minutes the first time), so questions rarely
        # need Ecowitt inline
        tasks.start(Archive(eco, eco.archive_pace).loop())
        if eco.ha:   # the readings Home Assistant kept: the charts' 5-minute ranges, for the days the bot was not watching too
            tasks.start(every(HA_IMPORT_SECONDS, eco.import_ranges))

    async def air_part():
        await safely(air.warm, True)
        if air.cloud:
            tasks.start(safely(air_backfill))  # even if the sensor was unreachable just now

    async def air_backfill():
        started = time.monotonic()
        fetched, failed, total = await air.backfill()
        log.info("AirGradient backfill: %d days fetched, %d failed, %d days held, %.0fs", fetched, failed, total, time.monotonic() - started)

    try:
        await asyncio.gather(*([ecowitt_part()] if eco else []), *([air_part()] if air else []))
    except Exception:
        log.exception("Startup fetch failed (the regular refreshes will catch up)")
    if missing := [LABELS[k] for k in available_kinds(bot.by_name, bot.irrigation is not None) if k not in running]:
        log.warning("Alerts NOT running: %s (see the error above)", ", ".join(missing))


def version() -> str:
    """The checked-out commit, so the log shows which code is running."""
    return commit_in(ROOT / ".git")


def commit_in(git_dir) -> str:
    """The short hash HEAD points at, read from the repository's files rather than by running git (which refuses a folder owned by
    another user, as when the service runs as its own user). "unknown" when it cannot be read."""
    try:
        head = (git_dir / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(None, 1)[1]
            try:
                head = (git_dir / ref).read_text().strip()
            except FileNotFoundError:   # after `git pack-refs` or gc the branch is a line in packed-refs
                head = next(line.split()[0] for line in (git_dir / "packed-refs").read_text().splitlines()
                            if line.endswith(f" {ref}"))
        return head[:7] if len(head) >= 7 and all(c in "0123456789abcdef" for c in head.lower()) else "unknown"
    except Exception:
        return "unknown"


async def main():
    log.info("Starting ecobot %s", version())
    cfg = Config.from_env()
    _handler.formatter.width = cfg.log_width   # every log line is cut to it from here on
    for name in ("ecobot", "lib"):   # the bot's own lines: the libraries keep theirs
        logging.getLogger(name).setLevel(cfg.log_level)
    air_metrics.REQUIRE_CONSECUTIVE = cfg.antispike_require_consecutive
    intent.FEELS_LIKE_IN_ALL = cfg.chart_all_feels_like
    intent.VPD_IN_ALL = cfg.chart_all_vpd
    # Whatever starts is closed on the way out, the last started first, however the bot stops (a failure part-way included)
    async with contextlib.AsyncExitStack() as stack:
        sources, cfg = await start_sources(cfg)   # in the weather station's time zone from here on
        for source in sources:
            stack.push_async_callback(safely, source.close)
        if not sources:
            raise SystemExit("No data source is working - nothing to talk about")
        by_name = {s.name: s for s in sources}
        eco, air = by_name.get("Ecowitt"), by_name.get("AirGradient")
        composer = Composer(eco, air) if eco and air else None  # charts and comparisons across the two sources
        tools = Tools([t for s in sources for t in s.tools] + (composer.tools if composer else []))
        # A model request may run as long as the bot gives a whole question (it then asks again, a reasoning step lower); a
        # dropped connection, a 429 or a 5xx is tried again twice, at once, inside that time
        client = AsyncOpenAI(api_key=cfg.openrouter_api_key, base_url=cfg.openrouter_base_url, timeout=Timeout(TURN_SECONDS, connect=5.0),
                             max_retries=2) if cfg.openrouter_api_key else None   # without a key the bot answers only what it works out in code
        if client:
            stack.push_async_callback(safely, client.close)
        else:
            log.warning("OPENROUTER_API_KEY is not set%s: running without the model, so answers are limited",
                        " (XAI_API_KEY is no longer used: the model now comes through OpenRouter)" if os.getenv("XAI_API_KEY") else "")
        state = AlertState(cfg.state_path)
        if cfg.admin_only:
            log.info("Telegram: ADMIN_ONLY=on: only admins are answered (see: ADMIN_CHAT_IDS)")
            if not cfg.admin_chat_ids and not any(chat < 0 for chat in state.chats):
                log.warning("Admin only is on but no group is known yet and ADMIN_CHAT_IDS is empty, so nobody can use the bot: "
                            "add it to a group, list your user id in ADMIN_CHAT_IDS, or set ADMIN_ONLY=off")
        irrigation = Tuya(cfg) if cfg.irrigation else None
        if irrigation:
            stack.push_async_callback(safely, irrigation.close)
        bot = Bot(cfg, Agent(client, cfg.openrouter_model, tools), sources, state, irrigation)
        stack.push_async_callback(bot.close)

        app = (Application.builder().token(cfg.telegram_token).concurrent_updates(True)
               .defaults(Defaults(disable_notification=True))  # silent messages
               .connect_timeout(10).read_timeout(15).write_timeout(15)  # default 5s is tight
               .build())
        bot.register(app)
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        try:
            await stack.enter_async_context(app)  # one task runs everything, so connections open and close cleanly
        except InvalidToken:   # (its message names the token, so it is not shown)
            raise SystemExit("TELEGRAM_BOT_TOKEN was rejected by Telegram: check it is the token @BotFather gave for this bot") from None
        await app.start()
        stack.push_async_callback(app.stop)
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False,
                                        error_callback=polling_error)   # messages sent while starting are answered (lib.bot.PENDING_MAX_SECONDS)
        stack.push_async_callback(app.updater.stop)
        tasks = Tasks()
        stack.push_async_callback(tasks.close)
        for source in sources:
            stack.push_async_callback(source.warmer.stop)

        log.info("Telegram: @%s, Model: %s", app.bot.username, cfg.openrouter_model if client else "none")
        for source in sources:   # what each is reading, in the order they are listed
            for line in source.startup_lines():
                log.info("%s", line)
        notify = Notifier(app.bot, state)
        await bot.publish_commands(app.bot)   # the / menu
        await bot.refresh_keyboards(app.bot)  # chats whose buttons are out of date are told, with the new ones
        running, weather = start_alerts(cfg, bot, notify, tasks)
        for source in sources:   # keeping warm: everything questions need, refreshed before they arrive
            tasks.start(source.warmer.run())
        tasks.start(warm_up(bot, weather, running, tasks))
        await stop.wait()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
