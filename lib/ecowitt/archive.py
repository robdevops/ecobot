"""Keeps the station's whole history in the cache, so questions rarely need Ecowitt inline.

Ecowitt keeps 5-minute data for 90 days, 30-minute for a year, 4-hour for two years and daily
for four. This copies every cycle into the history cache before it expires, oldest first (that
expires soonest), skipping ranges already cached and anything before the station existed. It
runs at startup (backfill) and each night (the newest days). Failed ranges are retried next run.
Keeping 5-minute data forever is what gives records their exact times and values.
"""

import asyncio
import logging
from collections.abc import Iterator
from datetime import datetime, time, timedelta

from ..timeutil import now_local
from .api import MIN_GAP_SECONDS, RETENTION
from .history import spans
from .store import BUCKET_SECONDS, horizon, subtract

log = logging.getLogger(__name__)

RUN_AT = time(1, 30)
PACE_SECONDS = 2.0  # gap between requests, so questions aren't starved
COARSE_CYCLES = ("1day", "4hour", "30min")  # 5-minute data goes day by day, first


class Archive:
    def __init__(self, station, pace: float | None = None):
        self.station = station
        self.pace = PACE_SECONDS if pace is None else pace

    @property
    def groups(self) -> list[str]:
        return self.station.groups  # shared with the warm-up, so both fetch the same thing

    def _work(self) -> Iterator[tuple[str, datetime, datetime]]:
        """(cycle, start, end) ranges to have in the cache, oldest first within each cycle. Only
        settled data: the newest hours are still changing, and the warm-up keeps those fresh."""
        tz = self.station.tz
        now = now_local(tz)
        today = now.date()
        created = self.station.created
        earliest = (created.date() - timedelta(days=1)) if created else None

        def first_day(cycle: str):
            oldest = today - timedelta(days=RETENTION[cycle] - 2)  # stay clear of the retention edge
            return max(oldest, earliest) if earliest else oldest

        def settled(cycle: str) -> datetime:
            """The end of the last whole bucket that is final at this resolution. Whole buckets only,
            so the edge holds still between runs instead of creeping forward with the clock (which
            would leave a few seconds uncovered, and refetched, every time)."""
            size = BUCKET_SECONDS[cycle]
            ts = horizon(cycle) - size
            return datetime.fromtimestamp(ts - ts % size - 1, tz).replace(tzinfo=None)

        for n in range((today - first_day("5min")).days, 0, -1):  # finished days only
            day = today - timedelta(days=n)
            start, end = datetime.combine(day, time()), min(datetime.combine(day, time(23, 59, 59)), settled("5min"))
            if start < end:
                yield "5min", start, end
        for cycle in COARSE_CYCLES:
            for start, end in spans(cycle, datetime.combine(first_day(cycle), time()), settled(cycle)):
                yield cycle, start, end

    async def run_once(self) -> tuple[int, int]:
        """Cache whatever is missing. Returns (ranges fetched, ranges failed). Each range is saved as
        soon as it arrives, so an interrupted run just carries on from there next time."""
        # What is cached, read once (a query per resolution and group) rather than once per range
        cover = await asyncio.to_thread(lambda: {(c, g): self.station.cache.coverage(self.station.mac, c, g)
                                                  for c in ("5min", *COARSE_CYCLES) for g in self.groups})
        epoch = self.station.fetcher(self.groups).epoch
        todo = [(cycle, start, end) for cycle, start, end in self._work()
                if any(subtract((epoch(start), epoch(end)), cover[cycle, g]) for g in self.groups)]
        if todo:
            # each range takes the pause after it plus about a second for the request itself
            seconds = len(todo) * (max(self.pace, MIN_GAP_SECONDS) + 1)
            log.info("Ecowitt archive: %d req to fetch (about %s)", len(todo),
                     f"{seconds:.0f} s" if seconds < 90 else f"{seconds / 60:.0f} min")
        fetched = failed = 0
        for i, (cycle, start, end) in enumerate(todo, 1):
            fetcher = self.station.fetcher(self.groups)
            await fetcher.get(cycle, start, end, load=False)  # only to cache it
            if fetcher.errors and not await self._drop_unsupported_groups(cycle, start, end):
                failed += 1
            else:
                fetched += 1
            if i % 20 == 0 and i < len(todo):
                log.info("Ecowitt archive: %d of %d req done", i, len(todo))
            await asyncio.sleep(self.pace)
        return fetched, failed

    async def _drop_unsupported_groups(self, cycle: str, start: datetime, end: datetime) -> bool:
        """After a failed request, try each group alone. Groups that fail on their own are dropped
        (the station probably doesn't have them). True if the range was then cached for the rest."""
        if len(self.groups) < 2:
            return False
        bad = []
        for group in self.groups:
            probe = self.station.fetcher([group])
            await probe.get(cycle, start, end, load=False)
            if probe.rejected:  # Ecowitt said no; a timeout or "too frequent" says nothing about the group
                bad.append(group)
            await asyncio.sleep(self.pace)
        if bad and len(bad) < len(self.groups):
            self.station.groups[:] = [g for g in self.groups if g not in bad]
            log.warning("Archive: dropping group(s) Ecowitt rejected: %s (keeping %s)",
                        ", ".join(bad), ", ".join(self.groups))
            return True
        return False

    def held(self) -> str:
        """Days of history held per resolution, e.g. "88/363/728/1456 days (5min/30min/4h/1d)" """
        cycles = ("5min", "30min", "4hour", "1day")
        days = "/".join(str(self.station.cache.days_held(self.station.mac, c, self.groups)) for c in cycles)
        return f"{days} days (5min/30min/4h/1d)"

    def _seconds_until_next_run(self) -> float:
        now = datetime.now(self.station.tz)
        nxt = now.replace(hour=RUN_AT.hour, minute=RUN_AT.minute, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return (nxt - now).total_seconds()

    async def loop(self):
        """Backfill now, then again every night at RUN_AT, until cancelled."""
        while True:
            try:
                started = datetime.now()
                fetched, failed = await self.run_once()
                log.info("Ecowitt archive: %d req, %sheld %s, %.0fs", fetched, f"{failed} failed, " if failed else "",
                         self.held(), (datetime.now() - started).total_seconds())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ecowitt archive run failed")
            await asyncio.sleep(self._seconds_until_next_run())
