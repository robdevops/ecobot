"""Runs ecobot.main() end to end against fake sources and a fake Telegram."""

import logging
import asyncio
import os
import signal
from types import SimpleNamespace as NS

import pytest

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
    cfg = config(tmp_path, log_width=120)
    monkeypatch.setattr(ecobot._handler.formatter, "width", 0)   # put back after the test
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
            if " req, held " in caplog.text and "days cached" in caplog.text:
                break
            await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)
    stopper = asyncio.create_task(stop_when_archived())
    await ecobot.main()
    await stopper
    log = caplog.text
    assert "Weather station: Ecowitt 'Fairleigh'" in log and "@testbot + " in log
    assert "Startup warm-up:" in log and " req to fetch" in log and "AirGradient archive:" in log
    final = next(line for line in log.splitlines() if "Ecowitt: " in line and " req, held " in line)
    assert "failed" not in final and "days (5m/30m/4h/1d)" in final
    assert len(app.handlers) == 10  # start+help, reset, keyboard, alerts, usage, the one button handler, other updates, membership, messages, errors
    assert "Alerts: rain, gusts, UV, temperature crossing, particulates: 0 chats" in log and "Alerts NOT running" not in log
    assert ecobot._handler.formatter.width == 120                   # LOG_WIDTH reaches the bot's own log lines


async def test_the_alerts_line_leaves_out_the_weather_alerts_when_they_could_not_start(tmp_path, monkeypatch, caplog):
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

    async def broken(self):
        raise RuntimeError("no readings")
    monkeypatch.setattr(ecobot.WeatherMonitor, "init", broken)

    async def stop_when_archived():
        for _ in range(300):
            if " req, held " in caplog.text and "days cached" in caplog.text:
                break
            await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)
    stopper = asyncio.create_task(stop_when_archived())
    await ecobot.main()
    await stopper
    log = caplog.text
    assert "Weather alerts couldn't start" in log and "Alerts: particulates: 0 chats" in log
    assert "Alerts NOT running: rain, gusts, UV, temperature crossing (see the error above)" in log


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
    off, _ = await ecobot.start_sources(config(tmp_path, airgradient_token="", airgradient_location=""))
    assert [s.name for s in off] == ["Ecowitt"]
    for s in off:
        await s.close()
    on, _ = await ecobot.start_sources(config(tmp_path, airgradient_token="", airgradient_location="", pollen=True, forecast=True))
    assert [s.name for s in on] == ["Ecowitt", "Pollen", "Forecast"]
    assert on[2].location == (-37.8, 145.0) and on[2].lines() and on[1].lines()
    for s in on:
        await s.close()


async def test_the_other_sources_run_in_the_weather_station_s_time_zone(tmp_path, monkeypatch, caplog):
    from zoneinfo import ZoneInfo
    eco_t, _ = ecowitt_transport(history_days=45)
    air_t, _ = air_transport(oldest=datetime.now(timezone.utc) - timedelta(days=5))
    for cls, transport in ((ecobot.Ecowitt, eco_t), (ecobot.AirGradient, air_t)):
        monkeypatch.setattr(cls, "__init__", lambda self, c, transport=None, orig=cls.__init__, t=transport: orig(self, c, t))
    monkeypatch.setenv("TZ", "UTC")
    sources, cfg = await ecobot.start_sources(config(tmp_path, tz=ZoneInfo("UTC")))   # TZ says UTC; the station is in Melbourne
    for s in sources:
        await s.close()
    assert [s.name for s in sources] == ["Ecowitt", "AirGradient"] and str(cfg.tz) == "Australia/Melbourne"
    assert all(str(s.tz) == "Australia/Melbourne" for s in sources)
    assert "TZ=UTC is not the weather station's time zone; using the station's, Australia/Melbourne" in caplog.text
    assert "has no name" not in caplog.text
    sources, cfg = await ecobot.start_sources(config(tmp_path, ecowitt_api_key="", tz=timezone(timedelta(hours=10))))
    for s in sources:
        await s.close()
    assert [s.name for s in sources] == ["AirGradient"] and cfg.tz == timezone(timedelta(hours=10))   # no station: the configured zone
    assert "The time zone has no name, only an offset (UTC+10:00)" in caplog.text


