"""Runs envirobot.main() end to end against fake sources and a fake Telegram."""

import asyncio
import os
import signal
from types import SimpleNamespace as NS

import envirobot
from lib.ecowitt import archive
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

    def add_handler(self, h):
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
    eco_t, _ = ecowitt_transport()
    air_t, _ = air_transport()
    app = FakeApp()
    monkeypatch.setattr(envirobot.Config, "from_env", classmethod(lambda cls: cfg))
    for cls, transport in ((envirobot.Ecowitt, eco_t), (envirobot.AirGradient, air_t)):
        monkeypatch.setattr(cls, "__init__", lambda self, c, transport=None, orig=cls.__init__, t=transport: orig(self, c, t))
    monkeypatch.setattr(envirobot.Application, "builder", staticmethod(lambda: FakeBuilder(app)))
    monkeypatch.setattr(archive, "PACE_SECONDS", 0)
    asyncio.get_running_loop().call_later(6, os.kill, os.getpid(), signal.SIGTERM)
    await envirobot.main()
    log = caplog.text
    assert "Weather station: Ecowitt 'Fairleigh'" in log and "Bot @testbot running" in log
    assert "Startup warm-up:" in log and "5-min archive +" in log and "AirGradient" in log
    assert len(app.handlers) == 5
