"""The report (and "weather now") written in code from the tools' own results, with no model: the layout, units and emoji are all
decided here, so it is instant and always the same. Each tool result is JSON; one that failed or is missing is left out."""

import json
import re

AIR_ROWS = (("pm2_5", "PM2.5"), ("pm10", "PM10"), ("pm1", "PM1"), ("co2", "CO₂"), ("voc_index", "VOC index"), ("nox_index", "NOx index"))
OUTLOOK_NOTE = re.compile(r"\s*\(an estimate from the station's readings, not an official forecast\)")


def _load(result: str | None) -> dict | None:
    """The tool's JSON, or None when it is missing, an error message or says it has nothing."""
    try:
        data = json.loads(result or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) and "error" not in data else None


def _units(text: str | None) -> str | None:
    """"12.3 ℃" -> "12.3 °C", "1.8 mm/hr" -> "1.8 mm/h"."""
    return text.replace("℃", "°C").replace(" º", "°").replace("º", "°").replace("mm/hr", "mm/h").strip() if text else None


def _zero(text: str | None) -> bool:
    """Is this reading missing or zero ("0.0 W/m²", "0")?"""
    try:
        return not text or float(text.split()[0]) == 0
    except ValueError:
        return False


def _join(parts: list[str | None], sep: str = ", ") -> str:
    return sep.join(p for p in parts if p)


def weather_lines(now: dict) -> list[str]:
    """The weather station's bullets from a weather_now result: an emoji sits right before the reading it is keyed to."""
    emoji = now.get("emoji") or {}

    def get(group: str, field: str) -> str | None:
        return _units((now.get(group) or {}).get(field))

    def tag(group: str, field: str, text: str | None = None) -> str | None:
        text = text or get(group, field)
        return f"{emoji[f'{group}.{field}']} {text}" if text and f"{group}.{field}" in emoji else text

    rain_rate = (now.get("rainfall") or {}).get("rain_rate", "")
    outlook = OUTLOOK_NOTE.sub(" (an estimate)", now["rain_outlook"]) if now.get("rain_outlook") else None
    if outlook and outlook.startswith("raining"):
        outlook = tag("rainfall", "rain_rate", outlook)
    elif not outlook and rain_rate.split()[:1] not in ([], ["0"], ["0.0"]):
        outlook = tag("rainfall", "rain_rate", f"raining ({get('rainfall', 'rain_rate')})")
    month = get("rainfall", "monthly")
    rain = _join([_join([tag("rainfall", "daily"), f"(month total {month})" if month else None], " "), outlook], "; ")

    degrees = (now.get("wind") or {}).get("wind_direction", "")
    calm = _zero(get("wind", "wind_speed")) and _zero(get("wind", "wind_gust"))    # no wind: no line
    wind = None if calm else _join([_join([tag("wind", "wind_speed"), f"from {_units(degrees)}" if degrees else None], " "),
                                    f"gust {tag('wind', 'wind_gust')}" if get("wind", "wind_gust") else None])
    solar, uvi = get("solar_and_uvi", "solar"), get("solar_and_uvi", "uvi")
    sun = _join([None if _zero(solar) else tag("solar_and_uvi", "solar", f"solar radiation {solar}"),       # night: no sun line
                 None if _zero(uvi) else tag("solar_and_uvi", "uvi", f"UV index {uvi}")])
    bullets = [("Outdoor", _join([tag("outdoor", "temperature"), tag("outdoor", "humidity"),
                                  f"dew point {get('outdoor', 'dew_point')}" if get("outdoor", "dew_point") else None,
                                  tag("outdoor", "vpd", f"VPD {get('outdoor', 'vpd')}" if get("outdoor", "vpd") else None)])),
               ("Indoor", _join([tag("indoor", "temperature"), tag("indoor", "humidity")])),
               ("Pressure", tag("pressure", "relative")),
               ("Rain today", rain),
               ("Sun", sun),
               ("Wind", wind)]
    return [f"• {name}: {text}" for name, text in bullets if text]


def air_lines(air: dict) -> list[str]:
    out = []
    for key, label in AIR_ROWS:
        entry = air.get(key)
        if not isinstance(entry, dict) or entry.get("value") is None:
            continue
        unit = entry.get("unit", "")
        shown = (f"{entry['value']:.1f} {unit}" if unit.startswith("µg") else f"{entry['value']:.0f} {unit}" if unit == "ppm"
                 else f"{entry['value']:.1f}" if key == "nox_index" else f"{entry['value']:.0f}")
        aqi = f" (AQI {entry['aqi_us']})" if key == "pm2_5" and "aqi_us" in entry else ""
        out.append(f"• {label}: {_join([shown, entry.get('rating')], ' ')}{aqi}")
    return out


def weather_now(result: str | None) -> str | None:
    """"Weather now": the weather station's bullets alone (None if the reading isn't available)."""
    now = _load(result)
    return "\n".join(weather_lines(now)) if now else None


def report(results: dict[str, str]) -> str:
    """The full report from the tool results by tool name (weather_now, air_quality, pollen_asthma, weather_forecast)."""
    sections = []
    if "weather_now" in results:
        now = _load(results["weather_now"])
        sections.append(["Weather station", *(weather_lines(now) if now else ["• not available right now"])])
    if "air_quality" in results:
        air = _load(results["air_quality"])
        sections.append(["Air quality", *(air_lines(air) if air else ["• not available right now"])])
    if pollen := _load(results.get("pollen_asthma")):
        sections.append(["Pollen & asthma", *(f"• {line}" for line in pollen.get("lines", []))])
    if forecast := _load(results.get("weather_forecast")):
        sections.append([f"Forecast [{forecast.get('tag', '')}]".replace(" []", ""), *(f"• {line}" for line in forecast.get("lines", []))])
    return "\n\n".join("\n".join(section) for section in sections) or "No readings are available right now."
