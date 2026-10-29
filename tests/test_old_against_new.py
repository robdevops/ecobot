"""TEMPORARY, for step 2 of docs/storage-plan.md: the same questions asked of the old code and of the new, on the same fixture at the same
moment, and the answers compared, so every difference the move to the new storage makes is seen and judged. The old code is `lib` as it
was before step 2 (commit BASE), taken from git into a package of its own, `oldlib`; the new code is this checkout's. Every difference is
printed (`pytest -s tests/test_old_against_new.py -n0`), and one in a question not judged in JUDGED fails the test. Deleted when step 2 is
done.
"""

import dataclasses
import importlib
import io
import json
import math
import subprocess
import sys
import tarfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from tests.fakes import HISTORY_DAYS, TZ, config, ecowitt_transport, home_assistant_transport

ROOT = Path(__file__).resolve().parent.parent
BASE = "ee6b05d"   # the last commit before step 2
OLD_FILES = ("cache.sqlite", "ecowitt_live.sqlite", "ecowitt_minutes.sqlite")
HA_HISTORY_DAYS = 60   # the Home Assistant station is younger: its archive is quick
USED = ("ecowitt", "ecowitt.api", "ecowitt.archive", "intent", "tools", "timeutil", "compose")

# Questions whose answers may differ, and why (judged): {question: reason}
JUDGED: dict[str, str] = {}


