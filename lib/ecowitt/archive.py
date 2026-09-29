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

from .api import MAX_SPAN, MIN_GAP_SECONDS, RETENTION

log = logging.getLogger(__name__)

RUN_AT = time(1, 30)
PACE_SECONDS = 2.0  # gap between requests, so questions aren't starved
GROUPS = ["outdoor", "indoor", "pressure", "wind", "rainfall", "rainfall_piezo"]
COARSE_CYCLES = ("1day", "4hour", "30min")  # 5-minute data goes day by day, first


class Archive:
    def __init__(self, station, pace: float | None = None):
        self.station, self.groups = station, list(GROUPS)
        self.pace = PACE_SECONDS if pace is None else pace

    def _work(self) -> Iterator[tuple[str, datetime, datetime]]:
        """(cycle, start, end) ranges to have in the cache, oldest first within each cycle."""
        tz = self.station.tz
        now = datetime.now(tz).replace(tzinfo=None, microsecond=0)
        today = now.date()
        created = self.station.created
        earliest = (created.date() - timedelta(days=1)) if created else None

        def first_day(cycle: str):
            oldest = today - timedelta(days=RETENTION[cycle] - 2)  # stay clear of the retention edge
            return max(oldest, earliest) if earliest else oldest

        for n in range((today - first_day("5min")).days, 0, -1):  # finished days only
            day = today - timedelta(days=n)
            yield "5min", datetime.combine(day, time()), datetime.combine(day, time(23, 59, 59))
        for cycle in COARSE_CYCLES:
            t = datetime.combine(first_day(cycle), time())
            while t < now:
                end = min(t + MAX_SPAN[cycle] - timedelta(seconds=1), now)
                yield cycle, t, end
                t = end + timedelta(seconds=1)

    async def run_once(self) -> tuple[int, int]:
        """Cache whatever is missing. Returns (ranges fetched, ranges failed). Each range is saved as
        soon as it arrives, so an interrupted run just carries on from there next time."""
        todo = []
        for cycle, start, end in self._work():
            fetcher = self.station.fetcher(self.groups)
            if not await fetcher.covered(cycle, fetcher.epoch(start), fetcher.epoch(end)):
                todo.append((cycle, start, end))
        if todo:
            # each range takes the pause after it plus about a second for the request itself
            seconds = len(todo) * (max(self.pace, MIN_GAP_SECONDS) + 1)
            log.info("Ecowitt archive: %d range(s) to fetch (about %s)", len(todo),
                     f"{seconds:.0f} s" if seconds < 90 else f"{seconds / 60:.0f} min")
        fetched = failed = 0
        for i, (cycle, start, end) in enumerate(todo, 1):
            fetcher = self.station.fetcher(self.groups)
            await fetcher.get(cycle, start, end)
            if fetcher.errors and not await self._drop_unsupported_groups(cycle, start, end):
                failed += 1
            else:
                fetched += 1
            if i % 20 == 0 and i < len(todo):
                log.info("Ecowitt archive: %d of %d range(s) done", i, len(todo))
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
            await probe.get(cycle, start, end)
            if probe.errors and "busy" not in probe.errors[-1].lower():
                bad.append(group)
            await asyncio.sleep(self.pace)
        if bad and len(bad) < len(self.groups):
            self.groups = [g for g in self.groups if g not in bad]
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
                log.info("Ecowitt archive: %d cached, %sheld %s, %.0fs", fetched, f"{failed} failed, " if failed else "",
                         self.held(), (datetime.now() - started).total_seconds())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ecowitt archive run failed")
            await asyncio.sleep(self._seconds_until_next_run())
