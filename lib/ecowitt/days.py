"""Find, rank and count days by what the station read, from the cache alone (never asks Ecowitt).

Questions like "the hottest day it also rained" or "how many days over 35" compare readings on the
same day across a long period, which the history tool's summaries can't answer. Here each day is
worked out in code from the best data the cache holds for it:
  - 5-minute or 30-minute readings, where cached: exact local days (midnight to midnight);
  - otherwise the daily buckets, which run 10am to 10am local time, so those days are approximate
    (a morning shower can land on the day before). They are flagged in the answer.
The exact range grows by itself as the archive keeps more sub-daily data.
"""

import asyncio
import json
import logging
import operator
from datetime import date, datetime, timedelta, tzinfo

from ..timeutil import day_bounds, local_date, now_local
from .history import collect
from .store import HistoryCache, subtract

log = logging.getLogger(__name__)

# friendly name -> (group, field, which extreme of the day, unit)
FIELDS = {
    "temp_max": ("outdoor", "temperature", "high", "°C"),
    "temp_min": ("outdoor", "temperature", "low", "°C"),
    "rain": ("rainfall", "daily", "high", "mm"),        # the day's total: the rain counter's highest reading
    "wind_gust": ("wind", "wind_gust", "high", "km/h"),
}
ALWAYS_SHOWN = ("temp_max", "temp_min", "rain")
OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "=": operator.eq}
SUB_DAILY = ("5min", "30min")
MAX_LIMIT = 20

PARAMETERS = {
    "type": "object",
    "properties": {
        "start_date": {"type": "string", "description": "First day, 'YYYY-MM-DD'. Use the start of the 'on record' range for all time."},
        "end_date": {"type": "string", "description": "Last day, 'YYYY-MM-DD' (today's readings aren't final, so up to yesterday)."},
        "where": {"type": "array", "description": "Conditions that must ALL hold on the same day, e.g. rain >= 1 and temp_max > 30.",
                  "items": {"type": "object", "properties": {
                      "field": {"type": "string", "enum": list(FIELDS)},
                      "op": {"type": "string", "enum": list(OPS)},
                      "value": {"type": "number"}}, "required": ["field", "op", "value"]}},
        "sort_by": {"type": "string", "enum": list(FIELDS), "description": "Rank matching days by this. Default: the first condition's field."},
        "order": {"type": "string", "enum": ["desc", "asc"], "description": "desc = highest first (default), asc = lowest first."},
        "limit": {"type": "integer", "description": f"How many days to list (default 5, at most {MAX_LIMIT}). The total is always counted."},
    },
    "required": ["start_date", "end_date"],
}
DESCRIPTION = ("Find, rank or count DAYS by the station's readings, checking every day in the period: for questions that "
               "compare readings on the same day or count days (\"the hottest day it also rained\", \"how many days over "
               "35°C\", \"the wettest day\", \"the windiest cold day\"). Fields: temp_max / temp_min (outdoor °C), "
               "rain (mm total for the day; a rainy day is 1 mm or more, above 0 is only a trace), wind_gust (km/h, highest). Returns the total of matching "
               "days and the top ones. Works from cached history, so any period up to the whole record is fast.")


def load_cached(cache: HistoryCache, mac: str, cycle: str, names: list[str], start: int, end: int) -> dict:
    """The cached readings of the wanted fields, folded by collect() into {"group.field": {"pts": ...}}."""
    data: dict = {}
    for name in names:
        group, field, _, _ = FIELDS[name]
        found = cache.load_fields(mac, cycle, group, [field, f"{field}_high", f"{field}_low"], start, end)
        data.setdefault(group, {}).update(found)
    store: dict = {}
    collect(store, data, cycle)
    return store


def per_day(store: dict, name: str, days: set[date], tz: tzinfo) -> dict[date, float]:
    """One extreme per day (the highest high, or the lowest low) of the readings that fall in `days`."""
    group, field, kind, _ = FIELDS[name]
    out: dict[date, float] = {}
    for ts, rec in store.get(f"{group}.{field}", {}).get("pts", {}).items():
        day = local_date(ts, tz)
        found = rec.get(kind) or rec.get("value")
        if day in days and found is not None:
            if day not in out or (found[0] > out[day] if kind == "high" else found[0] < out[day]):
                out[day] = found[0]
    return out


