"""One emoji per current reading so hot, cold, wet and windy show at a glance. Decided here, from the values, so the same
reading always gets the same emoji; the model just copies it next to the reading."""

# (lowest value that gets it, emoji), highest first
TEMPERATURE = ((35, "🔥"), (30, "🥵"), (22, "😎"), (16, "🙂"), (8, "🧣"), (0, "🥶"), (float("-inf"), "🧊"))       # outdoors: a scarf when it's cold out
INDOOR_TEMPERATURE = ((35, "🔥"), (28, "🥵"), (24, "😎"), (20, "🙂"), (16, "🧥"), (10, "🥶"), (float("-inf"), "🧊"))  # indoors
WIND = ((50, "🌪️"), (30, "💨"), (15, "🍃"))       # km/h; lighter than that gets none
HUMIDITY_HIGH, HUMIDITY_LOW = 85, 30              # % : muggy or dry; in between gets none


def _step(value: float, table: tuple) -> str:
    return next((emoji for floor, emoji in table if value >= floor), "")


def glance(group: str, field: str, value: float, kilometres_per_hour: bool = True) -> str:
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
    return ""
