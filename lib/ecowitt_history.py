"""Ecowitt history, fetched within the documented API limits.

Ecowitt keeps (and allows per request):
  5min   last 90 days    max 1 day per request
  30min  last 365 days   max 1 week per request
  4hour  last 730 days   max 1 month per request
  1day   last 1460 days  max 1 year per request

Coarse cycles carry true min/max fields (temperature_low/high etc.), so extremes can
be found cheaply. But 1day buckets cover UTC days (10am-10am in Melbourne), not local
days. So:
  - ranges up to 31 days use 30min data (local-day aligned, one request per week) for
    the daily breakdown;
  - longer ranges use 30min for the last 7 days and 1day for older data, only to locate
    the extremes and give a monthly breakdown;
  - each overall extreme is then refined by fetching exactly its bucket (a 30min slot or
    a UTC day) at the finest cycle still kept, which gives the real time and local date.
"""

import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone, tzinfo

from .charts import CHART_REQUESTS
from .history_cache import HistoryCache, merge as merge_intervals
from .recent_data import HOT
from .mcp_manager import MCPManager, _compact

log = logging.getLogger(__name__)

RETENTION = {"5min": 90, "30min": 365, "4hour": 730, "1day": 1460}  # days
MAX_SPAN = {"5min": timedelta(days=1), "30min": timedelta(days=7),
            "4hour": timedelta(days=28), "1day": timedelta(days=365)}
CYCLE_SECONDS = {"5min": 300, "30min": 1800, "4hour": 14400, "1day": 86400}
# Metric units (Ecowitt defaults to imperial). ecowitt-mcp takes these names, not Ecowitt's numeric IDs.
UNIT_PARAMS = {"temp_unitid": "C", "pressure_unitid": "hPa", "wind_speed_unitid": "kmh", "rainfall_unitid": "mm"}
BUSY_RETRIES = 3
MAX_REFINE_WINDOWS = 4          # overall records
# Periods up to this long are built from local-day-aligned data (archived 5-minute data
# where the cache has it, otherwise 30-minute), so each month's low/high and its date are
# exact even at month boundaries. Longer periods use daily data (10am-10am buckets).
DETAILED_DAYS = 93
MAX_MONTH_REFINE_WINDOWS = 24
# Readings Ecowitt only provides as averages (no _low/_high). Left out of results unless
# the question asks for them, so an "averaged data" note can't be misapplied elsewhere.
DERIVED = ("feels_like", "app_temp", "app_tempin", "dew_point", "vpd")
FMT = "%Y-%m-%d %H:%M:%S"


def _merge_data(into: dict, new: dict, window: tuple[int, int] | None = None):
    """Merge one Ecowitt 'data' object into another, optionally only timestamps in window."""
    for grp, fields in new.items():
        if not isinstance(fields, dict):
            continue
        for field, obj in fields.items():
            if not isinstance(obj, dict) or not isinstance(obj.get("list"), dict):
                continue
            entry = into.setdefault(grp, {}).setdefault(field, {"unit": obj.get("unit", ""), "list": {}})
            for ts, value in obj["list"].items():
                if window is None or window[0] <= int(ts) <= window[1]:
                    entry["list"][ts] = value


