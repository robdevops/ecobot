"""Ecowitt history: fetched within Ecowitt's limits, then turned into a compact answer.

Coarse cycles carry true min/max fields (temperature_low/high etc.), so extremes can be found
cheaply. But 1day buckets cover UTC days (10am-10am in Melbourne), not local days. So:
  - ranges up to 31 days use local-day-aligned data (archived 5-minute where the cache has it,
    else 30-minute: one request per week) for the daily breakdown;
  - longer ranges use 30-minute data for the last 7 days and 1day for older data, only to
    locate the extremes and give a monthly breakdown;
  - each extreme is then refined by fetching exactly its bucket (a 30-minute slot or a UTC day)
    at the finest cycle still available, which gives the real time and local date.
"""

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from statistics import fmean
from typing import NamedTuple

from ..captions import AVERAGE_CHART_HINT, CHART_HINT, STACK_CHART_HINT, DIRECTION_CHART_HINT, wants_chart
from ..lines import Sample, build_line, daily_summary
from ..daytable import grouping_in
from ..rain import rain_bars, rain_slots
from ..timeutil import day_bounds, day_text, local_date, now_local, parse_local, SLOT
from ..tools import json_result
from ..series import WEATHER, derives_range, field_of, find, find_name
from ..panels import panel_for, two_axes
from ..specs import Bars, Chart, Compass, Line, Panel, period_text, stack
from .minutes import MINUTE_HOURS
from .api import CYCLES, MINUTE, cycle_seconds, daily_run, kept_from
from .direction import SPEED_STEPS, rose as direction_rose, summarise as summarise_direction
from .extremes import Ext, better, collect, daily_readings, describe_time, fold, high_of, low_of, series_extremes
from .fetch import Fetcher, spans
from .live import BUCKET

log = logging.getLogger(__name__)

INTRADAY_DAYS = 8               # up to this many days: 5-minute readings where archived
FINE_DAYS = 31                  # up to this many: 30-minute readings and a breakdown by day; longer periods use daily records, by month
DAILY_ALL_DAYS = 7              # longer periods send every day's figures only when day by day was asked for
DIRECTION_DAYS = 365            # how far back wind direction is counted (from cached 5-minute readings)
MAX_REFINE_WINDOWS = 4          # overall records
MAX_MONTH_REFINE_WINDOWS = 24
# Periods up to this long are built from local-day-aligned data, so each month's low/high and its
# date are exact even at month boundaries. Longer periods use daily data (10am-10am buckets), unless
# the cache already holds the 30-minute data (the archive does), in which case up to a year is detailed.
DETAILED_DAYS = 93
# Slow readings, by "group.field": a 5-minute line of these is lightly smoothed (wind, rain and the rest are not)
SMOOTH_SERIES = {f"{g}.{f}" for g in ("outdoor", "indoor") for f in ("temperature", "humidity", "dew_point", "feels_like")} | {
    "pressure.relative", "pressure.absolute"}
MAX_ROWS = 500_000              # readings loaded per question from the cache: days x 48 x groups x FIELDS_PER_GROUP
FIELDS_PER_GROUP = 12           # about how many fields (with lows and highs) a group has
# Fields Ecowitt derives from the others (feels-like, dew point ...): left out of results unless the question asks for them
# (or charts one), so an "averaged data" note can't be misapplied to them.
DERIVED = ("feels_like", "app_temp", "app_tempin", "dew_point", "vpd")


def consecutive(days: list[date]) -> list[list[date]]:
    """Sorted days in runs of consecutive ones: [1, 2, 3, 5, 6] -> [[1, 2, 3], [5, 6]]."""
    runs: list[list[date]] = []
    for day in days:
        if runs and day - runs[-1][-1] == timedelta(days=1):
            runs[-1].append(day)
        else:
            runs.append([day])
    return runs


def chart_field(args: dict, turn) -> str | None:
    """The one reading a chart is asked for (the person's words, else the model's chart_field) as the field it plots, however it
    is written ("UV" and "uvi" -> "uvi", "wind gust" -> "wind_speed": wind is its speed shaded up to the gusts); None if none."""
    named = str(turn.chart_field or args.get("chart_field") or "").strip().lower().replace(" ", "_")
    return field_of(named) if named else None


