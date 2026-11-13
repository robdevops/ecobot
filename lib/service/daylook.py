"""A day's look: one word and an emoji for that day's outdoor weather, worked out from its figures (docs/ecobot-mcp.md 3.2, "Answers
ready to pass on"), so a model quotes `day_look: {word: "wet", emoji: "🌧️"}` beside a day and never guesses it.

The rule takes the first that holds:
  1. stormy ⛈️      heavy rain and strong gusts together: rain >= STORMY_RAIN_MM and a gust >= STORMY_GUST_KMH (the station has no
                    lightning sensor, so a storm is told by what it does);
  2. wet 🌧️        rain >= WET_MM;
  3. showery 🌦️    rain >= RAINY_DAY_MM, the rainy day of the days tool and the bot's prompt (rain >= 1): every day counted as rainy
                    looks rainy, and a trace (under 1 mm, often dew or drizzle) doesn't;
  4. windy 💨       a gust >= WINDY_GUST_KMH;
  5. very hot 🥵    the high >= VERY_HOT_C;
  6. very cold 🥶   the high under VERY_COLD_HIGH_C, or a frost: the low at or below FROST_C;
  7. by how sunny it was: the day's mean solar radiation over a clear sky's for that date and latitude (clear_sky_mean): sunny ☀️ from
     SUNNY, mostly sunny 🌤️ from MOSTLY_SUNNY, partly cloudy ⛅ from PARTLY_CLOUDY, else overcast ☁️.
A step whose figure is missing is passed over. With no solar radiation (or no latitude), step 7 gives nothing, so a day that none of
1 to 6 fits has no look: None, and the day goes without an emoji rather than with a guess. Fog 🌫️, which the prompt also lists, can't
be told from these figures, so it is never given.

The thresholds are for a temperate climate such as Melbourne's: there, 35 °C is a day people count, a high under 10 °C one of the
coldest days of a winter, gusts of 50 km/h a day people call windy (the gust alert is at 40), and 10 mm in a day heavy rain.
"""

import math
from collections.abc import Mapping
from datetime import date

STORMY_RAIN_MM = 10.0     # heavy rain, with ...
STORMY_GUST_KMH = 60.0    # ... strong gusts: a storm
WET_MM = 5.0              # more than showers
RAINY_DAY_MM = 1.0        # a rainy day (daytable and the prompt: rain >= 1); below it a trace
WINDY_GUST_KMH = 50.0
VERY_HOT_C = 35.0         # the high
VERY_COLD_HIGH_C = 10.0   # the high stays under it
FROST_C = 0.0             # or the low falls to it (on the ground it is colder still)
SUNNY = 0.80              # the day's solar radiation as a share of a clear sky's: the sensor and the model each err by up to about a
MOSTLY_SUNNY = 0.60       # tenth, so sunny starts below 1; an overcast day lets through a fifth to a third of a clear sky's
PARTLY_CLOUDY = 0.35

# The look of a day: its word and its emoji, from the prompt's WEATHER EMOJIS
LOOKS = {"stormy": "⛈️", "wet": "🌧️", "showery": "🌦️", "windy": "💨", "very hot": "🥵", "very cold": "🥶",
         "sunny": "☀️", "mostly sunny": "🌤️", "partly cloudy": "⛅", "overcast": "☁️"}
FIGURES = ("rain", "wind_gust", "temp_max", "temp_min", "solar_avg")   # what day_look reads: the days tool's names for them

# The clear sky (Haurwitz, 1945): on a cloudless day the sun gives a level surface 1098 cos(z) e^(-0.057 / cos(z)) W/m², z the sun's
# angle from overhead, which depends on the latitude, the sun's declination that day (Cooper, 1969) and the hour. It needs nothing but
# the date and the latitude, and over a day comes within about a tenth of what a cloudless sky gives where the air is clean.
HAURWITZ_W, HAURWITZ_K = 1098.0, 0.057
STEPS = 288   # the day's 24 hours in 5-minute steps of the sun's hour angle


def clear_sky_mean(day: date, latitude: float) -> float:
    """The mean solar radiation of a cloudless `day` at this latitude (degrees, south negative) over its 24 hours, W/m²: what a day's
    mean solar radiation (its `solar_avg`) is set against. 0 when the sun never rises."""
    declination = math.radians(23.45) * math.sin(2 * math.pi * (284 + day.timetuple().tm_yday) / 365)
    lat = math.radians(latitude)
    a, b = math.sin(lat) * math.sin(declination), math.cos(lat) * math.cos(declination)
    total = 0.0
    for k in range(STEPS):
        up = a + b * math.cos(math.pi * (2 * (k + 0.5) / STEPS - 1))   # the cosine of the sun's angle from overhead
        if up > 0:
            total += HAURWITZ_W * up * math.exp(-HAURWITZ_K / up)
    return total / STEPS


def day_look(figures: Mapping, *, latitude: float | None, day: date, so_far: bool = False) -> dict | None:
    """{"word": "wet", "emoji": "🌧️"} for a day from its figures, by the rule above, or None when none of it can be told. `figures` are
    the day's, named as the days tool names them: rain (mm that fell), wind_gust (its highest, km/h), temp_max and temp_min (outdoor,
    °C), solar_avg (its mean solar radiation over the whole day, W/m²); a missing one, or None, passes its steps over. `latitude`: the
    station's. `so_far`: the day isn't over (today): its figures run to now, so its solar mean is no whole day's and the cloud step is
    passed over."""
    rain, gust, high, low, solar = (_number(figures.get(name)) for name in FIGURES)
    word = ("stormy" if rain is not None and gust is not None and rain >= STORMY_RAIN_MM and gust >= STORMY_GUST_KMH else
            "wet" if rain is not None and rain >= WET_MM else
            "showery" if rain is not None and rain >= RAINY_DAY_MM else
            "windy" if gust is not None and gust >= WINDY_GUST_KMH else
            "very hot" if high is not None and high >= VERY_HOT_C else
            "very cold" if (high is not None and high < VERY_COLD_HIGH_C) or (low is not None and low <= FROST_C) else
            None if so_far else _sky(solar, latitude, day))
    return {"word": word, "emoji": LOOKS[word]} if word else None


def _sky(solar: float | None, latitude: float | None, day: date) -> str | None:
    """How sunny the day was, from its mean solar radiation against a clear sky's."""
    if solar is None or latitude is None or (clear := clear_sky_mean(day, latitude)) <= 0:
        return None
    share = solar / clear
    return "sunny" if share >= SUNNY else "mostly sunny" if share >= MOSTLY_SUNNY else "partly cloudy" if share >= PARTLY_CLOUDY else \
        "overcast"


def _number(value) -> float | None:
    """A figure as a number (one written as text, "12.3", too); None for none."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


__all__ = ["FIGURES", "LOOKS", "clear_sky_mean", "day_look"]
