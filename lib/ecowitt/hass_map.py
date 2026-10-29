"""Home Assistant's Ecowitt entities as the bot's series, and Home Assistant's numbers in the bot's units.

The Ecowitt integration names its sensors sensor.<station>_<what>: sensor.ws2900_outdoor_temperature, sensor.ws2900_relative_pressure ...
Each is one of the bot's series ("group.field", as Ecowitt's cloud names them), written in the unit the cloud writes it, so everything
downstream (the cache, the charts, the report) sees the same thing whichever one the readings came from. Home Assistant shows a sensor in
the unit its owner chose (°F, mph, inHg ...); `convert` brings that to the bot's (°C, km/h, hPa, mm).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Entity:
    series: str            # "group.field"
    suffix: str            # the entity's name after sensor.<station>_
    unit: str              # the unit as the cloud writes it
    mode: str              # how a stretch of readings is told as one: "mean" (over time), "max" (gusts), "last" (a counter, a compass point)
    kind: str = ""         # what it measures, when Home Assistant's own unit may need converting: temperature, pressure, speed, length, rate
    scale: float = 1.0     # then multiplied by this (the vapour pressure deficit is kPa here and hPa in Home Assistant)
    decimals: int = 1      # how many a reading is written with

    @property
    def group(self) -> str:
        return self.series.split(".", 1)[0]

    @property
    def field(self) -> str:
        return self.series.split(".", 1)[1]


def _entities(*rows) -> dict[str, Entity]:
    return {e.series: e for e in (Entity(*row) for row in rows)}


# series, suffix, unit, mode, kind, scale[, decimals]
ENTITIES = _entities(
    ("outdoor.temperature", "outdoor_temperature", "℃", "mean", "temperature", 1.0),
    ("outdoor.humidity", "humidity", "%", "mean", "", 1.0, 0),
    ("outdoor.dew_point", "dewpoint", "℃", "mean", "temperature", 1.0),
    ("outdoor.feels_like", "feels_like_temperature", "℃", "mean", "temperature", 1.0),
    ("outdoor.vpd", "vapour_pressure_deficit", "kPa", "mean", "pressure", 0.1, 3),
    ("indoor.temperature", "indoor_temperature", "℃", "mean", "temperature", 1.0),
    ("indoor.humidity", "indoor_humidity", "%", "mean", "", 1.0, 0),
    ("indoor.dew_point", "indoor_dewpoint", "℃", "mean", "temperature", 1.0),
    ("pressure.relative", "relative_pressure", "hPa", "mean", "pressure", 1.0),
    ("pressure.absolute", "absolute_pressure", "hPa", "mean", "pressure", 1.0),
    ("wind.wind_speed", "wind_speed", "km/h", "mean", "speed", 1.0),
    ("wind.wind_gust", "wind_gust", "km/h", "max", "speed", 1.0),
    ("wind.wind_direction", "wind_direction", "º", "last", "", 1.0, 0),
    ("rainfall.rain_rate", "rain_rate", "mm/hr", "mean", "rate", 1.0),
    ("rainfall.daily", "daily_rain", "mm", "last", "length", 1.0),
    ("rainfall.event", "event_rain", "mm", "last", "length", 1.0),
    ("rainfall.1_hour", "hourly_rain", "mm", "last", "length", 1.0),
    ("rainfall.weekly", "weekly_rain", "mm", "last", "length", 1.0),
    ("rainfall.monthly", "monthly_rain", "mm", "last", "length", 1.0),
    ("rainfall.yearly", "yearly_rain", "mm", "last", "length", 1.0),
    ("solar_and_uvi.solar", "solar_radiation", "W/m²", "mean", "", 1.0),
    ("solar_and_uvi.uvi", "uv_index", "", "mean", "", 1.0),
)
ANCHOR = ENTITIES["outdoor.temperature"].suffix   # the entity the station's name is read from
NO_RANGE = ("wind.wind_direction",)               # a compass point has no lowest or highest


# Home Assistant's units -> the bot's, per kind: a factor, or for the temperature a pair of (offset, factor): (value + offset) * factor
FACTORS = {
    "pressure": {"hPa": 1.0, "mbar": 1.0, "mb": 1.0, "kPa": 10.0, "Pa": 0.01, "inHg": 33.8639, "mmHg": 1.33322, "psi": 68.9476},
    "speed": {"km/h": 1.0, "mph": 1.609344, "m/s": 3.6, "kn": 1.852, "ft/s": 1.09728},
    "length": {"mm": 1.0, "cm": 10.0, "in": 25.4},
    "rate": {"mm/h": 1.0, "mm/hr": 1.0, "in/h": 25.4, "in/hr": 25.4},
}
TEMPERATURE = {"°C": (0.0, 1.0), "℃": (0.0, 1.0), "°F": (-32.0, 5 / 9), "K": (-273.15, 1.0)}


def convert(value: float, kind: str, unit: str | None, scale: float = 1.0) -> float:
    """`value` in Home Assistant's `unit` as the bot's unit for this kind of reading (then times `scale`). A unit it does not know, or
    a reading with no kind, is taken to be the bot's already."""
    if kind == "temperature" and unit in TEMPERATURE:
        offset, factor = TEMPERATURE[unit]
        value = (value + offset) * factor
    elif kind in FACTORS and unit in FACTORS[kind]:
        value *= FACTORS[kind][unit]
    return value * scale


def written(value: float, decimals: int) -> str:
    """A reading as the cloud writes one ("12.3", "94"): a zero is never written with a minus."""
    text = f"{value:.{decimals}f}"
    return text.lstrip("-") if float(text) == 0 else text


def entity_ids(prefix: str, series: list[str] | None = None) -> dict[str, Entity]:
    """{entity id: Entity} for these series (all of them when None) under the station's name `prefix` ("sensor.ws2900_")."""
    return {prefix + e.suffix: e for s, e in ENTITIES.items() if series is None or s in series}
