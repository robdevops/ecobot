"""The gauges' sheet (specs.Dashboard, what lib/gauges.py draws), laid out by the bot as a client of the service (docs/ecobot-mcp.md
3.1, 4): each gauge's scale and colours (the weather station's, lib/ecowitt/dashboard.py GAUGES, then the air sensor's,
lib/airgradient/dashboard.py AIR_GAUGES) filled from the service's `current` with `context`, its reading now and the highs and lows of
today, the month and the year beside it (rain: its totals and wettest day; wind: its highest gust; the US AQI: PM2.5's, as AQI). An air
gauge's band is named from its own reading now, and the US AQI is worked out from PM2.5 now: both dials agree with their needles (the
service's `aqi_us` and `band` are of the lower of the last two readings, for its ratings).

A tile is left out when current has neither a reading nor a figure for it (no indoor sensor, no air sensor, a reading the air sensor
never sent), and drawn dimmed at its floor when it has figures but no reading now. The whole dashboard is every gauge's; the report's
picture its three (STATUS).
"""

import math

from .airgradient.dashboard import AIR_GAUGES, AirGauge, air_gauges_of
from .airgradient.metrics import pm25_aqi
from .ecowitt.dashboard import GAUGES, PERIODS, STATUS, Gauge, gauges_of, scale
from .gauge_look import CELL_H, CELL_W
from .specs import Dashboard, Tile

ROWS = dict(zip(PERIODS, ("today", "month", "year")))   # a tile's rows, as current's context names them
STATION = "station"
PER_ROW = 3              # tiles to a row, up to ROWS_OF_THREE rows; more tiles: the columns that make them largest on a square
ROWS_OF_THREE = 4


def call(titles=None) -> dict:
    """current's arguments for the gauges of these titles (all when None), with context. All: no readings named, so every reading of
    the station and the air sensor there is (an air sensor's readings named where there is none would be refused), and those the
    station sends but its file hasn't recorded yet. Some: their readings named, so only their device is read (the report's three: the
    weather station's)."""
    if titles is None:
        return {"context": True}
    return {"readings": [g.reading for g in (*gauges_of(titles), *air_gauges_of(titles))], "context": True}


def _row(gauge: Gauge | AirGauge, figures: dict) -> tuple[float | None, ...]:
    """A row of a tile's table from current's figures of that period: Max and Min, Max alone, or (rain) its wettest day and Total; the
    AQI's as PM2.5's highest and lowest, worked out as AQI."""
    if isinstance(gauge, AirGauge):
        high, low = figures.get("high"), figures.get("low")
        return tuple(pm25_aqi(f)[0] if gauge.aqi and f is not None else f for f in (high, low))
    if gauge.total:
        return (figures.get("wettest_day") or {}).get("rain"), figures.get("total")
    high = figures.get("high", figures.get("high_gust"))   # (the wind's Max is its highest gust)
    return (high, figures.get("low")) if gauge.lowest else (high,)


def _value(entry: dict, key: str = "value") -> float | None:
    try:
        return float(entry[key])
    except (KeyError, TypeError, ValueError):
        return None


def _tile(gauge: Gauge | AirGauge, entry: dict) -> Tile | None:
    """A gauge's tile from its reading's entry in current's result; None when it has neither a reading nor a figure."""
    now = _value(entry)
    if isinstance(gauge, AirGauge) and gauge.aqi and now is not None:
        now = pm25_aqi(now)[0]   # (the AQI of PM2.5 now, as the PM2.5 dial beside it shows it: not the service's, of the lower of two)
    rows = {row: _row(gauge, entry.get(key) or {}) for row, key in ROWS.items()}
    if now is None and all(f is None for figures in rows.values() for f in figures):
        return None
    if isinstance(gauge, AirGauge):
        band = gauge.band(now)   # (its own reading's: the name always where the needle is)
        ticks = gauge.bounds
        return Tile(gauge.title, gauge.unit, now, "dial", gauge.low, gauge.high, gauge.colours, ("Max", "Min"), rows, ticks,
                    gauge.decimals, gauge.bounds, band)
    columns = ("Max", "Total") if gauge.total else ("Max", "Min") if gauge.lowest else ("Max",)
    low, high, colours, ticks = scale(gauge, now)   # (a thermometer's widens to take in the reading)
    return Tile(gauge.title, gauge.unit, now, gauge.kind, low, high, colours, columns, rows, ticks, gauge.decimals)


def per_row(n: int) -> int:
    """How many tiles to a row: three, up to ROWS_OF_THREE rows (the station's gauges); more, the columns (four to six) that make them
    largest in a square picture."""
    if n <= PER_ROW * ROWS_OF_THREE:
        return PER_ROW
    return max(range(4, 7), key=lambda cols: min(1 / cols, CELL_W / CELL_H / math.ceil(n / cols)))


def sheet(result: dict, titles=None) -> Dashboard | None:
    """The gauges of these titles (all when None) from current's result with context; None when it has none of them (an error, the
    station not answering, nothing read)."""
    if "error" in result or STATION in (result.get("unavailable") or {}):
        return None
    sensors = result.get("sensors") or {}
    tiles = []
    for gauge in (*gauges_of(titles), *air_gauges_of(titles)):
        group, name = gauge.reading.split(".", 1)
        entry = (sensors.get(group) or {}).get(name)
        if isinstance(entry, dict) and (tile := _tile(gauge, entry)):
            tiles.append(tile)
    return Dashboard(tiles, per_row(len(tiles))) if tiles else None


__all__ = ["AIR_GAUGES", "GAUGES", "STATUS", "call", "per_row", "sheet"]
