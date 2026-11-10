"""python -m ecobot_service.bridge: the service on stdio, for a desktop client that starts its servers as commands (docs/mcp-service.md
3.8). It is a thin bridge: an SDK client of the running service (WEATHER_MCP_URL, default http://127.0.0.1:8765/mcp, with the bearer
token WEATHER_MCP_TOKEN), re-exposed as an MCP server on stdin and stdout: the tools, resources, resource templates and prompts are
forwarded as they are (a tool call's request _meta too), the server's instructions are the service's, and the service's listen stream
is forwarded to the client's listens. No sources, no rules, no file of its own: a second process on the readings file would fetch and
alert twice, and its in-memory bus would hear nothing.

The upstream connection is supervised (Upstream): the protocol revision is pinned (2026-07-28, so a proxy's 502 during a restart
can't land it on the older revision, which can't listen) and discovered once by hand for the instructions; when the connection fails
or the listen is lost, the client is dropped and a new one built after a backoff, which listens again, and the bridge's own listeners
are told everything changed (there is no replay). A request while it is down is refused with an error that says so.

    python -m ecobot_service.bridge [--url URL]

Logs go to stderr: stdout is the protocol."""

import argparse
import asyncio
import contextlib
import logging
import os
import sys

import mcp_types as types
from mcp import Client
from mcp.server.lowlevel import Server
from mcp.server.subscriptions import (InMemorySubscriptionBus, ListenHandler, PromptsListChanged, ResourcesListChanged, ResourceUpdated,
                                      ToolsListChanged)
from mcp.shared.exceptions import MCPError

log = logging.getLogger("ecobot.bridge")

URL_ENV, TOKEN_ENV = "WEATHER_MCP_URL", "WEATHER_MCP_TOKEN"
DEFAULT_URL = "http://127.0.0.1:8765/mcp"
VERSION = "2026-07-28"          # the protocol revision pinned: the one that can listen
READ_SECONDS = 45.0             # the HTTP read timeout: three of the server's 15-second pings, so a silently dead link is noticed
CONNECT_WAIT = 10.0             # how long a request waits for the service to be reached before it is refused
BACKOFF = (1, 2, 5, 10, 30)     # seconds between attempts to reach it again, the last repeated
OWN_META = "io.modelcontextprotocol/"   # the SDK's own _meta keys, stamped by each side itself: never forwarded


def _forwarded(meta) -> dict | None:
    """A _meta as passed on: the SDK's own keys and the progress token left out (each side stamps its own)."""
    out = {k: v for k, v in dict(meta or {}).items() if not k.startswith(OWN_META) and k not in ("progressToken", "progress_token")}
    return out or None


def http_transport(url: str, token: str | None = None):
    """The Streamable HTTP transport to the service, its bearer token sent with every request, its read timeout READ_SECONDS."""
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    @contextlib.asynccontextmanager
    async def connect():
        headers = {"Authorization": f"Bearer {token}"} if token else None
        async with create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(30.0, read=READ_SECONDS)) as http:
            async with streamable_http_client(url, http_client=http) as streams:
                yield streams
    return connect()


class Upstream:
    """The one client of the service, owned by one supervisor task (`run`): built, discovered and listening; dropped when it fails or
    its listen is lost or ends, and built again after a backoff. `target`: the service's URL, or (in tests) a Server in-process."""

    def __init__(self, target, token: str | None = None, on_event=None, on_discover=None, on_listening=None, resources=None):
        """Callbacks: on_discover(DiscoverResult), each new connection's, before it is used; on_listening(), once its listen is
        acknowledged (what was missed is read from here: an event from now on isn't); on_event(event), each listen event. Async.
        resources: the URIs listened to, of those the service lists (None: every one)."""
        self.target, self.token, self.resources = target, token, resources
        self.on_event, self.on_discover, self.on_listening = on_event, on_discover, on_listening
        self.client: Client | None = None
        self.connected = asyncio.Event()
        self.attempts = 0

    def _client(self) -> Client:
        server = http_transport(self.target, self.token) if isinstance(self.target, str) else self.target
        return Client(server, mode=VERSION, cache=None)

    async def run(self):
        """Keep a connection, until cancelled."""
        while True:
            try:
                async with self._client() as client:
                    raw = await client.session.send_discover(VERSION)
                    discovered = types.DiscoverResult.model_validate(raw)
                    client.session.adopt(discovered)
                    if self.on_discover:
                        await self.on_discover(discovered)
                    self.client, self.attempts = client, 0
                    self.connected.set()
                    log.info("Bridge: connected to %s", self.target if isinstance(self.target, str) else "the service")
                    await self._listen(client)
                    log.warning("Bridge: the service ended the listen; connecting again")
            except asyncio.CancelledError:
                raise
            except BaseException as e:   # (a transport failure ends the whole client, as an exception group too)
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                if (me := asyncio.current_task()) is not None and me.cancelling():   # cancelled while connecting: the SDK may have
                    raise asyncio.CancelledError() from e                           # wrapped it (an exception group): stop all the same
                log.warning("Bridge: the service at %s isn't reachable (%s: %s); trying again", self.target, type(e).__name__, e)
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
            raise MCPError(types.INTERNAL_ERROR, f"The weather service isn't reachable ({self.target}): try again shortly.")
        return self.client


