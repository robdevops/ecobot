"""Writes the two small cache files in this folder. They were generated ONCE by the pre-recode code; the tests load them
to prove that caches written by older versions still open and read the same. Do not regenerate them from new code."""
import asyncio
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lib.airgradient import AirGradient  # noqa: E402
from lib.ecowitt import Archive, Ecowitt, api, archive  # noqa: E402
from tests.fakes import air_transport, config, ecowitt_transport  # noqa: E402

HERE = Path(__file__).resolve().parent


async def build():
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(api, "MIN_GAP_SECONDS", 0), mock.patch.object(archive, "PACE_SECONDS", 0):
        cfg = config(Path(tmp))
        transport, _ = ecowitt_transport(history_days=4)
        eco = Ecowitt(cfg, transport=transport)
        await eco.start()
        await Archive(eco).run_once()
        await eco.close()
        air_t, _ = air_transport()
        air = AirGradient(cfg, transport=air_t)
        await air.start()
        from datetime import datetime, timedelta
        now = air._now()
        await air.history((now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S"))
        await air.close()
        (HERE / "ecowitt_cache.sqlite").write_bytes(cfg.cache_path.read_bytes())
        (HERE / "airgradient_cache.sqlite").write_bytes(cfg.air_cache_path.read_bytes())


asyncio.run(build())
