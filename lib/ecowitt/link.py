"""weather_link: does rain come with a change in another reading (pressure by default)? Both series are read together
from the cached 30-minute data, so it is about WHEN the rain fell, not what a day's extremes were. Nothing here
calls Ecowitt: it works from what the archive holds.

Per 30-minute slot: the rain that fell (the rise in the day's rain total, allowing for its midnight reset) and how
the reading had changed over the previous 3 hours. From those: how much of the rain fell while the reading was
falling, steady or rising; the reading's average level in wet and dry slots; a correlation between the change
before and the rain after; and the biggest rain spells with the change that preceded each."""

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, tzinfo

import numpy as np

from ..charts import CHART_REQUESTS, LINK_CHART_HINT, wants_chart
from ..timeutil import BAND_FROM, bucket_width, bucketed, local_date, now_local, to_local
from .store import HistoryCache

log = logging.getLogger(__name__)

SLOT = 1800                    # 30-minute data
LOOKBACK = 6                   # slots: the change is measured over the previous 3 hours
AHEAD = 6                      # slots: and the rain counted over the next 3 hours
SPELL_GAP = 6                  # slots: 3 dry hours end a rain spell
SPELL_MM = 1.0                 # a spell needs this much rain to count as an event
DEFAULT_DAYS = 90
MAX_GAP = 3 * SLOT             # a longer hole in the readings is not counted as rain
# driver -> (group, field, unit, the change over 3 hours that counts as falling or rising)
DRIVERS = {"pressure": ("pressure", "relative", "hPa", 1.0),
           "humidity": ("outdoor", "humidity", "%", 5.0),
           "wind": ("wind", "wind_speed", "km/h", 5.0)}
CHART_BARS = ((4, 3600), (31, 6 * 3600), (10 ** 6, 86400))  # (up to this many days, seconds per bar)

PARAMETERS = {
    "type": "object",
    "properties": {
        "start_date": {"type": "string", "description": f"First day, 'YYYY-MM-DD'. Default: the last {DEFAULT_DAYS} days."},
        "end_date": {"type": "string", "description": "Last day, 'YYYY-MM-DD' (up to yesterday). Default: yesterday."},
        "driver": {"type": "string", "enum": list(DRIVERS),
                   "description": "The reading to relate to rain. Default pressure (relative)."},
        "chart": {"type": "boolean", "description": "Set true for a chart: the reading as a line, rain as bars below it."},
    },
}
DESCRIPTION = ("Does rain come WITH a change in pressure (or humidity or wind)? Reads both together at 30-minute "
               "resolution from the cache and reports how much rain fell while the reading was falling, steady or "
               "rising over the previous 3 hours, the reading's level in wet and dry periods, a correlation, and the "
               "biggest rain spells with the change before each. Use it for 'is there a correlation between pressure and "
               "rainfall', 'did the rain come with the pressure drop' and 'plot pressure against rainfall'. Up to the "
               "whole record; never averaged to days.")


def rain_slots(daily: dict[int, float]) -> dict[int, float]:
    """Rain per slot from the day's running rain total: the rise since the previous reading, or the whole total
    after the midnight reset. A long hole in the readings gives 0 rather than a guess."""
    out: dict[int, float] = {}
    prev_t = prev = None
    for t in sorted(daily):
        v = daily[t]
        if prev is not None and t - prev_t <= MAX_GAP:
            out[t] = round(v - prev if v >= prev else v, 3)
        prev_t, prev = t, v
    return out


def change_before(driver: dict[int, float], t: int) -> float | None:
    then = driver.get(t - LOOKBACK * SLOT)
    now = driver.get(t)
    return None if then is None or now is None else now - then


def verdict(moving: float | None, steady: float | None) -> str | None:
    """A plain yes/no from how much more rain fell while the reading was moving than while it was steady (ratios
    against the fair share of time). Not from the correlation, which is weak by construction."""
    if moving is None or steady is None:
        return None
    if moving >= 1.5 and steady <= 0.65:
        return "Yes - a clear link"
    if moving >= 1.25 and steady <= 0.85:
        return "Yes - a moderate link"
    if 0.85 <= moving <= 1.15 and 0.85 <= steady <= 1.15:
        return "No - rain fell about evenly whatever the reading did"
    return "Weak or mixed - not a consistent link"


