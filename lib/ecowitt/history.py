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
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import NamedTuple

from ..charts import CHART_HINT, CHART_REQUESTS, DIRECTION_CHART_HINT
from ..timeutil import local_date, local_epoch, now_local, to_local
from .api import CYCLE_SECONDS, EcowittError, MAX_SPAN, RETENTION
from .direction import summarise as summarise_direction
from .store import HistoryCache, HotStore, merge as merge_intervals

log = logging.getLogger(__name__)

MAX_DIRECTION_POINTS = 4000     # dots on the wind direction chart
DIRECTION_DAYS = 365            # how far back wind direction is counted (from cached 5-minute readings)
MAX_REFINE_WINDOWS = 4          # overall records
MAX_MONTH_REFINE_WINDOWS = 24
# Periods up to this long are built from local-day-aligned data, so each month's low/high and its
# date are exact even at month boundaries. Longer periods use daily data (10am-10am buckets), unless
# the cache already holds the 30-minute data (the archive does), in which case up to a year is detailed.
DETAILED_DAYS = 93
CACHED_DETAILED_DAYS = 365
# Readings Ecowitt only provides as averages (no _low/_high). Left out of results unless the
# question asks for them, so an "averaged data" note can't be misapplied elsewhere.
DERIVED = ("feels_like", "app_temp", "app_tempin", "dew_point", "vpd")


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
        self.detailed = False                              # local-day-aligned data available for the whole period
        self.overall: dict = {}                            # key -> {"low": Ext, "high": Ext}
        self.monthly: dict = {}                            # key -> {month: {"low": Ext, "high": Ext}}
        self.direction: dict = {}                          # "wind.wind_direction" -> its summary (never low/high)
        self.direction_period = (None, None)               # what the counted readings actually span
        self.direction_points: list = []                   # [local hour, degrees] of each counted reading, for the chart

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
        self.detailed = self.span <= timedelta(days=DETAILED_DAYS) or (
            self.span <= timedelta(days=CACHED_DETAILED_DAYS) and await self._cached_locally())

        await self._fetch_period()
        await self._summarise_direction()
        if not self.store and not self.direction:
            return "Error: no history data returned." + (
                " Details: " + "; ".join(self.f.errors[:5]) if self.f.errors else "")
        self.overall = {k: ex for k, s in self.store.items() if len(ex := series_extremes(s)) == 2}
        self.monthly = self._monthly_extremes() if self.span > timedelta(days=31) else {}
        refined = await self._refine()
        return self._answer(refined)

    async def _cached_locally(self) -> bool:
        """Is the whole period already in the cache at 5- or 30-minute resolution (bar the newest day,
        which is still settling)? Then detail costs no requests."""
        f = self.f
        floor = datetime.combine(self.now.date() - timedelta(days=RETENTION["30min"] - 2), time())
        for t, e in spans("30min", max(self.start, floor), self.end - timedelta(days=1)):
            first, last = f.epoch(t), f.epoch(e)
            if not (await f.covered("5min", first, last) or await f.covered("30min", first, last)):
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
        elif self.detailed and end > thirty_floor:
            # Weeks already in the 5-minute archive come free from the cache; the rest is
            # fetched at 30 minutes (one request per week, cached afterwards)
            for a, b in spans("30min", max(start, thirty_floor), end):
                cycle = "5min" if await f.covered("5min", f.epoch(a), f.epoch(b)) else "30min"
                collect(self.store, await f.get(cycle, a, b), cycle)
            if start < thirty_floor:
                await self._chunks("1day", start, min(end, thirty_floor - timedelta(seconds=1)))
        else:
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
            step = -(-len(counted) // MAX_DIRECTION_POINTS)
            self.direction_points = [[(lt := f.local(t)).hour + lt.minute / 60, d] for t, d, _ in counted[::step]]
            self.direction_period = (first, self.end)
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
        if self.args.get("chart") and holder is not None:
            spec = self._chart_spec(plottable) if plottable else None
            if spec:
                holder.append(spec)
                out["chart"] = CHART_HINT
            if self.direction_points:
                holder.append({"kind": "direction", "title": "Wind direction by hour of day",
                               "subtitle": _period(*self.direction_period), "points": self.direction_points})
                out["chart"] = (CHART_HINT + " For wind direction, give the most common direction, not a high and low."
                                if spec else DIRECTION_CHART_HINT)
        if f.errors:
            out["missing"] = f.errors[:10]
            out["warning"] = "Some data could not be fetched; the answer may be incomplete. Say so."
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))

    def _chart_spec(self, series_out: dict) -> dict | None:
        """Line chart: one line per reading type asked about (temperature if present, else the
        first), at the finest resolution fetched for the whole period (5- or 30-minute readings,
        or daily averages for long periods), plus the true record high and low with their times."""
        keys = [k for k in series_out if k.endswith(".temperature")]
        if not keys:
            field = next(iter(series_out)).split(".", 1)[-1]
            keys = [k for k in series_out if k.endswith("." + field)]
        field = keys[0].split(".", 1)[-1]
        unit = series_out[keys[0]]["unit"].replace("º", "°")
        names = {"5min": "5-minute readings", "30min": "30-minute readings", "4hour": "4-hour averages",
                 "1day": "daily averages"}
        series, resolution = [], None
        for k in keys:
            pts = {t: r for t, r in self.store[k]["pts"].items() if "value" in r}
            if not pts:
                continue
            cycle, line = self._line(pts)
            xs = sorted(line)
            if len(xs) < 2:
                continue
            resolution = resolution or names.get(cycle, cycle)
            rec = self.overall.get(k, {})
            records = {w: [rec[w].ts, rec[w].value] for w in ("low", "high") if w in rec}
            series.append({"label": k.split(".", 1)[0].replace("_", " ").capitalize(),
                           "x": xs, "y": [line[t] for t in xs], "records": records})
        if not series:
            return None
        return {"kind": "line", "title": field.replace("_", " ").capitalize(),
                "subtitle": f"{_period(self.start, self.end)}  ·  {resolution}", "unit": unit, "series": series}

    def _line(self, pts: dict) -> tuple[str, dict]:
        """(cycle, {ts: value}) for one series, at a single consistent resolution."""
        counts: dict = {}
        for r in pts.values():
            counts[r["cycle"]] = counts.get(r["cycle"], 0) + 1
        sub_daily = [c for c in ("5min", "30min") if c in counts]
        if len(sub_daily) > 1 and sum(counts[c] for c in sub_daily) >= counts.get("1day", 0):
            # archived 5-minute weeks next to 30-minute weeks: average into 30-minute bins
            bins: dict = {}
            for t, r in pts.items():
                if r["cycle"] in sub_daily:
                    bins.setdefault(t // 1800 * 1800, []).append(r["value"][0])
            return "30min", {t: sum(v) / len(v) for t, v in bins.items()}
        cycle = max(counts, key=counts.get)
        line = {t: pts[t]["value"][0] for t in pts if pts[t]["cycle"] == cycle}
        if cycle == "1day" and line:
            # The last week comes as 30-minute data: add it as daily averages so the line reaches
            # today (today's own average isn't meaningful until the day is over)
            last_day = max(local_date(t, self.tz) for t in line)
            today = now_local(self.tz).date()
            by_day: dict = {}
            for t, r in pts.items():
                d = local_date(t, self.tz)
                if r["cycle"] == "30min" and last_day < d < today:
                    by_day.setdefault(d, []).append(r["value"][0])
            for d, vals in by_day.items():
                noon = int(datetime.combine(d, datetime.min.time()).replace(hour=10, tzinfo=self.tz).timestamp())
                line[noon] = sum(vals) / len(vals)
        return cycle, line


def _period(start: datetime, end: datetime) -> str:
    a, b = start.date(), end.date()
    if a == b:
        return f"{a:%a} {a.day} {a:%b %Y}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{a:%a} {a.day} – {b:%a} {b.day} {b:%b %Y}"
    if a.year == b.year:
        return f"{a:%a} {a.day} {a:%b} – {b:%a} {b.day} {b:%b %Y}"
    return f"{a:%a} {a.day} {a:%b %Y} – {b:%a} {b.day} {b:%b %Y}"
