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
import json
import logging
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import NamedTuple

from ..charts import AVERAGE_ASKED, AVERAGE_CHART_HINT, CHART_FIELD, CHART_HINT, CHART_STACK, STACK_CHART_HINT, CHART_REQUESTS, DIRECTION_CHART_HINT, wants_chart
from ..timeutil import WIDTH_NAMES, WIDTHS, bucket_width, bucketed, daily_summary, local_date, local_epoch, now_local, rolling_mean, to_local
from .api import CYCLE_SECONDS, EcowittError, MAX_SPAN, RETENTION
from .direction import SPEED_STEPS, rose as direction_rose, summarise as summarise_direction
from .link import rain_bars, rain_slots
from .store import HistoryCache, HotStore, merge as merge_intervals

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
SMOOTH_POINTS = 3               # each drawn point is the mean of this many 5-minute readings (a 15-minute window)
MAX_ROWS = 500_000              # readings loaded per question from the cache: days x 48 x groups x FIELDS_PER_GROUP
FIELDS_PER_GROUP = 12           # about how many fields (with lows and highs) a group has
# Readings Ecowitt only provides as averages (no _low/_high). Left out of results unless the
# question asks for them, so an "averaged data" note can't be misapplied elsewhere.
DERIVED = ("feels_like", "app_temp", "app_tempin", "dew_point", "vpd")


# What "plot X and Y" can put on one chart: name -> (group, field, label, unit)
STACK = {"temperature": ("outdoor", "temperature", "Temperature", "°C"), "humidity": ("outdoor", "humidity", "Humidity", "%"),
         "pressure": ("pressure", "relative", "Pressure", "hPa"), "wind": ("wind", "wind_gust", "Wind", "km/h"),
         "rain": ("rainfall", "daily", "Rain", "mm")}


def stack_names(args: dict) -> list[str]:
    """The readings to put side by side (from the person's words, else the model's chart_fields), two or more."""
    raw = CHART_STACK.get() or args.get("chart_fields") or []
    names = [n for n in dict.fromkeys(str(x).strip().lower() for x in raw) if n in STACK]
    return names if len(names) >= 2 else []


# ---------- fetching ----------
def series_in(data: dict):
    """(group, field, obj) for every time series in an Ecowitt 'data' object; obj["list"] is {ts: value}."""
    for group, fields in data.items():
        if isinstance(fields, dict):
            for field, obj in fields.items():
                if isinstance(obj, dict) and isinstance(obj.get("list"), dict):
                    yield group, field, obj


def spans(cycle: str, t: datetime, until: datetime):
    """(start, end) pieces of [t, until) that each fit Ecowitt's per-request limit for this cycle."""
    while t < until:
        e = min(t + MAX_SPAN[cycle] - timedelta(seconds=1), until)
        yield t, e
        t = e + timedelta(seconds=1)


def merge_data(into: dict, new: dict, window: tuple[int, int] | None = None):
    """Merge one Ecowitt 'data' object into another, optionally only timestamps in window."""
    for grp, field, obj in series_in(new):
        entry = into.setdefault(grp, {}).setdefault(field, {"unit": obj.get("unit", ""), "list": {}})
        for ts, value in obj["list"].items():
            if window is None or window[0] <= int(ts) <= window[1]:
                entry["list"][ts] = value


