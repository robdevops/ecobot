import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TZ", "Australia/Melbourne")

import pytest


@pytest.fixture
def alert_state(tmp_path):
    """The bot's chats and settings (an AlertState) in a file of the test's own: what a Bot is given, as in the running bot."""
    from lib.alerts import AlertState
    return AlertState(tmp_path / "state.json")


@pytest.fixture(scope="session")
def archived_cache(tmp_path_factory):
    """The bot's file (ecobot.sqlite) holding the fake station's whole archive, built once. Tests copy it (archived_station)."""
    import asyncio
    from unittest import mock

    from lib.ecowitt import Archive, Ecowitt, api, archive
    from tests.fakes import config, ecowitt_transport

    async def build(path):
        transport, _ = ecowitt_transport()
        eco = Ecowitt(config(path), transport=transport)
        await eco.start()
        await Archive(eco).run_once()
        await eco.close()
        return config(path).storage_path
    path = tmp_path_factory.mktemp("archive")
    with mock.patch.object(api, "MIN_GAP_SECONDS", 0), mock.patch.object(archive, "PACE_SECONDS", 0), \
            mock.patch.object(asyncio, "to_thread", _inline):
        return asyncio.run(build(path))


@pytest.fixture(scope="session")
def old_archived_cache(tmp_path_factory):
    """The fake station's whole archive in the old cache's format (ecowitt_cache.sqlite, from before ecobot.sqlite), built once, for the
    move into the new file and its checks. Written as the old archive wrote it (tests/old_cache.py): each cycle's spans, from the fake
    station's answers, its settled readings only."""
    from datetime import datetime, time, timedelta

    from lib.ecowitt.api import CYCLES, GROUPS, UNITS, kept_from
    from lib.ecowitt.fetch import spans
    from tests.old_cache import HistoryCache
    from lib.timeutil import local_epoch
    from tests.fakes import HISTORY_DAYS, MAC, TZ, FakeEcowitt
    path = tmp_path_factory.mktemp("old-archive") / "ecowitt_cache.sqlite"
    cache = HistoryCache(path, UNITS)
    now = datetime.now(TZ).replace(tzinfo=None, microsecond=0)
    first = now.date() - timedelta(days=HISTORY_DAYS - 1)
    for cycle in CYCLES:
        for a, b in spans(cycle, datetime.combine(max(kept_from(cycle, now.date()), first), time()), now):
            asked = {"cycle_type": cycle, "start_date": f"{a:%Y-%m-%d %H:%M:%S}", "end_date": f"{b:%Y-%m-%d %H:%M:%S}", "call_back": ",".join(GROUPS)}
            cache.store(MAC, cycle, list(GROUPS), FakeEcowitt._history(asked), local_epoch(a, TZ), local_epoch(b, TZ))
    cache.close()
    return path


async def _inline(func, /, *args, **kwargs):
    """asyncio.to_thread without the thread: tests use tiny in-memory-speed SQLite calls, and a thread hop per call
    is most of their run time."""
    return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def no_request_spacing(monkeypatch):
    """Also closes every SQLite connection a test opened, so none is left for the garbage collector (a ResourceWarning)."""
    import asyncio
    import contextlib
    import sqlite3
    monkeypatch.setattr(asyncio, "to_thread", _inline)
    real_connect = sqlite3.connect
    opened = []

    def fast_connect(*args, **kwargs):  # tests don't need the caches to survive a power cut
        db = real_connect(*args, **kwargs)
        db.execute("PRAGMA synchronous=OFF")
        with contextlib.suppress(sqlite3.OperationalError):   # a file another connection holds in WAL mode stays in it
            db.execute("PRAGMA journal_mode=MEMORY")
        opened.append(db)
        return db
    monkeypatch.setattr(sqlite3, "connect", fast_connect)
    from lib.ecowitt import api
    monkeypatch.setattr(api, "MIN_GAP_SECONDS", 0)
    yield
    for db in opened:
        db.close()
