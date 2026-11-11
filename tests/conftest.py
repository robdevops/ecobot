import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TZ", "Australia/Melbourne")
from tests import clocks   # noqa: E402  (ECOBOT_CLOCK: the whole suite at another time; before anything of lib/ is imported)
CLOCK = clocks.install()

import pytest


from lib.storage import Storage as _Storage
REAL_HOP = _Storage.__dict__["_hop"]   # (the staticmethod itself)
del _Storage

HEAVY = ("test_ecowitt.py", "test_scan.py", "test_startup.py", "test_airgradient.py", "test_airgradient_local.py", "test_minute_lines.py")


def pytest_collection_modifyitems(items):
    """The files with the slowest tests go first, so with several workers (pytest-xdist) none is left running one of them at the end."""
    items.sort(key=lambda item: (item.path.name not in HEAVY, HEAVY.index(item.path.name) if item.path.name in HEAVY else 0))


@pytest.fixture(autouse=True)
def no_settings_from_outside(monkeypatch):
    """Every setting's variable, by its name and its old one, unset for each test (a test sets what it reads): one set where the suite
    runs (a CI secret named like an old setting, an env file loaded in the shell) changes nothing."""
    from dataclasses import fields

    from lib.config import RETIRED_TOKEN, Config
    for f in fields(Config):
        for name in (f.metadata.get("env"), f.metadata.get("old")):
            if name:
                monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(RETIRED_TOKEN, raising=False)


@pytest.fixture(autouse=True)
def the_log_levels_as_they_were():
    """The programs' main() (ecobot.py, ecobot_mcp) set the "ecobot" and "lib" loggers' levels from LOG_LEVEL: put back after every
    test, so a test that reads the debug lines doesn't depend on which ran before it on its worker."""
    import logging
    levels = {name: logging.getLogger(name).level for name in ("ecobot", "lib")}
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture
def alert_state(tmp_path):
    """The bot's chats and settings (an AlertState) in a file of the test's own: what a Bot is given, as in the running bot."""
    from lib.alerts.notify import AlertState
    return AlertState(tmp_path / "state.json")


@pytest.fixture
async def serve():
    """ecobot-mcp in memory, the bot's client of it connected (tests/served.py): `weather = await serve(ctx, alert_log, tools=...,
    sources=...)`, each closed when the test ends."""
    import contextlib

    from tests.served import ecobot_mcp
    async with contextlib.AsyncExitStack() as stack:
        async def start(*args, **kwargs):
            return await stack.enter_async_context(ecobot_mcp(*args, **kwargs))
        yield start


@pytest.fixture(scope="session")
def archived_cache(tmp_path_factory):
    """The bot's file (ecobot.sqlite) holding the fake station's whole archive, built once an hour and shared by every worker and run
    (under pytest's own temp folder, named for the hour and the code that shapes it). Tests copy it (archived_station)."""
    from datetime import datetime
    moved = "-moved" if CLOCK else ""   # (built at a clock ECOBOT_CLOCK moved: never shared with a run at the real one)
    return _shared_archive(tmp_path_factory, f"{datetime.now():%Y%m%d%H}{moved}", None)


@pytest.fixture(scope="session")
def pinned_archive(tmp_path_factory):
    """pinned_archive(when) -> the file holding the fake station's whole archive as it was at `when` (an aware datetime), built with
    the clock pinned there (tests/clocks.pin) once for the code that shapes it, and shared by every worker and run after. A test pins
    the clock at the same time (clocks.pin) and copies it (archived_station): the station at a fixed time of day, whatever the time."""
    return lambda when: _shared_archive(tmp_path_factory, f"pinned-{when.timestamp():.0f}", when)


@pytest.fixture(scope="session")
def shared_archive(tmp_path_factory):
    """shared_archive(name, make, shaped_by) -> the file make(folder, patch) builds, built once for every worker and run (shared_file):
    a module's own archive (at its own fixed clock), which its tests copy."""
    return lambda name, make, shaped_by=(): shared_file(tmp_path_factory, name, make, shaped_by)


@pytest.fixture
def daytime_archive(pinned_archive, monkeypatch):
    """The fake station's archive at a fixed time, 2:05pm on Tue 10 Nov 2026 (clocks.DAYTIME), the clock pinned there for the test: for
    a test whose answer depends on the date or the time of day (a year so far with days in it, this morning over)."""
    cache = pinned_archive(clocks.DAYTIME)
    clocks.pin(monkeypatch, clocks.DAYTIME)
    return cache


