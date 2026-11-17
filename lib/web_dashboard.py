"""The gauges as a live web page, served by ecobot-mcp beside /mcp (lib/mcp_server.py http_app), read-only and open to the LAN:

  - GET /dashboard (and /dashboard/, the same page): one self-contained HTML5 page (web_dashboard.html: no framework, nothing fetched from elsewhere) whose script draws
    the gauges as inline SVG, with HTML titles and tables, in lib/gauge_look.py's colours and sizes, as the Status picture's PNG
    (lib/gauges.py) draws its three, without the tables; its tiles laid out to fill the window, portrait or landscape, and arranged
    in the browser (dragged to reorder, hidden, reset; kept in its localStorage by the tiles' titles, nothing told to the server);
  - GET /dashboard/icon.png: the page's sun (its original, at ICON_SIZE px: lib/web_dashboard_icon.png) as a file, the page's own
    icon (favicon and apple-touch-icon), named relative to the path the page was asked for (/dashboard: "dashboard/icon.png",
    /dashboard/: "icon.png"), so it is right whatever path a proxy serves the page at. A file, not a data: URI in the page, because a
    link preview can't fetch one: some apps (Signal, iMessage) take the page's icon as the preview's picture.
    The page's Open Graph tags (og:title, og:image...) name the same file as this repository publishes it on GitHub (ICON_URL): WhatsApp
    and Messenger want og:image as a full https address and take no page icon, and ecobot-mcp can't know its own public address
    (nginx's `location / { proxy_pass http://deb1:8765/dashboard/; }` sends ecobot-mcp's own Host and /dashboard/), so nothing is set
    for it. Only link previews fetch og:image: a browser opening the page fetches nothing from elsewhere;
  - GET /dashboard/data: the tiles as JSON: the gauges' sheet (lib/gauge_sheet.py) from the service's `current` with context, answered
    from what is held (fetch off: a request never asks the station; the keep-warm keeps the live reading), as the Status picture's, each
    tile's specs.Tile fields and whether it is dimmed, with the time of the reading (`as_of`: the newer of the two devices'), each
    device's own (`devices`: {"station", "air"}: its reading's time, ISO in the station's zone, null with none held; a device not
    configured is not there), for the strip's stalled device greyed out, the look, and the station's rain rate now
    (`rain_rate`, mm/h: its rain gauge's, or its piezo gauge's where that's the one reporting, which no tile shows; null with
    neither), for the page's rain behind the tiles; today's highest of the dials with a thin red hand (`today_highs`, by title,
    the station's day in its zone: Wind's gust, Rain rate's, Solar radiation's, UV index's: their tiles' Daily Max, the Wind's its
    Daily Gust, the reading now counted in: today_highs()), the Wind direction tile's `rose` (today's
    share of the wind's direction from each of the 16 compass points, N first, clockwise, from the file's readings, calm ones left out;
    [] with none: rose()), and the gust now (`gust`, km/h: over 99, a new
    reading sends a cow, a barn or a chicken across behind the tiles);
  - GET /dashboard/events: Server-Sent Events, a `readings` event whenever the gauges' readings in the station's kept live reading or the
    air sensor's latest change, told by the server's own change detection for ecobot://now/weather and ecobot://now/air
    (EcobotServer.publish_changes: nothing is fetched for it), so the page fetches the data again and moves its gauges in place. A
    comment every HEARTBEAT_SECONDS keeps a proxy from closing it.

MCP_DASHBOARD_ANIMATIONS: the page served with data-animations="on", "low" or "off" on its <html> (ANIMATIONS), for every viewer.
On: the gauges sweep to their readings, the weather drawn behind them (its canvas, "stop", the test links), the zoom and the tiles'
reflow animated. Low: the gauges sweep and the tiles reflow as on, but no weather at all (nothing drawn, no loop, no "stop" or test
links) and the zoom at once. Off: no weather, and the gauges' needles and readings straight at their new place, the zoom and the reflow
at once. A browser asking for reduced motion gets off's, whatever the setting.

MCP_DASHBOARD_INDOOR off (privacy: the page needs no token): no indoor reading leaves the server through it. Its tiles (Indoor temperature,
Indoor humidity: any gauge of the indoor sensor's group, gauge_sheet.INDOOR) are left out of /dashboard/data's sheet, as if the station had
no indoor sensor, and /dashboard/events counts none of them in, so an indoor change alone sends no event (nor tells one happened). The bot,
the MCP tools, the Status picture and the alerts are as ever.

No token is asked for (it is the weather and air readings only) and the Host isn't checked: a browser opens it by the machine's name. The MCP
endpoint keeps both. MCP_DASHBOARD off: none of the four routes is served (404). `close()` ends the open streams, so uvicorn's
graceful shutdown doesn't wait on them (ecobot_mcp's Uvicorn calls it once it stops accepting connections)."""

