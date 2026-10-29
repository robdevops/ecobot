"""Air quality from your own AirGradient monitor, as a data source (peer of Ecowitt):
start() checks the monitor, tools are what the model can call, warm()/poke() keep recent
readings ready, backfill() caches the monitor's whole history, current() feeds the alert monitor.

Its readings come from each of its sources that is set (AIRGRADIENT_SOURCE; docs/storage-plan.md 3.12), each filling only what the ones
before it lack: the monitor's own server (local.py: read every minute, each 5 minutes' readings averaged into a row with their lowest and
highest as its band, and each reading kept as a minute), Home Assistant (homeassistant.py: its states of the monitor sampled the same way
while the monitor does not answer the bot, and its history read back for the times the bot did not read it at all), and AirGradient's
cloud (the history before Home Assistant's, the times both were down, and the monitor's name). A metric of a lower source is checked
against what the higher ones hold before it fills a gap: a metric that differs (the raw PM2.5 against the corrected one) fills none, and
is said once with the fix.

AirGradient's documented API:
  current: GET /public/api/v1/locations/{id}/measures/current?token=...
  history: GET /public/api/v1/locations/{id}/measures/past?from=...&to=...&token=...

Finished days are kept in the bot's file (readings.py, lib/storage), so questions are answered without asking
AirGradient unless a day is missing. Reports AirGradient's corrected (calibrated) values
where available, plus a US EPA AQI and band for PM2.5.
"""

import asyncio
import copy
import statistics
import logging
import time
from datetime import date, datetime, timedelta, UTC
from itertools import pairwise
from typing import NamedTuple

import httpx

from .. import intent
from ..captions import CHART_HINT, wants_chart
from ..config import Config
from ..sources import Source
from ..ecowitt.homeassistant import HomeAssistant
from ..ecowitt.live import BUCKET
from ..ecowitt.query import KEEP_HOURS, MINUTE_HOURS
from ..lines import RANGE_KEEP_DAYS, Plotted, Sample, build_line
from ..panels import panel_for
from ..specs import Chart, Line
from ..storage import Storage
from ..storage.catalogue import BOT, CLOUD, HOME_ASSISTANT
from ..timeutil import day_bounds, day_text, local_date, local_epoch, parse_local, to_local, when
from ..tools import Tool, Turn, json_result
from ..warm import Warmer
from .days import DESCRIPTION as DAYS_DESCRIPTION, PARAMETERS as DAYS_PARAMETERS, air_days_tool
from . import metrics
from .metrics import AIR_PANELS, ALL_METRICS, EXTRA, EXTRA_LABELS, EXTRA_UNITS, MARK_LOW, LABELS, METRICS, STORED, normalise, pm25_aqi, rating, value_of, without_spikes
from .homeassistant import (ANCHORS, MIN_COMPARED, checked, entity_ids, find_prefix, live as ha_live, minutes as ha_minutes, ranges as ha_ranges,
                            same_reading, verdicts)
from .local import LocalSensor
from .readings import NOT_HELD, AirReadings, Held

log = logging.getLogger(__name__)

API = "https://api.airgradient.com/public/api/v1/locations/{loc}/measures/{kind}"
MAX_DAYS = 400                    # longest history period per question
MAX_REQUEST_DAYS = 9              # the API allows 10 days per request; a day of margin for clock changes
MAX_INLINE_DAYS = 30              # missing days fetched while answering; more are left to the backfill
FRESH_SECONDS = 300               # the current reading and today's history are reused this long
HA_RECENT_SECONDS = 3 * 3600      # Home Assistant's ranges are read back for the last few hours each time (RANGE_KEEP_DAYS the first time)
SAMPLE_SECONDS = 60               # with the monitor's own server, or Home Assistant's states of it: how often it is read (the keep-warm interval)
MIN_SAMPLES = 3                   # a 5-minute average of fewer readings than this is not kept (the cloud fills that gap)
SOURCE_NAMES = {BOT: "the monitor", HOME_ASSISTANT: "Home Assistant", CLOUD: "the cloud"}
FIX = {"pm2_5": "set the monitor's PM2.5 correction to the dashboard's, or have it take its configuration from the dashboard",
       "air_temperature": "set the monitor's temperature correction to the dashboard's, or have it take its configuration from the dashboard",
       "air_humidity": "set the monitor's humidity correction to the dashboard's, or have it take its configuration from the dashboard"}
CORRECTIONS = {"none": "as measured, no correction", "epa_2021": "the monitor's EPA 2021 correction"}
GAP_GRACE = 120                   # a bucket is called missing only this long after it ends (its average is stored when the next one starts)
GAP_RETRY_SECONDS = 1800          # a gap in today's readings is asked of the cloud at most this often
MAX_GAP_SECONDS = 900             # the reading before the latest counts as its neighbour only if it is this recent
WARM_DAYS = 7                     # finished days kept ready by every refresh
BACKFILL_PACE = 1.0               # seconds between backfill requests
BACKFILL_EMPTY_STOP = 60          # this many empty days in a row, after some data: the sensor's data starts here
BACKFILL_EMPTY_BEFORE_DATA = 365  # how far back to look for any data at all (a long recent outage isn't the start)
BACKFILL_FAIL_STOP = 3            # this many failed requests in a row: give up until the next start
BACKFILL_MAX_DAYS = 1460