def test_the_time_zone_without_a_station_is_tz_else_the_one_etc_localtime_links_to(monkeypatch, caplog):
    from lib import config as config_module
    from lib.config import local_tz
    monkeypatch.setenv("TZ", "Australia/Perth")
    assert str(local_tz()) == "Australia/Perth"
    monkeypatch.setattr(config_module.os.path, "realpath", lambda p: "/usr/share/zoneinfo/Australia/Melbourne")
    monkeypatch.setenv("TZ", "Mars/Olympus")
    assert str(local_tz()) == "Australia/Melbourne" and "TZ: 'Mars/Olympus' is not a time zone name" in caplog.text
    monkeypatch.delenv("TZ")
    assert str(local_tz()) == "Australia/Melbourne"
    monkeypatch.setattr(config_module.os.path, "realpath", lambda p: "/etc/localtime")   # a copied file, not a link: no name
    assert not hasattr(local_tz(), "key")


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



def test_a_blank_setting_means_its_default_and_a_missing_key_says_which(monkeypatch, caplog):
    from lib.config import Config
    for k, v in (("TELEGRAM_BOT_TOKEN", " t "), ("XAI_API_KEY", "k"), ("ECOWITT_API_KEY", "a"), ("ECOWITT_APP_KEY", "b")):
        monkeypatch.setenv(k, v)
    for k in ("XAI_MODEL", "XAI_BASE_URL", "PLACE", "POLLEN_DISTRICT", "TUYA_BASE_URL"):
        monkeypatch.setenv(k, "  ")
    monkeypatch.setenv("POLLEN", "enabled")
    cfg = Config.from_env()
    assert (cfg.telegram_token, cfg.xai_model, cfg.xai_base_url, cfg.place, cfg.pollen_district, cfg.tuya_base_url) == (
        "t", "grok-4.3", "https://api.x.ai/v1", "Melbourne", "Central", "https://openapi.tuyaeu.com")
    assert cfg.pollen is False and "POLLEN: 'enabled' is not on or off; keeping the default (off)" in caplog.text
    monkeypatch.setenv("XAI_API_KEY", " ")
    with pytest.raises(SystemExit, match="XAI_API_KEY is not set"):
        Config.from_env()


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


async def test_main_with_the_irrigation_controller_starts_its_check_and_watchdog_and_closes_it(tmp_path, monkeypatch, caplog):
    import httpx
    from lib.irrigation import tuya as tuya_module
    caplog.set_level("INFO")
    cfg = config(tmp_path, tuya_device_id="dev1", tuya_client_id="cid", tuya_client_secret="s")
    eco_t, _ = ecowitt_transport(history_days=45)
    air_t, _ = air_transport(oldest=datetime.now(timezone.utc) - timedelta(days=5))
    closed = []

    def tuya(request):
        if request.url.path.startswith("/v1.0/token"):
            return httpx.Response(200, json={"success": True, "result": {"access_token": "t", "expire_time": 7200}})
        return httpx.Response(200, json={"success": True, "result": [{"code": "battery_percentage", "value": 80}]})
    app = FakeApp()
    monkeypatch.setattr(ecobot.Config, "from_env", classmethod(lambda cls: cfg))
    for cls, transport in ((ecobot.Ecowitt, eco_t), (ecobot.AirGradient, air_t)):
        monkeypatch.setattr(cls, "__init__", lambda self, c, transport=None, orig=cls.__init__, t=transport: orig(self, c, t))
    monkeypatch.setattr(tuya_module.Tuya, "__init__", lambda self, c, transport=None, orig=tuya_module.Tuya.__init__: orig(self, c, httpx.MockTransport(tuya)))
    monkeypatch.setattr(tuya_module.Tuya, "close", lambda self, orig=tuya_module.Tuya.close: closed.append(1) or orig(self))
    monkeypatch.setattr(ecobot.Application, "builder", staticmethod(lambda: FakeBuilder(app)))
    monkeypatch.setattr(archive, "PACE_SECONDS", 0)
    monkeypatch.setattr(air_source, "BACKFILL_PACE", 0)
    monkeypatch.setattr(air_source, "BACKFILL_EMPTY_STOP", 3)

    async def stop_when_archived():
        for _ in range(300):
            if " req, held " in caplog.text and "days cached" in caplog.text:
                break
            await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)
    stopper = asyncio.create_task(stop_when_archived())
    await ecobot.main()
    await stopper
    assert "irrigation battery" in caplog.text                       # in the list of alert types
    assert len(app.handlers) == 10 and closed == [1]                 # the same handlers (buttons share one entry point), and the client closed at shutdown


def test_log_lines_do_not_name_their_module():
    fmt = ecobot.LogFormat("%(levelname)s %(message)s")
    record = lambda name: logging.LogRecord(name, logging.INFO, __file__, 1, "hello", None, None)
    assert fmt.format(record("ecobot")) == "INFO hello" and fmt.format(record("lib.bot")) == "INFO hello"


