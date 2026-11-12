"""python -m irrigation_mcp: irrigation-mcp, the irrigation controller's MCP server (docs/ecobot-mcp.md 3.9). It runs, whatever any
client does:

  - the watchdog (lib/irrigation/watchdog.py), every CHECK_SECONDS: a run it started is closed when it should have ended, a restart
    mid-run included (its end time is kept in irrigation_state.json);
  - the daily check at IRRIGATION_CHECK_HOUR (lib/irrigation/check.py): a low battery, and a 24 hour pause when it rained or rain is
    likely, asking ecobot-mcp (IRRIGATION_ECOBOT_MCP_URL) as its MCP client for the rain measured and the forecast; its alerts go to
    irrigation://alerts;
  - the MCP server (lib/irrigation/server.py) at http://IRRIGATION_BIND/mcp (default 127.0.0.1:8766), Streamable HTTP under uvicorn: put
    it behind TLS to reach it from elsewhere.

Settings come from the environment (lib/config.py, docs/configuration.md): the IRRIGATION_ ones and the shared ones (AUTH_TOKENS, DATA_DIR,
TZ, LOG_LEVEL). It needs IRRIGATION_TUYA_DEVICE_ID, IRRIGATION_TUYA_CLIENT_ID and IRRIGATION_TUYA_CLIENT_SECRET. Every request needs one
of AUTH_TOKENS, or with that unset the token generated in the data folder (lib/auth_token.py, the one ecobot-mcp uses); it sends ecobot-mcp
the first of AUTH_TOKENS, or that same generated token. One irrigation-mcp runs per data folder (irrigation.lock)."""

import asyncio
import contextlib
import logging
import signal
import socket

from lib import auth_token, mcp_http
from lib.config import Config
from lib.logs import LogFormat, on_journal, stream_handlers
from lib.mcp_http import Uvicorn
from lib.version import version

log = logging.getLogger("lib.irrigation.main")   # (under "lib": LOG_LEVEL reaches it)
LOCK = "irrigation.lock"
STARTUP_WAIT = 15.0   # seconds it waits for ecobot-mcp at start (its station names the time zone) before it carries on without
SHUTDOWN_SECONDS = mcp_http.SHUTDOWN_SECONDS


def ecobot_token(cfg: Config):
    """The token sent to ecobot-mcp: the first of AUTH_TOKENS; else the one generated in the data folder they share, read again for each
    connection (a function: ecobot-mcp may write it after this starts). Never logged."""
    if cfg.auth_tokens:
        return cfg.auth_tokens[0]
    folder = cfg.state_path.parent
    return lambda: auth_token.read(folder)


async def main():
    from lib.alert_log import AlertLog
    from lib.irrigation import Tuya, Watchdog
    from lib.irrigation.check import IrrigationMonitor
    from lib.irrigation.ecobot import EcobotWeather
    from lib.irrigation.server import IrrigationServer, http_app
    from lib.irrigation.state import IrrigationState
    from lib.irrigation.watchdog import CHECK_SECONDS
    from lib.warm import Tasks, every, safely
    log.info("Starting irrigation-mcp %s", version())
    cfg = Config.from_env(program="irrigation")   # (stops, saying so, without the controller's Tuya settings)
    for name in ("ecobot", "lib"):
        logging.getLogger(name).setLevel(cfg.log_level)
    host, port = cfg.irrigation_bind
    folder = cfg.state_path.parent
    async with contextlib.AsyncExitStack() as stack:
        mcp_http.hold_folder(folder, LOCK, "irrigation-mcp", "python -m irrigation_mcp", stack)
        tokens = cfg.auth_tokens or (mcp_http.generated_token(folder, log, "irrigation-mcp"),)
        state = IrrigationState(cfg.irrigation_state_path, cfg.state_path)
        device = Tuya(cfg)
        stack.push_async_callback(safely, device.close)
        watchdog = Watchdog(device, state)
        weather = EcobotWeather(cfg.irrigation_ecobot_mcp_url, ecobot_token(cfg), cfg.tz)
        stack.push_async_callback(weather.close)
        tasks = Tasks()
        stack.push_async_callback(tasks.close)
        tasks.start(weather.run())
        try:
            await asyncio.wait_for(weather.listening.wait(), STARTUP_WAIT)
        except TimeoutError:
            log.warning("ecobot-mcp at %s isn't reachable yet: the daily check goes without the rain measured and on the forecast last "
                        "read until it is (the time zone is TZ's, %s, until its station is read)", cfg.irrigation_ecobot_mcp_url, weather.tz)
        tz = weather.tz
        alert_log = AlertLog(cfg.irrigation_alert_log_path, tz)
        stack.callback(alert_log.close)
        server = IrrigationServer(device, watchdog, alert_log, version=version(), tz=tz)
        monitor = IrrigationMonitor(device, state, server.notify, tz, cfg.irrigation_check_hour, weather.rain, weather.forecast)

        def zone(z):   # the station's time zone, read (late, or another one): the check's time, the alerts' and the tools' times in it
            if str(z) != str(monitor.tz):
                log.info("The weather station's time zone is %s: irrigation-mcp runs in it from now on (it was %s)", z, monitor.tz)
            monitor.tz = alert_log.tz = server.tz = z
        weather.on_zone = zone
        tasks.start(server.run())
        tasks.start(every(CHECK_SECONDS, watchdog.check))
        tasks.start(monitor.run())
        hosts = tuple(dict.fromkeys(("127.0.0.1", "localhost", socket.gethostname(), *cfg.irrigation_allowed_hosts)))
        app = http_app(server, tokens=tokens, allowed_hosts=hosts, issuer_url=f"http://{f'[{host}]' if ':' in host else host}:{port}")
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        web = Uvicorn(mcp_http.server_config(app, host, port, SHUTDOWN_SECONDS))
        serving = asyncio.create_task(web.serve())
        shown = host if ":" not in host else f"[{host}]"
        log.info("Irrigation: the daily check at %02d:00 (%s), the watchdog every %ds%s", cfg.irrigation_check_hour, tz, CHECK_SECONDS, f"; a run is watched until {server._time(watchdog.off_at)}"
                 if watchdog.off_at else "")
        log.info("MCP: http://%s:%d/mcp, %s; answering Host %s; ecobot-mcp at %s", shown, port,
                 f"{len(tokens)} token(s) from AUTH_TOKENS" if cfg.auth_tokens else f"the token in {auth_token.path(folder)}",
                 ", ".join(hosts), cfg.irrigation_ecobot_mcp_url)
        waiting = asyncio.create_task(stop.wait())
        await asyncio.wait({serving, waiting}, return_when=asyncio.FIRST_COMPLETED)
        server.listen.close()   # (the listen streams end gracefully: their clients know to listen again)
        web.should_exit = True
        await serving
        waiting.cancel()
        if not stop.is_set():
            raise SystemExit(f"The MCP server stopped: is {host}:{port} free?")


def run():
    _format = LogFormat("%(levelname)s %(message)s", priority=on_journal())
    logging.basicConfig(level=logging.INFO, handlers=stream_handlers(_format))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
