"""What the two MCP servers here share over HTTP (ecobot-mcp, lib/mcp_server.py, and irrigation-mcp, lib/irrigation/server.py), with
nothing of either in it, so irrigation-mcp loads none of ecobot-mcp's tools or sources:

  - `TokenCheck`: the TokenVerifier (docs/ecobot-mcp.md 3.8): a bearer token is one of AUTH_TOKENS, compared in constant time;
  - `host_settings`: the Host (and Origin) headers a server answers (TransportSecuritySettings);
  - `RequestLog`: ASGI middleware, one line per HTTP request, a refusal with its reason;
  - `Uvicorn` and `server_config`: uvicorn's server as both run it (the signals left to the program, a listen stream given a moment to
    end at shutdown);
  - `generated_token`: AUTH_TOKENS unset, the token kept in the data folder (lib/auth_token.py), made at the first start;
  - `hold_folder`: one process of a program per data folder (a lock file);
  - `alerts_read`: an alert log (lib/alert_log.py) as its resource gives it, after `since`, unexpired unless `expired`."""

import contextlib
import hmac
import json
import logging
import time
from pathlib import Path

import mcp_types as types
import uvicorn
from mcp.server.auth.provider import AccessToken
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError

from . import auth_token

log = logging.getLogger(__name__)

SHUTDOWN_SECONDS = 3   # how long uvicorn waits for open connections (a listen stream) to end when a server stops


# ---------- auth ----------
class TokenCheck:
    """A server's TokenVerifier (3.8): a bearer token is one of AUTH_TOKENS, compared in constant time against every one of them.
    `revoke(token)` takes one out while the server runs (AUTH_TOKENS without it does the same at the next start)."""

    def __init__(self, tokens):
        self.tokens = list(dict.fromkeys(tokens))

    def revoke(self, token: str):
        if token in self.tokens:
            self.tokens.remove(token)

    async def verify_token(self, token: str) -> AccessToken | None:
        found = False
        sent = (token or "").encode()
        for known in list(self.tokens):   # (every one compared: the time says nothing of which matched)
            found |= hmac.compare_digest(sent, known.encode())
        return AccessToken(token=token, client_id="client", scopes=[]) if found else None


def host_settings(allowed: tuple[str, ...] | list[str]) -> TransportSecuritySettings:
    """The Host (and Origin) headers a server answers: each name allowed with any port, unless it names one."""
    hosts, origins = [], []
    for name in allowed:
        bare = name.startswith("[") and name.endswith("]") or ":" not in name.rsplit("]", 1)[-1]
        hosts += [name, f"{name}:*"] if bare else [name]
        origins += [f"{scheme}://{h}" for scheme in ("http", "https") for h in ([name, f"{name}:*"] if bare else [name])]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def generated_token(folder: Path, logger: logging.Logger = log, program: str = "ecobot-mcp") -> str:
    """AUTH_TOKENS unset: the token kept in the data folder, made at the first start (its place is logged, never its value). Every
    server and client on the one data folder uses the same one."""
    try:
        token = auth_token.ensure(folder)
    except OSError as e:
        raise SystemExit(f"AUTH_TOKENS is not set and a token couldn't be kept in {auth_token.path(folder)} ({type(e).__name__}: {e}): "
                         "set AUTH_TOKENS (token,token,...), or make the data folder writable") from None
    logger.info("AUTH_TOKENS is not set: %s accepts the token kept in %s (the bot reads it from there; give it to any other client)",
                program, auth_token.path(folder))
    return token


def hold_folder(folder: Path, name: str, program: str, command: str, stack):
    """One `program` per data folder: two on the same folder would both act (both fetch and alert; both water). The lock (the file
    `name` in the folder) is the kernel's, so it goes with the process, however that ends; `stack` releases it."""
    import fcntl
    path = Path(folder) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    held = open(path, "w")
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        held.close()
        raise SystemExit(f"Another {program} ({command}) already runs on {path.parent}: stop it first, or give this one another DATA_DIR.")
    stack.callback(held.close)


# ---------- an alert log as a resource ----------
def alerts_read(alert_log, query: dict | None = None) -> dict:
    """{alerts, last_id, identity}: the alerts after `since` (0), only the unexpired unless `expired` is true (query: parse_qs's)."""
    query = query or {}
    since = (query.get("since") or ["0"])[-1].strip()
    expired = (query.get("expired") or ["false"])[-1].strip().lower() in ("true", "1", "yes", "on")
    if since and not since.isdecimal():
        raise MCPError(types.INVALID_PARAMS, f"`since` is an alert's id, a whole number, not {since!r}.")
    return {"alerts": alert_log.since(int(since or 0), expired=expired), "last_id": alert_log.last_id(), "identity": alert_log.identity}


# ---------- uvicorn ----------
class Uvicorn(uvicorn.Server):
    """uvicorn's server, leaving SIGINT and SIGTERM to the program (which closes everything in order: uvicorn's own handling raises the
    signal again once it has stopped, ending the process before the rest is closed), and ending the app's endless streams (ecobot-mcp's
    dashboard) as it stops."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    async def shutdown(self, sockets=None):
        """uvicorn's graceful shutdown, the app's endless streams (`app.state.closing`) ended first, once no new connection is
        accepted, so it doesn't wait on them."""
        for server in self.servers:
            server.close()
        closing = getattr(getattr(self.config.app, "state", None), "closing", None)
        if closing is not None:
            closing()
        await super().shutdown(sockets)