class Fetcher:
    """History for one job (a question, a refresh, an alert check): from the disk cache where
    possible, recent readings from memory if fresh, the rest from Ecowitt."""

    def __init__(self, api, cache: HistoryCache, hot: HotStore, mac: str, groups: list[str], tz: tzinfo):
        self.api, self.cache, self.hot, self.tz = api, cache, hot, tz
        self.mac = mac.strip().upper()
        self.groups = [g.split(".")[0].strip() for g in groups if g.strip()]  # plain names, never "outdoor.temp"
        self.ranges = self.from_cache = self.from_memory = self.calls = 0
        self.errors: list[str] = []
        self.rejected = 0  # requests Ecowitt itself refused (not network trouble or a busy server)

    def epoch(self, local: datetime) -> int:
        return local_epoch(local, self.tz)

    def local(self, epoch: int) -> datetime:
        return to_local(epoch, self.tz).replace(tzinfo=None)

    async def get(self, cycle: str, start: datetime, end: datetime, refresh: bool = False, load: bool = True) -> dict:
        """Readings for [start, end] (local time). refresh=True ignores the in-memory copy (used by
        the keep-warm refresh). load=False only makes sure the range is cached and in memory, and
        skips building the result nobody will read (the refresh and the archive)."""
        self.ranges += 1
        s, e = self.epoch(start), self.epoch(end)
        async with self.hot.lock(self.mac, cycle):
            missing = {g: await asyncio.to_thread(self.cache.missing, self.mac, cycle, g, s, e) for g in self.groups}
            gaps = merge_intervals([iv for ivs in missing.values() for iv in ivs])
            if not gaps:
                self.from_cache += 1
            fresh: dict = {}
            for gap_start, gap_end in gaps:
                need = [g for g in self.groups if any(a <= gap_end and b >= gap_start for a, b in missing[g])]
                hot = {} if refresh else {g: self.hot.get(self.mac, cycle, g, gap_start, gap_end) for g in need}
                if hot and all(v is not None for v in hot.values()):
                    self.from_memory += 1
                    merge_data(fresh, hot, (gap_start, gap_end))
                    continue
                data = await self._fetch(cycle, self.local(gap_start), self.local(gap_end), need)
                if data is None:
                    continue  # failed: not recorded as fetched, so it's retried next time
                await asyncio.to_thread(self.cache.store, self.mac, cycle, need, data, gap_start, gap_end)
                for g in need:
                    self.hot.put(self.mac, cycle, g, gap_start, gap_end, data.get(g) or {})
                merge_data(fresh, data)
            if not load:
                return {}
            result = await asyncio.to_thread(self.cache.load, self.mac, cycle, self.groups, s, e)
        merge_data(result, fresh, (s, e))  # recent readings aren't on disk; use the fresh ones
        return result

    async def _fetch(self, cycle: str, start: datetime, end: datetime, groups: list[str]) -> dict | None:
        """One Ecowitt request: its data ({} if none), or None if it failed."""
        self.calls += 1
        try:
            return await self.api.history(self.mac, cycle, start, end, ",".join(groups))
        except EcowittError as e:
            label = f"{cycle} {start:%d %b %Y} - {end:%d %b %Y}"
            log.warning("History request failed (%s): %s", label, e)
            self.errors.append(f"{label}: {e}")
            self.rejected += not e.transient
            return None

    async def covered(self, cycle: str, start: int, end: int) -> bool:
        """True if the cache holds every group for [start, end] (epoch seconds)."""
        for g in self.groups:
            if await asyncio.to_thread(self.cache.missing, self.mac, cycle, g, start, end):
                return False
        return bool(self.groups)

    async def finer_cycle(self, ts: int, current: str, window_end: int, now_utc: datetime) -> str | None:
        """Finest cycle finer than `current` available for this window: still kept by Ecowitt,
        or already in the cache (e.g. archived 5-minute data). None if neither."""
        age = (now_utc - datetime.fromtimestamp(ts, timezone.utc)).days
        for cycle in ("5min", "30min", "4hour"):
            if cycle == current:
                return None
            if age < RETENTION[cycle] - 1 or await self.covered(cycle, ts, window_end):
                return cycle
        return None


# ---------- extremes ----------
class Ext(NamedTuple):
    value: float
    raw: str        # as Ecowitt gave it, so no rounding artefacts
    ts: int
    cycle: str
    exact: bool     # a real reading or a bucket's true low/high, not an average


