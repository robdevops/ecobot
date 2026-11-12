"""Telegram bot for a personal weather station (Ecowitt) and air-quality sensor (AirGradient),
answered by a model through OpenRouter, with proactive alerts. Settings come from environment variables
(see lib/config.py); run it under systemd with the unit's EnvironmentFile set.

The bot is a client of ecobot-mcp (python -m ecobot_mcp, docs/ecobot-mcp.md 3.8 and step 6), reached over MCP at BOT_MCP_URL, which
it can't start without (lib/weather.RemoteWeather): ecobot-mcp runs the sources, the alert log and the alert rules, and draws every
picture; the bot's tools, instructions, station and alerts come from it. The bot does what only a Telegram bot can: the chats and their
buttons, the model, and delivering the alerts.

With BOT_IRRIGATION_MCP_URL set it is irrigation-mcp's client too (python -m irrigation_mcp, docs/ecobot-mcp.md 3.9 and step 8b,
lib/irrigation_remote.py): its Irrigation button and menu call irrigation-mcp's tools, and irrigation://alerts is delivered as
ecobot://alerts is. The controller itself (Tuya, the watchdog, the daily check) is irrigation-mcp's alone.
"""

import asyncio
import contextlib
import dataclasses
import logging
import os
import signal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from lib import auth_token
from lib.config import RETIRED_TOKEN, Config


from telegram import Update
from telegram.error import InvalidToken, NetworkError
from telegram.ext import Application, Defaults

from lib import theme   # (DARKMODE: the bot asks ecobot-mcp for charts in its own theme)
from lib.alerts.notify import AlertDelivery, AlertState, Notifier, ReadMark
from lib.bot import TURN_SECONDS, Bot, Tasks, polling_error
from lib.irrigation_remote import NAME as IRRIGATION_MCP, RemoteIrrigation
from lib.llm import Agent
from lib.netenv import docker_private_network
from lib.logs import LogFormat, on_journal, stream_handlers
from lib import model_tools
from lib.version import commit_in, version   # noqa: F401 (commit_in: named here for the tests)
from lib.warm import safely
from lib.weather import RemoteWeather


_format = LogFormat("%(levelname)s %(message)s", priority=on_journal())   # DEBUG and INFO to stdout, WARN and ERROR to stderr
logging.basicConfig(level=logging.INFO, handlers=stream_handlers(_format))
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)   # the MCP client's: a line per request otherwise
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)  # "Application started" etc.
log = logging.getLogger("ecobot")

STARTUP_WAIT = 30.0   # seconds the bot waits for ecobot-mcp at start (its station names the time zone) before it carries on without


def start_alerts(cfg: Config, bot: Bot, notify: Notifier, tasks: Tasks, weather: RemoteWeather,
                 irrigation: RemoteIrrigation | None = None) -> list:
    """The alerts are the servers': ecobot-mcp's alert log read over MCP (ecobot://alerts) and, with irrigation-mcp, its own
    (irrigation://alerts), each delivered (AlertDelivery) to the chats subscribed to its kinds, each with its own last id handled in
    bot_state.json. Returns what keeps the time zone (the deliveries: follow_zone)."""
    delivery = AlertDelivery(weather.alerts, notify, bot.state, cfg.tz, weather=weather)   # (its charts drawn by ecobot-mcp)
    zoned: list = [delivery]
    tasks.start(delivery.run())
    if irrigation is not None:   # (its alerts have no chart)
        watering = AlertDelivery(irrigation.alerts, notify, bot.state, cfg.tz, mark=ReadMark(bot.state, IRRIGATION_MCP),
                                 unexpired_first=True)   # (at the first start: the battery or pause alerts of a check that came before)
        zoned.append(watering)
        tasks.start(watering.run())
    return zoned


def _zone(name: str | None):
    try:
        return ZoneInfo(name) if name else None
    except Exception:
        return None


def follow_zone(weather: RemoteWeather, bot: Bot, state: AlertState, zoned: list):
    """The bot in the station's time zone whenever it is read (the first read, if ecobot-mcp couldn't be reached at start, or
    another zone later): the bot's settings, the charts, the alerts' charts, and what start_alerts started (`zoned`: AlertDelivery,
    IrrigationMonitor, whose wait for the check time is timed again). The zone is kept in bot_state.json, for a start while ecobot-mcp
    can't be reached (connect)."""
    def apply(zone):
        if state.zone != str(zone):
            state.zone = str(zone)
            try:
                state.save()
            except Exception as e:
                log.warning("Couldn't save the station's time zone (%s: %s)", type(e).__name__, e)
        if str(zone) == str(bot.cfg.tz):
            return
        log.info("The weather station's time zone is %s: the bot runs in it from now on (it was %s)", zone, bot.cfg.tz)
        bot.cfg = dataclasses.replace(bot.cfg, tz=zone)
        bot.alert_charts.tz = zone
        theme.configure(bot.cfg.dark_mode, zone)
        for keeper in zoned:
            keeper.tz = zone
    weather.on_zone = apply
    if weather.station_tz is not None:   # (read before this was hooked up)
        apply(weather.station_tz)


