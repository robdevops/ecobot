"""The Ecowitt weather station as a data source, peer of AirGradient:
start() finds the station, tools are what the model can call, warm()/poke() keep recent
readings ready, readings() feeds the alert monitors."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from ..config import Config
from ..timeutil import now_local
from .outlook import rain_outlook
from ..tools import Tool, Turn
from ..warm import FAST_REFRESH_SECONDS, Warmer
from .api import EcowittAPI, GROUPS, UNITS
from .calendar import PublicHolidays
from .link import DESCRIPTION as LINK_DESCRIPTION, PARAMETERS as LINK_PARAMETERS, link_tool
from .days import DESCRIPTION as DAYS_DESCRIPTION, PARAMETERS as DAYS_PARAMETERS, days_tool
from .glance import glance
from .fetch import Fetcher, spans
from ..series import WEATHER, find
from .query import HistoryQuery, stack_names
from .store import HistoryCache, HotStore

log = logging.getLogger(__name__)

DEFAULT_GROUPS = "outdoor,indoor"
FAST_CYCLES = ("5min", "30min")   # kept warm by refetching every cycle; the 4-hour and daily tails only when their memory copy is stale


def parse_groups(value, default: str = DEFAULT_GROUPS) -> list[str]:
    """'outdoor, indoor' or ['outdoor', 'indoor'] -> ['outdoor', 'indoor'] (plain group names only). A reading named instead of
    its group ("humidity", "dew_point", "rain") becomes its group; a name Ecowitt has no such group for is dropped, since one
    bad name fails the whole request."""
    parts = value if isinstance(value, list) else str(value or default).split(",")
    named = (p.split(".")[0].strip().lower() for p in parts if p.strip())
    groups = (g if g in GROUPS else (r.group if (r := find(g)) else None) for g in named)
    return list(dict.fromkeys(g for g in groups if g)) or default.split(",")


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
        "chart_fields": {"type": "array", "items": {"type": "string", "enum": list(WEATHER)},
                         "description": "To plot SEVERAL readings together ('plot temperature and rain'): which, in order, "
                                        "one panel each on a shared time axis. The groups they need are fetched for you."},
        "average": {"type": "boolean", "description": "Set true only when the question asks for an average or mean: adds the "
                                                      "period's average (and per day or month). Highs and lows are the default."},
        "chart_field": {"type": "string", "description": "What the chart should plot when the question is about something other "
                                                         "than temperature: the reading, e.g. 'humidity', 'pressure', 'wind', 'dew_point', 'solar' or 'uv' "
                                                         "(a wind chart shows the average speed shaded up to the gusts). "
                                                         "Omit for temperature."},
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
        self.warmer = Warmer(self.warm, FAST_REFRESH_SECONDS)
        self.mac = ""
        self.station_name = ""
        self.created: datetime | None = None
        self.longitude = 145.0  # Melbourne; only used to time the pressure tide
        self.tools = [Tool("weather_now", REALTIME_DESCRIPTION, REALTIME_PARAMS, self._realtime),
                      Tool("weather_history", HISTORY_DESCRIPTION, HISTORY_PARAMS, self._history),
                      Tool("weather_days", DAYS_DESCRIPTION, DAYS_PARAMETERS, self._days),
                      Tool("weather_link", LINK_DESCRIPTION, LINK_PARAMETERS, self._link)]

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
        if (problem := PublicHolidays(self.tz).problem()) and "package" in problem:
            log.warning("Public holiday questions won't work: pip install holidays (then restart)")
        log.info("Weather station: Ecowitt '%s' (%s)%s", self.station_name, self.mac,
                 f", devices found: {len(devices)}" if len(devices) > 1 else "")

    def describe(self) -> str:
        created = f", created {self.created:%Y-%m-%d %H:%M}" if self.created else ""
        return f"Ecowitt weather station '{self.station_name}'{created} (people say \"ecowitt\" or \"weather\")"

    def fetcher(self, groups: list[str]) -> Fetcher:
        return Fetcher(self.api, self.cache, self.hot, self.mac, groups, self.tz)

    def now(self) -> datetime:
        return now_local(self.tz)

    # ---------- tools ----------
    async def _history(self, args: dict, turn: Turn | None = None) -> str:
        turn = turn or Turn()
        groups = parse_groups(args.get("groups"))
        groups += [g for n in stack_names(args, turn) if (g := WEATHER[n].group) not in groups]  # what "plot temperature and rain" needs
        named = str(turn.chart_field or args.get("chart_field") or "").strip().lower()      # ... and what a chart of one reading needs
        if (r := find(named)) and r.group not in groups:
            groups.append(r.group)
        return await HistoryQuery(self.fetcher(groups), args, turn).run()

    async def _days(self, args: dict, turn: Turn | None = None) -> str:
        return await days_tool(self.cache, self.mac, self.tz, args)

    async def _link(self, args: dict, turn: Turn | None = None) -> str:
        return await link_tool(self.cache, self.mac, self.tz, args, turn or Turn())

    async def _realtime(self, args: dict, turn: Turn | None = None) -> str:
        groups = parse_groups(args.get("groups"))
        data = await self.api.realtime(self.mac, ",".join(groups))
        out, newest, emoji = {}, 0, {}
        for grp, fields in data.items():
            for name, obj in (fields.items() if isinstance(fields, dict) else ()):
                if isinstance(obj, dict) and "value" in obj:
                    out.setdefault(grp, {})[name] = f"{obj['value']} {obj.get('unit', '')}".strip()
                    try:
                        if tag := glance(grp, name, float(obj["value"])):
                            emoji[f"{grp}.{name}"] = tag
                    except (TypeError, ValueError):
                        pass
                    try:
                        newest = max(newest, int(obj.get("time") or 0))
                    except (TypeError, ValueError):
                        pass
        when = datetime.fromtimestamp(newest, timezone.utc).astimezone(self.tz).strftime("%a %d %b %Y %H:%M") if newest else None
        outlook = await self._rain_outlook() if "rainfall" in groups else None
        return json.dumps({"time": when, **out, **({"rain_outlook": outlook} if outlook else {}), **({"emoji": emoji} if emoji else {})},
                          ensure_ascii=False, separators=(",", ":"))

    async def live_rain(self) -> tuple[int, dict] | None:
        """The gauge's latest rate and daily total as a history-shaped row, for the rain alert: the 5-minute history lags
        by up to 5 minutes, this is about a minute old. None when unavailable."""
        try:
            group = (await self.api.realtime(self.mac, "rainfall")).get("rainfall") or {}
            row = {f"rainfall.{k}": float(group[k]["value"]) for k in ("rain_rate", "daily") if k in group}
            ts = max(int(group[k].get("time") or 0) for k in group if isinstance(group[k], dict))
        except Exception as e:  # the alert still works from the history
            log.debug("Live rain unavailable: %s", e)
            return None
        return (ts, row) if row and ts else None

    async def _rain_outlook(self) -> str | None:
        """Raining now, or likely soon (the same rules as the alerts), from the last 3 hours of readings."""
        try:
            return rain_outlook(await self.recent(3), self.tz, self.longitude)
        except Exception as e:  # the current reading is still worth sending without it
            log.warning("Rain outlook unavailable: %s", e)
            return None

    # ---------- keeping warm ----------
    async def warm(self, fresh: bool = True) -> str:
        """Every resolution's newest readings for every group: the last 7 days at 30 minutes, yesterday and today at
        5 minutes, the last 3 days at 4 hours and daily (only the unsettled tails go to Ecowitt; the archive keeps the
        settled history)."""
        now = self.now()
        today = datetime.combine(now.date(), datetime.min.time())
        windows = {"30min": today - timedelta(days=6), "5min": today - timedelta(days=1),
                   "4hour": today - timedelta(days=3), "1day": today - timedelta(days=3)}
        fetchers = {cycle: self.fetcher(self.groups) for cycle in windows}

        async def one(cycle: str):
            for start, end in spans(cycle, windows[cycle], now):   # each piece fits Ecowitt's per-request limit
                await fetchers[cycle].get(cycle, start, end, refresh=fresh and cycle in FAST_CYCLES, load=False)
        await asyncio.gather(*(one(cycle) for cycle in windows))
        return f"Ecowitt {sum(f.calls for f in fetchers.values())} req"

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
