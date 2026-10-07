"""Is it raining, or likely to soon? Read from the station's recent 5-minute readings, by one set of rules for the alerts
and the status report."""

import math
from datetime import datetime, UTC
from statistics import fmean
from typing import NamedTuple

PREDICT_MIN_SCORE = 4
# Air pressure has a twice-daily "tide" (highs ~10am/10pm, lows ~4am/4pm local solar time).
# Around 37 degrees south its swing is ~0.7 hPa either side; it's subtracted before judging a fall.
TIDE_AMPLITUDE_HPA = 0.7
NIGHT_HOURS = (20, 8)   # 8pm-8am: cooling alone brings the air close to its dew point

type Rows = list[tuple[int, dict]]  # [(epoch, {"group.field": value})], oldest first


def tide(ts: int, longitude: float) -> float:
    """Expected pressure offset from the atmospheric tide at this moment (hPa)."""
    utc = datetime.fromtimestamp(ts, UTC)
    solar_hour = (utc.hour + utc.minute / 60 + longitude / 15) % 24
    return TIDE_AMPLITUDE_HPA * math.cos(2 * math.pi * (solar_hour - 10) / 12)


def wet_flags(rows: Rows) -> list[tuple[int, bool, float]]:
    """[(ts, rained in this reading, rain rate)]: rate above zero, or the daily total rising."""
    out, prev_daily = [], None
    for ts, r in rows:
        rate = r.get("rainfall.rain_rate", 0.0)
        daily = r.get("rainfall.daily")
        rising = daily is not None and prev_daily is not None and daily > prev_daily  # ignores the midnight reset
        if daily is not None:
            prev_daily = daily
        out.append((ts, rate > 0 or rising, rate))
    return out


def rain_amount(rows: Rows, since: int, last_wet: int) -> float | None:
    """Rain that fell between since and last_wet: the rise in the daily total (allowing for its
    midnight reset). Ecowitt's 'event' counter isn't used, as it spans several showers."""
    total, prev = 0.0, None
    for ts, r in rows:
        daily = r.get("rainfall.daily")
        if daily is None or ts > last_wet:
            continue
        if ts >= since and prev is not None:
            total += daily - prev if daily >= prev else daily  # a drop means the midnight reset
        prev = daily
    return round(total, 1) if prev is not None else None


def duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"


class Outlook(NamedTuple):
    score: int
    reasons: list[str]
    raw_drop: float   # hPa fallen in 3 hours
    drop: float       # the same with the tide's expected change removed
    night: bool


def assess_rain(rows: Rows, tz, longitude: float) -> Outlook | None:
    """Score how likely rain is soon; None when there is no real pressure fall to go on.

    Two normal night-time effects are ignored, as they aren't signs of rain: the pressure tide's
    overnight dip (subtracted from the fall), and the air cooling to near its dew point (at night
    only a *rising* dew point counts, i.e. moister air moving in)."""
    latest_ts, latest = rows[-1]
    within = lambda secs: next((r for ts, r in rows if latest_ts - ts <= secs), None)
    old_ts, old = next(((ts, r) for ts, r in rows if latest_ts - ts <= 3 * 3600
                        and r.get("pressure.relative") is not None), (None, None))
    p_now = latest.get("pressure.relative")
    if p_now is None or old is None:
        return None
    raw_drop = old["pressure.relative"] - p_now
    drop = raw_drop + (tide(latest_ts, longitude) - tide(old_ts, longitude))
    if drop < 1.0:  # a real (beyond-tide) pressure fall is required
        return None
    score = 3 if drop >= 3 else 2 if drop >= 2 else 1
    reasons = [f"pressure down {raw_drop:.1f} hPa in 3 hours"]
    hour = datetime.fromtimestamp(latest_ts, UTC).astimezone(tz).hour
    night = hour >= NIGHT_HOURS[0] or hour < NIGHT_HOURS[1]
    temp, dew = latest.get("outdoor.temperature"), latest.get("outdoor.dew_point")
    if night:
        dew_old = (within(2 * 3600) or {}).get("outdoor.dew_point")
        if dew is not None and dew_old is not None and dew - dew_old >= 1:
            score += 2 if dew - dew_old >= 2 else 1
            reasons.append(f"dew point up {dew - dew_old:.1f}°C in 2 hours")
    else:
        hum = latest.get("outdoor.humidity")
        if hum is not None:
            hum_old = (within(3600) or {}).get("outdoor.humidity")
            rising = hum_old is not None and hum - hum_old >= 5
            if hum >= 90 or rising:
                score += (2 if hum >= 95 else 1 if hum >= 90 else 0) + (1 if rising else 0)
                reasons.append(f"humidity {hum:.0f}%" + (" and rising" if rising else ""))
        if temp is not None and dew is not None and temp - dew <= 2:
            score += 2 if temp - dew <= 1 else 1
            reasons.append(f"only {temp - dew:.1f}°C above the dew point")
    gusts = [(ts, r["wind.wind_gust"]) for ts, r in rows if r.get("wind.wind_gust") is not None]
    now_gusts = [g for ts, g in gusts if latest_ts - ts <= 1800]
    earlier = [g for ts, g in gusts if latest_ts - ts > 1800]
    if now_gusts and earlier and max(now_gusts) - fmean(earlier) >= 10:
        score += 1
        reasons.append(f"gusts picking up ({max(now_gusts):.0f} km/h)")
    return Outlook(score, reasons, raw_drop, drop, night)


def rain_outlook(rows: Rows, tz, longitude: float) -> str | None:
    """"Raining now" or "rain likely soon", in words for a status report, by the same rules as the alerts; None when neither."""
    wet = wet_flags(rows)
    if any(w for _, w, _ in wet[-2:]):
        rate = max(r for _, w, r in wet[-2:] if w)
        return "raining now" + (f" ({rate:g} mm/h)" if rate > 0 else "")
    outlook = assess_rain(rows, tz, longitude)
    if outlook and outlook.score >= PREDICT_MIN_SCORE:
        return ("rain looks likely soon: " + ", ".join(outlook.reasons[:3]) +
                " (an estimate from the station's readings, not an official forecast)")
    return None
