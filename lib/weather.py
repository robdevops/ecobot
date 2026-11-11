"""ecobot-mcp's tools as the bot reaches them (docs/ecobot-mcp.md section 4, steps 5 and 6): ecobot-mcp is another program, reached over
MCP at BOT_MCP_URL (RemoteWeather: a client pinned to protocol 2026-07-28 that one supervisor task keeps connected). The bot runs no
sources of its own and never opens the readings file (step 6b). Everything the bot asks of ecobot-mcp goes through it:

  - `service`: the tools as tools/list gives them: iterated in order, `get(name)` or `service[name]`, each with its name, description,
    parameters and `call(args, meta)`, which returns the tool's JSON text (lib/model_tools.py offers them to the model; the bot's
    shortcuts, report, charts drawn ahead and period buttons call them);
  - `names`: the sources ecobot-mcp has ("Ecowitt", "AirGradient", "Forecast", "Pollen", from ecobot://station): which alerts, report
    sections and gauges there are;
  - `catalogue()`: the readings it holds (a shortcut's words are read against them); `instructions()`: the server's instructions (from
    `server/discover`); `air_link()`: the AirGradient dashboard (the live-chart link under an answer about the air);
  - `alerts`: the alert feed lib/alerts/notify.AlertDelivery reads (`last_id`, `identity`, `since`, `wait`), and `alert(id)` one event
    (the 🔄 under an alert replays its chart's drawn_by);
  - `resource(uri)`: a resource's content (ecobot://station, ecobot://forecast ...);
  - `ready(wait)`: whether it can be reached (once connected, waiting up to `wait` seconds for it);
  - `call_images(name, args, meta)`: a tool's answer as data (its charts the full JSON, each with its drawn_by) and its charts as
    ecobot-mcp drew them (ChartImage each: the PNG, the chart's JSON, the theme), the model's calls and the bot's own;
    `chart_image(name, args, meta)` the same with its first chart alone, and `picture(chart)` a chart drawn again (an alert's, by
    replaying its drawn_by). Asked with request `_meta` `chart_image: "png"` (the images from the result's ImageContent, the charts' JSON
    and theme from its `_meta`), in the bot's own theme (`chart_dark`: its DARKMODE, lib/theme.py, so its image cache's theme holds). The
    bot draws nothing: a service that sends no image (no Pillow there) gives the answer without its pictures;
  - `status_picture()`: the Status picture (ecobot://status: the station's temperature, humidity and rain gauges), as ecobot-mcp draws
    it; None without one;
  - `prompts`: ecobot-mcp's prompts (name, description, argument names: `Prompt`), read on each connection and again when it says they
    changed (PromptsListChanged; `on_prompts()` is called after each read), and `prompt(name, arguments)` one's question in words (the
    bot offers each as a Telegram command, step 6c).

For the irrigation check while it is still the bot's (step 8 moves it to its own server): `ServiceRain` (the measured rain, from
`summary {readings: ["rain"], period: "last 24 hours"}`) and `ServiceForecast` (ecobot://forecast, the last read kept when the service
can't be reached), docs 3.9."""

import asyncio
import base64
import json
import logging
from datetime import date, datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

from . import chart_images
from .service.common import AS_DRAWN, CHART_NOTE
from .timeutil import now_local

log = logging.getLogger(__name__)

STATION_URI, ALERTS_URI, FORECAST_URI = "ecobot://station", "ecobot://alerts", "ecobot://forecast"   # (as lib/mcp_server.py's)
STATUS_URI = "ecobot://status"   # the Status picture (lib/status.py)
LISTENED = (STATION_URI, ALERTS_URI, FORECAST_URI)   # what the bot listens to (not the live readings, pushed every few seconds)
CALL_WAIT = 5.0          # how long a question's call waits for the service while it is being reached again, before it is refused
BACKGROUND = "background"   # request _meta priority of a call nobody waits for (lib/service/common.py): refused at once while down
AIR_LINK_LABEL = "live chart"   # the AirGradient dashboard link's words (lib/airgradient/source.py)
AS_A_CALL = object()   # a wait of CALL_WAIT seconds (read when called)
UNREACHABLE = "The weather service isn't reachable right now: try again in a minute."


