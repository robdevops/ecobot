"""The report (and "weather now") written in code from the tools' own results, with no model: the layout, units and emoji are all
decided here, so it is instant and always the same. Each tool result is JSON; one that failed or is missing is left out."""

import json
import re
from datetime import date, datetime

from telegram import MessageEntity

from .ecowitt.api import unit_text
from .ecowitt.glance import solar_band
from .entities import utf16_len
from .series import AIR, INDOOR_READINGS, WEATHER, find
from .timeutil import clock_text, day_text, to_local


def _load(result: str | None) -> dict | None:
    """The tool's JSON, or None when it is missing, an error message or says it has nothing."""
    try:
        data = json.loads(result or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) and "error" not in data else None


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


def _stamp(result: dict, today: date) -> str | None:
    """The time of the data, "11:45pm", with its day when that isn't today ("Sat 10 Oct 11:45pm"), from the result's "time" (as
    timeutil.when writes it, "Sun 11 Oct 2026 11:45pm"); None when it is missing or unreadable."""
    try:
        at = datetime.strptime(result.get("time") or "", "%a %d %b %Y %I:%M%p")
    except ValueError:
        return None
    return clock_text(at) if at.date() == today else f"{day_text(at, year=False)} {clock_text(at)}"


def _heading(name: str, result: dict, today: date) -> str:
    """"Forecast (Melbourne, 6:00pm)": the place the result names and the time of its data, whichever it has."""
    note = ", ".join(x for x in (result.get("place"), _stamp(result, today)) if x)
    return f"{name} ({note})" if note else name


def _getters(now: dict):
    """(get, tag) for a weather_now result: a reading's text, and the same with its emoji (if it has one) before it."""
    emoji = now.get("emoji") or {}

    def get(group: str, field: str) -> str | None:
        return unit_text((now.get(group) or {}).get(field))

    def tag(group: str, field: str, text: str | None = None) -> str | None:
        text = text or get(group, field)
        return f"{emoji[f'{group}.{field}']} {text}" if text and f"{group}.{field}" in emoji else text
    return get, tag


def _rain(now: dict) -> str:
    """Today's rain, the month's and, while it rains, the rate: "☔ 6.3 mm (month total 49.8 mm); 🌧️ raining (1.8 mm/h)"."""
    get, tag = _getters(now)
    rate, month = get("rainfall", "rain_rate"), get("rainfall", "monthly")
    return _join([_join([tag("rainfall", "daily"), f"(month total {month})" if month else None], " "),
                  None if _zero(rate) else tag("rainfall", "rain_rate", f"raining ({rate})")], "; ")


def _wind(now: dict) -> str | None:
    """The wind, where it comes from and its gusts: "14.0 km/h from 131°, gust 31.0 km/h"; None when it is calm."""
    get, tag = _getters(now)
    if _zero(get("wind", "wind_speed")) and _zero(get("wind", "wind_gust")):
        return None
    direction, gust = get("wind", "wind_direction"), tag("wind", "wind_gust")
    return _join([_join([tag("wind", "wind_speed"), f"from {direction}" if direction else None], " "), f"gust {gust}" if gust else None])


def weather_lines(now: dict) -> list[str]:
    """The weather station's bullets from a weather_now result: an emoji sits right before the reading it is keyed to."""
    get, tag = _getters(now)
    solar, uvi = get("solar_and_uvi", "solar"), get("solar_and_uvi", "uvi")
    sun = _join([None if _zero(solar) else tag("solar_and_uvi", "solar", f"solar radiation {solar}{_band(solar)}"),       # night: no sun line
                 None if _zero(uvi) else tag("solar_and_uvi", "uvi", f"UV index {uvi}")])
    bullets = [("Outdoor", _join([tag("outdoor", "temperature"), tag("outdoor", "humidity"),
                                  f"dew point {get('outdoor', 'dew_point')}" if get("outdoor", "dew_point") else None,
                                  tag("outdoor", "vpd", f"VPD {get('outdoor', 'vpd')}" if get("outdoor", "vpd") else None)])),
               ("Indoor", _join([tag("indoor", "temperature"), tag("indoor", "humidity")])),
               ("Pressure", tag("pressure", "relative")),
               ("Rain today", _rain(now)),
               ("Sun", sun),
               ("Wind", _wind(now))]    # calm: no line
    return [f"• {name}: {text}" for name, text in bullets if text]