def _shared_archive(tmp_path_factory, name: str, pinned) -> Path:
    """The fake station's whole archive in a file under pytest's own temp folder, named for `name` and the code that shapes it: built by
    the first worker that asks (the others wait and find it), at the clock as it is, or pinned at `pinned`."""
    from lib.ecowitt import Archive, Ecowitt
    from tests.fakes import config, ecowitt_transport

    async def build(path):
        transport, _ = ecowitt_transport()
        eco = Ecowitt(config(path), transport=transport)
        await eco.start()
        await Archive(eco).run_once()
        await eco.close()
        return config(path).storage_path

    def make(folder, patch):
        if pinned is not None:
            clocks.pin(patch, pinned)
        return _run(build(folder))
    return shared_file(tmp_path_factory, name, make)


def shared_file(tmp_path_factory, name: str, make, shaped_by=()) -> Path:
    """A file built once by make(folder, patch) -> its path (in that empty folder, with a MonkeyPatch undone after it, the archive's
    pacing off), kept under pytest's own temp folder named for `name` and the code that shapes it (lib/storage, lib/ecowitt,
    tests/fakes.py and `shaped_by`, the files of the test that builds it): built by the first worker that asks, the others wait and find
    it, and later runs too. A test copies it, so none sees another's writes."""
    import fcntl
    import hashlib

    from lib.ecowitt import api, archive
    root = Path(__file__).resolve().parent.parent
    files = sorted({*root.glob("lib/storage/*.py"), *root.glob("lib/ecowitt/*.py"), root / "tests" / "fakes.py",
                    root / "tests" / "clocks.py", *map(Path, shaped_by)})
    digest = hashlib.sha1(b"".join(f.read_bytes() for f in files)).hexdigest()[:12]
    base = tmp_path_factory.getbasetemp()   # this run's folder under pytest-of-<user> (a worker's is a folder under that)
    while not base.parent.name.startswith("pytest-of-"):
        base = base.parent
    shared = base.parent / f"ecobot-archive-{name}-{digest}.sqlite"
    with open(shared.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)   # the first worker builds it; the others wait and find it
        if not shared.exists():
            folder = tmp_path_factory.mktemp("archive")
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(api, "MIN_GAP_SECONDS", 0)
                patch.setattr(archive, "PACE_SECONDS", 0)
                patch.setattr(asyncio, "to_thread", _inline)
                built = make(folder, patch)
            os.replace(built, shared)
        return shared


def _run(coro):
    """asyncio.run on a thread of its own (an async test may be asking, its loop running on this one)."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


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


@pytest.fixture(autouse=True)
def charts_come_back_from_json(monkeypatch):
    """Every chart a test builds is also written as JSON and read back (specs.to_json, from_json), and must come back the same: the
    bot draws each chart from its JSON. A chart that doesn't fails the test when it ends (a tool that drew it may catch the error)."""
    import json
    from lib import specs
    from tests.fakes import TZ
    built, wrong, busy = specs.Chart.__post_init__, [], []

    def checked(chart):
        built(chart)
        if busy:   # the chart from_json is building
            return
        busy.append(chart)
        try:
            back = specs.from_json(json.loads(json.dumps(specs.to_json(chart, TZ))))
            if back != chart:
                wrong.append(f"{chart.title!r} came back from its JSON changed: {difference(chart, back)}")
        except Exception as e:
            wrong.append(f"{chart.title!r} couldn't go through JSON: {type(e).__name__}: {e}")
        finally:
            busy.pop()
    monkeypatch.setattr(specs.Chart, "__post_init__", checked)
    yield
    assert not wrong, wrong[0]


def difference(a, b, path: str = "chart") -> str:
    """Where two charts (or parts of them) first differ, "chart.panels[0].lines[1].x[0]: 1 != 2", rather than both whole."""
    from dataclasses import fields, is_dataclass
    if is_dataclass(a) and type(a) is type(b):
        name = next(f.name for f in fields(a) if getattr(a, f.name) != getattr(b, f.name))
        return difference(getattr(a, name), getattr(b, name), f"{path}.{name}")
    if type(a) is type(b) and isinstance(a, (list, tuple)) and len(a) == len(b):
        i = next(i for i, (x, y) in enumerate(zip(a, b)) if x != y)
        return difference(a[i], b[i], f"{path}[{i}]")
    return f"{path}: {a!r} != {b!r}"
