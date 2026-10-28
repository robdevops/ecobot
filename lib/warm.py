"""Keeping a data source warm, the same way for every source.

A source's `refresh(fresh)` fetches what questions usually need. A Warmer runs it
  - in the background as soon as a question arrives (the question's own reads wait for it), and
  - on a timer, so answers (and alert checks) find fresh data ready.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from . import counts
from .logs import BUGS

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
    except Exception as e:
        log.warning("%s failed: %s: %s", getattr(fn, "__qualname__", fn), type(e).__name__, e, exc_info=isinstance(e, BUGS))


async def every(delay: float | Callable[[], float], fn: Callable[[], Awaitable]):
    """Call fn now, then again after `delay` seconds (or as many as delay() says each time), until cancelled."""
    while True:
        await safely(fn)
        await asyncio.sleep(delay() if callable(delay) else delay)


class Warmer:
    def __init__(self, refresh: Callable[[bool], Awaitable], interval: float = REFRESH_SECONDS):
        self.refresh, self.interval = refresh, interval
        self.after: list[Callable[[], Awaitable]] = []  # run after each timed refresh (alert checks)
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()   # one refresh at a time: a timed one waits for a question's

    async def _refresh(self, fresh: bool):
        counts.detach()   # a refresh a question started is not the question's work
        async with self._lock:
            await safely(self.refresh, fresh)

    async def stop(self):
        """Cancel a refresh a question started, if one is still running (the timed ones end with their task)."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def poke(self):
        """A question arrived: refresh in the background, unless a refresh (a question's or a timed one) is running."""
        if not self._lock.locked() and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._refresh(False))

    async def run(self):
        """Refresh every `interval` seconds until cancelled (startup already fetched once)."""
        while True:
            await asyncio.sleep(self.interval)
            await self._refresh(True)
            for check in self.after:
                await safely(check)