class ServiceUnreachable(RuntimeError):
    """The remote service can't be reached (it is down, or the connection is being made again)."""

    def __init__(self, message: str = UNREACHABLE):
        super().__init__(message)


class ChartNotDrawn(RuntimeError):
    """A chart came but couldn't be drawn (its JSON isn't a chart, or the renderer failed): not the service's refusal."""


class Prompt(NamedTuple):
    """One of ecobot-mcp's prompts, as prompts/list gives it."""
    name: str
    description: str
    arguments: tuple[str, ...]   # its arguments' names, in order


class ChartImage(NamedTuple):
    """A chart drawn."""
    png: bytes
    chart: dict          # its JSON (schemas/chart.json), with the drawn_by that draws it again
    dark: bool           # drawn in the dark theme


def charts_of(out) -> list[dict]:
    """The charts of an answer (none for a refusal)."""
    charts = out.get("charts") if isinstance(out, dict) and "error" not in out else None
    return [c for c in charts if isinstance(c, dict)] if isinstance(charts, list) else []


def _object(text: str) -> dict | None:
    """A tool's text as the JSON object it is; None when it isn't one."""
    try:
        out = json.loads(text)
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


async def value(x):
    """x, awaited when it is awaitable: the alert feed's methods are plain in an alert log (lib/alert_log.py) and coroutines over MCP
    (RemoteAlerts)."""
    return await x if asyncio.iscoroutine(x) or isinstance(x, asyncio.Future) else x


# ---------- over MCP ----------
def bot_dark() -> bool:
    """The bot's own theme now (DARKMODE: lib/charts.dark_now), sent as chart_dark."""
    from .periods import dark_now
    return dark_now()


class RemoteTool:
    """One of the service's tools as tools/list gave it, called over MCP."""

    def __init__(self, weather: "RemoteWeather", entry):
        self.weather, self.name = weather, entry.name
        self.description, self.parameters = entry.description or "", entry.input_schema
        hints = entry.annotations
        self.read_only = bool(getattr(hints, "read_only_hint", True))
        self.open_world = bool(getattr(hints, "open_world_hint", False))

    async def call(self, args: dict | None = None, meta: dict | None = None) -> str:
        return await self.weather.call_text(self.name, args, meta)


class RemoteTools:
    """The service's tool list, as lib.service.Service is used: in order, by name."""

    def __init__(self, tools=()):
        self.tools = list(tools)
        self.by_name = {t.name: t for t in self.tools}

    def __iter__(self):
        return iter(self.tools)

    def __len__(self):
        return len(self.tools)

    def __contains__(self, name) -> bool:
        return name in self.by_name

    def get(self, name: str):
        return self.by_name.get(name)

    def __getitem__(self, name: str):
        return self.by_name[name]


