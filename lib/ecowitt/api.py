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
import re
from collections import deque
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import NamedTuple

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.ecowitt.net/api/v3/device"


class Cycle(NamedTuple):
    """One of Ecowitt's resolutions."""
    seconds: int       # the time one reading covers
    span: timedelta    # the most one request may ask for
    kept: int          # days Ecowitt keeps its readings
    settle: int        # seconds after a reading's time is over before its value is final


CYCLES = {"5min": Cycle(300, timedelta(days=1), 90, 3600), "30min": Cycle(1800, timedelta(days=7), 365, 7200),
          "4hour": Cycle(14400, timedelta(days=28), 730, 8 * 3600), "1day": Cycle(86400, timedelta(days=365), 1460, 86400)}
# Readings every half hour or finer: they fit a local day exactly (Ecowitt's 4-hour and daily ones are cut on UTC hours),
# and they change at every refresh.
FINE_CYCLES = tuple(name for name, c in CYCLES.items() if c.seconds <= 1800)


MINUTE = "1min"   # a cycle only Home Assistant has (hass_api): the state held at the end of each minute


def cycle_seconds(cycle: str) -> int:
    """The time one reading of this cycle covers (the minute's too)."""
    return 60 if cycle == MINUTE else CYCLES[cycle].seconds


def kept_from(cycle: str, today: date) -> date:
    """The first day whose readings at this cycle Ecowitt is sure still to have: two days clear of its retention edge, so a
    request can't race the deletion."""
    return today - timedelta(days=CYCLES[cycle].kept - 2)


# What the bot keeps cached and warm (the extras feed the rain alerts and predictions). One list, so the
# warm-up and the archive fetch the same thing and can reuse each other's data.
GROUPS = ["outdoor", "indoor", "pressure", "wind", "rainfall", "rainfall_piezo", "solar_and_uvi"]

# The metric units every request asks for, by Ecowitt's numeric ids.
UNIT_IDS = {"temp_unitid": 1, "pressure_unitid": 3, "wind_speed_unitid": 7, "rainfall_unitid": 12}


def unit_text(text: str | None) -> str | None:
    """A reading or a unit as Ecowitt writes it, written the bot's way: "12.3 ℃" -> "12.3 °C", "131 º" -> "131°", "1.8 mm/hr" ->
    "1.8 mm/h". None for nothing."""
    return text.replace("℃", "°C").replace(" º", "°").replace("º", "°").replace("mm/hr", "mm/h").strip() if text else None

BUSY_RETRIES = 3
MIN_GAP_SECONDS = 1.0  # Ecowitt rejects requests that come too fast ("Operation too frequent")
RETRY_ON = ("busy", "too frequent")
FMT = "%Y-%m-%d %H:%M:%S"
KEYS_IN_URL = re.compile(r"\b(application_key|api_key)=[^&\s'\"]*")   # the keys go in the query string


# Fields Ecowitt reports in a unit we do not use: (field prefix, its unit) -> (the unit kept, the factor). The vapour pressure
# deficit comes back in inHg whatever the pressure unit asked for; it is stored and charted in kPa.
UNIT_FIXES = {("vpd", "inHg"): ("kPa", 3.38639)}


def daily_run(tz: tzinfo, first: date, last: date) -> str:
    """When Ecowitt's daily records start and end, local time ("10am to 10am"): they are UTC days, so in Melbourne they run from
    10am, or from 11am in daylight saving; a period that has both says both."""
    days = [first + timedelta(days=n) for n in range(0, (last - first).days + 1, 30)] + [last]
    hours = sorted({datetime.combine(d, time(), UTC).astimezone(tz).hour for d in days})
    run = [f"{h % 12 or 12}{'am' if h < 12 else 'pm'} to {h % 12 or 12}{'am' if h < 12 else 'pm'}" for h in hours]
    return run[0] if len(run) == 1 else f"{run[0]} ({run[1]} in daylight saving)"


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
            elif waiter in self._queues[urgent]:            # (a release since the cancel has already dropped it)
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
        """The response's "data"; raises EcowittError. Asked again, up to BUSY_RETRIES times and waiting longer each time, when
        Ecowitt is busy, rate limited or erring (429, 5xx), not when it refuses the request or there is no answer at all.
        urgent: someone is waiting for it, so it goes before queued background requests (and the turn is free while a request
        waits to be asked again)."""
        loop = asyncio.get_running_loop()
        for attempt in range(BUSY_RETRIES + 1):
            await self._turn.acquire(urgent)
            try:
                if (pause := self._last + MIN_GAP_SECONDS - loop.time()) > 0:
                    await asyncio.sleep(pause)
                self._last = loop.time()
                self.requests += 1
                try:
                    res = await self.client.get(f"{BASE}/{path}", params={**self.keys, **params})
                except httpx.HTTPError as e:   # no answer: the network, a timeout
                    msg, passing, again = str(e) or type(e).__name__, True, False
                else:
                    body = _body(res)
                    if body is not None and str(body.get("code")) == "0" and res.is_success:
                        return body.get("data") or {}
                    if body is None:   # a gateway's error page, or something unreadable
                        msg = f"HTTP {res.status_code}" if not res.is_success else "unreadable response"
                    else:              # Ecowitt said no: passing only if it is busy, rate limiting or failing itself
                        msg = str(body.get("msg") or f"HTTP {res.status_code}")
                    again = res.status_code == 429 or res.status_code >= 500 or any(k in msg.lower() for k in RETRY_ON)
                    passing = again or body is None or res.status_code == 408
            finally:
                self._turn.release()
            msg = KEYS_IN_URL.sub(r"\1=…", msg)   # an error can quote the URL; its text is shown to the model and logged
            if again and attempt < BUSY_RETRIES:
                wait = 3 * 2 ** attempt
                log.info("Ecowitt says %r (%s), retrying in %.1fs", msg, path, wait)
                await asyncio.sleep(wait)
                continue
            raise EcowittError(msg, transient=passing)

    async def devices(self) -> list[dict]:
        data = await self._get("list", limit=50)
        return data.get("list", []) if isinstance(data, dict) else []

    async def realtime(self, mac: str, groups: str, urgent: bool = False) -> dict:
        return fix_units(await self._get("real_time", urgent=urgent, mac=mac, call_back=groups, **UNIT_IDS))

    async def history(self, mac: str, cycle: str, start: datetime, end: datetime, groups: str, urgent: bool = False) -> dict:
        """{group: {field: {"unit", "list": {epoch: value}}}}; empty when there is no data. urgent: as for _get."""
        data = await self._get("history", urgent=urgent, mac=mac, call_back=groups, cycle_type=cycle,
                               start_date=start.strftime(FMT), end_date=end.strftime(FMT), **UNIT_IDS)
        return fix_units(data) if isinstance(data, dict) else {}
