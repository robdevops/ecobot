"""The bot's one connection to ecobot-mcp (BOT_MCP_URL, docs/ecobot-mcp.md 3.8), kept by one supervisor task (Upstream):
the protocol revision is pinned (2026-07-28, so a proxy's 502 during a restart can't land it on the older revision, which can't listen)
and discovered by hand for the instructions; when the connection fails or the listen is lost, the client is dropped and a new one built
after a backoff, which listens again (there is no replay: the caller reads what it missed). A request while it is down is refused with
an error that says so."""

import asyncio
import contextlib
import logging

import mcp_types as types
from mcp import Client
from mcp.shared.exceptions import MCPError

log = logging.getLogger("lib.upstream")

VERSION = "2026-07-28"          # the protocol revision pinned: the one that can listen
READ_SECONDS = 45.0             # the HTTP read timeout: three of the server's 15-second pings, so a silently dead link is noticed
CONNECT_WAIT = 10.0             # how long a request waits for ecobot-mcp to be reached before it is refused
BACKOFF = (1, 2, 5, 10, 30)     # seconds between attempts to reach it again, the last repeated


REFUSED = {401: "401: the token the bot sends isn't one ecobot-mcp accepts: give both the same AUTH_TOKENS, or leave it unset in both "
                "on one data folder (the generated token)",
           403: "403: the token was refused",
           421: "421: ecobot-mcp doesn't answer to this address: add its host part to ecobot-mcp's MCP_ALLOWED_HOSTS",
           404: "404: nothing at this path: the URL ends in /mcp"}


def http_transport(url: str, token: str | None = None, refused: dict | None = None):
    """The Streamable HTTP transport to ecobot-mcp, its bearer token sent with every request, its read timeout READ_SECONDS.
    refused: given, its "status" is set to an HTTP error the service answered with (the SDK reports any of them as one message)."""
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    @contextlib.asynccontextmanager
    async def connect():
        headers = {"Authorization": f"Bearer {token}"} if token else None
        async with create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(30.0, read=READ_SECONDS)) as http:
            if refused is not None:
                async def note(response):
                    if response.status_code >= 400:
                        refused["status"] = response.status_code
                http.event_hooks["response"].append(note)
            async with streamable_http_client(url, http_client=http) as streams:
                yield streams
    return connect()


def _leaf(e: BaseException) -> BaseException:
    """The first real error inside an exception group (a refused connection, a 401, a 421), not the group's "unhandled errors"."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


class Upstream:
    """The one client of ecobot-mcp, owned by one supervisor task (`run`): built, discovered and listening; dropped when it fails or
    its listen is lost or ends, and built again after a backoff. `target`: ecobot-mcp's URL, or (in tests) a Server in-process. `token`: the
    bearer token, or a function giving it (called for each connection), or None."""

    def __init__(self, target, token=None, on_event=None, on_discover=None, on_listening=None, resources=None):
        """Callbacks: on_discover(DiscoverResult), each new connection's, before it is used; on_listening(), once its listen is
        acknowledged (what was missed is read from here: an event from now on isn't); on_event(event), each listen event. Async.
        resources: the URIs listened to, of those the service lists (None: every one)."""
        self.target, self.token, self.resources = target, token, resources
        self.on_event, self.on_discover, self.on_listening = on_event, on_discover, on_listening
        self.client: Client | None = None
        self.connected = asyncio.Event()
        self.attempts = 0
        self.refused: dict = {}   # the HTTP error the service last answered with

    def _client(self) -> Client:
        token = self.token() if callable(self.token) else self.token   # (a function: read again for each connection)
        server = http_transport(self.target, token, self.refused) if isinstance(self.target, str) else self.target
        return Client(server, mode=VERSION, cache=None)

    async def run(self):
        """Keep a connection, until cancelled."""
        while True:
            self.refused.clear()
            try:
                async with self._client() as client:
                    raw = await client.session.send_discover(VERSION)
                    discovered = types.DiscoverResult.model_validate(raw)
                    client.session.adopt(discovered)
                    if self.on_discover:
                        await self.on_discover(discovered)
                    self.client, self.attempts = client, 0
                    self.connected.set()
                    log.info("ecobot-mcp: connected to %s", self.target if isinstance(self.target, str) else "the service")
                    await self._listen(client)
                    log.warning("ecobot-mcp ended the listen; connecting again")
            except asyncio.CancelledError:
                raise
            except BaseException as e:   # (a transport failure ends the whole client, as an exception group too)
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                if (me := asyncio.current_task()) is not None and me.cancelling():   # cancelled while connecting: the SDK may have
                    raise asyncio.CancelledError() from e                           # wrapped it (an exception group): stop all the same
                cause, status = _leaf(e), self.refused.get("status")
                why = (REFUSED.get(status, f"HTTP {status}") if status else f"{type(cause).__name__}: {cause}")
                log.warning("ecobot-mcp at %s isn't reachable (%s); trying again", self.target, why)
            finally:
                self.client = None
                self.connected.clear()
            await asyncio.sleep(BACKOFF[min(self.attempts, len(BACKOFF) - 1)])
            self.attempts += 1

    async def _listen(self, client: Client):
        """Listen to every resource the service lists and its lists' changes, passing each event on, until the stream ends."""
        uris = [r.uri for r in (await client.list_resources()).resources if self.resources is None or str(r.uri) in self.resources]
        async with client.listen(tools_list_changed=True, prompts_list_changed=True, resources_list_changed=True,
                                 resource_subscriptions=uris) as sub:
            if self.on_listening:
                await self.on_listening()
            async for event in sub:
                if self.on_event:
                    await self.on_event(event)

    async def get(self) -> Client:
        """The connected client; refused (MCPError) when the service can't be reached within CONNECT_WAIT."""
        if self.client is None:
            try:
                await asyncio.wait_for(self.connected.wait(), CONNECT_WAIT)
            except TimeoutError:
                pass
        if self.client is None:
            raise MCPError(types.INTERNAL_ERROR, f"ecobot-mcp isn't reachable ({self.target}): try again shortly.")
        return self.client