class RemoteAlerts:
    """The service's alert log read over MCP (ecobot://alerts and its template with `since`), as AlertDelivery reads an alert log.
    A read waits for the service while it can't be reached; `wait` returns when the listen says the alerts changed (ResourceUpdated) or a
    new connection listens (nothing is replayed, so what was missed is read after the last id handled)."""
    remote = True

    def __init__(self, weather: "RemoteWeather"):
        self.weather = weather
        self.identity: str | None = None
        self.newest = 0
        self._changed = asyncio.Event()

    def changed(self):
        self._changed.set()

    async def read(self, since: int = 0, expired: bool = False) -> dict:
        """ecobot://alerts?since=… as {alerts, last_id, identity}, waiting for the service as long as it takes; a refusal (MCPError) is
        raised. The connection closing under the read (MCPError too, CONNECTION_CLOSED: the service stopping) is no refusal: the read
        waits for the next connection, as for any other lost one (raised, AlertDelivery would wait its RETRY_SECONDS, 30, before reading
        again, holding up the alerts of a service that is back in a moment)."""
        from mcp.shared.exceptions import MCPError
        from mcp_types import CONNECTION_CLOSED
        uri = f"{ALERTS_URI}?since={int(since)}" + ("&expired=true" if expired else "")
        while True:
            try:
                data = await self.weather.resource(uri, wait=None)
            except MCPError as e:
                if e.code != CONNECTION_CLOSED:
                    raise
                log.debug("The connection closed while the alerts were read; reading them again on the next")
                await asyncio.sleep(1)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:   # (the connection lost mid-read: the supervisor builds another)
                log.debug("The alerts couldn't be read (%s: %s); trying again", type(e).__name__, e)
                await asyncio.sleep(1)
                continue
            self.identity, self.newest = data.get("identity"), int(data.get("last_id") or 0)
            return data

    async def last_id(self) -> int:
        await self.read(self.newest)
        return self.newest

    async def since(self, after: int = 0, expired: bool = False) -> list[dict]:
        self._changed.clear()   # (before the read: a change while it runs wakes the next wait)
        return (await self.read(after, expired))["alerts"]

    async def get(self, event_id: int) -> dict | None:
        found = await self.read(max(int(event_id) - 1, 0), expired=True)
        return next((a for a in found["alerts"] if a.get("id") == event_id), None)

    async def wait(self, after: int, timeout: float | None = None) -> bool:
        try:
            async with asyncio.timeout(timeout):
                await self._changed.wait()
            return True
        except TimeoutError:
            return False


PICTURE_WAIT = 8.0   # seconds an alert's picture waits for the service to draw it again, before the alert goes without it

