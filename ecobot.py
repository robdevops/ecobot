"""Telegram bot for a personal weather station (Ecowitt) and air-quality sensor (AirGradient),
answered by a model through OpenRouter, with proactive alerts. Settings come from environment variables
(see lib/config.py); run it under systemd with the unit's EnvironmentFile set.

The service's half (the sources, the alert log, the tools, the alert rules, the keep-warm, the first fetch, the archive, the backfills and
Home Assistant's imports) is ecobot_service/core.py, which the service (python -m ecobot_service) runs the same way. With WEATHER_MCP_URL
unset the bot runs it in-process, beside its own half (Telegram, the model, delivering the alerts, the irrigation controller, its charts
drawn ahead). With it set the bot is the service's client over MCP (lib/weather.RemoteWeather, docs/mcp-service.md 3.8 and step 5): it
runs no sources, syncers or alert rules of its own; its tools, instructions, station and alerts come from the service. Either way the bot
reaches the service through one interface (lib/weather.py).
"""

import asyncio
import contextlib
import dataclasses
import logging
import signal

from lib.config import Config


from telegram import Update
from telegram.error import InvalidToken, NetworkError
from telegram.ext import Application, Defaults

from ecobot_service.core import Core, commit_in, configure, start_sources, version   # noqa: F401 (commit_in, start_sources: named here for the tests)
from lib import charts
from lib.airgradient import AirGradient   # noqa: F401 (the sources, named here for the startup tests)
from lib.alerts.irrigation import IrrigationMonitor
from lib.alerts.notify import AlertDelivery, AlertState, Notifier
from lib.alerts.weather import WeatherMonitor   # noqa: F401 (the weather rules, started by AlertRules; named here for the startup tests)
from lib.bot import AHEAD_SECONDS, TURN_SECONDS, Bot, Tasks, polling_error
from lib.ecowitt import Ecowitt   # noqa: F401
from lib.forecast import Forecast   # noqa: F401
from lib.irrigation import Tuya
from lib.irrigation.watchdog import CHECK_SECONDS as VALVE_CHECK_SECONDS
from lib.llm import Agent
from lib.logs import LogFormat, on_journal, stream_handlers
from lib.pollen import Pollen   # noqa: F401
from lib.service.alerts import AlertRules
from lib import model_tools
from lib.warm import every, safely
from lib.weather import RemoteWeather, ServiceRain


_format = LogFormat("%(levelname)s %(message)s", priority=on_journal())   # DEBUG and INFO to stdout, WARN and ERROR to stderr
logging.basicConfig(level=logging.INFO, handlers=stream_handlers(_format))
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)  # "Application started" etc.
log = logging.getLogger("ecobot")

STARTUP_WAIT = 30.0   # seconds the bot waits for a remote service at start (its station names the time zone) before it carries on without


def start_alerts(cfg: Config, bot: Bot, notify: Notifier, tasks: Tasks, core: Core | None,
                 weather: RemoteWeather | None = None) -> tuple[list[str], AlertRules | None]:
    """In-process (`core`): hook up the service's alert rules over every source that started (core.start_alerts: they append to the
    alert log); the weather rules start in warm_up, once the station has been read. Remote (`weather`): the rules are the service's, and
    its alert log is read over MCP. Either way the bot delivers from the log (AlertDelivery), and runs the irrigation controller's check
    and watchdog (sent by the bot itself until it is a server of its own, step 5b; remote, its rain and forecast are asked of the
    service). Returns the alert types now running here, and the rules (None when remote)."""
    if core is not None:
        running = core.start_alerts(moved_from=bot.state)   # (the first start moves the rules' state out of bot_state.json)
        feed, station, forecast = core.alert_log, bot.by_name.get("Ecowitt"), bot.by_name.get("Forecast")
    else:
        running, feed, station, forecast = [], weather.alerts, ServiceRain(weather), weather.forecast
    tasks.start(AlertDelivery(feed, notify, bot.state, cfg.tz).run())
    if bot.irrigation:   # the daily check (rain delay, battery) and the watchdog that closes the valve after a run
        tasks.start(IrrigationMonitor(bot.irrigation, bot.state, notify, cfg.tz, cfg.irrigation_check_hour, station, forecast).run())
        tasks.start(every(VALVE_CHECK_SECONDS, bot.watchdog.check))
        running += IrrigationMonitor.KINDS
    return running, core.rules if core is not None else None


