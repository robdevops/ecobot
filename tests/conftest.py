import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TZ", "Australia/Melbourne")

import pytest


@pytest.fixture(scope="session")
def archived_cache(tmp_path_factory):
    """A cache file holding the fake station's whole archive, built once. Tests copy it (archived_station)."""
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
        return path / "cache.sqlite"
    path = tmp_path_factory.mktemp("archive")
    with mock.patch.object(api, "MIN_GAP_SECONDS", 0), mock.patch.object(archive, "PACE_SECONDS", 0):
        return asyncio.run(build(path))


@pytest.fixture(autouse=True)
def no_request_spacing(monkeypatch):
    from lib.ecowitt import api
    monkeypatch.setattr(api, "MIN_GAP_SECONDS", 0)
