"""The settings' names (docs/configuration.md): ecobot-mcp's start with MCP_, irrigation-mcp's with IRRIGATION_, the
shared ones have neither; an old name (or either of two) still works when the new one is unset, with one warning naming the new name (never the value). And the token: AUTH_TOKENS, or the
one ecobot-mcp generates in the data folder (lib/auth_token.py)."""

import logging
import os
import stat
from dataclasses import fields

import pytest

from ecobot_mcp import __main__ as mcp_main
from lib import auth_token
from lib.config import Config, env
from tests.fakes import config

SHARED = {"DARKMODE", "LOG_LEVEL", "AUTH_TOKENS"}
MCP_ONLY = {"ecowitt_api_key", "ecowitt_app_key", "ecowitt_source", "airgradient_token", "airgradient_location", "airgradient_dashboard",
            "airgradient_local_url", "airgradient_source", "homeassistant_url", "homeassistant_token", "place", "pollen", "pollen_district",
            "forecast", "forecast_lat", "forecast_lon", "rain_quiet_hours", "alert_cooldown_minutes", "antispike_require_consecutive",
            "chart_all_feels_like", "chart_all_vpd", "mcp_bind", "mcp_allowed_hosts", "mcp_dashboard", "mcp_dashboard_animations",
            "mcp_dashboard_indoor",
            "mcp_chart_base_url", "mcp_chart_link_hours"}
IRRIGATION_ONLY = {"tuya_device_id", "tuya_client_id", "tuya_client_secret", "tuya_base_url", "irrigation_check_hour", "irrigation_bind",
                   "irrigation_allowed_hosts", "irrigation_ecobot_mcp_url"}


def test_every_setting_is_named_for_the_program_that_reads_it():
    for f in fields(Config):
        name = f.metadata.get("env")
        if not name:
            continue
        if name in SHARED:
            assert not name.startswith(("MCP_", "IRRIGATION_")), name
        elif f.name in IRRIGATION_ONLY:
            assert name.startswith("IRRIGATION_"), name
        elif f.name in MCP_ONLY:
            assert name.startswith("MCP_"), name
        else:
            raise AssertionError(f"{name}: not named for the program that reads it")


