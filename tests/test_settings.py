"""The settings' names (docs/configuration.md): ecobot-mcp's start with MCP_, the bot's with BOT_, the shared ones have neither; an old
name still works when the new one is unset, with one warning naming the new name (never the value). And the token: AUTH_TOKENS, or the
one ecobot-mcp generates in the data folder, which the bot reads from there (lib/auth_token.py)."""

import logging
import os
import stat
from dataclasses import fields

import pytest

import ecobot
from ecobot_mcp import __main__ as mcp_main
from lib import auth_token
from lib.config import Config, env
from tests.fakes import config

SHARED = {"DARKMODE", "LOG_LEVEL", "AUTH_TOKENS"}
MCP_ONLY = {"ecowitt_api_key", "ecowitt_app_key", "ecowitt_source", "airgradient_token", "airgradient_location", "airgradient_dashboard",
            "airgradient_local_url", "airgradient_source", "homeassistant_url", "homeassistant_token", "place", "pollen", "pollen_district",
            "forecast", "forecast_lat", "forecast_lon", "rain_quiet_hours", "alert_cooldown_minutes", "antispike_require_consecutive",
            "chart_all_feels_like", "chart_all_vpd", "mcp_bind", "mcp_allowed_hosts"}


def test_every_setting_is_named_for_the_program_that_reads_it():
    for f in fields(Config):
        name = f.metadata.get("env")
        if not name or name == "WEATHER_MCP_TOKEN":   # (retired)
            continue
        if name in SHARED:
            assert not name.startswith(("MCP_", "BOT_")), name
        elif f.name in MCP_ONLY:
            assert name.startswith("MCP_"), name
        else:
            assert name.startswith("BOT_"), name


def bot_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOT_TELEGRAM_TOKEN", "1:x")
    monkeypatch.setenv("MCP_ECOWITT_API_KEY", "a")
    monkeypatch.setenv("MCP_ECOWITT_APP_KEY", "b")


OLD_AND_NEW = [("ECOWITT_API_KEY", "MCP_ECOWITT_API_KEY", "ecowitt_api_key", "k3y", "k3y"),
               ("HOMEASSISTANT_URL", "MCP_HOMEASSISTANT_URL", "homeassistant_url", "http://ha:8123/", "http://ha:8123"),
               ("PLACE", "MCP_PLACE", "place", "Hobart", "Hobart"),
               ("RAIN_QUIET_HOURS", "MCP_RAIN_QUIET_HOURS", "rain_quiet_hours", "22-6", (22, 6)),
               ("CHART_ALL_VPD", "MCP_CHART_ALL_VPD", "chart_all_vpd", "on", True),
               ("MCP_SERVICE_BIND", "MCP_BIND", "mcp_bind", "0.0.0.0:9000", ("0.0.0.0", 9000)),
               ("MCP_SERVICE_ALLOWED_HOSTS", "MCP_ALLOWED_HOSTS", "mcp_allowed_hosts", "weather.lan", ("weather.lan",)),
               ("MCP_SERVICE_TOKENS", "AUTH_TOKENS", "auth_tokens", "s3cret,other", ("s3cret", "other")),
               ("OPENROUTER_API_KEY", "BOT_OPENROUTER_API_KEY", "openrouter_api_key", "sk-s3cret", "sk-s3cret"),
               ("OPENROUTER_MODEL", "BOT_OPENROUTER_MODEL", "openrouter_model", "a/model", "a/model"),
               ("ADMIN_ONLY", "BOT_ADMIN_ONLY", "admin_only", "on", True),
               ("ADMIN_CHAT_IDS", "BOT_ADMIN_CHAT_IDS", "admin_chat_ids", "5 -9", frozenset({5, -9})),
               ("LOG_WIDTH", "BOT_LOG_WIDTH", "log_width", "80", 80),
               ("WEATHER_MCP_URL", "BOT_MCP_URL", "mcp_url", "http://ecobot-mcp:8765/mcp", "http://ecobot-mcp:8765/mcp"),
               ("TUYA_CLIENT_SECRET", "BOT_TUYA_CLIENT_SECRET", "tuya_client_secret", "tuya-s3cret", "tuya-s3cret"),
               ("IRRIGATION_CHECK_HOUR", "BOT_IRRIGATION_CHECK_HOUR", "irrigation_check_hour", "7", 7),
               ("TELEGRAM_PRELOAD_CHAT", "BOT_TELEGRAM_PRELOAD_CHAT", "telegram_preload_chat", "-100", -100)]


