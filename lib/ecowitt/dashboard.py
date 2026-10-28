"""The dashboard's gauges from the station: each reading as it is now, with its highest and lowest today, this month and this year.

Now is the live reading. The extremes are worked out from the cache alone (days.day_values: exact days from the 5- or 30-minute readings
where it has them, else the daily buckets), with the live reading counted in, so the day's figures are never behind the gauge. A
tile is left out when the station has no such reading at all (no indoor sensor), and drawn dimmed when it has one but not now.
"""

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta, tzinfo

from ..specs import Dashboard, Tile
from ..timeutil import day_bounds
from .days import day_values
from .store import HistoryCache

COLD_TO_HOT = ("#7DE7FF", "#B2EEFA", "#F7C9C9", "#EE7070", "#D32F27")
WATER = ("#DFFBFF", "#B2EEFA", "#80E5FA", "#4FB3D6", "#2C7592")
PRESSURE = ("#C5CAFA", "#A5ABF7", "#8087F5", "#5C5CF0", "#3F2FD0")
WIND = ("#E8E8E8", "#C8CDD2", "#A2ABB4", "#7C8790", "#56626C")
SUN = ("#FBE79A", "#F5D45A", "#F2A53C", "#D27A2A", "#A9501F")
UV = ("#A9E34B", "#F7E04A", "#F4A93B", "#E8562A", "#9A1F5A", "#7A2CF0")
DEGREES = (60, 40, 20, 0, -20, -40)


@dataclass(frozen=True)
class Gauge:
    title: str
    unit: str
    kind: str
    low: float
    high: float
    colours: tuple[str, ...]
    ticks: tuple[float, ...]
    live: tuple[str, str]                 # the live reading: (group, field)
    highest: str                          # the day figure (days.FIELDS) whose highest is its Max
    lowest: str | None = None             # ... whose lowest is its Min (none: no Min column)
    decimals: int = 1
    total: bool = False                   # rain: the Total column, the sum of the days' figures (its Max is the wettest day's)


GAUGES = (
    Gauge("Temperature", "°C", "thermometer", -40, 60, COLD_TO_HOT, DEGREES, ("outdoor", "temperature"), "temp_max", "temp_min"),
    Gauge("Humidity", "%", "dial", 0, 100, WATER, (0, 20, 40, 60, 80, 100), ("outdoor", "humidity"), "humidity_max", "humidity_min", 0),
    Gauge("Atmospheric pressure", "hPa", "dial", 950, 1050, PRESSURE, (950, 970, 990, 1010, 1030, 1050), ("pressure", "relative"),
          "pressure_max", "pressure_min"),
    Gauge("Dew point", "°C", "thermometer", -40, 60, COLD_TO_HOT, DEGREES, ("outdoor", "dew_point"), "dew_point_max", "dew_point_min"),
    Gauge("Feels like", "°C", "thermometer", -40, 60, COLD_TO_HOT, DEGREES, ("outdoor", "feels_like"), "feels_like_max", "feels_like_min"),
    Gauge("Wind", "km/h", "dial", 0, 100, WIND, (0, 20, 40, 60, 80, 100), ("wind", "wind_speed"), "wind_gust", None, 0),
    Gauge("Rain", "mm", "beaker", 0, 100, WATER, (100, 80, 60, 40, 20, 0), ("rainfall", "daily"), "rain", None, 1, True),
    Gauge("Rain rate", "mm/h", "dial", 0, 50, WATER, (0, 10, 20, 30, 40, 50), ("rainfall", "rain_rate"), "rain_rate_max"),
    Gauge("Solar radiation", "W/m²", "dial", 0, 1500, SUN, (0, 300, 600, 900, 1200, 1500), ("solar_and_uvi", "solar"), "solar_max", None, 0),
    Gauge("UV index", "", "dial", 0, 12, UV, (0, 3, 6, 8, 11), ("solar_and_uvi", "uvi"), "uv_max"),
    Gauge("Indoor temperature", "°C", "thermometer", -40, 60, COLD_TO_HOT, DEGREES, ("indoor", "temperature"), "indoor_temp_max", "indoor_temp_min"),
    Gauge("Indoor humidity", "%", "dial", 0, 100, WATER, (0, 20, 40, 60, 80, 100), ("indoor", "humidity"), "indoor_humidity_max",
          "indoor_humidity_min", 0),
)
PERIODS = ("Daily", "Monthly", "Yearly")
STATUS = ("Temperature", "Humidity", "Rain")   # the gauges the report ("status") carries


def gauges_of(titles=None) -> list[Gauge]:
    """The gauges with these titles, in the dashboard's order (all of them when None)."""
    return [g for g in GAUGES if titles is None or g.title in titles]


def periods_of(today: date) -> dict[str, list[date]]:
    """The days each row covers, up to today: the day, the month so far and the year so far."""
    year = [today.replace(month=1, day=1) + timedelta(days=n) for n in range((today - today.replace(month=1, day=1)).days + 1)]
    return {"Daily": [today], "Monthly": [d for d in year if d.month == today.month], "Yearly": year}


