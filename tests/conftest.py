import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TZ", "Australia/Melbourne")

import pytest


from lib.storage import Storage as _Storage
REAL_HOP = _Storage.__dict__["_hop"]   # (the staticmethod itself)
del _Storage

HEAVY = ("test_ecowitt.py", "test_scan.py", "test_startup.py", "test_airgradient.py", "test_airgradient_local.py", "test_minute_lines.py")


def pytest_collection_modifyitems(items):
    """The files with the slowest tests go first, so with several workers (pytest-xdist) none is left running one of them at the end."""
    items.sort(key=lambda item: (item.path.name not in HEAVY, HEAVY.index(item.path.name) if item.path.name in HEAVY else 0))


@pytest.fixture
def alert_state(tmp_path):
    """The bot's chats and settings (an AlertState) in a file of the test's own: what a Bot is given, as in the running bot."""
    from lib.alerts.notify import AlertState
    return AlertState(tmp_path / "state.json")


@pytest.fixture(scope="session")
def archived_cache(tmp_path_factory):
    """The bot's file (ecobot.sqlite) holding the fake station's whole archive, built once an hour and shared by every worker and run
    (under pytest's own temp folder, named for the hour and the code that shapes it). Tests copy it (archived_station)."""
    import asyncio
    import fcntl
    import hashlib
    from datetime import datetime
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
    root = Path(__file__).resolve().parent.parent
    shaped_by = sorted([*root.glob("lib/storage/*.py"), *root.glob("lib/ecowitt/*.py"), root / "tests" / "fakes.py"])
    digest = hashlib.sha1(b"".join(f.read_bytes() for f in shaped_by)).hexdigest()[:12]
    base = tmp_path_factory.getbasetemp()   # this run's folder under pytest-of-<user> (a worker's is a folder under that)
    while not base.parent.name.startswith("pytest-of-"):
        base = base.parent
    shared = base.parent / f"ecobot-archive-{datetime.now():%Y%m%d%H}-{digest}.sqlite"
    with open(shared.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)   # the first worker builds it; the others wait and find it
        if not shared.exists():
            path = tmp_path_factory.mktemp("archive")
            with mock.patch.object(api, "MIN_GAP_SECONDS", 0), mock.patch.object(archive, "PACE_SECONDS", 0), \
                    mock.patch.object(asyncio, "to_thread", _inline):
                built = asyncio.run(build(path))
            os.replace(built, shared)
        return shared


async def _inline(func, /, *args, **kwargs):
    """asyncio.to_thread without the thread: tests use tiny in-memory-speed SQLite calls, and a thread hop per call
    is most of their run time."""
    return func(*args, **kwargs)


@pytest.fixture
def threaded_storage(monkeypatch):
    """The file's reader and writer on their own threads, as in the running bot (the tests of those threads)."""
    from lib.storage import Storage
    monkeypatch.setattr(Storage, "_hop", REAL_HOP)


async def _inline_hop(pool, run, fn, args):
    """Storage._hop without the thread (after one turn of the loop, as the hop gives the other tasks)."""
    await asyncio.sleep(0)
    return run(fn, args)


@pytest.fixture(autouse=True)
def no_request_spacing(monkeypatch):
    """Also closes every SQLite connection a test opened, so none is left for the garbage collector (a ResourceWarning)."""
    import asyncio
    import contextlib
    import sqlite3
    monkeypatch.setattr(asyncio, "to_thread", _inline)
    from lib.storage import Storage
    monkeypatch.setattr(Storage, "_hop", staticmethod(_inline_hop))   # the file's reader and writer threads: the same, for the same reason
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