class RemoteWeather:
    """The service at `target` (its MCP URL; in tests, a Server in memory), as the bot's interface. `run()` is the supervisor (the
    lib/upstream.py's Upstream: one client, discovered by hand for the instructions, listening; on a transport error or a lost or ended listen it
    is dropped and built again after a backoff, which discovers and listens again). Each new connection reads the tool list and the
    station; the listen's events keep them current (ToolsListChanged, the station's and the forecast's ResourceUpdated) and wake the
    alert delivery (the alerts'). A call while it is being reached waits up to CALL_WAIT seconds (a background one not at all) and is
    then refused with ServiceUnreachable."""
    is_weather, remote = True, True

    def __init__(self, target, token=None, tz=None):
        from lib.upstream import Upstream
        self.target, self.fallback_tz, self.station_tz = target, tz, None
        self.upstream = Upstream(target, token, on_event=self._event, on_discover=self._discovered, on_listening=self._listening,
                                 resources=LISTENED)
        self.service = RemoteTools()
        self.station: dict = {}
        self._instructions = ""
        self._catalogue = None
        self.alerts = RemoteAlerts(self)
        self.forecast = ServiceForecast(self)
        self.listening = asyncio.Event()   # the first connection's listen is up, its tools and station read
        self.on_zone = None   # on_zone(zone): the station's time zone, called when it is first read and when it changes (ecobot.py)
        self.prompts: list[Prompt] = []
        self.on_prompts = None   # on_prompts(): called after the prompts are read (ecobot.py: the bot's command menu set again)
        self._background: set[asyncio.Task] = set()

    def __iter__(self):   # (the tools, as the service lists them now: lib/model_tools.py follows the list)
        return iter(self.service)

    @property
    def url(self) -> str:
        return self.target if isinstance(self.target, str) else "the service"

    async def run(self):
        await self.upstream.run()

    # ---------- the connection ----------
    async def _discovered(self, discovered):
        self._instructions = discovered.instructions or ""

    async def _listening(self):
        """A new connection listens: the tools, the station and the prompts read again, the alerts looked at (what came while it was
        down is not replayed), the forecast read again."""
        for refresh in (self._read_tools, self._read_station, self._read_prompts):
            try:
                await refresh()
            except Exception as e:   # (the connection is still good: the listen's events refresh them later)
                log.warning("ecobot-mcp: couldn't read %s (%s: %s)", refresh.__name__.removeprefix("_read_"), type(e).__name__, e)
        self.alerts.changed()
        self._soon(self.forecast.refresh)
        self.listening.set()

    async def _event(self, event):
        from mcp.shared.subscriptions import PromptsListChanged, ResourceUpdated, ToolsListChanged
        if isinstance(event, ToolsListChanged):
            self._soon(self._read_tools)
        elif isinstance(event, PromptsListChanged):
            self._soon(self._read_prompts)
        elif isinstance(event, ResourceUpdated):
            uri = str(event.uri)
            if uri == ALERTS_URI:
                self.alerts.changed()
            elif uri == STATION_URI:
                self._soon(self._read_station)
            elif uri == FORECAST_URI:
                self._soon(self.forecast.refresh)

    def _soon(self, fn):
        """fn() in the background (a listen event is not held up by the reads it causes), its failure only logged."""
        async def run():
            try:
                await fn()
            except Exception as e:
                log.debug("ecobot-mcp: %s failed (%s: %s)", getattr(fn, "__name__", fn), type(e).__name__, e)
        task = asyncio.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _read_tools(self):
        client = await self._client(0)
        self.service = RemoteTools(RemoteTool(self, t) for t in (await client.list_tools()).tools)

    async def _read_prompts(self):
        client = await self._client(0)
        listed = (await client.list_prompts()).prompts
        self.prompts = [Prompt(p.name, p.description or "", tuple(a.name for a in p.arguments or ())) for p in listed]
        if self.on_prompts:
            self.on_prompts()

    async def _read_station(self):
        station = await self.resource(STATION_URI, wait=0)
        self.station, self._catalogue = station, None
        if zone := station.get("time_zone"):
            try:
                read = ZoneInfo(zone)
            except Exception:   # (an offset only: TZ stands)
                return
            first_or_new, self.station_tz = str(read) != str(self.station_tz), read
            if first_or_new and self.on_zone:
                self.on_zone(read)

    @property
    def tz(self):
        """The station's time zone (ecobot://station's), TZ until it has been read."""
        return self.station_tz or self.fallback_tz

    async def _client(self, wait: float | None):
        """The connected client, waiting up to `wait` seconds for one (None: as long as it takes); ServiceUnreachable after."""
        up = self.upstream
        if up.client is None and wait != 0:
            try:
                async with asyncio.timeout(wait):
                    while up.client is None:
                        await up.connected.wait()
                        if up.client is None:
                            await asyncio.sleep(0.05)
            except TimeoutError:
                pass
        if up.client is None:
            raise ServiceUnreachable()
        return up.client

    async def ready(self, wait=AS_A_CALL) -> bool:
        try:
            await self._client(CALL_WAIT if wait is AS_A_CALL else wait)
            return True
        except ServiceUnreachable:
            return False

    # ---------- what the bot asks ----------
    async def call_text(self, name: str, args: dict | None = None, meta: dict | None = None) -> str:
        """A tool's answer as its JSON text (the first text block: the tool's JSON, a refusal's too). A background request (nobody
        waiting) doesn't wait for the service."""
        meta = dict(meta or {})
        meta.setdefault("chart_image", chart_images.NO_IMAGE)   # (the JSON, even if BOT_MCP_URL asks for images: no picture wanted here)
        client = await self._client(0 if meta.get("priority") == BACKGROUND else CALL_WAIT)
        result = await client.call_tool(name, args or {}, meta=meta)
        for block in result.content:
            if getattr(block, "type", None) == "text":
                return block.text
        return json.dumps({"error": f"The service's {name} gave no answer."})

    async def _with_images(self, name: str, args: dict | None, meta: dict | None):
        """(the call's result, its text block, the meta it was asked with): asked with `chart_image: "png"` and, unless `meta` says,
        `chart_dark` the bot's own theme now."""
        meta = {**(meta or {}), "chart_image": chart_images.IMAGE}
        if chart_images.asked_dark(meta) is None:
            meta["chart_dark"] = bot_dark()
        client = await self._client(0 if meta.get("priority") == BACKGROUND else CALL_WAIT)
        result = await client.call_tool(name, args or {}, meta=meta)
        text = next((b.text for b in result.content if getattr(b, "type", None) == "text"), None)
        return result, text, meta

    async def call_images(self, name: str, args: dict | None = None, meta: dict | None = None) -> tuple[dict | str, list[ChartImage]]:
        """(the tool's answer as data, each of its charts as the service drew it). The answer's charts are the full JSON from the
        result's _meta (the text carries only their titles), its chart_note the one without images; the answer's text as it came when it
        isn't a JSON object. A service that sent no image (no Pillow there) gives none: the bot draws nothing."""
        result, text, meta = await self._with_images(name, args, meta)
        if text is None:
            return {"error": f"The service's {name} gave no answer."}, []
        out = _object(text)
        if out is None:
            return text, []
        extra = result.meta or {}
        if isinstance(full := extra.get(chart_images.CHARTS_META), list):
            out["charts"] = full
            if "chart_note" in out:
                out["chart_note"] = CHART_NOTE
        charts = charts_of(out)
        pngs = [base64.b64decode(b.data) for b in result.content if getattr(b, "type", None) == "image"]
        if charts and len(pngs) != len(charts):
            log.warning("ecobot-mcp: %s sent %d chart images for %d charts (%s); the answer goes without them", name, len(pngs),
                        len(charts), out.get("chart_image_note") or "no note")
            return out, []
        dark = extra.get(chart_images.DARK_META)
        dark = dark if isinstance(dark, bool) else meta["chart_dark"]
        return out, [ChartImage(png, chart, dark) for png, chart in zip(pngs, charts)]

    async def chart_image(self, name: str, args: dict | None = None, meta: dict | None = None,
                          alone: bool = False) -> tuple[dict, ChartImage | None]:
        """(the tool's answer as data, its first chart as the service drew it; None without a chart, or with more than one when
        `alone`, or when the service sent no image)."""
        out, images = await self.call_images(name, args, meta)
        if not isinstance(out, dict):
            return {"error": f"The service's {name} gave no answer."}, None
        charts = charts_of(out)
        if not charts or (alone and len(charts) != 1) or not images:
            return out, None
        return out, images[0]

    async def picture(self, chart: dict, dark: bool | None = None) -> ChartImage:
        """A chart's JSON drawn by the service: its drawn_by replayed (as drawn: the same chart, with any newer reading). ChartNotDrawn
        when it can't be (it has no drawn_by, the service draws no chart of it, sends no image, or can't be reached): the bot draws
        nothing itself."""
        dark = bot_dark() if dark is None else dark
        by = chart.get("drawn_by")
        if not isinstance(by, dict) or not by.get("tool"):
            raise ChartNotDrawn("the chart has no drawn_by for the service to draw it again")
        try:
            out, image = await asyncio.wait_for(   # (an alert waits on this: nothing fetched for it, and not for long)
                self.chart_image(by["tool"], by.get("arguments") or {}, {"chart_points": AS_DRAWN, "chart_dark": dark, "fetch": False}),
                PICTURE_WAIT)
        except Exception as e:   # (the service away, or its answer broken)
            raise ChartNotDrawn(f"{type(e).__name__}: {e}") from e
        if image is None:
            raise ChartNotDrawn(f"{by['tool']} drew no chart again: {out.get('error') or out.get('chart_image_note') or 'no image'}")
        return image

    async def status_picture(self) -> bytes | None:
        """ecobot://status's PNG (its blob); None when it sends none (no station, or no Pillow there)."""
        client = await self._client(CALL_WAIT)
        read = await client.read_resource(STATUS_URI)
        for content in read.contents:
            if getattr(content, "blob", None) and getattr(content, "mime_type", None) == chart_images.PNG:
                return base64.b64decode(content.blob)
        return None

    async def prompt(self, name: str, arguments: dict[str, str] | None = None) -> str:
        """A prompt's question in words: the text of its messages, its `arguments` filled in (the prompt's defaults for those not given). A
        refusal (MCPError: no such prompt, a required argument missing) is raised."""
        client = await self._client(CALL_WAIT)
        got = await client.get_prompt(name, arguments or None)
        return "\n\n".join(m.content.text for m in got.messages if getattr(m.content, "type", None) == "text").strip()

    async def resource(self, uri: str, wait=AS_A_CALL) -> dict:
        """A resource's content; `wait`: how long to wait for the service (CALL_WAIT; None as long as it takes)."""
        client = await self._client(CALL_WAIT if wait is AS_A_CALL else wait)
        read = await client.read_resource(uri)
        return json.loads(read.contents[0].text)

    @property
    def names(self) -> set[str]:
        return set(self.station.get("sources") or {})

    def catalogue(self):
        if self._catalogue is None:
            from .service.names import Catalogue
            sensors = self.station.get("sensors") or {}
            self._catalogue = Catalogue.listed([r for s in sensors.values() for r in s.get("readings") or []])
        return self._catalogue

    def instructions(self) -> str:
        return self._instructions

    def air_link(self) -> tuple[str, str] | None:
        url = self.station.get("airgradient_dashboard")
        return (AIR_LINK_LABEL, url) if url else None

    async def alert(self, event_id: int) -> dict | None:
        try:
            return await asyncio.wait_for(self.alerts.get(event_id), CALL_WAIT)
        except Exception as e:   # (not reached in time, or refused: the alert's type's own chart is drawn instead)
            log.debug("ecobot-mcp: alert %s couldn't be read (%s: %s)", event_id, type(e).__name__, e)
            return None

    async def close(self):
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)