def old_package(root: Path):
    """`lib` at BASE, as the package `oldlib` (its imports are relative, so it stands on its own)."""
    if "oldlib" in sys.modules:
        return sys.modules["oldlib"]
    try:
        tar = subprocess.run(["git", "archive", BASE, "lib"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        pytest.skip(f"the old code (commit {BASE}) is not in this checkout: {e}")
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        t.extractall(root, filter="data")
    (root / "lib").rename(root / "oldlib")
    sys.path.insert(0, str(root))
    return modules("oldlib")


def modules(name: str):
    """The package with the modules the questions use imported."""
    for part in USED:
        importlib.import_module(f"{name}.{part}")
    return sys.modules[name]


def copy(source: Path, target: Path):
    """A consistent copy of a SQLite file another connection has open (in WAL mode)."""
    import sqlite3
    src, dst = sqlite3.connect(source), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


# ---------- the stations ----------
def settings(folder: Path, ha: bool):
    return config(folder, **({"homeassistant_url": "http://ha.local:8123", "homeassistant_token": "tok"} if ha else {}))


async def old_station(pkg, folder: Path, ha: bool):
    """The old code's station with its archive built, and Home Assistant's ranges and minutes taken in (ha)."""
    folder.mkdir(parents=True)
    transport, _ = ecowitt_transport(history_days=HA_HISTORY_DAYS if ha else HISTORY_DAYS)
    kw = {"ha_transport": home_assistant_transport()[0]} if ha else {}
    eco = pkg.ecowitt.Ecowitt(settings(folder, ha), transport=transport, **kw)
    await eco.start()
    await pkg.ecowitt.Archive(eco).run_once()
    if ha:
        await eco.import_ranges()
        await eco.pull_minutes()
    return eco


async def new_station(pkg, old: Path, folder: Path, ha: bool):
    """The new code's station on a copy of the old station's files."""
    folder.mkdir(parents=True)
    for name in OLD_FILES:
        if (old / name).exists():
            copy(old / name, folder / name)
    transport, _ = ecowitt_transport(history_days=HA_HISTORY_DAYS if ha else HISTORY_DAYS)
    kw = {"ha_transport": home_assistant_transport()[0]} if ha else {}
    eco = pkg.ecowitt.Ecowitt(settings(folder, ha), transport=transport, **kw)
    await eco.start()
    return eco


# ---------- the questions ----------
TEXTS = [   # as the person writes them: the bot's own calls (intent.read), charts made in code included
    "temperature today", "temperature yesterday", "temperature last 3 days", "temperature last 7 days", "temperature last 30 days",
    "temperature last 3 months", "temperature last 12 months", "humidity chart last 7 days", "humidity chart 3m", "wind chart 7d",
    "wind chart 1d", "pressure chart 7d", "pressure chart 1y", "rain chart 7d", "rain this year", "UV chart 1d", "UV chart 7d",
    "solar radiation chart 1d", "dew point chart 7d", "feels like chart 7d", "plot temperature and rain last 7 days",
    "plot temperature and humidity on two axes last 7 days", "average temperature last 30 days", "temperature last 3 hours",
    "how many days over 18 degrees this year", "rain by month this year", "how many days was uv 9 or more this year",
    "hottest day per month this year", "average humidity per month this year", "how many days below 10 degrees by month this year",
    "weather now", "weather all week", "temperature chart 1d", "temperature chart 7d", "temperature chart 1m", "temperature chart 3m",
    "temperature chart 1y", "humidity chart 1m", "wind chart 1y", "rain chart 1y", "pressure chart 3m",
]
HA_TEXTS = ["temperature last 3 hours", "temperature last 6 hours", "temperature today", "temperature yesterday", "temperature chart 1d",
            "temperature chart 7d", "humidity chart 1d", "pressure chart 7d", "dew point chart 7d", "wind chart 1d", "rain chart 7d",
            "weather now"]


def calls(today: date, now: datetime, first: date) -> list[tuple[str, str, dict]]:
    """(name, tool, arguments) as the model writes them."""
    day = lambda d: d.isoformat()
    at = lambda d, t="00:00:00": f"{d.isoformat()} {t}"
    year = today.replace(month=1, day=1)
    return [
        ("history, the whole record", "weather_history", {"start_date": at(first), "end_date": at(today, "23:59:59"), "groups": "outdoor"}),
        ("history, the whole record, chart", "weather_history", {"start_date": at(first), "end_date": at(today, "23:59:59"),
                                                                 "groups": "outdoor", "chart": True}),
        ("history, a year of humidity", "weather_history", {"start_date": at(today - timedelta(days=365)), "end_date": at(today, "23:59:59"),
                                                            "groups": "outdoor", "chart": True, "chart_field": "humidity"}),
        ("history, 3 days derived, average", "weather_history", {"start_date": at(today - timedelta(days=3)), "end_date": at(today, "23:59:59"),
                                                                 "groups": "outdoor", "include_derived": ["dew_point", "feels_like"],
                                                                 "average": True}),
        ("history, one day 40 days ago", "weather_history", {"start_date": at(today - timedelta(days=40)),
                                                             "end_date": at(today - timedelta(days=40), "23:59:59"),
                                                             "groups": "outdoor,rainfall"}),
        ("history, one day 200 days ago", "weather_history", {"start_date": at(today - timedelta(days=200)),
                                                              "end_date": at(today - timedelta(days=200), "23:59:59"),
                                                              "groups": "outdoor,rainfall,wind,pressure"}),
        ("history, 90 days of rain", "weather_history", {"start_date": at(today - timedelta(days=90)), "end_date": at(today, "23:59:59"),
                                                         "groups": "rainfall", "chart": True}),
        ("history, 30 days of wind", "weather_history", {"start_date": at(today - timedelta(days=30)), "end_date": at(today, "23:59:59"),
                                                         "groups": "wind", "chart": True, "chart_field": "wind"}),
        ("history, 14 days of pressure", "weather_history", {"start_date": at(today - timedelta(days=14)), "end_date": at(today, "23:59:59"),
                                                             "groups": "pressure"}),
        ("history, a week of everything", "weather_history", {"start_date": at(today - timedelta(days=6)), "end_date": at(today, "23:59:59"),
                                                              "groups": "outdoor,indoor,pressure,rainfall,wind,solar_and_uvi"}),
        ("history, the last 3 hours", "weather_history", {"start_date": (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
                                                          "end_date": now.strftime("%Y-%m-%d %H:%M:%S"),
                                                          "groups": "outdoor,pressure,rainfall,wind"}),
        ("history, 5 months by month", "weather_history", {"start_date": at(today - timedelta(days=150)), "end_date": at(today, "23:59:59"),
                                                           "groups": "outdoor", "chart": True}),
        ("days, the hottest this year", "weather_days", {"start_date": day(year), "end_date": day(today), "sort_by": "temp_max"}),
        ("days, wet days by month", "weather_days", {"start_date": day(first), "end_date": day(today), "count_only": True, "group_by": "month",
                                                     "where": [{"field": "rain", "op": ">=", "value": 1}]}),
        ("days, rain per month", "weather_days", {"start_date": day(first), "end_date": day(today), "count_only": True, "group_by": "month",
                                                  "stat": "sum", "of": "rain"}),
        ("days, one day's figures", "weather_days", {"start_date": day(today - timedelta(days=20)), "end_date": day(today - timedelta(days=20))}),
        ("days, warm weekends", "weather_days", {"start_date": day(first), "end_date": day(today), "only": "weekend",
                                                 "where": [{"field": "temp_max", "op": ">", "value": 18}]}),
        ("days, UV 9 or more", "weather_days", {"start_date": day(first), "end_date": day(today), "count_only": True,
                                                "where": [{"field": "uv_max", "op": ">=", "value": 9}]}),
        ("days, the lowest pressure", "weather_days", {"start_date": day(first), "end_date": day(today), "sort_by": "pressure_min",
                                                       "order": "asc", "limit": 10}),
        ("days, average humidity per month", "weather_days", {"start_date": day(first), "end_date": day(today), "count_only": True,
                                                              "group_by": "month", "stat": "avg", "of": "humidity_avg"}),
        ("days, the wettest hot day", "weather_days", {"start_date": day(first), "end_date": day(today), "sort_by": "rain",
                                                       "where": [{"field": "rain", "op": ">=", "value": 1}, {"field": "temp_max", "op": ">", "value": 15}]}),
        ("link, pressure", "weather_link", {}),
        ("link, humidity, chart", "weather_link", {"driver": "humidity", "chart": True}),
        ("link, a year of wind, chart", "weather_link", {"driver": "wind", "chart": True, "start_date": day(today - timedelta(days=365)),
                                                         "end_date": day(today - timedelta(days=1))}),
        ("now, outdoor and indoor", "weather_now", {"groups": "outdoor,indoor"}),
    ]


def plain(value):
    """A result as plain JSON (tuples as lists, dates as text), for comparing."""
    return json.loads(json.dumps(value, default=str))


def specs(turn) -> list:
    return plain([dataclasses.asdict(c) for c in turn.charts])


def questions(pkg, eco, texts: list[str], with_calls: bool) -> list[tuple[str, object]]:
    """[(name, a coroutine function giving its answer)]: {"call", "result", "charts"}, or the error it raised."""
    now = pkg.timeutil.now_local(TZ)
    first = (eco.created.date() if eco.created else now.date() - timedelta(days=HISTORY_DAYS)) + timedelta(days=1)
    tools = {t.name: t for t in eco.tools}
    asked = []
    for text in texts:
        read = pkg.intent.read(text, now, ("Ecowitt",))
        assert read.fast, f"no call made in code for {text!r}"
        asked.append((text, read.fast[0], read.fast[1], lambda read=read, text=text: pkg.tools.Turn.of(read, text)))
    if with_calls:
        asked += [(name, tool, args, pkg.tools.Turn) for name, tool, args in calls(now.date(), now, first)]

    def tool_call(tool, args, make_turn):
        async def ask():
            turn = make_turn()
            try:
                result = await tools[tool].handler(args, turn)
                return {"call": [tool, args], "result": json.loads(result) if result.startswith("{") else result, "charts": specs(turn)}
            except Exception as e:   # (an error is an answer too: both should give it)
                return {"call": [tool, args], "error": f"{type(e).__name__}: {e}"}
        return ask

    async def dashboard():
        return plain(dataclasses.asdict(d)) if (d := await eco.dashboard()) else None

    async def recent():
        return plain(await eco.recent(3))

    async def week():
        at = pkg.timeutil.now_local(TZ).replace(tzinfo=None)
        return plain(await eco.readings("30min", at - timedelta(days=6, hours=23), at, ["outdoor", "indoor"]))

    out = [(name, tool_call(tool, args, make_turn)) for name, tool, args, make_turn in asked]
    out += [("dashboard", dashboard), ("alerts, the last 3 hours", recent), ("alerts, the last week at 30 minutes", week)]
    if with_calls:
        composer = pkg.compose.Composer(eco, None)

        def composed(args):
            async def ask():
                turn = pkg.tools.Turn()
                return {"result": json.loads(await composer.plot_chart(args, turn)), "charts": specs(turn)}
            return ask
        out += [("compose, temperature over rain", composed({"panels": [{"series": "temperature", "style": "line"}, {"series": "rain"}],
                                                             "start_date": (now.date() - timedelta(days=30)).isoformat(),
                                                             "end_date": (now.date() - timedelta(days=1)).isoformat()})),
                ("compose, pressure and humidity on two axes", composed({"panels": [{"series": "pressure"}, {"series": "humidity"}],
                                                                         "two_axes": True,
                                                                         "start_date": (now.date() - timedelta(days=10)).isoformat(),
                                                                         "end_date": (now.date() - timedelta(days=1)).isoformat()}))]
    return out


# ---------- comparing ----------
def _short(value) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= 160 else text[:157] + "..."


def _numbers(items: list) -> bool:
    return all(v is None or isinstance(v, int | float) and not isinstance(v, bool) for v in items)


def differences(old, new, path: str = "", out: list[str] | None = None, limit: int = 40) -> list[str]:
    """Where `new` differs from `old`, a line each (a list of numbers is told in one line: how many differ, the first, the largest)."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new), key=str):
            if k not in new:
                out.append(f"{path}.{k}: only before: {_short(old[k])}")
            elif k not in old:
                out.append(f"{path}.{k}: only now: {_short(new[k])}")
            else:
                differences(old[k], new[k], f"{path}.{k}", out, limit)
    elif isinstance(old, list) and isinstance(new, list):
        if _numbers(old) and _numbers(new) and (old or new):
            if old != new:
                pairs = [(i, a, b) for i, (a, b) in enumerate(zip(old, new)) if a != b]
                worst = max((abs(a - b) for _, a, b in pairs if a is not None and b is not None), default=None)
                first = f", first at [{pairs[0][0]}]: {pairs[0][1]} -> {pairs[0][2]}" if pairs else ""
                out.append(f"{path}: {len(old)} numbers, now {len(new)}; {len(pairs)} differ{first}"
                           + (f", by up to {worst:.4g}" if worst is not None and math.isfinite(worst) else ""))
            return out
        if len(old) != len(new):
            out.append(f"{path}: {len(old)} items, now {len(new)}")
        for i, (a, b) in enumerate(zip(old, new)):
            differences(a, b, f"{path}[{i}]", out, limit)
    elif old != new:
        out.append(f"{path}: {_short(old)} -> {_short(new)}")
    return out


async def compare(tmp_path, ha: bool) -> tuple[dict[str, list[str]], int]:
    """({question: its differences} for those that differ, how many questions). Both stations start from the same files and are
    refreshed as the keep-warm refreshes them, then asked each question one after the other, so the newest readings are the same."""
    new_pkg = modules("lib")
    old_pkg = old_package(tmp_path / "code")
    with mock.patch.object(old_pkg.ecowitt.api, "MIN_GAP_SECONDS", 0), mock.patch.object(old_pkg.ecowitt.archive, "PACE_SECONDS", 0), \
            mock.patch("lib.ecowitt.archive.PACE_SECONDS", 0):
        old = await old_station(old_pkg, tmp_path / "old", ha)
        new = await new_station(new_pkg, tmp_path / "old", tmp_path / "new", ha)
        found = {}
        try:
            await old.warm()
            await new.warm()
            texts = HA_TEXTS if ha else TEXTS
            pairs = list(zip(questions(old_pkg, old, texts, not ha), questions(new_pkg, new, texts, not ha)))
            for (name, before), (_, after) in pairs:
                if diffs := differences(await before(), await after()):
                    found[name] = diffs
        finally:
            await old.close()
            await new.close()
    return found, len(pairs)


def report(found: dict[str, list[str]], asked: int, label: str) -> list[str]:
    """The differences printed; returns the questions that differ and are not judged."""
    print(f"\n{label}: {asked} questions, {len(found)} answer(s) differ")
    for name, diffs in found.items():
        print(f"  {name}" + (f"  (judged: {JUDGED[name]})" if name in JUDGED else ""))
        for line in diffs:
            print(f"      {line}")
    return [name for name in found if name not in JUDGED]


async def test_the_old_code_and_the_new_answer_alike(tmp_path):
    found, asked = await compare(tmp_path / "cloud", ha=False)
    unjudged = report(found, asked, "Ecowitt's cloud, the whole archive")
    found_ha, asked_ha = await compare(tmp_path / "ha", ha=True)
    unjudged += report(found_ha, asked_ha, "Home Assistant with the cloud as fallback")
    assert not unjudged, f"answers that differ and are not judged: {unjudged}"
