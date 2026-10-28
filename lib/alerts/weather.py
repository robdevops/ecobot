"""Weather alerts from the Ecowitt station, checked after every keep-warm refresh (from the same
5-minute readings; the rain check also reads the gauge's live value, one small request).

  - Rain: "started" (one tip of the gauge is enough) and "stopped" (dry for ALERT_COOLDOWN_MINUTES), with how much fell over
    the whole spell. Not during quiet hours (RAIN_QUIET_HOURS, default midnight to 6am, and they may run across midnight):
    those are kept quiet and one summary of the rain that fell in them is sent after they end.
  - Strong gusts: one alert when a gust goes over 40 km/h, and no more until the gusts have stayed at or under
    it for an hour, so a blustery afternoon is one message, not twenty.
  - Strong sun: one alert when the UV index is 10 or more in two readings in a row (confirm.py), and no more until it has stayed
    below 10 for an hour.
  - Temperatures crossing: outdoor becomes warmer than indoor (or cooler); it has to differ by more than 1 degree, which stops
    sensor noise flip-flopping.
  Rain and crossing share one back-off rule (backoff.py): after an alert, a change is held for ALERT_COOLDOWN_MINUTES and then
  announced if the state still differs from the one last alerted; if it is back where it was, nothing is sent.
"""

import asyncio
import logging
from datetime import timedelta

from ..ecowitt.glance import UVI_ALERT
from . import confirm, pictures
from .menu import kinds_of
from .backoff import due, remember
from ..ecowitt.rain import Rows, duration, rain_amount, wet_flags
from ..timeutil import at_hour, clock_text, to_local

log = logging.getLogger(__name__)