@pytest.mark.parametrize("old, new, attr, text, want", OLD_AND_NEW, ids=[row[0] for row in OLD_AND_NEW])
def test_a_setting_is_read_by_its_new_name_or_its_old_one_with_a_warning(monkeypatch, tmp_path, caplog, old, new, attr, text, want):
    bot_settings(monkeypatch, tmp_path)
    monkeypatch.setenv(new, text)
    caplog.clear()
    assert getattr(Config.from_env(), attr) == want and "Old setting names" not in caplog.text
    monkeypatch.delenv(new)
    monkeypatch.setenv(old, text)
    caplog.clear()
    assert getattr(Config.from_env(), attr) == want
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "Old setting names" in r.getMessage()]
    assert len(warnings) == 1 and f"{old} -> {new}" in warnings[0] and (len(text) < 5 or text not in warnings[0])   # (never the value)
    assert env(new) == text.strip()                                     # (the scripts' way in, old names too)


def test_the_old_names_are_one_warning_and_a_new_name_wins_over_its_old_one(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for old, value in (("TELEGRAM_BOT_TOKEN", "1:s3cret"), ("ECOWITT_API_KEY", "a"), ("ECOWITT_APP_KEY", "b")):
        monkeypatch.setenv(old, value)
    monkeypatch.setenv("BOT_LOG_WIDTH", "30")
    monkeypatch.setenv("LOG_WIDTH", "99")
    cfg = Config.from_env()
    assert cfg.telegram_token == "1:s3cret" and cfg.ecowitt and cfg.log_width == 30
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    renamed = [w for w in warnings if "Old setting names" in w]
    assert len(renamed) == 1 and "TELEGRAM_BOT_TOKEN -> BOT_TELEGRAM_TOKEN" in renamed[0] and "ECOWITT_APP_KEY -> MCP_ECOWITT_APP_KEY" in renamed[0]
    assert any("Ignoring LOG_WIDTH" in w for w in warnings) and "s3cret" not in caplog.text and "99" not in caplog.text


def test_a_value_an_old_name_gives_is_checked_under_that_name(monkeypatch, tmp_path, caplog):
    bot_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("ADMIN_ONLY", "ture")
    assert Config.from_env().admin_only is False and "ADMIN_ONLY: 'ture' is not on or off" in caplog.text


# ---------- the token ----------
def test_ecobot_mcp_generates_a_token_once_keeps_it_private_and_says_where_never_what(tmp_path, caplog):
    caplog.set_level("INFO")
    cfg = config(tmp_path)
    token = mcp_main.generated_token(cfg)
    kept = tmp_path / "auth_token"
    assert len(token) >= 40 and kept.read_text().strip() == token
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert mcp_main.generated_token(cfg) == token                     # used again at the next start
    assert str(kept) in caplog.text and token not in caplog.text
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".auth_token")] == []   # (no file of its own left behind)


def test_an_empty_token_file_is_replaced_and_an_unwritable_folder_stops_ecobot_mcp_saying_why(tmp_path):
    (tmp_path / "auth_token").write_text("\n")
    token = auth_token.ensure(tmp_path)
    assert token and auth_token.read(tmp_path) == token
    if os.geteuid() != 0:   # (root writes anywhere)
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o500)
        with pytest.raises(SystemExit, match="AUTH_TOKENS is not set"):
            mcp_main.generated_token(config(locked))


def test_the_bot_sends_the_first_of_auth_tokens_else_the_retired_one_else_the_generated_one(tmp_path, caplog):
    assert ecobot.mcp_token(config(tmp_path, auth_tokens=("first", "second"))) == "first"
    assert ecobot.mcp_token(config(tmp_path, auth_tokens=("first",), retired_mcp_token="old")) == "first"
    assert "WEATHER_MCP_TOKEN is retired and ignored" in caplog.text
    caplog.clear()
    assert ecobot.mcp_token(config(tmp_path, retired_mcp_token="r3tired")) == "r3tired"
    assert "move it into AUTH_TOKENS" in caplog.text and "r3tired" not in caplog.text
    caplog.clear()
    read = ecobot.mcp_token(config(tmp_path))                          # ecobot-mcp hasn't written its token yet: said, and read again
    assert callable(read) and read() is None and f"there is no token at {tmp_path / 'auth_token'}" in caplog.text
    token = auth_token.ensure(tmp_path)
    assert read() == token
    caplog.clear()
    assert ecobot.mcp_token(config(tmp_path))() == token and "no token" not in caplog.text
