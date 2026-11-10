"""The service over MCP (docs/mcp-service.md 3.4-3.8): the SDK's low-level Server serving the ten tools (lib/service), the resources and
prompts, the server's instructions (lib/service/instructions.py) and the `subscriptions/listen` stream, as Streamable HTTP behind a bearer
token check.

  - **Tools**: each ServiceTool as it is, its hand-written input schema and description, `readOnlyHint` and `openWorldHint` from it. The
    list is built when asked, from the catalogue of readings the file holds: when that gains a reading (`refresh_catalogue`, every
    CATALOGUE_SECONDS) the tools are built again and ToolsListChanged is published. A call runs the ServiceTool with the request's
    `_meta` (`chart_points`, `fetch`, `priority`: common.request_meta) and answers its JSON as one text block, unchanged: its charts are
    the full chart JSON (3.7), sized by `chart_points`. A refusal is `isError`. A complete answer (3.7: `chart_answers_it` and a chart)
    also carries its `reply` as a second text block for the person (`audience: ["user"]`) and result `_meta` {"ecobot/complete": true}.
  - **Resources** (3.4): RESOURCES, and the alerts with `since` and `expired` (the ALERTS_TEMPLATE). Each is JSON. The live ones are
    published as ResourceUpdated when they change (`publish_changes`, every PUBLISH_SECONDS, from what the sources keep: no request is
    made for it; a reading asked for again and unchanged is not sent), the alerts on each new event (`watch_alerts`, lib/alert_log.py
    `wait`), the station when the catalogue gains a reading.
  - **Prompts** (3.5): PROMPTS, each a question in words with its period an argument; no logic.
  - **HTTP** (`http_app`): `streamable_http_app` at /mcp, its Host header checked against the allowed hosts
    (TransportSecuritySettings: 127.0.0.1 and localhost unless MCP_ALLOWED_HOSTS says), each request's bearer token against
    MCP_TOKENS (TokenCheck: a constant-time compare, one token per client, so one can be revoked alone).

`EcobotServer(ctx, alert_log)` builds it over the service's Context; `run()` is its publishing, for as long as it runs. Nothing here
fetches, alerts or writes: ecobot_service runs the sources and the rules."""

import asyncio
import hmac
import json
import logging
import os
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import mcp_types as types
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.lowlevel import Server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ResourceUpdated, ToolsListChanged
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError

from .config import ROOT
from .service import Service
from .service.about import AboutTool, alerts as alert_kinds
from .service.common import iso
from .service.current import CurrentTool
from .service.instructions import instructions
from .service.names import AIR_DEVICE, STATION, Catalogue

log = logging.getLogger(__name__)

NAME = "ecobot"
COMPLETE = "ecobot/complete"          # result _meta of a complete answer (3.7)
RESOURCE_NOT_FOUND = -32002           # MCP's error for a resource there is none of
PUBLISH_SECONDS = 2.0                 # how often the live resources are compared with what was last published
CATALOGUE_SECONDS = 60.0              # how often the file's catalogue of readings is read again
SCHEMA_PATH = ROOT / "schemas" / "chart.json"
JSON = "application/json"

