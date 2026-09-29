"""Recent (still-settling) history readings, kept warm in memory.

The on-disk cache never stores the last hour or two, because it may still change, so
every question used to fetch that tail from Ecowitt. Here the tail is kept in memory
for a few minutes, fetched ahead of time:
  - prefetch: as soon as a question arrives, fetch today's recent data in parallel
    with the model deciding what to ask for;
  - keep warm: refresh it every few minutes, around the clock, so questions find it
    ready (and the alert checks run on fresh readings).
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, tzinfo

log = logging.getLogger(__name__)

TTL_SECONDS = 300            # recent readings are reused for this long
REFRESH_SECONDS = 240        # keep-warm refresh interval (under the TTL)
PREFETCH_GROUPS = "outdoor,indoor,rainfall,pressure,wind"  # extras feed the rain alerts and prediction
BUCKET_SLACK = 60            # a request ending within this of its fetch time "reaches the present"


class HotStore:
    """Fresh responses per (mac, cycle, group): the fields, the range they cover, and when
    they were fetched. Only the latest response per key is kept."""

    def __init__(self):
        self._entries: dict = {}
        self._locks: dict = {}

    def lock(self, mac: str, cycle: str) -> asyncio.Lock:
        """One fetch at a time per mac/cycle, so a question waits for an in-flight
        prefetch instead of asking Ecowitt for the same thing."""
        return self._locks.setdefault((mac, cycle), asyncio.Lock())

    def get(self, mac: str, cycle: str, group: str, start: int, end: int):
        """Group's fields if a fresh-enough response covers the whole of [start, end], else
        None. A response fetched up to the present covers anything up to now (readings
        newer than that are at most TTL_SECONDS stale, by design)."""
        entry = self._entries.get((mac, cycle, group))
        if not entry or time.time() - entry["fetched_at"] > TTL_SECONDS or entry["start"] > start:
            return None
        reaches_present = entry["end"] >= entry["fetched_at"] - BUCKET_SLACK
        if end <= entry["end"] or reaches_present:
            return entry["fields"]
        return None

    def put(self, mac: str, cycle: str, group: str, start: int, end: int, fields: dict):
        self._entries[(mac, cycle, group)] = {"start": start, "end": end, "fields": fields,
                                              "fetched_at": time.time()}

    def clear(self):
        self._entries.clear()


HOT = HotStore()


class RecentData:
    """Prefetches today's recent readings and keeps them warm while chats are active."""

    def __init__(self, make_fetcher, tz: tzinfo, macs: list[str]):
        self.make_fetcher = make_fetcher  # (mac, callback) -> Fetcher
        self.tz, self.macs = tz, macs
        self._task: asyncio.Task | None = None
        self.after_refresh = []  # async callables run after each keep-warm refresh (alerts)

    async def fetch(self, refresh: bool = False) -> int:
        """Fetch the ranges most questions need: the last 7 days at 30 minutes (only
        the unsettled tail actually goes to Ecowitt) and today at 5 minutes."""
        now = datetime.now(self.tz).replace(tzinfo=None, microsecond=0)
        today = datetime.combine(now.date(), datetime.min.time())
        fetchers, jobs = [], []
        for mac in self.macs:  # both at once: a question may need either, and waits for it
            f30, f5 = self.make_fetcher(mac, PREFETCH_GROUPS), self.make_fetcher(mac, PREFETCH_GROUPS)
            fetchers += [f30, f5]
            jobs += [f30.get("30min", today - timedelta(days=6), now, refresh=refresh), f5.get("5min", today, now, refresh=refresh)]
        await asyncio.gather(*jobs)
        return sum(f.requests for f in fetchers)  # Ecowitt requests made

    def question_arrived(self):
        """Start fetching in the background; the question's own requests wait for it."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._safe_fetch(refresh=False))

    async def _safe_fetch(self, refresh: bool):
        try:
            await self.fetch(refresh=refresh)
        except Exception:
            log.exception("Recent data prefetch failed")

    async def keep_warm(self, skip_first: bool = False):
        """Refresh every few minutes, around the clock, then run the after-refresh checks.
        Runs until cancelled. skip_first: the startup warm-up already fetched, so wait first."""
        if skip_first:
            await asyncio.sleep(REFRESH_SECONDS)
        while True:
            await self._safe_fetch(refresh=True)
            for check in self.after_refresh:
                try:
                    await check()
                except Exception:
                    log.exception("After-refresh check failed")
            await asyncio.sleep(REFRESH_SECONDS)
