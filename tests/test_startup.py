"""Runs ecobot-mcp's main() (python -m ecobot_mcp) end to end over the fake sources, which runs them, their archives and the alert
rules; and the settings' reading (lib/config.py)."""

import logging
import asyncio
import os
import signal

import pytest

import ecobot_mcp.__main__ as service_main
from ecobot_mcp import core
from lib.config import Config
from lib.ecowitt import archive
from datetime import datetime, timedelta, timezone

from lib.airgradient import source as air_source
from tests.fakes import TZ, air_transport, config, ecowitt_transport
from tests.test_mcp_listen import free_port

TOKEN = "s3cret"


@pytest.fixture(autouse=True)
def the_log_as_it_was():
    """main() sets the log levels from the settings: put back after each test here, for the tests that come after."""
    levels = {name: logging.getLogger(name).level for name in ("ecobot", "lib")}
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture(autouse=True)
def quick_shutdown(monkeypatch):
    """A listen stream still open when ecobot-mcp stops is ended after a quarter of a second, not SHUTDOWN_SECONDS (3)."""
    monkeypatch.setattr(service_main, "SHUTDOWN_SECONDS", 0.25)


# ---------- ecobot-mcp (python -m ecobot_mcp): the sources, their archives, the alert rules ----------
def ecobot_mcp_settings(tmp_path, monkeypatch, history_days=10, **over) -> Config:
    """ecobot-mcp's settings (the fake station and air sensor, a free port), its sources' fakes plugged in, the archives unpaced."""
    cfg = config(tmp_path, auth_tokens=(TOKEN,), mcp_bind=("127.0.0.1", free_port()), **over)
    eco_t, _ = ecowitt_transport(history_days=history_days)
    air_t, _ = air_transport(oldest=datetime.now(timezone.utc) - timedelta(days=5))
    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls, program="mcp": cfg))
    for cls, transport in ((core.Ecowitt, eco_t), (core.AirGradient, air_t)):
        monkeypatch.setattr(cls, "__init__", lambda self, c, transport=None, orig=cls.__init__, t=transport, **kw: orig(self, c, t, **kw))
    monkeypatch.setattr(archive, "PACE_SECONDS", 0)
    monkeypatch.setattr(air_source, "BACKFILL_PACE", 0)
    monkeypatch.setattr(air_source, "BACKFILL_EMPTY_STOP", 3)
    return cfg


async def run_ecobot_mcp_until_archived(caplog):
    """ecobot-mcp's main() until both background archives have finished, then stopped (SIGTERM)."""
    async def stop_when_archived():
        for _ in range(300):
            if any("Backfill Ecowitt: " in l and " fetched/failed, " in l for l in caplog.text.splitlines()) and "Backfill AirGradient:" in caplog.text:
                break
            await asyncio.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)
    stopper = asyncio.create_task(stop_when_archived())
    await service_main.main()
    await stopper


