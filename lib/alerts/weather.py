"""Weather alerts from the Ecowitt station, checked after every keep-warm refresh (from the same
5-minute readings; the rain check also reads the gauge's live value, one small request).

  - Rain: "stopped" after 60 dry minutes (RAIN_STOP_MINUTES), with how much fell; any rain after that is a new
    "started" (one tip of the gauge is enough). One rule both ways, so the alerts never contradict each other (no flapping).
    Not during quiet hours (RAIN_QUIET_HOURS, default midnight to 6am): those are kept quiet and one summary of the overnight
    rain is sent after they end.
  - Rain likely soon: pressure falling over 3 hours plus arriving moisture, scored, tuned for
    Melbourne (see assess_rain). At most once every 6 hours.
  - Strong gusts: one alert when a gust goes over 40 km/h, and no more until the gusts have stayed at or under
    it for an hour, so a blustery afternoon is one message, not twenty.
  - Strong sun: one alert when the UV index reaches 10, and no more until it has stayed below 10 for an hour.
  - Temperatures crossing: outdoor becomes warmer than indoor (or cooler) after the other way
    round held for 2+ days. A 0.3 degree margin stops sensor noise flip-flopping.
"""

import logging
from datetime import timedelta

from ..ecowitt.glance import UVI_ALERT
from ..ecowitt.outlook import PREDICT_MIN_SCORE, Rows, assess_rain, duration, rain_amount, wet_flags
from ..timeutil import to_local

log = logging.getLogger(__name__)

RAIN_STOP_DRY_SECONDS = 60 * 60   # the default; RAIN_STOP_MINUTES sets it (the readings looked at go back 3 hours, so 150 minutes is the most)
PREDICT_EVERY_SECONDS = 6 * 3600
CROSS_MIN_SECONDS = 2 * 86400
CROSS_MARGIN = 0.3
GUST_ALERT_KMH = 40
GUST_REARM_SECONDS = 3600
UV_REARM_SECONDS = 3600


def side(r: dict) -> str | None:
    """Is outdoor clearly warmer or cooler than indoor in this reading?"""
    o, i = r.get("outdoor.temperature"), r.get("indoor.temperature")
    if o is None or i is None:
        return None
    return "warmer" if o - i > CROSS_MARGIN else "cooler" if o - i < -CROSS_MARGIN else None