def _summary(table: dict, level: dict, events: list[dict], threshold: float, unit: str, clock) -> tuple[list[str], str | None]:
    """The findings in words, decided here so the answer can't lean on the correlation alone (it is weak by
    construction: most slots are dry, and rain after a fall and after a rise cancel in a signed correlation)."""
    out = []
    parts = [f"{n} {g['rain_vs_fair_share']} ({g['share_of_rain']} of the rain in {g['share_of_time']} of the time, "
             f"raining in {g['wet_share']} of its half-hours)" for n, g in table.items() if g["rain_vs_fair_share"]]
    if parts:
        out.append("Rain against its fair share of time, by the reading's change in the previous 3 hours (1.0x = no "
                   "difference): " + "; ".join(parts))
    slots = sum(g["slots"] for g in table.values())
    rain = sum(g["rain_mm"] for g in table.values())
    call = move_time = None
    if slots and rain:
        move_slots = table["falling"]["slots"] + table["rising"]["slots"]
        move_rain = table["falling"]["rain_mm"] + table["rising"]["rain_mm"]
        steady = table["steady"]
        move_time = move_slots / slots
        call = verdict((move_rain / rain) / (move_slots / slots) if move_slots else None,
                       (steady["rain_mm"] / rain) / (steady["slots"] / slots) if steady["slots"] else None)
        out.append(f"Moving either way (a change of {threshold:g} {unit} or more): {100 * move_rain / rain:.0f}% of the rain "
                   f"in {100 * move_slots / slots:.0f}% of the time"
                   + (f" = {(move_rain / rain) / (move_slots / slots):.1f}x" if move_slots else "")
                   + f"; steady: {steady['rain_vs_fair_share'] or 'n/a'}")
    if level["during_rain"] is not None and level["dry"] is not None:
        out.append(f"Average level {level['during_rain']:g} {unit} during rain vs {level['dry']:g} when dry "
                   f"({level['during_rain'] - level['dry']:+.1f})")
    if events:
        fell = sum(1 for e in events if e["change"] is not None and e["change"] <= -threshold)
        rose = sum(1 for e in events if e["change"] is not None and e["change"] >= threshold)
        moved = fell + rose
        big = max(events, key=lambda e: e["mm"])
        before = "" if big["change"] is None else f", after a change of {big['change']:+.1f} {unit}"
        out.append(f"{fell} of {len(events)} rain events (1 mm or more) began after a fall and {rose} after a rise ({100 * moved / len(events):.0f}% after a move"
                   + (f", against {100 * move_time:.0f}% of the time moving" if move_time is not None else "") + "); "
                   f"the biggest was {big['mm']:g} mm from {clock(big['start'])}{before}")
    return out, call


def analyse(driver: dict[int, float], rain: dict[int, float], threshold: float, tz: tzinfo, unit: str = "") -> dict:
    """The numbers for the answer. `driver` and `rain` are {epoch: value} at 30-minute slots."""
    slots = sorted(t for t in rain if t in driver)
    if not slots:
        return {}
    wet = [t for t in slots if rain[t] > 0]
    total = sum(rain[t] for t in slots)
    groups: dict[str, list[int]] = {"falling": [], "steady": [], "rising": []}
    for t in slots:
        c = change_before(driver, t)
        if c is not None:
            groups["falling" if c <= -threshold else "rising" if c >= threshold else "steady"].append(t)
    table = {}
    for name, ts in groups.items():
        mm = sum(rain[t] for t in ts)
        time_share = len(ts) / max(sum(map(len, groups.values())), 1)
        rain_share = mm / total if total else 0.0
        table[name] = {"slots": len(ts), "share_of_time": f"{100 * time_share:.0f}%",
                       "rain_mm": round(mm, 1), "share_of_rain": f"{100 * rain_share:.0f}%",
                       "rain_vs_fair_share": f"{rain_share / time_share:.1f}x" if time_share else None,
                       "wet_share": f"{100 * sum(1 for t in ts if rain[t] > 0) / max(len(ts), 1):.0f}%"}
    dry = [t for t in slots if rain[t] <= 0]
    level = {"during_rain": round(float(np.mean([driver[t] for t in wet])), 1) if wet else None,
             "dry": round(float(np.mean([driver[t] for t in dry])), 1) if dry else None}
    pairs = [(c, sum(rain.get(t + k * SLOT, 0.0) for k in range(1, AHEAD + 1))) for t in slots
             if (c := change_before(driver, t)) is not None and t + AHEAD * SLOT in rain]
    r = None
    if len(pairs) >= 30:
        a, b = np.array(pairs).T
        if a.std() > 0 and b.std() > 0:
            r = round(float(np.corrcoef(a, b)[0, 1]), 2)
    spells, current = [], []
    for t in wet:
        if current and t - current[-1] > SPELL_GAP * SLOT:
            spells.append(current)
            current = []
        current.append(t)
    if current:
        spells.append(current)
    events = []
    for s in spells:
        mm = sum(rain[t] for t in s)
        if mm >= SPELL_MM:
            events.append({"start": s[0], "end": s[-1], "mm": round(mm, 1), "change": change_before(driver, s[0]),
                           "lowest": min(driver[t] for t in s if t in driver)})
    fell = sum(1 for e in events if e["change"] is not None and e["change"] <= -threshold)
    rose = sum(1 for e in events if e["change"] is not None and e["change"] >= threshold)
    clock = lambda ts: (lambda dt: f"{dt:%a} {dt.day} {dt:%b} {dt.strftime('%-I:%M%p').lower()}")(to_local(ts, tz))
    summary, call = _summary(table, level, events, threshold, unit, clock)
    top = sorted(events, key=lambda e: -e["mm"])[:5]
    return {"slots": len(slots), "rain_mm": round(total, 1), "wet_slots": len(wet), "verdict": call, "findings": summary, "by_change_before": table,
            "average_level": level, "correlation_change_vs_rain_next_3h": r,
            "rain_events": {"count": len(events), "started_after_a_fall": fell, "started_after_a_rise": rose,
                            "biggest": [{"start": clock(e["start"]), "mm": e["mm"], "hours": round((e["end"] - e["start"]) / 3600 + 0.5, 1),
                                         "change_in_3h_before": None if e["change"] is None else round(e["change"], 1),
                                         "lowest_during": round(e["lowest"], 1)} for e in top]}}


