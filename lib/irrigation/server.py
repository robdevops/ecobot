"""irrigation-mcp's MCP server (docs/ecobot-mcp.md 3.9): the irrigation controller (lib/irrigation/tuya.py) over MCP, with the same
low-level SDK as ecobot-mcp's (lib/mcp_server.py), as Streamable HTTP behind the same bearer token check (lib/mcp_http.py).

  - **Tools**: `irrigation_status` (readOnlyHint): the valve on or off, the run's seconds left (`countdown`), the weather delay and whether
    it pauses the timer, the battery, the work state, and the watchdog's `off_at` while a run is watched; `water {minutes: 1..60}`,
    refused with an error that says so while a weather delay is on, the watchdog armed first (a command whose reply is lost may still
    have opened the valve); `stop` (the valve off, the watchdog disarmed); `delay {duration: 24h|48h|72h|cancel}`. The last three carry
    destructiveHint, so a client asks before it calls them. Each answers JSON as one text block, as ecobot-mcp's tools do; a refusal,
    or the controller unreachable (IrrigationError's message, safe to show), is `isError` with {"error": ...}, a refusal with
    "refused" too: "paused" (water while a weather delay is on) or "arguments".
  - **Resources**: `irrigation://alerts` (and its template with `since` and `expired`): the alerts its daily check decides
    (irrigation_battery, irrigation_pause), in ecobot://alerts' shape, from an alert log of its own (lib/alert_log.py:
    irrigation_alerts.sqlite, its own identity), published as ResourceUpdated on each new one to whoever listens.
  - **HTTP** (`http_app`): `streamable_http_app` at /mcp, its Host header checked against IRRIGATION_ALLOWED_HOSTS, each request's
    bearer token against AUTH_TOKENS (lib/mcp_http.TokenCheck), one line per request in the log (RequestLog).

`IrrigationServer(device, watchdog, alert_log)` builds it; `notify(text, kind=...)` is the daily check's way to alert (it appends to the
alert log); `run()` publishes the alerts. The watchdog and the daily check themselves are run by irrigation_mcp."""

import json
import logging
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import mcp_types as types
from mcp.server.auth.settings import AuthSettings
from mcp.server.lowlevel import Server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ResourceUpdated
from mcp.shared.exceptions import MCPError

from ..mcp_http import RequestLog, TokenCheck, alerts_read, host_settings
from .common import DELAYS, MAX_MINUTES, IrrigationError, is_paused

log = logging.getLogger(__name__)
requests_log = logging.getLogger(__name__ + ".requests")   # one line per HTTP request (RequestLog)

NAME = "irrigation"
JSON = "application/json"
RESOURCE_NOT_FOUND = -32002           # MCP's error for a resource there is none of
ALERTS_URI = "irrigation://alerts"
ALERTS_TEMPLATE = "irrigation://alerts{?since,expired}"
INSTRUCTIONS = ("The garden's irrigation controller (a Tuya smart irrigation timer). irrigation_status reads it; water opens the valve "
                "for some minutes (refused while a weather delay pauses the timer: cancel the delay first), stop closes it, delay pauses "
                "the timer's own schedule for 24, 48 or 72 hours (cancel ends a pause). A run always ends: the server closes the valve "
                "itself if the controller hasn't. Once a day it pauses the timer for 24 hours when it rained or rain is likely, and "
                "warns of a low battery: irrigation://alerts.")
PAUSED = ("The irrigation is paused (weather delay {delay}): it can't water while paused. Cancel the pause first (delay with duration "
          "cancel), then water.")
ALERTS = {   # kind: (title, how long it is news, in seconds)
    "irrigation_battery": ("Irrigation battery low", 24 * 3600),   # (until the next day's check, which says it again if still low)
    "irrigation_pause": ("Irrigation paused for rain", 24 * 3600),   # (the pause's own length)
}