def server_config(app, host: str, port: int, shutdown_seconds: float = SHUTDOWN_SECONDS) -> uvicorn.Config:
    """uvicorn's settings for the app: its lifespan on (the MCP session manager runs in it), its own access log off, and an open
    listen stream given `shutdown_seconds` to end when it stops (else it would wait for the client to hang up)."""
    return uvicorn.Config(app, host=host, port=port, lifespan="on", log_level="warning", access_log=False, proxy_headers=True,
                          timeout_graceful_shutdown=shutdown_seconds)


# ---------- the request log ----------
BODY_READ = 1 << 20   # the most of a request's body read to name its MCP method (the SDK reads it all anyway)
CHART_PATH = "/charts"   # (ecobot-mcp's chart links' pictures: their ids are cut short in the log)


def why_refused(hosts_setting: str) -> dict[int, str]:
    """The reason logged for each refusal, naming the setting that allows a Host."""
    return {401: "no token, or one that isn't in AUTH_TOKENS", 421: f"its Host isn't in {hosts_setting}",
            404: "nothing at this path, or the session has ended"}


def _rpc(body: bytes) -> str:
    """What a JSON-RPC body asks: its method, and the tool, resource or prompt it names ("tools/call summary")."""
    try:
        msg = json.loads(body)
    except ValueError:
        return ""
    msgs = msg if isinstance(msg, list) else [msg]
    out = []
    for m in msgs:
        if not isinstance(m, dict) or "method" not in m:
            continue
        params = m.get("params") if isinstance(m.get("params"), dict) else {}
        target = params.get("name") or params.get("uri")
        line = f"{m['method']} {target}" if isinstance(target, str) else str(m["method"])
        if isinstance(args := params.get("arguments"), dict) and args:
            line += " " + _arguments(args)
        out.append(line)
    return ", ".join(out)


def _arguments(args: dict, most: int = 160) -> str:
    """What a tool call asks for, short: its readings, its dates (the period as written, or start to end) and its interval
    ("rain 1d", "outdoor.temperature 2026-11-01 to 2026-11-05 5min"); irrigation-mcp's minutes and duration ("10", "24h"); cut at
    `most` characters."""
    def text(v) -> str:
        return ",".join(map(str, v)) if isinstance(v, list) else str(v)
    metric = args.get("readings") or args.get("reading")
    dates = args.get("period") or " to ".join(str(args[k]) for k in ("start", "end") if args.get(k))
    parts = (metric, dates, args.get("interval"), args.get("minutes"), args.get("duration"))
    out = " ".join(text(v) for v in parts if v not in (None, "", []))
    return out if len(out) <= most else out[:most - 1] + "…"


def _shown_path(path) -> str:
    """A path as logged: a chart link's id cut to its first characters (/charts/AbCdEf….png), the rest as it is."""
    path = str(path or "")
    if path.startswith(CHART_PATH + "/"):
        name = path[len(CHART_PATH) + 1:]
        key = name[:-4] if name.endswith(".png") else name
        return f"{CHART_PATH}/{key[:6]}{'…' if len(key) > 6 else ''}{'.png' if name.endswith('.png') else ''}"
    return path


class RequestLog:
    """ASGI middleware, outermost: one line per HTTP request, once its response starts (a listen stream's when it opens), on `logger`.
    INFO when answered, WARNING when refused, with the reason for a 401 (the token), a 421 (the Host: `hosts_setting` names the setting
    that allows one) and a 404. The token itself is never logged; the client's address is the proxy's X-Forwarded-For when uvicorn
    trusts it."""

    def __init__(self, app, logger: logging.Logger | None = None, hosts_setting: str = "MCP_ALLOWED_HOSTS"):
        self.app = app
        self.logger = logger or logging.getLogger("lib.mcp_server.requests")
        self.why = why_refused(hosts_setting)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start = time.monotonic()
        held, body = [], b""
        if scope.get("method") == "POST":   # (read here to name the MCP method, then handed on as it came)
            while True:
                message = await receive()
                held.append(message)
                if message["type"] != "http.request":
                    break
                if len(body) < BODY_READ:
                    body += message.get("body", b"")
                if not message.get("more_body"):
                    break

        async def replay():
            return held.pop(0) if held else await receive()

        async def sending(message):
            if message["type"] == "http.response.start":
                self.record(scope, _rpc(body) if body else "", message["status"], time.monotonic() - start)
            await send(message)
        await self.app(scope, replay, sending)

    def record(self, scope, asked: str, status: int, took: float):
        headers = dict(scope.get("headers") or [])
        client = (scope.get("client") or ("?",))[0]
        host = headers.get(b"host", b"").decode("latin-1")
        what = f"{scope.get('method')} {_shown_path(scope.get('path'))}" + (f" {asked}" if asked else "")
        if status < 400:
            self.logger.info("MCP %s: %s -> %d in %.2fs", client, what, status, took)
        else:
            why = "the chart's link has expired, or never was" if status == 404 and str(scope.get("path")).startswith(CHART_PATH + "/") \
                else self.why.get(status)
            self.logger.warning("MCP %s refused: %s -> %d%s (Host %s)", client, what, status, f", {why}" if why else "", host or "none")


__all__ = ["RequestLog", "alerts_read", "SHUTDOWN_SECONDS", "TokenCheck", "Uvicorn", "generated_token", "hold_folder", "host_settings", "server_config"]
