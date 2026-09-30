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
from bisect import bisect_left
import json
import logging
from datetime import datetime, time, timedelta, timezone

from ..charts import AVERAGE_CHART_HINT, CHART_HINT, STACK_CHART_HINT, DIRECTION_CHART_HINT, wants_chart
from ..lines import build_line
from ..rain import rain_bars, rain_slots
from ..timeutil import SLOT, daily_summary, local_date, now_local
from ..series import WEATHER
from ..specs import Bars, Chart, Compass, Line, Panel, period_text, rain_behind
from .api import CYCLE_SECONDS, RETENTION
from .direction import SPEED_STEPS, rose as direction_rose, summarise as summarise_direction
from .extremes import Ext, better, collect, daily_readings, describe_time, fold, high_of, low_of, series_extremes
from .fetch import Fetcher, spans

log = logging.getLogger(__name__)

INTRADAY_DAYS = 8               # up to this many days: 5-minute readings where archived
FINE_DAYS = 31                  # up to this many: 30-minute readings; longer periods use daily records
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
# Readings Ecowitt only provides as averages (no _low/_high). Left out of results unless the
# question asks for them, so an "averaged data" note can't be misapplied elsewhere.
KNOWN_FIELDS = {r.field for r in WEATHER.values()}     # the fields a chart of one reading can be about
DERIVED = ("feels_like", "app_temp", "app_tempin", "dew_point", "vpd")


STACK = {name: (r.group, r.field, r.label, r.unit) for name, r in WEATHER.items()}  # what "plot X and Y" can put on one chart


def stack_names(args: dict, turn) -> list[str]:
    """The readings to put side by side (from the person's words, else the model's chart_fields), two or more."""
    raw = turn.chart_fields or args.get("chart_fields") or []
    names = [n for n in dict.fromkeys(str(x).strip().lower() for x in raw) if n in STACK]
    return names if len(names) >= 2 else []