import asyncio
import html
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import gauge_sheet
from .airgradient.dashboard import AIR_GAUGES
from .airgradient.metrics import value_of
from .service.common import iso
from .ecowitt.dashboard import GAUGES, GUST, live_value
from .ecowitt.reads import directions_on
from .gauge_look import look, tile_json
from .timeutil import day_bounds, local_date

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("web_dashboard.html")
ICON = Path(__file__).with_name("web_dashboard_icon.png")   # the page's sun, drawn large (its original, 534 px, at 512): WhatsApp asks 300 px, Facebook 200
ICON_NAME, ICON_SIZE = "icon.png", 512
ICON_URL = "https://raw.githubusercontent.com/robdevops/ecobot/main/lib/web_dashboard_icon.png"   # og:image: ICON as GitHub serves it (https, image/png)
ANIMATIONS = 'data-animations="on"'   # on the page's <html>: "off" or "low" there as MCP_DASHBOARD_ANIMATIONS is
MOTIONS = ("on", "off", "low")
PREVIEW = "<!--link preview-->"   # where the page's Open Graph tags go: first in its head, before its icons (Slack reads 32 KB of it)
TITLE = "Weather Dashboard"
DESCRIPTION = "The weather station's and the air sensor's gauges, live."
HEARTBEAT_SECONDS = 25.0     # a comment on an idle stream this often
RETRY_MS = 5000              # how soon a browser connects again after its stream ends
QUEUED = 4                   # events waiting on a stream that isn't read: more are dropped (one says it all: fetch again)
NOT_ANSWERING = "The weather station isn't answering right now."
RAIN_RATES = ("rainfall", "rainfall_piezo")   # where the station's rain rate is (current's groups): its rain gauge's first, then the piezo's
ROSE_SECONDS = 60           # the wind rose (today's directions) read from the file at most this often (the data asked for again sooner: the same)
ROSE_DECIMALS = 4           # its shares rounded so
RED_HANDS = {g.title: "Gust" if g.gust else "Max" for g in GAUGES if g.high_hand}   # the dials with a red hand at today's highest,
# and the column of their table whose Daily figure it is (the Wind's: its highest gust)


def readings_of(live: dict | None, indoor: bool = True) -> tuple | None:
    """What the gauges show now of a live reading ({group: {field: {value, ...}}}): each gauge's reading (the wind's direction's, its
    compass, too; the indoor ones only with `indoor`), those their red hands count in (the wind's: the gust, counted into its Gust too),
    and the piezo gauge's rain rate (the page's rain behind the tiles). None without one."""
    if not live:
        return None
    gauges = [g for g in GAUGES if indoor or not gauge_sheet.is_indoor(g)]
    return (*(live_value(live, where) for g in gauges for where in dict.fromkeys((g.live, *([g.high_hand] if g.high_hand else [])))),
            live_value(live, ("rainfall_piezo", "rain_rate")))   # (the piezo gauge's rain rate: the page's rain, with no gauge of its own)


