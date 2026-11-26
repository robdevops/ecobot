"""The servers' tests talk to ecobot-mcp and irrigation-mcp through the SDK's MCP client, as any MCP client does (nothing of the bot):

    async with serve_ecobot(Context(eco=eco, tz=TZ), alert_log) as mcp:        # the real server in memory, over a test's Context
        out = await mcp.call("summary", {"readings": ["temperature"], "period": "1d"})     # a tool's answer as data
        station = await mcp.read("ecobot://station")                                   # a resource as data
        mcp.server                                                                     # the EcobotServer behind it
    async with serve_ecobot(tools=StandInService(...), sources=["Ecowitt"]) as mcp:    # a test's stand-in tools, served as ecobot-mcp's
    async with serve_irrigation_mcp(tmp_path) as mcp:                                 # irrigation-mcp over a fake controller
    async with McpClient(f"http://127.0.0.1:{port}/mcp", token="t") as mcp:            # a server running under uvicorn

The `serve_mcp` and `serve_irrigation_mcp` fixtures (tests/conftest.py) start these and close them when the test ends."""

import base64
import contextlib
import json
from urllib.parse import quote

from mcp import Client

from lib.mcp_server import EcobotServer
from lib.service.context import Context
from tests.fakes import TZ

VERSION = "2026-07-28"   # the protocol revision that can listen


class McpClient:
    """One SDK client of a server: `target` is a server object (the in-memory transport) or an http(s) URL (Streamable HTTP, `token` as the
    bearer, `headers` added to every request as a proxy would)."""

    def __init__(self, target, token: str | None = None, headers: dict | None = None):
        self.target, self.token, self.headers = target, token, headers or {}
        self.server = None   # the server behind it, for the in-memory ones
        self._stack = contextlib.AsyncExitStack()
        self.client: Client | None = None

    async def __aenter__(self):
        transport = self.target
        if isinstance(transport, str):
            transport = self._http(transport)
        self.client = await self._stack.enter_async_context(Client(transport, mode=VERSION))
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    def _http(self, url: str):
        import httpx2
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client
        headers = {**({"Authorization": f"Bearer {self.token}"} if self.token else {}), **self.headers}

        @contextlib.asynccontextmanager
        async def connect():
            async with create_mcp_http_client(headers=headers, timeout=httpx2.Timeout(30.0)) as http:
                async with streamable_http_client(url, http_client=http) as streams:
                    yield streams
        return connect()

    async def result(self, tool: str, args: dict | None = None, meta: dict | None = None):
        """A tool's whole result (the SDK's CallToolResult: content blocks, is_error, meta)."""
        return await self.client.call_tool(tool, args or {}, meta=meta)

    async def call(self, tool: str, args: dict | None = None, meta: dict | None = None):
        """A tool's answer: its first text block as JSON, or as text when it isn't."""
        return text_of(await self.result(tool, args, meta))

    async def read(self, uri: str):
        """A resource's first text block as JSON, or as text when it isn't."""
        return text_of((await self.client.read_resource(uri)).contents[0])

    async def resources(self) -> list[str]:
        """The URIs of the resources listed."""
        return [str(r.uri) for r in (await self.client.list_resources()).resources]

    async def templates(self) -> list[str]:
        """The URI templates of the resource templates listed."""
        return [t.uri_template for t in (await self.client.list_resource_templates()).resource_templates]

    async def tools(self) -> list[str]:
        """The names of the tools listed."""
        return [t.name for t in (await self.client.list_tools()).tools]

    def listen(self, *uris: str):
        """The changes announced for these resources: `async with mcp.listen(uri) as updates:` then `await updates.__anext__()`."""
        return self.client.listen(resource_subscriptions=list(uris))

    async def alerts(self, uri: str, since: int = 0, expired: bool = False):
        """The alerts resource `uri` as data: what it holds after id `since`, with the expired ones when asked."""
        return await self.read(f"{uri}?since={since}" + ("&expired=1" if expired else ""))

    async def feed(self, uri: str, **query):
        """The feed resource `uri` as data."""
        return await self.read(uri + ("?" + "&".join(f"{k}={quote(str(v))}" for k, v in query.items()) if query else ""))


def text_of(block):
    """A result's or a content's text, as the JSON it is (else as it is)."""
    if hasattr(block, "content"):   # a tool's result
        block = next((b for b in block.content if getattr(b, "type", None) == "text"), None)
    if block is None:
        return None
    try:
        return json.loads(block.text)
    except ValueError:
        return block.text


