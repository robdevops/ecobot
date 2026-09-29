"""The Ecowitt weather station as a data source, peer of AirGradient:
start() finds the station, tools are what the model can call, warm()/poke() keep recent
readings ready, readings() feeds the alert monitors."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..tools import Tool
from ..warm import Warmer
from .api import EcowittAPI, GROUPS, UNITS
from .days import DESCRIPTION as DAYS_DESCRIPTION, PARAMETERS as DAYS_PARAMETERS, days_tool
from .history import Fetcher, HistoryQuery
from .store import HistoryCache, HotStore

log = logging.getLogger(__name__)

DEFAULT_GROUPS = "outdoor,indoor"


def parse_groups(value, default: str = DEFAULT_GROUPS) -> list[str]:
    """'outdoor, indoor' or ['outdoor', 'indoor'] -> ['outdoor', 'indoor'] (plain group names only)."""
    parts = value if isinstance(value, list) else str(value or default).split(",")
    return list(dict.fromkeys(p.split(".")[0].strip() for p in parts if p.strip())) or default.split(",")


def readings(data: dict) -> list[tuple[int, dict]]:
    """Ecowitt 'data' -> [(epoch, {"group.field": value})] sorted by time."""
    rows: dict[int, dict] = {}
    for grp, fields in data.items():
        if not isinstance(fields, dict):
            continue
        for field, obj in fields.items():
            if isinstance(obj, dict) and isinstance(obj.get("list"), dict):
                for ts, v in obj["list"].items():
                    try:
                        rows.setdefault(int(ts), {})[f"{grp}.{field}"] = float(v)
                    except (TypeError, ValueError):
                        pass
    return sorted(rows.items())


HISTORY_PARAMS = {
    "type": "object",
    "properties": {
        "start_date": {"type": "string", "description": "Start, 'YYYY-MM-DD HH:MM:SS' local time."},
        "end_date": {"type": "string", "description": "End, 'YYYY-MM-DD HH:MM:SS' local time (today is fine: up to now)."},
        "groups": {"type": "string", "description": "Comma-separated group names, e.g. 'outdoor,indoor'. Add 'rainfall', "
                                                    "'wind' or 'pressure' only if needed. Plain group names, not dotted fields."},
        "chart": {"type": "boolean", "description": "Set true to send a chart with the answer: for trends over several "
                                                    "days or longer, or when a graph/chart is asked for."},
        "include_derived": {"type": "array", "items": {"type": "string", "enum": ["feels_like", "app_temp", "dew_point", "vpd"]},
                            "description": "Only when the question asks about feels-like, apparent temperature, dew point or "
                                           "VPD: which of them to include. Otherwise omit (they are left out by default)."},
    },
    "required": ["start_date", "end_date"],
}
HISTORY_DESCRIPTION = ("History from the owner's Ecowitt weather station: each day's low/high and the overall extremes "
                       "with times, for any range up to 4 years. Resolution and units are handled automatically.")
REALTIME_PARAMS = {
    "type": "object",
    "properties": {"groups": {"type": "string", "description": "Comma-separated group names, e.g. 'outdoor,indoor'. "
                                                               "For rain outlooks: 'outdoor,pressure,rainfall,rainfall_piezo,wind'."}},
}
REALTIME_DESCRIPTION = "Current readings from the owner's Ecowitt weather station."


class Ecowitt:
    name = "Ecowitt"

    def __init__(self, cfg: Config, transport=None):
        self.tz = cfg.tz
        self.api = EcowittAPI(cfg.ecowitt_api_key, cfg.ecowitt_app_key, transport)
        self.cache = HistoryCache(cfg.cache_path, UNITS)
        self.hot = HotStore()
        self.groups = list(GROUPS)  # shared with the archive, which drops any group the station lacks
        self.warmer = Warmer(self.warm)
        self.mac = ""
        self.station_name = ""
        self.created: datetime | None = None
        self.longitude = 145.0  # Melbourne; only used to time the pressure tide
        self.tools = [Tool("weather_now", REALTIME_DESCRIPTION, REALTIME_PARAMS, self._realtime),
                      Tool("weather_history", HISTORY_DESCRIPTION, HISTORY_PARAMS, self._history),
                      Tool("weather_days", DAYS_DESCRIPTION, DAYS_PARAMETERS, self._days)]

    async def start(self):
        devices = await self.api.devices()
        if not devices:
            raise RuntimeError("no Ecowitt device on this account")
        device = devices[0]
        self.mac = str(device["mac"]).upper()
        self.station_name = device.get("name") or "station"
        try:
            self.created = datetime.fromtimestamp(int(device["createtime"]), timezone.utc).astimezone(self.tz)
        except (KeyError, TypeError, ValueError):
            pass
        try:
            self.longitude = float(device["longitude"])
        except (KeyError, TypeError, ValueError):
            pass
        log.info("Weather station: Ecowitt '%s' (%s)%s", self.station_name, self.mac,
                 f", devices found: {len(devices)}" if len(devices) > 1 else "")

    def describe(self) -> str:
        created = f", created {self.created:%Y-%m-%d %H:%M}" if self.created else ""
        return f"Ecowitt weather station '{self.station_name}'{created}"

    def fetcher(self, groups: list[str]) -> Fetcher:
        return Fetcher(self.api, self.cache, self.hot, self.mac, groups, self.tz)

    def now(self) -> datetime:
        return datetime.now(self.tz).replace(tzinfo=None, microsecond=0)

    # ---------- tools ----------
    async def _history(self, args: dict) -> str:
        return await HistoryQuery(self.fetcher(parse_groups(args.get("groups"))), args).run()

    async def _days(self, args: dict) -> str:
        return await days_tool(self.cache, self.mac, self.tz, args)

    async def _realtime(self, args: dict) -> str:
        data = await self.api.realtime(self.mac, ",".join(parse_groups(args.get("groups"))))
        out, newest = {}, 0
        for grp, fields in data.items():
            for name, obj in (fields.items() if isinstance(fields, dict) else ()):
                if isinstance(obj, dict) and "value" in obj:
                    out.setdefault(grp, {})[name] = f"{obj['value']} {obj.get('unit', '')}".strip()
                    try:
                        newest = max(newest, int(obj.get("time") or 0))
                    except (TypeError, ValueError):
                        pass
        when = datetime.fromtimestamp(newest, timezone.utc).astimezone(self.tz).strftime("%a %d %b %Y %H:%M") if newest else None
        return json.dumps({"time": when, **out}, ensure_ascii=False, separators=(",", ":"))

    # ---------- keeping warm ----------
    async def warm(self, fresh: bool = True) -> str:
        """The last 7 days at 30 minutes (only the unsettled tail goes to Ecowitt) and today at 5 minutes."""
        now = self.now()
        today = datetime.combine(now.date(), datetime.min.time())
        f30, f5 = self.fetcher(self.groups), self.fetcher(self.groups)
        await asyncio.gather(f30.get("30min", today - timedelta(days=6), now, refresh=fresh),
                             f5.get("5min", today, now, refresh=fresh))
        return f"Ecowitt {f30.calls + f5.calls} req"

    def wants(self, text: str) -> bool:
        """Should a question start refreshing this source? Weather data is used by nearly all."""
        return True

    def poke(self):
        self.warmer.poke()

    async def readings(self, cycle: str, start: datetime, end: datetime,
                       groups: list[str] | None = None) -> list[tuple[int, dict]]:
        """Readings for the alert monitors, straight from the warm data."""
        return readings(await self.fetcher(groups or self.groups).get(cycle, start, end))

    async def recent(self, hours: float) -> list[tuple[int, dict]]:
        now = self.now()
        return await self.readings("5min", now - timedelta(hours=hours), now)

    async def close(self):
        await self.api.close()
        self.cache.close()