def find_days(cache: HistoryCache, mac: str, tz: tzinfo, args: dict, now: datetime) -> dict:
    today = now.date()
    try:
        first = date.fromisoformat(str(args["start_date"])[:10])
        last = min(date.fromisoformat(str(args["end_date"])[:10]), today - timedelta(days=1))
    except (KeyError, ValueError) as e:
        return {"error": f"bad date ({e}); use 'YYYY-MM-DD'"}
    if first > last:
        return {"error": "start_date must be before end_date (today isn't final, so the latest day is yesterday)"}
    where = args.get("where") or []
    bad = [c for c in where if c.get("field") not in FIELDS or c.get("op") not in OPS or not isinstance(c.get("value"), (int, float))]
    if bad:
        return {"error": f"unusable condition(s) {bad}; fields are {', '.join(FIELDS)}, ops {' '.join(OPS)}"}
    sort_by = args.get("sort_by") or (where[0]["field"] if where else "temp_max")
    if sort_by not in FIELDS:
        return {"error": f"sort_by must be one of {', '.join(FIELDS)}"}
    shown = list(dict.fromkeys([*ALWAYS_SHOWN, sort_by, *(c["field"] for c in where)]))
    limit = max(1, min(int(args.get("limit") or 5), MAX_LIMIT))
    groups = sorted({FIELDS[n][0] for n in shown})

    # which cycle each day is answered from: exact sub-daily data where every needed group has it
    days = [first + timedelta(days=k) for k in range((last - first).days + 1)]
    source: dict[date, str] = {}
    for cycle in SUB_DAILY:
        cover = {g: cache.coverage(mac, cycle, g) for g in groups}
        for d in days:
            lo, hi = day_bounds(d, tz, True)
            if d not in source and all(not subtract((lo, hi), cover[g]) for g in groups):
                source[d] = cycle
    rest = {d for d in days if d not in source}

    values: dict[str, dict[date, float]] = {n: {} for n in shown}
    for cycle in SUB_DAILY:
        mine = {d for d, c in source.items() if c == cycle}
        if mine:
            store = load_cached(cache, mac, cycle, shown, day_bounds(min(mine), tz, True)[0], day_bounds(max(mine), tz, True)[1])
            for n in shown:
                values[n].update(per_day(store, n, mine, tz))
    if rest:  # daily buckets: labelled with the local date their 10am start falls on
        store = load_cached(cache, mac, "1day", shown, day_bounds(min(rest), tz, True)[0] - 86400, day_bounds(max(rest), tz, True)[1] + 86400)
        for n in shown:
            values[n].update(per_day(store, n, rest, tz))
        for d in rest:
            if any(d in values[n] for n in shown):
                source[d] = "daily"

    checked = [d for d in days if any(d in values[n] for n in shown)]
    holds = lambda d, conds: all(d in values[c["field"]] and OPS[c["op"]](values[c["field"]][d], c["value"]) for c in conds)
    rank = lambda ds: sorted((d for d in ds if d in values[sort_by]), key=lambda d: values[sort_by][d],
                             reverse=args.get("order", "desc") != "asc")
    label = lambda d: f"{d:%a} {d.day} {d:%b %Y}"
    row = lambda d: {"date": label(d), **{n: round(values[n][d], 1) for n in shown if d in values[n]},
                     "source": "daily" if source.get(d) == "daily" else "exact"}
    matches = [d for d in checked if holds(d, where)]
    ranked = rank(matches)
    rows = [row(d) for d in ranked[:limit]]

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
    out = {"period": f"{label(first)} - {label(last)}", "days_checked": len(checked), "matching_days": len(matches),
           "units": {n: FIELDS[n][3] for n in shown}, "days": rows}
    if trace:
        out["trace_rain_days"] = [row(d) for d in trace]
        out["note_trace"] = (f"These days rank higher but had less than {rain_cond['value']:g} mm of rain (a trace), so they "
                             "are not in \"days\". Mention the first as a short footnote.")
    if any(source.get(d) == "daily" for d in checked):
        out["exact_from"] = label(exact_days[0]) if exact_days else None
        out["note_daily"] = ("Days marked \"daily\" come from daily readings that run 10am to 10am, so their rain and dates "
                             "are approximate (a morning shower can be counted on the day before). Say so briefly if any "
                             "listed day is marked daily, or if the period reaches back before exact_from.")
    if not checked:
        out["note"] = "No cached readings for this period yet (the bot may still be downloading history)."
    log.info("Days %s to %s: %d checked, %d match, %d exact", first, last, len(checked), len(matches), len(exact_days))
    return out


async def days_tool(cache: HistoryCache, mac: str, tz: tzinfo, args: dict) -> str:
    now = now_local(tz)
    result = await asyncio.to_thread(find_days, cache, mac, tz, args, now)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
