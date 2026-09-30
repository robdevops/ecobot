"""Weather alerts from the Ecowitt station, checked after every keep-warm refresh (from the same
5-minute readings, so no extra requests).

  - Rain: "stopped" after 30 dry minutes, with how much fell; any rain after that is a new
    "started". One rule both ways, so the alerts never contradict each other (no flapping).
  - Rain likely soon: pressure falling over 3 hours plus arriving moisture, scored, tuned for
    Melbourne (see assess_rain). At most once every 6 hours.
  - Strong gusts: one alert when a gust goes over 40 km/h, and no more until the gusts have stayed at or under
    it for an hour, so a blustery afternoon is one message, not twenty.
  - Temperatures crossing: outdoor becomes warmer than indoor (or cooler) after the other way
    round held for 2+ days. A 0.3 degree margin stops sensor noise flip-flopping.
"""

import logging
from datetime import timedelta

from ..ecowitt.outlook import PREDICT_MIN_SCORE, Rows, assess_rain, duration, rain_amount, wet_flags
from ..timeutil import to_local

log = logging.getLogger(__name__)

RAIN_STOP_DRY_SECONDS = 30 * 60
PREDICT_EVERY_SECONDS = 6 * 3600
CROSS_MIN_SECONDS = 2 * 86400
CROSS_MARGIN = 0.3
GUST_ALERT_KMH = 40
GUST_REARM_SECONDS = 3600


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
        await self._gusts(rows)
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

    async def _gusts(self, rows: Rows):
        """Only readings since the last check count, so a restart doesn't re-announce an old gust."""
        m = self.state.monitor.setdefault("gust", {"active": False, "calm_since": None})
        latest_ts = rows[-1][0]
        seen, m["checked"] = m.get("checked", latest_ts), latest_ts
        over = [(g, ts) for ts, r in rows if ts > seen and (g := r.get("wind.wind_gust")) is not None and g > GUST_ALERT_KMH]
        if over:
            m["calm_since"] = None
            if not m["active"]:
                m["active"] = True
                gust, ts = max(over)
                await self.notify(f"\U0001f4a8 Strong gusts: {gust:.0f} km/h at {to_local(ts, self.tz):%-I:%M%p}".replace("AM", "am").replace("PM", "pm")
                                  + f" (alerts above {GUST_ALERT_KMH} km/h).")
        elif m["active"]:
            m["calm_since"] = m["calm_since"] or latest_ts
            if latest_ts - m["calm_since"] >= GUST_REARM_SECONDS:
                m.update(active=False, calm_since=None)

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
