"""Air quality from your own AirGradient sensor, as a data source (peer of Ecowitt):
start() checks the sensor, tools are what the model can call, warm()/poke() keep recent
readings ready, backfill() caches the sensor's whole history, current() feeds the alert monitor.

AirGradient's documented API:
  current: GET /public/api/v1/locations/{id}/measures/current?token=...
  history: GET /public/api/v1/locations/{id}/measures/past?from=...&to=...&token=...

Finished days are stored on disk (store.py), so questions are answered without asking
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
from ..lines import RANGE_KEEP_DAYS, Plotted, Sample, build_line
from ..panels import panel_for
from ..specs import Chart, Line
from ..timeutil import day_bounds, day_text, local_date, local_epoch, naive_local, parse_local, to_local, when
from ..tools import Tool, Turn, json_result
from ..warm import Warmer
from .days import DESCRIPTION as DAYS_DESCRIPTION, PARAMETERS as DAYS_PARAMETERS, air_days_tool
from . import metrics
from .metrics import AIR_PANELS, ALL_METRICS, EXTRA, EXTRA_LABELS, EXTRA_UNITS, MARK_LOW, LABELS, METRICS, STORED, normalise, pm25_aqi, rating, value_of, without_spikes
from .homeassistant import ANCHORS, checked, entity_ids, find_prefix, ranges as ha_ranges
from .local import LocalSensor
from .store import AirStore

log = logging.getLogger(__name__)

API = "https://api.airgradient.com/public/api/v1/locations/{loc}/measures/{kind}"
MAX_DAYS = 400                    # longest history period per question
MAX_REQUEST_DAYS = 9              # the API allows 10 days per request; a day of margin for clock changes
MAX_INLINE_DAYS = 30              # missing days fetched while answering; more are left to the backfill
HOURLY_MAX_READINGS = 30          # a day with this few readings holds hourly averages (24), not 5-minute (288)
FRESH_SECONDS = 300               # the current reading and today's history are reused this long
BUCKET = 300                      # the readings kept are 5-minute averages, on the clock (as AirGradient's are)
HA_RECENT_SECONDS = 3 * 3600      # Home Assistant's ranges are read back for the last few hours each time (RANGE_KEEP_DAYS the first time)
SAMPLE_SECONDS = 60               # with the sensor's own server: how often it is read (the keep-warm interval)
MIN_SAMPLES = 3                   # a 5-minute average of fewer readings than this is not kept (the cloud fills that gap)
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

    def __init__(self, cfg: Config, transport=None, ha: HomeAssistant | None = None):
        """ha: the weather station's Home Assistant client, shared (so a failing Home Assistant is said once); else one of its own when
        Home Assistant is set."""
        self.loc, self.tz = cfg.airgradient_location or "local", cfg.tz   # (the stored readings are kept under this)
        self.token = cfg.airgradient_token
        self.cloud = cfg.airgradient_cloud    # without an account only the sensor itself is asked, and the cloud never is
        self.link = ("live chart", cfg.airgradient_dashboard) if cfg.airgradient_dashboard else None
        self.client = httpx.AsyncClient(timeout=15, transport=transport)
        self.local = LocalSensor(cfg.airgradient_local_url, cloud=cfg.airgradient_cloud) if cfg.airgradient_local_url else None   # the live reading from the sensor itself
        self.store = AirStore(cfg.air_cache_path, self.loc, self.tz)
        self._current: tuple[dict, float] | None = None       # (raw row, fetched at)
        self._names: dict = {}                                # the sensor's name and type, from the last cloud reading
        self._recent: dict[date, tuple[list[dict], float]] = {}  # today / just-finished days, refreshed
        self._locks: dict = {}
        self.requests = 0
        self.warmer = Warmer(self.warm, SAMPLE_SECONDS) if self.local else Warmer(self.warm)
        self._bucket: tuple[int, list[dict]] | None = None   # the bucket being averaged: (its start, the readings so far)
        self._gap_tried: dict[date, float] = {}               # when the cloud was last asked to fill a gap in a day
        self.ha = ha or (HomeAssistant(cfg.homeassistant_url, cfg.homeassistant_token) if cfg.homeassistant else None)   # its ranges
        self._own_ha = ha is None and self.ha is not None
        self._ha_ok: bool | None = None                        # Home Assistant's ranges: None until first read
        self._ha_said: set[str] = set()                        # what was said once about them: no sensor, a metric that is another variant
        self.tools = [Tool("air_quality", DESCRIPTION, PARAMETERS, self.handle, asked=asked), Tool("air_days", DAYS_DESCRIPTION, DAYS_PARAMETERS, self._days)]

    async def start(self):
        try:
            row = await self._current_row(refresh=True)
            if self.local and self.cloud:   # the cloud is still asked once: it names the sensor, and its token and location are checked
                self._names = {k: v for k, v in (await self._get("current")).items() if k in ("locationName", "locationType")}
                row = {**row, **self._names}
                self._current = (row, self._current[1])
            self.summary = f"AirGradient: {self._summary(row)}"
        except Exception as e:  # keep going: the sensor may just be briefly unreachable
            log.warning("AirGradient %s not readable yet: %s", f"location {self.loc}" if self.cloud else "sensor", e)

    def _summary(self, row: dict) -> str:
        """The start-up line: the sensor's name, its cloud location, where the readings come from and how often."""
        local = self.local
        sensor = (local.describe() + ("" if local.describe().split(":")[0] == local.address[0] else f" ({local.address[0]})")
                  if local and local.working and local.address else None)
        via = [sensor, "cloud fallback" if sensor else "AirGradient cloud"] if self.cloud else [sensor]
        if local and not sensor and self.cloud:
            via.append(f"{local.describe()} not answering")
        name = row.get("locationName") or (local and local.serial) or "sensor"
        return f"'{name}'{f' ({self.loc})' if self.cloud else ''} via {', '.join(v for v in via if v)} (every {self.warmer.interval:g}s)"

    async def close(self):
        await super().close()
        if self._own_ha:
            await self.ha.close()

    async def import_ranges(self):
        """Fold the ranges of the readings Home Assistant kept (homeassistant.py) into the store's: the last RANGE_KEEP_DAYS days the first
        time (as far back as it has them), then the last few hours. Nothing when no Home Assistant is set or it has no AirGradient sensor
        (said once). A failure is logged once, with its recovery (Home Assistant's own account of it); the bot's own ranges stay."""
        if self.ha is None:
            return
        first, started, now = self._ha_ok is None, time.monotonic(), time.time()
        start = int(now - (RANGE_KEEP_DAYS * 86400 if first else HA_RECENT_SECONDS)) // BUCKET * BUCKET
        try:
            states = await self.ha.states()
            mine = {a: value_of(self._current[0], a) for a in ANCHORS} if self._current else None
            prefix, sensors = find_prefix(states, mine)
            if prefix is None:
                if "none" not in self._ha_said:
                    self._ha_said.add("none")
                    log.info("Home Assistant has no AirGradient sensor (no entity ending in _%s): the air sensor's ranges come from the "
                             "bot's own readings", " or _".join(ANCHORS))
                self.ha.trouble.ok()
                self._ha_ok = True
                return
            if sensors > 1 and "several" not in self._ha_said:
                self._ha_said.add("several")
                log.info("Home Assistant has %d AirGradient sensors; using %s*, whose reading is nearest this one's", sensors, prefix)
            ids = entity_ids(prefix, states)
            found: dict = {}
            for chunk in range(start, int(now), 86400):   # a day a request
                until = min(chunk + 86400, now)
                history = await self.ha.history(list(ids), chunk - BUCKET, until)
                for name, got in ha_ranges(history, ids, self.ha.units, chunk, until - 1, now).items():
                    found.setdefault(name, {}).update(got)
            rows, _, _ = await self.rows(naive_local(start, self.tz), naive_local(now, self.tz))
            keep, verdict = checked(found, rows)
            added = await asyncio.to_thread(self.store.add_spreads, keep)
        except Exception as e:
            self.ha.trouble.failed("history for the air sensor's ranges", f"{type(e).__name__}: {e}",
                                   "the air sensor's ranges come from the bot's own readings")
            self._ha_ok = False
            return
        self.ha.trouble.ok()
        for name, (compared, agreed) in verdict.items():
            if name not in keep and name not in self._ha_said:
                self._ha_said.add(name)
                log.info("Home Assistant's %s isn't the reading the bot keeps (it agreed in %d of %d five minutes: perhaps the sensor's raw "
                         "value), so its ranges aren't used", LABELS.get(name) or EXTRA_LABELS.get(name, name), agreed, compared)
        if first or not self._ha_ok:
            log.info("Backfill Home Assistant (AirGradient): %d ranges fetched, 0 failed, %d readings, %.0fs", added, len(keep),
                     time.monotonic() - started)
        self._ha_ok = True

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
        """The live reading: from the sensor's own server when AIRGRADIENT_LOCAL_URL answers (with the cloud's name for the sensor,
        last heard), else the cloud."""
        if self.local and (row := await self.local.read()):
            return {**(self._names or self._sensor_name()), **row}
        if not self.cloud:
            raise RuntimeError("the sensor did not answer")
        row = await self._get("current")
        self._names = {k: v for k, v in row.items() if k in ("locationName", "locationType")}
        return row

    def _sensor_name(self) -> dict:
        """What the sensor is called when the cloud has not said: its serial number."""
        return {"locationName": f"AirGradient {self.local.serial}"} if self.local and self.local.serial else {}

    def _final(self, day: date) -> bool:
        """Over for more than an hour, so its data won't change."""
        return self.now() > datetime.combine(day, datetime.min.time()) + timedelta(days=1, hours=1)

    async def _held(self, days: list[date]) -> dict[date, int | None]:
        """Readings stored per finished day, in one query (None: not fetched yet, or not finished)."""
        stored = await asyncio.to_thread(self.store.counts, days)
        return {d: stored[d] if self._final(d) else None for d in days}

    def _unstored(self, held: dict[date, int | None]) -> list[date]:
        """The finished days in `held` (see _held) that aren't stored yet."""
        return [d for d, n in held.items() if n is None and self._final(d)]

    async def _past(self, start: int, end: int) -> list[dict]:
        """Readings between two epochs (never past now), normalised and oldest first. Without a cloud account there are none to ask for."""
        if not self.cloud:
            return []
        utc = lambda ts: datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = await self._get("past", **{"from": utc(start), "to": utc(min(end, time.time()))})
        return sorted((normalise(r) for r in raw if isinstance(r, dict) and r.get("timestamp")), key=lambda r: r["ts"])

    async def _fetch_days(self, days: list[date]) -> dict[date, int]:
        """Fetch finished days and store them, in as few requests as the API allows (it takes up to 10 days at a time; we use 9).
        Returns how many readings each day has stored."""
        days, stored = sorted(days), {}
        for d in [d for d in days if await asyncio.to_thread(self.store.has_recorded, d)]:   # the bot's own readings: only the gaps are fetched
            stored[d] = await self._complete_day(d)
            days.remove(d)
        while days and self.cloud:
            window = [d for d in days if (d - days[0]).days < MAX_REQUEST_DAYS]
            days = days[len(window):]
            by_day: dict = {d: [] for d in window}
            for r in await self._past(day_bounds(window[0], self.tz)[0], day_bounds(window[-1], self.tz)[1]):
                by_day.get(local_date(r["ts"], self.tz), []).append(r)
            for d, day_rows in by_day.items():
                stored[d] = await asyncio.to_thread(self.store.save_day, d, day_rows)
                self._recent.pop(d, None)
        return stored

    async def _day_rows(self, day: date, refresh: bool = False) -> list[dict]:
        """All of one local day's readings, normalised. Finished days come from the disk cache (and
        are fetched once if missing); today, and a just-finished day, are refetched when a few
        minutes old."""
        final = self._final(day)
        async with self._lock(day):
            if final:
                if self._unstored(await self._held([day])):
                    await self._fetch_days([day])
                return await asyncio.to_thread(self.store.load_day, day)
            if self.local and (self.local.working or not self.cloud):
                return await self._recorded_day_rows(day)
            cached = self._recent.get(day)
            if cached and not refresh and time.time() - cached[1] <= FRESH_SECONDS:
                return cached[0]
            rows = await self._past(*day_bounds(day, self.tz))
            self._recent[day] = (rows, time.time())
            return rows

    # ---------- the sensor's own readings, kept by the bot ----------
    async def _record(self) -> bool:
        """Read the sensor itself; True if it answered. Its readings are averaged into 5-minute buckets, and a bucket with enough of them is
        stored (as the cloud would give it) when the next one starts."""
        row = await self.local.read()
        if not row:
            return False
        now = time.time()
        self._current = ({**self._names, **row}, now)
        bucket = int(now) // BUCKET * BUCKET
        if self._bucket and self._bucket[0] != bucket:
            await self._flush_bucket()
        if self._bucket is None:
            self._bucket = (bucket, [])
        self._bucket[1].append({n: v for n in STORED if (v := value_of(row, n)) is not None})
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
            await asyncio.to_thread(self.store.add_recorded, self._average(bucket, samples))

    def _missing(self, day: date, rows: list[dict], until: float) -> list[int]:
        """The start of each 5-minute bucket of this day, over by `until`, that has no reading."""
        lo, hi = day_bounds(day, self.tz)
        have = {r["ts"] // BUCKET * BUCKET for r in rows}
        return [t for t in range(lo // BUCKET * BUCKET, min(hi, int(until) // BUCKET * BUCKET), BUCKET) if t not in have]

    async def _fill(self, day: date, missing: list[int]):
        """Ask the cloud for the span that holds these missing buckets, and keep what it has for them."""
        lo, hi = day_bounds(day, self.tz)
        got = await self._past(missing[0], missing[-1] + BUCKET)
        wanted = set(missing)
        await asyncio.to_thread(self.store.merge, [r for r in got if lo <= r["ts"] < hi and r["ts"] // BUCKET * BUCKET in wanted])

    async def _recorded_day_rows(self, day: date) -> list[dict]:
        """An unfinished day (today, or one that has just ended) while the sensor answers: the stored readings, the bucket being averaged
        as the last one, and the cloud asked, at most every GAP_RETRY_SECONDS, only for buckets that are missing."""
        rows = await asyncio.to_thread(self.store.load_day, day)
        if self.cloud and time.time() - self._gap_tried.get(day, 0) > GAP_RETRY_SECONDS and (missing := self._missing(day, rows, time.time() - GAP_GRACE)):
            self._gap_tried[day] = time.time()
            try:
                await self._fill(day, missing)
                rows = await asyncio.to_thread(self.store.load_day, day)
            except Exception as e:   # the stored readings still stand
                log.warning("AirGradient: couldn't fill the gaps of %s from the cloud: %s", day, e)
        if self._bucket and self._bucket[1] and local_date(self._bucket[0], self.tz) == day and self._bucket[0] not in {r["ts"] for r in rows}:
            rows = [*rows, self._average(*self._bucket)]
        return rows

    async def _complete_day(self, day: date) -> int:
        """A finished day the bot recorded readings of: the cloud is asked only for the buckets it lacks (once), and the day is noted as done."""
        rows = await asyncio.to_thread(self.store.load_day, day)
        if missing := self._missing(day, rows, day_bounds(day, self.tz)[1]):
            if not self.cloud:   # nothing to fill it with: the day is left open, in case an account is added later
                return len(rows)
            await self._fill(day, missing)
            rows = await asyncio.to_thread(self.store.load_day, day)
        await asyncio.to_thread(self.store.mark_day, day, len(rows))
        self._recent.pop(day, None)
        return len(rows)

    async def warm(self, fresh: bool = True) -> str:
        """Current reading, today, and the last week of finished days."""
        before, today = self.requests, self.now().date()
        if not (self.local and await self._record()):   # (the sensor itself, when it answers: the reading is also kept)
            await self._current_row(fresh)
        await self._day_rows(today, fresh)
        week = [today - timedelta(days=n) for n in range(1, WARM_DAYS + 1)]
        await self._fetch_days(self._unstored(await self._held(week)))  # one request for all of them
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
            counts = await self._held(days)
            if missing := self._unstored(counts):
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
                if counts[d] is None:
                    continue
                total += 1
                seen_data = seen_data or counts[d] > 0
                empty_run = empty_run + 1 if counts[d] == 0 else 0
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
        held = await self._held(days)
        missing = self._unstored(held)
        skipped = missing[:-MAX_INLINE_DAYS] if self.cloud and len(missing) > MAX_INLINE_DAYS else []  # the oldest wait for the backfill
        held.update(await self._fetch_days([d for d in missing if d not in skipped]))
        finished = [d for d in days if self._final(d)]   # all stored by now but the skipped: read in one query
        rows = (await asyncio.to_thread(self.store.load, day_bounds(finished[0], self.tz)[0], day_bounds(finished[-1], self.tz)[1])
                if finished else [])
        for d in days[len(finished):]:   # today, and a day that has just ended: fetched, not stored (but their ranges may be)
            rows += await self._with_spreads(await self._day_rows(d), d)
        hourly = sum(1 for n in held.values() if n and n <= HOURLY_MAX_READINGS)
        lo, hi = local_epoch(t0, self.tz), local_epoch(t1, self.tz)
        return [r for r in without_spikes(rows) if lo <= r["ts"] <= hi], hourly, skipped

    async def _with_spreads(self, rows: list[dict], day: date) -> list[dict]:
        """These readings of a day with the ranges the store holds for them (Home Assistant's, for a day the cloud gave): a reading's own
        range, the bot's, wins."""
        ranges = await asyncio.to_thread(self.store.spreads, *day_bounds(day, self.tz))
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
        for name, (_, unit) in METRICS.items():
            pts = [(r["ts"], r[name]) for r in rows if name in r]
            if not pts:
                continue
            lo, hi = min(pts, key=lambda p: p[1]), max(pts, key=lambda p: p[1])
            entry = {"unit": unit, "low": lo[1], "low_time": when(lo[0], self.tz),
                     "high": hi[1], "high_time": when(hi[0], self.tz),
                     "average": round(statistics.fmean(v for _, v in pts), 1)}
            entry["high_rating"], entry["average_rating"] = rating(name, hi[1]), rating(name, entry["average"])
            if name == "pm2_5":
                entry["high_aqi_us"], entry["high_band"] = pm25_aqi(hi[1])
                entry["average_aqi_us"], entry["average_band"] = pm25_aqi(entry["average"])
            out[name] = entry
        for name in [m for m in metrics or [] if m in EXTRA]:   # the sensor's own temperature and humidity, only when asked for
            if pts := [(r["ts"], r[name]) for r in rows if name in r]:
                lo, hi = min(pts, key=lambda p: p[1]), max(pts, key=lambda p: p[1])
                out[name] = {"unit": EXTRA_UNITS[name], "low": lo[1], "low_time": when(lo[0], self.tz), "high": hi[1],
                             "high_time": when(hi[0], self.tz), "average": round(statistics.fmean(v for _, v in pts), 1)}
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
        return plotted.spec(LABELS.get(name) or EXTRA_LABELS[name], {"high": hi, **({"low": lo} if name in MARK_LOW else {})}, name), plotted

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