# ---------- resources (3.4) ----------
STATION_URI, WEATHER_URI, AIR_URI = "ecobot://station", "ecobot://now/weather", "ecobot://now/air"
ALERTS_URI, FORECAST_URI, POLLEN_URI = "ecobot://alerts", "ecobot://forecast", "ecobot://pollen"
HEALTH_URI, SCHEMA_URI = "ecobot://health", "ecobot://schema/chart"
ALERTS_TEMPLATE = "ecobot://alerts{?since,expired}"
RESOURCES = {   # uri: (name, title, description, the source it needs: None for always)
    STATION_URI: ("station", "The station", "Its name, time zone, location and when it was set up; the sensors with their readings (name, "
                  "label, unit) and the words people use for them; each source's mode and how far back its history goes; the alert "
                  "kinds, each with its rule and thresholds; the AirGradient dashboard link. Pushed when the readings change.", None),
    WEATHER_URI: ("now/weather", "The weather now", "The weather station's live reading: {as_of, sensors: {sensor: {reading: {value, "
                  "unit, text, [emoji], [compass]}}}, lines, heading}. Pushed on each new reading (every 5 s with Home Assistant, 60 s "
                  "from the cloud).", "eco"),
    AIR_URI: ("now/air", "The air now", "The air sensor's latest reading, with each reading's rating, PM2.5's US AQI and band: {as_of, "
              "sensors: {air: {reading: {value, unit, text, [rating], [aqi_us, band]}}}, lines, heading}. Pushed on each new reading.",
              "air"),
    ALERTS_URI: ("alerts", "Alerts", "The alerts the service has decided, newest last, kept 30 days: {alerts: [{id, kind, time, expires, "
                 "title, text, data, [link], [chart]}], last_id, identity}; only those not yet expired (the template: since an id, and "
                 "expired=true for all). Pushed on each new alert.", None),
    FORECAST_URI: ("forecast", "The forecast", "The forecast days, structured: {place, fetched_at, days: [{date, min_c, max_c, "
                   "rain_chance_pct, rain_1mm_pct, summary, text}]}. Pushed on each new forecast.", "forecast"),
    POLLEN_URI: ("pollen", "Pollen and thunderstorm asthma", "Grass pollen and thunderstorm asthma levels: {in_season, fetched_at, "
                 "grass: {level, emoji, date}, asthma: {level, emoji, updated}, lines}. Pushed on each new page.", "pollen"),
    HEALTH_URI: ("health", "Health", "Each loop's last success and failure, the archive's progress, the readings file's size and the "
                 "alert log's newest id. Not pushed.", None),
    SCHEMA_URI: ("schema/chart", "Chart JSON Schema", "The JSON Schema of the charts in the tools' results and the alerts "
                 "(schemas/chart.json, versioned). Not pushed.", None),
}
PUSHED = (WEATHER_URI, AIR_URI, FORECAST_URI, POLLEN_URI)   # compared every PUBLISH_SECONDS (the alerts and the station have their own)

# ---------- prompts (3.5) ----------
PERIOD = types.PromptArgument(name="period", description="The period in your own words (\"this week\", \"last 3 months\", \"yesterday\")",
                              required=False)
PROMPTS = {   # name: (description, arguments, the question: a format string over the arguments, with their defaults)
    "now": ("The weather and the air now.", [], "How is it outside now?"),
    "report": ("A report: the readings now, today's forecast and the pollen.", [],
               "Give me a report: the readings now, today's forecast and the pollen."),
    "will_it_rain": ("Will it rain in the next few hours: the station's read and the forecast.", [], "Will it rain in the next few hours?"),
    "weather": ("Every reading of the weather station over a period, a panel each.", [PERIOD],
                "Chart every weather reading, a panel each, over {period}."),
    "chart": ("A chart of a reading over a period.",
              [types.PromptArgument(name="reading", description="The reading (\"temperature\", \"pressure\", \"pm2.5\")", required=True),
               PERIOD], "Chart {reading} over {period}."),
    "rain": ("How much rain fell over a period, and when.", [PERIOD], "How much rain fell over {period}?"),
    "air": ("The air quality, now or over a period.", [PERIOD], "How was the air quality over {period}?"),
}
PROMPT_DEFAULTS = {"weather": {"period": "the last 7 days"}, "chart": {"period": "the last 24 hours"}, "rain": {"period": "today"}}
WITHOUT_ARGUMENTS = {"air": "How is the air quality now?"}   # the question when no argument is sent


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=_plain)


def _plain(x):
    """What JSON lacks: a date or a time as ISO."""
    if isinstance(x, (date, datetime)):
        return x.isoformat()
    raise TypeError(f"{type(x).__name__} is not JSON")


# ---------- a tool's answer as MCP's ----------
def tool_entry(tool) -> types.Tool:
    """A ServiceTool as tools/list gives it: its name, description, hand-written input schema and annotations."""
    return types.Tool(name=tool.name, description=tool.description, input_schema=tool.parameters,
                      annotations=types.ToolAnnotations(read_only_hint=tool.read_only, open_world_hint=tool.open_world))


