import asyncio
import logging

from lib.warm import Warmer, every, safely


async def test_a_warmer_runs_one_refresh_at_a_time():
    """A timed refresh waits for a question's to finish, and a question that arrives during a refresh starts none of its own."""
    release, started, seen = asyncio.Event(), asyncio.Event(), []

    async def refresh(fresh):
        seen.append(("start", fresh))
        started.set()
        await release.wait()
        seen.append(("end", fresh))
    warmer = Warmer(refresh, interval=0)
    warmer.poke()
    await started.wait()                            # a question's refresh is running
    timer = asyncio.create_task(warmer.run())       # and the timed one falls due at once
    warmer.poke()                                   # another question meanwhile
    for _ in range(5):
        await asyncio.sleep(0)
    assert seen == [("start", False)]
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    timer.cancel()
    assert seen[:3] == [("start", False), ("end", False), ("start", True)]
    assert all(a[0] != b[0] for a, b in zip(seen, seen[1:]))   # every refresh ends before the next starts


async def test_a_background_failure_has_its_traceback_logged_only_when_it_points_at_code(caplog):
    async def broken():
        raise TypeError("bad")

    async def down():
        raise ConnectionError("no route to host")
    caplog.set_level(logging.WARNING)
    assert await safely(broken) is None and await safely(down) is None      # neither escapes
    bug, outage = [r for r in caplog.records if r.name == "lib.warm"]
    assert bug.exc_info and "TypeError: bad" in bug.getMessage()
    assert not outage.exc_info and "ConnectionError: no route to host" in outage.getMessage()


async def test_every_waits_as_long_as_its_delay_says_each_time():
    runs, asked = [], []

    async def work():
        runs.append(1)

    def delay():
        asked.append(len(runs))
        return 0
    task = asyncio.create_task(every(delay, work))
    for _ in range(6):
        await asyncio.sleep(0)
    task.cancel()
    assert asked[:3] == [1, 2, 3]          # asked again after each run
