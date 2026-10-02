"""Keeping a data source warm, the same way for every source.

A source's `refresh(fresh)` fetches what questions usually need. A Warmer runs it
  - in the background as soon as a question arrives (the question's own reads wait for it), and
  - on a timer, so answers (and alert checks) find fresh data ready.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

log = logging.getLogger(__name__)

REFRESH_SECONDS = 240  # under the 5 minute freshness window both sources use
FAST_REFRESH_SECONDS = 60  # the weather station: each refresh also runs the rain and temperature alert checks, so this is their delay


SYNC_HOURS = (6, 18)  # the sources that are websites are only fetched from 6 am to 6 pm local time, to keep the hits down


def in_sync_hours(now: datetime) -> bool:
    return SYNC_HOURS[0] <= now.hour < SYNC_HOURS[1]


async def safely(fn: Callable[..., Awaitable], *args):
    """Await fn(*args); log a failure instead of raising (background work must not die)."""
    try:
        return await fn(*args)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("%s failed: %s: %s", getattr(fn, "__qualname__", fn), type(e).__name__, e)


async def every(seconds: float, fn: Callable[[], Awaitable]):
    """Call fn now, then every `seconds`, until cancelled."""
    while True:
        await safely(fn)
        await asyncio.sleep(seconds)


class Warmer:
    def __init__(self, refresh: Callable[[bool], Awaitable], interval: float = REFRESH_SECONDS):
        self.refresh, self.interval = refresh, interval
        self.after: list[Callable[[], Awaitable]] = []  # run after each timed refresh (alert checks)
        self._task: asyncio.Task | None = None

    def poke(self):
        """A question arrived: refresh in the background unless one is already running."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(safely(self.refresh, False))

    async def run(self):
        """Refresh every `interval` seconds until cancelled (startup already fetched once)."""
        while True:
            await asyncio.sleep(self.interval)
            await safely(self.refresh, True)
            for check in self.after:
                await safely(check)
