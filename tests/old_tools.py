"""The bot's twelve old tools' handlers, as they answered before the model got the service's tools (docs/ecobot-mcp.md 6, step 3), kept
for the tests only: they drive the engines the service wraps the way the old tools did (HistoryQuery, AirGradient.history, the day
rows, link, the readings), and they are the oracle the service's words were ported against (with tests/report_words.py). The corrections
from the person's words are gone with step 3. Deleted with the bot (step 6)."""

import asyncio
import logging
import operator
import time as clock
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, time

from lib.airgradient.days import ALWAYS_SHOWN as AIR_SHOWN, FIELDS as AIR_FIELDS
from lib.airgradient.metrics import EXTRA
from lib.captions import wants_chart
from lib.daytable import analyse, lists_days, prepare
from lib.ecowitt.api import GROUPS, daily_run
from lib.ecowitt.calendar import PublicHolidays
from lib.ecowitt.days import ALWAYS_SHOWN, AVERAGED, FIELD_UNITS, day_values_on
from lib.ecowitt.glance import glance
from lib.ecowitt.link import link
from lib.ecowitt.query import HistoryQuery, chart_field, stack_names
from lib.forecast.source import SOURCE
from lib.history import (AIR_SENSOR, ALIASES, DAY_VALUE_IS, INTERVALS, MAX_ROWS, RAIN, VALUE_IS, air_metrics, finest_fit,
                         interval_starts, read_days, read_intervals, row_count, station_fields, station_metrics)
from lib.pollen.source import OFF_SEASON
from lib.series import READING_GROUPS, WEATHER, find
from lib.timeutil import day_text, local_date, local_epoch, now_local, parse_local, when
from lib.tools import Turn, json_result

log = logging.getLogger(__name__)

LINK_CHART_HINT = ("Your reply is sent as a message just before a chart with the reading as a line and rain as bars behind it, so keep "
                   "it short: the period, then the finding in one or two lines (how much of the rain fell while the reading "
                   "was falling, and the correlation), citing the numbers. Don't mention or describe the chart.")
DEFAULT_GROUPS = "outdoor,indoor"
NOW_GROUPS = ",".join(READING_GROUPS)


def parse_groups(value, default: str = DEFAULT_GROUPS) -> list[str]:
    """'outdoor, indoor' or ['outdoor', 'indoor'] -> ['outdoor', 'indoor'] (a reading named instead of its group becomes its group)."""
    parts = value if isinstance(value, list) else str(value or default).split(",")
    named = (p.split(".")[0].strip().lower() for p in parts if p.strip())
    groups = (g if g in GROUPS else (r.group if (r := find(g)) else None) for g in named)
    return list(dict.fromkeys(g for g in groups if g)) or default.split(",")


# ---------- the weather station's ----------
async def weather_now(eco, args: dict, turn=None) -> str:
    """weather_now: the live reading of these groups, each value with its unit, and the emoji of a notable one."""
    data = await eco._live_data(parse_groups(args.get("groups")))
    out, newest, emoji = {}, 0, {}
    for grp, fields in data.items():
        for name, obj in (fields.items() if isinstance(fields, dict) else ()):
            if isinstance(obj, dict) and "value" in obj:
                out.setdefault(grp, {})[name] = f"{obj['value']} {obj.get('unit', '')}".strip()
                try:
                    if tag := glance(grp, name, float(obj["value"])):
                        emoji[f"{grp}.{name}"] = tag
                except (TypeError, ValueError):
                    pass
                try:
                    newest = max(newest, int(obj.get("time") or 0))
                except (TypeError, ValueError):
                    pass
    return json_result({"time": when(newest, eco.tz) if newest else None, **out, **({"emoji": emoji} if emoji else {})})


async def weather_history(eco, args: dict, turn: Turn | None = None) -> str:
    """weather_history: HistoryQuery over the groups asked for (and those its chart needs)."""
    turn = turn or Turn()
    groups = parse_groups(args.get("groups"))
    groups += [g for n in stack_names(args, turn) if (g := WEATHER[n].group) not in groups]
    if (r := find(chart_field(args, turn) or "")) and r.group not in groups:
        groups.append(r.group)
    return await HistoryQuery(eco.fetcher(groups, urgent=not turn.ahead), args, turn).run()


async def weather_days(eco, args: dict, turn=None) -> str:
    """weather_days: find_days over the day rows."""
    return json_result(await find_days(eco.storage, eco.mac, eco.tz, args, now_local(eco.tz), turn))