def air_lines(air: dict, only: list[str] | None = None) -> list[str]:
    out = []
    for key, label in ((name, m.label) for name, m in AIR.items()):
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
        if name in ("temperature", "humidity"):   # a line for each side asked about; the temperature with what it feels like
            for side in sides:
                feels = get(side, "feels_like") if name == "temperature" else None
                if text := _join([tag(side, reading.field), f"feels like {feels}" if feels else None]):
                    lines.append(f"• {side.capitalize()}: {text}")
        elif name == "wind":
            lines.append(f"• Wind: {_wind(now) or 'calm'}")
        elif name == "rain":
            lines.append(f"• Rain today: {_rain(now) or 'none'}")
        else:
            group = sides[0] if name in INDOOR_READINGS and len(sides) == 1 else reading.group   # "indoor dew point": that sensor's
            if text := tag(group, reading.field):
                lines.append(f"• {reading.label}{' (indoor)' if group == 'indoor' else ''}: {text}")
    return lines


def _own_lines(result: dict) -> list[str]:
    """The bullets of a result that brings its own lines (pollen and asthma, the forecast)."""
    return [f"• {line}" for line in result.get("lines", [])]


# The report's sections in order: the tool, the section's heading, its bullets from the tool's result, and whether it is still
# shown when that result failed ("• not available right now") rather than left out
SECTIONS = (("weather_now", "Weather station", weather_lines, True),
            ("air_quality", "Air quality", air_lines, True),
            ("pollen_asthma", "Pollen & asthma", _own_lines, False),
            ("weather_forecast", "Forecast", _own_lines, False))
HEADINGS = {tool: heading for tool, heading, _, _ in SECTIONS}   # the pollen and forecast lookups start with theirs too
HEADING_LINE = re.compile(rf"^({'|'.join(map(re.escape, HEADINGS.values()))})(?= \(|$)", re.M)


def heading_entities(text: str) -> list[MessageEntity]:
    """Bold entities over the name of each heading line in `text` (not over its brackets)."""
    return [MessageEntity(MessageEntity.BOLD, utf16_len(text[:m.start()]), utf16_len(m.group(1))) for m in HEADING_LINE.finditer(text)]


def report(results: dict[str, str], today: date) -> str:
    """The full report from the tool results by tool name (weather_now, air_quality, pollen_asthma, weather_forecast), on `today` (local)."""
    sections = []
    for tool, heading, bullets, always in SECTIONS:
        data = _load(results.get(tool))
        if data or (always and tool in results):
            sections.append("\n".join([_heading(heading, data or {}, today), *(bullets(data) if data else ["• not available right now"])]))
    return "\n\n".join(sections) or "No readings are available right now."


def weather_now(result: str | None) -> str | None:
    """"Weather now": the weather station's bullets alone (None if the reading isn't available)."""
    now = _load(result)
    return "\n".join(weather_lines(now)) if now else None


# ---------- plain lookups ----------
def _alone(tool: str, result: dict, today: date) -> str | None:
    """A pollen or forecast result on its own: its section of the report, or None when it has no lines."""
    lines = _own_lines(result)
    return "\n".join([_heading(HEADINGS[tool], result, today), *lines]) if lines else None


def _days(data: dict) -> str | None:
    """"137 of 1,454 days with UV index ≥ 10 (period)", or a total, average, highest or lowest over the period: the chart (if any)
    carries the breakdown."""
    if "matching_days" not in data:
        return None
    if data.get("stat") and data.get("value") is not None:
        unit = unit_text(data["stat"].get("unit")) or ""
        return f"{data['stat']['what']}: {data['value']:g} {unit}".rstrip() + f" over {data['days_checked']:,} days ({_period(data)})."
    what = f" with {data['condition']}" if data.get("condition") else ""
    note = " Older days use averages, so a count near the limit may fall slightly short." if data.get("note_averaged") else ""
    return f"{data['matching_days']:,} of {data['days_checked']:,} days{what} ({_period(data)}).{note}"


def _series_label(field: str) -> str:
    """A series' name: its reading's label ("Temperature"), the gusts their own ("Wind gust")."""
    if field == "wind_gust":
        return "Wind gust"
    return reading.label if (reading := find(field)) else field.replace("_", " ").capitalize()


def _extremes(data: dict, names: list[str]) -> str | None:
    """The period, then the low and high (or the average) of each reading named, each with its day and time: wind, its speed and
    its gusts; nothing named, the temperature. A reading on both sides (indoor, outdoor) has a line for each."""
    series = data.get("series") or {}
    wanted = [f for r in filter(None, map(find, names)) for f in (r.field, r.band_field) if f] or ["temperature"]
    keys = [k for k in series if k.split(".", 1)[1] in wanted and "low" in series[k]]
    fields = [k.split(".", 1)[1] for k in keys]
    lines = []
    for key in keys:
        group, field = key.split(".", 1)
        label = _series_label(field) + (f" ({group})" if fields.count(field) > 1 else "")
        lines.append(f"• {_range(label, series[key], 'average' in series[key])}")
    return "\n".join([_period(data), *lines]) if lines else None


