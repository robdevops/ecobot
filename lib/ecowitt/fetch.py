"""Ecowitt history fetching: what one job (a question, a refresh, an alert check) reads from the storage, or asks of Ecowitt first (and
writes there), within Ecowitt's per-request limits."""

import asyncio
import functools
import logging
import time
from datetime import datetime, timedelta, tzinfo

from ..storage import Storage
from ..storage.coverage import merge as merge_intervals
from ..timeutil import local_epoch, naive_local
from .api import CYCLES, EcowittError
from .reads import rows_on

log = logging.getLogger(__name__)


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
    async def get(self, cycle: str, start: datetime, end: datetime, refresh: bool = False, load: bool = True) -> list[tuple[int, dict]]:
        """The readings of [start, end] (local time) at this resolution, as rows ([(epoch, {"group.field": value, ...})], reads.rows_on),
        however long: what the storage lacks is asked of Ecowitt, split to its per-request limit, and written there first. refresh=True
        asks again for what a fresh fetch brought (the keep-warm refresh). load=False only makes sure the range is held, and reads nothing
        (the refresh, the archive, a question that reads in its own way). When the storage holds it all (the usual case) it is read at
        once, even while a refresh is fetching; otherwise it waits for any fetch of this cycle in flight (which may bring it), so the same
        thing isn't asked of Ecowitt twice."""
        self.ranges += 1
        s, e = self.epoch(start), self.epoch(end)
        if not refresh and (held := await self._read(cycle, s, e, load, fetch=False)) is not None:
            return held
        async with self.recent.lock(self.mac, cycle):
            return await self._read(cycle, s, e, load, fetch=True, refresh=refresh)

    async def _read(self, cycle: str, s: int, e: int, load: bool, fetch: bool, refresh: bool = False) -> list | None:
        """[s, e] (epoch seconds) from the storage, and (fetch) what it does not hold, nor a fresh fetch brought, from Ecowitt (written to
        the storage first). None, with nothing counted, when fetch is False and some of it is neither held nor fresh."""
        (missing,) = self.uncovered(cycle, [(s, e)])
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
            return []
        return await self.storage.read(rows_on, self.mac, self.groups, cycle, s, e)

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
    async def stored(self, fn, *args):
        """fn(reader, *args) on the storage's reader thread: one hop, its queries in one read transaction."""
        return await self.storage.read(fn, *args)

    def uncovered(self, cycle: str, windows: list[tuple[int, int]]) -> list[dict[str, list[tuple[int, int]]]]:
        """For each window (epoch seconds), the parts of it the storage lacks at this resolution, by group (the coverage is in memory; a
        group the storage has no sensor for lacks all of it)."""
        sensors = {g: self.storage.sensors.find(self.mac, g) for g in self.groups}
        return [{g: self.storage.coverage.gaps(s.id, cycle, a, b) if s else [(a, b)] for g, s in sensors.items()} for a, b in windows]

    def days_held(self, cycle: str) -> int:
        """How many days of readings the storage holds at this resolution, of the group it holds the fewest of."""
        return min((self.storage.coverage.days(s.id, cycle) if (s := self.storage.sensors.find(self.mac, g)) else 0 for g in self.groups),
                   default=0)

    def sibling(self, groups: list[str]) -> "Fetcher":
        """A fetcher of other groups for the same job."""
        return Fetcher(self.api, self.storage, self.recent, self.mac, groups, self.tz, self.urgent, self.has_minutes)

    async def covered(self, cycle: str, start: int, end: int) -> bool:
        """True if the cache holds every group for [start, end] (epoch seconds)."""
        return (await self.covered_each(cycle, [(start, end)]))[0]

    @timed
    async def covered_each(self, cycle: str, windows: list[tuple[int, int]]) -> list[bool]:
        """covered() for each of these windows, from one read of the coverage."""
        return [bool(self.groups) and not any(missing.values()) for missing in self.uncovered(cycle, windows)]
