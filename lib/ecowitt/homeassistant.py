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

from .live import BUCKET

log = logging.getLogger(__name__)

# series ("group.field") -> (the entity's name after the station's: sensor.<station>_<this>, what to multiply its state by)
ENTITIES = {
    "outdoor.temperature": ("outdoor_temperature", 1.0), "outdoor.humidity": ("humidity", 1.0), "outdoor.dew_point": ("dewpoint", 1.0),
    "outdoor.feels_like": ("feels_like_temperature", 1.0), "outdoor.vpd": ("vapour_pressure_deficit", 0.1),   # Home Assistant gives it in hPa, the bot in kPa
    "indoor.temperature": ("indoor_temperature", 1.0), "indoor.humidity": ("indoor_humidity", 1.0), "indoor.dew_point": ("indoor_dewpoint", 1.0),
    "pressure.relative": ("relative_pressure", 1.0), "solar_and_uvi.solar": ("solar_radiation", 1.0), "solar_and_uvi.uvi": ("uv_index", 1.0),
}
ANCHOR = "outdoor_temperature"   # the entity the station's name is read from
HALF = BUCKET // 2


def _epoch(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).timestamp()


def states_of(rows: list[dict]) -> list[tuple[float, float]]:
    """[(when, value)] from one entity's history (Home Assistant's minimal_response), oldest first: a state that is not a number
    ("unavailable", "unknown") is left out."""
    out = []
    for row in rows:
        try:
            out.append((_epoch(row.get("last_changed") or row["last_updated"]), float(row["state"])))
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


class HomeAssistant:
    def __init__(self, url: str, token: str, transport=None):
        self.client = httpx.AsyncClient(base_url=url.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=60, transport=transport)
        self.prefix: str | None = None

    async def close(self):
        await self.client.aclose()

    async def find_prefix(self) -> str | None:
        """The start of the Ecowitt entities' names ("sensor.ws2900_"), found from the one called ...outdoor_temperature; None when there is none."""
        res = await self.client.get("/api/states")
        res.raise_for_status()
        found = sorted(s["entity_id"] for s in res.json() if str(s.get("entity_id", "")).startswith("sensor.") and s["entity_id"].endswith("_" + ANCHOR))
        if not found:
            return None
        if len(found) > 1:
            log.warning("Home Assistant has %d stations' outdoor temperatures (%s); using %s", len(found), ", ".join(found), found[0])
        return found[0][: -len(ANCHOR)]

    async def ranges(self, start: float, end: float | None = None) -> dict[str, dict[int, tuple[int, float, float]]]:
        """{series: {5-minute mark: (readings, lowest, highest)}} for the readings Home Assistant kept from `start` to `end` (epoch seconds;
        now by default). A series it has no entity or no states for is left out."""
        end = time.time() if end is None else end
        if self.prefix is None:
            self.prefix = await self.find_prefix()
            if self.prefix is None:
                raise LookupError("Home Assistant has no sensor ending in _" + ANCHOR + " (is the Ecowitt integration set up?)")
        names = {self.prefix + name: (series, scale) for series, (name, scale) in ENTITIES.items()}
        when = datetime.fromtimestamp(start - BUCKET, UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        res = await self.client.get(f"/api/history/period/{when}", params={
            "end_time": datetime.fromtimestamp(end, UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00"), "filter_entity_id": ",".join(names),
            "minimal_response": "", "no_attributes": ""})
        res.raise_for_status()
        out = {}
        for rows in res.json():
            if not rows:
                continue
            entity = rows[0].get("entity_id")   # (minimal_response names the entity on the first state only)
            if entity not in names:
                continue
            series, scale = names[entity]
            states = [(t, round(v * scale, 6)) for t, v in states_of(rows)]
            if found := ranges_of(states, start, end):
                out[series] = {m: (n, lo, hi) for m, (n, lo, hi) in found.items()}
        return out
