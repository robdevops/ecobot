"""ecobot-mcp as the irrigation check asks it (docs/ecobot-mcp.md 3.9): the rain measured and the forecast.

  - `ServiceRain`: the measured rain, `summary {readings: ["rain"], period: "last 24 hours"}`'s total;
  - `ServiceForecast`: ecobot://forecast (its days, each with its chance of at least 1 mm, and when it was fetched), read again at each
    check; when ecobot-mcp can't be reached the one last read stands;
  - `EcobotWeather`: irrigation-mcp's client of ecobot-mcp (IRRIGATION_ECOBOT_MCP_URL), kept by lib/upstream.Upstream (pinned, listening
    to the station and the forecast): `call_text`, `resource`, `tz` (ecobot://station's time zone once read, TZ before; `on_zone(zone)`
    is called when it is first read and when it changes), `rain` and `forecast` (the two above, over it).

ServiceRain and ServiceForecast work over either client: this one, or the bot's (lib/weather.RemoteWeather) while it runs the check
itself (until step 8b). Nothing here imports ecobot-mcp's own code: only the MCP client."""

import asyncio
import json
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..timeutil import now_local

log = logging.getLogger(__name__)

STATION_URI, FORECAST_URI = "ecobot://station", "ecobot://forecast"   # (as lib/mcp_server.py's)


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


class EcobotWeather:
    """irrigation-mcp's client of ecobot-mcp at `target` (its MCP URL; in tests, a Server in memory), sending `token` (a string, a
    function giving it for each connection, or None). `run()` is the supervisor (lib/upstream.Upstream). Each new connection reads the
    station (its time zone) and the forecast; the listen keeps them current. A call while ecobot-mcp can't be reached waits for it up
    to lib/upstream.CONNECT_WAIT seconds, then fails (MCPError): the check then goes on the forecast last read, without the measured
    rain."""

    def __init__(self, target, token=None, tz=None):
        from ..upstream import Upstream
        self.target, self.fallback_tz, self.station_tz = target, tz, None
        self.upstream = Upstream(target, token, on_event=self._event, on_listening=self._listening, resources=(STATION_URI, FORECAST_URI))
        self.rain = ServiceRain(self)
        self.forecast = ServiceForecast(self)
        self.on_zone = None   # on_zone(zone): the station's time zone, when it is first read and when it changes
        self.listening = asyncio.Event()   # the first connection listens, its station read
        self._background: set[asyncio.Task] = set()

    @property
    def tz(self):
        """The station's time zone (ecobot://station's), TZ until it has been read."""
        return self.station_tz or self.fallback_tz

    async def run(self):
        await self.upstream.run()

    async def close(self):
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)

    # ---------- the connection ----------
    async def _listening(self):
        try:
            await self._read_station()
        except Exception as e:   # (the listen's events read it again later)
            log.warning("ecobot-mcp: couldn't read the station (%s: %s)", type(e).__name__, e)
        self._soon(self.forecast.refresh)
        self.listening.set()

    async def _event(self, event):
        from mcp.shared.subscriptions import ResourceUpdated
        if isinstance(event, ResourceUpdated):
            uri = str(event.uri)
            if uri == STATION_URI:
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

    async def _read_station(self):
        station = await self.resource(STATION_URI)
        if zone := station.get("time_zone"):
            try:
                read = ZoneInfo(zone)
            except Exception:   # (an offset only: TZ stands)
                return
            first_or_new, self.station_tz = str(read) != str(self.station_tz), read
            if first_or_new and self.on_zone:
                self.on_zone(read)

    # ---------- what the check asks ----------
    async def call_text(self, name: str, args: dict | None = None) -> str:
        """A tool's answer as its JSON text (its first text block; a refusal's too)."""
        client = await self.upstream.get()
        result = await client.call_tool(name, args or {})
        for block in result.content:
            if getattr(block, "type", None) == "text":
                return block.text
        return json.dumps({"error": f"ecobot-mcp's {name} gave no answer."})

    async def resource(self, uri: str) -> dict:
        client = await self.upstream.get()
        read = await client.read_resource(uri)
        return json.loads(read.contents[0].text)


__all__ = ["EcobotWeather", "ServiceForecast", "ServiceRain"]