class WeatherMonitor:
    def __init__(self, station, state, notify, rain_stop_seconds: int = RAIN_STOP_DRY_SECONDS, quiet: tuple[int, int] | None = None):
        self.station, self.state, self.notify = station, state, notify
        self.rain_stop_seconds = rain_stop_seconds
        self.quiet = quiet   # (from hour, to hour) local, no rain alerts in between; None: always on

    def _quiet(self, ts: int) -> bool:
        if not self.quiet:
            return False
        start, end = self.quiet
        hour = to_local(ts, self.tz).hour
        return start <= hour < end if start < end else hour >= start or hour < end

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
        live = await self.station.live_rain()  # newer than the 5-minute history by up to 5 minutes
        await self._rain(rows + [live] if live and live[0] > rows[-1][0] else rows)
        await self._night_summary(rows)
        await self._rain_likely(rows)
        await self._gusts(rows)
        await self._uv(rows)
        await self._cross(rows)
        self.state.save()

    async def _rain(self, rows: Rows):
        m = self.state.monitor.setdefault("rain", {"raining": False, "since": None})
        wet = wet_flags(rows)
        latest_ts = wet[-1][0]
        quiet = self._quiet(latest_ts)
        if quiet:
            self._note_night(rows, wet)
        wet_times = [ts for ts, w, _ in wet if w]
        last_wet = wet_times[-1] if wet_times else None
        if not m["raining"] and any(w for _, w, _ in wet[-2:]):
            rate = max(r for _, w, r in wet[-2:] if w)
            m.update(raining=True, since=next(ts for ts, w, _ in wet[-2:] if w))
            if not any(w for ts, w, _ in wet[:-1][-2:]):
                log.info("Alerts: rain started, seen in the newest reading only (the live one when it is newer than the history)")
            if not quiet:
                await self.notify("\U0001f327️ It's started raining" + (f" ({rate:g} mm/h)." if rate > 0 else "."), kind="rain")
        elif m["raining"] and (last_wet is None or latest_ts - last_wet >= self.rain_stop_seconds):
            if last_wet is None:  # nothing in the last 3 hours (e.g. the bot was down): close it quietly
                log.info("Alerts: rain ended while not watching; no alert")
            elif quiet:
                log.info("Alerts: rain stopped in quiet hours; the morning summary covers it")
            else:
                since = m.get("since") or wet_times[0]
                fell = rain_amount(rows, since, last_wet)
                amount = f"{fell:.1f} mm fell" if fell else "Only a trace fell"
                took = duration(last_wet + 300 - since)
                await self.notify(f"\U0001f324️ The rain has stopped. {amount} over {took}.", kind="rain")
            m.update(raining=False, since=None)

    def _note_night(self, rows: Rows, wet: list):
        """In quiet hours: when it first and last rained since midnight, and the day's total so far (the gauge's running total)."""
        latest = rows[-1][0]
        today = to_local(latest, self.tz).date()
        night = self.state.monitor.get("night")
        if not night or night["date"] != today.isoformat():
            night = self.state.monitor["night"] = {"date": today.isoformat(), "first": None, "last": None, "mm": 0.0}
        for ts, w, _ in wet:
            if w and to_local(ts, self.tz).date() == today and self._quiet(ts):
                night["first"] = night["first"] or ts
                night["last"] = ts
        daily = rows[-1][1].get("rainfall.daily")
        if daily is not None:
            night["mm"] = max(night["mm"], daily)

    async def _night_summary(self, rows: Rows):
        """After quiet hours end: one message about the rain that fell in them (only if any did)."""
        night = self.state.monitor.get("night")
        latest = rows[-1][0]
        if not night or self._quiet(latest):
            return
        self.state.monitor.pop("night")
        if night["date"] != to_local(latest, self.tz).date().isoformat() or not night["first"]:
            return   # an old note (the bot was down) or a dry night
        clock = lambda ts: f"{to_local(ts, self.tz):%-I:%M%p}".replace("AM", "am").replace("PM", "pm")
        amount = f"{night['mm']:.1f} mm" if night["mm"] >= 0.1 else "a trace"
        still = " It's still raining." if self.state.monitor.get("rain", {}).get("raining") else ""
        await self.notify(f"\U0001f327️ Overnight rain: {amount}, from about {clock(night['first'])} to {clock(night['last'] + 300)}.{still}", kind="rain")

    async def _rain_likely(self, rows: Rows):
        """Not while raining or within an hour of rain; at most every 6 hours."""
        m = self.state.monitor.setdefault("predict", {"last": 0})
        latest_ts = rows[-1][0]
        if self._quiet(latest_ts):
            return
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
                              ". (An estimate from the station's readings, not an official forecast.)", kind="rain_likely")

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
                                  + f" (alerts above {GUST_ALERT_KMH} km/h).", kind="gusts")
        elif m["active"]:
            m["calm_since"] = m["calm_since"] or latest_ts
            if latest_ts - m["calm_since"] >= GUST_REARM_SECONDS:
                m.update(active=False, calm_since=None)

    async def _uv(self, rows: Rows):
        """Like the gusts: only readings since the last check count, one alert per spell at or above UVI_ALERT."""
        m = self.state.monitor.setdefault("uv", {"active": False, "calm_since": None})
        latest_ts = rows[-1][0]
        seen, m["checked"] = m.get("checked", latest_ts), latest_ts
        over = [(u, ts) for ts, r in rows if ts > seen and (u := r.get("solar_and_uvi.uvi")) is not None and u >= UVI_ALERT]
        if over:
            m["calm_since"] = None
            if not m["active"]:
                m["active"] = True
                uv, ts = max(over)
                await self.notify(f"\U0001f9f4 UV index {uv:g} at {to_local(ts, self.tz):%-I:%M%p}".replace("AM", "am").replace("PM", "pm")
                                  + f": very high. Sunscreen, a hat and shade (alerts at {UVI_ALERT} and above).", kind="uv")
        elif m["active"]:
            m["calm_since"] = m["calm_since"] or latest_ts
            if latest_ts - m["calm_since"] >= UV_REARM_SECONDS:
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
                                  f"({i:.1f}°C), for the first time in {int(held // 86400)} days.", kind="temps")
            c.update(side=current, since=ts)
