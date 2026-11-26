"""Home Assistant as an Ecowitt API: the three calls ecobot-mcp makes of Ecowitt's cloud (devices, realtime, history), answered from Home Assistant.

The rest of ecobot-mcp never knows the difference: the cache, the archive, the charts and the alerts all take what `EcowittAPI` gives, in the
shapes it gives them (api.py; hass_history.py tells how the history is cut). What differs: its live reading is a request to a machine
on the network, so it is asked for every 30 seconds, not once a minute (source.watch_live); it has a "1min" history cycle (Ecowitt has
none); and empty_is_final is False, since Home Assistant only has what it recorded, so an answer with nothing in it does not prove there
was nothing to know (the cloud may have it): the storage does not remember such a range as fetched, and a fallback asks the cloud for it.
It is up as long as Home Assistant is: a failure is an EcowittError, as the cloud's are, passing (a network error, a restart) or not (a
refused token).
"""

import asyncio
import logging
import math
import time
from datetime import tzinfo

import httpx

from ..timeutil import local_epoch
from .api import EcowittError
from .hass_history import SECONDS, Series, converted, history, merged
from .hass_map import ENTITIES, convert, written
from .homeassistant import HomeAssistant, parse_time

log = logging.getLogger(__name__)

CHUNK = 86400                     # a request to Home Assistant covers at most this long (a day, so a response stays a few MB)
LOOKBACK_DAYS = 3650              # how far back its first state is looked for


def live_data(states: list[dict], prefix: str, groups: set[str], units: dict[str, str] | None = None) -> dict:
    """The live reading as Ecowitt's real_time answers: {group: {field: {"time": epoch, "unit", "value"}}}, from Home Assistant's states
    (its own `last_reported`, else `last_updated`, being when the gateway last said it). An entity that is not a number now
    (unavailable, unknown) is left out."""
    mine = {prefix + e.suffix: e for e in ENTITIES.values() if e.group in groups}
    out: dict = {}
    for state in states:
        e = mine.get(state.get("entity_id", "")) if isinstance(state, dict) else None
        if e is None:
            continue
        try:
            value = convert(float(state["state"]), e.kind, (units or {}).get(state["entity_id"]) or (state.get("attributes") or {}).get("unit_of_measurement"), e.scale)
            at = int(parse_time(state.get("last_reported") or state["last_updated"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(value):   # ("nan" is a number to float, not a reading)
            continue
        out.setdefault(e.group, {})[e.field] = {"time": str(at), "unit": e.unit, "value": written(value, e.decimals)}
    return out


class HomeAssistantAPI:
    empty_is_final = False

    def __init__(self, url: str, token: str, tz: tzinfo, mac: str = "HOMEASSISTANT", transport=None, clock=time.time):
        self.ha = HomeAssistant(url, token, transport)
        self.tz, self.mac, self.clock = tz, mac, clock
        self.earliest: float | None = None   # when its first state of the station is (nothing is asked for before it)
        self._looked = False                  # ... and whether that has been looked for (or was known: the station keeps it in its file)
        self._looking = asyncio.Lock()        # (looked for once, whoever asks first)
        self.requests = 0

    async def close(self):
        await self.ha.close()

    def set_timezone(self, tz: tzinfo):
        self.tz = tz

    async def _asked(self, coro):
        """What Home Assistant answered, or an EcowittError: passing for a network error, a timeout or its own failing (5xx, 429, 408); not for
        the rest (a refused token, a Home Assistant without the station)."""
        self.requests += 1
        try:
            return await coro
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            raise EcowittError(f"Home Assistant said HTTP {code}", transient=code >= 500 or code in (408, 429)) from None
        except httpx.HTTPError as e:
            raise EcowittError(f"Home Assistant: {str(e) or type(e).__name__}", transient=True) from None
        except LookupError as e:
            raise EcowittError(str(e), transient=False) from None
        except ValueError as e:   # a reply that is not JSON
            raise EcowittError(f"Home Assistant gave an unreadable reply ({e})", transient=True) from None

    def known_earliest(self, earliest: float):
        """When its first state of the station is, as kept from an earlier start (it doesn't change): not looked for again."""
        self.earliest, self._looked = earliest, True

    @property
    def looked(self) -> bool:
        return self._looked

    async def devices(self) -> list[dict]:
        """The station, as Ecowitt's device list gives one: its name, time zone and place (its settings and its states, asked for
        together), and when its first reading was if that is known (look() finds it: a long history query, left out of the start)."""
        config, prefix = await asyncio.gather(self._asked(self.ha.config()), self._asked(self.ha.station()))
        model = prefix.removeprefix("sensor.").strip("_").upper() or "Ecowitt"
        return [{"mac": self.mac, "name": model, "date_zone_id": config.get("time_zone"), "latitude": config.get("latitude"),
                 "longitude": config.get("longitude"), **({"createtime": int(self.earliest)} if self.earliest else {})}]

    async def look(self) -> float | None:
        """When Home Assistant's first state of the station is (its outdoor temperature's): looked for once, LOOKBACK_DAYS back."""
        return await self._look(await self._asked(self.ha.station()))

    async def _look(self, prefix: str) -> float | None:
        async with self._looking:
            if not self._looked:
                now = self.clock()
                name = prefix + ENTITIES["outdoor.temperature"].suffix
                found = await self._asked(self.ha.history([name], now - LOOKBACK_DAYS * 86400, now))
                self.earliest = found[name][0][0] if name in found else None
                self._looked = True
        return self.earliest

    async def realtime(self, mac: str, groups: str, urgent: bool = False) -> dict:
        prefix = await self._asked(self.ha.station())
        states = await self._asked(self.ha.states())
        return live_data(states, prefix, {g for g in groups.split(",") if g}, self.ha.units)

    async def history(self, mac: str, cycle: str, start, end, groups: str, urgent: bool = False) -> dict:
        """{group: {field: {"unit", "list": {epoch: value}}}} between two naive local times (the station's), as Ecowitt's history gives them;
        nothing for a time Home Assistant has no readings of."""
        if cycle not in SECONDS:
            raise EcowittError(f"no cycle {cycle!r}", transient=False)
        wanted = {g for g in groups.split(",") if g}
        entities = [e for e in ENTITIES.values() if e.group in wanted]
        now = self.clock()
        width = SECONDS[cycle]
        lo, hi = local_epoch(start, self.tz), min(local_epoch(end, self.tz), now)
        if not entities or hi < lo:
            return {}
        prefix = await self._asked(self.ha.station())
        await self._look(prefix)
        if self.earliest is not None:
            lo = max(lo, self.earliest)
        if hi < lo:
            return {}
        ids = {prefix + e.suffix: e for e in entities}
        stop = (int(hi // width) + 1) * width     # the end of the bucket `hi` falls in: a past bucket is told whole, a present one to now
        out: dict = {}
        for chunk in range(int(lo // width) * width, int(hi // width) * width + 1, CHUNK):
            found = await self._asked(self.ha.history(list(ids), chunk, min(chunk + CHUNK, stop, now)))
            series = [Series(ids[i], converted(states, ids[i], self.ha.units.get(i))) for i, states in found.items()]
            merged(out, history(series, cycle, chunk, min(chunk + CHUNK, stop) - 1, now))   # (a day's chunk tells buckets the last did not)
        return out


__all__ = ["HomeAssistantAPI", "live_data"]