def collect(store: dict, data: dict, cycle: str):
    """Fold an Ecowitt 'data' object into store["group.field"] = {unit, pts: {ts: rec}}.
    rec holds value / low / high as (float, raw string) and the source cycle."""
    for group, fname, fobj in series_in(data):
        base, kind = fname, "value"
        if fname.endswith("_low"):
            base, kind = fname[:-4], "low"
        elif fname.endswith("_high"):
            base, kind = fname[:-5], "high"
        series = store.setdefault(f"{group}.{base}", {"unit": fobj.get("unit", ""), "pts": {}})
        for ts, raw in fobj["list"].items():
            try:
                rec = series["pts"].setdefault(int(ts), {"cycle": cycle})
                rec[kind] = (float(raw), str(raw))
            except (TypeError, ValueError):
                continue


def _better(want: str, a: float, b: float) -> bool:
    return a < b if want == "low" else a > b


def _fold(into: dict, ts: int, rec: dict):
    """Update into["low"/"high"] with this point's low/high if more extreme."""
    for want in ("low", "high"):
        if want in rec:
            found = (*rec[want], True)
        elif "value" in rec:
            found = (*rec["value"], rec["cycle"] == "5min")  # 5-minute readings are real readings
        else:
            continue
        if want not in into or _better(want, found[0], into[want].value):
            into[want] = Ext(found[0], found[1], ts, rec["cycle"], found[2])


def series_extremes(series: dict, lo_ts: int | None = None, hi_ts: int | None = None) -> dict[str, Ext]:
    """Overall low/high of a series (optionally only points with lo_ts <= ts <= hi_ts)."""
    out: dict = {}
    for ts, rec in series["pts"].items():
        if lo_ts is None or lo_ts <= ts <= hi_ts:
            _fold(out, ts, rec)
    return out


def _low(rec: dict) -> float:
    return rec["low"][0] if "low" in rec else rec["value"][0]


def _high(rec: dict) -> float:
    return rec["high"][0] if "high" in rec else rec["value"][0]


def _readings(pts: dict, before: date | None = None, tz: tzinfo | None = None) -> list[tuple]:
    """(ts, value, low, high, fine) for the 5- and 30-minute readings (those before `before`), for daily_summary."""
    return [(t, r["value"][0], _low(r), _high(r), r["cycle"] == "5min") for t, r in pts.items()
            if r["cycle"] != "1day" and "value" in r and (before is None or local_date(t, tz) < before)]


def _clock(dt: datetime) -> str:
    """12-hour clock, e.g. '7:05am', '3:30pm', '10am'."""
    text = dt.strftime("%I:%M%p").lstrip("0").lower()
    return text.replace(":00", "") if dt.minute == 0 else text


def _day(dt: datetime) -> str:
    return f"{dt:%a} {dt.day} {dt:%b %Y}"


def describe_time(ts: int, cycle: str, tz: tzinfo) -> tuple[str, str, str]:
    """(raw local time, ready-made wording, date) for an extreme found in a point of this
    cycle. The wording already says "at" (5-minute readings), "around" (30-minute slots) or gives
    the window it happened in, so the model can copy it as is. The date is separate (so an emoji
    can go before it) and empty when the window spans two days, since then the wording has both."""
    start = datetime.fromtimestamp(ts, timezone.utc).astimezone(tz)
    raw = start.strftime("%a %d %b %Y %H:%M")
    if cycle == "5min":
        return raw, f"at {_clock(start)}", _day(start)
    if cycle == "30min":
        return raw, f"around {_clock(start)}", _day(start)
    end = datetime.fromtimestamp(ts + CYCLE_SECONDS[cycle], timezone.utc).astimezone(tz)
    if start.date() == end.date():
        return raw, f"sometime between {_clock(start)} and {_clock(end)}", _day(start)
    return raw, f"sometime between {_clock(start)} {_day(start)} and {_clock(end)} {_day(end)}", ""