async def test_ecobot_mcp_starts_warms_archives_and_shuts_down_cleanly(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO")
    ecobot_mcp_settings(tmp_path, monkeypatch, history_days=30)
    await run_ecobot_mcp_until_archived(caplog)
    log = caplog.text
    assert "Ecowitt: 'Fairleigh' (" in log and "): cloud (" in log and "MCP: http://127.0.0.1:" in log
    assert not any(line.startswith(("Sources:", "Refreshing every")) or "Alerts: " in line and " chats" in line for line in log.splitlines())
    assert "Startup fetch:" not in log and " requests to fetch" in log and "Backfill AirGradient:" in log
    final = next(line for line in log.splitlines() if "Backfill Ecowitt: " in line and " fetched/failed, " in line)
    assert "/0 fetched/failed, " in final and "d held (5m/30m/4h/1d)" in final
    assert "Alerts NOT running" not in log and "Telegram" not in log


async def test_the_stations_live_reading_is_asked_for_first_after_start_ahead_of_the_history(tmp_path, monkeypatch, caplog):
    """The dashboard's tiles and the first "weather now" wait on it: it goes before the warm-up's history requests (a second or more
    apart), not after them."""
    eco_t, fake = ecowitt_transport(history_days=10)
    eco_init = core.Ecowitt.__init__
    ecobot_mcp_settings(tmp_path, monkeypatch)
    monkeypatch.setattr(core.Ecowitt, "__init__", lambda self, c, transport=None, **kw: eco_init(self, c, eco_t, **kw))
    await run_ecobot_mcp_until_archived(caplog)
    paths = [call.get("path") for call in fake.calls]
    assert paths[:3] == ["list", "real_time", "history"] and "Live reading at start failed" not in caplog.text


async def test_the_weather_alerts_that_could_not_start_are_named_as_not_running(tmp_path, monkeypatch, caplog):
    from lib.alerts.weather import WeatherMonitor
    caplog.set_level("INFO")
    ecobot_mcp_settings(tmp_path, monkeypatch)

    async def broken(self):
        raise RuntimeError("no readings")
    monkeypatch.setattr(WeatherMonitor, "init", broken)
    await run_ecobot_mcp_until_archived(caplog)
    log = caplog.text
    assert "Weather alerts couldn't start" in log
    assert "Alerts NOT running: rain, gusts, UV, temperature crossing (see the error above)" in log


async def test_a_failed_first_refresh_does_not_stop_the_alerts_or_the_archives(tmp_path, monkeypatch, caplog):
    """If the first Ecowitt or AirGradient refresh raises, the archive and the backfill (and the alert
    monitor) must still start: the regular refreshes then catch up."""
    caplog.set_level("INFO")
    ecobot_mcp_settings(tmp_path, monkeypatch)

    async def broken(self, fresh=True):
        raise RuntimeError("the network dropped")
    monkeypatch.setattr(core.Ecowitt, "warm", broken)
    monkeypatch.setattr(core.AirGradient, "warm", broken)
    await run_ecobot_mcp_until_archived(caplog)
    log = caplog.text
    assert "the network dropped" in log                                     # the reason is in the log
    assert "Backfill Ecowitt: " in log and "Backfill AirGradient:" in log         # yet both archives ran
    assert "Startup fetch failed" not in log                            # not lost to the catch-all


async def test_a_source_that_cannot_even_be_made_is_left_out_and_the_others_start(tmp_path, monkeypatch, caplog):
    eco_t, _ = ecowitt_transport(history_days=10)
    monkeypatch.setattr(core.Ecowitt, "__init__", lambda self, c, orig=core.Ecowitt.__init__, **kw: orig(self, c, eco_t, **kw))

    def broken(self, cfg, transport=None, **kw):
        raise OSError("the cache folder is read-only")
    monkeypatch.setattr(core.AirGradient, "__init__", broken)
    sources, _ = await core.start_sources(config(tmp_path))
    for s in sources:
        await s.close()
    assert [s.name for s in sources] == ["Ecowitt"] and "AirGradient couldn't start (OSError: the cache folder is read-only) - continuing without it" in caplog.text


async def test_pollen_and_forecast_start_only_when_switched_on_from_their_last_fetch_and_the_warm_up_fetches(tmp_path, monkeypatch, caplog):
    """They start from what the file kept (nothing asked before the port opens); the warm-up fetches them when that is due again, and
    logs their lines anew. The forecast uses the station's location."""
    import contextlib
    from lib.warm import Tasks
    caplog.set_level("INFO")
    from lib.forecast import Forecast
    from lib.pollen import Pollen
    from tests.test_forecast import transport as forecast_transport
    from tests.test_pollen import page, transport as pollen_transport
    eco_t, _ = ecowitt_transport(history_days=10)
    calls = []
    eco_init, pollen_init, forecast_init = core.Ecowitt.__init__, Pollen.__init__, Forecast.__init__
    monkeypatch.setattr(core.Ecowitt, "__init__", lambda self, c, **kw: eco_init(self, c, eco_t, **kw))
    monkeypatch.setattr(Pollen, "__init__", lambda self, c: pollen_init(self, c, pollen_transport(page, calls)))
    monkeypatch.setattr(Forecast, "__init__", lambda self, c, place=None: forecast_init(self, c, place, forecast_transport(calls)))
    monkeypatch.setattr(Pollen, "now", lambda self: datetime(2026, 11, 10, 12, 0))     # in the pollen season, whatever day the test runs
    off, _ = await core.start_sources(config(tmp_path, airgradient_token="", airgradient_location=""))
    assert [s.name for s in off] == ["Ecowitt"]
    for s in off:
        await s.close()
    cfg = config(tmp_path, airgradient_token="", airgradient_location="", pollen=True, forecast=True)
    on, cfg = await core.start_sources(cfg)
    assert [s.name for s in on] == ["Ecowitt", "Pollen", "Forecast"]
    assert on[2].location is None and calls == [] and not on[2].lines() and not on[1].lines()   # nothing kept yet, nothing asked
    await on[0].refresh_device()                                         # (the warm-up's: the station's device list gives its place)
    assert on[2].location == (-37.8, 145.0)
    assert [s.summary for s in on[1:]] == ["Melbourne Pollen: no page of this season kept yet (1800s)", "Open-Meteo: none kept yet (every 900s)"]
    async with contextlib.AsyncExitStack() as stack:
        tasks = Tasks()
        warm = await core.Core.open(cfg, on, stack, tasks)
        stack.push_async_callback(tasks.close)
        await warm.warm_up([])
    assert on[2].lines() and on[1].lines() and calls                     # the page, the forecast and its ensemble
    assert "Open-Meteo: 3 days (every 900s)" in caplog.text and "Melbourne Pollen: Grass pollen" in caplog.text
    assert {"Forecast refresh", "Pollen refresh"} <= set(warm.health.loops)
    for s in on:
        await s.close()
    calls.clear()
    again, _ = await core.start_sources(cfg)                             # a restart: from the last fetch, nothing asked
    assert again[2].lines() and again[1].lines() and calls == [] and again[2].summary == "Open-Meteo: 3 days (every 900s)"
    for s in again:
        await s.close()


async def test_the_other_sources_run_in_the_weather_station_s_time_zone(tmp_path, monkeypatch, caplog):
    from zoneinfo import ZoneInfo
    eco_t, _ = ecowitt_transport(history_days=10)
    air_t, _ = air_transport(oldest=datetime.now(timezone.utc) - timedelta(days=5))
    for cls, transport in ((core.Ecowitt, eco_t), (core.AirGradient, air_t)):
        monkeypatch.setattr(cls, "__init__", lambda self, c, transport=None, orig=cls.__init__, t=transport, **kw: orig(self, c, t, **kw))
    monkeypatch.setenv("TZ", "UTC")
    caplog.set_level("INFO")
    sources, cfg = await core.start_sources(config(tmp_path, tz=ZoneInfo("UTC")))   # TZ says UTC; the station is in Melbourne
    assert str(cfg.tz) == "UTC" and not sources[0].mac   # a first start: the station is found after the port opens (its device list)
    await sources[0].refresh_device()
    assert sources[0].mac and str(sources[0].tz) == "UTC"   # ... this run keeps the configured zone, the next takes the station's
    assert "the station's time zone is Australia/Melbourne; this run keeps UTC (the configured one)" in caplog.text
    for s in sources:
        await s.close()
    sources, cfg = await core.start_sources(config(tmp_path, tz=ZoneInfo("UTC")))   # the restart: from the device kept in the file
    for s in sources:
        await s.close()
    assert [s.name for s in sources] == ["Ecowitt", "AirGradient"] and str(cfg.tz) == "Australia/Melbourne"
    assert all(str(s.tz) == "Australia/Melbourne" for s in sources)
    assert "TZ=UTC is not the weather station's time zone; using the station's, Australia/Melbourne" in caplog.text
    assert "has no name" not in caplog.text
    sources, cfg = await core.start_sources(config(tmp_path, ecowitt_api_key="", tz=timezone(timedelta(hours=10))))
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
    monkeypatch.setenv("MCP_ECOWITT_API_KEY", "a")
    monkeypatch.setenv("MCP_ECOWITT_APP_KEY", "b")
    monkeypatch.delenv("MCP_ALERT_COOLDOWN_MINUTES", raising=False)
    assert Config.from_env().alert_cooldown_minutes == 60   # (ecobot-mcp's: the alert rules are its)
    for value, want in (("45", 45), ("1", 5), ("0", 5), ("-3", 5), ("999", 150), ("junk", 60)):   # 0 is under 5 like 1 is
        monkeypatch.setenv("MCP_ALERT_COOLDOWN_MINUTES", value)
        assert Config.from_env().alert_cooldown_minutes == want, value


def test_the_log_level_is_a_setting_and_a_typo_keeps_info_with_a_warning(monkeypatch, caplog):
    from lib.config import Config
    for k, v in (("MCP_ECOWITT_API_KEY", "a"), ("MCP_ECOWITT_APP_KEY", "b")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert Config.from_env().log_level == "INFO"
    for value, want in (("debug", "DEBUG"), ("WARNING", "WARNING"), ("verbose", "INFO")):
        monkeypatch.setenv("LOG_LEVEL", value)
        assert Config.from_env().log_level == want, value
    assert "LOG_LEVEL: 'verbose' is not DEBUG, INFO, WARNING or ERROR; keeping INFO" in caplog.text



def test_a_blank_setting_means_its_default_and_a_missing_key_says_which(monkeypatch, caplog):
    from lib.config import Config
    for k, v in (("MCP_ECOWITT_API_KEY", " a "), ("MCP_ECOWITT_APP_KEY", "b")):
        monkeypatch.setenv(k, v)
    for k in ("PLACE", "MCP_POLLEN_DISTRICT", "IRRIGATION_TUYA_BASE_URL"):
        monkeypatch.setenv(k, "  ")
    monkeypatch.setenv("POLLEN", "enabled")
    service = Config.from_env()   # (ecobot-mcp's)
    for k, v in (("IRRIGATION_TUYA_DEVICE_ID", "d"), ("IRRIGATION_TUYA_CLIENT_ID", "c"), ("IRRIGATION_TUYA_CLIENT_SECRET", "s")):
        monkeypatch.setenv(k, v)
    irrigation = Config.from_env(program="irrigation")   # (irrigation-mcp's)
    assert (service.ecowitt_api_key, service.place, service.pollen_district, irrigation.tuya_base_url) == (
        "a", "Melbourne", "Central", "https://openapi.tuyaeu.com")
    assert service.pollen is False and "POLLEN: 'enabled' is not on or off; keeping the default (off)" in caplog.text
    for k in ("MCP_ECOWITT_API_KEY", "MCP_ECOWITT_APP_KEY"):
        monkeypatch.setenv(k, " ")
    with pytest.raises(SystemExit, match="Set MCP_ECOWITT_API_KEY"):   # (no source: it says which settings make one)
        Config.from_env()


def test_the_rain_quiet_hours_are_configurable():
    from lib.config import _hours
    hours = lambda text: _hours(text, (0, 6), "MCP_RAIN_QUIET_HOURS")
    assert hours("") == (0, 6) and hours("22-6") == (22, 6) and hours("off") is None
    assert hours("junk") == (0, 6) and hours("6-6") == (0, 6) and hours("0-25") == (0, 6)


def test_feels_like_in_chart_all_is_off_by_default_and_a_config_option_turns_it_on(monkeypatch):
    from lib.config import Config
    monkeypatch.setenv("BOT_TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("BOT_OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("MCP_ECOWITT_API_KEY", "a")
    monkeypatch.setenv("MCP_ECOWITT_APP_KEY", "b")
    monkeypatch.delenv("MCP_CHART_ALL_FEELS_LIKE", raising=False)
    assert Config.from_env().chart_all_feels_like is False   # (ecobot-mcp's: it draws the charts)
    monkeypatch.setenv("MCP_CHART_ALL_FEELS_LIKE", "on")
    assert Config.from_env().chart_all_feels_like is True


def test_vpd_in_chart_all_is_off_by_default_and_a_config_option_turns_it_on(monkeypatch):
    from lib.config import Config
    for k, v in (("BOT_TELEGRAM_TOKEN", "t"), ("BOT_OPENROUTER_API_KEY", "k"), ("BOT_MCP_URL", "http://127.0.0.1:8765/mcp"),
                 ("MCP_ECOWITT_API_KEY", "a"), ("MCP_ECOWITT_APP_KEY", "b")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MCP_CHART_ALL_VPD", raising=False)
    assert Config.from_env().chart_all_vpd is False
    monkeypatch.setenv("MCP_CHART_ALL_VPD", "on")
    assert Config.from_env().chart_all_vpd is True


def test_only_the_lines_that_are_not_ordinary_are_marked_with_their_level():
    from lib.logs import LogFormat
    fmt = LogFormat("%(levelname)s %(message)s")
    line = lambda level: fmt.format(logging.LogRecord("lib.bot", level, __file__, 1, "hello", None, None))
    assert [line(l) for l in (logging.INFO, logging.WARNING, logging.ERROR, logging.DEBUG)] == ["hello", "WARN hello", "ERROR hello", "DEBUG hello"]


def test_log_lines_do_not_name_their_module():
    from lib.logs import LogFormat
    fmt = LogFormat("%(levelname)s %(message)s")
    record = lambda name: logging.LogRecord(name, logging.INFO, __file__, 1, "hello", None, None)
    assert fmt.format(record("ecobot")) == "hello" and fmt.format(record("lib.bot")) == "hello"        # and no "INFO" before an ordinary line


def test_only_a_chat_message_is_cut_to_the_log_width_and_it_is_put_on_one_line():
    from lib.logs import QUOTES, LogFormat
    fmt = LogFormat("%(levelname)s %(message)s", width=20)

    def line(msg, *args, quotes=True):
        record = logging.LogRecord("lib.bot", logging.INFO, __file__, 1, msg, args, None)
        if quotes:
            record.__dict__.update(QUOTES)
        return fmt.format(record)
    asked = "what was the hottest it got last week, and when?"
    assert line("[@rob] %s", "how hot\nis  it") == "[@rob] how hot is it"        # short enough: whole, on one line
    assert line("[@rob] %s", asked) == "[@rob] " + asked[:20] + "…"                # 20 characters kept, then the ellipsis
    timings = "Chart ms: data 12, calc 6, draw 112, PNG 41, send 9, more words"
    assert line(timings, quotes=False) == timings                                  # a line that quotes nothing is never cut
    assert line("a\n    b " + "x" * 50, quotes=False).split("\n")[1] == "    b " + "x" * 50
    for level, name in ((logging.WARNING, "WARN"), (logging.ERROR, "ERROR")):   # nor is a warning or an error
        record = logging.LogRecord("lib.bot", level, __file__, 1, timings, None, None)
        assert fmt.format(record) == f"{name} " + timings and record.levelname == logging.getLevelName(level)
    fmt.width = 0
    assert line("[@rob] %s", asked) == "[@rob] " + asked                           # 0 keeps the whole message
    assert LogFormat("%(message)s").width == 24                                     # and the default is 24


def test_a_traceback_is_logged_whole_after_its_message():
    import sys
    from lib.logs import LogFormat
    fmt = LogFormat("%(levelname)s %(message)s", width=40)
    try:
        {}["a key whose name is far longer than the forty characters of the log width"]
    except KeyError:
        record = logging.LogRecord("lib.bot", logging.ERROR, __file__, 1, "Failed to deliver reply to chat 12345678 in the group", None,
                                   sys.exc_info())
    lines = fmt.format(record).split("\n")
    assert lines[0] == "ERROR Failed to deliver reply to chat 12345678 in the group" and lines[1] == "Traceback (most recent call last):"
    assert lines[-1] == "KeyError: 'a key whose name is far longer than the forty characters of the log width'"
    assert any(len(line) > 40 for line in lines[2:-1])                  # the frames too, whole


def test_the_http_clients_lines_do_not_start_with_http_request():
    from lib.logs import LogFormat
    fmt = LogFormat("%(levelname)s %(message)s", width=80)
    record = logging.LogRecord("httpx2", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"',
                               ("POST", "https://openrouter.ai/api/v1/chat/completions", "HTTP/1.1", 200, "OK"), None)
    assert fmt.format(record) == 'POST https://openrouter.ai/api/v1/chat/completions "HTTP/1.1 200 OK"'
    assert record.msg.startswith("HTTP Request: ")                    # the record itself is left as it was
