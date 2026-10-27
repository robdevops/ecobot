"""The Ecowitt weather station as a data source, peer of AirGradient:
start() finds the station, tools are what the model can call, warm()/poke() keep recent
readings ready, readings() feeds the alert monitors."""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, UTC
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import Config
from ..sources import Source
from ..timeutil import local_epoch, naive_local, when
from ..tools import Tool, Turn, json_result
from ..warm import FAST_REFRESH_SECONDS, Warmer
from .api import FINE_CYCLES, EcowittAPI, GROUPS, UNITS, unit_text
from .calendar import PublicHolidays
from .link import DESCRIPTION as LINK_DESCRIPTION, PARAMETERS as LINK_PARAMETERS, link_tool
from .days import DESCRIPTION as DAYS_DESCRIPTION, PARAMETERS as DAYS_PARAMETERS, days_tool
from .dashboard import dashboard
from .glance import glance
from .fetch import Fetcher, series_in
from ..series import READING_GROUPS, WEATHER, find
from .query import HistoryQuery, chart_field, stack_names
from .store import HistoryCache, HotStore

log = logging.getLogger(__name__)

DEFAULT_GROUPS = "outdoor,indoor"


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
    for group, field, obj in series_in(data):
        for ts, v in obj["list"].items():
            try:
                rows.setdefault(int(ts), {})[f"{group}.{field}"] = float(v)
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
        "two_axes": {"type": "boolean", "description": "Set true to draw the chart_fields as ONE panel with a scale each "
                                                       "(left and right axis) when the question says 'on two axes', 'dual axis' or 'overlay'."},
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
    "properties": {"groups": {"type": "string", "description": "Comma-separated group names, e.g. 'outdoor,indoor'."}},
}
REALTIME_DESCRIPTION = "Current readings from the owner's Ecowitt weather station."


LIVE_GROUPS = list(READING_GROUPS)   # what the once-a-minute live reading asks for: everything "weather now" and the report show
LIVE_FRESH_SECONDS = 90                # a question is answered from that reading while it is this young


def live_summary(data: dict) -> str:
    """A live reading in a few words for the log: rain first, then outdoor temperature and humidity, wind and pressure. A reading the
    response lacks is left out."""
    def get(group: str, field: str) -> str | None:
        obj = (data.get(group) or {}).get(field)
        if not isinstance(obj, dict) or "value" not in obj:
            return None
        unit = unit_text(obj.get("unit")) or ""
        return f"{obj['value']}{'' if unit in ('°C', '%') else ' '}{unit}".strip()
    wind = get("wind", "wind_speed")
    parts = [f"rain {rate}" if (rate := get("rainfall", "rain_rate")) else None,
             f"{daily}/day" if (daily := get("rainfall", "daily")) else None,
             get("outdoor", "temperature"), get("outdoor", "humidity"),
             f"wind {wind}" if wind else None, get("pressure", "relative")]
    return ", ".join(p for p in parts if p) or "no readings"


