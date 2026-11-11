"""Keeping a data source warm, the same way for every source.

A source's `refresh(fresh)` fetches what questions usually need. A Warmer runs it
  - in the background as soon as a question arrives (the question's own reads wait for it), and
  - on a timer, so answers (and alert checks) find fresh data ready.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime

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


class Tasks:
    """The tasks run in the background (the bot's, the service's): one that stops with an error is logged as it does, and close()
    cancels them."""

    def __init__(self):
        self.running: set[asyncio.Task] = set()

    def start(self, work: Coroutine) -> asyncio.Task:
        task = asyncio.create_task(work, name=getattr(work, "__qualname__", None))
        self.running.add(task)
        task.add_done_callback(self._ended)
        return task

    def _ended(self, task: asyncio.Task):
        self.running.discard(task)
        if not task.cancelled() and (error := task.exception()):
            log.error("%s stopped: %s: %s", task.get_name(), type(error).__name__, error, exc_info=error if isinstance(error, BUGS) else None)

    async def close(self):
        for task in self.running:
            task.cancel()
        await asyncio.gather(*self.running, return_exceptions=True)


class Warmer:
    """A source kept warm: refreshed every `interval` seconds (refresh(True): a timed refresh), the alert checks run after each."""

    def __init__(self, refresh: Callable[[bool], Awaitable], interval: float = REFRESH_SECONDS):
        self.refresh, self.interval = refresh, interval
        self.after: list[Callable[[], Awaitable]] = []  # run after each timed refresh (alert checks)

    async def run(self):
        """Refresh every `interval` seconds until cancelled (startup already fetched once)."""
        while True:
            await asyncio.sleep(self.interval)
            await safely(self.refresh, True)
            for check in self.after:
                await safely(check)
