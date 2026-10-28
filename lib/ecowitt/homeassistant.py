"""The station's readings as Home Assistant saw them, for the lowest and highest of each 5 minutes (live.LiveRanges).

Home Assistant's Ecowitt integration is sent every reading the gateway makes (every 16 to 60 seconds, as the gateway is set), and keeps
every change of each sensor for a while (10 days unless its recorder is set otherwise). That is finer than anything Ecowitt's cloud gives
(one reading a minute live, one value per 5 minutes in its history), and it covers the time the bot was not running. So the bot reads
those changes back through Home Assistant's REST API and folds them into the same 5-minute ranges its own live readings go to.

Home Assistant records a state only when it changes, so the value a sensor held at the start of a stretch (its last state before it)
counts in that stretch too. A stretch where it did not change has a range of nothing, which is a range all the same.
Entities are found by their names: the Ecowitt integration calls them sensor.<station>_outdoor_temperature, sensor.<station>_humidity ...
"""

import logging
import time
from bisect import bisect_left, bisect_right
from datetime import UTC, datetime

import httpx
from urllib.parse import urlsplit

from ..netenv import local_name_hint
from ..series import derives_range
from .hass_map import ANCHOR, ENTITIES, NO_RANGE, convert, entity_ids
from .live import BUCKET

log = logging.getLogger(__name__)

HALF = BUCKET // 2
RANGE_SERIES = [e.series for e in ENTITIES.values() if e.group != "rainfall" and e.series not in NO_RANGE and derives_range(e.field)]   # what a chart shades


def parse_time(text: str) -> float:
    """Home Assistant's ISO time ("2026-10-27T11:22:43.420827+00:00") as an epoch."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).timestamp()


def states_of(rows: list[dict]) -> list[tuple[float, float]]:
    """[(when, value)] from one entity's history (Home Assistant's minimal_response), oldest first: a state that is not a number
    ("unavailable", "unknown") is left out."""
    out = []
    for row in rows:
        try:
            out.append((parse_time(row.get("last_changed") or row["last_updated"]), float(row["state"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out)


def ranges_of(states: list[tuple[float, float]], start: float, end: float) -> dict[int, tuple[int, float, float]]:
    """{5-minute mark: (readings, lowest, highest)} for each mark whose five minutes (centred on it) fall within [start, end] and in which the
    sensor had a value: the states changed to in those minutes, and the one it held when they began. Fewer than two readings count as two."""
    times = [t for t, _ in states]
    out = {}
    for mark in range(int(start // BUCKET + 1) * BUCKET, int(end // BUCKET) * BUCKET + 1, BUCKET):
        lo_t, hi_t = mark - HALF, mark + HALF
        if hi_t > end:
            break
        inside = [v for _, v in states[bisect_left(times, lo_t):bisect_left(times, hi_t)]]
        before = bisect_left(times, lo_t)
        if before:   # what it held as the stretch began
            inside.append(states[before - 1][1])
        if inside:
            out[mark] = (max(2, len(inside)), min(inside), max(inside))
    return out


NAME_NOT_FOUND = ("name or service not known", "nodename nor servname", "temporary failure in name resolution", "getaddrinfo failed")


class Trouble:
    """Home Assistant failing, said once for all the places that ask it (the live poll, the minutes, the ranges, the history), not once
    for each; and that it is answering again, once."""

    def __init__(self, url: str):
        self.url, self.failing = url, False

    def failed(self, what: str, error: object, then: str = ""):
        if self.failing:
            return
        self.failing = True
        hint = ""
        if any(m in str(error).lower() for m in NAME_NOT_FOUND):
            hint = local_name_hint(urlsplit(self.url).hostname or "", "HOMEASSISTANT_URL") or (
                f". The name in HOMEASSISTANT_URL ({self.url}) can't be found from here: check it, or use the address (http://192.168.x.x:8123)")
        log.warning("Home Assistant: the %s failed (%s)%s%s", what, error, f"; {then}" if then else "", hint)

    def ok(self):
        if self.failing:
            self.failing = False
            log.info("Home Assistant is answering again")


class HomeAssistant:
    """Home Assistant's REST API: the station's entities (found by name), their states now and their history."""

    def __init__(self, url: str, token: str, transport=None):
        self.client = httpx.AsyncClient(base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=60, transport=transport)
        self.trouble = Trouble(url)
        self.prefix: str | None = None
        self.units: dict[str, str] = {}   # entity id -> the unit Home Assistant shows it in (read with its states)

    async def close(self):
        await self.client.aclose()

    async def _get(self, path: str, **params):
        res = await self.client.get(path, params=params or None)
        res.raise_for_status()
        return res.json()

    async def states(self) -> list[dict]:
        """Every entity's state now ({"entity_id", "state", "attributes", "last_changed", "last_updated" ...}); the units of the station's
        are kept for converting what its history says."""
        found = await self._get("/api/states")
        for s in found:
            if isinstance(s, dict) and (unit := (s.get("attributes") or {}).get("unit_of_measurement")):
                self.units[s.get("entity_id", "")] = unit
        return found

    async def config(self) -> dict:
        """Home Assistant's own settings: its time zone, its location and name."""
        return await self._get("/api/config")

    async def find_prefix(self, states: list[dict] | None = None) -> str | None:
        """The start of the Ecowitt entities' names ("sensor.ws2900_"), found from the one called ...outdoor_temperature; None when there is none."""
        found = sorted(s["entity_id"] for s in (states if states is not None else await self.states())
                       if str(s.get("entity_id", "")).startswith("sensor.") and s["entity_id"].endswith("_" + ANCHOR))
        if not found:
            return None
        if len(found) > 1:
            log.warning("Home Assistant has %d stations' outdoor temperatures (%s); using %s", len(found), ", ".join(found), found[0])
        return found[0][: -len(ANCHOR)]

    async def station(self) -> str:
        """The station's entity prefix, found once. LookupError when Home Assistant has no Ecowitt sensors."""
        if self.prefix is None:
            self.prefix = await self.find_prefix()
            if self.prefix is None:
                raise LookupError("Home Assistant has no sensor ending in _" + ANCHOR + " (is the Ecowitt integration set up?)")
        return self.prefix

    async def history(self, ids: list[str], start: float, end: float) -> dict[str, list[tuple[float, float]]]:
        """{entity id: [(when, value)]} for these entities between two epochs, each oldest first, as Home Assistant recorded them: a state
        only when it changed, the one held at `start` first. An entity it has no numbers for is left out."""
        when = lambda t: datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        found = await self._get(f"/api/history/period/{when(start)}", end_time=when(end), filter_entity_id=",".join(ids),
                                minimal_response="", no_attributes="")
        out = {}
        for rows in found:
            if rows and (entity := rows[0].get("entity_id")) in ids and (states := states_of(rows)):
                out[entity] = states
        return out

    async def ranges(self, start: float, end: float | None = None) -> dict[str, dict[int, tuple[int, float, float]]]:
        """{series: {5-minute mark: (readings, lowest, highest)}} for the readings Home Assistant kept from `start` to `end` (epoch seconds;
        now by default). A series it has no entity or no states for is left out."""
        end = time.time() if end is None else end
        names = entity_ids(await self.station(), RANGE_SERIES)
        out = {}
        for entity, states in (await self.history(list(names), start - BUCKET, end)).items():
            e = names[entity]
            converted = [(t, round(convert(v, e.kind, self.units.get(entity), e.scale), 6)) for t, v in states]
            if found := ranges_of(converted, start, end):
                out[e.series] = {m: (n, lo, hi) for m, (n, lo, hi) in found.items()}
        return out