class Ecowitt(Source):
    name = "Ecowitt"

    def __init__(self, cfg: Config, transport=None):
        self.tz = cfg.tz
        self.api = EcowittAPI(cfg.ecowitt_api_key, cfg.ecowitt_app_key, transport)
        self.cache = HistoryCache(cfg.cache_path, UNITS)
        self.hot = HotStore()
        self.groups = list(GROUPS)  # shared with the archive, which drops any group the station lacks
        self.warmer = Warmer(self.warm, FAST_REFRESH_SECONDS)
        self._live_ok: bool | None = None  # the live rain reading: None until first tried
        self._live: tuple[float, dict] | None = None   # (fetched at, the real_time data) from the keep-warm's minute check
        self.mac = ""
        self.station_name = ""
        self.created: datetime | None = None
        self.latitude: float | None = None  # the station's own location (the forecast uses it unless one is configured)
        self.longitude: float | None = None
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
        if zone := device.get("date_zone_id"):   # the station's own time zone ("Australia/Melbourne"): its days are local to it
            try:
                self.tz = ZoneInfo(zone)
            except (ValueError, ZoneInfoNotFoundError):
                log.warning("Ecowitt names a time zone Python doesn't know, %r; using %s", zone, self.tz)
        try:
            self.created = datetime.fromtimestamp(int(device["createtime"]), UTC).astimezone(self.tz)
        except (KeyError, TypeError, ValueError):
            pass
        try:
            self.longitude = float(device["longitude"])
        except (KeyError, TypeError, ValueError):
            pass
        try:
            self.latitude = float(device["latitude"])
        except (KeyError, TypeError, ValueError):
            pass
        if (problem := PublicHolidays(self.tz).problem()) and "package" in problem:
            log.warning("Public holiday questions won't work: pip install holidays (then restart)")
        log.info("Weather station: Ecowitt '%s' (%s), %s%s", self.station_name, self.mac, self.tz,
                 f", devices found: {len(devices)}" if len(devices) > 1 else "")
        try:
            size = os.path.getsize(self.cache.path) if os.path.exists(self.cache.path) else 0
            log.info("History cache: %s (%.1f MB)", self.cache.path, size / 1e6)
        except Exception as e:   # only a log line
            log.warning("History cache not readable: %s", e)

    def describe(self) -> str:
        created = f", created {self.created:%Y-%m-%d %H:%M}" if self.created else ""
        return f"Ecowitt weather station '{self.station_name}'{created} (people say \"ecowitt\" or \"weather\")"

    def fetcher(self, groups: list[str], urgent: bool = False) -> Fetcher:
        """urgent: for a question (its requests go before the background ones)."""
        return Fetcher(self.api, self.cache, self.hot, self.mac, groups, self.tz, urgent)

    # ---------- tools ----------
    async def _history(self, args: dict, turn: Turn | None = None) -> str:
        turn = turn or Turn()
        groups = parse_groups(args.get("groups"))
        groups += [g for n in stack_names(args, turn) if (g := WEATHER[n].group) not in groups]  # what "plot temperature and rain" needs
        if (r := find(chart_field(args, turn) or "")) and r.group not in groups:      # ... and what a chart of one reading needs
            groups.append(r.group)
        return await HistoryQuery(self.fetcher(groups, urgent=not turn.ahead), args, turn).run()

    async def _days(self, args: dict, turn: Turn | None = None) -> str:
        return await days_tool(self.cache, self.mac, self.tz, args, turn)

    async def _link(self, args: dict, turn: Turn | None = None) -> str:
        return await link_tool(self.cache, self.mac, self.tz, args, turn or Turn())

    async def _realtime(self, args: dict, turn: Turn | None = None) -> str:
        groups = parse_groups(args.get("groups"))
        data = await self._live_data(groups)
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
        return json_result({"time": when(newest, self.tz) if newest else None, **out, **({"emoji": emoji} if emoji else {})})

    async def _live_data(self, groups: list[str]) -> dict:
        """The real_time data for these groups: the keep-warm's reading when it is under a minute and a half old and asked for them,
        else a request of its own, ahead of the background refreshes (someone is waiting)."""
        if self._live and time.time() - self._live[0] <= LIVE_FRESH_SECONDS and set(groups) <= set(LIVE_GROUPS):
            return {g: self._live[1][g] for g in groups if g in self._live[1]}
        return await self.api.realtime(self.mac, ",".join(groups), urgent=True)

    async def dashboard(self):
        """The station's gauges (specs.Dashboard): the live reading and each one's highs and lows today, this month and this year; None
        when the station has no reading."""
        return await dashboard(await self._live_data(LIVE_GROUPS), self.cache, self.mac, self.tz, self.now().date())

    async def live_rain(self) -> tuple[int, dict] | None:
        """The gauge's latest rate and daily total as a history-shaped row, for the rain alert: the 5-minute history lags
        by up to 5 minutes, this is about a minute old. The same request keeps every live reading ready for "weather now" and the
        report (self._live), one request a minute. None when unavailable."""
        try:
            data = await self.api.realtime(self.mac, ",".join(LIVE_GROUPS))
            self._live = (time.time(), data)
            group = data.get("rainfall") or {}
            row = {f"rainfall.{k}": float(group[k]["value"]) for k in ("rain_rate", "daily") if k in group}
            ts = max((int(group[k].get("time") or 0) for k in group if isinstance(group[k], dict)), default=0)
            if not row or not ts:
                raise ValueError("no rain reading in the response")
        except Exception as e:  # the alert still works from the history
            if self._live_ok is not False:  # once per outage, not every minute
                log.warning("Live readings failed: %s; rain alert uses history", e)
            self._live_ok = False
            return None
        if self._live_ok is None:
            log.info("%s", live_summary(data))
        elif not self._live_ok:
            log.info("Recovered: %s", live_summary(data))
        self._live_ok = True
        return ts, row

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

        await asyncio.gather(*(fetchers[cycle].get(cycle, start, now, refresh=fresh and cycle in FINE_CYCLES, load=False)
                               for cycle, start in windows.items()))
        return f"Ecowitt {sum(f.calls for f in fetchers.values())} req"

    def wants(self, text: str) -> bool:
        """Should a question start refreshing this source? Weather data is used by nearly all."""
        return True

    async def readings(self, cycle: str, start: datetime, end: datetime,
                       groups: list[str] | None = None) -> list[tuple[int, dict]]:
        """Readings for the alert monitors, straight from the warm data."""
        return readings(await self.fetcher(groups or self.groups).get(cycle, start, end))

    async def recent(self, hours: float) -> list[tuple[int, dict]]:
        """The last `hours` of 5-minute readings: real hours, so the night the clocks change is no longer or shorter."""
        now = self.now()
        return await self.readings("5min", naive_local(local_epoch(now, self.tz) - hours * 3600, self.tz), now)

    async def close(self):
        await self.api.close()
        self.cache.close()
