"""Ecowitt history fetching: what one job (a question, a refresh, an alert check) reads from the disk cache, from memory
or from Ecowitt, within Ecowitt's per-request limits."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone, tzinfo

from ..timeutil import local_epoch, to_local
from .api import EcowittError, MAX_SPAN, RETENTION
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
        e = min(t + MAX_SPAN[cycle] - timedelta(seconds=1), until)
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


class Fetcher:
    """History for one job (a question, a refresh, an alert check): from the disk cache where
    possible, recent readings from memory if fresh, the rest from Ecowitt."""

    def __init__(self, api, cache: HistoryCache, hot: HotStore, mac: str, groups: list[str], tz: tzinfo):
        self.api, self.cache, self.hot, self.tz = api, cache, hot, tz
        self.mac = mac.strip().upper()
        self.groups = [g.split(".")[0].strip() for g in groups if g.strip()]  # plain names, never "outdoor.temp"
        self.ranges = self.from_cache = self.from_memory = self.calls = 0
        self.errors: list[str] = []
        self.rejected = 0  # requests Ecowitt itself refused (not network trouble or a busy server)

    def epoch(self, local: datetime) -> int:
        return local_epoch(local, self.tz)

    def local(self, epoch: int) -> datetime:
        return to_local(epoch, self.tz).replace(tzinfo=None)

    async def get(self, cycle: str, start: datetime, end: datetime, refresh: bool = False, load: bool = True) -> dict:
        """Readings for [start, end] (local time). refresh=True ignores the in-memory copy (used by
        the keep-warm refresh). load=False only makes sure the range is cached and in memory, and
        skips building the result nobody will read (the refresh and the archive)."""
        self.ranges += 1
        s, e = self.epoch(start), self.epoch(end)
        async with self.hot.lock(self.mac, cycle):
            missing = {g: await asyncio.to_thread(self.cache.missing, self.mac, cycle, g, s, e) for g in self.groups}
            gaps = merge_intervals([iv for ivs in missing.values() for iv in ivs])
            if not gaps:
                self.from_cache += 1
            fresh: dict = {}
            for gap_start, gap_end in gaps:
                need = [g for g in self.groups if any(a <= gap_end and b >= gap_start for a, b in missing[g])]
                hot = {} if refresh else {g: self.hot.get(self.mac, cycle, g, gap_start, gap_end) for g in need}
                if hot and all(v is not None for v in hot.values()):
                    self.from_memory += 1
                    merge_data(fresh, hot, (gap_start, gap_end))
                    continue
                data = await self._fetch(cycle, self.local(gap_start), self.local(gap_end), need)
                if data is None:
                    continue  # failed: not recorded as fetched, so it's retried next time
                await asyncio.to_thread(self.cache.store, self.mac, cycle, need, data, gap_start, gap_end)
                for g in need:
                    self.hot.put(self.mac, cycle, g, gap_start, gap_end, data.get(g) or {})
                merge_data(fresh, data)
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
            return await self.api.history(self.mac, cycle, start, end, ",".join(groups))
        except EcowittError as e:
            label = f"{cycle} {start:%d %b %Y} - {end:%d %b %Y}"
            log.warning("History request failed (%s): %s", label, e)
            self.errors.append(f"{label}: {e}")
            self.rejected += not e.transient
            return None

    async def covered(self, cycle: str, start: int, end: int) -> bool:
        """True if the cache holds every group for [start, end] (epoch seconds)."""
        for g in self.groups:
            if await asyncio.to_thread(self.cache.missing, self.mac, cycle, g, start, end):
                return False
        return bool(self.groups)

    async def finer_cycle(self, ts: int, current: str, window_end: int, now_utc: datetime) -> str | None:
        """Finest cycle finer than `current` available for this window: still kept by Ecowitt,
        or already in the cache (e.g. archived 5-minute data). None if neither."""
        age = (now_utc - datetime.fromtimestamp(ts, timezone.utc)).days
        for cycle in ("5min", "30min", "4hour"):
            if cycle == current:
                return None
            if age < RETENTION[cycle] - 1 or await self.covered(cycle, ts, window_end):
                return cycle
        return None