def both_programs_settings(monkeypatch, tmp_path):
    """What both programs need to start: a station (ecobot-mcp's) and the controller (irrigation-mcp's)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MCP_ECOWITT_API_KEY", "a")
    monkeypatch.setenv("MCP_ECOWITT_APP_KEY", "b")
    monkeypatch.setenv("IRRIGATION_TUYA_DEVICE_ID", "dev")   # (irrigation-mcp's: the three it needs)
    monkeypatch.setenv("IRRIGATION_TUYA_CLIENT_ID", "cid")
    monkeypatch.setenv("IRRIGATION_TUYA_CLIENT_SECRET", "sec")


def read_by(name: str) -> dict:
    """How the program that reads a setting reads its settings: ecobot-mcp's (MCP_) by ecobot-mcp, irrigation-mcp's (IRRIGATION_) by
    ecobot-mcp, the shared ones by either."""
    return {"program": "irrigation" if name.startswith("IRRIGATION_") else "mcp"}


OLD_AND_NEW = [("ECOWITT_API_KEY", "MCP_ECOWITT_API_KEY", "ecowitt_api_key", "k3y", "k3y"),
               ("HOMEASSISTANT_URL", "MCP_HOMEASSISTANT_URL", "homeassistant_url", "http://ha:8123/", "http://ha:8123"),
               ("PLACE", "MCP_PLACE", "place", "Hobart", "Hobart"),
               ("RAIN_QUIET_HOURS", "MCP_RAIN_QUIET_HOURS", "rain_quiet_hours", "22-6", (22, 6)),
               ("CHART_ALL_VPD", "MCP_CHART_ALL_VPD", "chart_all_vpd", "on", True),
               ("MCP_SERVICE_BIND", "MCP_BIND", "mcp_bind", "0.0.0.0:9000", ("0.0.0.0", 9000)),
               ("MCP_SERVICE_ALLOWED_HOSTS", "MCP_ALLOWED_HOSTS", "mcp_allowed_hosts", "weather.lan", ("weather.lan",)),
               ("MCP_SERVICE_TOKENS", "AUTH_TOKENS", "auth_tokens", "s3cret,other", ("s3cret", "other")),
               ("TUYA_CLIENT_SECRET", "IRRIGATION_TUYA_CLIENT_SECRET", "tuya_client_secret", "tuya-s3cret", "tuya-s3cret"),
               ("BOT_TUYA_CLIENT_SECRET", "IRRIGATION_TUYA_CLIENT_SECRET", "tuya_client_secret", "tuya-s3cret", "tuya-s3cret"),
               ("BOT_TUYA_BASE_URL", "IRRIGATION_TUYA_BASE_URL", "tuya_base_url", "https://openapi.tuyaus.com/", "https://openapi.tuyaus.com"),
               ("TUYA_BASE_URL", "IRRIGATION_TUYA_BASE_URL", "tuya_base_url", "https://openapi.tuyaus.com", "https://openapi.tuyaus.com"),
               ("BOT_IRRIGATION_CHECK_HOUR", "IRRIGATION_CHECK_HOUR", "irrigation_check_hour", "7", 7)]


@pytest.mark.parametrize("old, new, attr, text, want", OLD_AND_NEW, ids=[f"{row[0]}->{row[1]}" for row in OLD_AND_NEW])
def test_a_setting_is_read_by_its_new_name_or_its_old_one_with_a_warning(monkeypatch, tmp_path, caplog, old, new, attr, text, want):
    both_programs_settings(monkeypatch, tmp_path)
    monkeypatch.setenv(new, text)
    caplog.clear()
    assert getattr(Config.from_env(**read_by(new)), attr) == want and "Old setting names" not in caplog.text
    monkeypatch.delenv(new)
    monkeypatch.setenv(old, text)
    caplog.clear()
    assert getattr(Config.from_env(**read_by(new)), attr) == want
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "Old setting names" in r.getMessage()]
    assert len(warnings) == 1 and f"{old} -> {new}" in warnings[0] and (len(text) < 5 or text not in warnings[0])   # (never the value)
    assert env(new) == text.strip()                                     # (the scripts' way in, old names too)


def test_the_old_names_are_one_warning_and_a_new_name_wins_over_its_old_one(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for old, value in (("ECOWITT_API_KEY", "a"), ("ECOWITT_APP_KEY", "b"), ("PLACE", "Hobart"), ("MCP_PLACE", "Perth")):
        monkeypatch.setenv(old, value)
    cfg = Config.from_env()
    assert cfg.ecowitt and cfg.place == "Perth"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    renamed = [w for w in warnings if "Old setting names" in w]
    assert len(renamed) == 1 and "ECOWITT_APP_KEY -> MCP_ECOWITT_APP_KEY" in renamed[0] and "PLACE -> " not in renamed[0].replace("MCP_PLACE", "")
    assert any("Ignoring PLACE" in w for w in warnings) and "Hobart" not in caplog.text and "Perth" not in caplog.text


def test_of_two_old_names_the_newer_is_read_and_the_older_named_as_ignored(monkeypatch, tmp_path, caplog):
    """IRRIGATION_TUYA_CLIENT_SECRET was BOT_TUYA_CLIENT_SECRET, and before that TUYA_CLIENT_SECRET: either is still read, the newer
    when both are set; each set beside a newer one is named as ignored (never their values)."""
    both_programs_settings(monkeypatch, tmp_path)
    monkeypatch.delenv("IRRIGATION_TUYA_CLIENT_SECRET")
    monkeypatch.setenv("TUYA_CLIENT_SECRET", "oldest-s3cret")
    monkeypatch.setenv("BOT_TUYA_CLIENT_SECRET", "older-s3cret")
    caplog.clear()
    assert Config.from_env(program="irrigation").tuya_client_secret == "older-s3cret"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("BOT_TUYA_CLIENT_SECRET -> IRRIGATION_TUYA_CLIENT_SECRET" in w for w in warnings)
    assert any("Ignoring TUYA_CLIENT_SECRET: a newer name is set" in w for w in warnings) and "s3cret" not in caplog.text
    monkeypatch.setenv("IRRIGATION_TUYA_CLIENT_SECRET", "new-s3cret")
    caplog.clear()
    assert Config.from_env(program="irrigation").tuya_client_secret == "new-s3cret" and "Old setting names" not in caplog.text
    assert "Ignoring BOT_TUYA_CLIENT_SECRET, TUYA_CLIENT_SECRET: a newer name is set" in caplog.text


def test_irrigation_mcp_reads_only_its_own_settings_and_the_shared_ones_and_cannot_start_without_the_controller(monkeypatch, tmp_path,
                                                                                                                caplog):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for name, value in (("MCP_PLACE", "Hobart"), ("MCP_BIND", "0.0.0.0:9"), ("AUTH_TOKENS", "s3cret"), ("LOG_LEVEL", "debug"),
                        ("IRRIGATION_TUYA_DEVICE_ID", "dev")):
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match="irrigation-mcp needs the irrigation controller's Tuya settings") as stopped:
        Config.from_env(program="irrigation")
    assert "IRRIGATION_TUYA_CLIENT_ID, IRRIGATION_TUYA_CLIENT_SECRET not set" in str(stopped.value)
    monkeypatch.setenv("TUYA_CLIENT_ID", "cid")             # (an old name does)
    monkeypatch.setenv("BOT_TUYA_CLIENT_SECRET", "sec")
    monkeypatch.setenv("IRRIGATION_BIND", "0.0.0.0:9100")
    monkeypatch.setenv("IRRIGATION_ALLOWED_HOSTS", "irrigation.lan")
    monkeypatch.setenv("IRRIGATION_ECOBOT_MCP_URL", "http://ecobot-mcp:8765/mcp/")
    cfg = Config.from_env(program="irrigation")
    assert (cfg.tuya_device_id, cfg.tuya_client_id, cfg.tuya_client_secret) == ("dev", "cid", "sec")
    assert cfg.irrigation_bind == ("0.0.0.0", 9100) and cfg.irrigation_allowed_hosts == ("irrigation.lan",)
    assert cfg.irrigation_ecobot_mcp_url == "http://ecobot-mcp:8765/mcp" and cfg.irrigation_check_hour == 18
    assert cfg.auth_tokens == ("s3cret",) and cfg.log_level == "DEBUG"                     # the shared ones
    for f in fields(Config):
        if f.metadata.get("env", "").startswith("MCP_"):
            assert getattr(cfg, f.name) == f.default, f.name                              # the others': left at their defaults
    assert cfg.irrigation_state_path == tmp_path / "irrigation_state.json"
    assert cfg.irrigation_alert_log_path == tmp_path / "irrigation_alerts.sqlite"
    monkeypatch.setenv("MCP_ECOWITT_API_KEY", "a")
    monkeypatch.setenv("MCP_ECOWITT_APP_KEY", "b")
    service = Config.from_env(program="mcp")
    assert service.tuya_device_id == "" and service.irrigation_bind == ("127.0.0.1", 8766)   # ecobot-mcp reads none of irrigation-mcp's


def test_irrigation_mcp_s_settings_have_their_defaults():
    defaults = {f.name: f.default for f in fields(Config)}
    assert defaults["irrigation_bind"] == ("127.0.0.1", 8766) and defaults["irrigation_allowed_hosts"] == ("127.0.0.1", "localhost")
    assert defaults["irrigation_ecobot_mcp_url"] == "http://127.0.0.1:8765/mcp" and defaults["irrigation_check_hour"] == 18
    assert defaults["tuya_base_url"] == "https://openapi.tuyaeu.com"


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
