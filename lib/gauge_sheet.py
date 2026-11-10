"""The gauges' sheet (specs.Dashboard, what lib/gauges.py draws), laid out by the bot as a client of the service (docs/mcp-service.md
3.1, 4): each gauge's scale and colours (lib/ecowitt/dashboard.py GAUGES) filled from the service's `current` with `context`, its reading
now and the highs and lows of today, the month and the year beside it (rain: its totals and wettest day; wind: its highest gust).

A tile is left out when current has neither a reading nor a figure for it (no indoor sensor), and drawn dimmed when it has figures but
no reading now. The whole dashboard is every gauge's; the report's picture its three (STATUS).
"""

from .ecowitt.dashboard import GAUGES, PERIODS, STATUS, Gauge, gauges_of
from .specs import Dashboard, Tile

ROWS = dict(zip(PERIODS, ("today", "month", "year")))   # a tile's rows, as current's context names them
STATION = "station"


def call(titles=None) -> dict:
    """current's arguments for the gauges of these titles, with context: their readings named; all of them (titles None), every reading,
    so that those the station sends but its file hasn't recorded yet are there too (as the station's own dashboard drew them)."""
    return {"context": True} if titles is None else {"readings": [g.reading for g in gauges_of(titles)], "context": True}


def _row(gauge: Gauge, figures: dict) -> tuple[float | None, ...]:
    """A row of a tile's table from current's figures of that period: Max and Min, Max alone, or (rain) its wettest day and Total."""
    if gauge.total:
        return (figures.get("wettest_day") or {}).get("rain"), figures.get("total")
    high = figures.get("high", figures.get("high_gust"))   # (the wind's Max is its highest gust)
    return (high, figures.get("low")) if gauge.lowest else (high,)


def _value(entry: dict) -> float | None:
    try:
        return float(entry["value"])
    except (KeyError, TypeError, ValueError):
        return None


def sheet(result: dict, titles=None) -> Dashboard | None:
    """The gauges of these titles (all when None) from current's result with context; None when it has none of them (an error, the
    station not answering, nothing read)."""
    if "error" in result or STATION in (result.get("unavailable") or {}):
        return None
    sensors = result.get("sensors") or {}
    tiles = []
    for gauge in gauges_of(titles):
        group, name = gauge.reading.split(".", 1)
        entry = (sensors.get(group) or {}).get(name)
        if not isinstance(entry, dict):
            continue
        now = _value(entry)
        rows = {row: _row(gauge, entry.get(key) or {}) for row, key in ROWS.items()}
        if now is None and all(f is None for figures in rows.values() for f in figures):
            continue
        columns = ("Max", "Total") if gauge.total else ("Max", "Min") if gauge.lowest else ("Max",)
        tiles.append(Tile(gauge.title, gauge.unit, now, gauge.kind, gauge.low, gauge.high, gauge.colours, columns, rows, gauge.ticks, gauge.decimals))
    return Dashboard(tiles) if tiles else None


__all__ = ["GAUGES", "STATUS", "call", "sheet"]