async def weather_link(eco, args: dict, turn: Turn | None = None) -> str:
    """weather_link: link() over the 30-minute readings, its chart when asked (or the period is 3 days or more)."""
    turn = turn or Turn()
    out, spec, first, last = await asyncio.to_thread(link, eco.storage.read_now, eco.mac, eco.tz, args, now_local(eco.tz))
    if spec and wants_chart(args, turn, datetime.combine(first, time()), datetime.combine(last, time())):
        turn.charts.append(spec)
        out["chart"] = LINK_CHART_HINT
    return json_result(out)


# ---------- the air sensor's ----------
async def air_current(air, extras=()) -> dict:
    """The latest reading as air_quality gave it."""
    return (await air.latest(extras)).reading


async def air_quality(air, args: dict, turn: Turn | None = None) -> str:
    """air_quality: the reading now, or a period's summary (and chart)."""
    turn = turn or Turn()
    try:
        chart = wants_chart(args, turn)
        if args.get("start_date") or args.get("end_date") or chart:
            return json_result(await air.history(args.get("start_date"), args.get("end_date"), chart=chart, metrics=args.get("metrics"),
                                                 turn=turn))
        return json_result(await air_current(air, [m for m in args.get("metrics") or [] if m in EXTRA]))
    except Exception as e:
        log.warning("AirGradient request failed: %s", e)
        return f"Error: couldn't read the air-quality sensor ({e})"


async def air_days(store, tz, args: dict, turn=None) -> str:
    """air_days: find_air_days over the air sensor's day rows (its AirReadings)."""
    return json_result(await find_air_days(store, tz, args, now_local(tz), turn))


# ---------- the forecast's and the pollen's ----------
async def forecast(fc, args: dict, turn=None) -> str:
    """weather_forecast: the next days' lines."""
    if fc.days is None and not args.get("cached"):
        try:
            await fc.warm(True)
        except Exception as e:
            log.warning("Forecast not readable: %s", e)
    try:
        count = max(1, min(7, int(args.get("days") or 7)))
    except (TypeError, ValueError):
        count = 7
    lines = fc.lines(count)
    if not lines:
        return json_result({"error": "No forecast is available right now."})
    stamped = {"time": when(int(fc.fetched_at), fc.tz)} if fc.fetched_at else {}
    return json_result({"lines": lines, "place": fc.place, "source": SOURCE, **stamped})


async def pollen(p, args: dict, turn=None) -> str:
    """pollen_asthma: the pollen page's lines."""
    if not p.in_season():
        return json_result({"error": OFF_SEASON})
    if p.page is None and not args.get("cached"):
        try:
            await p.warm(True)
        except Exception as e:
            log.warning("Pollen not readable: %s", e)
    lines = p.lines()
    if not lines:
        return json_result({"error": "No pollen levels are available right now."})
    cur = p.current()
    out = {"lines": lines, "place": p.place, "source": "melbournepollen.com.au (grass pollen from pollen-trap data; asthma risk from the "
                                                   "Victorian Department of Health and BOM, 1 Oct - 31 Dec)"}
    if cur["fetched_at"]:
        out["time"] = when(int(cur["fetched_at"]), p.tz)
    if cur["asthma"] and cur["asthma"]["updated"]:
        out["asthma_updated"] = cur["asthma"]["updated"]
    if not cur["asthma"]:
        out["asthma_note"] = "The page had no thunderstorm asthma level for this district when it was last read."
    return json_result(out)


def as_drawn(tool: str, args: dict, turn: Turn) -> tuple[dict, Turn]:
    """(args, turn) as the old tool read them to draw its chart (what the bot captioned a chart that answered the question from): the
    readings charted, air quality's metrics (else PM2.5), an average if asked for."""
    if tool == "air_quality":
        return {**args, "metrics": args.get("metrics") or turn.air_named or ["pm2_5"]}, turn
    return args, replace(turn, chart_field=chart_field(args, turn), chart_fields=stack_names(args, turn),
                         average_asked=turn.average_asked or bool(args.get("average")))


