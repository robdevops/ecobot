"""The evals' frozen world: the service's ten tools over the fake station's archive, an AirGradient on the same file, Open-Meteo and the
pollen page (all faked, nothing fetched), with the clock fixed at the evals' NOW (evalkit.NOW: Tue 29 Sep 2026 14:05, Melbourne) in
lib/ and the engines. tests/test_service_evals.py calls the expected calls against it (at_eval_now, build_archive, real_context, with
pytest's monkeypatch), and `scripts/eval_prompts.py --live` answers the bot's model from it (`world`: built once per run, outside
pytest), so the model sees real results, notes and refusals and can take a second round."""

import asyncio
import datetime as _dt
import importlib
import pkgutil
import sys
import tempfile
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests import evalkit

EVAL_NOW = evalkit.NOW.replace(tzinfo=ZoneInfo(evalkit.NOW_TZ)).timestamp()


class _DatetimeType(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _dt.datetime)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _dt.datetime)


class _EvalDatetime(_dt.datetime, metaclass=_DatetimeType):
    @classmethod
    def now(cls, tz=None):
        return _dt.datetime.fromtimestamp(EVAL_NOW, tz)

    @classmethod
    def today(cls):
        return _dt.datetime.fromtimestamp(EVAL_NOW)


class _DateType(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _dt.date)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _dt.date)


class _EvalDate(_dt.date, metaclass=_DateType):
    @classmethod
    def today(cls):
        return _dt.datetime.fromtimestamp(EVAL_NOW).date()


def at_eval_now(patch: pytest.MonkeyPatch, *also: str):
    """The clock fixed at EVAL_NOW in every module of lib/ and tests/ (and the modules named in `also`, a test module whatever pytest
    names it): every module of lib/ is imported first (whatever was imported before), then every name bound to datetime or date is the
    fixed one, and time.time() (and the archive's epoch_now) returns EVAL_NOW; the process's zone is the station's (as conftest sets
    it), so a naive local time is Melbourne's. `patch.undo()` puts it all back."""
    import lib
    import tests.fakes, tests.test_pollen, tests.test_service_forecast  # noqa: F401
    for info in pkgutil.walk_packages(lib.__path__, "lib."):
        importlib.import_module(info.name)
    patch.setenv("TZ", evalkit.NOW_TZ)
    _time.tzset()
    patch.setattr(_time, "time", lambda: EVAL_NOW)
    for name, module in list(sys.modules.items()):
        if module is None or not (name in ("lib", *also) or name.startswith(("lib.", "tests."))):
            continue
        for attr, value in list(vars(module).items()):
            if value is _dt.datetime:
                patch.setattr(module, attr, _EvalDatetime)
            elif value is _dt.date:
                patch.setattr(module, attr, _EvalDate)
    patch.setattr(sys.modules["lib.ecowitt.archive"], "epoch_now", lambda: EVAL_NOW)


async def _inline(func, /, *args, **kwargs):
    return func(*args, **kwargs)


async def build_archive(folder: Path) -> Path:
    """The fake station's whole archive (its ecobot.sqlite in `folder`), built at the clock as it is (at_eval_now first), its pacing off."""
    from lib.ecowitt import Archive, Ecowitt, api, archive
    from tests.fakes import config, ecowitt_transport
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(api, "MIN_GAP_SECONDS", 0)
        patch.setattr(archive, "PACE_SECONDS", 0)
        patch.setattr(asyncio, "to_thread", _inline)
        transport, _ = ecowitt_transport()
        eco = Ecowitt(config(folder), transport=transport)
        await eco.start()
        await Archive(eco).run_once()
        await eco.close()
    return config(folder).storage_path


async def real_context(folder: Path, archive: Path):
    """The ten tools' sources, faked, at the clock as it is (at_eval_now), their files in `folder` (not the archive's): the archived
    station, an AirGradient on the same file, both together, Open-Meteo, the pollen page in season; the context's own clock is EVAL_NOW."""
    import json

    from lib.airgradient import AirGradient
    from lib.compose import Composer
    from lib.forecast import Forecast
    from lib.service.context import Context
    from tests.fakes import air_transport, archived_station, config
    from tests.test_pollen import make as pollen_source
    from tests.test_service_forecast import open_meteo
    eco, _ = await archived_station(folder, archive)
    air = AirGradient(config(folder), transport=air_transport(rows_per_day=24)[0], storage=eco.storage)
    await air.start()
    forecast = Forecast(config(folder, forecast=True, forecast_lat=-37.8, forecast_lon=144.9, place="Melbourne"), None, open_meteo([]))
    noon = datetime.combine(evalkit.NOW.date(), dtime(12, 0))
    forecast.now = lambda: noon
    pollen, _ = pollen_source(folder)
    ctx = await Context.open(eco=eco, air=air, composer=Composer(eco, air), forecast=forecast, pollen=pollen, clock=lambda: EVAL_NOW)
    (summary,) = [t for t in evalkit.service_tools(ctx) if t.name == "summary"]
    held = json.loads(await summary.call({"readings": ["air"], "period": "last 30 days", "chart": False}))   # (the air's month, held)
    assert "error" not in held
    return ctx


async def close(ctx):
    """Close the sources real_context started."""
    await ctx.pollen.close()
    await ctx.air.close()
    await ctx.eco.close()


@asynccontextmanager
async def world(folder: Path | None = None):
    """The frozen world for a whole run outside pytest: the clock fixed (at_eval_now), the archive built under it, and the lib.service
    Service of the ten tools over real_context; everything put back on leaving. `folder`: where its files go (a temporary one)."""
    from lib.service import Service
    patch = pytest.MonkeyPatch()
    with tempfile.TemporaryDirectory(prefix="ecobot-eval-") as tmp:
        root = Path(folder or tmp)
        (root / "archive").mkdir(parents=True, exist_ok=True)
        (root / "world").mkdir(parents=True, exist_ok=True)
        try:
            at_eval_now(patch)
            ctx = await real_context(root / "world", await build_archive(root / "archive"))
            try:
                yield Service.of(ctx)
            finally:
                await close(ctx)
        finally:
            patch.undo()
            _time.tzset()
