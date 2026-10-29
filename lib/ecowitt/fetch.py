"""Ecowitt history fetching: what one job (a question, a refresh, an alert check) reads from the storage, or asks of Ecowitt first (and
writes there), within Ecowitt's per-request limits."""

import asyncio
import functools
import logging
import time
from datetime import date, datetime, timedelta, tzinfo

from ..storage import Storage
from ..storage.coverage import merge as merge_intervals
from ..timeutil import local_epoch, naive_local
from .api import CYCLES, MINUTE, EcowittError, kept_from
from .stored import StoredHistory, load_on, minutes_on

log = logging.getLogger(__name__)


def series_in(data: dict):
    """(group, field, obj) for every time series in an Ecowitt 'data' object; obj["list"] is {ts: value}."""
    for group, fields in data.items():
        if isinstance(fields, dict):
            for field, obj in fields.items():
                if isinstance(obj, dict) and isinstance(obj.get("list"), dict):
                    yield group, field, obj


def spans(cycle: str, t: datetime, until: datetime):
    """(start, end) pieces of [t, until) that each fit Ecowitt's per-request limit for this cycle."""
    while t < until:
        e = min(t + CYCLES[cycle].span - timedelta(seconds=1), until)
        yield t, e
        t = e + timedelta(seconds=1)


REJECTIONS_STOP = 2             # requests Ecowitt refuses in one job before the rest are not sent
FRESH_SECONDS = 300             # the newest readings fetched are read from the storage this long, rather than asked for again ...
FRESH_SLACK_SECONDS = 60        # ... and a fetch ending within this of when it was made "reaches the present"


