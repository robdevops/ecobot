"""The Ecowitt weather station as a data source, peer of AirGradient:
start() finds the station, warm()/poke() keep recent readings ready, readings() feeds the alert monitors, and the service's tools
(lib/service) read its file and its live reading."""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, UTC

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import Config
from ..lines import RANGE_KEEP_DAYS
from ..sources import Source
from ..timeutil import local_epoch, naive_local
from ..warm import FAST_REFRESH_SECONDS, Warmer, safely
from .api import FINE_CYCLES, EcowittAPI, GROUPS, NotInYet, unit_text
from ..public_holidays import PublicHolidays
from .fetch import Fetcher, Recent
from .fallback import FallbackAPI, newest
from .hass_api import HomeAssistantAPI
from .homeassistant import HomeAssistant
from .live import BUCKET, readings_in
from ..series import READING_GROUPS
from ..storage import Storage
from ..storage.catalogue import AIR
from ..storage.write import MIN_READINGS
from .query import KEEP_HOURS
from .reads import newest_minute_on

log = logging.getLogger(__name__)

LIVE_GROUPS = list(READING_GROUPS)   # what the once-a-minute live reading asks for: everything "weather now" and the report show
HA_IMPORT_SECONDS = 900                # how often Home Assistant's readings are folded into the 5-minute ranges ...
HA_RECENT_SECONDS = 3 * 3600           # ... the last few hours each time (the whole RANGE_KEEP_DAYS the first time)
LIVE_POLL_SECONDS = 30                 # Home Assistant's live reading is asked for this often (the gateway reports about once a minute)
LIVE_RECORD_SECONDS = 30               # ... and counted into the ranges this often
LIVE_FRESH_SECONDS = 90                # a question is answered from that reading while it is this young
DEVICE_RETRY_SECONDS = (10, 300)       # a device list that failed is asked again after the first, doubling up to the second
LIVE_KEEP_SECONDS = 60                 # the live reading is kept in the file at most this often (a restart starts from it)
DEVICE_KEY, EARLIEST_KEY, LIVE_KEY = "station", "ha_earliest", "live"   # the file's meta: the station's device (as its API last listed
                                       # it); Home Assistant's first state; the last live reading ({"at", "data"})