def stack_names(args: dict, turn) -> list[str]:
    """The readings to put side by side (from the person's words, else the model's chart_fields), two or more."""
    raw = turn.chart_fields or args.get("chart_fields") or []
    names = [n for n in dict.fromkeys(str(x).strip().lower() for x in raw) if n in WEATHER]
    return names if len(names) >= 2 else []


class Target(NamedTuple):
    """An extreme that _refine looks for at a finer time: the period's record ("all") or a month's."""
    scope: str            # "all" or "month"
    key: str              # "group.field"
    want: str             # "low" or "high"
    month: str = ""       # a month's record: its row in the breakdown


# ---------- one history question ----------
class HistoryQuery:
    """Answers one question: fetch, find the extremes, refine them, and build the compact
    result (and chart) for the model."""

    def __init__(self, fetcher: Fetcher, args: dict, turn):
        self.f, self.tz, self.args, self.turn = fetcher, fetcher.tz, args, turn
        self.now = now_local(self.tz)
        self.store: dict = {}       # "group.field" -> {"unit", "pts"}, filled by _fetch_period
        self.start = self.end = self.span = None          # the period, set by run()
        self.held = False                                  # the whole period is in the cache at 30 minutes, and fits the row budget
        self.detailed = False                              # local-day-aligned data available for the whole period
        self.overall: dict = {}                            # key -> {"low": Ext, "high": Ext}
        self.monthly: dict = {}                            # key -> {month: {"low": Ext, "high": Ext}}
        self.direction: dict = {}                          # "wind.wind_direction" -> its summary (never low/high)
        self.rain_bars = Bars("Rain", "mm", [], [], 86400, "day")   # for a stacked chart with rain
        self.rain_from_daily = False                       # some of its days come from daily records (older than the 30-minute readings)
        self.compass: Compass | None = None                # the wind rose (counts per compass point by speed), drawn beside the wind chart
        self.wind: Fetcher | None = None                   # the wind rose's own (5-minute readings), made when wind direction is counted

    @property
    def by_day(self) -> bool:
        """The answer breaks the period down by day (up to FINE_DAYS), else by month."""
        return self.span <= timedelta(days=FINE_DAYS)

    async def run(self) -> str:
        try:
            return await self._run()
        finally:   # for the reply's timing line
            self.turn.times["data"] = self.turn.times.get("data", 0.0) + self.f.seconds + (self.wind.seconds if self.wind else 0.0)

    async def _run(self) -> str:
        today = self.now.date()
        try:
            start = parse_local(self.args.get("start_date") or today, self.tz)
            end = parse_local(self.args.get("end_date") or self.now, self.tz)
        except ValueError as e:
            return f"Error: bad date ({e}). Use 'YYYY-MM-DD HH:MM:SS'."
        self.start = max(start, datetime.combine(kept_from("1day", today), time()))
        self.end = min(end, self.now)
        if self.start >= self.end:
            return f"Error: start_date must be before end_date and within the last {CYCLES['1day'].kept // 365} years."
        self.span = self.end - self.start
        self.held = await self._held_locally()
        self.detailed = self.held or self.span <= timedelta(days=DETAILED_DAYS)

        await self._fetch_period()
        await self._fine_ranges()
        await self._live_bands()
        if "rain" in stack_names(self.args, self.turn) or "rainfall.daily" in self.store:   # rain alone is drawn as bars too
            self.rain_bars = await self._rain_bars()
        await self._summarise_direction()
        if not self.store and not self.direction:
            await self._log_empty()
            return "Error: no history data returned." + (
                " Details: " + "; ".join(self.f.errors[:5]) if self.f.errors else "")
        self.overall = {k: ex for k, s in self.store.items() if len(ex := series_extremes(s)) == 2}
        self.monthly = {} if self.by_day else self._monthly_extremes()
        refined = await self._refine()
        return self._answer(refined)

    async def _log_empty(self):
        """Say why a query found nothing: what was asked, where it was answered from, and what the cache holds for the window."""
        f = self.f
        lo, hi = f.epoch(self.start), f.epoch(self.end)
        try:
            held = {c: sum([await asyncio.to_thread(f.cache.count, f.mac, c, g, lo, hi) for g in f.groups]) for c in ("5min", "30min", "1day")}
        except Exception as e:
            held = f"unreadable ({e})"
        log.warning("History query empty: %s to %s (tz %s, epoch %d-%d), groups %s, %d ranges, %d from cache, %d from memory, %d requests, "
                    "%d failed%s, cache %s, readings held for the window %s", f"{self.start:%Y-%m-%d %H:%M}", f"{self.end:%Y-%m-%d %H:%M}",
                    getattr(self.tz, "key", self.tz), lo, hi, ",".join(f.groups), f.ranges, f.from_cache, f.from_memory, f.calls,
                    len(f.errors), f" ({f.errors[0]})" if f.errors else "", getattr(f.cache, "path", "?"), held)

    async def _fine_ranges(self):
        """30-minute readings with no low and high of their own get them from the cached 5-minute readings (store.slot_ranges),
        so their bands are real ranges. Wind keeps its own band (the speed up to the gusts)."""
        lo, hi = self.f.epoch(self.start), self.f.epoch(self.end)
        wanted = {}
        for key, series in self.store.items():
            group, field = key.split(".", 1)
            pts = series["pts"]
            if derives_range(field) and pts and not any(r["cycle"] != "30min" or "low" in r or "high" in r for r in pts.values()):
                wanted[key] = (group, field, {t: r["value"][0] for t, r in pts.items() if "value" in r})
        ranges = await self.f.read(lambda: {key: self.f.cache.slot_ranges(self.f.mac, group, field, values, lo, hi)
                                            for key, (group, field, values) in wanted.items()})
        for key, (lows, highs) in ranges.items():
            pts = self.store[key]["pts"]
            for t in lows:
                pts[t]["low"], pts[t]["high"] = (lows[t], str(lows[t])), (highs[t], str(highs[t]))

    async def _live_bands(self):
        """For a chart: the lowest and highest of the live readings the bot saw (live.LiveRanges) shade it as Home Assistant's are: a
        5-minute reading gets those of its five minutes, a 30-minute slot those of its half hour (the ranges rolled up, so its band holds the
        true extremes; Ecowitt's own range, or the lowest and highest of the 5-minute values, is only ever widened). A reading with none
        (before the bot was watching, or too few readings) keeps what it has, and a series the station itself gave a 5-minute range keeps it."""
        if self.f.live is None or not wants_chart(self.args, self.turn, self.start, self.end):   # (only a chart shows a band: no cost to the other answers)
            return
        drawn = {field_of(chart_field(self.args, self.turn) or "temperature"), *(WEATHER[n].field for n in stack_names(self.args, self.turn))}
        lo, hi = self.f.epoch(self.start) - BUCKET, self.f.epoch(self.end) + BUCKET
        for key, series in self.store.items():
            if key.split(".", 1)[1] not in drawn or not derives_range(key.split(".", 1)[1]):
                continue
            fine = {t: r for t, r in series["pts"].items() if r["cycle"] == "5min" and "value" in r and "low" not in r and "high" not in r}
            slots = {t: r for t, r in series["pts"].items() if r["cycle"] == "30min" and "value" in r}
            if fine:
                found = await self.f.read(self.f.live.between, key, lo, hi)
                for t, r in fine.items():
                    if got := found.get(round(t / BUCKET) * BUCKET):
                        self._widen(r, *got)
            if slots:
                found = await self.f.read(self.f.live.rolled, key, lo, hi, SLOT)
                for t, r in slots.items():
                    if got := found.get(t // SLOT * SLOT):
                        self._widen(r, *got)

    @staticmethod
    def _widen(rec: dict, low: float, high: float):
        """The reading's range taken out to these (and to its own value and range, where it has them)."""
        value = rec["value"][0]
        low, high = min(low, value, rec["low"][0] if "low" in rec else value), max(high, value, rec["high"][0] if "high" in rec else value)
        rec["low"], rec["high"] = (low, str(low)), (high, str(high))

    async def _held_locally(self) -> bool:
        """Is the whole period already in the cache at 30 minutes (bar the newest day, which is still settling), and
        small enough to load? Then any length is read from it at no cost in requests."""
        f = self.f
        if (self.span.days + 1) * 48 * len(f.groups) * FIELDS_PER_GROUP > MAX_ROWS:
            return False
        return await f.covered("30min", f.epoch(self.start), f.epoch(self.end - timedelta(days=1)))

    async def _read(self, cycle: str, t: datetime, until: datetime):
        collect(self.store, await self.f.get(cycle, t, until), cycle)

    async def _fetch_period(self):
        """Coarse pass: the best data for the whole period at the fewest requests."""
        f, start, end, today = self.f, self.start, self.end, self.now.date()
        thirty_floor = datetime.combine(kept_from("30min", today), time())
        recent = datetime.combine(today - timedelta(days=6), time())
        if self.span <= timedelta(hours=MINUTE_HOURS) and (minutes := await f.minutes(start, end)):
            collect(self.store, minutes, MINUTE)    # a short stretch: a reading a minute (Home Assistant's), straight from it
        elif self.span <= timedelta(days=1) and (start.date() >= kept_from("5min", today) or await f.covered(
                "5min", f.epoch(start), f.epoch(end))):
            collect(self.store, await f.get("5min", start, end), "5min")
        elif self.span <= timedelta(days=FINE_DAYS) and end > thirty_floor:
            # Resolution follows the length: a few days at 5 minutes (where archived, free from the cache),
            # up to a month at 30 minutes (one request per week, cached afterwards)
            for a, b in spans("30min", max(start, thirty_floor), end):
                five = self.span <= timedelta(days=INTRADAY_DAYS) and await f.covered("5min", f.epoch(a), f.epoch(b))
                collect(self.store, await f.get("5min" if five else "30min", a, b), "5min" if five else "30min")
            if start < thirty_floor:
                await self._read("1day", start, min(end, thirty_floor - timedelta(seconds=1)))
        elif self.held:
            await self._read("30min", start, end)  # held in the cache: exact local days, true lows and highs, no requests
        else:
            # Longer: Ecowitt's daily records (each has its own low and high, and it is a request or two, cached),
            # plus the newest days at 30 minutes so the line reaches today. Exact times come from refining the extremes.
            await self._read("30min", max(start, recent), end)
            await self._read("1day", start, min(end, recent - timedelta(seconds=1)))

    async def _summarise_direction(self):
        """Wind direction leaves the low/high pipeline: it is counted by compass point instead. Recent
        days are counted from 5-minute readings where the cache has them (averaging degrees is wrong
        near north); older parts of a long period are left out rather than counted from daily averages.
        Calm readings (no wind speed) are dropped: a sensor with nothing to point at repeats its last direction."""
        keys = [k for k in self.store if k.endswith(".wind_direction")]
        if not keys:
            return
        f = self.f
        wind = self.wind = f.sibling(["wind"])
        first = max(self.start, self.end - timedelta(days=DIRECTION_DAYS))
        bounds = {}   # each day of the period: its part of it, local times
        for n in range((self.end.date() - first.date()).days + 1):
            day = first.date() + timedelta(days=n)
            a, b = max(first, datetime.combine(day, time())), min(self.end, datetime.combine(day, time(23, 59, 59)))
            if a < b:
                bounds[day] = a, b
        held = await wind.covered_each("5min", [(wind.epoch(a), wind.epoch(b)) for a, b in bounds.values()])
        fine_days = {day for day, whole in zip(bounds, held)   # today's (or a settling yesterday's) comes from memory or one request
                     if whole or day == self.end.date() and (self.now.date() - day).days <= 1}
        fine: dict = {}
        for run in consecutive(sorted(fine_days)):   # a run of days is one read
            collect(fine, await wind.get("5min", bounds[run[0]][0], bounds[run[-1]][1]), "5min")
        for key in keys:
            pts = self.store.pop(key)["pts"]
            speed_key = key[:-len("wind_direction")] + "wind_speed"
            speeds = {t: r["value"][0] for src in (self.store, fine) for t, r in src.get(speed_key, {}).get("pts", {}).items()
                      if "value" in r}
            readings = {t: (r["value"][0], r["cycle"] == "5min") for t, r in pts.items()
                        if "value" in r and r["cycle"] in ("5min", "30min") and local_date(t, self.tz) not in fine_days}
            readings.update({t: (r["value"][0], True) for t, r in fine.get(key, {}).get("pts", {}).items() if "value" in r})
            counted = [(t, d, exact) for t, (d, exact) in sorted(readings.items()) if first <= f.local(t)]
            calm = [x for x in counted if speeds.get(x[0], 1) <= 0]
            counted = [x for x in counted if speeds.get(x[0], 1) > 0]
            result = summarise_direction(counted, self.tz, self.by_day, len(calm))
            if not result:
                continue
            self.compass = Compass(direction_rose([(t, d, x, speeds.get(t)) for t, d, x in counted]),
                                   any(t in speeds for t, _, _ in counted), tuple(SPEED_STEPS))
            if self.start < first:
                result["note_period"] = f"covers only the last {DIRECTION_DAYS} days of the period"
            self.direction[key] = result

    def _monthly_extremes(self) -> dict:
        """key -> {month label: {"low": Ext, "high": Ext}}"""
        out = {}
        for key, series in self.store.items():
            months: dict = {}
            until = None   # the end of the local day the reading falls in (epoch), its month worked out once a day
            for ts, rec in sorted(series["pts"].items()):
                if until is None or ts >= until:
                    day = local_date(ts, self.tz)
                    month, until = self._label(day), day_bounds(day, self.tz)[1]
                fold(months.setdefault(month, {}), ts, rec)
            out[key] = months
        return out

    async def _refine(self) -> int:
        """Fetch exactly each extreme's window (a 30-minute slot or a UTC day) at the finest cycle
        available, for the real time and value. Overall records first (temperatures first), then,
        for periods of up to a few months, each month's temperature records. A second pass narrows
        a 30-minute slot to archived 5-minute data. Returns the number of windows fetched."""
        f = self.f
        slots: dict[Target, Ext] = {}
        for key in sorted(self.overall, key=lambda k: "temperature" not in k):
            for want in ("low", "high"):
                slots[Target("all", key, want)] = self.overall[key][want]
        if self.monthly and self.detailed:
            for key, months in self.monthly.items():
                if key.endswith(".temperature"):
                    for month, d in reversed(list(months.items())):  # newest first: where 5-minute data is
                        for want, ext in d.items():
                            slots[Target("month", key, want, month)] = ext
        caps = {"all": MAX_REFINE_WINDOWS, "month": MAX_MONTH_REFINE_WINDOWS}
        refined = 0
        for _ in range(2):
            windows: dict = {}  # (cycle, start_ts, end_ts) -> [Target]
            used = {"all": 0, "month": 0}
            finer_of: dict = {}  # slots on the same bucket share an answer; a new pass looks again (data was fetched)
            for target, ext in slots.items():
                w_end = ext.ts + cycle_seconds(ext.cycle) - 1
                if (ext.ts, ext.cycle) not in finer_of:
                    finer_of[ext.ts, ext.cycle] = await f.finer_cycle(ext.ts, ext.cycle, w_end, self.now.date())
                finer = finer_of[ext.ts, ext.cycle]
                if not finer:
                    continue
                w = (finer, ext.ts, w_end)
                if w not in windows:
                    if used[target.scope] >= caps[target.scope]:
                        continue
                    used[target.scope] += 1
                    windows[w] = []
                windows[w].append(target)
            if not windows:
                break
            refined += len(windows)
            for (cycle, w_start, w_end), targets in windows.items():
                fine: dict = {}
                collect(fine, await f.get(cycle, f.local(w_start), min(f.local(w_end), self.now)), cycle)
                for target in targets:
                    found = series_extremes(fine[target.key], w_start, w_end).get(target.want) if target.key in fine else None
                    if not found:
                        continue
                    cur = slots[target]
                    keep = cur if better(target.want, cur.value, found.value) else found  # the more extreme value...
                    slots[target] = Ext(keep.value, keep.raw, found.ts, found.cycle, keep.exact)  # ...at the finer time
        for target, ext in slots.items():
            if target.scope == "all":
                self.overall[target.key][target.want] = ext
            else:
                self.monthly[target.key][target.month][target.want] = ext
        return refined

    def _answer(self, refined: int) -> str:
        f = self.f
        wanted = self.args.get("include_derived") or []
        wanted = {wanted} if isinstance(wanted, str) else set(wanted)
        asked = [chart_field(self.args, self.turn), *(WEATHER[n].field for n in stack_names(self.args, self.turn))]
        wanted |= {f for f in asked if f in DERIVED}   # charting one of them brings it into the result
        if "app_temp" in wanted:
            wanted.add("app_tempin")  # indoor's name for apparent temperature
        series_out = {key: self._result(key, ext) for key, ext in self.overall.items()
                      if key.split(".", 1)[-1] not in DERIVED or key.split(".", 1)[-1] in wanted}  # the rest: cached, not sent
        series_out.update(self.direction)
        shown = {k: v for k, v in series_out.items() if self._in_focus(k)}   # what the model is sent (charts use all of series_out)
        log.debug("%s..%s: %d reads (%d cache, %d mem), %d req%s, %d refined",
                f"{self.start:%m-%d}", f"{self.end:%m-%d}", f.ranges, f.from_cache, f.from_memory, f.calls,
                f", {len(f.errors)} failed" if f.errors else "", refined)
        out = {"period": f"{day_text(self.start)} - {day_text(self.end)}", "series": shown if shown else series_out}
        if self.monthly and not self.detailed:
            out["monthly_note"] = (f"Monthly figures for long periods come from daily data that runs "
                                   f"{daily_run(self.tz, self.start.date(), self.end.date())}, so they have no dates, and a low "
                                   "early on the 1st may be counted in the previous month.")
        if wants_chart(self.args, self.turn, self.start, self.end):
            self._add_charts(out, {k: v for k, v in series_out.items() if k in self.store})
        if errors := f.errors + (self.wind.errors if self.wind else []):
            out["missing"] = errors[:10]
            out["warning"] = "Some data could not be fetched; the answer may be incomplete. Say so."
        return json_result(out)

    def _in_focus(self, key: str) -> bool:
        """Is this series wanted by the question? Outdoor and indoor come with both temperature and humidity: when the words name
        only one of them, the other is left out of what the model reads (everything else is kept)."""
        field = key.split(".", 1)[-1]
        named = self.turn.readings
        return not named or field not in ("temperature", "humidity") or field in named

    def _result(self, key: str, ext: dict) -> dict:
        """One series as the model sees it: its records with when they happened, and a daily or monthly breakdown."""
        tz = self.tz
        entry = {"unit": self.store[key]["unit"]}
        for want in ("low", "high"):
            e = ext[want]
            entry[want] = e.raw
            entry[f"{want}_time"], entry[f"{want}_when"], entry[f"{want}_date"] = describe_time(e.ts, e.cycle, tz)
            if not e.exact:
                entry[f"{want}_note"] = "from averaged data; the real value may be more extreme"
        if self.by_day:
            days: dict = {}
            for ts, rec in self.store[key]["pts"].items():
                if rec["cycle"] != "1day":  # sub-daily points give correct local days
                    fold(days.setdefault(local_date(ts, tz), {}), ts, rec)
            keep = None if len(days) <= DAILY_ALL_DAYS or self.turn.per_day else {   # a long period: the days of its records, not all of them
                self._label(local_date(ext[w].ts, tz)) for w in ("low", "high")}
            entry["daily"] = {self._label(d): {w: e.raw for w, e in days[d].items()} for d in sorted(days)
                              if keep is None or self._label(d) in keep}
        else:
            entry["monthly"] = {}
            for month, d in self.monthly.get(key, {}).items():
                row = entry["monthly"][month] = {w: e.raw for w, e in d.items()}  # long periods: values only, to keep it small
                if self.detailed:  # each month's low/high with when and date
                    for w, e in d.items():
                        _, row[f"{w}_when"], row[f"{w}_date"] = describe_time(e.ts, e.cycle, tz)
        if (self.args.get("average") or self.turn.average_asked) and not key.startswith("rainfall"):  # a rain total has no mean
            self._add_averages(entry, self._daily_means(self.store[key]["pts"]))
        return entry

    def _add_charts(self, out: dict, plottable: dict):
        """The chart for this answer: the readings side by side, else one line per group; a compass goes beside the wind."""
        holder = self.turn.charts
        spec = self._stack_spec(names) if (names := stack_names(self.args, self.turn)) else None
        stacked = spec is not None
        if spec:
            out["rain_total_mm"] = round(sum(self.rain_bars.y), 1) if self.rain_bars.x else None
            holder.append(spec)
            out["chart"] = STACK_CHART_HINT
        else:
            spec = self._chart_spec(plottable) if plottable else None
            if spec:
                holder.append(spec)
                if spec.panels[0].bars:
                    out["rain_total_mm"] = round(sum(self.rain_bars.y), 1)
                    out["chart"] = STACK_CHART_HINT
                else:
                    out["chart"] = AVERAGE_CHART_HINT if self.turn.average_asked else CHART_HINT
        if self.compass and not stacked:  # wind direction was counted: the compass goes beside the wind speed line (a stack has no room for it)
            wind = spec if spec and spec.title == "Wind" else self._chart_spec(plottable, "wind")
            if wind:
                wind.compass = self.compass
                if wind is not spec:
                    holder.append(wind)
                out["chart"] = (CHART_HINT + " For wind direction, give the most common direction, not a high and low."
                                if spec and wind is not spec else DIRECTION_CHART_HINT)

    def _label(self, day: date) -> str:
        """The row a day belongs to in the breakdown, in the one form every table of it uses: the day ("Sat 3 Oct"), or its month
        ("Oct 2026") when the period is broken down by month."""
        return day_text(day, year=False) if self.by_day else f"{day:%b %Y}"

    def _daily_means(self, pts: dict) -> dict:
        """{local date: mean of that day's readings}: from 5- or 30-minute readings where held, else the daily bucket."""
        means = {local_date(ts, self.tz): rec["value"][0] for ts, rec in pts.items() if rec.get("cycle") == "1day" and "value" in rec}
        means.update({d: mean for d, (mean, _, _) in daily_summary(daily_readings(pts), self.tz).items()})  # exact local days win over 10am-10am buckets
        return means

    def _add_averages(self, entry: dict, means: dict):
        """The mean over the period, and per row of the breakdown (each day, or each month): the answer to "what was the average"."""
        if not means:
            return
        fmt = lambda v: f"{v:.1f}"
        entry["average"] = fmt(fmean(means.values()))
        table = entry.get("daily" if self.by_day else "monthly")
        for d, v in means.items():
            if table is not None and (label := self._label(d)) in table:
                table[label].setdefault("_sum", []).append(v)
        for table in (entry.get("daily"), entry.get("monthly")):
            for row in (table or {}).values():
                values = row.pop("_sum", None)
                if values:
                    row["avg"] = fmt(fmean(values))

    def _chart_spec(self, series_out: dict, field: str | None = None) -> Chart | None:
        """Line chart: one line per group for the field asked about (`field`, else chart_field; temperature by default,
        else the first field), at the finest resolution fetched for the whole period (5- or 30-minute readings,
        or daily averages for long periods), plus the true record high and low with their times."""
        asked = field_of(field) if field else chart_field(self.args, self.turn)
        wanted = asked or "temperature"
        keys = [k for k in series_out if k.endswith("." + wanted)]
        if not keys and (wanted in DERIVED or asked and find_name(wanted)):  # asked for on its own and not there: no chart, rather than a temperature one
            return None
        keys = keys or [k for k in series_out if k.endswith(".temperature")]
        if not keys:  # nothing to match: the wind chart when wind direction was counted, else the first field
            field = "wind_speed" if self.compass and any(k.endswith(".wind_speed") for k in series_out) else next(iter(series_out)).split(".", 1)[-1]
            keys = [k for k in series_out if k.endswith("." + field)]
        if keys[0] == "rainfall.daily" and self.rain_bars.x:  # the day's counter is a running total: draw what fell, as columns
            return Chart("Rain", f"{period_text(self.start.date(), self.end.date())}  ·  rain per {self.rain_bars.per}"
                                 + (f"  ·  older days from daily records ({daily_run(self.tz, self.start.date(), self.end.date())})"
                                    if self.rain_from_daily else ""),
                         [panel_for("rain", bars=self.rain_bars)], period=(self.start.date(), self.end.date()))
        field = keys[0].split(".", 1)[-1]
        unit = series_out[keys[0]]["unit"].replace("º", "°")
        lines, resolution, ranged = [], None, False
        banded = False
        for k in keys:
            got = self._series_entry(k)
            if got is None:
                continue
            line, cycle = got
            resolution = resolution or cycle
            band = self._band_key(k)  # a mean shaded up to another series (wind: the gusts): its records are that series'
            banded = banded or band is not None
            rec = self.overall.get(band or k, {})
            line.records = {w: (rec[w].ts, rec[w].value) for w in ("low", "high") if w in rec and (w == "high" or not band)}
            ranged = ranged or line.low is not None
            lines.append(line)
        if not lines:
            return None
        group = keys[0].split(".", 1)[0]
        if len(lines) == 1 and group not in ("outdoor", "indoor") and (reading := find(field)):
            lines[0].label = reading.label   # "UV index", not the group's name ("Solar and uvi"); outdoor and indoor keep theirs: they tell the lines apart
        title = field.replace("_", " ").capitalize()
        subtitle = (f"{period_text(self.start.date(), self.end.date())}  ·  {resolution}"
                    + (", shaded up to the gusts" if banded and ranged else ", range shaded" if ranged else "")
                    + ("  ·  records marked" if any(x.records for x in lines) and not banded else ""))
        panel = panel_for(name, lines) if (name := find_name(field)) else Panel(title, unit, lines)
        return Chart(panel.label, subtitle, [panel], period=(self.start.date(), self.end.date()))

    def _band_key(self, k: str) -> str | None:
        """The series this one is shaded up to, if it is a reading with one (wind: the gusts) and that series was fetched."""
        group, field = k.split(".", 1)
        reading = find(field)
        key = f"{group}.{reading.band_field}" if reading and reading.band_field and field == reading.field else None
        return key if key in self.store else None

    def _series_readings(self, k: str) -> list[Sample]:
        """One series as readings for lines.build_line. A reading with a band series (wind) is its mean shaded up to that series."""
        band = self._band_key(k)
        gust = self.store[band]["pts"] if band else {}
        out = []
        for t, r in self.store[k]["pts"].items():
            if "value" in r:
                v = r["value"][0]
                low, high = (r["low"][0] if "low" in r else None), (r["high"][0] if "high" in r else None)
                if band:
                    low, high = low_of(r), max(high_of(gust[t]) if t in gust and "value" in gust[t] else high_of(r), v)
                out.append(Sample(t, v, low, high, cycle_seconds(r["cycle"])))
        return out

    def _series_entry(self, k: str) -> tuple[Line, str] | None:
        """One series as a chart line and how it was drawn; None if there is too little to draw."""
        line = build_line(self._series_readings(k), self.tz, self.span.total_seconds(), smooth=k in SMOOTH_SERIES,
                          force_daily=self.turn.average_asked, until=now_local(self.tz).date())
        if line is None:
            return None
        group = k.split(".", 1)[0]
        return line.spec(group.replace("_", " ").capitalize(), indoor=group == "indoor"), line.name

    async def _rain_bars(self) -> Bars:
        """Rain for the chart, summed into bars. From the cached 30-minute readings and, after the last of them, the query's own
        (the newest hours are not in the cache yet: they are still settling); days older than those (about a year) come from
        the daily records, the day's total, which are approximate (10am to 10am). A figure per month or year asked for in the
        person's words (`by`) gets one bar for each, whatever the length of the period."""
        lo, hi = self.f.epoch(self.start), self.f.epoch(self.end)
        (daily,) = await self.f.read(self.f.cache.slots, self.f.mac, "30min", "rainfall", ["daily"], lo, hi)
        after = max(daily) + SLOT if daily else lo   # the cache stops at the settle horizon: what is newer, this query read itself
        daily.update({t: r["value"][0] for t, r in self.store.get("rainfall.daily", {}).get("pts", {}).items()
                      if t >= after and r["cycle"] in ("5min", "30min") and "value" in r})
        rain = rain_slots(daily)
        held = {local_date(t, self.tz) for t in rain}
        (old,) = await self.f.read(self.f.cache.slots, self.f.mac, "1day", "rainfall", ["daily"], lo - 86400, hi + 86400)
        for t, mm in old.items():   # a daily bucket is dated by the local day its 10am start falls on
            day = local_date(t, self.tz)
            if day not in held and self.start.date() <= day <= self.end.date():
                rain[day_bounds(day, self.tz)[0] + 12 * 3600] = mm
                self.rain_from_daily = True
        by = None if self.by_day else grouping_in(self.turn.text)
        return rain_bars(rain, self.tz, self.start.date(), self.end.date(), until=hi, by=by, since=lo)

    def _stack_spec(self, names: list[str]) -> Chart | None:
        """The readings asked for, one panel each on a shared time axis (rain behind the first line); None if fewer than
        two can be drawn."""
        panels = []
        for name in names:
            reading = WEATHER[name]
            group, field, label = reading.group, reading.field, reading.label
            if name == "rain":
                if self.rain_bars.x:
                    panels.append(panel_for("rain", bars=self.rain_bars))
                continue
            lines = [e[0] for k in sorted(self.store) if k.split(".", 1)[-1] == field and (
                     group == "outdoor" and k.split(".")[0] in ("outdoor", "indoor") or k.startswith(group + "."))
                     and (e := self._series_entry(k)) is not None]
            if lines:
                if len(lines) == 1:
                    lines[0].label = label
                panels.append(panel_for(name, lines))
        if len(panels) < 2:
            return None
        if (self.turn.two_axes or self.args.get("two_axes")) and (merged := two_axes(panels)):   # one panel, a scale each
            panels = [merged]
        return stack(panels, self.start.date(), self.end.date())
