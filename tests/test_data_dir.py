from pathlib import Path

from lib.config import ROOT, Config, data_dir

NAMES = ("bot_state.json", "ecowitt_cache.sqlite", "airgradient_cache.sqlite", "conditions_cache.sqlite")


def configured(monkeypatch, **env):
    for key, value in {"TELEGRAM_BOT_TOKEN": "t", "XAI_API_KEY": "x", "ECOWITT_API_KEY": "a", "ECOWITT_APP_KEY": "b", **env}.items():
        monkeypatch.setenv(key, value)
    return Config.from_env()


def paths(cfg):
    return (cfg.state_path, cfg.cache_path, cfg.air_cache_path, cfg.conditions_cache_path)


def test_without_data_dir_the_files_stay_where_they_have_always_been(monkeypatch):
    monkeypatch.delenv("DATA_DIR", raising=False)
    assert data_dir() == ROOT
    assert paths(configured(monkeypatch)) == tuple(ROOT / n for n in NAMES)
    monkeypatch.setenv("DATA_DIR", "  ")
    assert data_dir() == ROOT                                                     # blank is the same as unset


def test_data_dir_moves_the_state_and_all_three_caches_and_is_created(monkeypatch, tmp_path):
    target = tmp_path / "a" / "data"
    cfg = configured(monkeypatch, DATA_DIR=str(target))
    assert paths(cfg) == tuple(target.resolve() / n for n in NAMES) and target.is_dir()


def test_a_relative_data_dir_is_taken_from_the_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = configured(monkeypatch, DATA_DIR="state")
    assert cfg.state_path == (tmp_path / "state" / "bot_state.json").resolve() and (tmp_path / "state").is_dir()
    assert data_dir() == Path(tmp_path / "state").resolve()