# ---------- one history question ----------
class HistoryQuery:
    """Answers one question: fetch, find the extremes, refine them, and build the compact
    result (and chart) for the model."""

    def __init__(self, fetcher: Fetcher, args: dict, turn):
        self.f, self.tz, self.args, self.turn = fetcher, fetcher.tz, args, turn
        self.now = now_local(self.tz)
        self.now_utc = datetime.now(timezone.utc)
        self.store: dict = {}       # "group.field" -> {"unit", "pts"}, filled by _fetch_period
        self.start = self.end = self.span = None          # the period, set by run()
        self.held = False                                  # the whole period is in the cache at 30 minutes, and fits the row budget
        self.detailed = False                              # local-day-aligned data available for the whole period
        self.overall: dict = {}                            # key -> {"low": Ext, "high": Ext}
        self.monthly: dict = {}                            # key -> {month: {"low": Ext, "high": Ext}}
        self.direction: dict = {}                          # "wind.wind_direction" -> its summary (never low/high)
        self.rain_bars = Bars("Rain", "mm", [], [], 86400, "day")   # for a stacked chart with rain
        self.compass: Compass | None = None                # the wind rose (counts per compass point by speed), drawn beside the wind chart

    async def run(self) -> str:
        today = self.now.date()
        try:
            start = datetime.fromisoformat(str(self.args.get("start_date") or today).replace("T", " "))
            end = datetime.fromisoformat(str(self.args.get("end_date") or self.now).replace("T", " "))
        except ValueError as e:
            return f"Error: bad date ({e}). Use 'YYYY-MM-DD HH:MM:SS'."
        self.start = max(start.replace(tzinfo=None), datetime.combine(today - timedelta(days=RETENTION["1day"] - 2), time()))
        self.end = min(end.replace(tzinfo=None), self.now)
        if self.start >= self.end:
            return "Error: start_date must be before end_date and within the last 4 years."
        self.span = self.end - self.start
        self.held = await self._held_locally()
        self.detailed = self.held or self.span <= timedelta(days=DETAILED_DAYS)

        await self._fetch_period()
        await self._fine_ranges()
        if "rain" in stack_names(self.args, self.turn) or "rainfall.daily" in self.store:   # rain alone is drawn as bars too
            self.rain_bars = await self._rain_bars()
        await self._summarise_direction()
        if not self.store and not self.direction:
            return "Error: no history data returned." + (
                " Details: " + "; ".join(self.f.errors[:5]) if self.f.errors else "")
        self.overall = {k: ex for k, s in self.store.items() if len(ex := series_extremes(s)) == 2}
        self.monthly = self._monthly_extremes() if self.span > timedelta(days=31) else {}
        refined = await self._refine()
        return self._answer(refined)

    async def _fine_ranges(self):
        """30-minute readings with no low and high of their own (dew point, feels-like, VPD, solar, UV, pressure ...) get the
        lowest and highest of the cached 5-minute readings inside each slot, so their bands are real ranges. Only what the
        cache already holds (no requests); wind keeps its own band (the speed up to the gusts)."""
        lo, hi = self.f.epoch(self.start), self.f.epoch(self.end)
        for key, series in self.store.items():
            group, field = key.split(".", 1)
            pts = series["pts"]
            if group == "wind" or not pts or any(r["cycle"] != "30min" or "low" in r or "high" in r for r in pts.values()):
                continue
            (fine,) = await asyncio.to_thread(self.f.cache.slots, self.f.mac, "5min", group, [field], lo, hi)
            times = sorted(fine)
            for t, rec in pts.items():
                a, b = bisect_left(times, t), bisect_left(times, t + SLOT)
                if b - a >= 2 and "value" in rec:
                    values = [fine[x] for x in times[a:b]] + [rec["value"][0]]
                    rec["low"], rec["high"] = ((v, str(v)) for v in (min(values), max(values)))

    async def _held_locally(self) -> bool:
        """Is the whole period already in the cache at 30 minutes (bar the newest day, which is still settling), and
        small enough to load? Then any length is read from it at no cost in requests."""
        f = self.f
        if (self.span.days + 1) * 48 * len(f.groups) * FIELDS_PER_GROUP > MAX_ROWS:
            return False
        for t, e in spans("30min", self.start, self.end - timedelta(days=1)):
            if not await f.covered("30min", f.epoch(t), f.epoch(e)):
                return False
        return True

    async def _chunks(self, cycle: str, t: datetime, until: datetime):
        for a, b in spans(cycle, t, until):
            collect(self.store, await self.f.get(cycle, a, b), cycle)

    async def _fetch_period(self):
        """Coarse pass: the best data for the whole period at the fewest requests."""
        f, start, end, today = self.f, self.start, self.end, self.now.date()
        age_days = (today - start.date()).days
        thirty_floor = datetime.combine(today - timedelta(days=RETENTION["30min"] - 2), time())
        recent = datetime.combine(today - timedelta(days=6), time())
        if self.span <= timedelta(days=1) and (age_days < RETENTION["5min"] - 1 or await f.covered(
                "5min", f.epoch(start), f.epoch(end))):
            collect(self.store, await f.get("5min", start, end), "5min")
        elif self.span <= timedelta(days=FINE_DAYS) and end > thirty_floor:
            # Resolution follows the length: a few days at 5 minutes (where archived, free from the cache),
            # up to a month at 30 minutes (one request per week, cached afterwards)
            for a, b in spans("30min", max(start, thirty_floor), end):
                five = self.span <= timedelta(days=INTRADAY_DAYS) and await f.covered("5min", f.epoch(a), f.epoch(b))
                collect(self.store, await f.get("5min" if five else "30min", a, b), "5min" if five else "30min")
            if start < thirty_floor:
                await self._chunks("1day", start, min(end, thirty_floor - timedelta(seconds=1)))
        elif self.held:
            await self._chunks("30min", start, end)  # held in the cache: exact local days, true lows and highs, no requests
        else:
            # Longer: Ecowitt's daily records (each has its own low and high, and it is a request or two, cached),
            # plus the newest days at 30 minutes so the line reaches today. Exact times come from refining the extremes.
            await self._chunks("30min", max(start, recent), end)
            await self._chunks("1day", start, min(end, recent - timedelta(seconds=1)))

    async def _summarise_direction(self):
        """Wind direction leaves the low/high pipeline: it is counted by compass point instead. Recent
        days are counted from 5-minute readings where the cache has them (averaging degrees is wrong
        near north); older parts of a long period are left out rather than counted from daily averages.
        Calm readings (no wind speed) are dropped: a sensor with nothing to point at repeats its last direction."""
        keys = [k for k in self.store if k.endswith(".wind_direction")]
        if not keys:
            return
        f = self.f
        wind = Fetcher(f.api, f.cache, f.hot, f.mac, ["wind"], self.tz)
        first = max(self.start, self.end - timedelta(days=DIRECTION_DAYS))
        for key in keys:
            fine: dict = {}
            fine_days = set()
            for n in range((self.end.date() - first.date()).days + 1):
                day = first.date() + timedelta(days=n)
                a, b = max(first, datetime.combine(day, time())), min(self.end, datetime.combine(day, time(23, 59, 59)))
                if a < b and (day == self.end.date() or await wind.covered("5min", wind.epoch(a), wind.epoch(b))):
                    collect(fine, await wind.get("5min", a, b), "5min")  # today's comes from memory or one request
                    fine_days.add(day)
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
            result = summarise_direction(counted, self.tz, self.span <= timedelta(days=31), len(calm))
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
            for ts, rec in sorted(series["pts"].items()):
                month = datetime.fromtimestamp(ts, timezone.utc).astimezone(self.tz).strftime("%b %Y")
                fold(months.setdefault(month, {}), ts, rec)
            out[key] = months
        return out

    async def _refine(self) -> int:
        """Fetch exactly each extreme's window (a 30-minute slot or a UTC day) at the finest cycle
        available, for the real time and value. Overall records first (temperatures first), then,
        for periods of up to a few months, each month's temperature records. A second pass narrows
        a 30-minute slot to archived 5-minute data. Returns the number of windows fetched."""
        f = self.f
        slots: dict = {}  # ("all", key, want) or ("month", key, month, want) -> Ext
        for key in sorted(self.overall, key=lambda k: "temperature" not in k):
            for want in ("low", "high"):
                slots[("all", key, want)] = self.overall[key][want]
        if self.monthly and self.detailed:
            for key, months in self.monthly.items():
                if key.endswith(".temperature"):
                    for month, d in reversed(list(months.items())):  # newest first: where 5-minute data is
                        for want, ext in d.items():
                            slots[("month", key, month, want)] = ext
        caps = {"all": MAX_REFINE_WINDOWS, "month": MAX_MONTH_REFINE_WINDOWS}
        refined = 0
        for _ in range(2):
            windows: dict = {}  # (cycle, start_ts, end_ts) -> [slot id]
            used = {"all": 0, "month": 0}
            finer_of: dict = {}  # slots on the same bucket share an answer; a new pass looks again (data was fetched)
            for sid, ext in slots.items():
                w_end = ext.ts + CYCLE_SECONDS[ext.cycle] - 1
                if (ext.ts, ext.cycle) not in finer_of:
                    finer_of[ext.ts, ext.cycle] = await f.finer_cycle(ext.ts, ext.cycle, w_end, self.now_utc)
                finer = finer_of[ext.ts, ext.cycle]
                if not finer:
                    continue
                w = (finer, ext.ts, w_end)
                if w not in windows:
                    if used[sid[0]] >= caps[sid[0]]:
                        continue
                    used[sid[0]] += 1
                    windows[w] = []
                windows[w].append(sid)
            if not windows:
                break
            refined += len(windows)
            for (cycle, w_start, w_end), targets in windows.items():
                fine: dict = {}
                collect(fine, await f.get(cycle, f.local(w_start), min(f.local(w_end), self.now)), cycle)
                for sid in targets:
                    key, want = sid[1], sid[-1]
                    found = series_extremes(fine[key], w_start, w_end).get(want) if key in fine else None
                    if not found:
                        continue
                    cur = slots[sid]
                    keep = cur if better(want, cur.value, found.value) else found  # the more extreme value...
                    slots[sid] = Ext(keep.value, keep.raw, found.ts, found.cycle, keep.exact)  # ...at the finer time
        for sid, ext in slots.items():
            if sid[0] == "all":
                self.overall[sid[1]][sid[2]] = ext
            else:
                self.monthly[sid[1]][sid[2]][sid[3]] = ext
        return refined

    def _answer(self, refined: int) -> str:
        f = self.f
        wanted = self.args.get("include_derived") or []
        wanted = {wanted} if isinstance(wanted, str) else set(wanted)
        asked = [self.turn.chart_field or self.args.get("chart_field"), *(STACK[n][1] for n in stack_names(self.args, self.turn))]
        wanted |= {f for f in asked if f in DERIVED}   # charting one of them brings it into the result
        if "app_temp" in wanted:
            wanted.add("app_tempin")  # indoor's name for apparent temperature
        series_out = {key: self._result(key, ext) for key, ext in self.overall.items()
                      if key.split(".", 1)[-1] not in DERIVED or key.split(".", 1)[-1] in wanted}  # the rest: cached, not sent
        series_out.update(self.direction)
        log.info("%s to %s: %d ranges, %d cached, %d in mem, %d req%s, %d series, %d refined",
                 f"{self.start:%Y-%m-%d}", f"{self.end:%m-%d %H:%M}", f.ranges, f.from_cache, f.from_memory, f.calls,
                 f", {len(f.errors)} failed" if f.errors else "", len(series_out), refined)
        out = {"period": f"{self.start:%a %d %b %Y} - {self.end:%a %d %b %Y}", "series": series_out}
        if self.monthly and not self.detailed:
            out["monthly_note"] = ("Monthly figures for long periods come from daily data that runs 10am-10am, so they "
                                   "have no dates, and a low early on the 1st may be counted in the previous month.")
        if wants_chart(self.args, self.turn, self.start, self.end):
            self._add_charts(out, {k: v for k, v in series_out.items() if k in self.store})
        if f.errors:
            out["missing"] = f.errors[:10]
            out["warning"] = "Some data could not be fetched; the answer may be incomplete. Say so."
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))

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
        if self.span <= timedelta(days=31):
            days: dict = {}
            for ts, rec in self.store[key]["pts"].items():
                if rec["cycle"] != "1day":  # sub-daily points give correct local days
                    fold(days.setdefault(local_date(ts, tz), {}), ts, rec)
            entry["daily"] = {d.strftime("%a %d %b"): {w: e.raw for w, e in days[d].items()} for d in sorted(days)}
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
            wind = spec if spec and spec.title == "Wind" else self._chart_spec(plottable, "wind_gust")
            if wind:
                wind.compass = self.compass
                if wind is not spec:
                    holder.append(wind)
                out["chart"] = (CHART_HINT + " For wind direction, give the most common direction, not a high and low."
                                if spec and wind is not spec else DIRECTION_CHART_HINT)

    def _daily_means(self, pts: dict) -> dict:
        """{local date: mean of that day's readings}: from 5- or 30-minute readings where held, else the daily bucket."""
        means = {local_date(ts, self.tz): rec["value"][0] for ts, rec in pts.items() if rec.get("cycle") == "1day" and "value" in rec}
        means.update({d: mean for d, (mean, _, _) in daily_summary(daily_readings(pts), self.tz).items()})  # exact local days win over 10am-10am buckets
        return means

    def _add_averages(self, entry: dict, means: dict):
        """The mean over the period, and per day (up to 31 days) or per month: the answer to "what was the average"."""
        if not means:
            return
        fmt = lambda v: f"{v:.1f}"
        entry["average"] = fmt(sum(means.values()) / len(means))
        for d, v in means.items():
            table = entry.get("daily") if self.span <= timedelta(days=31) else entry.get("monthly")
            label = d.strftime("%a %d %b") if self.span <= timedelta(days=31) else d.strftime("%b %Y")
            if table is not None and label in table:
                table[label].setdefault("_sum", []).append(v)
        for table in (entry.get("daily"), entry.get("monthly")):
            for row in (table or {}).values():
                values = row.pop("_sum", None)
                if values:
                    row["avg"] = fmt(sum(values) / len(values))

    def _chart_spec(self, series_out: dict, field: str | None = None) -> Chart | None:
        """Line chart: one line per group for the field asked about (`field`, else chart_field; temperature by default,
        else the first field), at the finest resolution fetched for the whole period (5- or 30-minute readings,
        or daily averages for long periods), plus the true record high and low with their times."""
        asked = field or self.turn.chart_field or self.args.get("chart_field")
        wanted = str(asked or "temperature").strip().lower().replace(" ", "_")
        keys = [k for k in series_out if k.endswith("." + wanted)]
        if not keys and (wanted in DERIVED or asked and wanted in KNOWN_FIELDS):  # asked for on its own and not there: no chart, rather than a temperature one
            return None
        keys = keys or [k for k in series_out if k.endswith(".temperature")]
        if not keys:  # nothing to match: the wind chart when wind direction was counted, else the first field
            field = "wind_gust" if self.compass and any(k.endswith(".wind_gust") for k in series_out) else next(iter(series_out)).split(".", 1)[-1]
            keys = [k for k in series_out if k.endswith("." + field)]
        if keys[0] == "rainfall.daily" and self.rain_bars.x:  # the day's counter is a running total: draw what fell, as columns
            return Chart("Rain", f"{period_text(self.start.date(), self.end.date())}  ·  rain per {self.rain_bars.per}",
                         [Panel("Rain", "mm", bars=self.rain_bars)])
        field = keys[0].split(".", 1)[-1]
        unit = series_out[keys[0]]["unit"].replace("º", "°")
        lines, resolution, ranged = [], None, False
        wind = field == "wind_gust" and "wind.wind_speed" in self.store  # average speed, shaded up to the gusts
        for k in keys:
            got = self._series_entry(k)
            if got is None:
                continue
            line, cycle = got
            resolution = resolution or cycle
            rec = self.overall.get(k, {})
            line.records = {w: (rec[w].ts, rec[w].value) for w in ("low", "high") if w in rec and (w == "high" or not wind)}
            ranged = ranged or line.low is not None
            lines.append(line)
        if not lines:
            return None
        title = "Wind" if wind else field.replace("_", " ").capitalize()
        subtitle = (f"{period_text(self.start.date(), self.end.date())}  ·  {resolution}"
                    + (", shaded up to the gusts" if wind and ranged else ", range shaded" if ranged else "")
                    + ("  ·  records marked" if any(x.records for x in lines) and not wind else ""))
        reading = next((n for n, r in WEATHER.items() if r.field == field), "")
        return Chart(title, subtitle, [Panel(title, unit, lines, reading=reading)])

    def _series_readings(self, k: str) -> list:
        """One series as readings for lines.build_line. Wind is one series: the average speed, shaded up to the gusts."""
        wind = k == "wind.wind_gust" and "wind.wind_speed" in self.store
        speed = self.store["wind.wind_speed" if wind else k]["pts"]
        gust = self.store["wind.wind_gust"]["pts"] if wind else {}
        out = []
        for t, r in speed.items():
            if "value" in r:
                v = r["value"][0]
                low, high = (r["low"][0] if "low" in r else None), (r["high"][0] if "high" in r else None)
                if wind:
                    low, high = low_of(r), max(high_of(gust[t]) if t in gust and "value" in gust[t] else high_of(r), v)
                out.append((t, v, low, high, CYCLE_SECONDS[r["cycle"]]))
        return out

    def _series_entry(self, k: str, label: str | None = None) -> tuple[Line, str] | None:
        """One series as a chart line and how it was drawn; None if there is too little to draw."""
        line = build_line(self._series_readings(k), self.tz, self.span.total_seconds(), smooth=k in SMOOTH_SERIES, native_band=True,
                          force_daily=self.turn.average_asked, until=now_local(self.tz).date())
        if line is None:
            return None
        return line.spec(label or k.split(".", 1)[0].replace("_", " ").capitalize()), line.name

    async def _rain_bars(self) -> Bars:
        """Rain for the stacked chart: from the cached 30-minute readings (cache only), summed into bars."""
        lo, hi = self.f.epoch(self.start), self.f.epoch(self.end)
        (daily,) = await asyncio.to_thread(self.f.cache.slots, self.f.mac, "30min", "rainfall", ["daily"], lo, hi)
        return rain_bars(rain_slots(daily), self.tz, self.start.date(), self.end.date())

    def _stack_spec(self, names: list[str]) -> Chart | None:
        """The readings asked for, one panel each on a shared time axis (rain behind the first line); None if fewer than
        two can be drawn."""
        panels = []
        for name in names:
            group, field, label, unit = STACK[name]
            if name == "rain":
                if self.rain_bars.x:
                    panels.append(Panel(label, unit, bars=self.rain_bars))
                continue
            lines = [e[0] for k in sorted(self.store) if k.split(".", 1)[-1] == field and (
                     group == "outdoor" and k.split(".")[0] in ("outdoor", "indoor") or k.startswith(group + "."))
                     and (e := self._series_entry(k)) is not None]
            if lines:
                if len(lines) == 1 and name != "temperature":
                    lines[0].label = label
                panels.append(Panel(label, unit, lines, reading=name))
        if len(panels) < 2:
            return None
        rain = self.rain_bars.per if any(p.bars for p in panels) else None
        return Chart(", ".join(p.label for p in panels),
                     f"{period_text(self.start.date(), self.end.date())}" + (f"  ·  rain per {rain}" if rain else "")
                     + ("  ·  shaded: range" if any(s.low for p in panels for s in p.lines) else ""),
                     rain_behind(panels))
