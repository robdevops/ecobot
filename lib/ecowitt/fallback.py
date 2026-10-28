"""Two Ecowitt APIs as one: Home Assistant answers, and Ecowitt's cloud makes up what it cannot.

  - realtime: Home Assistant's reading, unless it is down or stale (the gateway has stopped reporting to it), when the cloud's is asked for and
    the newer of the two is used;
  - history: Home Assistant's buckets; those it has none of (before it began recording, while it or its Ecowitt integration was down) are
    asked of the cloud in one request for the span they cover, and the cloud's readings fill the gaps (Home Assistant's are kept where both
    have one). The cloud is never asked for a cycle only Home Assistant has (the minute's), or for a bucket that has not begun;
  - devices: the cloud's, which knows the station's own address (the cache is keyed by it) and when it was made, else Home Assistant's.
A failure of either is logged once for as long as it lasts. If both fail the error is raised, as for any one of them.
"""

import logging
import time
from datetime import datetime

from ..timeutil import local_epoch, naive_local
from .api import CYCLES, EcowittError
from .hass_history import SECONDS, merged, present
from .hass_map import ENTITIES

log = logging.getLogger(__name__)

STALE_SECONDS = 600     # a live reading older than this is stale: the other API may have a fresher one


def newest(data: dict) -> int:
    """The time of the newest reading of a real_time answer (0 for none)."""
    out = 0
    for fields in data.values() if isinstance(data, dict) else ():
        for obj in (fields.values() if isinstance(fields, dict) else ()):
            try:
                out = max(out, int(obj.get("time") or 0))
            except (AttributeError, TypeError, ValueError):
                pass
    return out


class FallbackAPI:
    empty_is_final = True    # what neither has is not there

    def __init__(self, first, second, tz, clock=time.time):
        """first: the API that answers (Home Assistant's); second: the one that makes up what it lacks (Ecowitt's cloud)."""
        self.first, self.second, self.tz, self.clock = first, second, tz, clock
        self.fast_live, self.minute_lines = first.fast_live, first.minute_lines
        self._down: dict[str, bool] = {}   # which of "first" / "second" is failing now, so each outage is said once

    @property
    def requests(self) -> int:
        return getattr(self.first, "requests", 0) + getattr(self.second, "requests", 0)

    async def close(self):
        await self.first.close()
        await self.second.close()

    def set_timezone(self, tz):
        self.tz = tz
        for api in (self.first, self.second):
            if hasattr(api, "set_timezone"):
                api.set_timezone(tz)

    def _failed(self, which: str, what: str, error: EcowittError):
        if not self._down.get(which):
            log.warning("%s: %s failed (%s); the other is used where it can", "Home Assistant" if which == "first" else "Ecowitt's cloud", what, error)
        self._down[which] = True

    def _fine(self, which: str):
        if self._down.pop(which, False):
            log.info("%s is answering again", "Home Assistant" if which == "first" else "Ecowitt's cloud")

    async def devices(self) -> list[dict]:
        cloud, ours, failure = [], [], None
        try:
            cloud = await self.second.devices()
            self._fine("second")
        except EcowittError as e:
            self._failed("second", "the device list", e)
            failure = e
        try:   # (asked either way: it finds the station's entities and when its first reading was)
            ours = await self.first.devices()
            self._fine("first")
        except EcowittError as e:
            self._failed("first", "the device list", e)
            failure = failure or e
        if not (cloud or ours):
            raise failure or EcowittError("no Ecowitt device", transient=False)
        return cloud or ours

    async def realtime(self, mac: str, groups: str, urgent: bool = False) -> dict:
        data, failure = None, None
        try:
            data = await self.first.realtime(mac, groups, urgent)
            self._fine("first")
        except EcowittError as e:
            self._failed("first", "the live reading", e)
            failure = e
        if data and self.clock() - newest(data) <= STALE_SECONDS:
            return data
        try:
            other = await self.second.realtime(mac, groups, urgent)
            self._fine("second")
        except EcowittError as e:
            self._failed("second", "the live reading", e)
            if data is not None:
                return data
            raise failure or e
        return other if newest(other) > newest(data or {}) else data

    def _missing(self, got: dict, groups: list[str], cycle: str, lo: int, hi: int) -> list[int]:
        """The start of each bucket in [lo, hi] (epochs) that the first API gave no reading of, in the groups it has entities for."""
        width = SECONDS[cycle]
        mine = [g for g in groups if any(e.group == g for e in ENTITIES.values())]
        if not mine:
            return []
        have = present(got, mine)
        began = getattr(self.first, "earliest", None) or 0   # a bucket that began before its first reading is told only in part
        wanted = range(int(lo // width) * width, int(min(hi, self.clock()) // width) * width + 1, width)
        return [b for b in wanted if b < began or any(b not in have[g] for g in mine)]

    async def history(self, mac: str, cycle: str, start: datetime, end: datetime, groups: str, urgent: bool = False) -> dict:
        got, failure = {}, None
        try:
            got = await self.first.history(mac, cycle, start, end, groups, urgent)
            self._fine("first")
        except EcowittError as e:
            self._failed("first", f"the {cycle} history", e)
            failure = e
        if cycle not in CYCLES:   # only the first has this cycle
            if failure:
                raise failure
            return got
        missing = self._missing(got, [g for g in groups.split(",") if g], cycle, local_epoch(start, self.tz), local_epoch(end, self.tz))
        if not missing:
            return got
        gap = (naive_local(missing[0], self.tz), naive_local(missing[-1] + SECONDS[cycle] - 1, self.tz))
        try:
            more = await self.second.history(mac, cycle, *gap, groups, urgent)
            self._fine("second")
        except EcowittError as e:
            self._failed("second", f"the {cycle} history", e)
            if failure or not got:
                raise failure or e
            return got
        return merged(got, more, frozenset(missing))