# ---------- one history question ----------
class HistoryQuery:
    """Answers one question: fetch, find the extremes, refine them, and build the compact
    result (and chart) for the model."""

    def __init__(self, fetcher: Fetcher, args: dict):
        self.f, self.tz, self.args = fetcher, fetcher.tz, args
        self.now = now_local(self.tz)
        self.now_utc = datetime.now(timezone.utc)
        self.store: dict = {}       # "group.field" -> {"unit", "pts"}, filled by _fetch_period
        self.start = self.end = self.span = None          # the period, set by run()
        self.held = False                                  # the whole period is in the cache at 30 minutes, and fits the row budget
        self.detailed = False                              # local-day-aligned data available for the whole period
        self.overall: dict = {}                            # key -> {"low": Ext, "high": Ext}
        self.monthly: dict = {}                            # key -> {month: {"low": Ext, "high": Ext}}
        self.direction: dict = {}                          # "wind.wind_direction" -> its summary (never low/high)
        self.rain_bars: dict = {"x": [], "y": [], "width": 86400, "per": "day"}   # for a stacked chart with rain
        self.compass: dict = {}                            # the wind rose (counts per compass point by speed), drawn beside the wind chart

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
        if stack_names(self.args) and "rain" in stack_names(self.args):
            self.rain_bars = await self._rain_bars()
        await self._summarise_direction()
        if not self.store and not self.direction:
            return "Error: no history data returned." + (
                " Details: " + "; ".join(self.f.errors[:5]) if self.f.errors else "")
        self.overall = {k: ex for k, s in self.store.items() if len(ex := series_extremes(s)) == 2}
        self.monthly = self._monthly_extremes() if self.span > timedelta(days=31) else {}
        refined = await self._refine()
        return self._answer(refined)

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
            self.compass = {"rose": direction_rose([(t, d, x, speeds.get(t)) for t, d, x in counted]),
                            "speeds": any(t in speeds for t, _, _ in counted), "speed_steps": list(SPEED_STEPS)}
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
                _fold(months.setdefault(month, {}), ts, rec)
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
                    keep = cur if _better(want, cur.value, found.value) else found  # the more extreme value...
                    slots[sid] = Ext(keep.value, keep.raw, found.ts, found.cycle, keep.exact)  # ...at the finer time
        for sid, ext in slots.items():
            if sid[0] == "all":
                self.overall[sid[1]][sid[2]] = ext
            else:
                self.monthly[sid[1]][sid[2]][sid[3]] = ext
        return refined

    def _answer(self, refined: int) -> str:
        tz, f = self.tz, self.f
        wanted = self.args.get("include_derived") or []
        wanted = {wanted} if isinstance(wanted, str) else set(wanted)
        if "app_temp" in wanted:
            wanted.add("app_tempin")  # indoor's name for apparent temperature
        series_out = {}
        for key, ext in self.overall.items():
            name = key.split(".", 1)[-1]
            if name in DERIVED and name not in wanted:
                continue  # still fetched and cached; just not sent unless asked for
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
                        _fold(days.setdefault(local_date(ts, self.tz), {}), ts, rec)
                entry["daily"] = {d.strftime("%a %d %b"): {w: e.raw for w, e in days[d].items()} for d in sorted(days)}
            else:
                entry["monthly"] = {}
                for month, d in self.monthly.get(key, {}).items():
                    if self.detailed:  # each month's low/high with when and date
                        entry["monthly"][month] = {}
                        for want, e in d.items():
                            entry["monthly"][month][want] = e.raw
                            _, entry["monthly"][month][f"{want}_when"], entry["monthly"][month][f"{want}_date"] = \
                                describe_time(e.ts, e.cycle, tz)
                    else:  # long periods: values only, to keep the result small
                        entry["monthly"][month] = {w: e.raw for w, e in d.items()}
            if (self.args.get("average") or AVERAGE_ASKED.get()) and not key.startswith("rainfall"):  # a rain total has no mean
                self._add_averages(entry, self._daily_means(self.store[key]["pts"]))
            series_out[key] = entry

        series_out.update(self.direction)
        log.info("%s to %s: %d ranges, %d cached, %d in mem, %d req%s, %d series, %d refined",
                 f"{self.start:%Y-%m-%d}", f"{self.end:%m-%d %H:%M}", f.ranges, f.from_cache, f.from_memory, f.calls,
                 f", {len(f.errors)} failed" if f.errors else "", len(series_out), refined)
        out = {"period": f"{self.start:%a %d %b %Y} - {self.end:%a %d %b %Y}", "series": series_out}
        if self.monthly and not self.detailed:
            out["monthly_note"] = ("Monthly figures for long periods come from daily data that runs 10am-10am, so they "
                                   "have no dates, and a low early on the 1st may be counted in the previous month.")
        holder = CHART_REQUESTS.get()
        plottable = {k: v for k, v in series_out.items() if k in self.store}
        if wants_chart(self.args, self.start, self.end) and holder is not None:
            spec = self._stack_spec(stack_names(self.args)) if stack_names(self.args) else None
            if spec:
                out["rain_total_mm"] = round(sum(self.rain_bars["y"]), 1) if self.rain_bars["x"] else None
                holder.append(spec)
                out["chart"] = STACK_CHART_HINT
            else:
                spec = self._chart_spec(plottable) if plottable else None
                if spec:
                    holder.append(spec)
                    out["chart"] = AVERAGE_CHART_HINT if AVERAGE_ASKED.get() else CHART_HINT
            if self.compass:  # wind direction was counted: the compass goes beside the wind speed line
                wind = spec if spec and spec["title"] == "Wind" else self._chart_spec(plottable, "wind_gust")
                if wind:
                    wind.update(self.compass, subtitle=wind["subtitle"] + "  ·  compass: wind direction")
                    if wind is not spec:
                        holder.append(wind)
                    out["chart"] = (CHART_HINT + " For wind direction, give the most common direction, not a high and low."
                                    if spec and wind is not spec else DIRECTION_CHART_HINT)
        if f.errors:
            out["missing"] = f.errors[:10]
            out["warning"] = "Some data could not be fetched; the answer may be incomplete. Say so."
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))

    def _daily_means(self, pts: dict) -> dict:
        """{local date: mean of that day's readings}: from 5- or 30-minute readings where held, else the daily bucket."""
        means = {local_date(ts, self.tz): rec["value"][0] for ts, rec in pts.items() if rec.get("cycle") == "1day" and "value" in rec}
        means.update({d: mean for d, (mean, _, _) in daily_summary(_readings(pts), self.tz).items()})  # exact local days win over 10am-10am buckets
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

    def _chart_spec(self, series_out: dict, field: str | None = None) -> dict | None:
        """Line chart: one line per group for the field asked about (`field`, else chart_field; temperature by default,
        else the first field), at the finest resolution fetched for the whole period (5- or 30-minute readings,
        or daily averages for long periods), plus the true record high and low with their times."""
        wanted = str(field or CHART_FIELD.get() or self.args.get("chart_field") or "temperature").strip().lower().replace(" ", "_")
        keys = [k for k in series_out if k.endswith("." + wanted)] or [k for k in series_out if k.endswith(".temperature")]
        if not keys:  # nothing to match: the wind chart when wind direction was counted, else the first field
            field = "wind_gust" if self.compass and any(k.endswith(".wind_gust") for k in series_out) else next(iter(series_out)).split(".", 1)[-1]
            keys = [k for k in series_out if k.endswith("." + field)]
        field = keys[0].split(".", 1)[-1]
        unit = series_out[keys[0]]["unit"].replace("º", "°")
        names = {"5min": "5-minute readings", "30min": "30-minute readings", "4hour": "4-hour averages",
                 "1day": "daily averages"}
        series, resolution, ranged = [], None, False
        wind = field == "wind_gust" and "wind.wind_speed" in self.store  # average speed, shaded up to the gusts
        for k in keys:
            got = self._series_entry(k)
            if got is None:
                continue
            entry, cycle = got
            resolution = resolution or names.get(cycle, cycle)
            rec = self.overall.get(k, {})
            entry["records"] = {w: [rec[w].ts, rec[w].value] for w in ("low", "high") if w in rec and (w == "high" or not wind)}
            if "low" in entry:
                ranged = True
            series.append(entry)
        if not series:
            return None
        return {"kind": "line", "title": "Wind" if wind else field.replace("_", " ").capitalize(),
                "subtitle": f"{_period(self.start, self.end)}  ·  {resolution}"
                            + (", shaded up to the gusts" if wind and ranged else ", range shaded" if ranged else "")
                            + ("  ·  records marked" if any(x["records"] for x in series) and not wind else ""),
                "unit": unit, "series": series}

    def _wind_pts(self) -> dict:
        """The wind as one series: the average speed, whose band runs up to the strongest gust at that moment."""
        speed = self.store["wind.wind_speed"]["pts"]
        gust = self.store["wind.wind_gust"]["pts"]
        out = {}
        for t, r in speed.items():
            if "value" in r:
                peak = _high(gust[t]) if t in gust and "value" in gust[t] else _high(r)
                out[t] = {"cycle": r["cycle"], "value": r["value"], "low": (_low(r), ""), "high": (max(peak, r["value"][0]), "")}
        return out

    def _series_entry(self, k: str, label: str | None = None) -> tuple[dict, str] | None:
        """One series as a chart line (x, y, and low/high where each point has its own range) and its resolution;
        None if there is too little to draw."""
        pts = {t: r for t, r in self.store[k]["pts"].items() if "value" in r}
        windy = k == "wind.wind_gust" and "wind.wind_speed" in self.store
        if windy:
            pts = self._wind_pts()
        if not pts:
            return None
        cycle, line, band = self._line(pts, keep_band=windy)
        smooth = cycle == "5min" and k in SMOOTH_SERIES  # only the drawn line: records and figures use the raw readings
        if smooth:
            raw, line = line, rolling_mean(line, CYCLE_SECONDS[cycle] * SMOOTH_POINTS // 2)  # finite readings only
            if line:
                line[max(line)] = raw[max(line)]  # the end dot is the latest reading
        xs = sorted(line)
        if len(xs) < 2:
            return None
        entry = {"label": label or k.split(".", 1)[0].replace("_", " ").capitalize(), "x": xs, "y": [line[t] for t in xs],
                 **({"smoothed": True} if smooth else {})}
        if len(band) >= len(xs) // 2:  # an average over a bucket: its low and high around it
            entry["low"] = [band.get(t, (line[t], line[t]))[0] for t in xs]
            entry["high"] = [band.get(t, (line[t], line[t]))[1] for t in xs]
        return entry, cycle

    async def _rain_bars(self) -> dict:
        """Rain for the stacked chart: from the cached 30-minute readings (cache only), summed into bars."""
        lo, hi = self.f.epoch(self.start), self.f.epoch(self.end)
        found = await asyncio.to_thread(self.f.cache.load_fields, self.f.mac, "30min", "rainfall", ["daily"], lo, hi)
        daily = {int(t): float(v) for t, v in found.get("daily", {"list": {}})["list"].items()}
        return rain_bars(rain_slots(daily), self.tz, self.start.date(), self.end.date())

    def _stack_spec(self, names: list[str]) -> dict | None:
        """The readings asked for, one panel each on a shared time axis; None if fewer than two can be drawn."""
        panels = []
        for name in names:
            group, field, label, unit = STACK[name]
            if name == "rain":
                if self.rain_bars["x"]:
                    panels.append({"label": label, "unit": unit, "bars": {k: v for k, v in self.rain_bars.items() if k != "per"}})
                continue
            entries = [e[0] for k in sorted(self.store) if k.split(".", 1)[-1] == field and (
                       group == "outdoor" and k.split(".")[0] in ("outdoor", "indoor") or k.startswith(group + "."))
                       and (e := self._series_entry(k)) is not None]
            if entries:
                panels.append({"label": label, "unit": unit, "series": entries if len(entries) > 1 or name == "temperature"
                               else [{**entries[0], "label": label}]})
        if len(panels) < 2:
            return None
        rain = self.rain_bars["per"] if any("bars" in p for p in panels) else None
        return {"kind": "stack", "title": " and ".join(p["label"] for p in panels),
                "subtitle": f"{_period(self.start, self.end)}" + (f"  ·  rain per {rain}" if rain else "")
                            + ("  ·  shaded: range" if any(s.get("low") for p in panels for s in p.get("series", [])) else ""),
                "panels": panels}

    def _line(self, pts: dict, keep_band: bool = False) -> tuple[str, dict, dict]:
        """(cycle, {ts: value}, {ts: (low, high)}) for one series, drawn at one resolution. Readings are drawn as they
        are while they fit the point budget; otherwise they are bucketed (30 minutes ... 4 hours: the mean, still a
        plain line; a day: the mean with its true low and high shaded as the band). keep_band keeps each point's own
        low and high as the band at every width (wind: the speed, shaded up to the gusts)."""
        counts: dict = {}
        for r in pts.values():
            counts[r["cycle"]] = counts.get(r["cycle"], 0) + 1
        sub_daily = [c for c in ("5min", "30min") if c in counts]
        if sub_daily:
            source = CYCLE_SECONDS[sub_daily[0]]
            width = 86400 if "1day" in counts or AVERAGE_ASKED.get() else bucket_width(self.span.total_seconds(), source)
            if width >= 86400:
                return self._daily_line(pts)  # long charts, and any average: a point a day (mean line, each day's range)
            if width > source or len(sub_daily) > 1:  # 5-minute weeks next to 30-minute ones also come out as one resolution
                width = max(width, WIDTHS[1])
                xs, mean, low, high = bucketed(_readings(pts), self.tz, width)
                return f"{WIDTH_NAMES[width]} averages", dict(zip(xs, mean)), dict(zip(xs, zip(low, high))) if keep_band else {}
        cycle = max(counts, key=counts.get)
        line = {t: pts[t]["value"][0] for t in pts if pts[t]["cycle"] == cycle}
        band = {} if cycle in ("5min", "30min") and not keep_band else {
            t: (_low(pts[t]), _high(pts[t])) for t in line if "low" in pts[t] and "high" in pts[t]}
        return cycle, line, band

    def _daily_line(self, pts: dict) -> tuple[str, dict, dict]:
        """One point a day: Ecowitt's daily buckets for the older part, and for days we hold at 5 or 30 minutes
        their own mean, low and high over the local day (so those days are exact). Today is left out until it is over."""
        days = daily_summary(_readings(pts, now_local(self.tz).date(), self.tz), self.tz)
        line, band = {}, {}
        for t, r in pts.items():
            if r["cycle"] == "1day" and local_date(t, self.tz) not in days and "value" in r:
                line[t] = r["value"][0]
                if "low" in r and "high" in r:
                    band[t] = (_low(r), _high(r))
        for d, (mean, low, high) in days.items():
            at = int(datetime.combine(d, datetime.min.time()).replace(hour=10, tzinfo=self.tz).timestamp())
            line[at], band[at] = mean, (low, high)
        return "1day", line, band


def _period(start: datetime, end: datetime) -> str:
    a, b = start.date(), end.date()
    if a == b:
        return f"{a:%a} {a.day} {a:%b %Y}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{a:%a} {a.day} – {b:%a} {b.day} {b:%b %Y}"
    if a.year == b.year:
        return f"{a:%a} {a.day} {a:%b} – {b:%a} {b.day} {b:%b %Y}"
    return f"{a:%a} {a.day} {a:%b %Y} – {b:%a} {b.day} {b:%b %Y}"
