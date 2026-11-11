"""python -m ecobot_mcp: ecobot-mcp, the MCP server on its own (docs/ecobot-mcp.md 3.8). It runs the sources and their syncers
(ecobot_mcp/core.py: the keep-warm, the first fetch, the archive, the backfills, Home Assistant's imports), the alert rules writing
the alert log, and the MCP server (lib/mcp_server.py) at http://MCP_BIND/mcp, Streamable HTTP under uvicorn: put it behind TLS (a
reverse proxy such as Caddy) to reach it from elsewhere.

Settings come from the environment (lib/config.py, docs/configuration.md): the MCP_ ones and the shared ones; the bot's (BOT_) are not
needed. Among them MCP_BIND (host:port, default 127.0.0.1:8765), MCP_ALLOWED_HOSTS (the Host names it answers, default 127.0.0.1 and
localhost) and AUTH_TOKENS (token,token,..., one per client). Every request needs one of the tokens: with AUTH_TOKENS unset, the one
it generates at its first start and keeps in the data folder (lib/auth_token.py)."""

import asyncio
import contextlib
import logging
import signal
import socket

import uvicorn

from lib import auth_token, chart_images
from lib.config import Config
from lib.logs import LogFormat, on_journal, stream_handlers

from .core import Core, configure, start_sources, version

log = logging.getLogger("ecobot.mcp")
SHUTDOWN_SECONDS = 3   # how long uvicorn waits for open connections (a listen stream) to end when ecobot-mcp stops


class Uvicorn(uvicorn.Server):
    """uvicorn's server, leaving SIGINT and SIGTERM to ecobot-mcp (which closes everything in order: uvicorn's own handling raises the
    signal again once it has stopped, ending the process before the sources are closed), and ending the dashboard's event streams as it
    stops."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    async def shutdown(self, sockets=None):
        """uvicorn's graceful shutdown, the app's endless streams (the dashboard's events: `app.state.closing`) ended first, once no new
        connection is accepted, so it doesn't wait on them."""
        for server in self.servers:
            server.close()
        closing = getattr(getattr(self.config.app, "state", None), "closing", None)
        if closing is not None:
            closing()
        await super().shutdown(sockets)


def server_config(app, host: str, port: int) -> uvicorn.Config:
    """uvicorn's settings for the app: its lifespan on (the MCP session manager runs in it), its own access log off, and an open
    listen stream given SHUTDOWN_SECONDS to end when it stops (else it would wait for the client to hang up)."""
    return uvicorn.Config(app, host=host, port=port, lifespan="on", log_level="warning", access_log=False, proxy_headers=True,
                          timeout_graceful_shutdown=SHUTDOWN_SECONDS)


def generated_token(cfg: Config) -> str:
    """AUTH_TOKENS unset: the token kept in the data folder, made at the first start (its place is logged, never its value)."""
    folder = cfg.state_path.parent
    try:
        token = auth_token.ensure(folder)
    except OSError as e:
        raise SystemExit(f"AUTH_TOKENS is not set and a token couldn't be kept in {auth_token.path(folder)} ({type(e).__name__}: {e}): "
                         "set AUTH_TOKENS (token,token,...), or make the data folder writable") from None
    log.info("AUTH_TOKENS is not set: accepting the token kept in %s (the bot reads it from there; give it to any other client)",
             auth_token.path(folder))
    return token


async def main():
    from lib.mcp_server import EcobotServer, http_app
    log.info("Starting ecobot-mcp %s", version())
    cfg = Config.from_env(bot=False)
    for name in ("ecobot", "lib"):
        logging.getLogger(name).setLevel(cfg.log_level)
    host, port = cfg.mcp_bind
    tokens = cfg.auth_tokens or (generated_token(cfg),)
    configure(cfg)
    async with contextlib.AsyncExitStack() as stack:
        sources, cfg = await start_sources(cfg)   # in the weather station's time zone from here on
        chart_images.configure(cfg.dark_mode, cfg.tz)   # DARKMODE for the charts' images, when a client asks for them (Pillow not imported yet)
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
        hosts = tuple(dict.fromkeys(("127.0.0.1", "localhost", socket.gethostname(), *cfg.mcp_allowed_hosts)))   # (its own name too: a container's hostname)
        app = http_app(server, tokens=tokens, allowed_hosts=hosts, issuer_url=f"http://{f'[{host}]' if ':' in host else host}:{port}",
                       dashboard=cfg.mcp_dashboard)
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
        shown = host if ":" not in host else f"[{host}]"
        log.info("MCP: http://%s:%d/mcp, %s; answering Host %s", shown, port,
                 f"{len(tokens)} token(s) from AUTH_TOKENS" if cfg.auth_tokens else f"the token in {auth_token.path(cfg.state_path.parent)}",
                 ", ".join(hosts))
        if cfg.mcp_dashboard:
            log.info("Dashboard: http://%s:%d/dashboard (read-only, no token, any Host; MCP_DASHBOARD=off turns it off)", shown, port)
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
