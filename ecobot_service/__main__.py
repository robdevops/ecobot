"""python -m ecobot_service: the weather and air service on its own (docs/mcp-service.md 3.8). It runs the sources and their syncers
(ecobot_service/core.py: the keep-warm, the first fetch, the archive, the backfills, Home Assistant's imports), the alert rules writing
the alert log, and the MCP server (lib/mcp_server.py) at http://MCP_BIND/mcp, Streamable HTTP under uvicorn: put it behind TLS (a
reverse proxy such as Caddy) to reach it from elsewhere.

Settings come from the environment, as the bot reads them (lib/config.py, docs/configuration.md), less the bot's own (no Telegram token
is needed), with MCP_TOKENS (name:token,..., one per client), MCP_BIND (host:port, default 127.0.0.1:8765) and MCP_ALLOWED_HOSTS (the
Host names it answers, default 127.0.0.1 and localhost). Without MCP_TOKENS it serves without auth, and then only on a loopback address."""

import asyncio
import contextlib
import ipaddress
import logging
import signal

import uvicorn

from lib.config import Config
from lib.logs import LogFormat, on_journal, stream_handlers

from .core import Core, configure, start_sources, version

log = logging.getLogger("ecobot.service")
SHUTDOWN_SECONDS = 3   # how long uvicorn waits for open connections (a listen stream) to end when the service stops


class Uvicorn(uvicorn.Server):
    """uvicorn's server, leaving SIGINT and SIGTERM to the service (which closes everything in order: uvicorn's own handling raises the
    signal again once it has stopped, ending the process before the sources are closed)."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def server_config(app, host: str, port: int) -> uvicorn.Config:
    """uvicorn's settings for the app: its lifespan on (the MCP session manager runs in it), its own access log off, and an open
    listen stream given SHUTDOWN_SECONDS to end when it stops (else it would wait for the client to hang up)."""
    return uvicorn.Config(app, host=host, port=port, lifespan="on", log_level="warning", access_log=False, proxy_headers=True,
                          timeout_graceful_shutdown=SHUTDOWN_SECONDS)


async def main():
    from lib.mcp_server import EcobotServer, http_app
    log.info("Starting the ecobot service %s", version())
    cfg = Config.from_env(bot=False)
    for name in ("ecobot", "lib"):
        logging.getLogger(name).setLevel(cfg.log_level)
    host, port = cfg.mcp_bind
    if not cfg.mcp_tokens and not loopback(host):
        raise SystemExit(f"MCP_BIND is {host}:{port}, which other machines can reach, and MCP_TOKENS is empty: set MCP_TOKENS "
                         "(name:token,...) or bind to 127.0.0.1")
    configure(cfg)
    async with contextlib.AsyncExitStack() as stack:
        sources, cfg = await start_sources(cfg)   # in the weather station's time zone from here on
        for source in sources:
            stack.push_async_callback(source.close)
        if not sources:
            raise SystemExit("No data source is working - nothing to serve")
        from lib.warm import Tasks
        tasks = Tasks()
        core = await Core.open(cfg, sources, stack, tasks)
        stack.push_async_callback(tasks.close)
        for source in sources:
            stack.push_async_callback(source.warmer.stop)
        server = EcobotServer(core.ctx, core.alert_log, health=core.health.report, version=version())
        app = http_app(server, tokens=cfg.mcp_tokens, allowed_hosts=cfg.mcp_allowed_hosts, issuer_url=f"http://{host}:{port}")
        core.log_startup()
        running = core.start_alerts()
        core.start_warmers()
        tasks.start(server.run())

        async def warm_up():
            await core.warm_up(running)
            if missing := core.missing_alerts(running):
                log.warning("Alerts NOT running: %s (see the error above)", ", ".join(missing))
        tasks.start(warm_up())

        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        web = Uvicorn(server_config(app, host, port))
        serving = asyncio.create_task(web.serve())
        log.info("MCP: http://%s:%d/mcp, %s; answering Host %s", host if ":" not in host else f"[{host}]", port,
                 f"tokens for {', '.join(name for name, _ in cfg.mcp_tokens)}" if cfg.mcp_tokens else "no auth (loopback only)",
                 ", ".join(cfg.mcp_allowed_hosts))
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
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
