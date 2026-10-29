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
from time import monotonic, time as epoch_now

from ..timeutil import naive_local, now_local, seconds_until
from ..warm import every
from .api import CYCLES, MIN_GAP_SECONDS, kept_from
from .fetch import spans
from ..storage.write import horizon

log = logging.getLogger(__name__)

RUN_AT = time(1, 30)
PACE_SECONDS = 2.0  # gap between requests, so questions aren't starved
COARSE_CYCLES = tuple(c for c in reversed(CYCLES) if c != "5min")   # coarsest first; 5-minute data goes day by day, before them
OUTAGE_STOP = 3     # requests failing in a row (no answer, or a busy Ecowitt) that end a run: the rest wait for the next


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
            oldest = kept_from(cycle, today)
            return max(oldest, earliest) if earliest else oldest

        def settled(cycle: str) -> datetime:
            """The end of the last whole bucket that is final at this resolution. Whole buckets only,
            so the edge holds still between runs instead of creeping forward with the clock (which
            would leave a few seconds uncovered, and refetched, every time)."""
            size = CYCLES[cycle].seconds
            ts = horizon(cycle, epoch_now()) - size
            return naive_local(ts - ts % size - 1, tz)

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
        started = monotonic()
        work, fetcher = list(self._work()), self.station.fetcher(self.groups)
        todo = []   # what is held is known in memory: no read
        for cycle, runs in {c: [w for w in work if w[0] == c] for c in dict.fromkeys(w[0] for w in work)}.items():
            missing = fetcher.uncovered(cycle, [(fetcher.epoch(start), fetcher.epoch(end)) for _, start, end in runs])
            todo += [w for w, gaps in zip(runs, missing) if any(gaps.values())]
        if todo:
            # each range takes the pause after it plus about a second for the request itself
            seconds = len(todo) * (max(self.pace, MIN_GAP_SECONDS) + 1)
            if seconds >= 60:   # a short run only gets the line when it finishes
                log.info("Backfill Ecowitt: %d requests to fetch (about %s)", len(todo),
                         f"{seconds:.0f} s" if seconds < 90 else f"{seconds / 60:.0f} min")
        fetched = failed = in_a_row = 0
        for i, (cycle, start, end) in enumerate(todo, 1):
            fetcher = self.station.fetcher(self.groups)
            await fetcher.get(cycle, start, end, load=False)  # only to cache it
            if not fetcher.errors or fetcher.rejected and await self._drop_unsupported_groups(cycle, start, end):
                fetched, in_a_row = fetched + 1, 0
            else:
                failed += 1
                in_a_row = 0 if fetcher.rejected else in_a_row + 1   # a refusal is about this range; a timeout, about Ecowitt
                if in_a_row == OUTAGE_STOP:
                    log.warning("Backfill Ecowitt: %d requests in a row failed (%s); the other %d wait for the next run", in_a_row,
                                fetcher.errors[-1], len(todo) - i)
                    break
            if i % 20 == 0 and i < len(todo):
                log.info("Backfill Ecowitt: %d of %d requests done", i, len(todo))
            await asyncio.sleep(self.pace)
        log.info("Backfill Ecowitt: %d/%d fetched/failed, %s, %.0fs", fetched, failed, self.held(), monotonic() - started)
        return fetched, failed

    async def _drop_unsupported_groups(self, cycle: str, start: datetime, end: datetime) -> bool:
        """After a request Ecowitt refused, try each group alone. Groups that fail on their own are dropped
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
        """Days of history held per resolution, e.g. "88/363/728/1456d held (5m/30m/4h/1d)" """
        cycles = ("5min", "30min", "4hour", "1day")
        days = "/".join(str(self.station.fetcher(self.groups).days_held(c)) for c in cycles)
        return f"{days}d held (5m/30m/4h/1d)"

    def _seconds_until_next_run(self) -> float:
        return seconds_until(RUN_AT.hour, RUN_AT.minute, self.station.tz)

    async def loop(self):
        """Backfill now, then again every night at RUN_AT, until cancelled. A run that fails is logged; the next night's tries again."""
        await every(self._seconds_until_next_run, self.run_once)
