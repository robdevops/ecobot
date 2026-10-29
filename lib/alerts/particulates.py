"""Particulates alerts (PM2.5 and PM10) from the AirGradient sensor, checked every 30 minutes.

"Wear a mask outside" when PM2.5 or PM10 reaches US AQI 151+ ("unhealthy", the official level
at which masks are advised for everyone) in two readings in a row, each check counting only when the sensor's last
ANTISPIKE_REQUIRE_CONSECUTIVE (2) readings are all that high (a spike never does); "back to safe" when both are at
AQI 100 or below in two readings in a row (confirm.py: a check that brings no new reading counts for nothing). Every episode gets the pair. Particles only: masks
filter particles, not gases like CO2/VOC/NOx.
"""

import logging
from datetime import datetime

from . import confirm, pictures
from .menu import kinds_of

log = logging.getLogger(__name__)

CHECK_SECONDS = 30 * 60
MASK_PM25, MASK_PM10 = 55.5, 255.0     # US AQI 151+
SAFE_PM25, SAFE_PM10 = 35.4, 154.0     # US AQI 100 or better
STALE_SECONDS = 2 * 3600               # ignore readings older than this (sensor offline)


class ParticulatesMonitor:
    KINDS = kinds_of("AirGradient")

    def __init__(self, air, state, notify):
        self.air, self.state, self.notify = air, state, notify

    async def check(self, latest=None, now: datetime | None = None):
        """latest: the sensor's latest reading (AirGradient.latest(): the reading, when it was taken, what it has been sustained at)."""
        reading, when, held = latest if latest is not None else await self.air.latest()
        now = now or datetime.now(self.air.tz)
        m = self.state.monitor.setdefault("particulates", {"unsafe": False, "above": 0, "below": 0})
        if when and (now - when).total_seconds() > STALE_SECONDS:
            log.info("Particulates: latest reading is stale (%s); skipping", when)
            return
        pm25 = (reading.get("pm2_5") or {}).get("value")
        pm10 = (reading.get("pm10") or {}).get("value")
        if pm25 is None and pm10 is None:
            return
        if when is not None and not confirm.new_reading(m, when.timestamp()):   # the sensor has said nothing new since the last check
            return
        # held: the lowest of the last readings (ANTISPIKE_REQUIRE_CONSECUTIVE), so a spike alone can't make the air unhealthy
        high25, high10 = held.get("pm2_5", pm25), held.get("pm10", pm10)
        bad = (high25 is not None and high25 >= MASK_PM25) or (high10 is not None and high10 >= MASK_PM10)
        good = (pm25 is None or pm25 <= SAFE_PM25) and (pm10 is None or pm10 <= SAFE_PM10)
        confirm.run(m, "above", bad)
        confirm.run(m, "below", good)
        if not m["unsafe"] and m["above"] >= confirm.CONFIRM_READINGS:
            m["unsafe"] = True
            await self.notify("\U0001f637 Unhealthy air outside — wear a P2/N95 mask today. " + self._levels(reading),
                              link=self.air.link, kind="particulates", chart=self._picture)
        elif m["unsafe"] and m["below"] >= confirm.CONFIRM_READINGS:
            m["unsafe"] = False
            await self.notify("✅ Outdoor air is safe again. " + self._levels(reading), link=self.air.link, kind="particulates",
                              chart=self._picture)
        self.state.save()

    async def _picture(self) -> bytes | None:
        """The 1-day particulates chart (what the Particulates button gives), for the alert; None when it cannot be made."""
        return await pictures.chart(self.air, pictures.CHARTS["particulates"], self.air.tz)

    @staticmethod
    def _levels(reading: dict) -> str:
        """Short summary: PM2.5 (with AQI), plus PM10 only when it's elevated."""
        parts = []
        pm25, pm10 = reading.get("pm2_5") or {}, reading.get("pm10") or {}
        if pm25.get("value") is not None:
            parts.append(f"PM2.5 {pm25['value']:.0f} µg/m³" + (f" (AQI {pm25['aqi_us']})" if "aqi_us" in pm25 else ""))
        if pm10.get("value") is not None and pm10["value"] > SAFE_PM10:
            parts.append(f"PM10 {pm10['value']:.0f} µg/m³")
        return (", ".join(parts) + ".") if parts else ""
