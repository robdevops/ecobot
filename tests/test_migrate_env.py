"""scripts/migrate_env.py: an environment file's settings renamed to their new names, values, comments and order kept."""

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import migrate_env  # noqa: E402

from lib.config import Config, old_names  # noqa: E402
from dataclasses import fields  # noqa: E402

OLD = """# keys
TELEGRAM_BOT_TOKEN=123:abc
export ECOWITT_API_KEY = "k e y"

PLACE=Melbourne
WEATHER_MCP_URL=http://ecobot-service:8765/mcp
MCP_SERVICE_ALLOWED_HOSTS=127.0.0.1,localhost,ecobot-service
MCP_SERVICE_TOKENS=other,s3cret
WEATHER_MCP_TOKEN=s3cret
TZ=Australia/Melbourne
"""


def test_every_renamed_setting_is_found_from_the_configs_text():
    want = {old: f.metadata["env"] for f in fields(Config) for old in old_names(f)}
    assert migrate_env.renames() == want


def test_a_file_is_renamed_keeping_values_comments_quotes_and_order_and_the_token_goes_first():
    out, notes = migrate_env.migrate(OLD.splitlines(keepends=True), migrate_env.renames())
    assert "".join(out) == """# keys
BOT_TELEGRAM_TOKEN=123:abc
export MCP_ECOWITT_API_KEY = "k e y"

MCP_PLACE=Melbourne
BOT_MCP_URL=http://ecobot-mcp:8765/mcp
MCP_ALLOWED_HOSTS=127.0.0.1,localhost,ecobot-mcp
AUTH_TOKENS=s3cret,other
TZ=Australia/Melbourne
"""
    assert any("put first" in n for n in notes) and any("WEATHER_MCP_TOKEN removed" in n for n in notes)
    again, _ = migrate_env.migrate(out, migrate_env.renames())
    assert again == out   # (run again: nothing changes)


def test_the_retired_token_alone_becomes_the_list():
    out, _ = migrate_env.migrate(["WEATHER_MCP_TOKEN=t0k\n"], migrate_env.renames())
    assert out == ["AUTH_TOKENS=t0k\n"]


def test_an_old_and_a_new_name_set_differently_stop_it_and_the_same_value_drops_the_old_line():
    with pytest.raises(SystemExit, match="PLACE and MCP_PLACE"):
        migrate_env.migrate(["PLACE=a\n", "MCP_PLACE=b\n"], migrate_env.renames())
    out, _ = migrate_env.migrate(["PLACE=a\n", "MCP_PLACE=a\n"], migrate_env.renames())
    assert out == ["MCP_PLACE=a\n"]


