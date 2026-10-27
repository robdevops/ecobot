"""The weather readings a chart can plot and the air-quality metrics, in one place: what each is called, where a reading lives
in Ecowitt's data, the words that name it, and whether its lowest is worth telling. Adding a reading here makes it plottable,
chartable side by side with others, and recognised in a question."""

from typing import NamedTuple

TEMP_WORDS = r"temp\w*|hot\w*|cold\w*|warm\w*|cool\w*|heat\w*|freez\w*|degrees?|celsius"
PRESSURE_WORDS = r"pressure|barometer|barometric"


class Reading(NamedTuple):
    group: str      # Ecowitt group
    field: str      # the field a chart draws (wind: the average speed)
    label: str
    unit: str
    words: str      # a pattern for the words that name it in a question
    band_field: str | None = None   # a field its line is shaded up to (wind: the gusts); it keeps that band at every width
    low: bool = True                # its lowest is worth telling: not when it is nothing every night, calm or dry spell


# A rain chart puts the rain behind the first of these readings that is on it (rain tracks humidity and pressure more than
# temperature), else behind the first line.
RAIN_WITH = ("humidity", "pressure")

WEATHER = {
    "temperature": Reading("outdoor", "temperature", "Temperature", "°C", TEMP_WORDS),
    "humidity": Reading("outdoor", "humidity", "Humidity", "%", r"humid\w*"),
    "pressure": Reading("pressure", "relative", "Pressure", "hPa", PRESSURE_WORDS),
    "wind": Reading("wind", "wind_speed", "Wind", "km/h", r"gusts?|wind\w*", "wind_gust", low=False),
    "rain": Reading("rainfall", "daily", "Rain", "mm", r"rain\w*|precip\w*", low=False),
    "dew_point": Reading("outdoor", "dew_point", "Dew point", "°C", r"dew\w*"),
    "feels_like": Reading("outdoor", "feels_like", "Feels like", "°C", r"feel\w*|apparent"),
    "vpd": Reading("outdoor", "vpd", "Vapour pressure deficit", "kPa", r"vpd|vapou?r pressure deficit"),
    "solar": Reading("solar_and_uvi", "solar", "Solar radiation", "W/m²", r"solar\w*|radiation|sunshine|sun(?!\s+\d)", low=False),
    "uv": Reading("solar_and_uvi", "uvi", "UV index", "", r"uvi?|ultraviolet", low=False),
}
SPECIFIC = ("dew_point", "feels_like", "vpd")   # kinds of temperature and humidity, named on their own: they outrank "temperature" in a question
INDOOR_READINGS = ("temperature", "humidity", "dew_point", "feels_like")   # what the indoor sensor measures as well
READING_GROUPS = tuple(dict.fromkeys(["outdoor", "indoor", *(r.group for r in WEATHER.values())]))   # the groups they are in, the indoor sensor's too




class Metric(NamedTuple):
    label: str
    unit: str                      # on a chart's axis ("" for an index)
    words: str                     # a pattern for the words that name it in a question
    limits: tuple[float, float]    # good up to the first, poor up to the second, very poor above
    low: bool = False              # its lowest is worth telling: the others are often 0 or at their baseline


# The air sensor's metrics, in the order a question's words are tried (PM10 before PM1). The ratings: the particles follow the
# US AQI (very poor = AQI 151+, the level of the mask alerts), PM1 has no standard so it uses PM2.5's, and CO2, VOC and NOx
# follow AirGradient's colour scales.
AIR = {
    "pm2_5": Metric("PM2.5", "µg/m³", r"pm ?2\.?5|pm25", (9.0, 55.4)),
    "pm10": Metric("PM10", "µg/m³", r"pm ?10", (54.0, 254.0)),
    "pm1": Metric("PM1", "µg/m³", r"pm ?1(?![0-9])", (9.0, 55.4)),
    "co2": Metric("CO₂", "ppm", r"co2|co₂", (799.0, 1499.0), low=True),   # an outdoor floor near 400 ppm: its lowest says something
    "voc_index": Metric("VOC index", "", r"voc\w*", (150.0, 250.0)),
    "nox_index": Metric("NOx index", "", r"nox", (20.0, 150.0)),
}


def find_name(name: str) -> str | None:
    """The name of a reading given its name ("uv", "pressure"), the field it plots ("uvi", "relative") or the field it is shaded
    up to ("wind_gust"); None if none of those."""
    return name if name in WEATHER else next((n for n, r in WEATHER.items() if name in (r.field, r.band_field)), None)


def find(name: str) -> Reading | None:
    """A reading by its name or by the field it plots."""
    return WEATHER[n] if (n := find_name(name)) else None


def field_of(name: str) -> str:
    """The field a chart of this reading plots ("uv" -> "uvi"); a name that is not a reading comes back as it is."""
    return r.field if (r := find(name)) else name


def derives_range(field: str) -> bool:
    """Is this a charted reading whose band can come from the cached 5-minute readings? Not wind, which has its own (up to the
    gusts), nor a field no chart plots (wind direction, rain rate ...)."""
    return any(r.field == field and not r.band_field for r in WEATHER.values())
