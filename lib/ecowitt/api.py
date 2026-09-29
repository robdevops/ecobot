"""Ecowitt cloud API v3 (https://doc.ecowitt.net), the calls the bot needs.

  devices:  GET /device/list
  realtime: GET /device/real_time
  history:  GET /device/history   (times are the station's local time)

Every response is {"code": 0, "msg": ..., "data": ...}; a non-zero code (e.g. "System is
busy.") is an error. Ecowitt keeps and allows per request:
  5min   last 90 days    max 1 day per request
  30min  last 365 days   max 1 week per request
  4hour  last 730 days   max 1 month per request
  1day   last 1460 days  max 1 year per request
"""

import asyncio
import logging
from datetime import datetime, timedelta

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.ecowitt.net/api/v3/device"
RETENTION = {"5min": 90, "30min": 365, "4hour": 730, "1day": 1460}  # days kept
MAX_SPAN = {"5min": timedelta(days=1), "30min": timedelta(days=7),
            "4hour": timedelta(days=28), "1day": timedelta(days=365)}
CYCLE_SECONDS = {"5min": 300, "30min": 1800, "4hour": 14400, "1day": 86400}

# Metric units. The names are what the cache remembers (it is cleared if they change);
# UNIT_IDS are Ecowitt's numeric ids for them.
UNITS = {"temp_unitid": "C", "pressure_unitid": "hPa", "wind_speed_unitid": "kmh", "rainfall_unitid": "mm"}
UNIT_IDS = {"temp_unitid": 1, "pressure_unitid": 3, "wind_speed_unitid": 7, "rainfall_unitid": 12}

BUSY_RETRIES = 3
MAX_CONCURRENT = 2
FMT = "%Y-%m-%d %H:%M:%S"


class EcowittError(Exception):
    pass


class EcowittAPI:
    def __init__(self, api_key: str, app_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.keys = {"application_key": app_key, "api_key": api_key}
        self.client = httpx.AsyncClient(timeout=30, transport=transport)
        self._slots = asyncio.Semaphore(MAX_CONCURRENT)
        self.requests = 0  # sent so far, including retries

    async def close(self):
        await self.client.aclose()

    async def _get(self, path: str, **params) -> dict | list:
        """The response's "data". Retries when Ecowitt says it is busy; raises EcowittError."""
        async with self._slots:
            for attempt in range(BUSY_RETRIES + 1):
                self.requests += 1
                try:
                    res = await self.client.get(f"{BASE}/{path}", params={**self.keys, **params})
                    res.raise_for_status()
                    body = res.json()
                    if str(body.get("code")) == "0":
                        return body.get("data") or {}
                    msg = str(body.get("msg") or "API error")
                except (httpx.HTTPError, ValueError) as e:
                    msg = str(e) or type(e).__name__
                if "busy" in msg.lower() and attempt < BUSY_RETRIES:
                    wait = 1.5 * 2 ** attempt
                    log.info("Ecowitt busy (%s), retrying in %.1fs", path, wait)
                    await asyncio.sleep(wait)
                    continue
                raise EcowittError(msg)

    async def devices(self) -> list[dict]:
        data = await self._get("list", limit=50)
        return data.get("list", []) if isinstance(data, dict) else []

    async def realtime(self, mac: str, groups: str) -> dict:
        return await self._get("real_time", mac=mac, call_back=groups, **UNIT_IDS)

    async def history(self, mac: str, cycle: str, start: datetime, end: datetime, groups: str) -> dict:
        """{group: {field: {"unit", "list": {epoch: value}}}}; empty when there is no data."""
        data = await self._get("history", mac=mac, call_back=groups, cycle_type=cycle,
                               start_date=start.strftime(FMT), end_date=end.strftime(FMT), **UNIT_IDS)
        return data if isinstance(data, dict) else {}