def mcp_token(cfg: Config):
    """The token the bot sends ecobot-mcp: the first of AUTH_TOKENS; else the retired WEATHER_MCP_TOKEN, with a warning; else the one
    ecobot-mcp generated in the data folder they share, read again for each connection (a function: ecobot-mcp may write it after the
    bot starts: at a start of both, the bot may look before ecobot-mcp has written it). Never logged: only where it is."""
    if cfg.auth_tokens:
        if cfg.retired_mcp_token:
            log.warning("%s is retired and ignored: the bot sends the first of AUTH_TOKENS", RETIRED_TOKEN)
        return cfg.auth_tokens[0]
    if cfg.retired_mcp_token:
        log.warning("%s is retired: the bot sends it for now, but move it into AUTH_TOKENS (and into ecobot-mcp's AUTH_TOKENS, "
                    "if it isn't there already)", RETIRED_TOKEN)
        return cfg.retired_mcp_token
    folder = cfg.state_path.parent
    return lambda: auth_token.read(folder)


def no_token_yet(cfg: Config, token) -> bool:
    """Said (a warning) when ecobot-mcp couldn't be reached in time and the bot has no token to send it: the generated one isn't in the
    data folder. Only then: at a start of both, the file can appear a moment after the bot first looks."""
    if not callable(token) or token() is not None:
        return False
    log.warning("AUTH_TOKENS is not set and there is no token at %s: ecobot-mcp writes it there at its first start, on the same data "
                "folder. Until it can be read the bot sends none and is refused; set AUTH_TOKENS (the same in both) if ecobot-mcp keeps "
                "another folder", auth_token.path(cfg.state_path.parent))
    return True


async def connect(cfg: Config, stack: contextlib.AsyncExitStack, state: AlertState) -> tuple[RemoteWeather, Config]:
    """The bot as ecobot-mcp's client (BOT_MCP_URL): the supervisor started (closed by `stack`), the first connection waited for up to
    STARTUP_WAIT seconds; the settings in the station's time zone once it has been read, else in the one last read (kept in
    bot_state.json), else in TZ's, with a warning. Read later, it is followed then (follow_zone)."""
    kept = _zone(state.zone)
    token = mcp_token(cfg)
    weather = RemoteWeather(cfg.mcp_url, token, kept or cfg.tz)
    supervisor = asyncio.create_task(weather.run())

    async def stop():
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
        await weather.close()
    stack.push_async_callback(stop)
    try:
        await asyncio.wait_for(weather.listening.wait(), STARTUP_WAIT)
    except TimeoutError:
        no_token_yet(cfg, token)
        log.warning("ecobot-mcp at %s isn't reachable yet: questions are told so until it is (the time zone is %s, %s, "
                    "until its station is read)", cfg.mcp_url, "the station's last read" if kept else "TZ's", weather.tz)
    if str(weather.tz) != str(cfg.tz):
        cfg = dataclasses.replace(cfg, tz=weather.tz)
    return weather, cfg


TUYA_NAMES = ("IRRIGATION_TUYA_DEVICE_ID", "BOT_TUYA_DEVICE_ID", "TUYA_DEVICE_ID")   # (any of them set: the bot once ran the controller)


