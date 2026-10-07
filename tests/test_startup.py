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
    assert "Weather station: Ecowitt 'Fairleigh'" in log and "@testbot + " in log
    assert "Startup warm-up:" in log and " req to fetch" in log and "AirGradient archive:" in log
    final = next(line for line in log.splitlines() if "Ecowitt archive:" in line and " req, held " in line)
    assert "failed" not in final and "days (5min/30min/4h/1d)" in final
    assert len(app.handlers) == 10  # start+help, reset, keyboard, alerts, alert buttons, period buttons, other updates, membership, messages, errors


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


async def test_pollen_and_forecast_start_only_when_switched_on_and_the_forecast_uses_the_stations_location(tmp_path, monkeypatch):
    from tests.test_forecast import transport as forecast_transport
    from tests.test_pollen import page, transport as pollen_transport
    eco_t, _ = ecowitt_transport(history_days=45)
    calls = []
    eco_init, pollen_init, forecast_init = ecobot.Ecowitt.__init__, ecobot.Pollen.__init__, ecobot.Forecast.__init__
    monkeypatch.setattr(ecobot.Ecowitt, "__init__", lambda self, c: eco_init(self, c, eco_t))
    monkeypatch.setattr(ecobot.Pollen, "__init__", lambda self, c: pollen_init(self, c, pollen_transport(page, calls)))
    monkeypatch.setattr(ecobot.Forecast, "__init__", lambda self, c, place=None: forecast_init(self, c, place, forecast_transport(calls)))
    monkeypatch.setattr(ecobot.Pollen, "now", lambda self: datetime(2026, 11, 10, 12, 0))     # in the pollen season, whatever day the test runs
    off = await ecobot.start_sources(config(tmp_path, airgradient_token="", airgradient_location=""))
    assert [s.name for s in off] == ["Ecowitt"]
    for s in off:
        await s.close()
    on = await ecobot.start_sources(config(tmp_path, airgradient_token="", airgradient_location="", pollen=True, forecast=True))
    assert [s.name for s in on] == ["Ecowitt", "Pollen", "Forecast"]
    assert on[2].location == (-37.8, 145.0) and on[2].lines() and on[1].lines()
    for s in on:
        await s.close()


def test_the_alert_cooldown_is_configurable_with_a_default_and_limits(monkeypatch):
    from lib.config import Config
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("XAI_API_KEY", "k")
    monkeypatch.setenv("ECOWITT_API_KEY", "a")
    monkeypatch.setenv("ECOWITT_APP_KEY", "b")
    monkeypatch.delenv("ALERT_COOLDOWN_MINUTES", raising=False)
    assert Config.from_env().alert_cooldown_minutes == 30
    for value, want in (("45", 45), ("1", 5), ("999", 150), ("junk", 30)):
        monkeypatch.setenv("ALERT_COOLDOWN_MINUTES", value)
        assert Config.from_env().alert_cooldown_minutes == want



def test_the_rain_quiet_hours_are_configurable():
    from lib.config import _hours
    assert _hours("", (0, 6)) == (0, 6) and _hours("22-6", (0, 6)) == (22, 6) and _hours("off", (0, 6)) is None
    assert _hours("junk", (0, 6)) == (0, 6) and _hours("6-6", (0, 6)) == (0, 6) and _hours("0-25", (0, 6)) == (0, 6)


def test_feels_like_in_chart_all_is_off_by_default_and_a_config_option_turns_it_on(monkeypatch):
    from lib.config import Config
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("XAI_API_KEY", "k")
    monkeypatch.setenv("ECOWITT_API_KEY", "a")
    monkeypatch.setenv("ECOWITT_APP_KEY", "b")
    monkeypatch.delenv("CHART_ALL_FEELS_LIKE", raising=False)
    assert Config.from_env().chart_all_feels_like is False
    monkeypatch.setenv("CHART_ALL_FEELS_LIKE", "on")
    assert Config.from_env().chart_all_feels_like is True


def test_vpd_in_chart_all_is_off_by_default_and_a_config_option_turns_it_on(monkeypatch):
    from lib.config import Config
    for k, v in (("TELEGRAM_BOT_TOKEN", "t"), ("XAI_API_KEY", "k"), ("ECOWITT_API_KEY", "a"), ("ECOWITT_APP_KEY", "b")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CHART_ALL_VPD", raising=False)
    assert Config.from_env().chart_all_vpd is False
    monkeypatch.setenv("CHART_ALL_VPD", "on")
    assert Config.from_env().chart_all_vpd is True
