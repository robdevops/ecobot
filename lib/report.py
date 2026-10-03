"""The report (and "weather now") written in code from the tools' own results, with no model: the layout, units and emoji are all
decided here, so it is instant and always the same. Each tool result is JSON; one that failed or is missing is left out."""

import json
import re
from datetime import datetime

from .ecowitt.glance import solar_band
from .series import WEATHER, find

AIR_ROWS = (("pm2_5", "PM2.5"), ("pm10", "PM10"), ("pm1", "PM1"), ("co2", "CO₂"), ("voc_index", "VOC index"), ("nox_index", "NOx index"))
OUTLOOK_NOTE = re.compile(r"\s*\(an estimate from the station's readings, not an official forecast\)", re.I)


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


def _band(solar: str | None) -> str:
    """" (medium)" for a solar reading like "40.2 W/m²"; nothing when it can't be read."""
    try:
        return f" ({solar_band(float(solar.split()[0]))})"
    except (AttributeError, IndexError, ValueError):
        return ""


def _join(parts: list[str | None], sep: str = ", ") -> str:
    return sep.join(p for p in parts if p)


def _heading(name: str, result: dict) -> str:
    """"Forecast (Melbourne)": the place the result names, if it does."""
    return f"{name} ({result['place']})" if result.get("place") else name


def _getters(now: dict):
    """(get, tag) for a weather_now result: a reading's text, and the same with its emoji (if it has one) before it."""
    emoji = now.get("emoji") or {}

    def get(group: str, field: str) -> str | None:
        return _units((now.get(group) or {}).get(field))

    def tag(group: str, field: str, text: str | None = None) -> str | None:
        text = text or get(group, field)
        return f"{emoji[f'{group}.{field}']} {text}" if text and f"{group}.{field}" in emoji else text
    return get, tag


