import os
import subprocess
import sys
from pathlib import Path

import pytest

from lib.config import ROOT, Config, data_dir

NAMES = ("bot_state.json", "ecobot.sqlite")


def configured(monkeypatch, **env):
    for key, value in {"TELEGRAM_BOT_TOKEN": "t", "OPENROUTER_API_KEY": "x", "ECOWITT_API_KEY": "a", "ECOWITT_APP_KEY": "b", **env}.items():
        monkeypatch.setenv(key, value)
    return Config.from_env()


def paths(cfg):
    return (cfg.state_path, cfg.storage_path)


def test_without_data_dir_the_files_stay_where_they_have_always_been(monkeypatch):
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    assert data_dir() == ROOT
    assert paths(configured(monkeypatch)) == tuple(ROOT / n for n in NAMES)
    monkeypatch.setenv("DATA_DIR", "  ")
    assert data_dir() == ROOT                                                     # blank is the same as unset


def test_data_dir_moves_the_state_and_the_file_and_is_created(monkeypatch, tmp_path):
    target = tmp_path / "a" / "data"
    cfg = configured(monkeypatch, DATA_DIR=str(target))
    assert paths(cfg) == tuple(target / n for n in NAMES) and target.is_dir()


def test_a_relative_data_dir_is_taken_from_the_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = configured(monkeypatch, DATA_DIR="state")
    assert cfg.state_path == tmp_path / "state" / "bot_state.json" and (tmp_path / "state").is_dir()
    assert data_dir() == Path(tmp_path / "state").resolve()


def test_systemd_state_directory_is_the_default_and_data_dir_wins(monkeypatch, tmp_path):
    first, second = tmp_path / "one", tmp_path / "two"
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setenv("STATE_DIRECTORY", f"{first}:{second}")                     # systemd may list several: the first is used
    assert data_dir() == first and paths(configured(monkeypatch))[0] == first / "bot_state.json" and first.is_dir()
    monkeypatch.setenv("DATA_DIR", str(second))
    assert data_dir() == second


def test_a_symlinked_data_dir_is_used_as_written(monkeypatch, tmp_path):
    real = tmp_path / "private"
    real.mkdir()
    link = tmp_path / "ecobot"
    link.symlink_to(real)
    monkeypatch.setenv("DATA_DIR", str(link))
    assert data_dir() == link and configured(monkeypatch).state_path == link / "bot_state.json"


def test_a_folder_that_cannot_be_made_stops_the_start_with_a_plain_message(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    with pytest.raises(SystemExit) as stop:
        configured(monkeypatch, DATA_DIR=str(blocker / "data"))
    assert "not writable" in str(stop.value) and "DATA_DIR" in str(stop.value)


def test_the_bot_starts_without_matplotlib_or_numpy():
    """The charts are drawn with Pillow and the statistics in plain Python: importing the bot loads neither."""
    done = subprocess.run([sys.executable, "-c", "import sys, ecobot, lib.bot, lib.compose; assert not {'matplotlib', 'numpy'} & set(sys.modules)"], cwd=ROOT,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-500:]

def test_the_running_commit_is_read_without_running_git(tmp_path):
    from ecobot import commit_in
    git = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True, text=True,
                                    env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                         "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()
    assert commit_in(tmp_path / ".git") == "unknown"                              # no repository
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "one")
    want = git("rev-parse", "--short=7", "HEAD")
    assert commit_in(tmp_path / ".git") == want                                    # a loose branch ref
    git("pack-refs", "--all", "--prune")
    assert commit_in(tmp_path / ".git") == want                                    # a packed ref
    git("checkout", "-q", "--detach")
    assert commit_in(tmp_path / ".git") == want                                    # a detached HEAD
    (tmp_path / ".git" / "HEAD").write_text("garbage")
    assert commit_in(tmp_path / ".git") == "unknown"
