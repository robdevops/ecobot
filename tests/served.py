"""ecobot-mcp in memory, as the bot reaches it (docs/ecobot-mcp.md step 6b: the bot is only ever its client): the real server
(lib/mcp_server.EcobotServer) over a test's Context (the fake sources, or none) and alert log, and the bot's own client of it
(lib/weather.RemoteWeather) connected through the SDK's in-memory transport, its supervisor running and listening. Every test that runs a
bot runs it against this (the `serve` fixture in tests/conftest.py):

    weather = await serve()                                    # no sources: the tools that need none, the instructions, no station
    weather = await serve(Context(eco=eco, tz=TZ))             # the fake station's tools
    weather = await serve(tools=StandInService(summary=...))   # a test's stand-in tools, served as ecobot-mcp's
    weather = await serve(tools=..., sources=["Ecowitt"])      # ... with ecobot://station naming these sources (the alerts, the shortcuts)
    bot = Bot(NS(tz=TZ), agent, alert_state, service=weather)

irrigation-mcp the same way (step 8b: the bot is its client too, BOT_IRRIGATION_MCP_URL): its real server (lib/irrigation/server.py)
over a fake controller (tests.fakes.FakeController, or a Tuya client over the fake Tuya cloud), its watchdog and its own alert log, and
the bot's client of it (lib/irrigation_remote.RemoteIrrigation), connected and listening (the `serve_irrigation` fixture):

    irrigation = await serve_irrigation(tmp_path)                    # the fake controller; irrigation.server.device is it
    bot = Bot(NS(tz=TZ), agent, alert_state, irrigation, service=weather)
"""

import asyncio
import contextlib

from lib.mcp_server import EcobotServer
from lib.service.context import Context
from lib.weather import RemoteWeather
from tests.fakes import TZ

READY_SECONDS = 10   # how long the client is given to connect and listen


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


def unreached() -> RemoteWeather:
    """ecobot-mcp at an address nothing answers, never connected: for a test of the bot's Telegram side alone, which asks it nothing."""
    return RemoteWeather("http://127.0.0.1:9/mcp", tz=TZ)


@contextlib.asynccontextmanager
async def irrigation_mcp(folder, device=None, *, tz=TZ):
    """(the bot's client of) irrigation-mcp over `device` (default: a FakeController), its watchdog's state and its alert log in
    `folder`, publishing its alerts. `.server` is the server behind it (its device, watchdog, alert_log, notify)."""
    from lib.alert_log import AlertLog
    from lib.irrigation import Watchdog
    from lib.irrigation.server import IrrigationServer
    from lib.irrigation.state import IrrigationState
    from lib.irrigation_remote import RemoteIrrigation
    from tests.fakes import FakeController
    device = device if device is not None else FakeController()
    folder.mkdir(parents=True, exist_ok=True)
    alert_log = AlertLog(folder / "irrigation_alerts.sqlite", tz)
    server = IrrigationServer(device, Watchdog(device, IrrigationState(folder / "irrigation_state.json")), alert_log, tz=tz)
    remote = RemoteIrrigation(server.server)
    remote.server = server   # (the test's way to the server behind it)
    tasks = [asyncio.create_task(server.run()), asyncio.create_task(remote.run())]
    try:
        await asyncio.wait_for(remote.listening.wait(), READY_SECONDS)
        yield remote
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await remote.close()
        alert_log.close()


@contextlib.asynccontextmanager
async def ecobot_mcp(ctx: Context | None = None, alert_log=None, *, tools=None, sources=None, tz=TZ):
    """(the bot's client of) ecobot-mcp over `ctx` (default: no sources, in `tz`) and `alert_log`; `tools` in place of the ones `ctx`
    gives, `sources` (names) in place of the ones ecobot://station lists."""
    server = EcobotServer(ctx if ctx is not None else Context(tz=tz), alert_log)
    if tools is not None:
        server._service = ServedTools(tools)
    if sources is not None:
        stand_in(server, set(sources))
    remote = RemoteWeather(server.server, tz=tz)
    remote.server = server   # (the test's way to the server behind it)
    task = asyncio.create_task(remote.run())
    try:
        await asyncio.wait_for(remote.listening.wait(), READY_SECONDS)
        yield remote
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await remote.close()
