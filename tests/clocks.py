"""The clock in tests: pinned for one test (pin), or moved for the whole suite (ECOBOT_CLOCK).

pin(monkeypatch, when) fixes the clock at `when` (an aware datetime) for one test, as tests/evalworld.py fixes it for the evals: every
module of lib/ is imported, then every name in a module of lib/, tests/ and scripts/ bound to datetime or date is a class whose now() and today() are `when`,
time.time() returns it, and so does the archive's epoch_now. With conftest's pinned_archive(when), the fake station's archive as it was
then: a test of something that depends on the time of day (just after midnight, the evening) runs at that time whenever it is run.

The whole suite at another time of day: ECOBOT_CLOCK="2026-11-11 00:05" (a Melbourne wall-clock time) makes the clock start there,
and run on from it, for everything the suite and lib/ read, so a test that only fails just after midnight (or late at night, or on the
1st of a month) can be run at that time on purpose:

    ECOBOT_CLOCK="2026-11-11 00:05" python -m pytest -q

conftest installs it before anything of lib/ is imported (install()): time.time() is moved by a fixed offset (it still ticks), and
datetime.datetime and datetime.date, in the datetime module itself, become subclasses whose now() and today() read it, so every later
`from datetime import datetime` (lib/, tests/, a default argument `clock=time.time`) gets the moved clock. The tests that fix the clock
themselves (tests/evalworld.py, tests/test_service_switch.py) still do: they rebind these classes as they would the real ones.
Without ECOBOT_CLOCK nothing is changed."""

import datetime as _dt
import importlib
import os
import pkgutil
import sys
import time as _time
from zoneinfo import ZoneInfo

_REAL_TIME = _time.time
_RealDatetime, _RealDate = _dt.datetime, _dt.date
OFFSET = 0.0
_PINNED = 0.0
DAYTIME = _RealDatetime(2026, 11, 10, 14, 5, tzinfo=ZoneInfo("Australia/Melbourne"))   # (conftest's daytime_archive)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OURS = tuple(os.path.join(_ROOT, d) + os.sep for d in ("lib", "tests", "scripts"))   # (a test module too, whatever pytest names it)


def _now() -> float:
    return _REAL_TIME() + OFFSET


class _DatetimeType(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _RealDatetime)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _RealDatetime)


class SweepDatetime(_RealDatetime, metaclass=_DatetimeType):
    @classmethod
    def now(cls, tz=None):
        return _RealDatetime.fromtimestamp(_now(), tz)

    @classmethod
    def today(cls):
        return _RealDatetime.fromtimestamp(_now())

    @classmethod
    def utcnow(cls):
        return _RealDatetime.fromtimestamp(_now(), _dt.UTC).replace(tzinfo=None)


class _DateType(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _RealDate)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _RealDate)


class SweepDate(_RealDate, metaclass=_DateType):
    @classmethod
    def today(cls):
        return _RealDatetime.fromtimestamp(_now()).date()


def install() -> str | None:
    """Move the clock to ECOBOT_CLOCK (local wall-clock time, in the process's zone, Melbourne), if it is set; the text it was set to."""
    global OFFSET
    text = os.environ.get("ECOBOT_CLOCK")
    if not text:
        return None
    _time.tzset()
    start = _RealDatetime.fromisoformat(text)
    if start.tzinfo is None:
        start = start.astimezone()   # (the process's zone: conftest sets TZ first)
    OFFSET = start.timestamp() - _REAL_TIME()
    _time.time = _now
    _dt.datetime, _dt.date = SweepDatetime, SweepDate
    return text


class PinnedDatetime(SweepDatetime):
    """datetime whose now() and today() are the pinned time (anything else, and its instances, are the real datetime's)."""

    @classmethod
    def now(cls, tz=None):
        return _RealDatetime.fromtimestamp(_PINNED, tz)

    @classmethod
    def today(cls):
        return _RealDatetime.fromtimestamp(_PINNED)

    @classmethod
    def utcnow(cls):
        return _RealDatetime.fromtimestamp(_PINNED, _dt.UTC).replace(tzinfo=None)


class PinnedDate(SweepDate):
    @classmethod
    def today(cls):
        return _RealDatetime.fromtimestamp(_PINNED).date()


def pin(patch, when: _RealDatetime):
    """Fix the clock at `when` (aware) in every module of lib/ and tests/, with `patch` (a MonkeyPatch: its undo puts it all back)."""
    import lib
    for info in pkgutil.walk_packages(lib.__path__, "lib."):
        importlib.import_module(info.name)
    ts = when.timestamp()
    patch.setattr(sys.modules[__name__], "_PINNED", ts)
    patch.setattr(_time, "time", lambda: ts)
    datetimes, dates = (_RealDatetime, SweepDatetime), (_RealDate, SweepDate)
    for name, module in list(sys.modules.items()):
        if module is None or module is sys.modules[__name__] or not str(getattr(module, "__file__", None) or "").startswith(_OURS):
            continue
        for attr, value in list(vars(module).items()):
            if any(value is c for c in datetimes):
                patch.setattr(module, attr, PinnedDatetime)
            elif any(value is c for c in dates):
                patch.setattr(module, attr, PinnedDate)
    patch.setattr(sys.modules["lib.ecowitt.archive"], "epoch_now", lambda: ts)


def yesterday_at(hour: int, minute: int, tz) -> _RealDatetime:
    """Yesterday at hour:minute in tz (aware): a fixed time of day that the fake station's archive, built at the real clock, always
    holds the day of; for a test whose answer depends on the time of day alone (a tool's Context(clock=...))."""
    day = _RealDatetime.fromtimestamp(_time.time(), tz).date() - _dt.timedelta(days=1)
    return _RealDatetime.combine(day, _dt.time(hour, minute), tz)
