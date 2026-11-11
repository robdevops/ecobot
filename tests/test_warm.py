import asyncio
import logging

from lib.warm import Warmer, every, safely


async def test_a_warmer_refreshes_every_interval_as_a_timed_refresh_then_runs_its_checks_and_a_failure_stops_neither():
    seen = []

    async def refresh(fresh):
        seen.append(("refresh", fresh))
        if len(seen) == 1:
            raise ConnectionError("no route to host")

    async def check():
        seen.append(("check",))
    warmer = Warmer(refresh, interval=0)
    warmer.after.append(check)
    task = asyncio.create_task(warmer.run())
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert seen[:4] == [("refresh", True), ("check",), ("refresh", True), ("check",)]


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