class Fetcher:
    """Gets history for one question: from the cache where possible, otherwise from
    Ecowitt one request at a time, retrying when it says it's busy."""

    def __init__(self, mcp: MCPManager, oname: str, base_args: dict, tz: tzinfo, cache: HistoryCache | None):
        self.mcp, self.oname, self.base, self.tz, self.cache = mcp, oname, base_args, tz, cache
        self.lock = asyncio.Lock()
        self.ranges = 0         # ranges asked for
        self.from_cache = 0     # ranges answered entirely from the cache
        self.requests = 0       # requests sent to Ecowitt (incl. retries)
        self.from_memory = 0    # recent readings reused from memory instead of Ecowitt
        self.errors: list[str] = []

    def _epoch(self, local: datetime) -> int:
        return int(local.replace(tzinfo=self.tz).timestamp())

    def _local(self, epoch: int) -> datetime:
        return datetime.fromtimestamp(epoch, timezone.utc).astimezone(self.tz).replace(tzinfo=None)

    async def get(self, cycle: str, start: datetime, end: datetime, refresh: bool = False) -> dict:
        """Readings for [start, end]: settled ones from the disk cache, recent ones from
        memory if fetched in the last few minutes, the rest from Ecowitt. refresh=True
        ignores the in-memory copy (used by the keep-warm loop)."""
        self.ranges += 1
        callback = str(self.base.get("callback", ""))
        groups = self._groups()
        mac = str(self.base.get("mac", "")).strip().upper()
        if self.cache is None or not groups or not mac or any("." in g for g in groups):
            return await self._request(cycle, start, end, callback) or {}  # uncacheable request

        s, e = self._epoch(start), self._epoch(end)
        async with HOT.lock(mac, cycle):
            missing = {g: await asyncio.to_thread(self.cache.missing, mac, cycle, g, s, e) for g in groups}
            gaps = merge_intervals([iv for ivs in missing.values() for iv in ivs])
            if not gaps:
                self.from_cache += 1
            fresh: dict = {}
            for gap_start, gap_end in gaps:
                need = [g for g in groups if any(a <= gap_end and b >= gap_start for a, b in missing[g])]
                hot = {g: HOT.get(mac, cycle, g, gap_start, gap_end) for g in need} if not refresh else {}
                if need and all(v is not None for v in hot.values()) and hot:
                    self.from_memory += 1
                    _merge_data(fresh, hot, (gap_start, gap_end))
                    continue
                data = await self._request(cycle, self._local(gap_start), self._local(gap_end), ",".join(need))
                if data is None:
                    continue  # failed: not recorded as fetched, so it's retried next time
                await asyncio.to_thread(self.cache.store, mac, cycle, need, data, gap_start, gap_end)
                for g in need:
                    HOT.put(mac, cycle, g, gap_start, gap_end, data.get(g) or {})
                _merge_data(fresh, data)
            result = await asyncio.to_thread(self.cache.load, mac, cycle, groups, s, e)
        _merge_data(result, fresh, (s, e))  # recent readings aren't on disk; use the fresh ones
        return result

    def _groups(self) -> list[str]:
        return [g.strip() for g in str(self.base.get("callback", "")).split(",") if g.strip()]

    async def covered(self, cycle: str, start: int, end: int) -> bool:
        """True if the cache holds every requested group for [start, end] (epoch seconds)."""
        mac, groups = str(self.base.get("mac", "")).strip().upper(), self._groups()
        if self.cache is None or not groups or not mac or any("." in g for g in groups):
            return False
        for g in groups:
            if await asyncio.to_thread(self.cache.missing, mac, cycle, g, start, end):
                return False
        return True

    async def finer_cycle(self, ts: int, current: str, window_end: int, now_utc: datetime):
        """Finest cycle finer than `current` available for this window: still kept by
        Ecowitt, or already in the cache (e.g. archived 5-minute data). None if neither."""
        age = (now_utc - datetime.fromtimestamp(ts, timezone.utc)).days
        for cycle in ("5min", "30min", "4hour"):
            if cycle == current:
                return None
            if age < RETENTION[cycle] - 1 or await self.covered(cycle, ts, window_end):
                return cycle
        return None

    async def _request(self, cycle: str, start: datetime, end: datetime, callback: str) -> dict | None:
        """One Ecowitt request. Returns its data ({} if none) or None if it failed."""
        req = {**self.base, "callback": callback, "cycle_type": cycle,
               "start_date": start.strftime(FMT), "end_date": end.strftime(FMT)}
        label = f"{cycle} {start:%d %b %Y} - {end:%d %b %Y}"
        async with self.lock:
            for attempt in range(BUSY_RETRIES + 1):
                self.requests += 1
                # ecowitt-mcp returns just Ecowitt's "data" object on success, and an
                # MCP error with the message text (e.g. "System is busy.") on failure.
                try:
                    parts, is_error = await self.mcp.call_raw(self.oname, req)
                    text = "\n".join(parts)
                    if is_error:
                        msg = text.strip()[:200] or "unknown error"
                    else:
                        body = json.loads(text)
                        if isinstance(body, dict) and "code" in body and "data" in body:  # raw API shape
                            if str(body["code"]) != "0":
                                raise ValueError(body.get("msg", "API error"))
                            body = body["data"]
                        return body if isinstance(body, dict) else {}  # [] = no data
                except Exception as e:
                    msg = str(e) or type(e).__name__
                if "busy" in str(msg).lower() and attempt < BUSY_RETRIES:
                    wait = 1.5 * 2 ** attempt
                    log.info("Ecowitt busy (%s), retrying in %.1fs", label, wait)
                    await asyncio.sleep(wait)
                    continue
                log.warning("History request failed (%s): %s", label, msg)
                self.errors.append(f"{label}: {msg}")
                return None
        return None


