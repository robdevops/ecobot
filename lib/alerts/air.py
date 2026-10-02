"""Air-quality alerts from the AirGradient sensor, checked every 30 minutes.

"Wear a mask outside" when PM2.5 or PM10 reaches US AQI 151+ ("unhealthy", the official level
at which masks are advised for everyone) for two checks in a row; "back to safe" when both are at
AQI 100 or below for two checks in a row. Every episode gets the pair. Particles only: masks
filter particles, not gases like CO2/VOC/NOx.
"""

import logging
from datetime import datetime

log = logging.getLogger(__name__)

CHECK_SECONDS = 30 * 60
MASK_PM25, MASK_PM10 = 55.5, 255.0     # US AQI 151+
SAFE_PM25, SAFE_PM10 = 35.4, 154.0     # US AQI 100 or better
CONFIRM_CHECKS = 2                     # consecutive checks, so a passing puff of smoke doesn't count
STALE_SECONDS = 2 * 3600               # ignore readings older than this (sensor offline)


class AirMonitor:
    def __init__(self, air, state, notify):
        self.air, self.state, self.notify = air, state, notify

    async def check(self, reading: dict | None = None, now: datetime | None = None):
        reading = reading if reading is not None else await self.air.current()
        now = now or datetime.now(self.air.tz)
        m = self.state.monitor.setdefault("air", {"unsafe": False, "above": 0, "below": 0})
        when = reading.get("_time_utc")
        if when and (now - when).total_seconds() > STALE_SECONDS:
            log.info("Air quality: latest reading is stale (%s); skipping", when)
            return
        pm25 = (reading.get("pm2_5") or {}).get("value")
        pm10 = (reading.get("pm10") or {}).get("value")
        if pm25 is None and pm10 is None:
            return
        bad = (pm25 is not None and pm25 >= MASK_PM25) or (pm10 is not None and pm10 >= MASK_PM10)
        good = (pm25 is None or pm25 <= SAFE_PM25) and (pm10 is None or pm10 <= SAFE_PM10)
        m["above"] = m["above"] + 1 if bad else 0
        m["below"] = m["below"] + 1 if good else 0
        if not m["unsafe"] and m["above"] >= CONFIRM_CHECKS:
            m["unsafe"] = True
            await self.notify("\U0001f637 Unhealthy air outside — wear a P2/N95 mask today. " + self._levels(reading),
                              link=self.air.link, kind="air")
        elif m["unsafe"] and m["below"] >= CONFIRM_CHECKS:
            m["unsafe"] = False
            await self.notify("✅ Outdoor air is safe again. " + self._levels(reading), link=self.air.link, kind="air")
        self.state.save()

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