# ---------- readings ----------
class History:
    """The old `readings` tool over the weather station's and the air sensor's stored readings (either may be missing)."""

    def __init__(self, eco=None, air=None, sent: dict[str, dict[str, str]] | None = None, now: Callable[[], float] = clock.time):
        self.eco, self.air, self.now = eco, air, now
        self.storage = eco.storage if eco else air.store.storage
        self.tz = eco.tz if eco else air.tz
        station = station_metrics(sent) if eco else []
        self.metrics = {m.name: m for m in [*station, *(air_metrics(extra=True) if air else [])]}

    @classmethod
    async def open(cls, eco=None, air=None) -> "History":
        return cls(eco, air, await station_fields(eco) if eco else None)

    def find(self, name: str):
        name = name.strip().lower()
        if name in self.metrics:
            return self.metrics[name]
        prefix, _, bare = name.rpartition(".")
        if prefix == AIR_SENSOR and (m := self.metrics.get(f"{AIR_SENSOR}.{AIR_SENSOR}_{bare}")):
            return m
        reading = WEATHER.get(bare) or next((r for r in WEATHER.values() if r.field == bare), None)
        m = (reading and self.metrics.get(f"{reading.group}.{RAIN if reading.field == 'daily' else reading.field}")
             or self.metrics.get(f"{AIR_SENSOR}.{bare}") or self.metrics.get(f"outdoor.{bare}"))
        return None if m and prefix in ("indoor", AIR_SENSOR) and m.sensor != prefix else m

    def _sensor(self, m) -> int | None:
        if m.air:
            s = self.air.store.sensor if self.air else None
        else:
            s = self.storage.sensors.find(self.eco.mac, m.sensor) if self.eco else None
        return s.id if s else None

    def _time(self, value, default: int) -> int:
        if value in (None, ""):
            return default
        text = str(value).strip()
        try:
            local = parse_local(text, self.tz)
        except ValueError:
            local = parse_local(text.replace("T", " ")[:19], self.tz)
        return local_epoch(local, self.tz)

    async def handle(self, args: dict, turn: Turn | None = None) -> str:
        m = self.find(str(args.get("metric") or ""))
        if m is None:
            return json_result({"error": f"unknown metric {args.get('metric')!r}", "metrics": list(self.metrics)})
        interval = str(args.get("interval") or "").strip().lower()
        interval = ALIASES.get(interval, interval)
        if interval not in INTERVALS:
            return json_result({"error": f"unknown interval {args.get('interval')!r}", "intervals": list(INTERVALS)})
        now = int(self.now())
        try:
            start, end = self._time(args.get("start"), None), min(self._time(args.get("end"), now), now)
        except (TypeError, ValueError):
            return json_result({"error": "start and end are 'YYYY-MM-DD HH:MM', local time"})
        if start is None or start >= end:
            return json_result({"error": "start must be before end (and before now)"})
        by_day = interval == "1day" and m.how != "last"
        n = row_count(start, end, interval, self.tz)
        too_many = n > (MAX_ROWS if by_day else MAX_ROWS + 1)
        marks = [] if too_many or by_day else interval_starts(start, end, INTERVALS[interval], self.tz)
        if too_many or len(marks) > MAX_ROWS:
            fits = finest_fit(start, end, self.tz)
            return json_result({"error": "too many rows", "rows": max(n, len(marks)), "max_rows": MAX_ROWS, "fits": fits,
                                "note": f"Ask again with interval {fits}." if fits else
                                        "Too long even for days: split the period, or use weather_history or weather_days."})
        out = {"metric": m.name, "unit": m.unit, "interval": interval, "value_is": (DAY_VALUE_IS if by_day else VALUE_IS)[m.how]}
        sensor = self._sensor(m)
        if sensor is None:
            return json_result({**out, "rows": [], "note": "Nothing is stored for this sensor yet."})
        if by_day:
            first, last = local_date(start, self.tz), local_date(end, self.tz)
            out |= await self.storage.read(read_days, m, sensor, first, last, self.tz)
            partial = last == local_date(now, self.tz)
        else:
            out |= await self.storage.read(read_intervals, m, sensor, interval, marks, end, now, self.tz)
            partial = marks[-1] + INTERVALS[interval] > now
        if partial:
            out["note_last"] = "The last row is still filling: it runs up to now."
        if not any(row[1] is not None for row in out["rows"]):
            out["note"] = (f"No readings at this interval for this period: ask again with interval {out['fits']}." if out.get("fits") else
                           "No readings are stored for this period at this interval: say so; never fill the gap.")
        return json_result(out)



