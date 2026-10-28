"""Ecowitt history fetching: what one job (a question, a refresh, an alert check) reads from the disk cache, from memory
or from Ecowitt, within Ecowitt's per-request limits."""

import asyncio
import functools
import logging
import time
from datetime import date, datetime, timedelta, tzinfo

from ..timeutil import local_epoch, naive_local
from .api import CYCLES, MINUTE, EcowittError, kept_from
from .store import HistoryCache, HotStore, merge as merge_intervals

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


def merge_data(into: dict, new: dict, window: tuple[int, int] | None = None):
    """Merge one Ecowitt 'data' object into another, optionally only timestamps in window."""
    for grp, field, obj in series_in(new):
        entry = into.setdefault(grp, {}).setdefault(field, {"unit": obj.get("unit", ""), "list": {}})
        for ts, value in obj["list"].items():
            if window is None or window[0] <= int(ts) <= window[1]:
                entry["list"][ts] = value


REJECTIONS_STOP = 2             # requests Ecowitt refuses in one job before the rest are not sent


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
    """History for one job (a question, a refresh, an alert check): from the disk cache where
    possible, recent readings from memory if fresh, the rest from Ecowitt."""

    def __init__(self, api, cache: HistoryCache, hot: HotStore, mac: str, groups: list[str], tz: tzinfo, urgent: bool = False, live=None,
                 minute_store=None):
        """urgent: someone is waiting for the answer, so its requests go before the background ones queued for Ecowitt."""
        self.api, self.cache, self.hot, self.tz, self.urgent = api, cache, hot, tz, urgent
        self.live = live   # live.LiveRanges: the lows and highs of the live readings, for 5-minute bands (None: no such bands)
        self.minute_store = minute_store   # minutes.MinuteStore: the last hours' minute readings (None: this station has none)
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
        async with self.hot.lock(self.mac, cycle):
            return await self._read(cycle, s, e, load, fetch=True, refresh=refresh)

    async def _read(self, cycle: str, s: int, e: int, load: bool, fetch: bool, refresh: bool = False) -> dict | None:
        """[s, e] (epoch seconds) from the cache, its newest readings from memory, and (fetch) what neither holds from Ecowitt, which
        is then cached and kept in memory. None, with nothing counted, when fetch is False and the cache and memory lack some of it."""
        (missing,) = await self._missing(cycle, [(s, e)])
        gaps = merge_intervals([iv for ivs in missing.values() for iv in ivs])
        fresh: dict = {}
        from_memory = 0
        for gap_start, gap_end in gaps:
            need = [g for g in self.groups if any(a <= gap_end and b >= gap_start for a, b in missing[g])]
            hot = {} if refresh else {g: self.hot.get(self.mac, cycle, g, gap_start, gap_end) for g in need}
            if hot and all(v is not None for v in hot.values()):
                from_memory += 1
                if load:
                    merge_data(fresh, hot, (gap_start, gap_end))
                continue
            if not fetch:
                return None
            for a, b in spans(cycle, self.local(gap_start), self.local(gap_end)):   # one request each, within Ecowitt's limit
                data = await self._fetch(cycle, a, b, need)
                if data is None or not data and not getattr(self.api, "empty_is_final", True):
                    continue  # failed, or told nothing by an API that only has what it recorded: not recorded as fetched, so it's tried again
                piece = (self.epoch(a), self.epoch(b))
                await asyncio.to_thread(self.cache.store, self.mac, cycle, need, data, *piece)
                for g in need:
                    self.hot.put(self.mac, cycle, g, *piece, data.get(g) or {})
                if load:
                    merge_data(fresh, data)
        self.from_cache += not gaps
        self.from_memory += from_memory
        if not load:
            return {}
        result = await asyncio.to_thread(self.cache.load, self.mac, cycle, self.groups, s, e)
        merge_data(result, fresh, (s, e))  # recent readings aren't on disk; use the fresh ones
        return result

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
        """fn(*args), a read of the cache's own (slots, slot ranges), in a thread."""
        return await asyncio.to_thread(fn, *args)

    async def _missing(self, cycle: str, windows: list[tuple[int, int]]) -> list[dict[str, list[tuple[int, int]]]]:
        """For each window (epoch seconds), the parts of it the cache lacks, by group: one read of the coverage in one thread hop
        (the hop costs more than the query, and far more while a chart is being drawn)."""
        return await asyncio.to_thread(self.cache.uncovered, self.mac, self.groups, [(cycle, s, e) for s, e in windows])

    async def covered(self, cycle: str, start: int, end: int) -> bool:
        """True if the cache holds every group for [start, end] (epoch seconds)."""
        return (await self.covered_each(cycle, [(start, end)]))[0]

    @timed
    async def covered_each(self, cycle: str, windows: list[tuple[int, int]]) -> list[bool]:
        """covered() for each of these windows, from one read of the coverage."""
        return [bool(self.groups) and not any(missing.values()) for missing in await self._missing(cycle, windows)]

    @timed
    async def minutes(self, start: datetime, end: datetime) -> dict:
        """The minute readings of [start, end] (local time) the bot has kept (minutes.py): {} where it has none or they do not reach back
        to the start, and the caller uses the 5-minute readings."""
        if self.minute_store is None:
            return {}
        return await asyncio.to_thread(self.minute_store.get, self.groups, self.epoch(start), self.epoch(end))

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
