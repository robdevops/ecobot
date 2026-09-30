"""The weather readings a chart can plot, in one place: what each is called, where it lives in Ecowitt's data and the
words that name it. Adding a reading here makes it plottable, chartable side by side with others, and recognised in
a question."""

from typing import NamedTuple

TEMP_WORDS = r"temp\w*|hot\w*|cold\w*|warm\w*|cool\w*|heat\w*|freez\w*|degrees?|celsius"
PRESSURE_WORDS = r"pressure|barometer|barometric"


class Reading(NamedTuple):
    group: str      # Ecowitt group
    field: str      # the field a chart draws (wind: the gusts, whose line is the speed shaded up to them)
    label: str
    unit: str
    words: str      # a pattern for the words that name it in a question


# A rain chart puts the rain behind the first of these readings that is on it (rain tracks humidity and pressure more than
# temperature), else behind the first line.
RAIN_WITH = ("humidity", "pressure")

WEATHER = {
    "temperature": Reading("outdoor", "temperature", "Temperature", "°C", TEMP_WORDS),
    "humidity": Reading("outdoor", "humidity", "Humidity", "%", r"humid\w*"),
    "pressure": Reading("pressure", "relative", "Pressure", "hPa", PRESSURE_WORDS),
    "wind": Reading("wind", "wind_gust", "Wind", "km/h", r"gusts?|wind\w*"),
    "rain": Reading("rainfall", "daily", "Rain", "mm", r"rain\w*|precip\w*"),
    "dew_point": Reading("outdoor", "dew_point", "Dew point", "°C", r"dew\w*"),
    "feels_like": Reading("outdoor", "feels_like", "Feels like", "°C", r"feels?[ -]?like|apparent"),
    "vpd": Reading("outdoor", "vpd", "VPD", "kPa", r"vpd|vapou?r pressure deficit"),
}
SPECIFIC = ("dew_point", "feels_like", "vpd")   # kinds of temperature and humidity, named on their own: they outrank "temperature" in a question