def _collect(store: dict, data: dict, cycle: str):
    """Fold an Ecowitt 'data' object into store[group.field] = {unit, pts: {ts: rec}}.
    rec holds value / low / high as (float, raw string) and the source cycle."""
    for group, fields in data.items():
        if not isinstance(fields, dict):
            continue
        for fname, fobj in fields.items():
            if not isinstance(fobj, dict) or not isinstance(fobj.get("list"), dict):
                continue
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


def _extreme(rec: dict, want: str):
    """(value, raw, is_true_extreme) for 'low' or 'high' from one point."""
    if want in rec:
        return (*rec[want], True)
    if "value" in rec:
        return (*rec["value"], rec["cycle"] == "5min")  # 5-minute readings are real readings
    return None


def _better(want: str, a: float, b: float) -> bool:
    return a < b if want == "low" else a > b


def _series_extremes(series: dict, lo_ts: int = None, hi_ts: int = None) -> dict:
    """Overall low/high of a series (optionally only points with lo_ts <= ts <= hi_ts).
    Each is (value, raw, ts, cycle, is_true_extreme)."""
    out = {}
    for ts, rec in series["pts"].items():
        if lo_ts is not None and not lo_ts <= ts <= hi_ts:
            continue
        for want in ("low", "high"):
            ex = _extreme(rec, want)
            if ex and (want not in out or _better(want, ex[0], out[want][0])):
                out[want] = (ex[0], ex[1], ts, rec["cycle"], ex[2])
    return out


def _local_day_extremes(series: dict, tz: tzinfo) -> dict:
    """{local date: {"low": raw, "high": raw}} from sub-daily points (correct local days)."""
    days: dict = {}
    for ts, rec in series["pts"].items():
        d = datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).date()
        slot = days.setdefault(d, {})
        for want in ("low", "high"):
            ex = _extreme(rec, want)
            if ex and (want not in slot or _better(want, ex[0], slot[want][0])):
                slot[want] = ex
    return {d: {w: v[1] for w, v in s.items()} for d, s in days.items()}


def _clock(dt: datetime) -> str:
    """12-hour clock, e.g. '7:05am', '3:30pm', '10am'."""
    text = dt.strftime("%I:%M%p").lstrip("0").lower()
    return text.replace(":00", "") if dt.minute == 0 else text


def _day(dt: datetime) -> str:
    return f"{dt:%a} {dt.day} {dt:%b %Y}"


def _describe_time(ts: int, cycle: str, tz: tzinfo) -> tuple[str, str, str]:
    """(raw local time, ready-made wording, date) for an extreme found in a point of
    this cycle. The wording already says "at" (5-minute readings), "around" (30-minute
    slots) or gives the window it happened in, so the model can copy it as is. The
    date is separate (so an emoji can go before it) and empty when the window spans
    two days, since then the wording carries both dates."""
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