def live_value(live: dict, where: tuple[str, str]) -> float | None:
    group, field = where
    try:
        return float(((live.get(group) or {}).get(field) or {})["value"])
    except (KeyError, TypeError, ValueError):
        return None


def figures(gauge: Gauge, values: dict, days: list[date], now: float | None, seen: tuple[float, float] | None = None) -> tuple[float | None, ...]:
    """A row of a gauge's table: Max, then Min or Total, over these days (the live reading counted in for today's highs and lows, and
    `seen`, the lowest and highest the bot recorded over the stretch the row covers, which the cache's 5-minute and 30-minute figures
    can miss between their marks)."""
    highs = [values[gauge.highest][d] for d in days if d in values.get(gauge.highest, {})]
    if gauge.total:
        return (max(highs) if len(days) > 1 and highs else None, round(sum(highs), 1) if highs else None)
    highs += [x for x in (now, seen and seen[1]) if x is not None]
    row = [max(highs) if highs else None]
    if gauge.lowest:
        lows = [values[gauge.lowest][d] for d in days if d in values.get(gauge.lowest, {})] + [x for x in (now, seen and seen[0]) if x is not None]
        row.append(min(lows) if lows else None)
    return tuple(row)


def live_extremes(ranges, gauges: list[Gauge], tz: tzinfo, today: date) -> dict[str, dict[str, tuple[float, float]]]:
    """{series "group.field": {"Daily": (lowest, highest), "Monthly": ...}} the bot recorded today and this month so far (live.LiveRanges:
    at most the last RANGE_KEEP_DAYS, which holds the month). The year's row uses the month's: it takes in the month."""
    start, end = day_bounds(today, tz)[0], day_bounds(today, tz)[1]
    month = day_bounds(today.replace(day=1), tz)[0]
    out: dict = {}
    for gauge in gauges:
        series = ".".join(gauge.live)
        for name, since in (("Daily", start), ("Monthly", month)):
            if got := ranges.extremes(series, since, end):
                out.setdefault(series, {})[name] = got
    return out


def sheet(live: dict, values: dict, today: date, titles=None, seen: dict | None = None) -> Dashboard | None:
    """The dashboard from a live reading and the days' figures, with the gauges of these titles only (all when None); None when the
    station has none of the readings. seen: live_extremes()."""
    tiles = []
    days = periods_of(today)
    for gauge in gauges_of(titles):
        now = live_value(live, gauge.live)
        if now is None and not any(values.get(name) for name in (gauge.highest, gauge.lowest) if name):
            continue
        mine = (seen or {}).get(".".join(gauge.live), {})
        rows = {name: figures(gauge, values, days[name], None if gauge.total else now, mine.get("Monthly" if name == "Yearly" else name))
                for name in PERIODS}
        if gauge.total and now is not None:   # today's rain is the gauge's own reading, whatever the day's cached figure lags behind
            rows["Daily"] = (None, now)
        columns = ("Max", "Total") if gauge.total else ("Max", "Min") if gauge.lowest else ("Max",)
        tiles.append(Tile(gauge.title, gauge.unit, now, gauge.kind, gauge.low, gauge.high, gauge.colours, columns, rows, gauge.ticks, gauge.decimals))
    return Dashboard(tiles) if tiles else None


async def dashboard(live: dict, cache: HistoryCache, mac: str, tz: tzinfo, today: date, titles=None,
                    memo: dict | None = None, ranges=None) -> Dashboard | None:
    """The gauges (of these titles; all when None) for a live reading ({group: {field: {value, unit}}}, as Ecowitt's real_time gives
    it), the extremes from the cache. `memo` keeps the figures of the days before yesterday between calls (they no longer change), so
    the report's Refresh reads only yesterday's and today's again. `ranges` (live.LiveRanges): what the bot recorded between the cache's
    marks, for today's and the month's highs and lows."""
    names = sorted({n for g in gauges_of(titles) for n in (g.highest, g.lowest) if n})
    year = periods_of(today)["Yearly"]
    settled = [d for d in year if d < today - timedelta(days=1)]
    key = (today, tuple(names))
    old = memo.get(key) if memo is not None else None
    if old is None:
        old = (await asyncio.to_thread(day_values, cache, mac, tz, names, settled))[0] if settled else {}
        if memo is not None:
            for stale in [k for k in memo if k[0] != today]:
                del memo[stale]
            memo[key] = old
    fresh, _ = await asyncio.to_thread(day_values, cache, mac, tz, names, [d for d in year if d not in settled])
    seen = await asyncio.to_thread(live_extremes, ranges, gauges_of(titles), tz, today) if ranges is not None else None
    return sheet(live, {n: {**old.get(n, {}), **fresh.get(n, {})} for n in names}, today, titles, seen)
