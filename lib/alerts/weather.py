"""Weather alerts from the Ecowitt station, checked after every keep-warm refresh (from the same
5-minute readings, so no extra requests).

  - Rain: "stopped" after 30 dry minutes, with how much fell; any rain after that is a new
    "started". One rule both ways, so the alerts never contradict each other (no flapping).
  - Rain likely soon: pressure falling over 3 hours plus arriving moisture, scored, tuned for
    Melbourne (see assess_rain). At most once every 6 hours.
  - Temperatures crossing: outdoor becomes warmer than indoor (or cooler) after the other way
    round held for 2+ days. A 0.3 degree margin stops sensor noise flip-flopping.
"""

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

log = logging.getLogger(__name__)

RAIN_STOP_DRY_SECONDS = 30 * 60
PREDICT_MIN_SCORE = 4
PREDICT_EVERY_SECONDS = 6 * 3600
# Air pressure has a twice-daily "tide" (highs ~10am/10pm, lows ~4am/4pm local solar time).
# Around 37 degrees south its swing is ~0.7 hPa either side; it's subtracted before judging a fall.
TIDE_AMPLITUDE_HPA = 0.7
NIGHT_HOURS = (20, 8)   # 8pm-8am: cooling alone brings the air close to its dew point
CROSS_MIN_SECONDS = 2 * 86400
CROSS_MARGIN = 0.3

Rows = list[tuple[int, dict]]  # [(epoch, {"group.field": value})], oldest first


def tide(ts: int, longitude: float) -> float:
    """Expected pressure offset from the atmospheric tide at this moment (hPa)."""
    utc = datetime.fromtimestamp(ts, timezone.utc)
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
    hour = datetime.fromtimestamp(latest_ts, timezone.utc).astimezone(tz).hour
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
    if now_gusts and earlier and max(now_gusts) - sum(earlier) / len(earlier) >= 10:
        score += 1
        reasons.append(f"gusts picking up ({max(now_gusts):.0f} km/h)")
    return Outlook(score, reasons, raw_drop, drop, night)


def side(r: dict) -> str | None:
    """Is outdoor clearly warmer or cooler than indoor in this reading?"""
    o, i = r.get("outdoor.temperature"), r.get("indoor.temperature")
    if o is None or i is None:
        return None
    return "warmer" if o - i > CROSS_MARGIN else "cooler" if o - i < -CROSS_MARGIN else None


class WeatherMonitor:
    def __init__(self, station, state, notify):
        self.station, self.state, self.notify = station, state, notify

    @property
    def tz(self):
        return self.station.tz

    async def init(self):
        """Work out the current state from recent data without alerting (first run only)."""
        m = self.state.monitor
        if "rain" not in m:
            wet = wet_flags(await self.station.recent(3))
            raining = any(w for _, w, _ in wet[-2:])
            m["rain"] = {"raining": raining, "since": next((ts for ts, w, _ in wet if w), None) if raining else None}
        if "cross" not in m:
            now = self.station.now()
            rows = await self.station.readings("30min", now - timedelta(days=6, hours=23), now, ["outdoor", "indoor"])
            current, since = None, None
            for ts, r in rows:
                s = side(r)
                if s and s != current:
                    current, since = s, ts
            if current:
                m["cross"] = {"side": current, "since": since}
        self.state.save()

    async def check(self):
        """Look at the latest readings and send any alerts. Called after each refresh."""
        rows = await self.station.recent(3)
        if not rows:
            return
        await self._rain(rows)
        await self._rain_likely(rows)
        await self._cross(rows)
        self.state.save()

    async def _rain(self, rows: Rows):
        m = self.state.monitor.setdefault("rain", {"raining": False, "since": None})
        wet = wet_flags(rows)
        latest_ts = wet[-1][0]
        wet_times = [ts for ts, w, _ in wet if w]
        last_wet = wet_times[-1] if wet_times else None
        if not m["raining"] and any(w for _, w, _ in wet[-2:]):
            rate = max(r for _, w, r in wet[-2:] if w)
            m.update(raining=True, since=next(ts for ts, w, _ in wet[-2:] if w))
            await self.notify("\U0001f327️ It's started raining" + (f" ({rate:g} mm/h)." if rate > 0 else "."))
        elif m["raining"] and (last_wet is None or latest_ts - last_wet >= RAIN_STOP_DRY_SECONDS):
            if last_wet is None:  # nothing in the last 3 hours (e.g. the bot was down): close it quietly
                log.info("Alerts: rain ended while not watching; no alert")
            else:
                since = m.get("since") or wet_times[0]
                fell = rain_amount(rows, since, last_wet)
                amount = f"{fell:.1f} mm fell" if fell else "Only a trace fell"
                took = duration(last_wet + 300 - since)
                await self.notify(f"\U0001f324️ The rain has stopped. {amount} over {took}.")
            m.update(raining=False, since=None)

    async def _rain_likely(self, rows: Rows):
        """Not while raining or within an hour of rain; at most every 6 hours."""
        m = self.state.monitor.setdefault("predict", {"last": 0})
        latest_ts = rows[-1][0]
        if self.state.monitor.get("rain", {}).get("raining") or any(
                w for ts, w, _ in wet_flags(rows) if latest_ts - ts < 3600):
            return
        if latest_ts - m.get("last", 0) < PREDICT_EVERY_SECONDS:
            return
        outlook = assess_rain(rows, self.tz, self.station.longitude)
        if outlook is None:
            return
        log.info("Rain-likely check: pressure fall %.1f hPa (%.1f beyond the tide), %s, score %d",
                 outlook.raw_drop, outlook.drop, "night" if outlook.night else "day", outlook.score)
        if outlook.score >= PREDICT_MIN_SCORE:
            m["last"] = latest_ts
            await self.notify("\U0001f326️ Rain looks likely soon: " + ", ".join(outlook.reasons[:3]) +
                              ". (An estimate from the station's readings, not an official forecast.)")

    async def _cross(self, rows: Rows):
        ts, r = rows[-1]
        current = side(r)
        c = self.state.monitor.get("cross")
        if current is None:
            return
        if c is None:
            self.state.monitor["cross"] = {"side": current, "since": ts}
            return
        if current != c["side"]:
            held = ts - c["since"]
            if held >= CROSS_MIN_SECONDS:
                o, i = r["outdoor.temperature"], r["indoor.temperature"]
                await self.notify(f"\U0001f321️ It's now {current} outside ({o:.1f}°C) than inside "
                                  f"({i:.1f}°C), for the first time in {int(held // 86400)} days.")
            c.update(side=current, since=ts)