def connect_irrigation(cfg: Config, stack: contextlib.AsyncExitStack, token) -> RemoteIrrigation | None:
    """irrigation-mcp's client (BOT_IRRIGATION_MCP_URL), its supervisor started (closed by `stack`), not waited for: a press while it
    can't be reached is told so. None without the setting (no Irrigation button, no irrigation alerts); then a Tuya setting still in the
    environment (the bot ran the controller itself before step 8b) is a warning saying what to do."""
    if not cfg.irrigation_mcp_url:
        if any(os.getenv(name, "").strip() for name in TUYA_NAMES):
            log.warning("The irrigation controller's settings are set but BOT_IRRIGATION_MCP_URL isn't: the controller is irrigation-mcp's "
                        "now (python -m irrigation_mcp, the irrigation-mcp.service unit, or the irrigation-mcp container). Run it and set "
                        "BOT_IRRIGATION_MCP_URL (systemd: http://127.0.0.1:8766/mcp) for the Irrigation button and its alerts "
                        "(docs/configuration.md)")
        return None
    if urlsplit(cfg.irrigation_mcp_url).hostname in ("127.0.0.1", "localhost", "::1") and docker_private_network():
        log.warning("BOT_IRRIGATION_MCP_URL is %s, but in a container on Docker's own network that is the bot's container, not "
                    "irrigation-mcp's: set it to http://irrigation-mcp:8766/mcp (README: Docker)", cfg.irrigation_mcp_url)
    irrigation = RemoteIrrigation(cfg.irrigation_mcp_url, token)
    supervisor = asyncio.create_task(irrigation.run())

    async def stop():
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
        await irrigation.close()
    stack.push_async_callback(stop)
    return irrigation


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
    cfg = Config.from_env()   # (stops, saying so, without BOT_MCP_URL: the bot is ecobot-mcp's client and nothing else)
    _format.width = cfg.log_width   # a chat message in the log is cut to it from here on
    for name in ("ecobot", "lib"):   # the bot's own lines: the libraries keep theirs
        logging.getLogger(name).setLevel(cfg.log_level)
    # Whatever starts is closed on the way out, the last started first, however the bot stops (a failure part-way included)
    async with contextlib.AsyncExitStack() as stack:
        app = (Application.builder().token(cfg.telegram_token).concurrent_updates(True)
               .defaults(Defaults(disable_notification=True))  # silent messages
               .connect_timeout(10).read_timeout(15).write_timeout(15)  # default 5s is tight
               .build())
        handshake = asyncio.create_task(app.initialize())   # Telegram is asked who the bot is while ecobot-mcp is reached
        state = AlertState(cfg.state_path)
        try:
            weather, cfg = await connect(cfg, stack, state)   # in the weather station's time zone from here on, once it is read
        except BaseException:
            handshake.cancel()
            raise
        theme.configure(cfg.dark_mode, cfg.tz)   # DARKMODE (in the station's time zone, as everything from here on)
        client = _client(cfg) if cfg.openrouter_api_key else None   # without a key the bot answers its buttons and sends alerts, no more
        if client:
            stack.push_async_callback(safely, client.close)
        else:
            log.warning("BOT_OPENROUTER_API_KEY is not set: running without the model, so the bot answers its buttons and sends alerts, "
                        "and a typed question is told the model isn't set up")
        if cfg.admin_only and not cfg.admin_chat_ids and not any(chat < 0 for chat in state.chats):
            log.warning("Admin only is on but no group is known yet and BOT_ADMIN_CHAT_IDS is empty, so nobody can use the bot: "
                        "add it to a group, list your user id in BOT_ADMIN_CHAT_IDS, or set BOT_ADMIN_ONLY=off")
        tasks = Tasks()   # everything that runs in the background, the bot's own uploads among it; cancelled first on the way out (tasks.close, below)
        irrigation = connect_irrigation(cfg, stack, mcp_token(cfg))   # irrigation-mcp's client (BOT_IRRIGATION_MCP_URL), or None
        # ecobot-mcp's tools over MCP (lib/weather.py): the model is offered them (lib/model_tools.py), and the bot's own code paths
        # call them (the keyboard's charts, the Status picture, the alert charts and the period buttons)
        bot = Bot(cfg, Agent(client, cfg.openrouter_model, model_tools.tools(weather)), state, irrigation, tasks, service=weather)

        bot.register(app)
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        try:
            await handshake
            stack.push_async_callback(app.shutdown)   # one task runs everything, so connections open and close cleanly
        except InvalidToken:   # (its message names the token, so it is not shown)
            raise SystemExit("BOT_TELEGRAM_TOKEN was rejected by Telegram: check it is the token @BotFather gave for this bot") from None
        except NetworkError as e:   # (the host's network or DNS, most often): one line, not the library's pages of stack
            raise SystemExit(f"Can't reach Telegram ({type(e).__name__}: {e}): check the network and DNS of the machine or container") from None
        await app.start()
        stack.push_async_callback(app.stop)
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False,
                                        error_callback=polling_error)   # messages sent while starting are answered (lib.bot.PENDING_MAX_SECONDS)
        stack.push_async_callback(app.updater.stop)
        stack.push_async_callback(tasks.close)

        log.info("Telegram: @%s, Model: %s", app.bot.username, cfg.openrouter_model if client else "none")
        log.info("ecobot-mcp: %s (%s)", cfg.mcp_url, ", ".join(sorted(weather.names)) or "not reached yet")
        if irrigation:
            log.info("irrigation-mcp: %s", cfg.irrigation_mcp_url)
        notify = Notifier(app.bot, state, bot.alert_charts)   # the alerts' charts, kept with the bot's
        # the / menu: the bot's own commands and ecobot-mcp's prompts, set again whenever the prompts are read (each connection, and
        # when ecobot-mcp says they changed)
        weather.on_prompts = lambda: tasks.start(bot.publish_commands(app.bot))
        await bot.publish_commands(app.bot)
        zoned = start_alerts(cfg, bot, notify, tasks, weather, irrigation)
        follow_zone(weather, bot, state, zoned)
        await stop.wait()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
