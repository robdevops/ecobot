"""The `outlook` tool (docs/mcp-service.md 3.3): will it rain in the next few hours. No arguments.

The bot's prompt told the model how to read the station for this (lib/prompt.py, its "outlook" block) and fetched the reading now and
the last 3 hours for it (intent.model_prefetch). Here the same rules are worked out in code, from the station's 5-minute readings of
the last 3 hours (as Ecowitt.recent reads them) and its reading now, so any model gets the same read:

  - Pressure tendency (relative pressure over the 3 hours): falling when its highest came before its lowest, rising when the lowest
    came first; the change is the highest minus the lowest. Down more than 3 hPa: rain likely; down 1 to 3 hPa: possible; under
    1 hPa (steady) or rising: unlikely.
  - Each of these moves that a step towards rain: pressure under 1005 hPa (unsettled); moist air, humidity 90% or more or the
    temperature within 2 °C of the dew point; wind picking up while the pressure falls. Pressure over 1020 hPa (settled) moves it a
    step away. The verdict stays between unlikely and likely.
  - Recent rain, a rain rate above 0 now or rain in the last hour, makes it likely: showers tend to go on for a while.

The answer: {as_of, fresh, verdict, reason ("pressure down 2.4 hPa in 3 hours, humidity 91%"), pressure {hpa, change_hpa, tendency,
level, text}, moisture {humidity, dew_point_spread, moist, text}, recent_rain {rained, raining_now, rate_mm_h, last_hour_mm, text},
wind_trend {trend, first_hour_kmh, last_hour_kmh, text}, [missing: what the station had none of], [forecast: today's and tomorrow's
lines, when the forecast is on, with forecast_note when it has none], [warning: the newest reading is old], how_to_read}. `read`
works it out from the readings alone; the tool fetches them (`readings`).
"""

import logging
import math
from collections.abc import Sequence

from ..ecowitt.reads import rows_on
from ..ecowitt.source import LIVE_GROUPS
from ..timeutil import clock_text, day_text, naive_local, to_local
from .common import BACKGROUND, Refusal, ServiceTool, answer
from .forecast import day_lines, held_days

log = logging.getLogger(__name__)

HOURS = 3                       # the station's last 3 hours
RECENT_SECONDS = 3600           # rain in the last hour counts as recent
LIKELY_FALL_HPA = 3.0           # pressure down more than this: likely
STEADY_HPA = 1.0                # a change under this is steady
UNSETTLED_HPA = 1005.0          # pressure under this is unsettled ...
SETTLED_HPA = 1020.0            # ... over this, settled
MOIST_HUMIDITY = 90.0           # humidity at or above this ...
MOIST_SPREAD_C = 2.0            # ... or the temperature within this of the dew point: moist air
WIND_CHANGE_KMH = 5.0           # the last hour's mean wind against the first hour's: picking up (or easing) by this much
STALE_SECONDS = 30 * 60         # a newest reading older than this is warned about
VERDICTS = ("unlikely", "possible", "likely")
GROUPS = ("outdoor", "pressure", "rainfall", "rainfall_piezo", "wind")   # what the read needs (the prefetch's groups)
GAUGES = ("rainfall", "rainfall_piezo")   # the rain gauges, the main one first

Rows = Sequence[tuple[int, dict]]


