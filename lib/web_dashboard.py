"""The gauges as a live web page, served by ecobot-mcp beside /mcp (lib/mcp_server.py http_app), read-only and open to the LAN:

  - GET /dashboard (and /dashboard/, the same page): one self-contained HTML5 page (web_dashboard.html: no framework, nothing fetched from elsewhere) whose script draws
    the gauges as inline SVG, with HTML titles and tables, in lib/gauge_look.py's colours and sizes, as the Status picture's PNG
    (lib/gauges.py) draws its three, without the tables; its tiles laid out to fill the window, portrait or landscape;
  - GET /dashboard/data: the tiles as JSON: the gauges' sheet (lib/gauge_sheet.py) from the service's `current` with context, answered
    from what is held (fetch off: a request never asks the station; the keep-warm keeps the live reading), as the Status picture's, each
    tile's specs.Tile fields and whether it is dimmed, with the time of the reading (`as_of`) and the look;
  - GET /dashboard/events: Server-Sent Events, a `readings` event whenever the gauges' readings in the station's kept live reading or the
    air sensor's latest change, told by the server's own change detection for ecobot://now/weather and ecobot://now/air
    (EcobotServer.publish_changes: nothing is fetched for it), so the page fetches the data again and moves its gauges in place. A
    comment every HEARTBEAT_SECONDS keeps a proxy from closing it.

No token is asked for (it is the weather and air readings only) and the Host isn't checked: a browser opens it by the machine's name. The MCP
endpoint keeps both. MCP_DASHBOARD off: none of the three routes is served (404). `close()` ends the open streams, so uvicorn's
graceful shutdown doesn't wait on them (ecobot_mcp's Uvicorn calls it once it stops accepting connections)."""

import asyncio
import json
import logging
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from . import gauge_sheet
from .airgradient.dashboard import AIR_GAUGES
from .airgradient.metrics import value_of
from .ecowitt.dashboard import GAUGES, live_value
from .gauge_look import look, tile_json

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("web_dashboard.html")
HEARTBEAT_SECONDS = 25.0     # a comment on an idle stream this often
RETRY_MS = 5000              # how soon a browser connects again after its stream ends
QUEUED = 4                   # events waiting on a stream that isn't read: more are dropped (one says it all: fetch again)
NOT_ANSWERING = "The weather station isn't answering right now."


def readings_of(live: dict | None) -> tuple | None:
    """What the gauges show now of a live reading ({group: {field: {value, ...}}}): each gauge's reading, and the gust counted into the
    wind's Max. None without one."""
    if not live:
        return None
    return tuple(live_value(live, where) for g in GAUGES for where in (g.live, *([g.counted] if g.counted else [])))


def air_readings_of(row: dict | None) -> tuple | None:
    """What the air gauges show now of the air sensor's latest reading (its raw row, as AirGradient keeps it): each of their readings
    (the AQI is PM2.5's). None without one."""
    if not row:
        return None
    return tuple(value_of(row, column) for column in dict.fromkeys(g.column for g in AIR_GAUGES))


class WebDashboard:
    """The page, its data and its event streams over an EcobotServer (its service's `current`, its bus and its context); `uris` the
    resources whose updates are the station's live reading's and the air sensor's (ecobot://now/weather, ecobot://now/air)."""

    def __init__(self, server, uris: tuple[str, ...] = ("ecobot://now/weather", "ecobot://now/air")):
        self.server, self.uris = server, tuple(uris)
        self._streams: set[asyncio.Queue] = set()
        self._shown = self._readings()
        self.closed = False
        server.bus.subscribe(self._on_event)

    # ---------- the data ----------
    async def data(self) -> dict:
        """{as_of, tiles, look}, or with `unavailable` (and no tiles) when the station gives nothing. From what is held, whatever its age
        (fetch off): an open request on the LAN never asks the station (the keep-warm keeps the live reading, and the events come from it)."""
        tool = self.server.service.get("current")
        result = json.loads(await tool.call(gauge_sheet.call(), {"fetch": False})) if tool else {"error": "no current"}
        sheet = gauge_sheet.sheet(result)
        out = {"as_of": result.get("as_of"), "tiles": [tile_json(t) for t in sheet.tiles] if sheet else [], "look": look()}
        if sheet is None:
            out["unavailable"] = (result.get("unavailable") or {}).get("station") or NOT_ANSWERING
        return out

    # ---------- the events ----------
    def _readings(self):
        """(the station's gauges' readings, the air sensor's) as they are kept."""
        try:
            kept = getattr(self.server.ctx.eco, "_live", None)
            air = getattr(self.server.ctx.air, "_current", None)
            return readings_of(kept[1] if kept else None), air_readings_of(air[0] if air else None)
        except Exception as e:   # (never stops the publishing)
            log.warning("Dashboard: couldn't read the live reading: %s: %s", type(e).__name__, e)
            return None

    def _on_event(self, event):
        """The bus's listener (sync, on the event loop): a weather or air update whose gauges' readings changed goes to every stream."""
        if getattr(event, "uri", None) not in self.uris:
            return
        now = self._readings()
        if now is None or now == (None, None) or now == self._shown:
            return
        self._shown = now
        for queue in list(self._streams):
            if queue.qsize() < QUEUED:
                queue.put_nowait("readings")

    def close(self):
        """End every open stream (and answer a new one with just its retry), for the server's shutdown."""
        self.closed = True
        for queue in list(self._streams):
            queue.put_nowait(None)

    async def _stream(self, queue: asyncio.Queue):
        try:
            yield f"retry: {RETRY_MS}\n: ecobot dashboard\n\n"
            while not self.closed:
                try:
                    what = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": still here\n\n"
                    continue
                if what is None:
                    break
                yield f"event: {what}\ndata: {{}}\n\n"
        finally:
            self._streams.discard(queue)

    # ---------- the routes ----------
    async def page(self, request: Request):
        return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})

    async def data_route(self, request: Request):
        return JSONResponse(await self.data(), headers={"Cache-Control": "no-store"})

    async def events(self, request: Request):
        queue: asyncio.Queue = asyncio.Queue()
        if self.closed:
            queue.put_nowait(None)
        else:
            self._streams.add(queue)
        return StreamingResponse(self._stream(queue), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    def routes(self, path: str = "/dashboard") -> list[Route]:
        # The page at `path/` too, not a redirect to `path`: a redirect is built from the Host the request came with, so behind a proxy
        # that maps a site's root to `path/` it would send the browser to ecobot-mcp's own LAN address (the page finds its data either way)
        return [Route(path, self.page, methods=["GET"]), Route(f"{path}/", self.page, methods=["GET"]),
                Route(f"{path}/data", self.data_route, methods=["GET"]),
                Route(f"{path}/events", self.events, methods=["GET"])]


__all__ = ["WebDashboard", "air_readings_of", "readings_of"]