class Recent:
    """The newest fetch of each station, cycle and group: the time it covered and when it was made. What it brought is in the storage (what
    is newer than the cycle's settle horizon as provisional readings, which the next fetch replaces), so for FRESH_SECONDS a question reads
    it there instead of asking Ecowitt again (the keep-warm refreshes it). And one fetch at a time per station and cycle, so a question
    waits for one in flight instead of asking for the same thing."""

    def __init__(self):
        self._fetched: dict[tuple[str, str, str], tuple[int, int, float]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def lock(self, mac: str, cycle: str) -> asyncio.Lock:
        return self._locks.setdefault((mac, cycle), asyncio.Lock())

    def covers(self, mac: str, cycle: str, group: str, start: int, end: int) -> bool:
        """Did a fresh enough fetch cover the whole of [start, end]? One that reached the present covers anything up to now (what is newer
        is at most FRESH_SECONDS late, by design)."""
        entry = self._fetched.get((mac, cycle, group))
        if not entry or time.time() - entry[2] > FRESH_SECONDS or entry[0] > start:
            return False
        return end <= entry[1] or entry[1] >= entry[2] - FRESH_SLACK_SECONDS

    def note(self, mac: str, cycle: str, group: str, start: int, end: int):
        """A fetch of [start, end] was made, unless a fresh one that reaches later is noted: an old range (the archive's) must not push out
        the newest hours that questions are answered from."""
        held = self._fetched.get((mac, cycle, group))
        if held and time.time() - held[2] <= FRESH_SECONDS and held[1] > end:
            return
        self._fetched[mac, cycle, group] = (start, end, time.time())


def timed(method):
    """A Fetcher method whose time (waits included) is added to the fetcher's `seconds`."""
    @functools.wraps(method)
    async def run(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return await method(self, *args, **kwargs)
        finally:
            self.seconds += time.perf_counter() - started
    return run


class Fetcher:
    """History for one job (a question, a refresh, an alert check): from the storage where it holds it (the newest readings included, while
    Recent says they are fresh), the rest from Ecowitt, written to the storage first."""

    def __init__(self, api, storage: Storage, recent: Recent, mac: str, groups: list[str], tz: tzinfo, urgent: bool = False,
                 minutes: bool = False):
        """urgent: someone is waiting for the answer, so its requests go before the background ones queued for Ecowitt. minutes: the
        station's minute readings are kept (Home Assistant's)."""
        self.api, self.storage, self.recent, self.tz, self.urgent, self.has_minutes = api, storage, recent, tz, urgent, minutes
        self.cache = StoredHistory(storage)   # (TEMPORARY: what the readers not moved to the storage yet read)
        self.live = None                      # (the bands are in the readings now: HistoryQuery's own shading has nothing to add)
        self.mac = mac.strip().upper()
        self.groups = [g.split(".")[0].strip() for g in groups if g.strip()]  # plain names, never "outdoor.temp"
        self.ranges = self.from_cache = self.from_memory = self.calls = 0
        self.errors: list[str] = []
        self.rejected = 0  # requests Ecowitt itself refused (not network trouble or a busy server)
        self.seconds = 0.0  # time spent reading (the cache, memory, Ecowitt), for the reply's timing line

    def epoch(self, local: datetime) -> int:
        return local_epoch(local, self.tz)

    def local(self, epoch: int) -> datetime:
        return naive_local(epoch, self.tz)

    @timed
    async def get(self, cycle: str, start: datetime, end: datetime, refresh: bool = False, load: bool = True) -> dict:
        """Readings for [start, end] (local time), however long: what Ecowitt is asked for is split to its per-request limit.
        refresh=True ignores the in-memory copy (used by
        the keep-warm refresh). load=False only makes sure the range is cached and in memory, and
        skips building the result nobody will read (the refresh and the archive).
        When the cache and memory hold it all (the usual case) it is read at once, even while a refresh is fetching; otherwise
        it waits for any fetch of this cycle in flight (which may bring it), so the same thing isn't asked of Ecowitt twice."""
        self.ranges += 1
        s, e = self.epoch(start), self.epoch(end)
        if not refresh and (held := await self._read(cycle, s, e, load, fetch=False)) is not None:
            return held
        async with self.recent.lock(self.mac, cycle):
            return await self._read(cycle, s, e, load, fetch=True, refresh=refresh)

    async def _read(self, cycle: str, s: int, e: int, load: bool, fetch: bool, refresh: bool = False) -> dict | None:
        """[s, e] (epoch seconds) from the storage, and (fetch) what it does not hold, nor a fresh fetch brought, from Ecowitt (written to
        the storage first). None, with nothing counted, when fetch is False and some of it is neither held nor fresh."""
        (missing,) = self._missing(cycle, [(s, e)])
        gaps = merge_intervals([iv for ivs in missing.values() for iv in ivs])
        from_memory = 0
        for gap_start, gap_end in gaps:
            need = [g for g in self.groups if any(a <= gap_end and b >= gap_start for a, b in missing[g])]
            if not refresh and need and all(self.recent.covers(self.mac, cycle, g, gap_start, gap_end) for g in need):
                from_memory += 1   # (its readings are in the storage, provisional)
                continue
            if not fetch:
                return None
            for a, b in spans(cycle, self.local(gap_start), self.local(gap_end)):   # one request each, within Ecowitt's limit
                data = await self._fetch(cycle, a, b, need)
                if data is None or not data and not getattr(self.api, "empty_is_final", True):
                    continue  # failed, or told nothing by an API that only has what it recorded: not recorded as fetched, so it's tried again
                piece = (self.epoch(a), self.epoch(b))
                await self.storage.write(lambda w, data=data, piece=piece: w.put_ecowitt(self.mac, cycle, need, data, *piece))
                for g in need:
                    self.recent.note(self.mac, cycle, g, *piece)
        self.from_cache += not gaps
        self.from_memory += from_memory
        if not load:
            return {}
        return await self.storage.read(load_on, self.mac, cycle, self.groups, s, e)

    async def _fetch(self, cycle: str, start: datetime, end: datetime, groups: list[str]) -> dict | None:
        """One Ecowitt request: its data ({} if none), or None if it failed."""
        if self.rejected >= REJECTIONS_STOP:   # Ecowitt keeps saying no (a bad parameter): stop asking for the rest of this job
            return None
        self.calls += 1
        try:
            return await self.api.history(self.mac, cycle, start, end, ",".join(groups), urgent=self.urgent)
        except EcowittError as e:
            label = f"{cycle} {start:%d %b %Y} - {end:%d %b %Y}"
            log.warning("History request failed (%s): %s", label, e)
            self.errors.append(f"{label}: {e}")
            self.rejected += not e.transient
            return None

    @timed
    async def read(self, fn, *args):
        """fn(*args), a read of the storage made as the old cache's (slots, slot ranges), in a thread."""
        return await asyncio.to_thread(fn, *args)

    def _missing(self, cycle: str, windows: list[tuple[int, int]]) -> list[dict[str, list[tuple[int, int]]]]:
        """For each window (epoch seconds), the parts of it the storage lacks, by group (the coverage is in memory)."""
        return self.cache.uncovered(self.mac, self.groups, [(cycle, s, e) for s, e in windows])

    def sibling(self, groups: list[str]) -> "Fetcher":
        """A fetcher of other groups for the same job."""
        return Fetcher(self.api, self.storage, self.recent, self.mac, groups, self.tz, self.urgent, self.has_minutes)

    async def covered(self, cycle: str, start: int, end: int) -> bool:
        """True if the cache holds every group for [start, end] (epoch seconds)."""
        return (await self.covered_each(cycle, [(start, end)]))[0]

    @timed
    async def covered_each(self, cycle: str, windows: list[tuple[int, int]]) -> list[bool]:
        """covered() for each of these windows, from one read of the coverage."""
        return [bool(self.groups) and not any(missing.values()) for missing in self._missing(cycle, windows)]

    @timed
    async def minutes(self, start: datetime, end: datetime) -> dict:
        """The minute readings of [start, end] (local time) the bot has kept (Home Assistant's): {} where it has none or they do not reach
        back to the start, and the caller uses the 5-minute readings."""
        if not self.has_minutes:
            return {}
        return await self.storage.read(minutes_on, self.mac, self.groups, self.epoch(start), self.epoch(end))

    async def finer_cycle(self, ts: int, current: str, window_end: int, today: date) -> str | None:
        """Finest cycle finer than `current` available for this window: still kept by Ecowitt,
        or already in the cache (e.g. archived 5-minute data). None if neither."""
        if current == MINUTE:
            return None
        day = self.local(ts).date()
        for cycle in ("5min", "30min", "4hour"):
            if cycle == current:
                return None
            if day >= kept_from(cycle, today) or await self.covered(cycle, ts, window_end):
                return cycle
        return None
