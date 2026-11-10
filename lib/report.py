"""What the model path still needs written in code from the old tools' results, until the model gets the service's tools (docs/
mcp-service.md 6, step 3): the caption of a chart that answers the question by itself (the bot's chart_answers_it finish on
weather_history, air_quality and the day tools), and the bold names of the report's headings. Everything the bot answers without the
model is the service's own words now (lib/service: current's lines, summary's caption and text, days' answer, the forecast's and the
pollen's text). Each tool result is JSON; one that failed or is missing gives nothing."""

import json
import re

from telegram import MessageEntity

from .ecowitt.api import unit_text
from .entities import utf16_len
from .series import AIR, find
from .timeutil import clock_text, day_text, to_local


def _load(result: str | None) -> dict | None:
    """The tool's JSON, or None when it is missing, an error message or says it has nothing."""
    try:
        data = json.loads(result or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) and "error" not in data else None


# ---------- the report's headings (the service's current, pollen and forecast write them) ----------
HEADINGS = ("Weather station", "Air quality", "Pollen & asthma", "Forecast")
HEADING_LINE = re.compile(rf"^({'|'.join(map(re.escape, HEADINGS))})(?= \(|$)", re.M)


def heading_entities(text: str) -> list[MessageEntity]:
    """Bold entities over the name of each heading line in `text` (not over its brackets)."""
    return [MessageEntity(MessageEntity.BOLD, utf16_len(text[:m.start()]), utf16_len(m.group(1))) for m in HEADING_LINE.finditer(text)]


# ---------- a days lookup ----------
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


def lookup(kind: str, result: str | None, names: list[str] = (), sides: list[str] = (), today=None) -> str | None:
    """A day tool's result in words (`kind` "days"): the count, or the figure over the period. None when the result can't be used (the
    model then answers). (`names`, `sides` and `today` are the old lookups', kept so its callers read as before.)"""
    data = _load(result)
    return (_days(data) or None) if data and kind == "days" else None


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


def _weather_caption(result: dict, turn, tz) -> list[str]:
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


def _air_caption(result: dict, args: dict) -> list[str]:
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
    lines = _air_caption(data, args) if tool == "air_quality" else _weather_caption(data, turn, tz)
    if not lines:
        return None
    notes = (["(Older days are still downloading, so they are left out.)"] if data.get("note_missing") else
             ["(Some data couldn't be fetched, so this may be incomplete.)"] if data.get("warning") else [])
    return "\n".join([_period(data), *lines, *notes]).strip()
