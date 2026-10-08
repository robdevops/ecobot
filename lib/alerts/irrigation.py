"""The irrigation controller, checked once a day (18:00 local by default).

  - a battery below 5% is told to the chats subscribed to "irrigation battery" (at most once a day, until it is charged);
  - if more than 1 mm of rain fell in the last 24 hours, or Open-Meteo gives today or tomorrow a 50% or better chance of at least
    1 mm of rain, the controller is put on a 24 hour weather delay (it skips its own schedule). A longer delay already set is left alone.

The bot has no scheduler, so `run` sleeps until the next check time. If the controller can't be reached it tries again every 30 minutes
until it works or the day ends. The date of the last completed check is saved, so a restart neither repeats a day nor skips one.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from ..ecowitt.outlook import rain_amount
from ..timeutil import now_local
from .forecast import RAIN_FROM_PCT

log = logging.getLogger(__name__)

BATTERY_LOW_PCT = 5
MEASURED_MM = 1.0              # more than this in the last 24 hours counts as rain
RETRY_SECONDS = 30 * 60
MEASURED_HOURS = 24


class IrrigationMonitor:
    def __init__(self, device, state, notify, tz, hour: int = 18, station=None, forecast=None):
        """station: the Ecowitt source (its recent() gives the measured rain); forecast: the Forecast source. Either may be None."""
        self.device, self.state, self.notify, self.tz, self.hour = device, state, notify, tz, hour
        self.station, self.forecast = station, forecast

    def _memory(self) -> dict:
        return self.state.monitor.setdefault("irrigation", {})

    def due(self, now: datetime) -> bool:
        """Is today's check still to be done (it is past the check time and none is recorded for today)?"""
        return now.hour >= self.hour and self._memory().get("checked") != now.date().isoformat()

    def seconds_until_next(self, now: datetime) -> float:
        target = now.replace(hour=self.hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return max(1.0, (target - now).total_seconds())

    async def run(self):
        """Forever: check when due, else sleep until the next check time."""
        while True:
            now = now_local(self.tz)
            if self.due(now):
                try:
                    await self.check(now)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("Irrigation check failed (%s: %s); trying again in %d minutes", type(e).__name__, e, RETRY_SECONDS // 60)
                    await asyncio.sleep(min(RETRY_SECONDS, self.seconds_until_next(now)))
                    continue
                now = now_local(self.tz)
            await asyncio.sleep(self.seconds_until_next(now))

    async def check(self, now: datetime | None = None):
        """One check: the battery, then the rain. Raises when the controller can't be reached (nothing is recorded then)."""
        now = now or now_local(self.tz)
        today = now.date().isoformat()
        status = await self.device.status()
        memory = self._memory()
        battery = status.get("battery_percentage")
        if battery is not None and battery < BATTERY_LOW_PCT and memory.get("battery_warned") != today:
            memory["battery_warned"] = today
            self.state.save()
            await self.notify(f"🪫 The irrigation battery is at {battery}%. Replace or recharge it soon, or watering will stop.",
                              kind="irrigation")
        reason = await self.rain_reason()
        if reason:
            current = status.get("weather_delay")
            if current in (None, "cancel"):
                await self.device.delay("24h")
                log.info("Irrigation: delayed 24 hours (%s)", reason)
            else:
                log.info("Irrigation: %s, but the delay is already %s", reason, current)
        else:
            log.info("Irrigation: no rain measured or forecast; the schedule stands")
        memory["checked"] = today
        self.state.save()

    async def rain_reason(self) -> str | None:
        """Why the irrigation should be delayed (what rained or will), or None."""
        if self.station is not None:
            try:
                rows = await self.station.recent(MEASURED_HOURS)
                fell = rain_amount(rows, rows[0][0], rows[-1][0]) if rows else None
            except Exception as e:   # the forecast can still decide
                log.warning("Irrigation: the measured rain is unavailable (%s: %s)", type(e).__name__, e)
                fell = None
            if fell is not None and fell > MEASURED_MM:
                return f"{fell:g} mm of rain in the last 24 hours"
        if self.forecast is not None:
            today = self.forecast.now().date()
            for day in self.forecast.upcoming(2):
                chance = day.get("rain_1mm_pct")
                if chance is not None and chance >= RAIN_FROM_PCT:
                    return f"{chance}% chance of at least 1 mm {'today' if day['date'] == today else 'tomorrow'}"
        return None