def rain_rate_of(result: dict, live: dict | None = None) -> float | None:
    """The station's rain rate now (mm/h): in current's result, its rain gauge's, or, with no reading there, its piezo gauge's; then the
    piezo gauge's in the station's kept live reading (`live`), which current gives only when it is named. None with none of them."""
    sensors = result.get("sensors") or {}
    for group in RAIN_RATES:
        try:
            return float(((sensors.get(group) or {}).get("rain_rate") or {})["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return live_value(live or {}, ("rainfall_piezo", "rain_rate"))


def now_of(result: dict, live: dict | None, where: tuple[str, str]) -> float | None:
    """A reading of the station's now: in the kept live reading (`live`), else in current's result. None with neither."""
    got = live_value(live or {}, where)
    if got is None:
        try:
            got = float((((result.get("sensors") or {}).get(where[0]) or {}).get(where[1]) or {})["value"])
        except (KeyError, TypeError, ValueError):
            return None
    return got


def today_highs(tiles) -> dict[str, float | None]:
    """{title: today's highest} of these tiles' dials with a thin red hand (RED_HANDS: Wind's gust, Rain rate's, Solar radiation's, UV
    index's): their table's Daily figure (the Max; the Wind's Gust), which current gives for the station's day in its zone with the
    reading now counted in, so the hand rises with it; None with none. Nothing read for it: the sheet already holds it."""
    out = {}
    for tile in tiles:
        if tile.title in RED_HANDS:
            daily = dict(zip(tile.columns, tile.rows.get("Daily") or ()))
            out[tile.title] = daily.get(RED_HANDS[tile.title])
    return out


def air_readings_of(row: dict | None) -> tuple | None:
    """What the air gauges show now of the air sensor's latest reading (its raw row, as AirGradient keeps it): each of their readings
    (the AQI is PM2.5's). None without one."""
    if not row:
        return None
    return tuple(value_of(row, column) for column in dict.fromkeys(g.column for g in AIR_GAUGES))


class WebDashboard:
    """The page, its data and its event streams over an EcobotServer (its service's `current`, its bus and its context); `uris` the
    resources whose updates are the station's live reading's and the air sensor's (ecobot://now/weather, ecobot://now/air);
    `animations` (MCP_DASHBOARD_ANIMATIONS: "on", "off" or "low"; True and False are "on" and "off") off: the page draws no weather
    behind its gauges and nothing moves on it; low: no weather and the zoom at once, the gauges and the tiles moving as on. `indoor`
    (MCP_DASHBOARD_INDOOR) False: no indoor reading in its data or its events."""

    def __init__(self, server, uris: tuple[str, ...] = ("ecobot://now/weather", "ecobot://now/air"), animations: bool | str = "on",
                 indoor: bool = True):
        self.server, self.uris, self.indoor = server, tuple(uris), bool(indoor)
        self.animations = ("on" if animations else "off") if isinstance(animations, bool) else animations
        if self.animations not in MOTIONS:
            raise ValueError(f"animations: {animations!r} is not one of {', '.join(MOTIONS)}")
        self._streams: set[asyncio.Queue] = set()
        self._rose: tuple[float, int, list[float]] | None = None   # (when read, the day's start, its shares): rose()'s, kept ROSE_SECONDS
        self._shown = self._readings()
        self.closed = False
        server.bus.subscribe(self._on_event)

    # ---------- the data ----------
    async def data(self) -> dict:
        """{as_of, devices, tiles, look, rain_rate, gust, today_highs}, or with `unavailable` (and no tiles) when the station gives nothing. From what is held, whatever its age
        (fetch off): an open request on the LAN never asks the station (the keep-warm keeps the live reading, and the events come from it)."""
        tool = self.server.service.get("current")
        result = json.loads(await tool.call(gauge_sheet.call(), {"fetch": False})) if tool else {"error": "no current"}
        sheet = gauge_sheet.sheet(result, indoor=self.indoor)   # (indoor off: no indoor tile, nothing indoor sent)
        live = self._kept_live()
        tiles = [tile_json(t) for t in sheet.tiles] if sheet else []
        for tile in tiles:
            if tile["kind"] == "compass":   # (the wind's direction: today's rose under its needle)
                tile["rose"] = await self.rose()
        out = {"as_of": result.get("as_of"), "devices": self.device_times(), "tiles": tiles, "look": look(),
               "rain_rate": rain_rate_of(result, live), "gust": now_of(result, live, GUST),
               "today_highs": today_highs(sheet.tiles if sheet else [])}
        if sheet is None:
            out["unavailable"] = (result.get("unavailable") or {}).get("station") or NOT_ANSWERING
        return out

    async def rose(self) -> list[float]:
        """Today's wind rose (the station's day, in its zone): the share of today's readings of the wind's direction (its 5-minute
        readings and minutes in the file, calm ones left out: directions_on) from each of the 16 compass points, N first, clockwise;
        [] with none (or no file, or an error reading it). Read at most every ROSE_SECONDS (and again as the day turns), else kept."""
        eco, now = self.server.ctx.eco, time.time()
        start = day_bounds(local_date(int(now), self.server.ctx.zone), self.server.ctx.zone)[0]
        if self._rose and self._rose[1] == start and now - self._rose[0] < ROSE_SECONDS:
            return self._rose[2]
        shares: list[float] = []
        if getattr(eco, "storage", None) is not None and getattr(eco, "mac", None):
            try:
                counts = await eco.storage.read(directions_on, eco.mac, start, int(now) + 60)
                total = sum(counts)
                shares = [round(n / total, ROSE_DECIMALS) for n in counts] if total else []
            except Exception as e:   # (no rose drawn)
                log.warning("Dashboard: couldn't read today's wind directions: %s: %s", type(e).__name__, e)
        self._rose = (now, start, shares)
        return shares

    def device_times(self) -> dict[str, str | None]:
        """{"station", "air"}: the time of each configured device's reading as it is held (the station's: its newest reading's own time
        in the kept live reading, else when that was kept; the air sensor's: its latest row's timestamp, else when that was fetched), ISO
        in the station's zone; None for one holding none. A device not configured has no entry."""
        ctx, out = self.server.ctx, {}
        if ctx.eco is not None:
            kept = getattr(ctx.eco, "_live", None)
            at = 0
            for fields in (kept[1] if kept else {}).values():
                for obj in (fields.values() if isinstance(fields, dict) else ()):
                    try:
                        at = max(at, int(obj.get("time") or 0) if isinstance(obj, dict) else 0)
                    except (TypeError, ValueError):
                        pass
            out["station"] = iso(at or kept[0], ctx.zone) if kept else None
        if ctx.air is not None:
            held = getattr(ctx.air, "_current", None)
            at = None
            if held:
                try:
                    at = datetime.fromisoformat(str(held[0]["timestamp"]).replace("Z", "+00:00")).timestamp()
                except (KeyError, TypeError, ValueError):
                    at = held[1]
            out["air"] = iso(at, ctx.zone) if at else None
        return out

    def _kept_live(self) -> dict | None:
        kept = getattr(self.server.ctx.eco, "_live", None)
        return kept[1] if kept else None

    # ---------- the events ----------
    def _readings(self):
        """(the station's gauges' readings, the air sensor's) as they are kept."""
        try:
            air = getattr(self.server.ctx.air, "_current", None)
            return readings_of(self._kept_live(), self.indoor), air_readings_of(air[0] if air else None)
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

    # ---------- the link preview ----------
    def icon_url(self, path: str) -> str:
        """Where the icon is for a page asked for at `path`, relative to it (/dashboard: "dashboard/icon.png", /dashboard/: "icon.png"),
        which a browser or a link preview resolves against the URL it was given."""
        return ICON_NAME if path.endswith("/") else f"{path.rsplit('/', 1)[-1]}/{ICON_NAME}"

    def preview(self, path: str) -> str:
        """The Open Graph tags (and Twitter's card, read by Slack and Discord: "summary", a small square picture) and the page's icon links
        for a page asked for at `path`."""
        tags = [("og:type", "website"), ("og:title", TITLE), ("og:description", DESCRIPTION),
                ("og:image", ICON_URL), ("og:image:type", "image/png"), ("og:image:width", str(ICON_SIZE)),
                ("og:image:height", str(ICON_SIZE)), ("og:image:alt", "A sun")]
        icon = html.escape(self.icon_url(path))
        return "\n".join([*(f'<meta property="{k}" content="{html.escape(v)}">' for k, v in tags),
                          '<meta name="twitter:card" content="summary">',
                          # the page's own icon, the same picture: what a browser shows, and what some link previews take (Signal, iMessage)
                          # (Signal, iMessage): a data: URI can't be fetched by one, a file beside the page needs no public URL
                          f'<link rel="icon" type="image/png" href="{icon}">', f'<link rel="apple-touch-icon" href="{icon}">'])

    # ---------- the routes ----------
    async def page(self, request: Request):
        page = PAGE.read_text(encoding="utf-8").replace(PREVIEW, self.preview(request.url.path), 1)
        page = page.replace(ANIMATIONS, f'data-animations="{self.animations}"', 1)
        return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

    async def icon(self, request: Request):
        return Response(ICON.read_bytes(), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

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
                Route(f"{path}/{ICON_NAME}", self.icon, methods=["GET"]), Route(f"{path}/data", self.data_route, methods=["GET"]),
                Route(f"{path}/events", self.events, methods=["GET"])]


__all__ = ["RED_HANDS", "ROSE_SECONDS", "WebDashboard", "air_readings_of", "now_of", "rain_rate_of", "readings_of", "today_highs"]