async def connect(cfg: Config, stack: contextlib.AsyncExitStack) -> tuple[RemoteWeather, Config]:
    """The bot as the service's client (WEATHER_MCP_URL): the supervisor started (closed by `stack`), the first connection waited for up to
    STARTUP_WAIT seconds; the settings in the station's time zone once it has been read (else TZ's, with a warning)."""
    weather = RemoteWeather(cfg.weather_mcp_url, cfg.weather_mcp_token or None, cfg.tz)
    supervisor = asyncio.create_task(weather.run())

    async def stop():
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
        await weather.close()
    stack.push_async_callback(stop)
    try:
        await asyncio.wait_for(weather.listening.wait(), STARTUP_WAIT)
    except TimeoutError:
        log.warning("The weather service at %s isn't reachable yet: questions are told so until it is (the time zone is TZ's, %s)",
                    cfg.weather_mcp_url, cfg.tz)
    if weather.station_tz is not None and str(weather.station_tz) != str(cfg.tz):
        cfg = dataclasses.replace(cfg, tz=weather.station_tz)
    return weather, cfg


async def warm_up(bot: Bot, core: Core, running: list[str], tasks: Tasks, tg_bot=None):
    """The service's warm-up (core.warm_up: everything fetched once, together; then the weather alerts and the archives), then what
    the bot waited for: the alerts not running named, the keyboard's charts drawn ahead every few minutes."""
    await core.warm_up(running)
    if missing := core.missing_alerts(running, bot.irrigation is not None):
        log.warning("Alerts NOT running: %s (see the error above)", ", ".join(missing))
    if tg_bot is not None:
        tasks.start(every(AHEAD_SECONDS, lambda: bot.draw_buttons_ahead(tg_bot)))


AsyncOpenAI = Timeout = None   # openai's, imported when the model's client is made (_client): the import takes half a second, more than all of lib


def _client(cfg: Config):
    """The model's client. A request may run as long as the bot gives a whole question (it then asks again, a reasoning step lower); a
    dropped connection, a 429 or a 5xx is tried again twice, at once, inside that time."""
    global AsyncOpenAI, Timeout
    if AsyncOpenAI is None or Timeout is None:
        import openai
        AsyncOpenAI, Timeout = AsyncOpenAI or openai.AsyncOpenAI, Timeout or openai.Timeout
    return AsyncOpenAI(api_key=cfg.openrouter_api_key, base_url=cfg.openrouter_base_url, timeout=Timeout(TURN_SECONDS, connect=5.0), max_retries=2)


