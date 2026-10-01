"""One emoji per current reading so hot, cold, wet, windy and sunny show at a glance. Decided here, from the values, so the same
reading always gets the same emoji; the model just copies it next to the reading."""

# (lowest value that gets it, emoji), highest first; a comfortable range has none, so an emoji is something to notice
TEMPERATURE = ((35, "🔥"), (30, "🥵"), (25, "🏖️"), (16, ""), (8, "🧥"), (0, "🥶"), (float("-inf"), "🧊"))       # outdoors
INDOOR_TEMPERATURE = ((35, "🔥"), (28, "🥵"), (25, "🏖️"), (20, ""), (16, "🧥"), (10, "🥶"), (float("-inf"), "🧊"))  # indoors
WIND = ((50, "🌪️"), (30, "💨"), (15, "🍃"))       # km/h; lighter than that gets none
HUMIDITY_HIGH, HUMIDITY_LOW = 85, 30              # % : muggy or dry; in between gets none
PRESSURE_HIGH = 1025                              # hPa, sea level: a strong high. VPD_HIGH: kPa, the air is drying things fast
VPD_HIGH = 1.2
SOLAR_HIGH = 600                                  # W/m2: bright sun (full sun is about 1000)
UVI = ((9, "🧴"), (6, "😎"))                      # UV index: 6 is 'high'; 9 and above is also an alert
UVI_ALERT = 9


def _step(value: float, table: tuple) -> str:
    return next((emoji for floor, emoji in table if value >= floor), "")


def glance(group: str, field: str, value: float) -> str:
    """The emoji for one reading, or "" when it is unremarkable."""
    name = field.lower()
    if name in ("temperature", "temp") or name.startswith(("feels_like", "app_temp")):
        return _step(value, INDOOR_TEMPERATURE if group == "indoor" else TEMPERATURE)
    if name.startswith("humidity"):
        return "💦" if value >= HUMIDITY_HIGH else "🏜️" if value <= HUMIDITY_LOW else ""
    if name in ("wind_speed", "wind_gust") or name.endswith("wind_speed"):
        return _step(value, WIND)
    if name == "rain_rate":
        return "🌧️" if value > 0 else ""
    if name in ("daily", "rain_daily", "rain_today"):
        return "☔" if value > 0 else ""
    if name == "solar":
        return "☀️" if value >= SOLAR_HIGH else ""
    if name == "uvi":
        return _step(value, UVI)
    if group == "pressure" and name == "relative":
        return "🗜️" if value >= PRESSURE_HIGH else ""
    if name == "vpd":
        return "🧽" if value >= VPD_HIGH else ""
    return ""
