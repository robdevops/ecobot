"""Runs ecobot.main() end to end against fake sources and a fake Telegram."""

import asyncio
import os
import signal
from types import SimpleNamespace as NS

import ecobot
from lib.ecowitt import archive
from datetime import datetime, timedelta, timezone

from lib.airgradient import source as air_source
from tests.fakes import air_transport, config, ecowitt_transport


class FakeApp:
    def __init__(self):
        self.sent, self.handlers = [], []
        self.updater = NS(start_polling=self._noop, stop=self._noop)
        self.bot = NS(username="testbot", send_message=self.send_message)

    async def _noop(self, *a, **k):
        pass

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    start = stop = _noop

    def add_handler(self, h, group=0):
        self.handlers.append(h)

    def add_error_handler(self, h):
        self.handlers.append(h)


class FakeBuilder:
    def __init__(self, app):
        self.app = app

    def __getattr__(self, name):
        return lambda *a, **k: self if name != "build" else self.app


async def test_main_starts_warms_and_shuts_down_cleanly(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
    cfg = config(tmp_path)
    eco_t, _ = ecowitt_transport(history_days=45)
    air_t, _ = air_transport(oldest=datetime.now(timezone.utc) - timedelta(days=5))
    app = FakeApp()
    monkeypatch.setattr(ecobot.Config, "from_env", classmethod(lambda cls: cfg))
    for cls, transport in ((ecobot.Ecowitt, eco_t), (ecobot.AirGradient, air_t)):
        monkeypatch.setattr(cls, "__init__", lambda self, c, transport=None, orig=cls.__init__, t=transport: orig(self, c, t))
    monkeypatch.setattr(ecobot.Application, "builder", staticmethod(lambda: FakeBuilder(app)))
    monkeypatch.setattr(archive, "PACE_SECONDS", 0)
    monkeypatch.setattr(air_source, "BACKFILL_PACE", 0)
    monkeypatch.setattr(air_source, "BACKFILL_EMPTY_STOP", 3)

    async def stop_when_archived():  # the background archives finish, then we shut down
        for _ in range(300):
            if " req, held " in caplog.text and "day(s) cached" in caplog.text:
                break
            await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)
    stopper = asyncio.create_task(stop_when_archived())
    await ecobot.main()
    await stopper
    log = caplog.text
    assert "Weather station: Ecowitt 'Fairleigh'" in log and "Bot @testbot running" in log
    assert "Startup warm-up:" in log and " req to fetch" in log and "AirGradient archive:" in log
    final = next(line for line in log.splitlines() if "Ecowitt archive:" in line and " req, held " in line)
    assert "failed" not in final and "days (5min/30min/4h/1d)" in final
    assert len(app.handlers) == 8  # start+help, reset, keyboard, alerts, other updates, membership, messages, errors


async def test_a_failed_first_refresh_does_not_stop_the_alerts_or_the_archives(tmp_path, monkeypatch, caplog):
    """If the first Ecowitt or AirGradient refresh raises, the archive and the backfill (and the alert
    monitor) must still start: the regular refreshes then catch up."""
    caplog.set_level("INFO")
    cfg = config(tmp_path)
    eco_t, _ = ecowitt_transport(history_days=45)
    air_t, _ = air_transport(oldest=datetime.now(timezone.utc) - timedelta(days=5))
    app = FakeApp()
    monkeypatch.setattr(ecobot.Config, "from_env", classmethod(lambda cls: cfg))
    for cls, transport in ((ecobot.Ecowitt, eco_t), (ecobot.AirGradient, air_t)):
        monkeypatch.setattr(cls, "__init__", lambda self, c, transport=None, orig=cls.__init__, t=transport: orig(self, c, t))
    monkeypatch.setattr(ecobot.Application, "builder", staticmethod(lambda: FakeBuilder(app)))
    monkeypatch.setattr(archive, "PACE_SECONDS", 0)
    monkeypatch.setattr(air_source, "BACKFILL_PACE", 0)
    monkeypatch.setattr(air_source, "BACKFILL_EMPTY_STOP", 3)

    async def broken(self, fresh=True):
        raise RuntimeError("the network dropped")
    monkeypatch.setattr(ecobot.Ecowitt, "warm", broken)
    monkeypatch.setattr(ecobot.AirGradient, "warm", broken)

    async def stop_when_archived():
        for _ in range(300):
            if " req, held " in caplog.text and "AirGradient archive:" in caplog.text:
                break
            await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)
    stopper = asyncio.create_task(stop_when_archived())
    await ecobot.main()
    await stopper
    log = caplog.text
    assert "Ecowitt failed" in log and "AirGradient failed" in log        # the summary says so
    assert "the network dropped" in log                                     # and the reason is in the log
    assert " req, held " in log and "AirGradient archive:" in log         # yet both archives ran
    assert "Startup warm-up failed" not in log                            # not lost to the catch-all