def test_without_write_nothing_changes_and_secrets_are_masked_with_write_a_backup_keeps_mode(tmp_path, capsys):
    env = tmp_path / "ecobot.env"
    env.write_text(OLD)
    os.chmod(env, 0o600)
    assert migrate_env.main([str(env)]) == 0
    shown = capsys.readouterr().out
    assert env.read_text() == OLD and "123:abc" not in shown and "s3cret" not in shown and "+BOT_TELEGRAM_TOKEN=****" in shown
    assert migrate_env.main([str(env), "--write"]) == 0
    assert "BOT_TELEGRAM_TOKEN=123:abc" in env.read_text() and stat.S_IMODE(env.stat().st_mode) == 0o600
    backups = list(tmp_path.glob("ecobot.env.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == OLD and stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert migrate_env.main([str(env), "--write"]) == 0 and "nothing to rename" in capsys.readouterr().out


def test_a_setting_with_two_old_names_takes_either_and_the_irrigation_check_hour_keeps_its_name():
    """IRRIGATION_TUYA_* was BOT_TUYA_*, and before that TUYA_*; IRRIGATION_CHECK_HOUR was BOT_IRRIGATION_CHECK_HOUR (and before that
    IRRIGATION_CHECK_HOUR itself, which stays as it is)."""
    names = migrate_env.renames()
    assert names["BOT_TUYA_DEVICE_ID"] == names["TUYA_DEVICE_ID"] == "IRRIGATION_TUYA_DEVICE_ID"
    assert names["BOT_IRRIGATION_CHECK_HOUR"] == "IRRIGATION_CHECK_HOUR" and "IRRIGATION_CHECK_HOUR" not in names
    out, _ = migrate_env.migrate(["TUYA_DEVICE_ID=d\n", "BOT_TUYA_CLIENT_ID=c\n", "IRRIGATION_CHECK_HOUR=7\n"], names)
    assert out == ["IRRIGATION_TUYA_DEVICE_ID=d\n", "IRRIGATION_TUYA_CLIENT_ID=c\n", "BOT_IRRIGATION_MCP_URL=http://127.0.0.1:8766/mcp\n",
                   "IRRIGATION_CHECK_HOUR=7\n"]                                            # (the bot's way to irrigation-mcp added: below)
    out, notes = migrate_env.migrate(["TUYA_CLIENT_SECRET=s\n", "BOT_TUYA_CLIENT_SECRET=s\n"], names)   # both old ones, the same value
    assert out[0] == "IRRIGATION_TUYA_CLIENT_SECRET=s\n" and len(out) == 2 and any("BOT_TUYA_CLIENT_SECRET removed" in n for n in notes)
    out, _ = migrate_env.migrate(["BOT_IRRIGATION_CHECK_HOUR=6\n", "IRRIGATION_CHECK_HOUR=6\n"], names)
    assert out == ["IRRIGATION_CHECK_HOUR=6\n"]
    with pytest.raises(SystemExit, match="BOT_TUYA_CLIENT_SECRET and TUYA_CLIENT_SECRET"):
        migrate_env.migrate(["TUYA_CLIENT_SECRET=a\n", "BOT_TUYA_CLIENT_SECRET=b\n"], names)
    with pytest.raises(SystemExit, match="BOT_IRRIGATION_CHECK_HOUR and IRRIGATION_CHECK_HOUR"):
        migrate_env.migrate(["BOT_IRRIGATION_CHECK_HOUR=6\n", "IRRIGATION_CHECK_HOUR=7\n"], names)


def test_the_irrigation_controllers_settings_bring_the_bots_way_to_irrigation_mcp_once():
    """Step 8b: the controller is irrigation-mcp's and the bot its client; a file with any Tuya setting (any of its names, set) and no
    BOT_IRRIGATION_MCP_URL gets it, after them, as systemd reaches irrigation-mcp, with a note (Docker's address in it)."""
    names = migrate_env.renames()
    out, notes = migrate_env.migrate(["TUYA_DEVICE_ID=d\n", "BOT_TUYA_CLIENT_SECRET=s\n", "TZ=Australia/Melbourne"], names)
    assert out == ["IRRIGATION_TUYA_DEVICE_ID=d\n", "IRRIGATION_TUYA_CLIENT_SECRET=s\n", "BOT_IRRIGATION_MCP_URL=http://127.0.0.1:8766/mcp\n",
                   "TZ=Australia/Melbourne"]
    assert any("BOT_IRRIGATION_MCP_URL=http://127.0.0.1:8766/mcp added" in n and "http://irrigation-mcp:8766/mcp" in n for n in notes)
    again, notes = migrate_env.migrate(out, names)
    assert again == out and notes == []                                                     # (run again: nothing changes)
    out, _ = migrate_env.migrate(["IRRIGATION_TUYA_DEVICE_ID=d"], names)                    # the last line, without its newline
    assert out == ["IRRIGATION_TUYA_DEVICE_ID=d\n", "BOT_IRRIGATION_MCP_URL=http://127.0.0.1:8766/mcp\n"]
    kept = ["IRRIGATION_TUYA_DEVICE_ID=d\n", "BOT_IRRIGATION_MCP_URL=http://irrigation-mcp:8766/mcp\n"]
    assert migrate_env.migrate(kept, names) == (kept, [])                                   # one already there (Docker's) is left alone
    for none in (["TZ=UTC\n"], ["TUYA_DEVICE_ID=\n"], ["# TUYA_DEVICE_ID=d\n"]):        # no controller (unset, or commented out)
        assert "BOT_IRRIGATION_MCP_URL" not in "".join(migrate_env.migrate(none, names)[0]), none