class Bridge:
    """The stdio server (`server`) forwarding to the service through `upstream`."""

    def __init__(self, target=DEFAULT_URL, token: str | None = None):
        self.bus = InMemorySubscriptionBus()
        self.upstream = Upstream(target, token, on_event=self.bus.publish, on_discover=self._discovered, on_listening=self._listening)
        self._first = True
        self.server = Server("ecobot-bridge", title="Ecobot weather and air service (bridge)",
                             on_list_tools=self._list_tools, on_call_tool=self._call_tool,
                             on_list_resources=self._list_resources, on_list_resource_templates=self._list_templates,
                             on_read_resource=self._read_resource, on_list_prompts=self._list_prompts, on_get_prompt=self._get_prompt,
                             on_subscriptions_listen=ListenHandler(self.bus))

    async def _discovered(self, discovered: types.DiscoverResult):
        """A new connection: the service's instructions are the bridge's."""
        self.server.instructions = discovered.instructions

    async def _listening(self):
        """Listening again: after the first time, every listener is told everything changed, since what happened while the link was
        down is not replayed."""
        if self._first:
            self._first = False
            return
        client = self.upstream.client
        await self.bus.publish(ToolsListChanged())
        await self.bus.publish(PromptsListChanged())
        await self.bus.publish(ResourcesListChanged())
        if client is not None:
            for r in (await client.list_resources()).resources:
                await self.bus.publish(ResourceUpdated(str(r.uri)))

    async def _call(self, fn):
        """fn(client) against the service: its error (MCPError) passed on as it is; a failed connection refused with one that says so."""
        client = await self.upstream.get()
        try:
            return await fn(client)
        except MCPError:
            raise
        except Exception as e:
            raise MCPError(types.INTERNAL_ERROR, f"The weather service didn't answer ({type(e).__name__}: {e}).") from e

    async def _list_tools(self, ctx, params) -> types.ListToolsResult:
        r = await self._call(lambda c: c.list_tools())
        return types.ListToolsResult(tools=r.tools)

    async def _call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        r = await self._call(lambda c: c.call_tool(params.name, params.arguments, meta=_forwarded(ctx.meta)))
        return types.CallToolResult(content=r.content, structured_content=r.structured_content, is_error=r.is_error,
                                    _meta=_forwarded(r.meta))

    async def _list_resources(self, ctx, params) -> types.ListResourcesResult:
        r = await self._call(lambda c: c.list_resources())
        return types.ListResourcesResult(resources=r.resources)

    async def _list_templates(self, ctx, params) -> types.ListResourceTemplatesResult:
        r = await self._call(lambda c: c.list_resource_templates())
        return types.ListResourceTemplatesResult(resource_templates=r.resource_templates)

    async def _read_resource(self, ctx, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        r = await self._call(lambda c: c.read_resource(str(params.uri)))
        return types.ReadResourceResult(contents=r.contents)

    async def _list_prompts(self, ctx, params) -> types.ListPromptsResult:
        r = await self._call(lambda c: c.list_prompts())
        return types.ListPromptsResult(prompts=r.prompts)

    async def _get_prompt(self, ctx, params: types.GetPromptRequestParams) -> types.GetPromptResult:
        r = await self._call(lambda c: c.get_prompt(params.name, params.arguments))
        return types.GetPromptResult(description=r.description, messages=r.messages)

    @contextlib.asynccontextmanager
    async def running(self, wait: float = CONNECT_WAIT):
        """The upstream supervisor running (the first connection waited for, up to `wait` seconds, so the instructions are there for the
        client's first request), cancelled on leaving."""
        task = asyncio.create_task(self.upstream.run())
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.upstream.connected.wait(), wait)
            yield self
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def main(argv: list[str] | None = None):
    from mcp.server.stdio import stdio_server
    parser = argparse.ArgumentParser(prog="python -m ecobot_service.bridge", description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=os.getenv(URL_ENV) or DEFAULT_URL, help=f"the service's MCP endpoint (${URL_ENV}, default {DEFAULT_URL})")
    args = parser.parse_args(argv)
    bridge = Bridge(args.url, os.getenv(TOKEN_ENV) or None)
    async with bridge.running():
        async with stdio_server() as (read, write):
            await bridge.server.run(read, write, bridge.server.create_initialization_options())


def run():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
