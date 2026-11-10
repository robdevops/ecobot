"""The weather service as the bot reaches it (docs/mcp-service.md section 4 and step 5): one interface, whether the service runs in this
process (LocalWeather: lib/service's tools over the bot's own sources, the alert log in this process; WEATHER_MCP_URL unset, as before)
or is another program reached over MCP (RemoteWeather: WEATHER_MCP_URL, a client pinned to protocol 2026-07-28 that one supervisor
task keeps connected). Everything the bot asks of the service goes through it, so the bot doesn't branch on where the service runs:

  - `service`: the tools, as lib.service.Service gives them: iterated in order, `get(name)` or `service[name]`, each with its name,
    description, parameters and `call(args, meta)`, which returns the tool's JSON text (lib/model_tools.py offers them to the model; the
    bot's shortcuts, report, gauges, charts drawn ahead and period buttons call them);
  - `names`: the sources the service has ("Ecowitt", "AirGradient", "Forecast", "Pollen"): which alerts, report sections and gauges
    there are;
  - `catalogue()`: the readings it holds (a shortcut's words are read against them); `instructions()`: the server's instructions (from
    `server/discover` when remote); `air_link()`: the AirGradient dashboard (the live-chart link under an answer about the air);
  - `alerts`: the alert feed lib/alerts/notify.AlertDelivery reads (`last_id`, `identity`, `since`, `wait`), and `alert(id)` one event
    (the 🔄 under an alert replays its chart's drawn_by);
  - `resource(uri)`: a resource's content (ecobot://station, ecobot://forecast ...);
  - `ready(wait)`: whether it can be reached (in-process, always; remote, once connected, waiting up to `wait` seconds for it).

For the irrigation check while it is still the bot's (step 5b moves it to its own server): `ServiceRain` (the measured rain, from
`summary {readings: ["rain"], period: "last 24 hours"}`) and `ServiceForecast` (ecobot://forecast, the last read kept when the service
can't be reached), docs 3.9."""

import asyncio
import json
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .service.instructions import instructions as service_instructions
from .timeutil import now_local

log = logging.getLogger(__name__)

STATION_URI, ALERTS_URI, FORECAST_URI = "ecobot://station", "ecobot://alerts", "ecobot://forecast"   # (as lib/mcp_server.py's)
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


async def value(x):
    """x, awaited when it is awaitable: the alert feed's methods are plain in-process and coroutines when remote."""
    return await x if asyncio.iscoroutine(x) or isinstance(x, asyncio.Future) else x


# ---------- in-process ----------
class LocalWeather:
    """The service in this process: its tools (a lib.service.Service, built over `sources` on first use when none is given), the sources
    it runs over, the alert log."""
    is_weather, remote = True, False

    def __init__(self, sources=(), service=None, alert_log=None, tz=None):
        self.sources, self.alert_log, self.tz = list(sources), alert_log, tz
        self.by_name = {s.name: s for s in self.sources}
        self._service = service

    @property
    def service(self):
        if self._service is None:
            from .service import Service
            from .service.context import Context
            get = self.by_name.get
            self._service = Service.of(Context(eco=get("Ecowitt"), air=get("AirGradient"), forecast=get("Forecast"), pollen=get("Pollen"),
                                               tz=self.tz))
        return self._service

    def __iter__(self):   # (the tools, as the service lists them: lib/model_tools.py follows the list)
        return iter(self.service)

    @property
    def names(self) -> set[str]:
        return set(self.by_name)

    @property
    def ctx(self):
        return getattr(self.service, "ctx", None)

    def catalogue(self):
        if (ctx := self.ctx) is not None:
            return ctx.readings()
        from .service.names import Catalogue
        return Catalogue.of(station="Ecowitt" in self.by_name, air="AirGradient" in self.by_name)

    def instructions(self) -> str:
        return service_instructions(self.ctx)

    def air_link(self) -> tuple[str, str] | None:
        return getattr(self.by_name.get("AirGradient"), "link", None)

    @property
    def alerts(self):
        return self.alert_log

    async def alert(self, event_id: int) -> dict | None:
        return self.alert_log.get(event_id) if self.alert_log is not None else None

    async def resource(self, uri: str) -> dict:
        from .mcp_server import EcobotServer
        return await EcobotServer(self.ctx, self.alert_log).read(uri)

    async def ready(self, wait: float = 0) -> bool:
        return True


# ---------- over MCP ----------
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
    """The service's alert log read over MCP (ecobot://alerts and its template with `since`), as AlertDelivery reads the log in process.
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
        raised."""
        from mcp.shared.exceptions import MCPError
        uri = f"{ALERTS_URI}?since={int(since)}" + ("&expired=true" if expired else "")
        while True:
            try:
                data = await self.weather.resource(uri, wait=None)
            except MCPError:
                raise
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


class RemoteWeather:
    """The service at `target` (its MCP URL; in tests, a Server in-process), as the bot's interface. `run()` is the supervisor (the
    bridge's Upstream: one client, discovered by hand for the instructions, listening; on a transport error or a lost or ended listen it
    is dropped and built again after a backoff, which discovers and listens again). Each new connection reads the tool list and the
    station; the listen's events keep them current (ToolsListChanged, the station's and the forecast's ResourceUpdated) and wake the
    alert delivery (the alerts'). A call while it is being reached waits up to CALL_WAIT seconds (a background one not at all) and is
    then refused with ServiceUnreachable."""
    is_weather, remote = True, True

    def __init__(self, target, token: str | None = None, tz=None):
        from ecobot_service.bridge import Upstream
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
        """A new connection listens: the tools and the station read again, the alerts looked at (what came while it was down is not
        replayed), the forecast read again."""
        for refresh in (self._read_tools, self._read_station):
            try:
                await refresh()
            except Exception as e:   # (the connection is still good: the listen's events refresh them later)
                log.warning("Weather service: couldn't read %s (%s: %s)", refresh.__name__.removeprefix("_read_"), type(e).__name__, e)
        self.alerts.changed()
        self._soon(self.forecast.refresh)
        self.listening.set()

    async def _event(self, event):
        from mcp.shared.subscriptions import ResourceUpdated, ToolsListChanged
        if isinstance(event, ToolsListChanged):
            self._soon(self._read_tools)
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
                log.debug("Weather service: %s failed (%s: %s)", getattr(fn, "__name__", fn), type(e).__name__, e)
        task = asyncio.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _read_tools(self):
        client = await self._client(0)
        self.service = RemoteTools(RemoteTool(self, t) for t in (await client.list_tools()).tools)

    async def _read_station(self):
        station = await self.resource(STATION_URI, wait=0)
        self.station, self._catalogue = station, None
        if zone := station.get("time_zone"):
            try:
                self.station_tz = ZoneInfo(zone)
            except Exception:   # (an offset only: TZ stands)
                pass

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
        client = await self._client(0 if meta.get("priority") == BACKGROUND else CALL_WAIT)
        result = await client.call_tool(name, args or {}, meta=meta or None)
        for block in result.content:
            if getattr(block, "type", None) == "text":
                return block.text
        return json.dumps({"error": f"The service's {name} gave no answer."})

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
            log.debug("Weather service: alert %s couldn't be read (%s: %s)", event_id, type(e).__name__, e)
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


__all__ = ["LocalWeather", "RemoteWeather", "ServiceForecast", "ServiceRain", "ServiceUnreachable", "UNREACHABLE", "value"]