async def fetch_history(mcp: MCPManager, oname: str, tz: tzinfo, unit_params: dict, args: dict,
                        cache: HistoryCache | None = None) -> str:
    now = datetime.now(tz).replace(tzinfo=None)
    now_utc = datetime.now(timezone.utc)
    today = now.date()
    try:
        start = datetime.fromisoformat(str(args.get("start_date") or today).replace("T", " "))
        end = datetime.fromisoformat(str(args.get("end_date") or now).replace("T", " "))
    except ValueError as e:
        return f"Error: bad date ({e}). Use 'YYYY-MM-DD HH:MM:SS'."
    start = max(start.replace(tzinfo=None), datetime.combine(today - timedelta(days=RETENTION["1day"] - 2), time()))
    end = min(end.replace(tzinfo=None), now)
    if start >= end:
        return "Error: start_date must be before end_date and within the last 4 years."

    wanted = args.get("include_derived") or []
    wanted = {wanted} if isinstance(wanted, str) else set(wanted)
    if "app_temp" in wanted:
        wanted.add("app_tempin")  # indoor's name for apparent temperature
    base = {k: v for k, v in args.items()
            if k not in ("cycle_type", "start_date", "end_date", "include_derived", "chart", *UNIT_PARAMS)}
    fetcher = Fetcher(mcp, oname, {**base, **unit_params}, tz, cache)
    store: dict = {}

    async def fetch_chunks(cycle: str, t: datetime, until: datetime):
        while t < until:
            e = min(t + MAX_SPAN[cycle] - timedelta(seconds=1), until)
            _collect(store, await fetcher.get(cycle, t, e), cycle)
            t = e + timedelta(seconds=1)

    # 1. Coarse pass. Ecowitt's 1day data covers UTC days (10am-10am in Melbourne), so it
    #    can't give local-day figures; use 30min (local-aligned) wherever a daily breakdown
    #    is shown, and 1day only to locate extremes over long ranges.
    span = end - start
    age_days = (today - start.date()).days
    thirty_floor = datetime.combine(today - timedelta(days=RETENTION["30min"] - 2), time())
    recent = datetime.combine(today - timedelta(days=6), time())
    if span <= timedelta(days=1) and (age_days < RETENTION["5min"] - 1 or await fetcher.covered(
            "5min", fetcher._epoch(start), fetcher._epoch(end))):
        _collect(store, await fetcher.get("5min", start, end), "5min")
    elif span <= timedelta(days=DETAILED_DAYS) and end > thirty_floor:
        # Local-day-aligned data: weeks already in the 5-minute archive come free from the
        # cache; the rest is fetched at 30 minutes (one request per week, cached afterwards)
        t = max(start, thirty_floor)
        while t < end:
            e = min(t + MAX_SPAN["30min"] - timedelta(seconds=1), end)
            if await fetcher.covered("5min", fetcher._epoch(t), fetcher._epoch(e)):
                _collect(store, await fetcher.get("5min", t, e), "5min")
            else:
                _collect(store, await fetcher.get("30min", t, e), "30min")
            t = e + timedelta(seconds=1)
        if start < thirty_floor:
            await fetch_chunks("1day", start, min(end, thirty_floor - timedelta(seconds=1)))
    else:
        await fetch_chunks("30min", max(start, recent), end)
        await fetch_chunks("1day", start, min(end, recent - timedelta(seconds=1)))

    if not store:
        return "Error: no history data returned." + (
            " Details: " + "; ".join(fetcher.errors[:5]) if fetcher.errors else "")

    overall = {key: ex for key, s in store.items() if len(ex := _series_extremes(s)) == 2}

    # Monthly extremes for periods over a month (each month's low/high, with times)
    months_ext: dict = {}  # key -> {month label: {"low": ext, "high": ext}}
    if span > timedelta(days=31):
        for key, series in store.items():
            mm: dict = {}
            for ts, rec in sorted(series["pts"].items()):
                slot = mm.setdefault(datetime.fromtimestamp(ts, timezone.utc).astimezone(tz).strftime("%b %Y"), {})
                for want in ("low", "high"):
                    ex = _extreme(rec, want)
                    if ex and (want not in slot or _better(want, ex[0], slot[want][0])):
                        slot[want] = (ex[0], ex[1], ts, rec["cycle"], ex[2])
            months_ext[key] = mm
    n_months = max((len(m) for m in months_ext.values()), default=0)

    # 2. Refine extremes by fetching exactly their window (a 30min slot or a UTC day) at
    #    the finest cycle available: still kept by Ecowitt, or archived in the cache. Overall
    #    records first (temperatures first); then, for periods of up to a few months, each
    #    month's temperature records. A second pass narrows a 30min slot to archived 5min data.
    slots: dict = {}  # ("all", key, want) or ("month", key, month, want) -> extreme
    for key in sorted(overall, key=lambda k: "temperature" not in k):
        for want in ("low", "high"):
            slots[("all", key, want)] = overall[key][want]
    detailed = span <= timedelta(days=DETAILED_DAYS)
    if n_months and detailed:
        for key, mm in months_ext.items():
            if key.endswith(".temperature"):
                for month, d in mm.items():
                    for want, ext in d.items():
                        slots[("month", key, month, want)] = ext
    caps = {"all": MAX_REFINE_WINDOWS, "month": MAX_MONTH_REFINE_WINDOWS}
    to_local = lambda t: datetime.fromtimestamp(t, timezone.utc).astimezone(tz).replace(tzinfo=None)
    refined = 0
    for _ in range(2):
        windows: dict = {}  # (cycle, start_ts, end_ts) -> [slot id]
        used = {"all": 0, "month": 0}
        for sid, (_, _, ts, cycle, _) in slots.items():
            finer = await fetcher.finer_cycle(ts, cycle, ts + CYCLE_SECONDS[cycle] - 1, now_utc)
            if not finer:
                continue
            w = (finer, ts, ts + CYCLE_SECONDS[cycle] - 1)
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
            _collect(fine, await fetcher.get(cycle, to_local(w_start), min(to_local(w_end), now)), cycle)
            for sid in targets:
                key, want = sid[1], sid[-1]
                if key not in fine:
                    continue
                f = _series_extremes(fine[key], w_start, w_end).get(want)
                if not f:
                    continue
                cur = slots[sid]
                value = f if not _better(want, cur[0], f[0]) else cur  # keep the more extreme value
                slots[sid] = (value[0], value[1], f[2], f[3], value[4])  # time from finer data
    for sid, ext in slots.items():
        if sid[0] == "all":
            overall[sid[1]][sid[2]] = ext
        else:
            months_ext[sid[1]][sid[2]][sid[3]] = ext

    # 3. Compact answer for the model
    series_out = {}
    for key, ext in overall.items():
        if key.split(".", 1)[-1] in DERIVED and key.split(".", 1)[-1] not in wanted:
            continue  # still fetched and cached; just not sent unless asked for
        entry = {"unit": store[key]["unit"]}
        for want in ("low", "high"):
            v, raw, ts, cycle, true_extreme = ext[want]
            entry[want] = raw
            entry[f"{want}_time"], entry[f"{want}_when"], entry[f"{want}_date"] = _describe_time(ts, cycle, tz)
            if not true_extreme:
                entry[f"{want}_note"] = "from averaged data; the real value may be more extreme"
        pts = store[key]["pts"]
        if span <= timedelta(days=31):
            sub_daily = {"pts": {t: r for t, r in pts.items() if r["cycle"] != "1day"}}
            days = _local_day_extremes(sub_daily, tz)
            entry["daily"] = {d.strftime("%a %d %b"): days[d] for d in sorted(days)}
        else:
            mm = months_ext.get(key, {})
            if detailed:  # each month's low/high with when and date
                entry["monthly"] = {}
                for month, d in mm.items():
                    e = {}
                    for want in ("low", "high"):
                        if want in d:
                            _, raw, ts, cycle, _ = d[want]
                            e[want] = raw
                            _, e[f"{want}_when"], e[f"{want}_date"] = _describe_time(ts, cycle, tz)
                    entry["monthly"][month] = e
            else:  # long periods: values only, to keep the result small
                entry["monthly"] = {month: {w: d[w][1] for w in d} for month, d in mm.items()}
        series_out[key] = entry

    log.info("History %s -> %s: %d range(s), %d fully cached, %d recent from memory, %d Ecowitt request(s), "
             "%d failed, %d series, %d refined", start, end, fetcher.ranges, fetcher.from_cache,
             fetcher.from_memory, fetcher.requests, len(fetcher.errors), len(series_out), refined)
    out = {"period": f"{start:%a %d %b %Y} - {end:%a %d %b %Y}", "series": series_out}
    if months_ext and not detailed:
        out["monthly_note"] = ("Monthly figures for long periods come from daily data that runs 10am-10am, so they "
                               "have no dates, and a low early on the 1st may be counted in the previous month.")
    holder = CHART_REQUESTS.get()
    if args.get("chart") and holder is not None and series_out:
        spec = _chart_spec(series_out, store, overall, start, end, tz)
        if spec:
            holder.append(spec)
            out["chart"] = ("Your reply becomes the caption of a chart of this data, so keep it to one or two short lines: "
                                "the period and the most notable point (e.g. the peak). No lists or breakdowns; "
                                "don't mention or describe the chart.")
    if fetcher.errors:
        out["missing"] = fetcher.errors[:10]
        out["warning"] = "Some data could not be fetched; the answer may be incomplete. Say so."
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def _chart_spec(series_out: dict, store: dict, overall: dict, start: datetime, end: datetime,
                tz: tzinfo) -> dict | None:
    """Line chart spec: one line per reading type asked about (temperature if present,
    else the first), plotting its readings at the finest resolution fetched for the
    whole period (5- or 30-minute readings, or daily averages for long periods), plus
    the true record high and low with their times."""
    keys = [k for k in series_out if k.endswith(".temperature")]
    if not keys:
        field = next(iter(series_out)).split(".", 1)[-1]
        keys = [k for k in series_out if k.endswith("." + field)]
    field = keys[0].split(".", 1)[-1]
    unit = series_out[keys[0]]["unit"].replace("\u00ba", "\u00b0")
    a, b = start.date(), end.date()
    if a == b:
        period = f"{a:%a} {a.day} {a:%b %Y}"
    elif (a.year, a.month) == (b.year, b.month):
        period = f"{a:%a} {a.day} \u2013 {b:%a} {b.day} {b:%b %Y}"
    elif a.year == b.year:
        period = f"{a:%a} {a.day} {a:%b} \u2013 {b:%a} {b.day} {b:%b %Y}"
    else:
        period = f"{a:%a} {a.day} {a:%b %Y} \u2013 {b:%a} {b.day} {b:%b %Y}"
    label = lambda k: k.split(".", 1)[0].replace("_", " ").capitalize()
    names = {"5min": "5-minute readings", "30min": "30-minute readings", "4hour": "4-hour averages",
             "1day": "daily averages"}

    series, resolution = [], None
    for k in keys:
        pts = {t: r for t, r in store[k]["pts"].items() if "value" in r}
        if not pts:
            continue
        counts: dict = {}
        for r in pts.values():
            counts[r["cycle"]] = counts.get(r["cycle"], 0) + 1
        sub_daily = [c for c in ("5min", "30min") if c in counts]
        if len(sub_daily) > 1 and sum(counts[c] for c in sub_daily) >= counts.get("1day", 0):
            # archived 5-minute weeks next to 30-minute weeks: average into 30-minute bins
            cycle, bins = "30min", {}
            for t, r in pts.items():
                if r["cycle"] in sub_daily:
                    bins.setdefault(t // 1800 * 1800, []).append(r["value"][0])
            line = {t: sum(v) / len(v) for t, v in bins.items()}
        else:
            cycle = max(counts, key=counts.get)  # one resolution, so the line is consistent
            line = {t: pts[t]["value"][0] for t in pts if pts[t]["cycle"] == cycle}
        if cycle == "1day" and line:
            # The last week comes as 30-minute data: add it as daily averages so the line reaches today
            last_day = max(datetime.fromtimestamp(t, timezone.utc).astimezone(tz).date() for t in line)
            by_day: dict = {}
            today = datetime.now(tz).date()  # today's average isn't meaningful until the day is over
            for t, r in pts.items():
                d = datetime.fromtimestamp(t, timezone.utc).astimezone(tz).date()
                if r["cycle"] == "30min" and last_day < d < today:
                    by_day.setdefault(d, []).append(r["value"][0])
            for d, vals in by_day.items():
                noon = int(datetime.combine(d, datetime.min.time()).replace(hour=10, tzinfo=tz).timestamp())
                line[noon] = sum(vals) / len(vals)
        xs = sorted(line)
        if len(xs) < 2:
            continue
        resolution = resolution or names.get(cycle, cycle)
        rec = overall.get(k, {})
        records = {w: [rec[w][2], rec[w][0]] for w in ("low", "high") if w in rec}
        series.append({"label": label(k), "x": xs, "y": [line[t] for t in xs], "records": records})
    if not series:
        return None
    return {"kind": "line", "title": field.replace("_", " ").capitalize(),
            "subtitle": f"{period}  \u00b7  {resolution}", "unit": unit, "series": series}


def _unit_params_for(mcp: MCPManager, oname: str) -> dict:
    """Unit parameters this tool accepts, and hide them from the model."""
    for tool in mcp.openai_tools:
        fn = tool["function"]
        if fn["name"] == oname:
            params = json.loads(json.dumps(fn["parameters"]))
            props = params.get("properties", {})
            found = {k: v for k, v in UNIT_PARAMS.items() if k in props}
            for k in found:
                props.pop(k)
            if "required" in params:
                params["required"] = [r for r in params["required"] if r not in found]
            fn["parameters"] = params
            return found
    return {}


INSTALLED: dict = {}  # oname -> unit params, filled by install(); used by the archive and prefetch


def fetcher_factory(mcp: MCPManager, tz: tzinfo, cache: HistoryCache | None):
    """(mac, callback) -> Fetcher for the installed history tool."""
    oname, units = next(iter(INSTALLED.items()))
    return lambda mac, callback: Fetcher(mcp, oname, {"mac": mac, "callback": callback, **units}, tz, cache)


def install(mcp: MCPManager, tz: tzinfo, cache: HistoryCache | None = None) -> bool:
    found = False
    for oname in list(mcp.tools):
        if oname.endswith("__get_device_historical_info"):
            units = _unit_params_for(mcp, oname)

            async def history(args, _oname=oname, _units=units):
                return await fetch_history(mcp, _oname, tz, _units, args, cache)

            mcp.interceptors[oname] = history
            INSTALLED[oname] = units
            for tool in mcp.openai_tools:
                fn = tool["function"]
                if fn["name"] == oname:
                    fn["parameters"].get("properties", {}).pop("cycle_type", None)
                    # The server's own example ('outdoor.temp', an invalid field) and its advice to
                    # call realtime first both contradict the prompt, so replace them.
                    if "callback" in fn["parameters"].get("properties", {}):
                        fn["parameters"]["properties"]["callback"]["description"] = (
                            "Comma-separated group names, e.g. 'outdoor,indoor'. Add 'rainfall', 'wind' "
                            "or 'pressure' only if needed. Use plain group names, not dotted fields.")
                    fn["parameters"].setdefault("properties", {})["chart"] = {
                        "type": "boolean",
                        "description": "Set true to send a chart with the answer: for trends over several days "
                                       "or longer, or when a graph/chart is asked for."}
                    fn["parameters"]["properties"]["include_derived"] = {
                        "type": "array", "items": {"type": "string", "enum": ["feels_like", "app_temp", "dew_point", "vpd"]},
                        "description": "Only when the question asks about feels-like, apparent temperature, dew point "
                                       "or VPD: which of them to include. Otherwise omit (they are left out by default)."}
                    if "required" in fn["parameters"]:
                        fn["parameters"]["required"] = [r for r in fn["parameters"]["required"] if r != "cycle_type"]
                    fn["description"] = (fn["description"] + " Returns each day's low/high and the "
                                         "overall extremes with times. Any range up to 4 years; "
                                         "resolution and units are handled automatically.")[:1024]
            log.debug("History handler enabled for %s (units: %s)", oname, units or "not supported by tool")
            found = True
        elif oname.endswith("__get_device_realtime_info"):
            units = _unit_params_for(mcp, oname)
            if units:
                async def realtime(args, _oname=oname, _units=units):
                    parts, is_error = await mcp.call_raw(_oname, {**args, **_units})
                    out = "\n".join(_compact(p, mcp.max_series_points, mcp.tz) for p in parts)
                    return ("Tool reported an error:\n" + out) if is_error else out

                mcp.interceptors[oname] = realtime
                log.debug("Metric units forced for %s", oname)
    return found