def images_of(result) -> list[bytes]:
    """The PNGs among a tool result's content blocks."""
    return [base64.b64decode(b.data) for b in result.content if getattr(b, "type", None) == "image"]


class ServedTool:
    """A test's stand-in tool as ecobot-mcp serves it: a name, a description, an input schema (anything goes), its `call`."""
    read_only, open_world = True, False

    def __init__(self, name: str, tool):
        self.name, self.tool = name, tool
        self.description = getattr(tool, "description", "") or f"The test's {name}."
        self.parameters = getattr(tool, "parameters", None) or {"type": "object"}

    async def call(self, args=None, meta=None) -> str:
        return await self.tool.call(args or {}, meta)


class ServedTools:
    """A test's tools (a lib.service.Service, a list of tools, or a tests.fakes.StandInService) as ecobot-mcp's: by name, in order."""

    def __init__(self, tools):
        if hasattr(tools, "answers"):   # (a StandInService: its tools by the answers it holds)
            named = [(name, tools.get(name)) for name in tools.answers]
        else:
            named = [(t.name, t) for t in tools]
        self.tools = [ServedTool(name, tool) for name, tool in named]
        self.by_name = {t.name: t for t in self.tools}

    def __iter__(self):
        return iter(self.tools)

    def get(self, name: str):
        return self.by_name.get(name)

    def __getitem__(self, name: str):
        return self.by_name[name]

    def __contains__(self, name: str) -> bool:
        return name in self.by_name


SOURCE_OF = {"eco": "Ecowitt", "air": "AirGradient", "forecast": "Forecast", "pollen": "Pollen"}   # (a resource's need: its source)


def stand_in(server: EcobotServer, sources: set[str]):
    """The server as if it ran these sources (names) over stand-in tools: ecobot://station names them, with every reading of the devices
    among them (as a station that has sent every field), and the resources that need one of them are there."""
    from lib.service.names import AIR_DEVICE, STATION
    station = server.station
    devices = {STATION: "Ecowitt" in sources, AIR_DEVICE: "AirGradient" in sources}

    async def named():
        readings = server.ctx.readings()
        sensors = {device: {"readings": [{"name": r.name, "label": r.label, "unit": r.unit} for r in readings if r.device == device]}
                   for device, there in devices.items() if there}
        return {**await station(), "sensors": sensors, "sources": {name: {} for name in sorted(sources)}}
    server.station = named
    server._there = lambda need: need is None or SOURCE_OF.get(need) in sources


def ecobot_server(ctx: Context | None = None, alert_log=None, *, tools=None, sources=None, tz=TZ) -> EcobotServer:
    """ecobot-mcp's real server over `ctx` (default: no sources, in `tz`) and `alert_log`; `tools` in place of the ones `ctx` gives,
    `sources` (names) in place of the ones ecobot://station lists."""
    server = EcobotServer(ctx if ctx is not None else Context(tz=tz), alert_log)
    if tools is not None:
        server._service = ServedTools(tools)
    if sources is not None:
        stand_in(server, set(sources))
    return server


@contextlib.asynccontextmanager
async def serve_ecobot(ctx: Context | None = None, alert_log=None, *, tools=None, sources=None, tz=TZ):
    """ecobot_server() in memory with a connected McpClient (`.server` is the server behind it)."""
    server = ecobot_server(ctx, alert_log, tools=tools, sources=sources, tz=tz)
    async with McpClient(server.server) as mcp:
        mcp.server = server
        yield mcp


@contextlib.asynccontextmanager
async def serve_irrigation_mcp(folder, device=None, *, tz=TZ):
    """irrigation-mcp's real server over `device` (default: a FakeController), its watchdog's state and its alert log in `folder`,
    publishing its alerts, with a connected McpClient (`.server` is the IrrigationServer: its device, watchdog, alert_log, notify)."""
    import asyncio

    from lib.alert_log import AlertLog
    from lib.irrigation import Watchdog
    from lib.irrigation.server import IrrigationServer
    from lib.irrigation.state import IrrigationState
    from tests.fakes import FakeController
    device = device if device is not None else FakeController()
    folder.mkdir(parents=True, exist_ok=True)
    alert_log = AlertLog(folder / "irrigation_alerts.sqlite", tz)
    server = IrrigationServer(device, Watchdog(device, IrrigationState(folder / "irrigation_state.json")), alert_log, tz=tz)
    task = asyncio.create_task(server.run())
    try:
        async with McpClient(server.server) as mcp:
            mcp.server = server
            yield mcp
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        alert_log.close()