# ---------- the irrigation check's inputs (docs 3.9) ----------
class ServiceRain:
    """The measured rain as the irrigation check asks the service for it: summary {readings: ["rain"], period: "last N hours"}'s total."""

    def __init__(self, weather):
        self.weather = weather

    async def rain_mm(self, hours: int = 24) -> float | None:
        out = json.loads(await self.weather.call_text("summary", {"readings": ["rain"], "period": f"last {hours} hours"}))
        if "error" in out:
            raise ValueError(out["error"])
        return out.get("rain_total_mm")


class ServiceForecast:
    """The forecast as the irrigation check reads it, from ecobot://forecast (its days, each with its chance of at least 1 mm, and when it
    was fetched), as lib/forecast's source gives them (`days`, `fetched_at`, `now()`, `upcoming()`). `refresh()` reads it again; when the
    service can't be reached the forecast last read stands."""

    def __init__(self, weather):
        self.weather = weather
        self.days: list[dict] | None = None
        self.fetched_at: float = 0
        self.place: str | None = None

    async def refresh(self):
        data = await self.weather.resource(FORECAST_URI)
        days = []
        for d in data.get("days") or []:
            try:
                days.append({**d, "date": date.fromisoformat(str(d["date"])[:10])})
            except (KeyError, ValueError):
                continue
        fetched = data.get("fetched_at")
        self.days, self.place = days, data.get("place")
        self.fetched_at = datetime.fromisoformat(fetched).timestamp() if fetched else 0

    def now(self) -> datetime:
        return now_local(self.weather.tz)

    def upcoming(self, count: int = 2) -> list[dict]:
        today = self.now().date()
        return [d for d in (self.days or []) if d["date"] >= today][:count]


__all__ = ["ChartImage", "ChartNotDrawn", "Prompt", "RemoteWeather", "ServiceForecast", "ServiceRain", "ServiceUnreachable", "UNREACHABLE", "value"]