# ---------- the day tools' engines (the service's days reads the day rows itself: lib/service/days.py) ----------
async def find_days(storage, mac: str, tz, args: dict, now: datetime, turn=None) -> dict:
    """weather_days' engine: the station's days from its day rows (lib/ecowitt/days.py day_values_on), counted and ranked by
    lib/daytable.py, with the old tool's notes."""
    asked = prepare(args, FIELD_UNITS, ALWAYS_SHOWN, now.date())
    if isinstance(asked, str):
        return {"error": asked}
    only = args.get("only")
    if only not in (None, "public_holiday", "weekend"):
        return {"error": "only must be public_holiday or weekend"}
    calendar = PublicHolidays(tz)
    if only == "public_holiday" and not calendar.available:
        return {"error": calendar.problem()}
    kind = {None: lambda d: True, "weekend": lambda d: d.weekday() >= 5,
            "public_holiday": lambda d: calendar.name(d) is not None}[only]
    first, last, shown, where, sort_by = asked.first, asked.last, asked.shown, asked.where, asked.sort_by
    days = asked.days
    values, source = await storage.read(day_values_on, mac, shown, days)

    checked = [d for d in days if kind(d) and any(d in values[n] for n in shown)]
    got = analyse(values, asked, checked, tz, turn,
                  lambda d: {**({"holiday": calendar.name(d)} if only == "public_holiday" else {}),
                             "source": "daily" if source.get(d) == "daily" else "exact"},
                  {"public_holiday": "public holidays only", "weekend": "weekends only"}.get(only, ""))
    out, matches, ranked, rank, holds, row = got.out, got.matches, got.ranked, got.rank, got.holds, got.row

    # Days that would have ranked higher had a trace of rain counted: the answer should mention them
    trace: list[date] = []
    rain_cond = next((c for c in where if c["field"] == "rain" and c["op"] in (">", ">=") and c["value"] > 0), None)
    if rain_cond:
        relaxed = [{"field": "rain", "op": ">", "value": 0} if c is rain_cond else c for c in where]
        matched = set(matches)
        better = operator.lt if args.get("order", "desc") == "asc" else operator.gt
        for d in rank(d for d in checked if holds(d, relaxed)):
            if d in matched:
                break  # from here on the real matches rank at least as high
            if not ranked or better(values[sort_by][d], values[sort_by][ranked[0]]):  # strictly ahead, not a tie
                trace.append(d)
            if len(trace) == 2:
                break

    exact_days = sorted(d for d in checked if source.get(d) != "daily")
    if not lists_days(args):
        trace = []   # just the figures
    if trace:
        out["trace_rain_days"] = [row(d) for d in trace]
        out["note_trace"] = (f"These days rank higher but had less than {rain_cond['value']:g} mm of rain (a trace), so they "
                             "are not in \"days\". Mention the first as a short footnote.")
    if any(source.get(d) == "daily" for d in checked):
        out["exact_from"] = day_text(exact_days[0]) if exact_days else None
        out["note_daily"] = (f"Days marked \"daily\" come from daily readings that run {daily_run(tz, first, last)}, so their rain and dates "
                             "are approximate (a morning shower can be counted on the day before). Say so briefly if any "
                             "listed day is marked daily, or if the period reaches back before exact_from.")
    if AVERAGED & set(shown) and any(source.get(d) != "5min" for d in checked):
        out["note_averaged"] = ("For these readings, days from 30-minute or daily data use each interval's average, so a day's true peak "
                                "(or low) may be a little beyond what is listed: a count of days at or past a limit can fall slightly short "
                                "near the limit. Say so briefly.")
    if not checked:
        out["note"] = "No cached readings for this period yet (the bot may still be downloading history)."
    log.info("Days %s to %s: %d checked, %d match, %d exact", first, last, len(checked), len(matches), len(exact_days))
    return out


async def find_air_days(store, tz, args: dict, now: datetime, turn=None) -> dict:
    """air_days' engine: the answer, from one read of the air sensor's day rows (readings.AirReadings.day_figures)."""
    asked = prepare(args, AIR_FIELDS, AIR_SHOWN, now.date())
    if isinstance(asked, str):
        return {"error": asked}
    days, shown = asked.days, asked.shown
    figures = await store.day_figures(asked.first, asked.last, shown)
    values, counts = figures.values, figures.counts
    checked = [d for d in days if any(d in values[n] for n in shown)]
    got = analyse(values, asked, checked, tz, turn)
    out = got.out
    hourly = [d for d in checked if d in figures.hourly]
    if hourly and any(n.endswith(("_max", "_min")) for n in shown):
        out["note_hourly"] = (f"{len(hourly)} of these days only exist as hourly averages, so their highs and lows are hourly "
                              "averages and a short spike is smoothed. Say so briefly if it matters.")
    missing = sum(1 for d in days if not counts.get(d))
    if missing:
        out["note_missing"] = f"{missing} day(s) in this period have no stored readings (not downloaded yet, or the sensor was off) and are not counted."
    if not checked:
        out["note"] = "No stored readings for this period yet (the bot may still be downloading history)."
    log.info("Air days %s to %s: %d checked, %d match", asked.first, asked.last, len(checked), len(got.matches))
    return out


__all__ = ["History", "find_air_days", "find_days", "NOW_GROUPS", "air_current", "as_drawn", "air_days", "air_quality", "forecast", "parse_groups", "pollen", "weather_days",
           "weather_history", "weather_link", "weather_now"]