# ---------- the read, from the readings ----------
def read(rows: Rows, now_reading: dict | None, now_ts: int, live_ts: int | None = None) -> dict:
    """The outlook's fields from the station's readings: `rows`, the last HOURS of 5-minute readings ([(epoch, {"group.field": value})],
    oldest first, as Ecowitt.recent gives them), `now_reading` the reading now ({"group.field": value}, None when there is none) of
    `live_ts`, and `now_ts` the time asked. The fields: verdict, reason, pressure, moisture, recent_rain, wind_trend, [missing], and
    `newest`, the time of the newest reading read (None when there is none)."""
    live = dict(now_reading or {}) if live_ts else {}
    if live and rows and live_ts <= rows[-1][0]:   # the reading now is older than the newest 5 minutes': the rows say it all
        live = {}
    last = rows[-1][1] if rows else {}
    newest = max([ts for ts in (live_ts if live else None, rows[-1][0] if rows else None) if ts], default=None)
    gauge = next((g for g in GAUGES if any(f"{g}.daily" in r or f"{g}.rain_rate" in r for r in (live, last))), GAUGES[0])

    def points(key: str) -> list[tuple[int, float]]:
        out = [(ts, float(r[key])) for ts, r in rows if _number(r.get(key))]
        if _number(live.get(key)):
            out.append((live_ts, float(live[key])))
        return out

    def latest(*keys: str) -> list[float | None]:
        """These readings now, all from one reading: the reading now when it has them all, else the newest 5 minutes'."""
        for source in (live, last):
            if all(_number(source.get(k)) for k in keys):
                return [float(source[k]) for k in keys]
        return [float(v) if _number(v := live.get(k, last.get(k))) else None for k in keys]

    pressure = _pressure(points("pressure.relative"))
    moisture = _moisture(*latest("outdoor.temperature", "outdoor.humidity", "outdoor.dew_point"))
    until = newest or now_ts   # "the last hour" ends at the newest reading held: stale readings (a read without fetching, just after
    #                            midnight) still count their rain, and the result's as_of and warning say how old they are
    rain = _recent_rain(rows, live, live_ts, gauge, until)
    wind = _wind(points("wind.wind_speed"), until)

    falling = pressure is not None and pressure["tendency"] == "falling"
    score = 0 if not falling else 2 if -pressure["change_hpa"] > LIKELY_FALL_HPA else 1
    wind_counts = falling and wind is not None and wind["trend"] == "picking up"
    up, down = [], []   # the readings behind the verdict, towards rain and away from it, the strongest first
    if rain is not None and rain["rained"]:
        up.append(rain["why"])
    if falling:
        up.append(f"pressure {pressure['change']}")
    if moisture is not None and moisture["moist"]:
        score += 1
        up.append(moisture["why"])
    if pressure is not None and pressure["level"] == "unsettled":
        score += 1
        up.append(f"low pressure ({pressure['hpa']:.1f} hPa)")
    if wind_counts:
        score += 1
        up.append("wind picking up")
    if pressure is not None and not falling:
        down.append(f"pressure {pressure['change']}")
    if pressure is not None and pressure["level"] == "settled":
        score -= 1
        down.append(f"high pressure ({pressure['hpa']:.1f} hPa)")
    besides = [f"humidity {moisture['humidity']:.0f}%"] if moisture is not None and not moisture["moist"] and \
        moisture["humidity"] is not None else []   # (a figure to quote when one reading alone is behind the verdict)
    score = 2 if rain is not None and rain["rained"] else min(max(score, 0), 2)
    verdict = VERDICTS[score]
    parts = (down + up if verdict == "unlikely" else up + down) + besides
    out = {"verdict": verdict, "reason": ", ".join(parts[:2]) or "too few readings in the last 3 hours to tell",
           "pressure": _public(pressure), "moisture": _public(moisture), "recent_rain": _public(rain), "wind_trend": wind}
    if missing := [name for name, got in (("pressure", pressure), ("humidity and dew point", moisture), ("rain", rain), ("wind", wind))
                   if got is None]:
        out["missing"] = missing
    out["newest"] = newest
    return out