def test_every_log_line_is_cut_to_the_log_width_and_a_quote_is_put_on_one_line():
    from lib.logs import QUOTES, LogFormat
    fmt = LogFormat("%(levelname)s %(message)s", width=40)

    def line(msg, *args, quotes=True):
        record = logging.LogRecord("lib.bot", logging.INFO, __file__, 1, msg, args, None)
        if quotes:
            record.__dict__.update(QUOTES)
        return fmt.format(record)
    asked = "what was the hottest it got last week, and when?"
    assert line("[@rob] %s", "how hot\nis  it") == "INFO [@rob] how hot is it"        # short enough: whole, on one line
    assert line("[@rob] %s", asked) == "INFO [@rob] what was the hottest it got…"               # 40 characters, the ellipsis included
    timings = "Chart ms: data 12, calc 6, draw 112, PNG 41, send 9, more words"
    assert line(timings, quotes=False) == "INFO Chart ms: data 12, calc 6, draw 11…"      # a line that quotes nothing is cut too
    assert line("a\n    b " + "x" * 50, quotes=False).split("\n")[1] == "    b " + "x" * 33 + "…"   # each line of a long message
    for level, name in ((logging.WARNING, "WARN"), (logging.ERROR, "ERROR")):   # a warning or an error is cut too
        record = logging.LogRecord("lib.bot", level, __file__, 1, timings, None, None)
        assert fmt.format(record) == f"{name} " + timings[:40 - len(name) - 2] + "…" and record.levelname == logging.getLevelName(level)
    fmt.width = 0
    assert line("[@rob] %s", asked) == "INFO [@rob] " + asked                           # 0 cuts none
    assert line(timings, quotes=False) == "INFO " + timings


def test_a_traceback_is_logged_whole_after_its_cut_message():
    import sys
    from lib.logs import LogFormat
    fmt = LogFormat("%(levelname)s %(message)s", width=40)
    try:
        {}["a key whose name is far longer than the forty characters of the log width"]
    except KeyError:
        record = logging.LogRecord("lib.bot", logging.ERROR, __file__, 1, "Failed to deliver reply to chat 12345678 in the group", None,
                                   sys.exc_info())
    lines = fmt.format(record).split("\n")
    assert lines[0] == "ERROR Failed to deliver reply to chat 1…" and lines[1] == "Traceback (most recent call last):"
    assert lines[-1] == "KeyError: 'a key whose name is far longer than the forty characters of the log width'"
    assert any(len(line) > 40 for line in lines[2:-1])                  # the frames too, whole


def test_the_log_width_is_71_unless_set_and_0_cuts_none(monkeypatch):
    from lib.config import Config
    for k, v in (("TELEGRAM_BOT_TOKEN", "t"), ("XAI_API_KEY", "k"), ("ECOWITT_API_KEY", "a"), ("ECOWITT_APP_KEY", "b")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LOG_WIDTH", raising=False)
    assert Config.from_env().log_width == 71
    for value, want in (("120", 120), ("0", 0), ("junk", 71), ("-5", 71), ("7.5", 71), ("²", 71)):
        monkeypatch.setenv("LOG_WIDTH", value)
        assert Config.from_env().log_width == want, value


def test_the_preload_chat_is_a_chat_id_or_off(monkeypatch):
    from lib.config import Config
    for k, v in (("TELEGRAM_BOT_TOKEN", "t"), ("XAI_API_KEY", "k"), ("ECOWITT_API_KEY", "a"), ("ECOWITT_APP_KEY", "b")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TELEGRAM_PRELOAD_CHAT", raising=False)
    assert Config.from_env().telegram_preload_chat is None
    for value, want in (("-1001234567890", -1001234567890), ("42", 42), ("", None), ("junk", None)):
        monkeypatch.setenv("TELEGRAM_PRELOAD_CHAT", value)
        assert Config.from_env().telegram_preload_chat == want, value


def test_the_http_clients_lines_do_not_start_with_http_request():
    from lib.logs import LogFormat
    fmt = LogFormat("%(levelname)s %(message)s", width=80)
    record = logging.LogRecord("httpx2", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"',
                               ("POST", "https://api.x.ai/v1/chat/completions", "HTTP/1.1", 200, "OK"), None)
    assert fmt.format(record) == 'INFO POST https://api.x.ai/v1/chat/completions "HTTP/1.1 200 OK"'
    assert record.msg.startswith("HTTP Request: ")                    # the record itself is left as it was