def bar_layout(tz: tzinfo, first: date, last: date) -> tuple[int, int, str]:
    """(origin epoch, seconds per bar, what a bar covers) for a bar chart of this period: hourly up to 4 days, 6-hourly
    up to a month, daily beyond. Bars start at local midnight of the first day."""
    width = next(w for limit, w in CHART_BARS if (last - first).days + 1 <= limit)
    return (int(datetime.combine(first, time()).replace(tzinfo=tz).timestamp()), width,
            {3600: "hour", 6 * 3600: "6 hours", 86400: "day"}[width])


def rain_bars(rain: dict[int, float], tz: tzinfo, first: date, last: date) -> dict:
    """Rain summed into bars (see bar_layout)."""
    origin, width, per = bar_layout(tz, first, last)
    bars: dict[int, float] = {}
    for t, mm in rain.items():
        if mm > 0:
            k = origin + (t - origin) // width * width
            bars[k] = bars.get(k, 0.0) + mm
    xs = sorted(bars)
    return {"x": xs, "y": [round(bars[k], 2) for k in xs], "width": width, "per": per}


def driver_series(driver: dict[int, float], tz: tzinfo, first: date, last: date, label: str,
                  lows: dict[int, float] | None = None, highs: dict[int, float] | None = None, keep_band: bool = False) -> dict | None:
    """The reading as a line: the 30-minute readings while they fit the point budget, else bucketed (hourly ... daily);
    a day's mean has its range shaded, from Ecowitt's own 30-minute lows and highs where the cache holds them."""
    if len(driver) < 2:
        return None
    ts = sorted(driver)
    width = bucket_width(((last - first).days + 1) * 86400, SLOT)
    if width <= SLOT and not keep_band:
        return {"label": label, "x": ts, "y": [driver[t] for t in ts]}
    if width <= SLOT:  # a band at every width (wind: the speed, shaded up to the gusts)
        return {"label": label, "x": ts, "y": [driver[t] for t in ts], "low": [(lows or {}).get(t, driver[t]) for t in ts],
                "high": [max((highs or {}).get(t, driver[t]), driver[t]) for t in ts]}
    lows, highs = lows or {}, highs or {}
    xs, mean, low, high = bucketed(((t, driver[t], lows.get(t, driver[t]), highs.get(t, driver[t]), False) for t in ts), tz, width)
    return {"label": label, "x": xs, "y": mean, **({"low": low, "high": high} if width >= BAND_FROM or keep_band else {})}