PARAMETERS = {
    "type": "object",
    "properties": {
        "chart": {"type": "boolean", "description": "Set true to send a chart (graph) with the answer. Without dates, "
                                                    "it covers the last 24 hours."},
        "metrics": {"type": "array", "items": {"type": "string", "enum": list(LABELS)},
                    "description": "Which metrics to chart (several are drawn as panels in one image; "
                                   "for 'all', list all six). Default: pm2_5."},
        "start_date": {"type": "string", "description": "Start of a past period, 'YYYY-MM-DD HH:MM:SS' local time. "
                                                        "Omit (with end_date) for the current reading."},
        "end_date": {"type": "string", "description": "End of the past period, 'YYYY-MM-DD HH:MM:SS' local time."},
    },
}
DESCRIPTION = ("Air quality from the owner's AirGradient outdoor sensor: PM2.5 (with US AQI and band), PM10, "
               "PM1, CO2, VOC and NOx indexes, each with a traffic-light rating. No dates: the current reading. "
               "With start_date/end_date (up to about a year): lowest, highest and average of each, with when they "
               "happened. chart=true sends a graph.")


def asked() -> tuple[str, dict]:
    """air_quality as a question about the sensor's own temperature or humidity is offered it: those two as metrics too."""
    parameters = copy.deepcopy(PARAMETERS)
    parameters["properties"]["metrics"]["items"]["enum"] = [*LABELS, *EXTRA]
    parameters["properties"]["metrics"]["description"] += " air_temperature and air_humidity are the sensor's own temperature and humidity."
    return (DESCRIPTION + " With metrics air_temperature / air_humidity it gives the sensor's own temperature (°C) and humidity (%), only "
            "when the question names the AirGradient's.", parameters)


class Latest(NamedTuple):
    """The latest reading as the air_quality tool gives it, and what the particulates alert also needs."""
    reading: dict                  # the sensor, the time, and each metric's value, unit and rating
    at: datetime | None            # when it was taken (UTC)
    sustained: dict[str, float]    # each metric's lowest over the last REQUIRE_CONSECUTIVE readings: what the air has been that long