class LiveMarks:
    """The live readings of the newest 5-minute marks, each counted in the mark nearest it (as the old live ranges were): once a mark has two
    readings or more of a reading, its lowest and highest are a band, written to that 5-minute reading."""

    def __init__(self):
        self.marks: dict[str, dict[int, list]] = {}   # series -> mark -> [readings, lowest, highest]
        self.seen: dict[str, int] = {}                # series -> the reading time last counted (the same reading asked for again is not another)

    def add(self, data: dict, now: float) -> dict[str, dict[int, tuple[int, float, float]]]:
        """Count a real_time response in; returns the bands it made or widened ({series: {mark: (readings, lowest, highest)}})."""
        found: dict = {}
        for series, at, value in readings_in(data):
            at = at or int(now)
            if self.seen.get(series) == at:
                continue
            self.seen[series] = at
            mark = round(at / BUCKET) * BUCKET
            got = self.marks.setdefault(series, {}).setdefault(mark, [0, value, value])
            got[0], got[1], got[2] = got[0] + 1, min(got[1], value), max(got[2], value)
            if got[0] >= MIN_READINGS:
                found.setdefault(series, {})[mark] = tuple(got)
        for marks in self.marks.values():   # only the newest marks are still being counted
            for mark in [m for m in marks if m < now - 2 * BUCKET]:
                del marks[mark]
        return found

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

    def __init__(self, cfg: Config, transport=None, ha_transport=None, storage: Storage | None = None):
        """transport: for Ecowitt's cloud, ha_transport: for Home Assistant (a test's stand-ins). Where the readings come from follows the
        settings (config.ecowitt_source): Home Assistant, Ecowitt's cloud, or Home Assistant with the cloud to make up what it lacks.
        storage: the bot's file (lib/storage), opened at start; without one the station opens it itself. Either way the station closes it."""
        self.tz = cfg.tz
        self.storage = storage or Storage(cfg.storage_path, cfg.tz)
        self.fetches = Recent()   # the newest fetches, read from the storage while fresh
        macs = sorted({s.device for s in self.storage.sensors.all() if s.kind != AIR[0]})   # the station the file holds, so Home Assistant's readings join its history
        home = HomeAssistantAPI(cfg.homeassistant_url, cfg.homeassistant_token, cfg.tz, macs[0] if len(macs) == 1 else "HOMEASSISTANT",
                                ha_transport) if cfg.ecowitt_home_assistant else None
        cloud = EcowittAPI(cfg.ecowitt_api_key, cfg.ecowitt_app_key, transport) if cfg.ecowitt_cloud else None
        self.api = FallbackAPI(home, cloud, cfg.tz) if home and cloud else home or cloud
        self.home = home                     # Home Assistant's API, if it is one of them (its first state is kept in the file)
        self.via = "HA/cloud" if home and cloud else "HA" if home else "cloud"
        self.has_minutes = home is not None   # (only Home Assistant has minutes)
        self.fast = home                     # the API that can be asked every LIVE_POLL_SECONDS, if any
        self.ha = home.ha if home else HomeAssistant(cfg.homeassistant_url, cfg.homeassistant_token) if cfg.homeassistant else None   # (shared with the air sensor)
        self._ha_ok: bool | None = None
        self.live_marks = LiveMarks()   # the live readings' lows and highs, for the recent charts' bands
        self.groups = list(GROUPS)  # shared with the archive, which drops any group the station lacks
        self.warmer = Warmer(self.warm, FAST_REFRESH_SECONDS)
        self._live_ok: bool | None = None  # the live rain reading: None until first tried
        self._live: tuple[float, dict] | None = None   # (fetched at, the real_time data) from the keep-warm's minute check
        self._live_flight: tuple[bool, asyncio.Future] | None = None   # (urgent, the request) of every group, while it is asked
        self.on_live: list = []             # called (no arguments) when the poll finds a reading newer than the last: the rain alert's
        self._seen = 0                       # the time of the newest reading the poll has found
        self._polled: float = 0.0            # when the poll last had an answer
        self._recorded = 0.0
        self.cache_line = ""
        self.listed = asyncio.Event()        # set once the station is known (its address): from the file at start, else its device list
        self._live_now = False               # this run has had a live reading (until then a question is answered from the kept one)
        self._live_kept = 0.0                # when the live reading was last kept in the file
        self.mac = ""
        self.station_name = ""
        self.created: datetime | None = None
        self.latitude: float | None = None  # the station's own location (the forecast uses it unless one is configured)
        self.longitude: float | None = None

    async def start(self, wait: bool = True):
        """Find the station: its address, name, time zone, place and age: from what the file kept of it when its API last listed it (the
        same API), else from the file's readings (the station they are of, in the file's time zone), else (a first start) from its
        device list (wait; a failure raises) or, without waiting (ecobot-mcp's start: nothing asked of the network, so the port opens at
        once), not yet: its time zone is the configured one and its readings aren't in until refresh_device, in the warm-up, has its
        device list. ecobot-mcp asks for the list again in the background either way. The live reading kept in the file (the last
        before the restart) is taken too: questions and the dashboard are answered from it, as of its time, until this run's first
        comes in."""
        saved = await self._saved(DEVICE_KEY)
        if self.home is not None and isinstance(earliest := await self._saved(EARLIEST_KEY), (int, float)):
            self.home.known_earliest(float(earliest))
        if isinstance(saved, dict) and saved.get("via") == self.via and isinstance(saved.get("device"), dict) and saved["device"].get("mac"):
            self._apply(saved["device"], int(saved.get("count") or 1))
        elif known := self._file_station():
            log.info("Ecowitt: starting from the history file's station (%s, %s); its device list is asked for in the background",
                     known["mac"], known["date_zone_id"])
            self._apply(known, 1)
        elif wait:
            devices = await self.api.devices()
            if not devices:
                raise RuntimeError("no Ecowitt device on this account")
            await self._keep_device(devices[0], len(devices))
            self._apply(devices[0], len(devices))
        else:
            self.summary = f"Ecowitt: not found yet ({self.via}, {self.warmer.interval:g}s): its device list is asked for in the background"
        if self.mac:
            await self._take_kept_live()
        if self.created is None and self.home is not None and self.home.earliest is not None:   # (Home Assistant's first state, kept)
            self.created = datetime.fromtimestamp(int(self.home.earliest), UTC).astimezone(self.tz)
        await self.storage.set_timezone(self.tz)   # (so are the day rows)
        if (problem := PublicHolidays(self.tz).problem()) and "package" in problem:
            log.warning("Public holiday questions won't work: pip install holidays (then restart)")
        try:
            size = os.path.getsize(self.storage.path) if os.path.exists(self.storage.path) else 0
            self.cache_line = f"History: {self.storage.path} ({size / 1e6:.1f} MB)"
        except Exception as e:   # only a log line
            log.warning("History file not readable: %s", e)

    def _file_station(self) -> dict | None:
        """The station whose readings the file holds, as a device ({"mac", "date_zone_id"}: the file's days are in its zone), if just one."""
        mac = self.home.mac if isinstance(self.api, HomeAssistantAPI) else None
        macs = sorted({s.device for s in self.storage.sensors.all() if s.kind != AIR[0]})
        if mac is None and len(macs) == 1:
            mac = macs[0]
        if mac is None or mac not in macs:
            return None
        return {"mac": mac, "date_zone_id": str(getattr(self.storage.tz, "key", self.storage.tz))}

    def _apply(self, device: dict, count: int, zone: bool = True):
        """The station as a device list gives it: its address, name, time zone (its days are local to it; not with `zone` False), place
        and creation time."""
        self.mac = str(device["mac"]).upper()
        self.station_name = device.get("name") or "station"
        if zone and (named := device.get("date_zone_id")):   # the station's own time zone ("Australia/Melbourne"): its days are local to it
            try:
                self.tz = ZoneInfo(named)
            except (ValueError, ZoneInfoNotFoundError):
                log.warning("Ecowitt names a time zone Python doesn't know, %r; using %s", named, self.tz)
        if hasattr(self.api, "set_timezone"):   # (Home Assistant's history is cut in the station's days)
            self.api.set_timezone(self.tz)
        for name, key, read in (("created", "createtime", lambda v: datetime.fromtimestamp(int(v), UTC).astimezone(self.tz)),
                                ("longitude", "longitude", float), ("latitude", "latitude", float)):
            try:
                setattr(self, name, read(device[key]))
            except (KeyError, TypeError, ValueError):
                pass
        self.summary = (f"Ecowitt: '{self.station_name}' ({self.mac}): {self.via} ({self.warmer.interval:g}s)"
                        + (f", {count} devices" if count > 1 else ""))
        self.listed.set()

    @property
    def location(self) -> tuple[float, float] | None:
        """(lat, lon) as the station's device list gives it; None while it gives none (or, on a first start, hasn't answered yet)."""
        return None if None in (self.latitude, self.longitude) else (self.latitude, self.longitude)

    async def _saved(self, key: str):
        """A value the file keeps for the station (JSON), None when it has none or it can't be read."""
        try:
            text = await self.storage.read(lambda r: r.meta(key))
            return json.loads(text) if text else None
        except Exception as e:   # (then it is asked for, as at a first start)
            log.warning("Ecowitt: the history file's %s couldn't be read (%s: %s)", key, type(e).__name__, e)
            return None

    async def _keep(self, key: str, value):
        try:
            await self.storage.write(lambda w: w.set_meta(key, json.dumps(value)))
        except Exception as e:   # (only the next start's shortcut)
            log.warning("Ecowitt: couldn't keep the station's %s in the history file (%s: %s)", key, type(e).__name__, e)

    async def _keep_device(self, device: dict, count: int):
        kept = {k: device[k] for k in ("mac", "name", "date_zone_id", "latitude", "longitude", "createtime") if device.get(k) is not None}
        await self._keep(DEVICE_KEY, {"via": self.via, "count": count, "device": kept})

    async def refresh_device(self):
        """The device list, asked for in the background after the start (which never waits on it), again after a failure (DEVICE_RETRY_SECONDS)
        until it answers: what it says is kept in the file for the next start. A station found at start (from the file) takes its name,
        place and creation time at once; a new address or time zone is said, and taken at the next start (the tools and the file's days
        were set up in the old). A first start's station is found now (listed): its time zone is taken at the next start if it is
        not the configured one, which this run keeps."""
        wait, said = DEVICE_RETRY_SECONDS[0], False
        while True:
            try:
                devices = await self.api.devices()
                if not devices:
                    raise RuntimeError("no Ecowitt device on this account")
                break
            except Exception as e:
                if not said:
                    log.warning("Ecowitt: the device list failed (%s: %s); asked again in %gs, then less often%s", type(e).__name__, e, wait,
                                "" if self.mac else ": the station's readings aren't in until it answers")
                said = True
                await asyncio.sleep(wait)
                wait = min(wait * 2, DEVICE_RETRY_SECONDS[1])
        device, count = devices[0], len(devices)
        await self._keep_device(device, count)
        if said:
            log.info("Ecowitt: the device list answers again")
        zone, ours = device.get("date_zone_id"), str(getattr(self.tz, "key", self.tz))
        if not self.mac:
            self._apply(device, count, zone=False)
            log.info("%s", self.summary)
            if zone and zone != ours:
                log.warning("Ecowitt: the station's time zone is %s; this run keeps %s (the configured one): restart ecobot-mcp to use the "
                            "station's", zone, ours)
            return
        if str(device["mac"]).upper() != self.mac or zone and zone != ours:
            log.warning("Ecowitt: the device list now names %s in %s (this run is on %s in %s): restart ecobot-mcp to use it",
                        str(device["mac"]).upper(), zone or "no time zone", self.mac, self.tz)
            return
        self._apply(device, count)

    async def keep_earliest(self):
        """When Home Assistant's first state of the station is, if not known from the file: looked for (a long history query, after
        start), kept in the file; the station's creation time when no device list gave one (the archive starts from it)."""
        if self.home is None:
            return
        looked = self.home.looked
        earliest = await self.home.look()
        if earliest is not None and not looked:
            await self._keep(EARLIEST_KEY, earliest)
        if earliest is not None and self.created is None:
            self.created = datetime.fromtimestamp(int(earliest), UTC).astimezone(self.tz)

    def startup_lines(self) -> list[str]:
        return [*([self.cache_line] if self.cache_line else []), *super().startup_lines()]

    @property
    def archive_pace(self) -> float | None:
        """Seconds the archive waits between its requests: none when only Home Assistant answers (the pause is for the cloud's rate limit);
        None for the archive's own."""
        return 0.0 if isinstance(self.api, HomeAssistantAPI) else None

    async def _take_kept_live(self):
        """The live reading the file kept (the last before the restart), as this run's until its own comes in."""
        kept = await self._saved(LIVE_KEY)
        if isinstance(kept, dict) and isinstance(kept.get("data"), dict) and isinstance(kept.get("at"), (int, float)) and kept.get("mac") == self.mac:
            self._live = self._live or (float(kept["at"]), kept["data"])

    async def _keep_live(self, at: float, data: dict):
        """The live reading kept in the file for the next start, at most every LIVE_KEEP_SECONDS (only a restart's first answers need it)."""
        if at - self._live_kept >= LIVE_KEEP_SECONDS:
            self._live_kept = at
            await self._keep(LIVE_KEY, {"mac": self.mac, "at": at, "data": data})

    def _got_live(self, data: dict):
        """A live reading of this run's: kept for the next to ask (and, every LIVE_KEEP_SECONDS, in the file)."""
        self._live, self._live_now = (time.time(), data), True

    def fetcher(self, groups: list[str], urgent: bool = False) -> Fetcher:
        """urgent: for a question (its requests go before the background ones)."""
        return Fetcher(self.api, self.storage, self.fetches, self.mac, groups, self.tz, urgent, self.has_minutes)

    async def _live_data(self, groups: list[str], max_age: float = LIVE_FRESH_SECONDS, urgent: bool = True, kept: bool = True) -> dict:
        """The real_time data for these groups: the kept reading when it is under `max_age` old (a minute and a half for a question) and
        asked for them, or when Home Assistant's poll has just confirmed it is the newest; else a request of its own, ahead of the background
        refreshes when someone is waiting (urgent). Until this run has had a live reading (just after a start), the one kept from before
        it answers at once whatever its age (its readings carry their times), one being asked for meanwhile, unless `kept` is False (the
        rain alert's: it never acts on an old reading)."""
        fresh = self._live and (time.time() - self._live[0] <= max_age or time.time() - self._polled <= LIVE_POLL_SECONDS * 3)
        if set(groups) <= set(LIVE_GROUPS) and (fresh or self._live and kept and not self._live_now):
            if not fresh:
                self._ask_live()
            return {g: self._live[1][g] for g in groups if g in self._live[1]}
        return await self._fetch_live(groups, urgent=urgent)

    def _ask_live(self):
        """The live reading asked for in the background, unless it already is (or the station isn't found yet: the warm-up asks then)."""
        if self.mac and not ((flight := self._live_flight) and not flight[1].done()):
            asyncio.ensure_future(self.first_live())

    async def _fetch_live(self, groups: list[str], urgent: bool = False) -> dict:
        """A real_time request for these groups, counted into the 5-minute bands; with every group in it, kept as the minute's own reading
        for the next to ask (the report's gauges). Single-flight: while a request of every group is out, a caller wanting no more than it
        awaits that one instead of queueing another (a question joins a background one only once it is urgent too, so it never waits
        behind the refreshes queued ahead of it)."""
        wanted = set(groups)
        if (flight := self._live_flight) and not flight[1].done() and wanted <= set(LIVE_GROUPS) and (flight[0] or not urgent):
            data = await asyncio.shield(flight[1])
            return {g: data[g] for g in groups if g in data}
        if not self.mac:
            raise NotInYet()
        if not wanted >= set(LIVE_GROUPS):
            data = await self.api.realtime(self.mac, ",".join(groups), urgent=urgent)
            await self._record_live(data)
            return data
        task = asyncio.ensure_future(self._fetch_every_live(groups, urgent))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())   # (its failure is its callers'; none left is no warning)
        self._live_flight = (urgent, task)
        return await asyncio.shield(task)   # (a caller that gives up leaves it to the others, and the reading is still kept)

    async def _fetch_every_live(self, groups: list[str], urgent: bool) -> dict:
        data = await self.api.realtime(self.mac, ",".join(groups), urgent=urgent)
        self._got_live(data)
        await self._record_live(data)
        await self._keep_live(self._live[0], data)
        return data

    async def first_live(self):
        """The live reading at start, before the history: the dashboard's tiles and the first "weather now" wait on it (a question asked
        meanwhile joins this request). Urgent: people are waiting. A failure is logged; the next minute's check asks again."""
        try:
            await self._fetch_live(LIVE_GROUPS, urgent=True)
        except Exception as e:
            log.warning("Live reading at start failed: %s: %s", type(e).__name__, e)

    @property
    def ranges_from_ha(self) -> bool:
        """Are the charts' 5-minute bands read back from Home Assistant's history (import_ranges)? Only when the station's readings come
        from the cloud and Home Assistant is set: as the station's source, Home Assistant's history carries the bands with the readings."""
        return self.ha is not None and self.fast is None

    async def import_ranges(self):
        """Fold Home Assistant's readings into the 5-minute ranges: the last RANGE_KEEP_DAYS days the first time, then the last few hours.
        Nothing unless ranges_from_ha. A failure is logged once, then again when it recovers; the next try is a while off."""
        if not self.ranges_from_ha:
            return
        first, started = self._ha_ok is None, time.monotonic()
        try:
            found = await self.ha.ranges(time.time() - (RANGE_KEEP_DAYS * 86400 if first else HA_RECENT_SECONDS))
            marks = await self.storage.write(lambda w: w.put_ranges(self.mac, found))
        except Exception as e:
            self.ha.trouble.failed("history for the charts' ranges", f"{type(e).__name__}: {e}",
                                   "the ranges come from the bot's own readings")
            self._ha_ok = False
            return
        self.ha.trouble.ok()
        if first or not self._ha_ok:
            log.info("Backfill HA (Ecowitt): %d 5-min bands widened across %d series, %.0fs", marks, len(found), time.monotonic() - started)
        self._ha_ok = True

    async def _record_live(self, data: dict):
        """Count a live reading into the 5-minute bands (LiveMarks). Only a chart's shading depends on it, so a failure is logged and goes no
        further."""
        try:
            if found := self.live_marks.add(data, time.time()):
                await self.storage.write(lambda w: w.put_ranges(self.mac, found))
        except Exception as e:
            log.warning("Couldn't keep the live readings' ranges: %s", e)

    async def watch_live(self):
        """Ask Home Assistant for the live reading every LIVE_POLL_SECONDS, for ever. A reading newer than the last one found is kept for "weather
        now", the report and the rain alert (which is called at once, not at the next minute's refresh). A failure is logged once, with its
        recovery; the cloud, where there is one, answers the questions meanwhile."""
        if self.fast is None:
            return
        await self.pull_minutes()
        while True:
            await asyncio.sleep(LIVE_POLL_SECONDS)
            try:
                data = await self.fast.realtime(self.mac, ",".join(LIVE_GROUPS))
            except Exception as e:
                self.fast.ha.trouble.failed("live reading", e, f"asked again every {LIVE_POLL_SECONDS:g} s")
                continue
            self.fast.ha.trouble.ok()
            self._polled = time.time()
            if (at := newest(data)) <= self._seen:
                continue
            self._seen = at
            self._got_live(data)
            await self._keep_live(self._live[0], data)
            if time.time() - self._recorded >= LIVE_RECORD_SECONDS:
                self._recorded = time.time()
                await self._record_live(data)
            await self.pull_minutes()
            for callback in self.on_live:
                await safely(callback)

    async def pull_minutes(self):
        """Take Home Assistant's minute readings since the newest held (the last KEEP_HOURS at first, or after the bot was down) into the
        minute store, for the short charts. A failure is logged once; the charts then use the 5-minute readings."""
        now = time.time()
        since = max(await self.storage.read(newest_minute_on, self.mac) or 0, now - KEEP_HOURS * 3600)
        try:
            data = await self.fast.history(self.mac, "1min", naive_local(since, self.tz), naive_local(now, self.tz), ",".join(self.groups))
            await self.storage.write(lambda w: w.put_minutes(self.mac, data, now))
        except Exception as e:
            self.fast.ha.trouble.failed("minute readings", e, "short charts use the 5-minute readings")
            return
        self.fast.ha.trouble.ok()

    async def live_rain(self) -> tuple[int, dict] | None:
        """The gauge's latest rate and daily total as a history-shaped row, for the rain alert: the 5-minute history lags
        by up to 5 minutes, this is about a minute old. The same request keeps every live reading ready for "weather now" and the
        report (self._live), one request a minute. None when unavailable."""
        try:
            data = await self._live_data(LIVE_GROUPS, max_age=0, urgent=False, kept=False)   # the newest minute: the poll's, else a request
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
        if self._live_ok is False:
            log.info("Recovered: %s", live_summary(data))
        self._live_ok = True
        return ts, row

    # ---------- keeping warm ----------
    async def warm(self, fresh: bool = True) -> str:
        """Every resolution's newest readings for every group: the last 7 days at 30 minutes, yesterday and today at
        5 minutes, the last 3 days at 4 hours and daily (only the unsettled tails go to Ecowitt; the archive keeps the
        settled history). Not before the station is found (a first start: the warm-up's device list)."""
        await self.listed.wait()
        now = self.now()
        today = datetime.combine(now.date(), datetime.min.time())
        windows = {"5min": today - timedelta(days=1), "30min": today - timedelta(days=6),   # (in this order: what questions on
                   "4hour": today - timedelta(days=3), "1day": today - timedelta(days=3)}    # the last hours and days read first)
        fetchers = {cycle: self.fetcher(self.groups) for cycle in windows}

        await asyncio.gather(*(fetchers[cycle].get(cycle, start, now, refresh=fresh and cycle in FINE_CYCLES, load=False)
                               for cycle, start in windows.items()))
        return f"Ecowitt {sum(f.calls for f in fetchers.values())} req"

    async def readings(self, cycle: str, start: datetime, end: datetime,
                       groups: list[str] | None = None) -> list[tuple[int, dict]]:
        """Readings for the alert monitors ([(epoch, {"group.field": value, ...})]), from the storage (the newest fetched first)."""
        return await self.fetcher(groups or self.groups).get(cycle, start, end)

    async def recent(self, hours: float) -> list[tuple[int, dict]]:
        """The last `hours` of 5-minute readings: real hours, so the night the clocks change is no longer or shorter."""
        now = self.now()
        return await self.readings("5min", naive_local(local_epoch(now, self.tz) - hours * 3600, self.tz), now)

    async def close(self):
        if (flight := self._live_flight) and not flight[1].done():
            flight[1].cancel()
        try:
            await self.api.close()
        finally:
            self.storage.close()
            if self.ha:
                await self.ha.close()
