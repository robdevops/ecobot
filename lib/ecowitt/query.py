"""Ecowitt history: fetched within Ecowitt's limits, then turned into a compact answer.

The readings drawn are the period's at the resolution its length calls for: up to 31 days local-day-aligned data (archived 5-minute
where the storage has it, else 30-minute: one request per week); longer, the 30-minute readings where the storage holds them all, else
Ecowitt's daily records (10am to 10am in Melbourne: UTC days) and the newest days at 30 minutes. The records, the breakdown by day or by
month and the averages come from the period's days instead (period.py): each whole day's row, worked out from the finest readings held
for it, so a record's time is as close as those readings give it, and the part of a day at either end from its own readings.
"""

import logging
from datetime import date, datetime, time, timedelta
from statistics import fmean

from ..captions import AVERAGE_CHART_HINT, CHART_HINT, STACK_CHART_HINT, DIRECTION_CHART_HINT, wants_chart
from ..lines import DAY, Plotted, Sample, build_line, how_drawn, shaded_up_to
from ..daytable import grouping_in
from ..rain import rain_bars, rain_slots
from ..timeutil import at_hour, day_bounds, day_text, local_date, now_local, parse_local, SLOT
from ..storage.catalogue import text
from ..storage.days import utc_days
from ..tools import json_result
from ..series import WEATHER, field_of, find, find_name
from ..panels import panel_for, two_axes
from ..specs import Bars, Chart, Compass, Line, Panel, period_text, stack
from .api import CYCLES, MINUTE, cycle_seconds, daily_run, kept_from
from .direction import SPEED_STEPS, rose as direction_rose, summarise as summarise_direction
from .extremes import describe_time
from .fetch import Fetcher, spans
from .period import Period, extremes
from .reads import Held, Store, counts_on, first_minute_on, history_on

log = logging.getLogger(__name__)

# A chart of up to MINUTE_HOURS is drawn a reading a minute (Home Assistant's "1min"; Ecowitt's cloud has no such cycle). Rather than ask
# Home Assistant for them at every chart, the bot takes them as they come (at start for the last KEEP_HOURS, then at each new live reading:
# source.pull_minutes) into the storage's 1min rows, where they are kept ten days (storage.write.MINUTES_KEPT).
MINUTE_HOURS = 8                # a chart of up to this long is drawn from minutes
KEEP_HOURS = MINUTE_HOURS + 1   # ... and may end an hour ago: what is older is no use
SLACK_SECONDS = 300             # the first minute held may be this much after a chart's start (the chart's first point is then a little late)
INTRADAY_DAYS = 8               # up to this many days: 5-minute readings where archived
FINE_DAYS = 31                  # up to this many: 30-minute readings and a breakdown by day; longer periods use daily records, by month
DAILY_ALL_DAYS = 7              # longer periods send every day's figures only when day by day was asked for
DIRECTION_DAYS = 365            # how far back wind direction is counted (from cached 5-minute readings)
# Slow readings, by "group.field": a 5-minute line of these is lightly smoothed (wind, rain and the rest are not)
SMOOTH_SERIES = {f"{g}.{f}" for g in ("outdoor", "indoor") for f in ("temperature", "humidity", "dew_point", "feels_like")} | {
    "pressure.relative", "pressure.absolute"}
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


