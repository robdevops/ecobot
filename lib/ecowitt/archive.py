"""Nightly archive of 5-minute history.

Ecowitt drops 5-minute data after 90 days. This copies every finished day into the history
cache before that happens, so records keep exact times and values forever. At startup it
backfills all days Ecowitt still has (oldest first, as those expire soonest), then fetches the
previous day each night. Days already cached are skipped and failed days are retried next run.
"""

import asyncio
import logging
from datetime import datetime, time, timedelta

from .api import RETENTION

log = logging.getLogger(__name__)

RUN_AT = time(1, 30)
PACE_SECONDS = 2.0  # gap between requests, so questions aren't starved
GROUPS = ["outdoor", "indoor", "pressure", "wind", "rainfall", "rainfall_piezo"]


class Archive:
    def __init__(self, station, pace: float | None = None):
        self.station, self.groups = station, list(GROUPS)
        self.pace = PACE_SECONDS if pace is None else pace

    async def run_once(self) -> tuple[int, int]:
        """Archive finished days not yet cached. Returns (days fetched, days failed)."""
        tz = self.station.tz
        today = datetime.now(tz).date()
        days = [today - timedelta(days=n) for n in range(RETENTION["5min"] - 2, 0, -1)]  # oldest first, clear of the edge
        fetched = failed = 0
        for day in days:
            start, end = datetime.combine(day, time()), datetime.combine(day, time(23, 59, 59))
            fetcher = self.station.fetcher(self.groups)
            if await fetcher.covered("5min", fetcher.epoch(start), fetcher.epoch(end)):
                continue
            await fetcher.get("5min", start, end)
            if fetcher.errors and not await self._drop_unsupported_groups(start, end):
                failed += 1
            else:
                fetched += 1
            await asyncio.sleep(self.pace)
        return fetched, failed

    async def _drop_unsupported_groups(self, start: datetime, end: datetime) -> bool:
        """After a failed request, try each group alone. Groups that fail on their own are dropped
        (the station probably doesn't have them). True if the day was then archived for the rest."""
        if len(self.groups) < 2:
            return False
        bad = []
        for group in self.groups:
            probe = self.station.fetcher([group])
            await probe.get("5min", start, end)
            if probe.errors and "busy" not in probe.errors[-1].lower():
                bad.append(group)
            await asyncio.sleep(self.pace)
        if bad and len(bad) < len(self.groups):
            self.groups = [g for g in self.groups if g not in bad]
            log.warning("Archive: dropping group(s) Ecowitt rejected: %s (keeping %s)",
                        ", ".join(bad), ", ".join(self.groups))
            return True
        return False

    def _seconds_until_next_run(self) -> float:
        now = datetime.now(self.station.tz)
        nxt = now.replace(hour=RUN_AT.hour, minute=RUN_AT.minute, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return (nxt - now).total_seconds()

    async def loop(self):
        """Run every night at RUN_AT until cancelled (the startup warm-up did the backfill)."""
        while True:
            await asyncio.sleep(self._seconds_until_next_run())
            try:
                started = datetime.now()
                fetched, failed = await self.run_once()
                log.info("Archive run: %d day(s) archived, %d failed, %.0fs", fetched, failed,
                         (datetime.now() - started).total_seconds())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Archive run failed")