def chart_spec(driver: dict[int, float], rain: dict[int, float], tz: tzinfo, first: date, last: date,
               name: str, unit: str, lows: dict[int, float] | None = None, highs: dict[int, float] | None = None) -> dict | None:
    """Two panels on one time axis: the reading above the rain."""
    line = driver_series(driver, tz, first, last, name.capitalize(), lows, highs)
    if line is None:
        return None
    bars = rain_bars(rain, tz, first, last)
    return {"kind": "stack", "title": f"{name.capitalize()} and rain",
            "subtitle": f"{first:%a} {first.day} {first:%b} – {last:%a} {last.day} {last:%b %Y}  ·  rain per {bars.pop('per')}",
            "panels": [{"label": name.capitalize(), "unit": unit, "series": [line]},
                       {"label": "Rain", "unit": "mm", "bars": bars}]}


def link(cache: HistoryCache, mac: str, tz: tzinfo, args: dict, now: datetime) -> tuple[dict, dict | None, date | None, date | None]:
    """(result for the model, chart spec or None, first day, last day)."""
    today = now.date()
    try:
        last = min(date.fromisoformat(str(args["end_date"])[:10]), today - timedelta(days=1)) if args.get("end_date") \
            else today - timedelta(days=1)
        first = date.fromisoformat(str(args["start_date"])[:10]) if args.get("start_date") else last - timedelta(days=DEFAULT_DAYS - 1)
    except ValueError as e:
        return {"error": f"bad date ({e}); use 'YYYY-MM-DD'"}, None, None, None
    if first > last:
        return {"error": "start_date must be before end_date (today isn't final, so the latest day is yesterday)"}, None, None, None
    name = args.get("driver") or "pressure"
    if name not in DRIVERS:
        return {"error": f"driver must be one of {', '.join(DRIVERS)}"}, None, None, None
    group, field, unit, threshold = DRIVERS[name]
    lo = int(datetime.combine(first, time()).replace(tzinfo=tz).timestamp())
    hi = int(datetime.combine(last, time(23, 59, 59)).replace(tzinfo=tz).timestamp())
    def pick(g: str, *fields: str) -> list[dict[int, float]]:
        got = cache.load_fields(mac, "30min", g, list(fields), lo, hi)
        return [{int(t): float(v) for t, v in got.get(f, {"list": {}})["list"].items()} for f in fields]
    (driver, lows, highs), (daily,) = pick(group, field, field + "_low", field + "_high"), pick("rainfall", "daily")
    rain = rain_slots(daily)
    out = {"period": f"{first:%a} {first.day} {first:%b %Y} - {last:%a} {last.day} {last:%b %Y}", "driver": f"{name} ({unit})",
           "resolution": "30-minute readings (not averaged to days)",
           "how_to_read": ("by_change_before groups each 30-minute slot by how the reading had changed over the previous 3 "
                           f"hours: falling = down {threshold:g} {unit} or more, rising = up {threshold:g} or more. share_of_rain "
                           "is the share of all the rain that fell in that group and rain_vs_fair_share compares it with share_of_time. "
                           "Open with `verdict` (Yes / No / Weak) in your own words, then give the evidence from `findings`. The correlation (3-hour change against the next 3 hours' rain) is weak by "
                           "construction, so a small value is NOT evidence of no link: say 'no link' only if findings show it "
                           "too (every group near 1.0x, no lower level in rain).")}
    result = analyse(driver, rain, threshold, tz, unit)
    log.info("Link %s to %s (%s): %d %s readings, %d rain readings, %d slots in common", first, last, name, len(driver),
             field, len(daily), len(set(rain) & set(driver)))
    if not result:
        missing = [what for what, got in ((f"{name} ({group}.{field})", driver), ("rainfall (rainfall.daily)", daily)) if not got]
        out["note"] = ("No cached 30-minute readings of " + " or ".join(missing) + " for this period." if missing else
                       "The two series share no 30-minute readings in this period.") + " Say exactly that."
        return out, None, first, last
    out.update(result)
    out["days_with_data"] = len({local_date(t, tz) for t in rain})
    spec = chart_spec(driver, rain, tz, first, last, name, unit, lows, highs)
    return out, spec, first, last


async def link_tool(cache: HistoryCache, mac: str, tz: tzinfo, args: dict) -> str:
    out, spec, first, last = await asyncio.to_thread(link, cache, mac, tz, args, now_local(tz))
    holder = CHART_REQUESTS.get()
    if spec and holder is not None and wants_chart(args, datetime.combine(first, time()), datetime.combine(last, time())):
        holder.append(spec)
        out["chart"] = LINK_CHART_HINT
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))