# ---------- one history question ----------
class HistoryQuery:
    """Answers one question: fetch, find the records from the period's days, and build the compact result (and chart) for the model."""

    def __init__(self, fetcher: Fetcher, args: dict, turn):
        self.f, self.tz, self.args, self.turn = fetcher, fetcher.tz, args, turn
        self.now = now_local(self.tz)
        self.units: dict[str, str] = {}                    # "group.field" -> its unit: the series of the period
        self.period: dict[str, Period] = {}                # "group.field" -> its days in the period and its records over them
        self.store: Store = {}      # "group.field" -> {"unit", "pts": {epoch: Sample}}: its readings, where a line or a count needs them
        self.drawing = ("none", 0)  # how the chart's lines are drawn (_drawing)
        self.lines: dict[str, Plotted] = {}                # the lines the database bucketed
        self.start = self.end = self.span = None          # the period, set by run()
        self.held = False                                  # the whole period is in the cache at 30 minutes: local days, so each month's records are dated
        self.overall: dict = {}                            # key -> {"low": Ext, "high": Ext}
        self.monthly: dict = {}                            # key -> {month: {"low": Ext, "high": Ext}}
        self.direction: dict = {}                          # "wind.wind_direction" -> its summary (never low/high)
        self.rain_bars = Bars("Rain", "mm", [], [], 86400, "day")   # for a stacked chart with rain
        self.rain_from_daily = False                       # some of its days come from daily records (older than the 30-minute readings)
        self.compass: Compass | None = None                # the wind rose (counts per compass point by speed), drawn beside the wind chart
        self.wind: Fetcher | None = None                   # the wind rose's own (5-minute readings), made when the wind is asked for

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

        f = self.f
        plan = await self._hold_period()
        first, fine_days, runs = await self._hold_wind() if "wind" in f.groups else (self.end, set(), [])
        chart = wants_chart(self.args, self.turn, self.start, self.end)
        self.drawing = self._drawing(plan) if chart else ("none", 0)
        how, width = self.drawing
        fine = tuple(dict.fromkeys(cycle for cycle, _, _ in plan if cycle != "1day"))
        held = await f.stored(history_on, f.mac, f.groups, [(cycle, f.epoch(a), f.epoch(b)) for cycle, a, b in plan], f.epoch(self.start),
                              f.epoch(self.end), self.end >= self.now - timedelta(minutes=1), [(f.epoch(a), f.epoch(b)) for a, b in runs],
                              chart and "rainfall" in f.groups, how == "readings", (width, fine) if how == "buckets" else None)
        self.units, self.period, self.store, self.lines = held.units, held.period, held.store, held.lines
        if chart and ("rain" in stack_names(self.args, self.turn) or "rainfall.daily" in self.units):   # rain alone is drawn as bars too
            self.rain_bars = self._rain_bars(held)
        self._summarise_direction(held.wind, first, fine_days)
        if not self.units and not self.direction:
            await self._log_empty()
            return "Error: no history data returned." + (
                " Details: " + "; ".join(self.f.errors[:5]) if self.f.errors else "")
        self.overall = {k: {"low": p.low, "high": p.high} for k in self.units if (p := self.period[k]).low and p.high}
        self.monthly = {} if self.by_day else self._monthly_extremes()
        return self._answer()

    async def _log_empty(self):
        """Say why a query found nothing: what was asked, where it was answered from, and what the cache holds for the window."""
        f = self.f
        lo, hi = f.epoch(self.start), f.epoch(self.end)
        log.warning("History query empty: %s to %s (tz %s, epoch %d-%d), groups %s, %d ranges, %d from cache, %d from memory, %d requests, "
                    "%d failed%s, cache %s, readings held for the window %s", f"{self.start:%Y-%m-%d %H:%M}", f"{self.end:%Y-%m-%d %H:%M}",
                    getattr(self.tz, "key", self.tz), lo, hi, ",".join(f.groups), f.ranges, f.from_cache, f.from_memory, f.calls,
                    len(f.errors), f" ({f.errors[0]})" if f.errors else "", f.storage.path, await f.stored(counts_on, f.mac, f.groups, lo, hi))

    async def _held_locally(self) -> bool:
        """Is the whole period already in the cache at 30 minutes (bar the newest day, which is still settling)? Then any length is read
        from it at no cost in requests."""
        f = self.f
        return (await f.covered_each("30min", [(f.epoch(self.start), f.epoch(self.end - timedelta(days=1)))]))[0]

    async def _hold_period(self) -> list[tuple[str, datetime, datetime]]:
        """The readings the period is drawn from, at the fewest requests: (resolution, start, end) each, held (what the storage lacks of
        them asked of Ecowitt first). A short stretch by the minute, where the bot keeps them from its start (Home Assistant's); up to a
        day at 5 minutes; up to a month, a few days at 5 minutes where archived (free) and the rest at 30 (a request a week, kept after);
        longer, the 30-minute readings where the storage holds them all (exact local days, true lows and highs, no requests), else
        Ecowitt's daily records (each with its own low and high, a request or two) and the newest days at 30 minutes, so the line reaches
        today."""
        f, start, end, today = self.f, self.start, self.end, self.now.date()
        thirty_floor = datetime.combine(kept_from("30min", today), time())
        recent = datetime.combine(today - timedelta(days=6), time())
        if self.span <= timedelta(hours=MINUTE_HOURS) and f.has_minutes and (
                first := await f.stored(first_minute_on, f.mac, f.groups, f.epoch(start), f.epoch(end))) is not None \
                and first <= f.epoch(start) + SLACK_SECONDS:
            return [(MINUTE, start, end)]   # (kept by the bot: nothing to ask for)
        if self.span <= timedelta(days=1) and (start.date() >= kept_from("5min", today) or (await f.covered_each(
                "5min", [(f.epoch(start), f.epoch(end))]))[0]):
            plan = [("5min", start, end)]
        elif self.span <= timedelta(days=FINE_DAYS) and end > thirty_floor:
            plan = []
            for a, b in spans("30min", max(start, thirty_floor), end):
                five = self.span <= timedelta(days=INTRADAY_DAYS) and (await f.covered_each("5min", [(f.epoch(a), f.epoch(b))]))[0]
                plan.append(("5min" if five else "30min", a, b))
            if start < thirty_floor:
                plan.append(("1day", start, min(end, thirty_floor - timedelta(seconds=1))))
        elif self.held:
            plan = [("30min", start, end)]
        else:
            plan = [("30min", max(start, recent), end), ("1day", start, min(end, recent - timedelta(seconds=1)))]
        for cycle, a, b in plan:
            await f.get(cycle, a, b, load=False)
        return plan

    async def _hold_wind(self) -> tuple[datetime, set[date], list[tuple[datetime, datetime]]]:
        """The days the wind's direction is counted from 5-minute readings (averaging degrees is wrong near north): those the storage
        holds at 5 minutes, and today's (or a settling yesterday's: from memory or one request), held. (The first moment counted, the
        days, their runs as stretches of local time: a run is one read.)"""
        wind = self.wind = self.f.sibling(["wind"])
        first = max(self.start, self.end - timedelta(days=DIRECTION_DAYS))
        bounds = {}   # each day of the period: its part of it, local times
        for n in range((self.end.date() - first.date()).days + 1):
            day = first.date() + timedelta(days=n)
            a, b = max(first, datetime.combine(day, time())), min(self.end, datetime.combine(day, time(23, 59, 59)))
            if a < b:
                bounds[day] = a, b
        held = await wind.covered_each("5min", [(wind.epoch(a), wind.epoch(b)) for a, b in bounds.values()])
        fine_days = {day for day, whole in zip(bounds, held) if whole or day == self.end.date() and (self.now.date() - day).days <= 1}
        runs = [(bounds[run[0]][0], bounds[run[-1]][1]) for run in consecutive(sorted(fine_days))]
        for a, b in runs:
            await wind.get("5min", a, b, load=False)
        return first, fine_days, runs

    def _summarise_direction(self, fine: Store, first: datetime, fine_days: set[date]):
        """Wind direction leaves the low/high pipeline: it is counted by compass point instead, from 5-minute readings on the days
        _hold_wind gave (`fine`), else the period's own (5- or 30-minute: older parts of a long period are left out rather than counted from
        daily averages). Calm readings (no wind speed) are dropped: a sensor with nothing to point at repeats its last direction."""
        for key in [k for k in self.units if k.endswith(".wind_direction")]:
            self.units.pop(key)
            pts = self.store.pop(key, {}).get("pts", {})
            speed_key = key[:-len("wind_direction")] + "wind_speed"
            speeds = {t: s.value for src in (self.store, fine) for t, s in src.get(speed_key, {}).get("pts", {}).items()}
            readings = {t: (s.value, s.seconds == 300) for t, s in pts.items() if s.seconds in (300, 1800) and local_date(t, self.tz) not in fine_days}
            readings.update({t: (s.value, True) for t, s in fine.get(key, {}).get("pts", {}).items()})
            counted = [(t, d, exact) for t, (d, exact) in sorted(readings.items()) if first <= self.f.local(t)]
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
        """key -> {month label: {"low": Ext, "high": Ext}}: each month's records, from its days."""
        out = {}
        for key, p in self.period.items():
            if key in self.units:
                months: dict = {}
                for d in p.days:
                    months.setdefault(self._label(d.day), []).append(d)
                out[key] = {month: extremes(p.column, days) for month, days in months.items()}
        return out

    def _answer(self) -> str:
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
        log.debug("%s..%s: %d reads (%d cache, %d mem), %d req%s", f"{self.start:%m-%d}", f"{self.end:%m-%d}", f.ranges, f.from_cache,
                  f.from_memory, f.calls, f", {len(f.errors)} failed" if f.errors else "")
        out = {"period": f"{day_text(self.start)} - {day_text(self.end)}", "series": shown if shown else series_out}
        if self.monthly and not self.held:
            out["monthly_note"] = (f"Monthly figures for long periods come from daily data that runs "
                                   f"{daily_run(self.tz, self.start.date(), self.end.date())}, so they have no dates, and a low "
                                   "early on the 1st may be counted in the previous month.")
        if wants_chart(self.args, self.turn, self.start, self.end):
            self._add_charts(out, {k: v for k, v in series_out.items() if k in self.units})
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
        entry = {"unit": self.units[key]}
        for want in ("low", "high"):
            e = ext[want]
            entry[want] = e.raw
            entry[f"{want}_time"], entry[f"{want}_when"], entry[f"{want}_date"] = describe_time(e.ts, e.cycle, tz)
            if not e.exact:
                entry[f"{want}_note"] = "from averaged data; the real value may be more extreme"
        p = self.period[key]
        if self.by_day:
            days = [d for d in p.days if d.source != "1day"]   # exact local days (a daily record runs 10am to 10am)
            keep = None if len(days) <= DAILY_ALL_DAYS or self.turn.per_day else {   # a long period: the days of its records, not all of them
                self._label(local_date(ext[w].ts, tz)) for w in ("low", "high")}
            written = lambda v: text(v, p.column.decimals, p.column.whole)
            entry["daily"] = {self._label(d.day): {w: written(v) for w, v in (("low", d.lo), ("high", d.hi)) if v is not None} for d in days
                              if keep is None or self._label(d.day) in keep}
        else:
            entry["monthly"] = {}
            for month, d in self.monthly.get(key, {}).items():
                row = entry["monthly"][month] = {w: e.raw for w, e in d.items()}  # long periods: values only, to keep it small
                if self.held:  # each month's low/high with when and date
                    for w, e in d.items():
                        _, row[f"{w}_when"], row[f"{w}_date"] = describe_time(e.ts, e.cycle, tz)
        if (self.args.get("average") or self.turn.average_asked) and not key.startswith("rainfall"):  # a rain total has no mean
            self._add_averages(entry, {d.day: d.mean for d in p.days if d.mean is not None})
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
        return key if key in self.units else None

    def _series_readings(self, k: str) -> list[Sample]:
        """One series as readings for lines.build_line. A reading with a band series (wind) is its mean shaded up to that series."""
        band, pts = self._band_key(k), self.store[k]["pts"]
        if band is None:
            return list(pts.values())
        tops = shaded_up_to({t: s.bounds()[1] for t, s in pts.items()}, {t: s.bounds()[1] for t, s in self.store[band]["pts"].items()})
        return [s._replace(low=s.bounds()[0], high=max(tops[t], s.value)) for t, s in pts.items()]

    def _drawing(self, plan: list[tuple[str, datetime, datetime]]) -> tuple[str, int]:
        """How the chart's lines are drawn, as lines.build_line would draw the plan's readings (lines.how_drawn): ("readings", 0), the
        readings themselves, by build_line (Ecowitt's daily records alone too); ("buckets", width), in buckets the database works out;
        ("days", hour), a point a day from the period's days (at that hour where they are local days)."""
        how, at = how_drawn(self.span.total_seconds(), {cycle_seconds(cycle) for cycle, _, _ in plan if cycle != "1day"},
                            any(cycle == "1day" for cycle, _, _ in plan), self.turn.average_asked)
        return (how, at) if how in ("buckets", "days") else ("readings", 0)

    def _day_line(self, k: str, hour: int) -> Plotted | None:
        """One point a day from the period's days (as lines._daily): a day held finer than a daily record at `hour` (how_drawn's), its mean,
        lowest and highest; a daily record's at its own time (its 10am start); today and later not (the day is not over)."""
        if (p := self.period.get(k)) is None:
            return None
        points = {}
        for d in p.days:
            if d.day < self.now.date() and d.mean is not None:
                t = (utc_days(d.day, self.tz) or (None,))[0] if d.source == "1day" else at_hour(d.day, hour, self.tz)
                if t is not None:
                    points[t] = (d.mean, d.mean if d.lo is None else d.lo, d.mean if d.hi is None else d.hi)
        xs = sorted(points)
        return Plotted(xs, [points[t][0] for t in xs], DAY, False, [points[t][1] for t in xs], [points[t][2] for t in xs]) if len(xs) >= 2 else None

    def _shaded(self, line: Plotted | None, band: Plotted | None) -> Plotted | None:
        """A line shaded up to its band series' (wind: up to the gusts), where that is higher."""
        if line is not None and band is not None:
            tops = shaded_up_to(dict(zip(line.x, line.high)), dict(zip(band.x, band.high)))
            line.high = [tops[x] for x in line.x]
        return line

    def _series_entry(self, k: str) -> tuple[Line, str] | None:
        """One series as a chart line and how it was drawn; None if there is too little to draw."""
        how, at = self.drawing
        band = self._band_key(k)
        if how == "buckets":
            line = self._shaded(self.lines.get(k), self.lines.get(band) if band else None)
        elif how == "days":
            line = self._shaded(self._day_line(k, at), self._day_line(band, at) if band else None)
        else:
            line = build_line(self._series_readings(k), self.tz, self.span.total_seconds(), smooth=k in SMOOTH_SERIES,
                              force_daily=self.turn.average_asked, until=now_local(self.tz).date())
        if line is None:
            return None
        group = k.split(".", 1)[0]
        return line.spec(group.replace("_", " ").capitalize(), indoor=group == "indoor"), line.name

    def _rain_bars(self, held: Held) -> Bars:
        """Rain for the chart, summed into bars. From the settled 30-minute readings and, after the last of them, the period's own (the
        newest hours are still settling); days older than those (about a year) come from the daily records, the day's total, which are
        approximate (10am to 10am). A figure per month or year asked for in the person's words (`by`) gets one bar for each, whatever the
        length of the period."""
        lo, hi = self.f.epoch(self.start), self.f.epoch(self.end)
        daily = dict(held.rain)
        after = max(daily) + SLOT if daily else lo
        daily.update({t: s.value for t, s in self.store.get("rainfall.daily", {}).get("pts", {}).items() if t >= after and s.seconds in (300, 1800)})
        rain = rain_slots(daily)
        days = {local_date(t, self.tz) for t in rain}
        for t, mm in held.rain_daily.items():   # a daily bucket is dated by the local day its 10am start falls on
            day = local_date(t, self.tz)
            if day not in days and self.start.date() <= day <= self.end.date():
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
            lines = [e[0] for k in sorted(self.units) if k.split(".", 1)[-1] == field and (
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