TOOLS = {   # name: (description, input schema, annotations)
    "irrigation_status": (
        "The irrigation controller now: `switch` (the valve on or off), `countdown` (seconds left of the run), `weather_delay` (24h, 48h, "
        "72h, or cancel for none) and `paused` (a weather delay pauses the timer), `battery_percentage`, `work_state` (auto, manual or "
        "idle), and `off_at` (when the server closes the valve itself, while a run it started is on).",
        {"type": "object", "properties": {}, "additionalProperties": False},
        types.ToolAnnotations(title="Irrigation status", read_only_hint=True, open_world_hint=False)),
    "water": (
        f"Open the valve for `minutes` (1 to {MAX_MINUTES}); the run ends by itself, and the server closes the valve if the controller "
        "doesn't. Refused while a weather delay pauses the timer (cancel it first with delay).",
        {"type": "object", "properties": {"minutes": {"type": "integer", "minimum": 1, "maximum": MAX_MINUTES,
                                                      "description": f"How long to water, 1 to {MAX_MINUTES} minutes."}},
         "required": ["minutes"], "additionalProperties": False},
        types.ToolAnnotations(title="Water", read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)),
    "stop": (
        "Close the valve now, ending any run.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        types.ToolAnnotations(title="Stop watering", read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                              open_world_hint=False)),
    "delay": (
        "Pause the timer's own schedule (a weather delay) for 24h, 48h or 72h; cancel ends a pause.",
        {"type": "object", "properties": {"duration": {"type": "string", "enum": list(DELAYS),
                                                       "description": "24h, 48h or 72h; cancel to end a pause."}},
         "required": ["duration"], "additionalProperties": False},
        types.ToolAnnotations(title="Pause the schedule", read_only_hint=False, destructive_hint=True, idempotent_hint=True,
                              open_world_hint=False)),
}


class Refused(Exception):
    """A call refused (its arguments, or the timer paused): its message is the answer's error, `why` its "refused"."""

    def __init__(self, message: str, why: str = "arguments"):
        super().__init__(message)
        self.why = why


def _result(data: dict) -> types.CallToolResult:
    """A tool's answer as tools/call gives it: the JSON as one text block; `isError` for a refusal ({"error": ...})."""
    return types.CallToolResult(content=[types.TextContent(text=json.dumps(data, ensure_ascii=False))], is_error="error" in data)


def _check_arguments(name: str, args: dict):
    """The arguments as the tool's schema has them, or Refused saying what is wrong."""
    _, schema, _ = TOOLS[name]
    allowed = schema["properties"]
    if extra := [k for k in args if k not in allowed]:
        raise Refused(f"{name} takes {', '.join(allowed) or 'no arguments'}, not {', '.join(extra)}.")
    if missing := [k for k in schema.get("required", ()) if k not in args]:
        raise Refused(f"{name} needs {', '.join(missing)}.")
    if name == "water":
        minutes = args["minutes"]
        if isinstance(minutes, float) and minutes.is_integer():
            minutes = int(minutes)
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= MAX_MINUTES:
            raise Refused(f"minutes is a whole number from 1 to {MAX_MINUTES}, not {args['minutes']!r}.")
        args["minutes"] = minutes
    if name == "delay" and args["duration"] not in DELAYS:
        raise Refused(f"duration is one of {', '.join(DELAYS)}, not {args['duration']!r}.")


class IrrigationServer:
    """irrigation-mcp's MCP server over the controller (`device`: lib/irrigation/tuya.Tuya, or a test's), its watchdog
    (lib/irrigation/watchdog.Watchdog) and its alert log (lib/alert_log.AlertLog). `tz`: the zone its times are given in (the station's,
    once ecobot-mcp has been read)."""

    def __init__(self, device, watchdog, alert_log=None, *, version: str = "", bus=None, tz=None):
        self.device, self.watchdog, self.alert_log, self.tz = device, watchdog, alert_log, tz
        self.bus = bus or InMemorySubscriptionBus()
        self.listen = ListenHandler(self.bus)
        self.server = Server(NAME, version=version, title="Irrigation controller", instructions=INSTRUCTIONS,
                             get_tool_input_schema=lambda name: TOOLS[name][1] if name in TOOLS else None,
                             on_list_tools=self._list_tools, on_call_tool=self._call_tool,
                             on_list_resources=self._list_resources, on_list_resource_templates=self._list_templates,
                             on_read_resource=self._read_resource, on_subscriptions_listen=self.listen)

    # ---------- tools ----------
    async def _list_tools(self, ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[types.Tool(name=name, description=what, input_schema=schema, annotations=hints)
                                            for name, (what, schema, hints) in TOOLS.items()])

    async def _call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name not in TOOLS:
            raise MCPError(types.INVALID_PARAMS, f"No tool {params.name!r}: the tools are {', '.join(TOOLS)}.")
        return _result(await self.call(params.name, dict(params.arguments or {})))

    async def call(self, name: str, args: dict) -> dict:
        """A tool's answer as data: what it did or read, or {"error": ...} when it was refused or the controller couldn't be reached."""
        try:
            _check_arguments(name, args)
            return await getattr(self, f"_{name}")(**args)
        except Refused as e:
            return {"error": str(e), "refused": e.why}
        except IrrigationError as e:   # (its message is safe to show: no secrets, no raw response)
            log.warning("Irrigation %s failed: %s", name, e)
            return {"error": f"The irrigation controller couldn't be reached: {e}."}

    def _time(self, epoch: float | None) -> str | None:
        return None if epoch is None else datetime.fromtimestamp(epoch, self.tz).isoformat(timespec="seconds")

    async def _irrigation_status(self) -> dict:
        status = await self.device.status()
        out = {"switch": bool(status.get("switch")), "countdown": status.get("countdown"), "weather_delay": status.get("weather_delay"),
               "paused": is_paused(status), "battery_percentage": status.get("battery_percentage"),
               "work_state": status.get("work_state")}
        if (off_at := self.watchdog.off_at) is not None:
            out["off_at"] = self._time(off_at)
        return out

    async def _water(self, minutes: int) -> dict:
        status = await self.device.status()
        if is_paused(status):
            log.info("Irrigation: water %d refused, paused for %s", minutes, status.get("weather_delay"))
            raise Refused(PAUSED.format(delay=status.get("weather_delay")), "paused")
        self.watchdog.arm(minutes)   # first: a command whose reply is lost may still have opened the valve
        await self.device.water(minutes)
        log.info("Irrigation: watering for %d minutes", minutes)
        return {"done": f"Watering for {minutes} minutes", "minutes": minutes, "off_at": self._time(self.watchdog.off_at)}

    async def _stop(self) -> dict:
        await self.device.set_switch(False)
        self.watchdog.disarm()
        log.info("Irrigation: switched off")
        return {"done": "Switched off"}

    async def _delay(self, duration: str) -> dict:
        await self.device.delay(duration)
        log.info("Irrigation: %s", "unpaused" if duration == "cancel" else f"paused {duration}")
        return {"done": "Unpaused" if duration == "cancel" else f"Paused {duration}", "weather_delay": duration}

    # ---------- alerts ----------
    async def notify(self, text: str, kind: str, **_):
        """The daily check's alert (lib/irrigation/check.py): appended to the alert log, which wakes the listeners."""
        title, lasts = ALERTS.get(kind, ("Irrigation", 24 * 3600))
        self.alert_log.append(kind, text, title, expires=self.alert_log.now() + lasts)

    async def _list_resources(self, ctx, params) -> types.ListResourcesResult:
        if self.alert_log is None:
            return types.ListResourcesResult(resources=[])
        return types.ListResourcesResult(resources=[types.Resource(
            uri=ALERTS_URI, name="alerts", title="Irrigation alerts", mime_type=JSON,
            description="The alerts the daily check has decided (irrigation_battery: the battery is low; irrigation_pause: paused for rain), "
                        "newest last, kept 30 days: {alerts: [{id, kind, time, expires, title, text, data}], last_id, identity}; only "
                        "those not yet expired (the template: since an id, and expired=true for all). Pushed on each new alert.")])

    async def _list_templates(self, ctx, params) -> types.ListResourceTemplatesResult:
        if self.alert_log is None:
            return types.ListResourceTemplatesResult(resource_templates=[])
        return types.ListResourceTemplatesResult(resource_templates=[types.ResourceTemplate(
            uri_template=ALERTS_TEMPLATE, name="alerts since", title="Irrigation alerts since an id",
            description="The alerts after the id `since` (a client's last handled), only those not yet expired unless `expired=true`.",
            mime_type=JSON)])

    async def _read_resource(self, ctx, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        uri = str(params.uri)
        return types.ReadResourceResult(contents=[types.TextResourceContents(uri=uri, mime_type=JSON,
                                                                             text=json.dumps(self.read(uri), ensure_ascii=False))])

    def read(self, uri: str) -> dict:
        """A resource's content. Raises MCPError (RESOURCE_NOT_FOUND) for one there is none of."""
        parts = urlsplit(uri)
        if f"{parts.scheme}://{parts.netloc}{parts.path}" == ALERTS_URI and self.alert_log is not None:
            return alerts_read(self.alert_log, parse_qs(parts.query))
        raise MCPError(RESOURCE_NOT_FOUND, f"No resource {uri!r}: there are {ALERTS_URI} and {ALERTS_TEMPLATE}.")

    async def watch_alerts(self):
        """Publish the alerts' URI on each new alert, until cancelled."""
        if self.alert_log is None:
            return
        last = self.alert_log.last_id()
        while True:
            await self.alert_log.wait(last)
            last = self.alert_log.last_id()
            await self.bus.publish(ResourceUpdated(ALERTS_URI))

    async def run(self):
        """The publishing, for as long as it runs."""
        await self.watch_alerts()


def http_app(server: IrrigationServer, *, tokens=(), allowed_hosts=("127.0.0.1", "localhost"), issuer_url: str = "http://127.0.0.1:8766",
             path: str = "/mcp"):
    """The Streamable HTTP app (Starlette) at `path`: the Host header checked against `allowed_hosts` (IRRIGATION_ALLOWED_HOSTS), and,
    with `tokens` (AUTH_TOKENS), every request's bearer token (TokenCheck). `issuer_url` is required by the SDK with auth but unused."""
    check = TokenCheck(tokens) if tokens else None
    app = server.server.streamable_http_app(
        streamable_http_path=path, transport_security=host_settings(allowed_hosts),
        auth=AuthSettings(issuer_url=issuer_url, resource_server_url=None) if check else None, token_verifier=check)
    app.state.tokens = check
    app.add_middleware(RequestLog, logger=requests_log, hosts_setting="IRRIGATION_ALLOWED_HOSTS")   # (outermost: it sees the refusals)
    return app


__all__ = ["ALERTS", "ALERTS_TEMPLATE", "ALERTS_URI", "IrrigationServer", "TOOLS", "http_app"]