class AirGradient(Source):
    """AirGradient client with its data kept warm and cached:
      - the current reading and today's history are refreshed every few minutes (warm);
      - finished days are fetched once and stored on disk, so they never need fetching again."""

    name = "AirGradient"

    def __init__(self, cfg: Config, transport=None, ha: HomeAssistant | None = None, storage: Storage | None = None,
                 owned: bool | None = None):
        """ha: the weather station's Home Assistant client, shared (so a failing Home Assistant is said once); else one of its own when
        Home Assistant is set. storage: the bot's file (lib/storage), the station's, shared; without one the source opens it itself.
        owned: the file is this source's to close (so when it opened it, or no station has it)."""
        self.loc, self.tz = cfg.airgradient_location or "local", cfg.tz   # (the stored readings are kept under this)
        self.token = cfg.airgradient_token
        self.cloud = cfg.airgradient_cloud    # without an account (or with AIRGRADIENT_SOURCE saying not) the cloud is never asked
        self.link = ("live chart", cfg.airgradient_dashboard) if cfg.airgradient_dashboard else None
        self.client = httpx.AsyncClient(timeout=15, transport=transport)
        self.local = LocalSensor(cfg.airgradient_local_url, cloud=self.cloud) if cfg.airgradient_local else None   # the monitor's own server
        owned = (storage is None) if owned is None else owned
        storage = storage or Storage(cfg.storage_path, cfg.tz)
        self.store = AirReadings(storage, self.loc, self.tz, owned)
        self._current: tuple[dict, float] | None = None       # (raw row, fetched at)
        self._names: dict = {}                                # the sensor's name and type, from the last cloud reading
        self._recent: dict[date, tuple[list[dict], float]] = {}  # today / just-finished days, refreshed
        self._locks: dict = {}
        self.requests = 0
        self.warmer = Warmer(self.warm, SAMPLE_SECONDS) if self.local else Warmer(self.warm)   # (Home Assistant's states too, once found)
        self._bucket: tuple[int, list[dict]] | None = None   # the bucket being averaged: (its start, the readings so far)
        self._bucket_src = BOT                                # ... and whose readings they are (the monitor's, or Home Assistant's states)
        self._sampled: bool | None = None                     # did the last sample find a reading (the monitor's or Home Assistant's)?
        self._gap_tried: dict[date, float] = {}               # when the cloud was last asked to fill a gap in a day
        self.ha = (ha or (HomeAssistant(cfg.homeassistant_url, cfg.homeassistant_token) if cfg.homeassistant else None)
                   if cfg.airgradient_source in ("auto", "homeassistant") else None)   # (a client passed is the station's, shared)
        self._own_ha = ha is None and self.ha is not None
        self._ha_ids: dict[str, str] | None = None            # Home Assistant's entities of the monitor (entity id -> metric), once found
        self._ha_ok: bool | None = None                       # Home Assistant's history: None until first read
        self._ha_said: set[str] = set()                       # what was said once about it: no sensor, a metric that is another variant
        self._verdict: dict[int, dict[str, tuple[int, int]]] = {HOME_ASSISTANT: {}, CLOUD: {}}   # per source, per metric: (five minutes compared, agreed)
        self._correction: str | None = None                   # how the monitor corrects its PM2.5, from its own configuration
        self.tools = [Tool("air_quality", DESCRIPTION, PARAMETERS, self.handle, asked=asked), Tool("air_days", DAYS_DESCRIPTION, DAYS_PARAMETERS, self._days)]

    async def start(self):
        try:
            row = await self._current_row(refresh=True)
            if self.ha is not None:
                await self._ha_entities()   # (found now, so the line below says whether Home Assistant has the monitor)
            if self.cloud and (self.local or self._ha_ids):   # the cloud is still asked once: it names the sensor, and its token and location are checked
                self._names = {k: v for k, v in (await self._get("current")).items() if k in ("locationName", "locationType")}
                row = {**row, **self._names}
                self._current = (row, self._current[1])
            elif self.local and not self.cloud and not self._ha_ids:   # nothing to check its readings against: say how it corrects them
                self._correction = self._correction_of(await self.local.config())
            if self.cloud and self._sampled:   # today's gaps asked of the cloud now (warm would in a moment): its check against what the bot holds, for the line
                await self._day_rows(self.now().date())
            self.summary = f"AirGradient: {self._summary(row)}"
        except Exception as e:  # keep going: the sensor may just be briefly unreachable
            log.warning("AirGradient %s not readable yet: %s", f"location {self.loc}" if self.cloud else "sensor", e)

    @staticmethod
    def _correction_of(config: dict | None) -> str | None:
        """What the monitor's configuration says of its PM2.5: the dashboard's correction when it takes its configuration from there, else
        the algorithm it applies, as CORRECTIONS names it."""
        if not config:
            return None
        if config.get("configurationControl") == "cloud":
            return "the dashboard's correction"
        pm02 = (config.get("corrections") or {}).get("pm02")
        algorithm = pm02.get("correctionAlgorithm") if isinstance(pm02, dict) else None
        return None if not algorithm else CORRECTIONS.get(algorithm, f"the monitor's {algorithm} correction")

    def _summary(self, row: dict) -> str:
        """The start-up line: the sensor's name, its cloud location, where the readings come from and how often, and the PM2.5's
        correction where known (or that a metric differs between two sources, with the fix)."""
        local = self.local
        sensor = (local.describe() + ("" if local.describe().split(":")[0] == local.address[0] else f" ({local.address[0]})")
                  if local and local.working and local.address else None)
        via = [sensor] if sensor else [f"{local.describe()} not answering"] if local else []
        if self._ha_ids:
            via.append("Home Assistant")
        if self.cloud:
            via.append("cloud fallback" if sensor or self._ha_ids else "AirGradient cloud")
        name = row.get("locationName") or (local and local.serial) or "sensor"
        line = f"'{name}'{f' ({self.loc})' if self.cloud else ''} via {', '.join(via)} (every {self.warmer.interval:g}s)"
        notes = [f"{LABELS.get(name) or EXTRA_LABELS.get(name, name)} differs between {SOURCE_NAMES[src]} and what the bot keeps ({FIX.get(name, 'check the correction each applies')})"
                 for src, found in self._verdict.items() for name, v in found.items() if not same_reading(v)]
        if self._correction:
            notes.append(f"PM2.5: {self._correction}")
        return line + "".join(f"; {n}" for n in notes)

    async def close(self):
        await super().close()
        if self._own_ha:
            await self.ha.close()

    async def _ha_entities(self, states: list[dict] | None = None) -> dict[str, str] | None:
        """Home Assistant's entities of the monitor (entity id -> metric), found once by their names (homeassistant.find_prefix: the one
        whose reading is nearest the bot's when it has several); None when it has none (said once). Raises when it does not answer."""
        if self._ha_ids is not None:
            return self._ha_ids
        states = await self.ha.states() if states is None else states
        mine = {a: value_of(self._current[0], a) for a in ANCHORS} if self._current else None
        prefix, sensors = find_prefix(states, mine)
        if prefix is None:
            if "none" not in self._ha_said:
                self._ha_said.add("none")
                log.info("Home Assistant has no AirGradient monitor (no entity ending in _%s): nothing of the air comes from it",
                         " or _".join(ANCHORS))
            return None
        if sensors > 1 and "several" not in self._ha_said:
            self._ha_said.add("several")
            log.info("Home Assistant has %d AirGradient monitors; using %s*, whose reading is nearest this one's", sensors, prefix)
        self._ha_ids = entity_ids(prefix, states)
        if not self.local:   # its states are sampled as the monitor's own readings would be
            self.warmer.interval = SAMPLE_SECONDS
        return self._ha_ids

    async def import_history(self):
        """Fold what Home Assistant kept of the monitor (homeassistant.py) into the store: the readings of the five minutes the bot has
        none of its own for (each five minutes' mean, with its lowest and highest as the band; in place of the cloud's), the bands of
        those it has, and the minutes of the last KEEP_HOURS; the last RANGE_KEEP_DAYS days the first time (as far back as it has them),
        then the last few hours. A metric whose five-minute means disagree with the readings the bot took from the monitor itself (the
        raw PM2.5 against the corrected one) fills nothing and gives no bands (said once); with none of those, its values are taken as they
        are. Nothing when no Home Assistant is set or it has no AirGradient monitor (said once). A failure is logged once, with its
        recovery (Home Assistant's own account of it); what the bot has stays."""
        if self.ha is None:
            return
        first, started, now = self._ha_ok is None, time.monotonic(), time.time()
        start = int(now - (RANGE_KEEP_DAYS * 86400 if first else HA_RECENT_SECONDS)) // BUCKET * BUCKET
        minutes_from = max(start, int(now - KEEP_HOURS * 3600) // 60 * 60)
        try:
            ids = await self._ha_entities()
            if ids is None:
                self.ha.trouble.ok()
                self._ha_ok = True
                return
            found: dict = {}
            minutes: list[dict] = []
            for chunk in range(start, int(now), 86400):   # a day a request
                until = min(chunk + 86400, now)
                history = await self.ha.history(list(ids), chunk - BUCKET, until)
                for name, got in ha_ranges(history, ids, self.ha.units, chunk, until - 1, now).items():
                    found.setdefault(name, {}).update(got)
                if until > minutes_from:
                    minutes += ha_minutes(history, ids, self.ha.units, max(chunk, minutes_from), until - 1, now)
            own = await self.store.load(start, int(now), srcs=(BOT,))   # what the bot read from the monitor itself: the check, and the rest is filled
            keep, fill, verdict = checked(found, own)
            self._verdict[HOME_ASSISTANT].update({n: v for n, v in verdict.items() if v[0] >= MIN_COMPARED})
            added = await self.store.add_spreads(keep)
            filled = await self.store.merge(fill, True, HOME_ASSISTANT)
            kept = await self.store.add_minutes([self._allowed(m, HOME_ASSISTANT) for m in minutes], HOME_ASSISTANT, now)
        except Exception as e:
            self.ha.trouble.failed("history of the air monitor", f"{type(e).__name__}: {e}", "the air's readings come from the bot's own and the cloud's")
            self._ha_ok = False
            return
        self.ha.trouble.ok()
        for name, (compared, agreed) in verdict.items():
            if name not in keep and name not in self._ha_said:
                self._ha_said.add(name)
                log.warning("Home Assistant's %s isn't the reading the bot keeps (it agreed in %d of %d five minutes: perhaps the monitor's raw "
                            "value), so it fills no gaps and its ranges aren't used: %s", LABELS.get(name) or EXTRA_LABELS.get(name, name),
                            agreed, compared, FIX.get(name, "check the correction each applies"))
        if first or not self._ha_ok:
            log.info("Backfill Home Assistant (AirGradient): %d 5-min bands widened, %d filled, %d kept, %.0fs", added, filled, kept,
                     time.monotonic() - started)
        self._ha_ok = True

    def _allowed(self, row: dict, src: int) -> dict:
        """This reading of a source's without the metrics the verdicts say differ from what the bot keeps (they fill no gaps)."""
        verdict = self._verdict[src]
        return {k: v for k, v in row.items() if k not in STORED or same_reading(verdict.get(k, (0, 0)))}

    def _judge(self, src: int, rows: list[dict], others: list[dict]) -> list[dict]:
        """These readings of a source's (buckets' values, [{"ts", metric...}]) against the readings the bot holds from the sources ranked
        above it, over the five minutes both hold: each metric's verdict is kept where enough were compared (homeassistant.verdicts), and
        a metric found to differ, or to agree again, is said. Returns the readings with the differing metrics taken out."""
        found = {name: {r["ts"] // BUCKET * BUCKET: r[name] for r in rows if name in r} for name in STORED}
        for name, v in verdicts(found, others).items():
            if v[0] < MIN_COMPARED:
                continue
            before = self._verdict[src].get(name)
            self._verdict[src][name] = v
            if before is not None and same_reading(before) == same_reading(v) or before is None and same_reading(v):
                continue
            label = LABELS.get(name) or EXTRA_LABELS.get(name, name)
            if same_reading(v):
                log.info("AirGradient: %s's %s agrees with the bot's readings again (%d of %d five minutes), so it fills gaps again",
                         SOURCE_NAMES[src], label, v[1], v[0])
            else:
                log.warning("AirGradient: %s's %s isn't the reading the bot keeps (it agreed in %d of %d five minutes), so it fills no gaps: %s",
                            SOURCE_NAMES[src], label, v[1], v[0], FIX.get(name, "check the correction each applies"))
        return [row for r in rows if len(row := self._allowed(r, src)) > 1]

    def describe(self) -> str:
        return f"AirGradient outdoor air-quality sensor (location {self.loc}) (people say \"ag\", \"airgradient\", \"air gradient\", \"aq\" or \"air quality\")"

    def wants(self, text: str) -> bool:
        """Should a question start refreshing this source? Only air-quality questions."""
        return intent.mentions_air(text)

    async def _get(self, kind: str, **params) -> dict | list:
        """The reply: an object for "current", a list of readings for "past". A period with no readings is an empty list (AirGradient
        says so with a 404); a reply of any other shape raises, so it is never stored as days without readings."""
        if not self.cloud:
            raise RuntimeError("AirGradient's cloud is not configured")
        self.requests += 1
        res = await self.client.get(API.format(loc=self.loc, kind=kind), params={**params, "token": self.token})
        if res.status_code == 404 and kind == "past" and "no data" in res.text.lower():   # "No data available" for the period
            return []
        if res.status_code != 200:
            raise RuntimeError(f"AirGradient {kind} failed: HTTP {res.status_code}")
        data = res.json()
        if kind == "past" and isinstance(data, dict) and isinstance(data.get("data"), list):
            data = data["data"]
        if not isinstance(data, list if kind == "past" else dict):
            raise RuntimeError(f"AirGradient {kind} sent something unexpected: {res.text[:100]}")
        return data

    def _lock(self, key) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    # ---------- cached data ----------
    async def _current_row(self, refresh: bool = False) -> dict:
        async with self._lock("current"):
            if refresh or not self._current or time.time() - self._current[1] > FRESH_SECONDS:
                self._current = (await self._fetch_current(), time.time())
            return self._current[0]

    async def _fetch_current(self) -> dict:
        """The live reading: from the monitor's own server when AIRGRADIENT_LOCAL_URL answers, else Home Assistant's states of it (each
        with the cloud's name for the sensor, last heard), else the cloud."""
        row, _ = await self._sample()
        if row is not None:
            return row
        if not self.cloud:
            raise RuntimeError("the monitor did not answer")
        row = await self._get("current")
        self._names = {k: v for k, v in row.items() if k in ("locationName", "locationType")}
        return row

    async def _sample(self) -> tuple[dict | None, int]:
        """(the monitor's reading now, whose it is): the monitor's own server when it answers (BOT), else Home Assistant's states of it
        (HOME_ASSISTANT); (None, CLOUD) when neither answers. Never raises; Home Assistant failing is said once."""
        if self.local and (row := await self.local.read()):
            self._sampled = True
            return {**(self._names or self._sensor_name()), **row}, BOT
        if self.ha is not None:
            try:
                states = await self.ha.states()
                if (ids := await self._ha_entities(states)) and (row := ha_live(states, ids, time.time())):
                    self.ha.trouble.ok()
                    self._sampled = True
                    return {**(self._names or self._sensor_name()), **row}, HOME_ASSISTANT
                self.ha.trouble.ok()
            except Exception as e:
                self.ha.trouble.failed("live reading of the air monitor", f"{type(e).__name__}: {e}",
                                       "the cloud answers meanwhile" if self.cloud else "asked again at the next refresh")
        self._sampled = False
        return None, CLOUD

    def _sensor_name(self) -> dict:
        """What the sensor is called when the cloud has not said: its serial number."""
        return {"locationName": f"AirGradient {self.local.serial}"} if self.local and self.local.serial else {}

    def _final(self, day: date) -> bool:
        """Over for more than an hour, so its data won't change."""
        return self.now() > datetime.combine(day, datetime.min.time()) + timedelta(days=1, hours=1)

    async def _held(self, days: list[date]) -> tuple[dict[date, Held], list[date]]:
        """(what is stored of each finished day, in one query: NOT_HELD for one not fetched yet, or not finished; the finished days that
        aren't stored yet)."""
        stored = await self.store.held(days)
        held = {d: stored[d] if self._final(d) else NOT_HELD for d in days}
        return held, [d for d, h in held.items() if h.n is None and self._final(d)]

    async def _past(self, start: int, end: int) -> list[dict]:
        """Readings between two epochs (never past now), normalised and oldest first. Without a cloud account there are none to ask for."""
        if not self.cloud:
            return []
        utc = lambda ts: datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = await self._get("past", **{"from": utc(start), "to": utc(min(end, time.time()))})
        return sorted((normalise(r) for r in raw if isinstance(r, dict) and r.get("timestamp")), key=lambda r: r["ts"])

    async def _fetch_days(self, days: list[date]) -> dict[date, Held]:
        """Fetch finished days and store them, in as few requests as the API allows (it takes up to 10 days at a time; we use 9).
        Returns what each day has stored."""
        days, stored = sorted(days), {}
        for d in [d for d in days if await self.store.has_own(d)]:   # the bot's own readings, or Home Assistant's: only the gaps are fetched
            stored[d] = await self._complete_day(d)
            days.remove(d)
        while days and self.cloud:
            window = [d for d in days if (d - days[0]).days < MAX_REQUEST_DAYS]
            days = days[len(window):]
            by_day: dict = {d: [] for d in window}
            for r in await self._past(day_bounds(window[0], self.tz)[0], day_bounds(window[-1], self.tz)[1]):
                by_day.get(local_date(r["ts"], self.tz), []).append(r)
            for d, day_rows in by_day.items():
                stored[d] = await self.store.save_day(d, day_rows)
                self._recent.pop(d, None)
        return stored

    async def _day_rows(self, day: date, refresh: bool = False) -> list[dict]:
        """All of one local day's readings, normalised. Finished days come from the disk cache (and
        are fetched once if missing); today, and a just-finished day, are refetched when a few
        minutes old."""
        final = self._final(day)
        async with self._lock(day):
            if final:
                if (await self._held([day]))[1]:
                    await self._fetch_days([day])
                return await self.store.load_day(day)
            if self._sampled or (not self.cloud and (self.local or self._ha_ids)):   # the bot's own readings are the day's
                return await self._recorded_day_rows(day)
            cached = self._recent.get(day)
            if cached and not refresh and time.time() - cached[1] <= FRESH_SECONDS:
                return cached[0]
            rows = await self._past(*day_bounds(day, self.tz))
            self._recent[day] = (rows, time.time())
            return rows

    # ---------- the monitor's own readings, kept by the bot ----------
    async def _record(self) -> bool:
        """Read the monitor itself, else Home Assistant's states of it; True if either answered. The readings are averaged into 5-minute
        buckets, and a bucket with enough of them is stored (as the cloud would give it, with whose readings they are) when the next one
        starts; each reading is also kept as a minute. Of Home Assistant's, only the metrics that agree with what the bot keeps."""
        row, src = await self._sample()
        if row is None:
            return False
        now = time.time()
        self._current = (row, now)
        bucket = int(now) // BUCKET * BUCKET
        if self._bucket and (self._bucket[0] != bucket or self._bucket_src != src):
            await self._flush_bucket()
        if self._bucket is None:
            self._bucket, self._bucket_src = (bucket, []), src
        sample = {n: v for n in STORED if (v := value_of(row, n)) is not None}
        if src != BOT:
            sample = self._allowed(sample, src)
        self._bucket[1].append(sample)
        await self.store.add_minutes([{"ts": int(now) // 60 * 60, **sample}], src, now)
        return True

    @staticmethod
    def _average(bucket: int, samples: list[dict]) -> dict:
        row, spread = {"ts": bucket}, {}
        for name in STORED:
            values = [s[name] for s in samples if name in s]
            if values:
                row[name] = statistics.fmean(values)
            if len(values) >= 2:   # the lowest and highest reading in the bucket: what a chart shades
                spread[name] = (min(values), max(values))
        return {**row, **({"_range": spread} if spread else {})}

    async def _flush_bucket(self):
        bucket, samples = self._bucket
        self._bucket = None
        if len(samples) >= MIN_SAMPLES:
            await self.store.add_recorded(self._average(bucket, samples), self._bucket_src)

    def _missing(self, day: date, rows: list[dict], until: float) -> list[int]:
        """The start of each 5-minute bucket of this day, over by `until`, that the cloud could fill: one lacking a reading of some metric
        its verdict does not rule out; without the cloud, one with no reading at all."""
        lo, hi = day_bounds(day, self.tz)
        wanted = [n for n in STORED if same_reading(self._verdict[CLOUD].get(n, (0, 0)))] if self.cloud else []
        whole = {r["ts"] // BUCKET * BUCKET for r in rows if all(n in r for n in wanted)}
        return [t for t in range(lo // BUCKET * BUCKET, min(hi, int(until) // BUCKET * BUCKET), BUCKET) if t not in whole]

    async def _fill(self, day: date, start: int, end: int):
        """Ask the cloud for this span of the day (epochs), check its readings against the bot's own and Home Assistant's over the five
        minutes both hold (_judge), and keep what it has of the agreeing metrics where they lack it (the storage keeps theirs)."""
        lo, hi = day_bounds(day, self.tz)
        got = [r for r in await self._past(start, end) if lo <= r["ts"] < hi]
        others = await self.store.load(lo, hi, srcs=(BOT, HOME_ASSISTANT))
        await self.store.merge(self._judge(CLOUD, got, others), self._final(day))

    async def _recorded_day_rows(self, day: date) -> list[dict]:
        """An unfinished day (today, or one that has just ended) while the monitor or Home Assistant answers: the stored readings, the
        bucket being averaged as the last one, and the cloud asked, at most every GAP_RETRY_SECONDS, for the span of the buckets that are
        missing (what it has of them fills only what the bot lacks)."""
        rows = await self.store.load_day(day)
        if self.cloud and time.time() - self._gap_tried.get(day, 0) > GAP_RETRY_SECONDS and (missing := self._missing(day, rows, time.time() - GAP_GRACE)):
            self._gap_tried[day] = time.time()
            try:
                await self._fill(day, missing[0], missing[-1] + BUCKET)
                rows = await self.store.load_day(day)
            except Exception as e:   # the stored readings still stand
                log.warning("AirGradient: couldn't fill the gaps of %s from the cloud: %s", day, e)
        if self._bucket and self._bucket[1] and local_date(self._bucket[0], self.tz) == day and self._bucket[0] not in {r["ts"] for r in rows}:
            rows = [*rows, self._average(*self._bucket)]
        return rows

    async def _complete_day(self, day: date) -> Held:
        """A finished day the bot holds readings of from the monitor or Home Assistant: the cloud is asked for the whole day once (one
        request: the day's check of each metric against the bot's, and its readings fill only what the bot lacks), and the day is noted
        as done. Without the cloud, a day with gaps is left open, in case an account is added later."""
        lo, hi = day_bounds(day, self.tz)
        rows = await self.store.load_day(day)
        if self.cloud:
            await self._fill(day, lo, hi)
            rows = await self.store.load_day(day)
        elif self._missing(day, rows, hi):
            return Held(len(rows), False)
        await self.store.mark_day(day)
        self._recent.pop(day, None)
        return Held(len(rows), False)

    async def warm(self, fresh: bool = True) -> str:
        """Current reading, today, and the last week of finished days."""
        before, today = self.requests, self.now().date()
        if not ((self.local or self._ha_ids) and await self._record()):   # (the monitor, or Home Assistant's states of it: the reading is also kept)
            await self._current_row(fresh)
        await self._day_rows(today, fresh)
        week = [today - timedelta(days=n) for n in range(1, WARM_DAYS + 1)]
        await self._fetch_days((await self._held(week))[1])  # one request for all of them
        for day in week:
            if not self._final(day):  # a day that just ended is still refetched for a while; finished ones are stored
                await self._day_rows(day)
        return f"AirGradient {self.requests - before} req"

    async def backfill(self, pace: float | None = None, empty_stop: int | None = None,
                       empty_before_data: int | None = None, max_days: int = BACKFILL_MAX_DAYS) -> tuple[int, int, int]:
        """Cache every finished day back to where the sensor's data starts (newest first, so recent
        charts are ready soonest; up to 9 days per request). Returns (days fetched, days failed,
        days cached in total)."""
        if not self.cloud:
            return 0, 0, 0
        pace = BACKFILL_PACE if pace is None else pace
        empty_stop = BACKFILL_EMPTY_STOP if empty_stop is None else empty_stop
        empty_before_data = BACKFILL_EMPTY_BEFORE_DATA if empty_before_data is None else empty_before_data
        today = self.now().date()
        fetched = failed = failed_run = empty_run = total = 0
        seen_data = False
        n = 1
        while n <= max_days:
            days = [today - timedelta(days=k) for k in range(n, min(n + MAX_REQUEST_DAYS, max_days + 1))]
            n += MAX_REQUEST_DAYS
            counts, missing = await self._held(days)
            if missing:
                try:
                    counts.update(await self._fetch_days(missing))
                    fetched, failed_run = fetched + len(missing), 0
                    if fetched // 30 != (fetched - len(missing)) // 30:
                        log.info("Backfill AirGradient: %d days fetched so far, now at %s", fetched, days[-1])
                except Exception as e:
                    failed, failed_run = failed + len(missing), failed_run + 1
                    log.warning("Backfill AirGradient of %s to %s failed: %s", missing[-1], missing[0], e)
                    if failed_run >= BACKFILL_FAIL_STOP:
                        break
                    continue
                finally:
                    await asyncio.sleep(pace)
            for d in days:  # newest first
                if (count := counts[d].n) is None:
                    continue
                total += 1
                seen_data = seen_data or count > 0
                empty_run = empty_run + 1 if count == 0 else 0
                # A gap after data means the sensor's history starts before it; a gap before any data is
                # most likely a recent outage, so look much further back before giving up
                if empty_run >= (empty_stop if seen_data else empty_before_data):
                    return fetched, failed, total
        return fetched, failed, total

    # ---------- the tool ----------
    async def handle(self, args: dict, turn: Turn | None = None) -> str:
        """The air_quality tool: current reading, or a summary of a past period."""
        turn = turn or Turn()
        try:
            turn.air_extra_used = turn.air_extra_used or any(m in EXTRA for m in args.get("metrics") or [])
            chart = wants_chart(args, turn)
            if args.get("start_date") or args.get("end_date") or chart:
                return json_result(await self.history(args.get("start_date"), args.get("end_date"),
                                                      chart=chart, metrics=args.get("metrics"), turn=turn))
            return json_result(await self.current([m for m in args.get("metrics") or [] if m in EXTRA]))
        except Exception as e:
            log.warning("AirGradient request failed: %s", e)
            return f"Error: couldn't read the air-quality sensor ({e})"

    async def _days(self, args: dict, turn: Turn | None = None) -> str:
        """The air_days tool: count, rank and total days from the stored readings."""
        return await air_days_tool(self.store, self.tz, args, turn)

    async def _previous_rows(self, ts: int, count: int) -> list[dict]:
        """Up to `count` of today's cached readings just before `ts`, each within MAX_GAP_SECONDS of the next, oldest first."""
        today = to_local(ts, self.tz).date()
        earlier, got, after = [r for r in await self._day_rows(today) if r["ts"] < ts], [], ts
        for r in reversed(earlier):
            if len(got) >= count or after - r["ts"] > MAX_GAP_SECONDS:
                break
            got.insert(0, r)
            after = r["ts"]
        return got

    async def latest(self, extras: list[str] = ()) -> Latest:
        """The latest reading. A rating (and PM2.5's AQI band) is the level the last two readings both reached, so a passing
        spike is not called poor until the next reading agrees; the value shown is always the latest one. The readings before it
        come from today's history: when that can't be read, the latest is rated alone."""
        row = await self._current_row()
        at = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")) if row.get("timestamp") else None
        need = metrics.REQUIRE_CONSECUTIVE - 1   # the readings before the latest that `sustained` takes in
        try:
            earlier = await self._previous_rows(int(at.timestamp()), max(need, 1)) if at else []
        except Exception as e:   # only the history failing: the latest reading still stands
            log.warning("AirGradient: today's readings unavailable (%s); rating the latest alone", e)
            earlier = []
        previous, held = (earlier[-1] if earlier else {}), (earlier[-need:] if need else [])
        reading = {"sensor": row.get("locationName"), "sensor_type": row.get("locationType"),
                   "time": when(int(at.timestamp()), self.tz) if at else None}
        sustained = {}
        for name, (_, unit) in METRICS.items():
            v = value_of(row, name)
            if v is None:
                continue
            rated = min(v, previous.get(name, v))   # thresholds only rise: the lower value's zone is the one both reached
            reading[name] = {"value": v, "unit": unit, "rating": rating(name, rated)}
            if reading[name]["rating"] != rating(name, v):
                reading[name]["rating_note"] = "rated on the last two readings: the previous one was lower"
            if name == "pm2_5":
                aqi, band = pm25_aqi(rated)
                reading[name].update(aqi_us=aqi, band=band)
            sustained[name] = min([v, *(r[name] for r in held if name in r)])
        for name in extras:   # the sensor's own temperature and humidity, only when asked for: no rating
            if name in EXTRA and (v := value_of(row, name)) is not None:
                reading[name] = {"value": v, "unit": EXTRA_UNITS[name]}
        return Latest(reading, at, sustained)

    async def current(self, extras: list[str] = ()) -> dict:
        """The latest reading as the air_quality tool gives it (see latest); `extras`: the sensor's own temperature and humidity, if asked."""
        return (await self.latest(extras)).reading

    async def rows(self, t0: datetime, t1: datetime) -> tuple[list[dict], int, list[date]]:
        """(readings in [t0, t1] (naive local times), days that only exist as hourly averages, older days still waiting for
        the backfill and left out). Finished days not yet stored are fetched first, up to MAX_INLINE_DAYS. A single-reading spike
        of the particles is a glitch of the sensor: it is taken out here, once, so no figure or chart has it."""
        days = [t0.date() + timedelta(days=k) for k in range((t1.date() - t0.date()).days + 1)]
        held, missing = await self._held(days)
        skipped = missing[:-MAX_INLINE_DAYS] if self.cloud and len(missing) > MAX_INLINE_DAYS else []  # the oldest wait for the backfill
        held.update(await self._fetch_days([d for d in missing if d not in skipped]))
        finished = [d for d in days if self._final(d)]   # all stored by now but the skipped: read in one query
        rows = await self.store.load(day_bounds(finished[0], self.tz)[0], day_bounds(finished[-1], self.tz)[1]) if finished else []
        for d in days[len(finished):]:   # today, and a day that has just ended: fetched, not stored (but their ranges may be)
            rows += await self._with_spreads(await self._day_rows(d), d)
        hourly = sum(1 for h in held.values() if h.n and h.hourly)
        lo, hi = local_epoch(t0, self.tz), local_epoch(t1, self.tz)
        if hi - lo <= MINUTE_HOURS * 3600 and (minutes := await self.store.load_minutes(lo, hi + 1)):   # a short period: a reading a minute
            have = {m["ts"] // BUCKET * BUCKET for m in minutes}
            rows = sorted([*minutes, *(r for r in rows if r["ts"] // BUCKET * BUCKET not in have)], key=lambda r: r["ts"])
        return [r for r in without_spikes(rows) if lo <= r["ts"] <= hi], hourly, skipped

    async def _with_spreads(self, rows: list[dict], day: date) -> list[dict]:
        """These readings of a day with the ranges the store holds for them (Home Assistant's, for a day the cloud gave): a reading's own
        range, the bot's, wins."""
        ranges = await self.store.spreads(*day_bounds(day, self.tz))
        return [{**r, "_range": {**ranges[r["ts"]], **r.get("_range", {})}} if r["ts"] in ranges else r for r in rows]

    async def history(self, start: str | None, end: str | None, chart: bool = False,
                      metrics: list[str] | None = None, turn: Turn | None = None) -> dict:
        turn = turn or Turn()
        now = self.now()
        try:   # a date the model sends, with or without an offset; a bad one is its mistake, not the sensor's
            t0 = parse_local(start, self.tz) if start else now - timedelta(days=1)
            t1 = min(parse_local(end, self.tz), now) if end else now
        except ValueError as e:
            return {"error": f"bad date ({e}); use 'YYYY-MM-DD HH:MM:SS'"}
        trimmed = t0 < t1 - timedelta(days=MAX_DAYS)
        t0 = max(t0, t1 - timedelta(days=MAX_DAYS))
        if t0 >= t1:
            return {"error": "start must be before end"}
        chart = wants_chart({"chart": chart}, turn, t0, t1)
        before, started = self.requests, time.perf_counter()
        rows, hourly, skipped = await self.rows(t0, t1)
        turn.times["data"] = turn.times.get("data", 0.0) + time.perf_counter() - started   # for the reply's timing line
        log.debug("AirGradient %s to %s: %d readings, %d req", f"{t0:%Y-%m-%d}",
                f"{t1:%m-%d %H:%M}", len(rows), self.requests - before)
        out = {"sensor_type": "outdoor", "period": f"{day_text(t0)} - {day_text(t1)}",
               "readings": len(rows)}
        units = {**{name: unit for name, (_, unit) in METRICS.items()},
                 **{name: EXTRA_UNITS[name] for name in metrics or [] if name in EXTRA}}   # the sensor's own temperature and humidity, only when asked for
        for name, unit in units.items():
            if pts := [(r["ts"], r[name]) for r in rows if name in r]:
                out[name] = self._entry(name, pts, unit)
        if trimmed:
            out["note_period"] = f"Only the last {MAX_DAYS} days can be covered per question; this covers {out['period']}."
        if hourly:
            out["note_resolution"] = (f"{hourly} of the days in this period only exist as hourly averages (AirGradient keeps "
                                      "5-minute readings for recent months), so their highs and lows are hourly averages and "
                                      "short spikes are smoothed. Say so briefly if it matters to the answer.")
        if skipped:
            out["note_missing"] = (f"{len(skipped)} older day(s) aren't in the bot's cache yet and were left out; it is still "
                                   "downloading the sensor's history. Say so briefly.")
        if not rows:
            out["note"] = "No readings for this period."
        if chart and rows:
            chart_spec = self._chart([m for m in (*ALL_METRICS, *EXTRA) if m in (metrics or ["pm2_5"])] or ["pm2_5"], rows, out["period"],
                                     (t0.date(), t1.date()))
            if chart_spec:
                turn.charts.append(chart_spec)
                out["chart"] = CHART_HINT
        return out

    def _entry(self, name: str, pts: list[tuple[int, float]], unit: str) -> dict:
        """A metric's lowest, highest and average over these readings ((ts, value)), with when the extremes happened; a rated metric
        (METRICS) with the ratings of its high and average, and PM2.5 with their AQI and band too."""
        lo, hi = min(pts, key=lambda p: p[1]), max(pts, key=lambda p: p[1])
        entry = {"unit": unit, "low": lo[1], "low_time": when(lo[0], self.tz), "high": hi[1], "high_time": when(hi[0], self.tz),
                 "average": round(statistics.fmean(v for _, v in pts), 1)}
        if name in METRICS:
            entry["high_rating"], entry["average_rating"] = rating(name, hi[1]), rating(name, entry["average"])
            if name == "pm2_5":
                entry["high_aqi_us"], entry["high_band"] = pm25_aqi(hi[1])
                entry["average_aqi_us"], entry["average_band"] = pm25_aqi(entry["average"])
        return entry

    def _line(self, name: str, rows: list[dict]) -> tuple[Line, Plotted] | None:
        """One metric as a chart line, in its own units, and how it was drawn. The record high/low are the true readings.
        The readings themselves while they fit the point budget; more than that are bucketed (30 minutes ... a day): each
        bucket's mean, its range shaded."""
        pts = [(r["ts"], r[name]) for r in rows if name in r]
        if len(pts) < 2:
            return None
        gap = max(60, round(statistics.median(b[0] - a[0] for a, b in pairwise(pts))))
        spread = {r["ts"]: r["_range"][name] for r in rows if name in r and name in r.get("_range", {})}   # what the bot saw in a bucket it recorded
        plotted = build_line([Sample(t, v, *(((min(spread[t][0], v), max(spread[t][1], v)) if t in spread else (None, None))), gap)
                              for t, v in pts], self.tz, pts[-1][0] - pts[0][0])
        if plotted is None:
            return None
        lo, hi = min(pts, key=lambda p: p[1]), max(pts, key=lambda p: p[1])
        line = plotted.spec(LABELS.get(name) or EXTRA_LABELS[name], {"high": hi, **({"low": lo} if name in MARK_LOW else {})}, name)
        line.on_line = plotted.raw   # the readings themselves: marked where they peak, not at the top of the shading (one glitch inside a bucket sets that)
        return line, plotted

    def _chart(self, names: list[str], rows: list[dict], period: str, days: tuple[date, date] | None = None) -> Chart | None:
        """These metrics as one chart. One metric is drawn large, with its rating zones. Several go into panels on a
        shared time axis, grouped by AIR_PANELS (the particles together, the rest each alone). `period` is the subtitle's
        text for it, `days` its first and last day (for the caption)."""
        drawn = {n: got for n in names if (got := self._line(n, rows))}
        if not drawn:
            return None
        if len(drawn) == 1:
            (name, (line, plotted)), = drawn.items()
            subtitle = (f"{period}  ·  {plotted.name}" + (", range shaded" if plotted.low else "") + "  ·  records marked"
                        if not plotted.raw else f"{period}  ·  AirGradient readings" + (", range shaded" if plotted.low else ""))
            return Chart(LABELS.get(name) or EXTRA_LABELS[name], subtitle, [panel_for(name, [line])], period=days)
        panels = []
        for group in AIR_PANELS:
            members = [m for m in group if m in drawn]
            if members:
                panels.append(panel_for(members, [drawn[m][0] for m in members]))
        panels += [panel_for(m, [drawn[m][0]]) for m in EXTRA if m in drawn]   # the sensor's own temperature and humidity, each alone
        only_extra = all(m in EXTRA for m in drawn)
        return Chart(" and ".join(EXTRA_LABELS[m] for m in drawn) if only_extra else "Air quality", f"{period}  ·  AirGradient readings"
                     + ("  ·  range shaded" if any(plotted.low for _, plotted in drawn.values()) else ""), panels, period=days)
