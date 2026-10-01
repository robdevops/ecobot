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
from collections import deque
from datetime import datetime, timedelta

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.ecowitt.net/api/v3/device"
RETENTION = {"5min": 90, "30min": 365, "4hour": 730, "1day": 1460}  # days kept
MAX_SPAN = {"5min": timedelta(days=1), "30min": timedelta(days=7),
            "4hour": timedelta(days=28), "1day": timedelta(days=365)}
CYCLE_SECONDS = {"5min": 300, "30min": 1800, "4hour": 14400, "1day": 86400}
# What the bot keeps cached and warm (the extras feed the rain alerts and predictions). One list, so the
# warm-up and the archive fetch the same thing and can reuse each other's data.
GROUPS = ["outdoor", "indoor", "pressure", "wind", "rainfall", "rainfall_piezo", "solar_and_uvi"]

# Metric units. The names are what the cache remembers (it is cleared if they change);
# UNIT_IDS are Ecowitt's numeric ids for them.
UNITS = {"temp_unitid": "C", "pressure_unitid": "hPa", "wind_speed_unitid": "kmh", "rainfall_unitid": "mm"}
UNIT_IDS = {"temp_unitid": 1, "pressure_unitid": 3, "wind_speed_unitid": 7, "rainfall_unitid": 12}

BUSY_RETRIES = 3
MIN_GAP_SECONDS = 1.0  # Ecowitt rejects requests that come too fast ("Operation too frequent")
RETRY_ON = ("busy", "too frequent")
FMT = "%Y-%m-%d %H:%M:%S"


# Fields Ecowitt reports in a unit we do not use: (field prefix, its unit) -> (the unit kept, the factor). The vapour pressure
# deficit comes back in inHg whatever the pressure unit asked for; it is stored and charted in kPa.
UNIT_FIXES = {("vpd", "inHg"): ("kPa", 3.38639)}


def fix_units(data) -> dict:
    """The response with any field in UNIT_FIXES converted (readings and unit), in place."""
    for fields in data.values() if isinstance(data, dict) else ():
        for name, obj in (fields.items() if isinstance(fields, dict) else ()):
            if not isinstance(obj, dict):
                continue
            unit, factor = next(((u, f) for (prefix, old), (u, f) in UNIT_FIXES.items()
                                 if name.split("_")[0] == prefix and obj.get("unit") == old), (None, None))
            if unit:
                obj["unit"] = unit
                if isinstance(obj.get("list"), dict):
                    obj["list"] = {ts: f"{float(v) * factor:.3f}" if _number(v) else v for ts, v in obj["list"].items()}
                elif _number(obj.get("value")):
                    obj["value"] = f"{float(obj['value']) * factor:.3f}"
    return data


def _number(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


class EcowittError(Exception):
    """transient: a passing problem (network, timeout, rate limit), not Ecowitt rejecting the request."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def _body(res: httpx.Response) -> dict | None:
    """The JSON object Ecowitt answered with ({"code": ..., "msg": ...}), or None if the reply isn't one."""
    try:
        body = res.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) and "code" in body else None


class _TurnLock:
    """One request at a time, with an urgent line: a person waiting for a reading goes before the background refreshes
    queued behind the request in flight (a keep-warm round can queue several requests, two seconds apart)."""

    def __init__(self):
        self._locked = False
        self._queues = {True: deque(), False: deque()}

    async def acquire(self, urgent: bool = False):
        if not self._locked and not self._queues[True] and not self._queues[False]:
            self._locked = True
            return
        waiter = asyncio.get_running_loop().create_future()
        self._queues[urgent].append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():   # the turn was already handed to us: pass it on
                self.release()
            else:
                self._queues[urgent].remove(waiter)
            raise

    def release(self):
        for urgent in (True, False):
            while self._queues[urgent]:
                waiter = self._queues[urgent].popleft()
                if not waiter.cancelled():
                    waiter.set_result(None)                 # the lock stays held: its turn passes to the waiter
                    return
        self._locked = False


class EcowittAPI:
    def __init__(self, api_key: str, app_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.keys = {"application_key": app_key, "api_key": api_key}
        self.client = httpx.AsyncClient(timeout=30, transport=transport)
        self._turn = _TurnLock()  # one request at a time, spaced out; a person's request goes first
        self._last = 0.0
        self.requests = 0  # sent so far, including retries

    async def close(self):
        await self.client.aclose()

    async def _get(self, path: str, urgent: bool = False, **params) -> dict | list:
        """The response's "data". Retries when Ecowitt says it is busy or we were too quick;
        raises EcowittError. urgent: someone is waiting for it, so it goes before queued background requests."""
        loop = asyncio.get_running_loop()
        await self._turn.acquire(urgent)
        try:
            for attempt in range(BUSY_RETRIES + 1):
                if (pause := self._last + MIN_GAP_SECONDS - loop.time()) > 0:
                    await asyncio.sleep(pause)
                self._last = loop.time()
                self.requests += 1
                try:
                    res = await self.client.get(f"{BASE}/{path}", params={**self.keys, **params})
                    body = _body(res)
                    if body is None:  # no Ecowitt answer: the network, or a gateway error page
                        res.raise_for_status()
                        raise ValueError("unreadable response")
                    if str(body.get("code")) == "0" and res.is_success:
                        return body.get("data") or {}
                    msg = str(body.get("msg") or f"HTTP {res.status_code}")
                    # Ecowitt answered and said no (any HTTP status): passing only if busy or rate limited
                    passing = res.status_code in (408, 429) or res.status_code >= 500 or any(k in msg.lower() for k in RETRY_ON)
                except (httpx.HTTPError, ValueError) as e:
                    msg, passing = str(e) or type(e).__name__, True
                if any(k in msg.lower() for k in RETRY_ON) and attempt < BUSY_RETRIES:
                    wait = 3 * 2 ** attempt
                    log.info("Ecowitt says %r (%s), retrying in %.1fs", msg, path, wait)
                    await asyncio.sleep(wait)
                    continue
                raise EcowittError(msg, transient=passing)
        finally:
            self._turn.release()

    async def devices(self) -> list[dict]:
        data = await self._get("list", limit=50)
        return data.get("list", []) if isinstance(data, dict) else []

    async def realtime(self, mac: str, groups: str, urgent: bool = False) -> dict:
        return fix_units(await self._get("real_time", urgent=urgent, mac=mac, call_back=groups, **UNIT_IDS))

    async def history(self, mac: str, cycle: str, start: datetime, end: datetime, groups: str) -> dict:
        """{group: {field: {"unit", "list": {epoch: value}}}}; empty when there is no data."""
        data = await self._get("history", mac=mac, call_back=groups, cycle_type=cycle,
                               start_date=start.strftime(FMT), end_date=end.strftime(FMT), **UNIT_IDS)
        return fix_units(data) if isinstance(data, dict) else {}