def weather_lines(now: dict) -> list[str]:
    """The weather station's bullets from a weather_now result: an emoji sits right before the reading it is keyed to."""
    get, tag = _getters(now)
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
    sun = _join([None if _zero(solar) else tag("solar_and_uvi", "solar", f"solar radiation {solar}{_band(solar)}"),       # night: no sun line
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


def air_lines(air: dict, only: list[str] | None = None) -> list[str]:
    out = []
    for key, label in AIR_ROWS:
        entry = air.get(key) if only is None or key in only else None
        if not isinstance(entry, dict) or entry.get("value") is None:
            continue
        unit = entry.get("unit", "")
        shown = (f"{entry['value']:.1f} {unit}" if unit.startswith("µg") else f"{entry['value']:.0f} {unit}" if unit == "ppm"
                 else f"{entry['value']:.1f}" if key == "nox_index" else f"{entry['value']:.0f}")
        aqi = f" (AQI {entry['aqi_us']})" if key == "pm2_5" and "aqi_us" in entry else ""
        out.append(f"• {label}: {_join([shown, entry.get('rating')], ' ')}{aqi}")
    return out


def reading_lines(now: dict, names: list[str], sides: list[str]) -> list[str]:
    """Just the readings asked about from a weather_now result ("how hot is it": the temperature, indoors and out)."""
    get, tag = _getters(now)
    lines = []
    for name in names:
        reading = WEATHER[name]
        if reading.group in ("outdoor", "indoor") and name in ("temperature", "humidity"):
            for side in sides:
                text = _join([tag(side, name), f"feels like {get(side, 'feels_like')}" if name == "temperature" and get(side, "feels_like") else None], ", ")
                lines.append(f"• {side.capitalize()}: {text}" if text else None)
        elif name == "wind":
            wind = (now.get("wind") or {})
            if _zero(get("wind", "wind_speed")) and _zero(get("wind", "wind_gust")):
                lines.append("• Wind: calm")
            else:
                degrees = wind.get("wind_direction")
                lines.append("• Wind: " + _join([_join([tag("wind", "wind_speed"), f"from {_units(degrees)}" if degrees else None], " "),
                                                  f"gust {tag('wind', 'wind_gust')}" if get("wind", "wind_gust") else None]))
        elif name == "rain":
            lines += [line for line in weather_lines(now) if line.startswith("• Rain today")] or [
                f"• Rain today: {get('rainfall', 'daily') or 'none'}"]
        else:
            text = tag(reading.group, reading.field)
            if text:
                lines.append(f"• {reading.label}: {text}")
    return [line for line in lines if line]


def lookup(kind: str, result: str | None, names: list[str], sides: list[str]) -> str | None:
    """A plain lookup written from its one tool result: "reading", "air", "pollen" or "forecast". None when the result can't be
    used (the model then takes the question)."""
    data = _load(result)
    if not data:
        return None
    if kind == "reading":
        lines = reading_lines(data, names, sides)
    elif kind == "air":
        lines = air_lines(data, [k for k in names] or None)
    elif kind == "extremes":   # the highs and lows (or averages) of a short period, each with its day and time
        series = data.get("series") or {}
        wanted = [f for n in names if find(n) for f in (find(n).field, find(n).band_field) if f] or ["temperature"]   # wind: speed and gust
        keys = [k for k in series if k.split(".", 1)[1] in wanted and "low" in series[k]]
        lines = []
        for k in keys:
            group, field = k.split(".", 1)
            label = ("Wind gust" if field == "wind_gust" else find(field).label if find(field) else field.replace("_", " ").capitalize()) + (
                f" ({group})" if sum(x.split(".", 1)[1] == field for x in keys) > 1 else "")
            lines.append(f"• {_range(label, series[k], 'average' in series[k])}")
        return "\n".join([_period(data), *lines]) if lines else None
    elif kind in ("pollen", "forecast"):
        lines = [f"• {line}" for line in data.get("lines", [])]
        return "\n".join([_heading("Pollen & asthma" if kind == "pollen" else "Forecast", data), *lines]) if lines else None
    else:
        return None
    return "\n".join(lines) or None


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
        sections.append([_heading("Pollen & asthma", pollen), *(f"• {line}" for line in pollen.get("lines", []))])
    if forecast := _load(results.get("weather_forecast")):
        sections.append([_heading("Forecast", forecast), *(f"• {line}" for line in forecast.get("lines", []))])
    return "\n\n".join("\n".join(section) for section in sections) or "No readings are available right now."


# ---------- chart captions ----------
def _year(text: str | None) -> str:
    """"Tue 29 Sep 2026" -> "Tue 29 Sep"."""
    return re.sub(r" 20\d\d\b", "", text or "")


def _period(data: dict) -> str:
    """"Tue 29 Sep 2026 - Tue 06 Oct 2026" -> "Tue 29 Sep – Tue 06 Oct 2026"."""
    return re.sub(r" 20\d\d\b(?= - )", "", data.get("period", "")).replace(" - ", " – ")


def _at(entry: dict, which: str) -> str:
    """"high 24.3 °C, Tue 29 Sep around 3pm" for one record of a series."""
    when = _year(f"{entry.get(f'{which}_date', '')} {entry.get(f'{which}_when', '')}".strip())
    return f"{entry[which]} {_units(entry.get('unit')) or ''}".strip() + (f", {when}" if when else "")


def _range(label: str, entry: dict, average: bool) -> str:
    unit = _units(entry.get("unit")) or ""
    if average and entry.get("average"):
        return f"{label}: average {entry['average']} {unit} (low {entry['low']}, high {entry['high']})".replace(" )", ")")
    return f"{label}: low {_at(entry, 'low')} · high {_at(entry, 'high')}"


def _rain_line(turn, tz) -> list[str]:
    """Least and most rain in a period (the chart's own bars), then whether more is expected."""
    bars = next((p.bars for c in turn.charts for p in c.panels if p.bars and p.bars.x), None)
    if not bars:
        return []
    def day(i: int) -> str:
        return datetime.fromtimestamp(bars.x[i], tz).strftime("%a %-d %b" + (" %-I%p" if bars.width < 86400 else "")).replace("AM", "am").replace("PM", "pm")
    lo, hi = min(range(len(bars.y)), key=bars.y.__getitem__), max(range(len(bars.y)), key=bars.y.__getitem__)
    per = f" per {bars.per}" if bars.per else ""
    return [f"☔ Rain{per}: least {bars.y[lo]:g} mm ({day(lo)}) · most {bars.y[hi]:g} mm ({day(hi)}) · {sum(bars.y):.1f} mm in all"]


def _weather_caption(result: dict, args: dict, turn, tz, now_result: dict | None) -> list[str]:
    series = result.get("series") or {}
    names = [n for n in dict.fromkeys(turn.chart_fields or [turn.chart_field or "temperature"]) if find(n)]
    lines: list[str] = []
    for name in names:
        reading = find(name)
        keys = [k for k in series if k.split(".", 1)[1] == reading.field]
        if reading.field == "daily":   # rain: the amounts the chart draws, and what is expected
            lines += _rain_line(turn, tz)
            if now_result is not None:
                outlook = OUTLOOK_NOTE.sub(" (an estimate)", now_result.get("rain_outlook") or "")
                lines.append("☔ " + (outlook or "No rain is expected soon.").lstrip("☔ "))
            continue
        for key in keys:
            group = key.split(".", 1)[0]
            label = reading.label + (f" ({group})" if len(keys) > 1 and group in ("outdoor", "indoor") else "")
            if reading.field == "wind_speed":
                gust = series.get(f"{group}.wind_gust") or series.get("wind.wind_gust")
                lines.append(f"🌬️ Wind: strongest gust {_at(gust, 'high')}" if gust else _range(label, series[key], bool(turn.average_asked)))
            else:
                lines.append(_range(label, series[key], bool(turn.average_asked)))
        if reading.field == "wind_speed" and (d := series.get("wind.wind_direction")):
            lines.append(f"🧭 Mostly from the {d['most_common']}" + (f", {d['steadiness']}" if d.get("steadiness") else ""))
    return lines


def _air_caption(result: dict, args: dict, turn) -> list[str]:
    lines = []
    for key, label in AIR_ROWS:
        entry = result.get(key)
        if key not in (args.get("metrics") or ["pm2_5"]) or not isinstance(entry, dict):
            continue
        unit = _units(entry.get("unit")) or ""
        rating = entry.get("high_rating")
        lines.append(f"• {label}: peak {entry['high']} {unit} ({_year(entry.get('high_time'))})".replace(" )", ")")
                     + (f" · {rating}" if rating else "") + f" · average {entry['average']}")
    return lines


def chart_caption(tool: str, args: dict, result: str | None, turn, tz, extra: str | None = None) -> str | None:
    """The caption for a chart, from the tool's result: the period, then a line per reading. None when there is nothing to say (the
    model then takes the question)."""
    data = _load(result)
    if not data or not turn.charts:
        return None
    lines = (_air_caption(data, args, turn) if tool == "air_quality" else
             _weather_caption(data, args, turn, tz, _load(extra) if extra is not None else None))
    if not lines:
        return None
    notes = (["(Older days are still downloading, so they are left out.)"] if data.get("note_missing") else
             ["(Some data couldn't be fetched, so this may be incomplete.)"] if data.get("warning") else [])
    return "\n".join([_period(data), *lines, *notes]).strip()