ALERT_COOLDOWN_SECONDS = 60 * 60   # the default; ALERT_COOLDOWN_MINUTES sets it (the readings looked at go back 3 hours, so 150 minutes is the most)
CROSS_MARGIN = 1.0
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
    KINDS = kinds_of("Ecowitt")

    def __init__(self, station, state, notify, cooldown_seconds: int = ALERT_COOLDOWN_SECONDS, quiet: tuple[int, int] | None = None):
        self.station, self.state, self.notify = station, state, notify
        self.cooldown = cooldown_seconds
        self.quiet = quiet   # (from hour, to hour) local, no rain alerts in between; None: always on
        self._rain_lock = asyncio.Lock()   # the minute's check and the live reading's look at the rain are one at a time

    def _quiet(self, ts: int) -> bool:
        if not self.quiet:
            return False
        start, end = self.quiet
        hour = to_local(ts, self.tz).hour
        return start <= hour < end if start < end else hour >= start or hour < end

    def _quiet_began(self, ts: int) -> int:
        """When the quiet hours that `ts` falls in began (epoch): the evening before, for hours that run across midnight."""
        local = to_local(ts, self.tz)
        day = local.date() - timedelta(days=1 if local.hour < self.quiet[0] else 0)
        return at_hour(day, self.quiet[0], self.tz)

    @property
    def tz(self):
        return self.station.tz

    async def init(self):
        """Work out the current state from recent data without alerting (first run only)."""
        m = self.state.monitor
        if "rain" not in m:
            wet = wet_flags(await self.station.recent(3))
            raining = any(w for _, w, _ in wet[-2:])
            m["rain"] = {"alerted_state": "raining" if raining else "dry", "alerted_time": None,
                         "since": next((ts for ts, w, _ in wet if w), None) if raining else None}
        if "cross" not in m:
            now = self.station.now()
            rows = await self.station.readings("30min", now - timedelta(days=6, hours=23), now, ["outdoor", "indoor"])
            current, since = None, None
            for ts, r in rows:
                s = side(r)
                if s and s != current:
                    current, since = s, ts
            if current:
                m["cross"] = {"alerted_state": current, "alerted_time": since}   # the side it is on, from when it has been
        self.state.save()

    async def check(self):
        """Look at the latest readings and send any alerts. Called after each refresh."""
        rows = await self.station.recent(3)
        if not rows:
            return
        live = await self.station.live_rain()  # newer than the 5-minute history by up to 5 minutes
        await self._rain(rows + [live] if live and live[0] > rows[-1][0] else rows)
        await self._night_summary(rows)
        await self._gusts(rows)
        await self._uv(rows)
        await self._cross(rows)
        self.state.save()

    async def check_rain(self):
        """Look at the rain alone, on a live reading that has just come in (the station is polled every few seconds, so this is the alert
        within a few seconds of the gauge, not at the next minute's check)."""
        rows = await self.station.recent(3)
        live = await self.station.live_rain() if rows else None
        if rows and live and live[0] > rows[-1][0]:
            await self._rain(rows + [live])
            self.state.save()

    async def _rain(self, rows: Rows):
        async with self._rain_lock:
            await self._rain_locked(rows)

    async def _rain_locked(self, rows: Rows):
        m = self.state.monitor.setdefault("rain", {})
        m.setdefault("alerted_state", "dry")
        wet = wet_flags(rows)
        latest_ts = wet[-1][0]
        quiet = self._quiet(latest_ts)
        if quiet:
            self._note_night(wet)
        wet_times = [ts for ts, w, _ in wet if w]
        last_wet = wet_times[-1] if wet_times else None
        raining = last_wet is not None and latest_ts - last_wet < self.cooldown   # dry means no rain for the whole cooldown
        state = "raining" if raining else "dry"
        if not due(m, state, latest_ts, self.cooldown) and not (quiet and state != m["alerted_state"]):
            return
        if raining:
            m["since"] = next(ts for ts in wet_times if latest_ts - ts < self.cooldown)
            rate = next(r for ts, w, r in reversed(wet) if w)
            if not quiet:
                await self.notify("\U0001f327️ It's started raining" + (f" ({rate:g} mm/h)." if rate > 0 else "."), kind="rain")
        elif m["alerted_state"] == "raining":
            if last_wet is None:  # nothing in the last 3 hours (e.g. the bot was down): close it quietly
                log.info("Alerts: rain ended while not watching; no alert")
                quiet = True
            elif quiet:
                log.info("Alerts: rain stopped in quiet hours; the morning summary covers it")
            else:
                since = m.get("since") or wet_times[0]
                fell = await self._fell(rows, since, last_wet)
                amount = f"{fell:.1f} mm fell" if fell else "Only a trace fell"
                took = duration(last_wet + 300 - since)
                await self.notify(f"\U0001f324️ The rain has stopped. {amount} over {took}.", kind="rain")
            m["since"] = None
        remember(m, state, latest_ts, alerted=not quiet)

    def _note_night(self, wet: list):
        """In quiet hours: when it first and last rained since they began. The note lives until the summary after them takes it."""
        began = self._quiet_began(wet[-1][0])
        night = self.state.monitor.get("night")
        if not night or night.get("began") != began:   # a new night (a note left from an earlier one is replaced)
            night = self.state.monitor["night"] = {"began": began, "first": None, "last": None}
        for ts, w, _ in wet:
            if w and ts >= began:
                night["first"] = night["first"] or ts
                night["last"] = ts

    async def _night_summary(self, rows: Rows):
        """After quiet hours end: one message about the rain that fell in them (only if any did)."""
        night = self.state.monitor.get("night")
        latest = rows[-1][0]
        if not night or self._quiet(latest):
            return
        self.state.monitor.pop("night")
        if not night.get("first") or latest - night.get("began", 0) >= 86400:
            return   # a dry night, or an old note (the bot was down)
        clock = lambda ts: clock_text(to_local(ts, self.tz))
        fell = await self._fell(rows, night["first"], night["last"])
        amount = f"{fell:.1f} mm" if fell else "a trace"
        still = " It's still raining." if self.state.monitor.get("rain", {}).get("alerted_state") == "raining" else ""
        await self.notify(f"\U0001f327️ Overnight rain: {amount}, from about {clock(night['first'])} to {clock(night['last'] + 300)}.{still}", kind="rain")

    async def _gusts(self, rows: Rows):
        """Only readings since the last check count, so a restart doesn't re-announce an old gust."""
        m = self.state.monitor.setdefault("gust", {"active": False, "calm_since": None})
        latest_ts = rows[-1][0]
        seen, m["checked"] = m.get("checked", latest_ts), latest_ts
        over = [(g, ts) for ts, r in rows if ts > seen and (g := r.get("wind.wind_gust")) is not None and g > GUST_ALERT_KMH]
        confirm.rearm(m, bool(over), latest_ts, GUST_REARM_SECONDS)
        if over and not m["active"]:
            m["active"] = True
            gust, ts = max(over)
            await self.notify(f"\U0001f4a8 Strong gusts: {gust:.0f} km/h at {clock_text(to_local(ts, self.tz))}"
                              f" (alerts above {GUST_ALERT_KMH} km/h).", kind="gusts", chart=lambda: self._picture("Wind chart 1d"))

    async def _uv(self, rows: Rows):
        """One alert per spell at or above UVI_ALERT, once it holds for two readings in a row (confirm.py); only readings newer than the last
        one counted are looked at, so a restart doesn't re-announce an old spell."""
        m = self.state.monitor.setdefault("uv", {"active": False, "calm_since": None})
        latest_ts = rows[-1][0]
        m.setdefault("stamp", latest_ts)   # the first look only sets the mark
        high = False
        for ts, r in rows:
            if not confirm.new_reading(m, ts):
                continue
            u = r.get("solar_and_uvi.uvi")
            hit = None if u is None else u >= UVI_ALERT
            streak = confirm.run(m, "streak", hit)
            if hit and (streak == 1 or u > m["peak"][0]):    # the run's highest reading is the one told
                m["peak"] = [u, ts]
            high = high or bool(hit)
        confirm.rearm(m, high, latest_ts, UV_REARM_SECONDS)
        if m.get("streak", 0) >= confirm.CONFIRM_READINGS and not m["active"]:
            m["active"] = True
            uv, ts = m["peak"]
            await self.notify(f"\U0001f9f4 UV index {uv:g} at {clock_text(to_local(ts, self.tz))}"
                              f": very high. Sunscreen, a hat and shade (alerts at {UVI_ALERT} and above).", kind="uv",
                              chart=lambda: self._picture("UV chart 1d"))

    async def _cross(self, rows: Rows):
        ts, r = rows[-1]
        current = side(r)
        c = self.state.monitor.get("cross")
        if current is None:
            return
        if c is None:
            self.state.monitor["cross"] = {"alerted_state": current, "alerted_time": ts}
            return
        if due(c, current, ts, self.cooldown):
            o, i = r["outdoor.temperature"], r["indoor.temperature"]
            held = ts - c["alerted_time"]   # how long the side we last told about has stood
            was = f"{int(held // 86400)} days" if held >= 2 * 86400 else duration(held)
            icon = "\U0001f321️" if current == "warmer" else "\u2744\ufe0f"   # a thermometer for warmer, a snowflake for cooler
            await self.notify(f"{icon} It's now {abs(o - i):.1f}°C {current} outside than inside "
                              f"({o:.1f}°C vs {i:.1f}°C). It had been {'cooler' if current == 'warmer' else 'warmer'} for {was}.", kind="temps",
                              chart=lambda: self._picture("Temperature chart 1d"))
            remember(c, current, ts)

    async def _fell(self, rows: Rows, since: int, until: int) -> float | None:
        """The rain that fell from `since` to `until`. A spell that began before `rows` (the last 3 hours) is read back to its start:
        its first tip shows only against the reading before it."""
        if since <= rows[0][0]:
            rows = await self.station.recent((rows[-1][0] - since) / 3600 + 0.25)
        return rain_amount(rows, since, until)

    async def _picture(self, text: str) -> bytes | None:
        """The chart a typed `text` ("Wind chart 1d") would give, for an alert; None when it cannot be made."""
        return await pictures.chart(self.station, text, self.tz)