def _number(value) -> bool:
    """Is it a reading's value: a finite number?"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _public(fields: dict | None) -> dict | None:
    """The fields an answer shows: those the read keeps for its reason (`change`, `why`) left out."""
    return None if fields is None else {k: v for k, v in fields.items() if k not in ("change", "why")}


def _over(seconds: float) -> str:
    """"3 hours", "1.5 hours", "1 hour", "30 minutes": how long the readings span, to the half hour."""
    halves = max(1, round(seconds / 1800))
    return "30 minutes" if halves == 1 else "1 hour" if halves == 2 else f"{halves / 2:g} hours"


def _pressure(points: list[tuple[int, float]]) -> dict | None:
    """{hpa, change_hpa, tendency, level, text}: the pressure now, its change over the readings (signed: down is negative), whether it
    is falling (its highest came before its lowest), rising or steady (a change under STEADY_HPA), and its level."""
    if len(points) < 2:
        return None
    high = max(points, key=lambda p: p[1])   # (the first, where several are as high)
    low = min(points, key=lambda p: p[1])
    size = round(high[1] - low[1], 1)
    falling = high[0] < low[0]
    tendency = "steady" if size < STEADY_HPA else "falling" if falling else "rising"
    over = _over(points[-1][0] - points[0][0])
    change = (f"down {size:.1f} hPa in {over}" if tendency == "falling" else f"up {size:.1f} hPa in {over}" if tendency == "rising"
              else "steady")
    hpa = points[-1][1]
    level = "unsettled" if hpa < UNSETTLED_HPA else "settled" if hpa > SETTLED_HPA else "normal"
    steady = f"steady ({'no change' if not size else ('down ' if falling else 'up ') + f'{size:.1f} hPa'} in {over})"
    where = {"unsettled": f"; under {UNSETTLED_HPA:g} hPa: unsettled", "settled": f"; over {SETTLED_HPA:g} hPa: settled"}.get(level, "")
    text = f"{hpa:.1f} hPa, {steady if tendency == 'steady' else change}{where}"
    return {"hpa": round(hpa, 1), "change_hpa": (-size if falling else size) or 0.0, "tendency": tendency, "level": level, "text": text,
            "change": change}


def dew_point(temperature: float, humidity: float) -> float:
    """The dew point (°C) of air at this temperature (°C) and relative humidity (%), by the Magnus formula: for a station that sends
    no dew point of its own."""
    a, b = 17.62, 243.12
    gamma = math.log(max(humidity, 1.0) / 100) + a * temperature / (b + temperature)
    return b * gamma / (a - gamma)


def _moisture(temperature: float | None, humidity: float | None, dew: float | None) -> dict | None:
    """{humidity, dew_point_spread, moist, text}: the humidity now and how far the temperature is above the dew point; moist when the
    humidity is MOIST_HUMIDITY or more or the spread MOIST_SPREAD_C or less."""
    if dew is None and temperature is not None and humidity is not None:
        dew = dew_point(temperature, humidity)
    spread = round(temperature - dew, 1) if temperature is not None and dew is not None else None
    if humidity is None and spread is None:
        return None
    by_humidity = humidity is not None and humidity >= MOIST_HUMIDITY
    by_spread = spread is not None and spread <= MOIST_SPREAD_C
    gap = None if spread is None else "at the dew point" if spread <= 0 else f"{spread:.1f} °C above the dew point"
    text = ", ".join(x for x in (f"humidity {humidity:.0f}%" if humidity is not None else None, gap) if x)
    why = f"humidity {humidity:.0f}%" if by_humidity else "the temperature at the dew point" if by_spread and spread <= 0 else \
        f"temperature within {spread:.1f} °C of the dew point" if by_spread else None
    return {"humidity": None if humidity is None else round(humidity), "dew_point_spread": spread, "moist": by_humidity or by_spread,
            "text": text, "why": why}


def _recent_rain(rows: Rows, live: dict, live_ts: int | None, gauge: str, now_ts: int) -> dict | None:
    """{rained, raining_now, rate_mm_h, last_hour_mm, text}: is it raining now (a rain rate above 0), and the rain of the last hour
    (the daily total's rise, its midnight reset allowed for, and any rate above 0)."""
    total, rate = f"{gauge}.daily", f"{gauge}.rain_rate"
    seq = [*rows, *([(live_ts, live)] if live else [])]
    if not any(_number(r.get(total)) or _number(r.get(rate)) for _, r in seq):
        return None
    since = now_ts - RECENT_SECONDS
    fell, before = 0.0, None
    for ts, r in seq:
        if not _number(daily := r.get(total)):
            continue
        if ts >= since and before is not None:
            fell += daily - before if daily >= before else daily   # (a drop is the midnight reset)
        before = daily
    rate_now = next((float(r[rate]) for _, r in reversed(seq) if _number(r.get(rate))), None)
    raining = rate_now is not None and rate_now > 0
    rate_seen = any(_number(r.get(rate)) and r[rate] > 0 for ts, r in seq if ts >= since)
    rained = raining or fell > 0 or rate_seen
    mm = round(fell, 1)
    in_hour = (f"{mm:.1f} mm in the last hour" if mm else "a trace of rain in the last hour" if fell > 0
               else "rain in the last hour" if rate_seen else "no rain in the last hour")
    now = f"raining now ({rate_now:.1f} mm/h)" if raining else None
    text = now + (f", {mm:.1f} mm in the last hour" if mm else "") if raining else in_hour
    return {"rained": rained, "raining_now": raining, "rate_mm_h": None if rate_now is None else round(rate_now, 1),
            "last_hour_mm": mm, "text": text, "why": now or in_hour}


def _wind(points: list[tuple[int, float]], now_ts: int) -> dict | None:
    """{trend, first_hour_kmh, last_hour_kmh, text}: the mean wind speed of the last hour against that of the first of the 3 hours;
    picking up, or easing, when it differs by WIND_CHANGE_KMH or more. None without readings in both hours."""
    start = now_ts - HOURS * 3600
    first = [v for ts, v in points if start <= ts < start + 3600]
    last = [v for ts, v in points if ts >= now_ts - 3600]
    if not first or not last:
        return None
    a, b = sum(first) / len(first), sum(last) / len(last)
    trend = "picking up" if b - a >= WIND_CHANGE_KMH else "easing" if a - b >= WIND_CHANGE_KMH else "steady"
    return {"trend": trend, "first_hour_kmh": round(a, 1), "last_hour_kmh": round(b, 1),
            "text": f"{trend}: {b:.1f} km/h on average in the last hour, {a:.1f} km/h 2 to 3 hours ago"}


# ---------- the tool ----------
DESCRIPTION = (
    "Will it rain in the next few hours: the weather station's own read of its last 3 hours, by fixed rules, with the forecast's lines "
    "for today and tomorrow. For \"will it rain?\", \"do I need an umbrella?\", \"what's it doing later?\": {}. Takes no arguments.\n"
    "Not for: the days ahead (forecast); rain that fell (summary for a period's total, days for the wettest day); whether rain follows "
    "pressure over weeks (relate).\n"
    "The rules: pressure over the 3 hours is falling when its high came before its low; down more than 3 hPa: likely; down 1 to 3 hPa: "
    "possible; under 1 hPa or rising: unlikely. A step towards rain for each of: pressure under 1005 hPa (unsettled); humidity 90% or "
    "more, or the temperature within 2 °C of the dew point; wind picking up while pressure falls. A step away: pressure over 1020 hPa "
    "(settled). A rain rate above 0 now, or rain in the last hour, makes it likely: showers tend to go on.\n"
    "Result: `verdict` (likely, possible, unlikely) and `reason` (the one or two readings behind it, ready to quote); `pressure`, "
    "`moisture`, `recent_rain` and `wind_trend`, each with its figures and text; `forecast`, Open-Meteo's lines for today and tomorrow, "
    "to copy as they are. Open with the verdict and reason, say it is a read of the station's readings, then the forecast's lines; if "
    "they disagree, say so.")
PARAMETERS = {"type": "object", "properties": {}}
HOW_TO_READ = ("`verdict` and `reason` are the station's read of the next few hours from its last 3 hours, by the rules in this tool's "
               "description: open with them, and say it is a read of the station's readings. `forecast` holds Open-Meteo's lines for "
               "today and tomorrow: give them after it, copied as they are, emoji included; if they disagree with the station's read, "
               "say so plainly. For the days further ahead, call forecast.")


def tools(ctx) -> list[ServiceTool]:
    """The `outlook` tool, when there is a weather station."""
    if ctx.eco is None:
        return []

    async def handle(args: dict, meta: dict) -> str:
        eco = ctx.eco
        tz = ctx.zone
        now_ts = int(ctx.clock())
        rows, live, live_ts = await readings(eco, now_ts, tz, fetch=meta["fetch"], urgent=meta["priority"] != BACKGROUND)
        got = read(rows, live, now_ts, live_ts)
        if got["newest"] is None:
            raise Refusal("The weather station has no readings from the last 3 hours, so there is nothing to read the next few hours "
                          "from." + (" Ask forecast for today's forecast." if ctx.forecast is not None else ""))
        newest = to_local(got.pop("newest"), tz)
        extra = {}
        if ctx.forecast is not None:
            today = to_local(now_ts, tz).date()
            days = [d for d in await held_days(ctx.forecast, today, meta["fetch"]) if (d["date"] - today).days <= 1]
            extra["forecast"] = day_lines(days, today) if days else []
            if not days:
                extra["forecast_note"] = "No forecast is available right now: the station's read is the whole answer."
        if now_ts - newest.timestamp() > STALE_SECONDS:
            extra["warning"] = (f"The station's newest reading is from {day_text(newest, year=False)} {clock_text(newest)}: this read may "
                                f"be out of date.")
        return answer(period=None, as_of=newest, **got, **extra, how_to_read=HOW_TO_READ)

    return [ServiceTool("outlook", DESCRIPTION, PARAMETERS, handle, open_world=True)]


async def readings(eco, now_ts: int, tz, *, fetch: bool, urgent: bool) -> tuple[list, dict | None, int | None]:
    """(the last HOURS of 5-minute readings, the reading now as {"group.field": value}, its time) from the station: the readings as
    Ecowitt.recent reads them (what the file lacks asked of Ecowitt, ahead of the background requests when someone is waiting), the
    reading now as the report's and the gauges' (the kept live reading while fresh, else a request for every group, kept in turn). With
    `fetch` False, only what is held: the file's readings and the kept live reading."""
    groups = [g for g in GROUPS if g in eco.groups]
    start = now_ts - HOURS * 3600
    if fetch:
        rows = await eco.fetcher(groups, urgent=urgent).get("5min", naive_local(start, tz), naive_local(now_ts, tz))
        try:
            data = await eco._live_data(LIVE_GROUPS, urgent=urgent)   # (every group, as the report's: kept for the next to ask)
        except Exception as e:   # the readings of the last 3 hours still give a read
            log.warning("Outlook: no live reading (%s: %s); reading the 5-minute history alone", type(e).__name__, e)
            data = None
    else:
        rows = await eco.storage.read(rows_on, eco.mac, groups, "5min", start, now_ts)
        data = eco._live[1] if getattr(eco, "_live", None) else None
    live, live_ts = _values(data or {})
    return rows, live, live_ts


def _values(data: dict) -> tuple[dict, int | None]:
    """A real_time response ({group: {field: {value, unit, time}}}) as {"group.field": value} and the time of its newest reading."""
    out, newest = {}, 0
    for group, fields in data.items():
        for name, obj in (fields.items() if isinstance(fields, dict) else ()):
            try:
                out[f"{group}.{name}"] = float(obj["value"])
                newest = max(newest, int(obj.get("time") or 0))
            except (KeyError, TypeError, ValueError):
                continue
    return out, newest or None


__all__ = ["DESCRIPTION", "PARAMETERS", "dew_point", "read", "readings", "tools"]