def lookup(kind: str, result: str | None, names: list[str], sides: list[str], today: date) -> str | None:
    """A plain lookup written from its one tool result: `kind` is "reading", "air", "days", "extremes", "pollen" or "forecast",
    `names` the readings asked about and `sides` where (indoor, outdoor), on `today` (local). None when the result can't be used
    (the model then takes the question)."""
    data = _load(result)
    answers = {"reading": lambda: "\n".join(reading_lines(data, names, sides)),
               "air": lambda: "\n".join(air_lines(data, names or None)),
               "days": lambda: _days(data),
               "extremes": lambda: _extremes(data, names),
               "pollen": lambda: _alone("pollen_asthma", data, today),
               "forecast": lambda: _alone("weather_forecast", data, today)}
    return (answers[kind]() or None) if data and kind in answers else None


# ---------- chart captions ----------
def _year(text: str | None) -> str:
    """"Tue 29 Sep 2026" -> "Tue 29 Sep"."""
    return re.sub(r" 20\d\d\b", "", text or "")


def _period(data: dict) -> str:
    """"Tue 29 Sep 2026 - Tue 06 Oct 2026" -> "Tue 29 Sep – Tue 06 Oct 2026" (the first year is dropped only when it is the same year)."""
    text = data.get("period", "")
    years = re.findall(r" (20\d\d)\b", text)
    if len(years) == 2 and years[0] == years[1]:
        text = re.sub(r" 20\d\d\b(?= - )", "", text)
    return text.replace(" - ", " – ")


def _at(entry: dict, which: str) -> str:
    """"high 24.3 °C, Tue 29 Sep around 3pm" for one record of a series."""
    when = _year(f"{entry.get(f'{which}_date', '')} {entry.get(f'{which}_when', '')}".strip())
    return f"{entry[which]} {unit_text(entry.get('unit')) or ''}".strip() + (f", {when}" if when else "")


def _range(label: str, entry: dict, average: bool) -> str:
    unit = unit_text(entry.get("unit")) or ""
    if average and entry.get("average"):
        return f"{label}: average {entry['average']} {unit} (low {entry['low']}, high {entry['high']})".replace(" )", ")")
    return f"{label}: low {_at(entry, 'low')} · high {_at(entry, 'high')}"


def _rain_line(turn, tz) -> list[str]:
    """Least and most rain in a period (the chart's own bars)."""
    bars = next((p.bars for c in turn.charts for p in c.panels if p.bars and p.bars.x), None)
    if not bars:
        return []
    def day(i: int) -> str:
        start = to_local(bars.x[i], tz)
        return day_text(start, year=False) + (f" {clock_text(start, whole_hours=True)}" if bars.width < 86400 else "")
    lo, hi = min(range(len(bars.y)), key=bars.y.__getitem__), max(range(len(bars.y)), key=bars.y.__getitem__)
    per = f" per {bars.per}" if bars.per else ""
    return [f"☔ Rain{per}: least {bars.y[lo]:g} mm ({day(lo)}) · most {bars.y[hi]:g} mm ({day(hi)}) · {sum(bars.y):.1f} mm in all"]


def _weather_caption(result: dict, args: dict, turn, tz) -> list[str]:
    series = result.get("series") or {}
    names = [n for n in dict.fromkeys(turn.chart_fields or [turn.chart_field or "temperature"]) if find(n)]
    lines: list[str] = []
    for name in names:
        reading = find(name)
        keys = [k for k in series if k.split(".", 1)[1] == reading.field]
        if reading.field == "daily":   # rain: the amounts the chart draws
            lines += _rain_line(turn, tz)
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
    for key, label in ((name, m.label) for name, m in AIR.items()):
        entry = result.get(key)
        if key not in (args.get("metrics") or ["pm2_5"]) or not isinstance(entry, dict):
            continue
        unit = unit_text(entry.get("unit")) or ""
        rating = entry.get("high_rating")
        lines.append(f"• {label}: peak {entry['high']} {unit} ({_year(entry.get('high_time'))})".replace(" )", ")")
                     + (f" · {rating}" if rating else "") + f" · average {entry['average']}")
    return lines


def chart_caption(tool: str, args: dict, result: str | None, turn, tz) -> str | None:
    """The caption for a chart, from the tool's result: the period, then a line per reading. None when there is nothing to say (the
    model then takes the question)."""
    data = _load(result)
    if not data or not turn.charts:
        return None
    lines = (_air_caption(data, args, turn) if tool == "air_quality" else
             _weather_caption(data, args, turn, tz))
    if not lines:
        return None
    notes = (["(Older days are still downloading, so they are left out.)"] if data.get("note_missing") else
             ["(Some data couldn't be fetched, so this may be incomplete.)"] if data.get("warning") else [])
    return "\n".join([_period(data), *lines, *notes]).strip()