def tool_result(text: str) -> types.CallToolResult:
    """A ServiceTool's answer as tools/call gives it: the JSON as one text block, unchanged; `isError` for a refusal; a complete answer
    (3.7) with its reply for the person as a second block (audience user) and result _meta {COMPLETE: true}."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    content: list = [types.TextContent(text=text)]
    meta = None
    if isinstance(data, dict) and data.get("complete") is True and isinstance(data.get("reply"), str) and "error" not in data:
        content.append(types.TextContent(text=data["reply"], annotations=types.Annotations(audience=["user"])))
        meta = {COMPLETE: True}
    return types.CallToolResult(content=content, is_error=isinstance(data, dict) and "error" in data, _meta=meta)


# ---------- auth ----------
class TokenCheck:
    """The service's TokenVerifier (3.8): a bearer token is one of MCP_TOKENS', compared in constant time against every one of them;
    the client it names is the AccessToken's client_id. `revoke(name)` takes one out while the service runs (MCP_TOKENS without it does
    the same at the next start)."""

    def __init__(self, tokens: dict[str, str] | tuple[tuple[str, str], ...]):
        self.tokens = dict(tokens)

    def revoke(self, name: str):
        self.tokens.pop(name, None)

    async def verify_token(self, token: str) -> AccessToken | None:
        found = None
        sent = (token or "").encode()
        for name, known in list(self.tokens.items()):   # (every one compared: the time says nothing of which matched)
            if hmac.compare_digest(sent, known.encode()):
                found = name
        return AccessToken(token=token, client_id=found, scopes=[]) if found else None


def host_settings(allowed: tuple[str, ...] | list[str]) -> TransportSecuritySettings:
    """The Host (and Origin) headers the service answers: each name allowed with any port, unless it names one."""
    hosts, origins = [], []
    for name in allowed:
        bare = name.startswith("[") and name.endswith("]") or ":" not in name.rsplit("]", 1)[-1]
        hosts += [name, f"{name}:*"] if bare else [name]
        origins += [f"{scheme}://{h}" for scheme in ("http", "https") for h in ([name, f"{name}:*"] if bare else [name])]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


# ---------- the server ----------
class EcobotServer:
    """The service's MCP server over a Context (lib/service/context.py) and the alert log. health: a callable giving the service's own
    health figures (ecobot_service's loops), merged into ecobot://health."""

    def __init__(self, ctx, alert_log=None, *, health: Callable[[], dict] | None = None, version: str = "", bus=None):
        self.ctx, self.alert_log, self.health_of = ctx, alert_log, health
        self.bus = bus or InMemorySubscriptionBus()
        self.listen = ListenHandler(self.bus)
        self._service = Service.of(ctx)
        self._seen: dict[str, str | None] = {uri: self._fingerprint(uri) for uri in PUSHED}
        self.server = Server(NAME, version=version, title="Ecobot weather and air service", instructions=instructions(ctx),
                             get_tool_input_schema=self._schema,
                             on_list_tools=self._list_tools, on_call_tool=self._call_tool,
                             on_list_resources=self._list_resources, on_list_resource_templates=self._list_templates,
                             on_read_resource=self._read_resource,
                             on_list_prompts=self._list_prompts, on_get_prompt=self._get_prompt,
                             on_subscriptions_listen=self.listen)

    @property
    def service(self) -> Service:
        return self._service

    # ---------- tools ----------
    def _schema(self, name: str):
        tool = self._service.get(name)
        return tool.parameters if tool else None

    async def _list_tools(self, ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[tool_entry(t) for t in self._service])

    async def _call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        tool = self._service.get(params.name)
        if tool is None:
            raise MCPError(types.INVALID_PARAMS, f"No tool {params.name!r}: the tools are {', '.join(t.name for t in self._service)}.")
        return tool_result(await tool.call(params.arguments or {}, dict(ctx.meta or {})))

    async def refresh_catalogue(self) -> bool:
        """Read the file's catalogue of readings again; when it gained a reading (a sensor sent a field it never sent), build the tools
        and the instructions again and publish ToolsListChanged and the station. True when it did."""
        if self.ctx.eco is None and self.ctx.air is None:
            return False
        new = await Catalogue.open(self.ctx.eco, self.ctx.air)
        if not (gained := [n for n in new.names if n not in set(self.ctx.readings().names)]):
            return False
        self.ctx.catalogue = new
        self._service = Service.of(self.ctx)
        self.server.instructions = instructions(self.ctx)
        log.info("MCP: the readings now include %s: the tools are built again", ", ".join(gained))
        await self.bus.publish(ToolsListChanged())
        await self.bus.publish(ResourceUpdated(STATION_URI))
        return True

    # ---------- resources ----------
    def _there(self, need: str | None) -> bool:
        return need is None or (need == "eco" and self.ctx.eco is not None) or (need == "air" and self.ctx.air is not None) or \
            (need == "forecast" and self.ctx.forecast is not None) or (need == "pollen" and self.ctx.pollen is not None)

    async def _list_resources(self, ctx, params) -> types.ListResourcesResult:
        return types.ListResourcesResult(resources=[
            types.Resource(uri=uri, name=name, title=title, description=what, mime_type=JSON)
            for uri, (name, title, what, need) in RESOURCES.items() if self._there(need) and (uri != ALERTS_URI or self.alert_log)])

    async def _list_templates(self, ctx, params) -> types.ListResourceTemplatesResult:
        if self.alert_log is None:
            return types.ListResourceTemplatesResult(resource_templates=[])
        return types.ListResourceTemplatesResult(resource_templates=[types.ResourceTemplate(
            uri_template=ALERTS_TEMPLATE, name="alerts since", title="Alerts since an id",
            description="The alerts after the id `since` (a client's last handled), only those not yet expired unless `expired=true`.",
            mime_type=JSON)])

    async def _read_resource(self, ctx, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        uri = str(params.uri)
        return types.ReadResourceResult(contents=[types.TextResourceContents(uri=uri, mime_type=JSON, text=_json(await self.read(uri)))])

    async def read(self, uri: str) -> dict:
        """A resource's content (a dict, sent as JSON). Raises MCPError (RESOURCE_NOT_FOUND) for one there is none of."""
        parts = urlsplit(uri)
        key = f"{parts.scheme}://{parts.netloc}{parts.path}"
        query = parse_qs(parts.query)
        if key == ALERTS_URI and self.alert_log is not None:
            return self.alerts(query)
        entry = RESOURCES.get(key)
        if entry is None or parts.query or key == ALERTS_URI or not self._there(entry[3]):
            raise MCPError(RESOURCE_NOT_FOUND, f"No resource {uri!r}: there are "
                                               f"{', '.join(u for u, e in RESOURCES.items() if self._there(e[3]))}, and {ALERTS_TEMPLATE}.")
        return await {STATION_URI: self.station, WEATHER_URI: lambda: self.now(STATION), AIR_URI: lambda: self.now(AIR_DEVICE),
                      FORECAST_URI: self.forecast, POLLEN_URI: self.pollen, HEALTH_URI: self.health, SCHEMA_URI: self.schema}[key]()

    def alerts(self, query: dict | None = None) -> dict:
        """{alerts, last_id, identity}: the alerts after `since` (0), only the unexpired unless `expired` is true."""
        query = query or {}
        since = (query.get("since") or ["0"])[-1].strip()
        expired = (query.get("expired") or ["false"])[-1].strip().lower() in ("true", "1", "yes", "on")
        if since and not since.isdecimal():
            raise MCPError(types.INVALID_PARAMS, f"`since` is an alert's id, a whole number, not {since!r}.")
        log_ = self.alert_log
        return {"alerts": log_.since(int(since or 0), expired=expired), "last_id": log_.last_id(), "identity": log_.identity}

    async def station(self) -> dict:
        """ecobot://station: what the station is and has (3.4)."""
        c, tz = self.ctx, self.ctx.zone
        catalogue = c.readings()
        about = AboutTool(c)
        sensors = {}
        for device, there in ((STATION, c.eco), (AIR_DEVICE, c.air)):
            if there:
                sensors[device] = {"readings": [{"name": r.name, "label": r.label, "unit": r.unit} for r in catalogue if r.device == device]}
        sources = {}
        if c.eco:
            sources["Ecowitt"] = {"mode": c.eco.via, "station": c.eco.station_name}
        if c.air:
            sources["AirGradient"] = {"mode": "/".join(m for m, on in (("local", c.air.local), ("homeassistant", c.air.ha),
                                                                         ("cloud", c.air.cloud)) if on) or "none"}
        if c.forecast:
            sources["Forecast"] = {"mode": "Open-Meteo", "place": c.forecast.place}
        if c.pollen:
            sources["Pollen"] = {"mode": "Melbourne Pollen", "district": c.pollen.district}
        eco = c.eco
        return {"name": eco.station_name if eco else None, "time_zone": str(tz),
                "latitude": eco.latitude if eco else None, "longitude": eco.longitude if eco else None,
                "created": iso(eco.created) if eco and eco.created else None,
                "sensors": sensors, "words": catalogue.words(), "sources": sources, "history": await about.history(),
                "alerts": alert_kinds(about.sources),
                "airgradient_dashboard": c.air.link[1] if c.air and c.air.link else None}

    async def now(self, device: str) -> dict:
        """ecobot://now/weather, ecobot://now/air: the device's reading as kept (nothing is fetched for it)."""
        part = await CurrentTool(self.ctx).now_of(device, {"fetch": False})
        out = {"as_of": iso(part.at) if part.at else None, "sensors": {}, "lines": part.lines, "heading": part.heading}
        for r, entry in part.entries:
            out["sensors"].setdefault(r.sensor, {})[r.reading] = entry
        if part.unavailable:
            out["unavailable"] = part.unavailable
        return out

    async def forecast(self) -> dict:
        from .forecast.source import describe_day
        f = self.ctx.forecast
        return {"place": f.place, "fetched_at": iso(f.fetched_at, self.ctx.zone) if f.fetched_at else None,
                "days": [{**d, "text": describe_day(d)} for d in (f.days or [])]}

    async def pollen(self) -> dict:
        p = self.ctx.pollen
        now = p.current()
        return {"in_season": p.in_season(), "fetched_at": iso(now["fetched_at"], self.ctx.zone) if now["fetched_at"] else None,
                "grass": now["grass"], "asthma": now["asthma"], "lines": p.lines()}

    async def health(self) -> dict:
        out = {}
        storage = self.ctx.storage
        if storage is not None:
            path = Path(storage.path)
            out["file"] = {"path": str(path), "bytes": os.path.getsize(path) if path.exists() else 0}
        if self.alert_log is not None:
            out["alert_log"] = {"last_id": self.alert_log.last_id()}
        if self.health_of is not None:
            out.update(self.health_of())
        return out

    async def schema(self) -> dict:
        return json.loads(SCHEMA_PATH.read_text())

    # ---------- prompts ----------
    async def _list_prompts(self, ctx, params) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=[types.Prompt(name=name, description=what, arguments=args or None)
                                                for name, (what, args, _) in PROMPTS.items()])

    async def _get_prompt(self, ctx, params: types.GetPromptRequestParams) -> types.GetPromptResult:
        return prompt(params.name, params.arguments)

    # ---------- publishing (3.4) ----------
    def _fingerprint(self, uri: str) -> str | None:
        """What a pushed resource's change is told by, from what the sources keep (nothing fetched): the live reading itself (its
        values and times), the forecast's days, the pollen page's levels. None while there is none."""
        c = self.ctx
        try:
            if uri == WEATHER_URI:
                kept = getattr(c.eco, "_live", None)
                return _json(kept[1]) if kept else None
            if uri == AIR_URI:
                kept = getattr(c.air, "_current", None)
                return _json(kept[0]) if kept else None
            if uri == FORECAST_URI:
                return _json(c.forecast.days) if c.forecast and c.forecast.days else None
            if uri == POLLEN_URI:
                return _json({k: v for k, v in c.pollen.current().items() if k != "fetched_at"}) if c.pollen else None
        except Exception as e:   # (only what is told: never stops the publishing)
            log.warning("MCP: couldn't compare %s: %s: %s", uri, type(e).__name__, e)
        return None

    async def publish_changes(self) -> list[str]:
        """Publish ResourceUpdated for each pushed resource whose content changed since the last look; the URIs published."""
        changed = []
        for uri in PUSHED:
            now = self._fingerprint(uri)
            if now is not None and now != self._seen.get(uri):
                changed.append(uri)
            if now is not None:
                self._seen[uri] = now
        for uri in changed:
            await self.bus.publish(ResourceUpdated(uri))
        return changed

    async def watch_alerts(self):
        """Publish the alerts' URI on each new alert, until cancelled."""
        if self.alert_log is None:
            return
        last = self.alert_log.last_id()
        while True:
            await self.alert_log.wait(last)
            last = self.alert_log.last_id()
            await self.bus.publish(ResourceUpdated(ALERTS_URI))

    async def run(self, publish_every: float = PUBLISH_SECONDS, catalogue_every: float = CATALOGUE_SECONDS):
        """The publishing, until cancelled: the alerts as they come, the live resources every `publish_every` s, the catalogue every
        `catalogue_every` s. A failure is logged and the next round tries again."""
        async def every(seconds: float, fn):
            while True:
                await asyncio.sleep(seconds)
                try:
                    await fn()
                except Exception as e:
                    log.warning("MCP: %s failed: %s: %s", fn.__name__, type(e).__name__, e)
        await asyncio.gather(self.watch_alerts(), every(publish_every, self.publish_changes), every(catalogue_every, self.refresh_catalogue))


def prompt(name: str, arguments: dict | None = None) -> types.GetPromptResult:
    """A prompt's question in words, its arguments filled in (their defaults where not sent)."""
    if name not in PROMPTS:
        raise MCPError(types.INVALID_PARAMS, f"No prompt {name!r}: the prompts are {', '.join(PROMPTS)}.")
    what, args, text = PROMPTS[name]
    given = {**PROMPT_DEFAULTS.get(name, {}), **{k: str(v).strip() for k, v in (arguments or {}).items() if v is not None and str(v).strip()}}
    if missing := [a.name for a in args if a.required and a.name not in given]:
        raise MCPError(types.INVALID_PARAMS, f"The prompt {name!r} needs {', '.join(missing)}.")
    question = WITHOUT_ARGUMENTS[name] if name in WITHOUT_ARGUMENTS and not given else text.format(**given)
    return types.GetPromptResult(description=what, messages=[types.PromptMessage(role="user", content=types.TextContent(text=question))])


def http_app(server: EcobotServer, *, tokens=(), allowed_hosts=("127.0.0.1", "localhost"), issuer_url: str = "http://127.0.0.1:8765",
             path: str = "/mcp"):
    """The Streamable HTTP app (Starlette) at `path`: the Host header checked against `allowed_hosts`, and, with `tokens` (MCP_TOKENS:
    name -> token), every request's bearer token (TokenCheck). `issuer_url` is required by the SDK with auth but unused: no OAuth
    metadata is published (resource_server_url=None)."""
    check = TokenCheck(tokens) if tokens else None
    app = server.server.streamable_http_app(
        streamable_http_path=path, transport_security=host_settings(allowed_hosts),
        auth=AuthSettings(issuer_url=issuer_url, resource_server_url=None) if check else None, token_verifier=check)
    app.state.tokens = check
    return app


__all__ = ["ALERTS_TEMPLATE", "ALERTS_URI", "COMPLETE", "EcobotServer", "PROMPTS", "RESOURCES", "TokenCheck", "host_settings", "http_app",
           "prompt", "tool_entry", "tool_result"]
