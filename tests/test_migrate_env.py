"""scripts/migrate_env.py: an environment file's settings renamed to their new names, values, comments and order kept."""

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import migrate_env  # noqa: E402

from lib.config import Config  # noqa: E402
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
    want = {f.metadata["old"]: f.metadata["env"] for f in fields(Config) if f.metadata.get("old")}
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