async def main():
    log.info("Starting ecobot %s", version())
    cfg = Config.from_env()
    _format.width = cfg.log_width   # a chat message in the log is cut to it from here on
    for name in ("ecobot", "lib"):   # the bot's own lines: the libraries keep theirs
        logging.getLogger(name).setLevel(cfg.log_level)
    configure(cfg)
    # Whatever starts is closed on the way out, the last started first, however the bot stops (a failure part-way included)
    async with contextlib.AsyncExitStack() as stack:
        app = (Application.builder().token(cfg.telegram_token).concurrent_updates(True)
               .defaults(Defaults(disable_notification=True))  # silent messages
               .connect_timeout(10).read_timeout(15).write_timeout(15)  # default 5s is tight
               .build())
        handshake = asyncio.create_task(app.initialize())   # Telegram is asked who the bot is while the sources start
        weather = None   # (WEATHER_MCP_URL: the service, reached over MCP)
        try:
            if cfg.weather_mcp_url:   # a client of the service: no sources of its own
                sources = []
                weather, cfg = await connect(cfg, stack)
            else:
                sources, cfg = await start_sources(cfg)   # in the weather station's time zone from here on
        except BaseException:
            handshake.cancel()
            raise
        for source in sources:
            stack.push_async_callback(safely, source.close)
        if not sources and weather is None:
            handshake.cancel()
            raise SystemExit("No data source is working - nothing to talk about")
        charts.configure(cfg.dark_mode, cfg.tz)   # DARKMODE (in the station's time zone, as everything from here on)
        client = _client(cfg) if cfg.openrouter_api_key else None   # without a key the bot answers only what it works out in code
        if client:
            stack.push_async_callback(safely, client.close)
        else:
            log.warning("OPENROUTER_API_KEY is not set: running without the model, so answers are limited")
        state = AlertState(cfg.state_path)
        if cfg.admin_only and not cfg.admin_chat_ids and not any(chat < 0 for chat in state.chats):
            log.warning("Admin only is on but no group is known yet and ADMIN_CHAT_IDS is empty, so nobody can use the bot: "
                        "add it to a group, list your user id in ADMIN_CHAT_IDS, or set ADMIN_ONLY=off")
        tasks = Tasks()   # everything that runs in the background, the bot's own uploads among it; cancelled first on the way out (tasks.close, below)
        # The service in-process (ecobot_service/core.py): the alert log the rules write and the bot delivers from, and the ten tools
        # over the sources that started (lib/service); or, remote, the same over MCP (lib/weather.py). The model is offered the tools
        # (lib/model_tools.py), and the bot's own code paths call them (its shortcuts, report, gauges, alert charts and period buttons)
        core = await Core.open(cfg, sources, stack, tasks) if weather is None else None
        irrigation = Tuya(cfg) if cfg.irrigation else None
        if irrigation:
            stack.push_async_callback(safely, irrigation.close)
        service = weather if weather is not None else core.service
        bot = Bot(cfg, Agent(client, cfg.openrouter_model, model_tools.tools(service)), sources, state, irrigation, tasks,
                  service=service, alert_log=core.alert_log if core else None)

        bot.register(app)
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        try:
            await handshake
            stack.push_async_callback(app.shutdown)   # one task runs everything, so connections open and close cleanly
        except InvalidToken:   # (its message names the token, so it is not shown)
            raise SystemExit("TELEGRAM_BOT_TOKEN was rejected by Telegram: check it is the token @BotFather gave for this bot") from None
        except NetworkError as e:   # (the host's network or DNS, most often): one line, not the library's pages of stack
            raise SystemExit(f"Can't reach Telegram ({type(e).__name__}: {e}): check the network and DNS of the machine or container") from None
        await app.start()
        stack.push_async_callback(app.stop)
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False,
                                        error_callback=polling_error)   # messages sent while starting are answered (lib.bot.PENDING_MAX_SECONDS)
        stack.push_async_callback(app.updater.stop)
        stack.push_async_callback(tasks.close)
        for source in sources:
            stack.push_async_callback(source.warmer.stop)

        log.info("Telegram: @%s, Model: %s", app.bot.username, cfg.openrouter_model if client else "none")
        if core is not None:
            core.log_startup()   # what each source is reading, in the order they are listed
        else:
            log.info("Weather service: %s (%s)", cfg.weather_mcp_url, ", ".join(sorted(weather.names)) or "not reached yet")
        notify = Notifier(app.bot, state, bot.alert_charts)   # the alerts' charts, kept with the bot's
        await bot.publish_commands(app.bot)   # the / menu
        running, rules = start_alerts(cfg, bot, notify, tasks, core, weather)
        if core is not None:
            core.start_warmers()   # keeping warm: everything questions need, refreshed before they arrive
            tasks.start(warm_up(bot, core, running, tasks, app.bot))
        else:   # the service keeps itself warm: the bot only draws its keyboard's charts ahead (quietly nothing while it can't be reached)
            tasks.start(every(AHEAD_SECONDS, lambda: bot.draw_buttons_ahead(app.bot)))
        await stop.wait()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
