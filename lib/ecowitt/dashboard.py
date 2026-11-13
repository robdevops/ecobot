"""The station's gauges: what each shows (GAUGES: its reading, its scale and colours, the day figures beside it) and their figures, each
reading as it is now with its highest and lowest today, this month and this year (figures()).

Now is the live reading. The extremes are the day rows' (days.day_values_on: exact days from the 5- or 30-minute readings where they hold
the day, else the daily buckets; today from what there is so far), which take in the bands the bot and Home Assistant saw between the
5-minute readings, with the live reading counted in, so the day's figures are never behind the gauge.

The service's `current` with `context` gives these figures (lib/service/current.py context_of); the bot lays its sheet of gauges out
from that result (lib/gauge_sheet.py), with GAUGES' scales and colours.
"""

import math
from dataclasses import dataclass
from datetime import date, timedelta

COLD_TO_HOT = ("#7DE7FF", "#B2EEFA", "#F7C9C9", "#EE7070", "#D32F27")
WATER = ("#DFFBFF", "#B2EEFA", "#80E5FA", "#4FB3D6", "#2C7592")
PRESSURE = ("#C5CAFA", "#A5ABF7", "#8087F5", "#5C5CF0", "#3F2FD0")
WIND = ("#E8E8E8", "#C8CDD2", "#A2ABB4", "#7C8790", "#56626C")
SUN = ("#FBE79A", "#F5D45A", "#F2A53C", "#D27A2A", "#A9501F")
UV = ("#A9E34B", "#F7E04A", "#F4A93B", "#E8562A", "#9A1F5A", "#7A2CF0")
DEGREES = (40, 30, 20, 10, 0)          # a thermometer's scale as it starts (THERMOMETER), before it widens to take in the reading
THERMOMETER = (0, 40, 10)               # a thermometer's scale: its ends and its step, in °C; widened a step at a time to the reading


def degree_colour(lower: float) -> str:
    """The colour of a thermometer's band of 10 degrees, from its lower end: blues below 0, pinks to reds above."""
    for top, colour in ((-10, "#7DE7FF"), (0, "#B2EEFA"), (10, "#F7C9C9"), (20, "#F2A0A0"), (30, "#EE7070"), (40, "#E04A42")):
        if lower < top:
            return colour
    return "#D32F27"


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
    counted: tuple[str, str] | None = None   # the live reading counted into its Max and Min, when not `live` (wind: the gust, as its
                                             # Max is the highest gust: the speed now would stand in for it before a day row has one)

    @property
    def reading(self) -> str:
        """The service's name of the reading it shows (lib/service/names.py): its live reading's, the rain gauge's "daily" being
        rainfall.rain."""
        group, field = self.live
        return f"{group}.{'rain' if field == 'daily' else field}"


GAUGES = (
    Gauge("Temperature", "°C", "thermometer", 0, 40, COLD_TO_HOT, DEGREES, ("outdoor", "temperature"), "temp_max", "temp_min"),
    Gauge("Humidity", "%", "dial", 0, 100, WATER, (0, 20, 40, 60, 80, 100), ("outdoor", "humidity"), "humidity_max", "humidity_min", 0),
    Gauge("Atmospheric pressure", "hPa", "dial", 950, 1050, PRESSURE, (950, 970, 990, 1010, 1030, 1050), ("pressure", "relative"),
          "pressure_max", "pressure_min"),
    Gauge("Dew point", "°C", "thermometer", 0, 40, COLD_TO_HOT, DEGREES, ("outdoor", "dew_point"), "dew_point_max", "dew_point_min"),
    Gauge("Feels like", "°C", "thermometer", 0, 40, COLD_TO_HOT, DEGREES, ("outdoor", "feels_like"), "feels_like_max", "feels_like_min"),
    Gauge("Wind", "km/h", "dial", 0, 100, WIND, (0, 20, 40, 60, 80, 100), ("wind", "wind_speed"), "wind_gust", None, 0,
          counted=("wind", "wind_gust")),
    Gauge("Rain", "mm", "beaker", 0, 100, WATER, (100, 80, 60, 40, 20, 0), ("rainfall", "daily"), "rain", None, 1, True),
    Gauge("Rain rate", "mm/h", "dial", 0, 50, WATER, (0, 10, 20, 30, 40, 50), ("rainfall", "rain_rate"), "rain_rate_max"),
    Gauge("Solar radiation", "W/m²", "dial", 0, 1500, SUN, (0, 300, 600, 900, 1200, 1500), ("solar_and_uvi", "solar"), "solar_max", None, 0),
    Gauge("UV index", "", "dial", 0, 12, UV, (0, 3, 6, 8, 11), ("solar_and_uvi", "uvi"), "uv_max"),
    Gauge("Indoor temperature", "°C", "thermometer", 0, 40, COLD_TO_HOT, DEGREES, ("indoor", "temperature"), "indoor_temp_max", "indoor_temp_min"),
    Gauge("Indoor humidity", "%", "dial", 0, 100, WATER, (0, 20, 40, 60, 80, 100), ("indoor", "humidity"), "indoor_humidity_max",
          "indoor_humidity_min", 0),
)
PERIODS = ("Daily", "Monthly", "Yearly")
STATUS = ("Temperature", "Humidity", "Rain")   # the gauges the report ("status") carries


def scale(gauge: Gauge, value: float | None) -> tuple[float, float, tuple[str, ...], tuple[float, ...]]:
    """(low end, high end, the bands' colours from the low end, the numbers) of a gauge's scale for this reading. A thermometer's is
    0 to 40 °C, widened by 10 degrees at a time to take in a reading outside it (-3 °C: -10 to 40), its bands coloured by their
    degrees; every other gauge's is fixed."""
    if gauge.kind != "thermometer":
        return gauge.low, gauge.high, gauge.colours, gauge.ticks
    low, high, step = THERMOMETER
    if value is not None:
        low = min(low, math.floor(value / step) * step)
        high = max(high, math.ceil(value / step) * step)
    bands = range(int(low), int(high), step)
    return low, high, tuple(degree_colour(b) for b in bands), tuple(range(int(high), int(low) - 1, -step))


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


def figures(gauge: Gauge, values: dict, days: list[date], now: float | None) -> tuple[float | None, ...]:
    """A row of a gauge's table: Max, then Min or Total, over these days (the live reading counted in for today's highs and lows)."""
    highs = [values[gauge.highest][d] for d in days if d in values.get(gauge.highest, {})]
    if gauge.total:
        return (max(highs) if len(days) > 1 and highs else None, round(sum(highs), 1) if highs else None)
    highs += [now] if now is not None else []
    row = [max(highs) if highs else None]
    if gauge.lowest:
        lows = [values[gauge.lowest][d] for d in days if d in values.get(gauge.lowest, {})] + ([now] if now is not None else [])
        row.append(min(lows) if lows else None)
    return tuple(row)
