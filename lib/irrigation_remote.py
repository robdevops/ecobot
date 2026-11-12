"""irrigation-mcp as the bot reaches it (docs/ecobot-mcp.md 3.9, step 8b): the irrigation controller is irrigation-mcp's (python -m
irrigation_mcp), and the bot its client over MCP at BOT_IRRIGATION_MCP_URL (RemoteIrrigation: lib/upstream.Upstream, the same token it
sends ecobot-mcp). The bot's Irrigation button, its menu and every ir: button (lib/bot.py, lib/irrigation_menu.py) call its tools through
the methods the bot always called on the controller:

  - `status()`: irrigation_status ({switch, countdown, weather_delay, paused, battery_percentage, work_state, off_at});
  - `water(minutes)`, `delay(duration)`, `set_switch(False)` (stop): irrigation-mcp watches a run and closes the valve itself;
  - each raises IrrigationError (lib/irrigation/common.py) when irrigation-mcp can't be reached within CALL_WAIT seconds, or answers
    with an error (the controller unreachable, a refusal): the bot says it couldn't reach the controller;
  - `alerts`: irrigation://alerts read as AlertDelivery reads an alert log (lib/weather.RemoteAlerts), woken by the listen.

Nothing here loads the Tuya client, the watchdog or the daily check: they are irrigation-mcp's."""

import asyncio
import json
import logging

from .irrigation.common import IrrigationError
from .weather import RemoteAlerts

log = logging.getLogger(__name__)

NAME = "irrigation-mcp"
ALERTS_URI = "irrigation://alerts"   # (as lib/irrigation/server.py's)
CALL_WAIT = 5.0   # how long a press waits for irrigation-mcp while it is being reached again, before it is told it can't be
UNREACHABLE = "irrigation-mcp isn't reachable"


class RemoteIrrigation:
    """irrigation-mcp at `target` (its MCP URL; in tests, a Server in memory), sending `token` (a string, a function giving it for each
    connection, or None). `run()` is the supervisor (lib/upstream.Upstream): it keeps a connection, listening to irrigation://alerts."""
    remote = True

    def __init__(self, target, token=None):
        from .upstream import Upstream
        self.target = target
        self.upstream = Upstream(target, token, on_event=self._event, on_listening=self._listening, resources=(ALERTS_URI,), name=NAME)
        self.alerts = RemoteAlerts(self, ALERTS_URI)
        self.listening = asyncio.Event()   # the first connection listens

    @property
    def url(self) -> str:
        return self.target if isinstance(self.target, str) else "in-process"

    async def run(self):
        await self.upstream.run()

    async def close(self):
        pass

    # ---------- the connection ----------
    async def _listening(self):
        """A new connection listens: the alerts looked at (what came while it was down is not replayed)."""
        self.alerts.changed()
        self.listening.set()

    async def _event(self, event):
        from mcp.shared.subscriptions import ResourceUpdated
        if isinstance(event, ResourceUpdated) and str(event.uri) == ALERTS_URI:
            self.alerts.changed()

    async def _client(self, wait: float | None):
        """The connected client, waiting up to `wait` seconds for one (None: as long as it takes); IrrigationError after."""
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
            raise IrrigationError(f"{UNREACHABLE} ({self.url})")
        return up.client

    async def resource(self, uri: str, wait: float | None = CALL_WAIT) -> dict:
        """A resource's content (irrigation://alerts...); `wait`: how long to wait for irrigation-mcp (None: as long as it takes)."""
        client = await self._client(wait)
        read = await client.read_resource(uri)
        return json.loads(read.contents[0].text)

    # ---------- the controller ----------
    async def call(self, name: str, args: dict | None = None) -> dict:
        """A tool's answer as data; IrrigationError when irrigation-mcp can't be reached or the tool answers with an error."""
        client = await self._client(CALL_WAIT)
        try:
            result = await client.call_tool(name, args or {})
        except Exception as e:   # (the connection lost mid-call, or a protocol error: the message names no secret)
            raise IrrigationError(f"{NAME}'s {name} failed ({type(e).__name__}: {e})") from e
        text = next((b.text for b in result.content if getattr(b, "type", None) == "text"), None)
        try:
            data = json.loads(text) if text is not None else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise IrrigationError(f"{NAME}'s {name} gave no answer")
        if result.is_error or "error" in data:
            raise IrrigationError(str(data.get("error") or f"{NAME}'s {name} failed"))
        return data

    async def status(self) -> dict:
        return await self.call("irrigation_status")

    async def water(self, minutes: int):
        await self.call("water", {"minutes": int(minutes)})

    async def delay(self, duration: str):
        await self.call("delay", {"duration": duration})

    async def set_switch(self, on: bool):
        """Off (stop): there is no On, a run is always timed (water)."""
        if on:
            raise IrrigationError("The valve is opened only for a timed run (water)")
        await self.call("stop")


__all__ = ["ALERTS_URI", "CALL_WAIT", "RemoteIrrigation"]
