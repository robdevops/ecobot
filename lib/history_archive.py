"""Nightly archive of 5-minute history.

Ecowitt drops 5-minute data after 90 days. This job copies every finished day into
the history cache before that happens, so records keep exact times and values
forever. At startup it backfills all days Ecowitt still has (oldest first, as those
expire soonest), then fetches the previous day each night. Days already in the cache
are skipped and failed days are retried on the next run.
"""

import asyncio
import logging
import re
from datetime import datetime, time, timedelta, tzinfo

from .ecowitt_history import INSTALLED, RETENTION, Fetcher
from .history_cache import HistoryCache
from .mcp_manager import MCPManager

log = logging.getLogger(__name__)

PACE_SECONDS = 2.0  # gap between archive requests, so questions aren't starved
MAC_RE = re.compile(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b")


def macs_from(devices_text: str) -> list[str]:
    return sorted({m.upper() for m in MAC_RE.findall(devices_text or "")})


class Archive:
    def __init__(self, mcp: MCPManager, tz: tzinfo, cache: HistoryCache, macs: list[str],
                 groups: list[str], run_at: time, pace: float = PACE_SECONDS):
        self.mcp, self.tz, self.cache, self.macs = mcp, tz, cache, macs
        self.groups, self.run_at, self.pace = list(groups), run_at, pace
        self.oname, self.units = next(iter(INSTALLED.items()))

    def _fetcher(self, mac: str, groups: list[str]) -> Fetcher:
        return Fetcher(self.mcp, self.oname, {"mac": mac, "callback": ",".join(groups), **self.units},
                       self.tz, self.cache)

    async def run_once(self, days_back: int | None = None) -> tuple[int, int]:
        """Archive finished days not yet cached. Returns (days fetched, days failed)."""
        today = datetime.now(self.tz).date()
        days_back = days_back or RETENTION["5min"] - 2  # stay clear of the retention edge
        days = [today - timedelta(days=n) for n in range(days_back, 0, -1)]  # oldest first
        fetched = failed = 0
        for mac in self.macs:
            for day in days:
                start, end = datetime.combine(day, time()), datetime.combine(day, time(23, 59, 59))
                fetcher = self._fetcher(mac, self.groups)
                if await fetcher.covered("5min", fetcher._epoch(start), fetcher._epoch(end)):
                    continue
                await fetcher.get("5min", start, end)
                if fetcher.errors and not await self._drop_unsupported_groups(mac, start, end):
                    failed += 1
                else:
                    fetched += 1
                await asyncio.sleep(self.pace)
        return fetched, failed

    async def _drop_unsupported_groups(self, mac: str, start: datetime, end: datetime) -> bool:
        """After a failed request, try each group alone. Groups that fail on their own are
        dropped from the archive (the station probably doesn't have them). Returns True
        if the day was then archived for the remaining groups."""
        if len(self.groups) < 2:
            return False
        bad = []
        for group in list(self.groups):
            probe = self._fetcher(mac, [group])
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
        now = datetime.now(self.tz)
        nxt = now.replace(hour=self.run_at.hour, minute=self.run_at.minute, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        return (nxt - now).total_seconds()

    async def loop(self, skip_first: bool = False):
        """Backfill now (unless the startup warm-up already did), then run every night at
        run_at. Runs until cancelled."""
        log.debug("Archive: 5-minute data for %s, groups %s, nightly at %s",
                 ", ".join(self.macs), ", ".join(self.groups), self.run_at.strftime("%H:%M"))
        if skip_first:
            await asyncio.sleep(self._seconds_until_next_run())
        while True:
            try:
                started = datetime.now()
                fetched, failed = await self.run_once()
                log.info("Archive run: %d day(s) archived, %d failed, %.0fs", fetched, failed,
                         (datetime.now() - started).total_seconds())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Archive run failed")
            await asyncio.sleep(self._seconds_until_next_run())
