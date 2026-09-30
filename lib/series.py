"""The weather readings a chart can plot, in one place: what each is called, where it lives in Ecowitt's data and the
words that name it. Adding a reading here makes it plottable, chartable side by side with others, and recognised in
a question."""

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


# A rain chart puts the rain behind the first of these readings that is on it (rain tracks humidity and pressure more than
# temperature), else behind the first line.
RAIN_WITH = ("humidity", "pressure")

WEATHER = {
    "temperature": Reading("outdoor", "temperature", "Temperature", "°C", TEMP_WORDS),
    "humidity": Reading("outdoor", "humidity", "Humidity", "%", r"humid\w*"),
    "pressure": Reading("pressure", "relative", "Pressure", "hPa", PRESSURE_WORDS),
    "wind": Reading("wind", "wind_speed", "Wind", "km/h", r"gusts?|wind\w*", "wind_gust"),
    "rain": Reading("rainfall", "daily", "Rain", "mm", r"rain\w*|precip\w*"),
    "dew_point": Reading("outdoor", "dew_point", "Dew point", "°C", r"dew\w*"),
    "feels_like": Reading("outdoor", "feels_like", "Feels like", "°C", r"feel\w*|apparent"),
    "vpd": Reading("outdoor", "vpd", "VPD", "kPa", r"vpd|vapou?r pressure deficit"),
    "solar": Reading("solar_and_uvi", "solar", "Solar radiation", "W/m²", r"solar\w*|radiation|sunshine|sun(?!\s+\d)"),
    "uv": Reading("solar_and_uvi", "uvi", "UV index", "", r"uvi?|ultraviolet"),
}
SPECIFIC = ("dew_point", "feels_like", "vpd")   # kinds of temperature and humidity, named on their own: they outrank "temperature" in a question


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
