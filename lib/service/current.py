"""The `current` tool (docs/ecobot-mcp.md 3.3): how it is now, from the weather station and the air sensor, every reading or the ones
named, and with `context` today's, this month's and this year's highs and lows beside each reading, as the gauges show them. It replaces
weather_now and air_quality without dates (and the gauges' figures), on their engines:

  - the station's reading is its live reading as the gauges and weather_now read it (Ecowitt._live_data: the kept reading while it
    is fresh, else a request; raw {group: {field: {value, unit, time}}}), each reading with its value, unit, its text as weather_now writes
    it ("12.3 °C") and its glance emoji (lib/ecowitt/glance.py), when the reading is one to notice;
  - the air sensor's is AirGradient.latest(): value, unit, rating (held at the better level until two readings agree), PM2.5's US AQI and
    band, and its text as the report writes it;
  - `context` is the gauges' figures (lib/ecowitt/dashboard.py figures(), over the day rows of the year so far,
    lib/ecowitt/days.day_values_on, the live reading counted in for today's highs and lows), each high and low with when it happened (the
    day row's own time of it, worded as weather_history words a record) and its day; rain as its totals and its wettest day, wind as its
    highest gust. With no readings named, every gauge's reading the station sends now is there, those its file hasn't recorded yet (not
    in the catalogue: unrecorded()) too, so a client lays out the gauges (lib/gauge_sheet.py) from this result alone. The air sensor's
    air-quality readings (not its own temperature and humidity) have their highs and lows too, from its day rows (air_context_of: each
    with its day, but not its time of day), so a reading it lacks now is there with its figures alone, as the station's are.

`lines` are each device's readings as bullets, worded as the bot's report, "weather now" and plain lookups word them today
(lib/report.py weather_lines, air_lines, reading_lines, ported here with the same words): every reading (none named) in the report's
layout, else a line for each reading named. `headings` are the report's section headings, with the time of each device's reading. So a
client's report is current's headings and lines, then forecast's (today) and pollen's.

The request's `fetch` False answers from the readings held (the station's kept live reading, whatever its age, and the air sensor's
latest); `priority` "background" asks the station behind the readings someone is waiting for. There are no charts.
"""

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, tzinfo

from ..airgradient.metrics import EXTRA as AIR_OWN_METRICS
from ..ecowitt.api import MINUTE, unit_text
from ..ecowitt.dashboard import GAUGES, Gauge, figures, live_value, periods_of
from ..ecowitt.days import FIELDS, PARTS, day_values_on
from ..ecowitt.direction import point
from ..ecowitt.extremes import describe_time
from ..ecowitt.glance import glance, solar_band
from ..series import AIR, INDOOR_READINGS, WEATHER
from ..storage.catalogue import BY_NAME, column_of, kind_of, text as written
from ..storage.read import Reader
from ..timeutil import clock_text, day_text, to_local
from .common import BACKGROUND, Refusal, ServiceTool, answer, request_meta
from .context import Context
from .names import AIR_DEVICE, CATALOGUE, STATION, Catalogue, Reading, _split

log = logging.getLogger(__name__)

NAME = "current"
ARGUMENTS = ("readings", "context")
ANOTHER_TIME = ("period", "start", "end")   # a call about another time than now: summary's or readings'
HEADINGS = {STATION: "Weather station", AIR_DEVICE: "Air quality"}       # the report's sections (lib/report.py SECTIONS)
NOT_AVAILABLE = "• not available right now"                              # ... and a section whose device didn't answer
UNAVAILABLE = {STATION: "The weather station isn't answering right now.", AIR_DEVICE: "The air sensor isn't answering right now."}
NOTHING_HELD = {STATION: "No reading of the weather station is held (and `fetch` is off).",
                AIR_DEVICE: "No reading of the air sensor is held (and `fetch` is off)."}
PERIODS = (("Daily", "today"), ("Monthly", "month"), ("Yearly", "year"))   # a gauge's rows, as the result names them

# ---------- the description and the input schema ----------
DESCRIPTION = (
    "How it is now, from the weather station (Ecowitt) and the air sensor (AirGradient): every reading, or those named. Everything now "
    "(\"weather now\", \"how's it outside\"): {}. How hot is it: {readings: [\"temperature\"]}. Is it raining: {readings: [\"rain\"]}. "
    "The air now: {readings: [\"air\"]}. With today's, this month's and this year's highs and lows beside each reading, as gauges show "
    "them (rain: its totals and wettest day; wind: its highest gust): {context: true}. A report, status or sitrep: current, forecast "
    "(period \"today\") and pollen, called together. Not for: will it rain (outlook); a period's highs, lows (\"lowest and highest "
    "humidity\"), averages or a chart, "
    "a device or reading with a period, \"ecowitt week\", \"weather week\" (summary); values at set times (readings); the days ahead (forecast). Result: sensors.<sensor>.<reading> (sensors.outdoor."
    "temperature) with value, unit, text ready to quote (\"12.3 °C\") and emoji when the reading is one to notice (put it right "
    "before the reading); rainfall.rain is today's rain so far; wind_direction has its compass point; the air "
    "sensor's have their rating (put it beside the value) and PM2.5 its US aqi_us and band. With context: today, month and year, each "
    "with high and low (_text to quote, _when copied as written, _date the day). lines: each "
    "device's readings as bullet lines, to pass on as they are (headings for a report). missing: readings asked "
    "for that aren't being read now; unavailable: a device not answering.")
CONTEXT = {"type": "boolean", "description": "true to add today's, this month's and this year's highs and lows beside each of the "
                                             "weather station's readings that has them (temperature, humidity, pressure, dew point, "
                                             "feels like, wind's highest gust, rain's totals and wettest day, rain rate, solar, UV, "
                                             "indoor temperature and humidity) and the air sensor's air-quality readings. Default "
                                             "false."}


def parameters(description: str) -> dict:
    """The input schema, its readings described by the catalogue the tool reads (`description`)."""
    return {"type": "object", "properties": {
        "readings": {"type": "array", "items": {"type": "string"},
                     "description": f"{description} Default: every reading the station and the air sensor have now."},
        "context": CONTEXT}}


# ---------- the words, as lib/report.py writes them ----------
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


def _getters(now: dict):
    """(get, tag) for a weather_now-shaped reading ({group: {field: "12.3 ℃"}, "emoji": {"group.field": emoji}}): a reading's text, and
    the same with its emoji (if it has one) before it."""
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
    """The weather station's bullets, the report's and "weather now"'s: an emoji sits right before the reading it is keyed to."""
    get, tag = _getters(now)
    solar, uvi = get("solar_and_uvi", "solar"), get("solar_and_uvi", "uvi")
    sun = _join([None if _zero(solar) else tag("solar_and_uvi", "solar", f"solar radiation {solar}{_band(solar)}"),       # night: no sun line
                 None if _zero(uvi) else tag("solar_and_uvi", "uvi", f"UV index {uvi}")])
    bullets = [("Outdoor", _join([tag("outdoor", "temperature"), tag("outdoor", "humidity")])),   # (dew point and VPD are there to ask for)
               ("Indoor", _join([tag("indoor", "temperature"), tag("indoor", "humidity")])),
               ("Pressure", tag("pressure", "relative")),
               ("Rain today", _rain(now)),
               ("Sun", sun),
               ("Wind", _wind(now))]    # calm: no line
    return [f"• {name}: {text}" for name, text in bullets if text]


def air_shown(key: str, value: float, unit: str) -> str:
    """An air reading as the report writes it: "1.4 µg/m³", "433 ppm", "88" (VOC), "1.0" (NOx); the sensor's own temperature and
    humidity as the station's are ("18.5 °C", "60 %")."""
    if key in AIR_OWN_METRICS:
        return f"{value:.1f} {unit}" if unit == "°C" else f"{value:.0f} {unit}"
    return (f"{value:.1f} {unit}" if unit.startswith("µg") else f"{value:.0f} {unit}" if unit == "ppm"
            else f"{value:.1f}" if key == "nox_index" else f"{value:.0f}")


def air_lines(air: dict, only: list[str] | None = None) -> list[str]:
    """The air sensor's bullets from its latest reading (AirGradient.latest()), those in `only` (all when None), as the report writes them."""
    out = []
    for key, label in ((name, m.label) for name, m in AIR.items()):
        entry = air.get(key) if only is None or key in only else None
        if not isinstance(entry, dict) or entry.get("value") is None:
            continue
        aqi = f" (AQI {entry['aqi_us']})" if key == "pm2_5" and "aqi_us" in entry else ""
        out.append(f"• {label}: {_join([air_shown(key, entry['value'], entry.get('unit', '')), entry.get('rating')], ' ')}{aqi}")
    return out


def reading_lines(now: dict, names: list[str], sides: list[str]) -> list[str]:
    """Just the readings asked about (lib/series.py's names: "how hot is it": the temperature, indoors and out), as the bot's plain
    lookup writes them."""
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


def _stamp(at: datetime, today: date) -> str:
    """"11:45pm", with its day when that isn't today ("Sat 10 Oct 11:45pm")."""
    return clock_text(at) if at.date() == today else f"{day_text(at, year=False)} {clock_text(at)}"


def heading(device: str, at: datetime | None, today: date) -> str:
    """"Weather station (2:21am)": the report's heading of a device's section, with the time of its reading when it has one."""
    return f"{HEADINGS[device]} ({_stamp(at, today)})" if at else HEADINGS[device]


# ---------- the station's live reading ----------
def now_shaped(live: dict, tz: tzinfo) -> tuple[dict, int]:
    """(weather_now's result from a live reading, {group: {field: "12.3 ℃"}, "emoji": {"group.field": emoji}}, as Ecowitt's
    weather_now tool makes it; the time of the newest reading in it, an epoch, 0 for none)."""
    out, newest, emoji = {}, 0, {}
    for group, fields in live.items():
        for name, obj in (fields.items() if isinstance(fields, dict) else ()):
            if isinstance(obj, dict) and "value" in obj:
                out.setdefault(group, {})[name] = f"{obj['value']} {obj.get('unit', '')}".strip()
                try:
                    if tag := glance(group, name, float(obj["value"])):
                        emoji[f"{group}.{name}"] = tag
                except (TypeError, ValueError):
                    pass
                try:
                    newest = max(newest, int(obj.get("time") or 0))
                except (TypeError, ValueError):
                    pass
    return {**out, **({"emoji": emoji} if emoji else {})}, newest


def _resolve(catalogue, names, *, default) -> list[Reading]:
    """The readings named, as the catalogue reads them; a reading's full name that the file hasn't recorded yet but the service knows
    (names.CATALOGUE) counts too, since current answers from the reading now (a new station's first minutes, a gauge's reading)."""
    known = [w for w in _split(names) if " ".join(w.lower().split()) not in catalogue and " ".join(w.lower().split()) in CATALOGUE]
    if not known:
        return catalogue.resolve(names, default=default)
    out: dict[str, Reading] = {}
    for word in _split(names):
        exact = " ".join(word.lower().split())
        for r in [CATALOGUE.get(exact)] if word in known else catalogue.resolve(word, default=[]):
            out.setdefault(r.name, r)
    return list(out.values())


def unrecorded(live: dict, listed: set[str]) -> list[Reading]:
    """The station's readings in its live reading that aren't among these names (the catalogue's: what its file has recorded), as the
    catalogue would name them once recorded: a sensor sending, or a reading sent, since the file last took the station's fields."""
    sent = {group: {name: obj.get("unit", "") for name, obj in fields.items() if isinstance(obj, dict) and "value" in obj}
            for group, fields in live.items() if isinstance(fields, dict)}
    return [r for r in Catalogue.of({g: f for g, f in sent.items() if f}, air=False)
            if r.sensor in sent and r.name not in listed and live_field(live, r) is not None]


def live_field(live: dict, reading: Reading) -> str | None:
    """The live reading's own name for a catalogue reading in its group ("daily" for rainfall.rain, "app_tempin" for indoor.app_temp),
    or None when the live reading lacks it."""
    for name, obj in (live.get(reading.sensor) or {}).items():
        if isinstance(obj, dict) and "value" in obj and column_of(reading.sensor, name) == (reading.column, "value"):
            return name
    return None


def _number(raw) -> float | int | None:
    """A reading as a number ("94" -> 94, "12.3" -> 12.3); None when it isn't one."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return int(value) if re.fullmatch(r"\s*-?\d+\s*", str(raw)) else value


def station_entry(live: dict, now: dict, reading: Reading, field: str) -> dict:
    """{value, unit, text, [emoji], [compass]} of one of the station's readings now."""
    obj = live[reading.sensor][field]
    entry = {"value": _number(obj["value"]), "unit": unit_text(obj.get("unit")) or "",   # (as its text has it: the UV index has none)
             "text": unit_text(now[reading.sensor][field])}
    if emoji := (now.get("emoji") or {}).get(f"{reading.sensor}.{field}"):
        entry["emoji"] = emoji
    if reading.reading == "wind_direction" and entry["value"] is not None:
        entry["compass"] = point(entry["value"])
    return entry


# ---------- the gauges' figures (context) ----------
def gauge_of(name: str) -> Gauge | None:
    """The gauge a reading's context comes from: the one whose live reading it is (rainfall.rain: the Rain gauge's "daily"); the gusts
    get the Wind gauge's highest, with the live gust counted in."""
    if name == "wind.wind_gust":
        return replace(next(g for g in GAUGES if g.live == ("wind", "wind_speed")), live=("wind", "wind_gust"), counted=None)
    return next((g for g in GAUGES if g.reading == name), None)


def _times_on(r: Reader, mac: str, names: list[str], days: list[date]) -> dict[str, dict[date, tuple[int, str]]]:
    """{figure: {day: (when its high or low happened, the resolution that time is from)}}: the day rows' own times of the figures
    day_values_on reads, for these days (one query per kind of sensor)."""
    out: dict[str, dict[date, tuple[int, str]]] = {n: {} for n in names}
    by_kind: dict[str, tuple[dict, set]] = {}
    places = {}
    for name in names:
        group, field, which, _ = FIELDS[name]
        sensor, place = r.sensor(mac, group), column_of(group, field)
        if sensor is None or place is None or PARTS[which] == "mean":
            continue
        places[name] = (sensor, place[0], PARTS[which] + "_ts")
        by_kind.setdefault(sensor.kind, ({}, set()))[0][sensor.id] = sensor
        by_kind[sensor.kind][1].add(place[0])
    rows = {}
    for kind, (sensors, columns) in by_kind.items():
        rows.update(r.days(kind, list(sensors), min(days), max(days), sorted(columns)))
    wanted = set(days)
    for name, (sensor, column, part) in places.items():
        got = rows.get(sensor.id)
        if got is None or column not in getattr(got, part):
            continue
        for d, ts, timing in zip(got.day, getattr(got, part)[column], got.timing):
            if ts is not None and d in wanted:
                out[name][d] = (ts, timing)
    return out


def context_on(r: Reader, mac: str, names: list[str], days: list[date]) -> tuple[dict, dict]:
    """(the figures of these days as the gauges show them, day_values_on's {figure: {day: value}}; their times, _times_on's)."""
    values, _ = day_values_on(r, mac, names, days)
    return values, _times_on(r, mac, names, days)


def _figure_text(gauge: Gauge, figure: str, value: float, unit: str) -> str:
    """A gauge's figure written as the station writes the reading (its column's decimals) with its unit: "24.3 °C", "62 %", "11"."""
    group, field, _, _ = FIELDS[figure]
    column = BY_NAME[kind_of(group)][column_of(group, field)[0]]
    return f"{written(value, column.decimals, column.whole)} {unit}".strip()


class Live:
    """The live reading of one gauge: its value and when it was read (an epoch, or None)."""

    def __init__(self, live: dict, gauge: Gauge):
        self.value = live_value(live, gauge.live)
        group, field = gauge.live
        try:
            self.ts = int(((live.get(group) or {}).get(field) or {}).get("time") or 0) or None
        except (TypeError, ValueError):
            self.ts = None


def _extreme(key: str, value: float, figure: str, days: list[date], values: dict, times: dict, live: Live | None, gauge: Gauge,
             unit: str, tz: tzinfo, dated: bool) -> dict:
    """{key, key_text, key_when, [key_date]} of one high or low: when it happened is the first day of these that had it (its day row's
    time of it), else the live reading's (it counted in, and is higher or lower than any day's)."""
    out = {key: value, f"{key}_text": _figure_text(gauge, figure, value, unit)}
    day = next((d for d in days if values.get(figure, {}).get(d) == value), None)
    if day is not None and day in times.get(figure, {}):
        ts, timing = times[figure][day]
        _, out[f"{key}_when"], on = describe_time(ts, timing, tz)
    elif day is None and live is not None and live.value == value and live.ts:
        _, out[f"{key}_when"], on = describe_time(live.ts, MINUTE, tz)
    else:
        return out
    if dated and on:
        out[f"{key}_date"] = on
    return out


def context_of(name: str, live: dict, values: dict, times: dict, today: date, tz: tzinfo) -> dict:
    """{today, month, year} beside one reading: the figures its gauge's rows give (dashboard.figures, the live reading counted in for its
    highs and lows, today's rain the gauge's own reading), each high and low with when it happened and its day; rain's totals and wettest
    day; wind speed's highest as `high_gust`. {} for a reading with no gauge, or none of whose figures are held."""
    gauge = gauge_of(name)
    if gauge is None:
        return {}
    days, unit = periods_of(today), gauge.unit
    now = Live(live, gauge)
    counted = Live(live, replace(gauge, live=gauge.counted)) if gauge.counted else now   # (wind: the gust now, not the speed)
    out = {}
    for row, key in PERIODS:
        got = figures(gauge, values, days[row], None if gauge.total else counted.value)
        if gauge.total and row == "Daily" and now.value is not None:   # today's rain is the gauge's own reading (as the gauge shows it)
            got = (None, now.value)
        entry: dict = {}
        if gauge.total:
            wettest, total = got
            if total is not None:
                entry.update(total=total, total_text=_figure_text(gauge, gauge.highest, total, unit))
            if wettest is not None:
                day = next(d for d in days[row] if values.get(gauge.highest, {}).get(d) == wettest)
                entry["wettest_day"] = {"rain": wettest, "text": _figure_text(gauge, gauge.highest, wettest, unit), "date": day_text(day)}
        else:
            high = "high_gust" if name == "wind.wind_speed" else "high"
            pairs = [(high, gauge.highest, got[0])] + ([("low", gauge.lowest, got[1])] if gauge.lowest else [])
            for key_, figure, value in pairs:
                if value is not None:
                    entry.update(_extreme(key_, value, figure, days[row], values, times, counted, gauge, unit, tz,
                                          dated=row != "Daily"))
        if entry:
            out[key] = entry
    return out


def air_context_of(column: str, now: float | None, unit: str, values: dict, today: date) -> dict:
    """{today, month, year} beside one of the air sensor's readings: the highest and lowest of its day rows (values: the air sensor's
    {"pm2_5_max": {day: value}, ...}, AirReadings.day_figures'), the reading now counted in, each written as the report writes the
    reading, with its day (month and year). {} when none of its figures is held."""
    days, out = periods_of(today), {}
    for row, key in PERIODS:
        entry: dict = {}
        for name, part, pick in (("high", "max", max), ("low", "min", min)):
            held = {d: v for d in days[row] if (v := values.get(f"{column}_{part}", {}).get(d)) is not None}
            figures = [*held.values(), *([now] if now is not None else [])]
            if not figures:
                continue
            value = pick(figures)
            entry.update({name: value, f"{name}_text": air_shown(column, value, unit)})
            on = next((d for d, v in held.items() if v == value), today)
            if row != "Daily":
                entry[f"{name}_date"] = day_text(on)
        if entry:
            out[key] = entry
    return out


# ---------- the tool ----------
class CurrentTool:
    """The current tool over a Context: the station's and the air sensor's readings now."""

    def __init__(self, ctx: Context):
        self.ctx = ctx

    @property
    def devices(self) -> list[str]:
        return [*([STATION] if self.ctx.eco else []), *([AIR_DEVICE] if self.ctx.air else [])]

    def tool(self) -> ServiceTool:
        return ServiceTool(NAME, DESCRIPTION, parameters(self.ctx.readings().description()), self.handle, open_world=True)

    # ---------- the call ----------
    def _asked(self, args: dict) -> tuple[list[Reading], bool, bool]:
        """(the readings asked for, whether they were named, context): the call checked, a wrong one refused with what fixes it."""
        if unknown := [k for k in args if k not in ARGUMENTS]:
            then = any(k in ANOTHER_TIME for k in unknown)
            raise Refusal(f"{', '.join(map(repr, unknown))} {'is not an argument' if len(unknown) == 1 else 'are not arguments'} of "
                          "current" + (": it gives the readings now. For a period's highs, lows or averages call summary with that "
                                       "period; for values at set times, readings" if then else "") + ".", arguments=list(ARGUMENTS))
        context = args.get("context")
        if isinstance(context, str) and context.strip().lower() in ("true", "false"):
            context = context.strip().lower() == "true"
        if context is not None and not isinstance(context, bool):
            raise Refusal(f"`context` is true or false, not {context!r}.")
        catalogue = self.ctx.readings()
        here = [r for r in catalogue if r.device in self.devices]
        named = args.get("readings") not in (None, "", [])
        asked = _resolve(catalogue, args.get("readings"), default=[r.name for r in here])
        if named and (absent := [r for r in asked if r.device not in self.devices]):
            device = "weather station" if absent[0].device == STATION else "air sensor"
            raise Refusal(f"{absent[0].name} is the {device}'s, and this service has no {device}.", names=[r.name for r in here])
        return [r for r in asked if r.device in self.devices], named, bool(context)

    async def handle(self, args: dict, meta: dict) -> str:
        asked, named, context = self._asked(args)
        parts, gauges = [], context and not named   # every gauge: those of a reading the file hasn't recorded yet too
        if (station := [r for r in asked if r.device == STATION]) or (gauges and STATION in self.devices):
            parts.append(await self._station(station, named, context, meta))
        if gauges and AIR_DEVICE in self.devices:
            air_names = {r.name for r in asked}
            asked = asked + [r for c in AIR if f"air.{c}" not in air_names and (r := CATALOGUE.get(f"air.{c}"))]
        if air := [r for r in asked if r.device == AIR_DEVICE]:
            parts.append(await self._air(air, named, meta, context))
        sensors: dict[str, dict] = {}
        for part in parts:
            for r, entry in part.entries:
                sensors.setdefault(r.sensor, {})[r.reading] = entry
        fields = {"sensors": sensors, "lines": {p.device: p.lines for p in parts}, "headings": {p.device: p.heading for p in parts}}
        if missing := [name for p in parts for name in p.missing]:
            fields["missing"] = missing
        if unavailable := {p.device: p.unavailable for p in parts if p.unavailable}:
            fields["unavailable"] = unavailable
        times = [p.at for p in parts if p.at]
        return answer(period=None, as_of=max(times) if times else self.ctx.now() if sensors else None, **fields)

    async def now_of(self, device: str, meta: dict | None = None) -> "Part":
        """One device's part as `current` with no readings named gives it (its every reading now, its lines and heading), for the
        service's resources (ecobot://now/weather, ecobot://now/air: lib/mcp_server.py). meta: the request's (request_meta)."""
        readings = [r for r in self.ctx.readings() if r.device == device]
        meta = request_meta(meta)
        return await (self._station(readings, False, False, meta) if device == STATION else self._air(readings, False, meta))

    async def _station(self, station: list[Reading], named: bool, context: bool, meta: dict) -> "Part":
        """The weather station's part: each reading asked for now (with the gauges' figures beside it, with `context`), its lines and
        heading. A reading named that it isn't reading is missing; so is a gauge's, with `context`, shown with its figures alone."""
        tz, today = self.ctx.zone, self.ctx.now().date()
        try:
            live = await self._live(station, named, meta)
        except NothingHeld:
            return Part.down(STATION, NOTHING_HELD[STATION], today)
        except Exception as e:
            log.warning("current: the weather station's reading failed: %s", e)
            return Part.down(STATION, UNAVAILABLE[STATION], today)
        now, newest = now_shaped(live, tz)
        at = to_local(newest, tz) if newest else None
        if context and not named:   # every gauge, as it shows: a gauge's reading sent now that the file hasn't recorded yet too
            station = station + [r for r in unrecorded(live, {r.name for r in station}) if gauge_of(r.name)]
        gauges = await self._context(station) if context else None
        part, shown = Part(STATION, at=at), []
        for r in station:
            own = live_field(live, r)
            beside = context_of(r.name, live, *gauges, today, tz) if gauges else {}
            if own is not None:
                part.entries.append((r, {**station_entry(live, now, r, own), **beside}))
                shown.append(r)
                continue
            if named or beside:
                part.missing.append(r.name)
            if beside:   # (a gauge drawn dimmed: no reading now, its figures still there)
                part.entries.append((r, {"value": None, "unit": gauge_of(r.name).unit, **beside}))
        part.lines = station_lines(now, shown) if named else weather_lines(now)
        part.heading = heading(STATION, at, today)
        return part

    async def _air(self, air: list[Reading], named: bool, meta: dict, context: bool = False) -> "Part":
        """The air sensor's part: each reading asked for now (with its highs and lows beside it, with `context`), its lines and heading.
        A reading named that it isn't reading is missing; so is one with figures, with `context`, shown with its figures alone (as when
        the sensor isn't answering)."""
        tz, today = self.ctx.zone, self.ctx.now().date()
        figures = await self._air_context(air) if context else {}
        try:
            latest = await self._latest([r.column for r in air if r.column in AIR_OWN_METRICS], meta)
        except Exception as e:
            if not isinstance(e, NothingHeld):
                log.warning("current: the air sensor's reading failed: %s", e)
            part = Part.down(AIR_DEVICE, NOTHING_HELD[AIR_DEVICE] if isinstance(e, NothingHeld) else UNAVAILABLE[AIR_DEVICE], today)
            part.entries = [(r, {"value": None, "unit": AIR[r.column].unit, **beside}) for r in air
                            if r.column in AIR and (beside := air_context_of(r.column, None, AIR[r.column].unit, figures, today))]
            return part
        reading, at = latest.reading, latest.at.astimezone(tz) if latest.at else None
        part, shown = Part(AIR_DEVICE, at=at), []
        for r in air:
            entry = reading.get(r.column)
            now = entry.get("value") if isinstance(entry, dict) else None
            unit = entry.get("unit", "") if isinstance(entry, dict) else AIR[r.column].unit if r.column in AIR else ""
            beside = air_context_of(r.column, now, unit, figures, today) if figures and r.column in AIR else {}
            if now is not None:
                part.entries.append((r, {**air_entry(r.column, entry), **beside}))
                shown.append(r)
                continue
            if named or beside:
                part.missing.append(r.name)
            if beside:   # (a gauge drawn dimmed: no reading now, its figures still there)
                part.entries.append((r, {"value": None, "unit": unit, **beside}))
        part.lines = own_air_lines(reading, shown) if named else air_lines(reading)
        part.heading = heading(AIR_DEVICE, at, today)
        return part

    async def _air_context(self, air: list[Reading]) -> dict:
        """The air sensor's day figures for its air-quality readings among these ({"pm2_5_max": {day: value}, ...}), the year so far,
        in one read; {} when they can't be read (the readings now are still given)."""
        names = [f"{r.column}_{part}" for r in air if r.column in AIR for part in ("max", "min")]
        if not names:
            return {}
        days = periods_of(self.ctx.now().date())["Yearly"]
        try:
            return (await self.ctx.air.store.day_figures(days[0], days[-1], names)).values
        except Exception as e:   # (only the figures: the readings now stand)
            log.warning("current: the air sensor's day figures failed: %s: %s", type(e).__name__, e)
            return {}

    async def _live(self, station: list[Reading], named: bool, meta: dict) -> dict:
        """The station's live reading for every group "weather now" shows (the reading it keeps, while that is fresh) and, only when they
        are named, those of other readings (the piezo gauge: a request of its own); with `fetch` off, the reading it keeps, whatever its
        age (NothingHeld when it keeps none)."""
        from ..ecowitt.source import LIVE_GROUPS   # (lazy: the station's package is the engine; this module only reads it)
        eco = self.ctx.eco
        groups = list(dict.fromkeys([*LIVE_GROUPS, *(r.sensor for r in station if named)]))
        if not meta["fetch"]:
            kept = getattr(eco, "_live", None)
            if not kept:
                raise NothingHeld()
            return {g: kept[1][g] for g in groups if g in kept[1]}
        return await eco._live_data(groups, urgent=meta["priority"] != BACKGROUND)

    async def _latest(self, extras: list[str], meta: dict):
        """The air sensor's latest reading (AirGradient.latest), with its own temperature and humidity when named; with `fetch` off, the
        reading it keeps, whatever its age, rated on the readings before it it already has: nothing fetched (NothingHeld when it keeps
        none), as the station's (_live)."""
        if not meta["fetch"]:
            if getattr(self.ctx.air, "_current", None) is None:
                raise NothingHeld()
            return await self.ctx.air.latest(extras, held=True)
        return await self.ctx.air.latest(extras)

    async def _context(self, station: list[Reading]) -> tuple[dict, dict]:
        """The gauges' figures for the readings that have a gauge (context_on): their day rows of the year so far, in one read."""
        names = sorted({n for r in station if (g := gauge_of(r.name)) for n in (g.highest, g.lowest) if n})
        if not names:
            return {}, {}
        eco = self.ctx.eco
        return await eco.storage.read(context_on, eco.mac, names, periods_of(self.ctx.now().date())["Yearly"])


@dataclass
class Part:
    """One device's part of the answer: its readings (each with its entry), its lines and heading, what was named but isn't read now,
    why it gave nothing (when it didn't answer), and the time of its reading."""
    device: str
    entries: list[tuple[Reading, dict]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    heading: str = ""
    missing: list[str] = field(default_factory=list)
    unavailable: str | None = None
    at: datetime | None = None

    @classmethod
    def down(cls, device: str, why: str, today: date) -> "Part":
        """A device that gave nothing: the report's section for it ("Weather station", "• not available right now")."""
        return cls(device, lines=[NOT_AVAILABLE], heading=heading(device, None, today), unavailable=why)


class NothingHeld(Exception):
    """With `fetch` off, a device whose reading isn't held."""


def air_entry(key: str, entry: dict) -> dict:
    """{value, unit, text, [rating], [rating_note], [aqi_us, band]} of an air reading (AirGradient.latest()'s), with its text."""
    unit = entry.get("unit", "")
    return {"value": entry["value"], "unit": unit, "text": air_shown(key, entry["value"], unit),
            **{k: entry[k] for k in ("rating", "rating_note", "aqi_us", "band") if k in entry}}


COVERED = {"wind.wind_gust": "wind", "wind.wind_direction": "wind", "rainfall.rain_rate": "rain"}   # in the line of another reading


def station_lines(now: dict, readings: list[Reading]) -> list[str]:
    """The lines of the station's readings named: the bot's plain lookup's (reading_lines) for each reading it words, each on the sides
    asked for it (outdoor, indoor; both when it has neither), then a line of its own for each other reading not in one of those."""
    names = list(dict.fromkeys(r.series for r in readings if r.series in WEATHER))
    out = []
    for name in names:
        sides = [s for s in ("outdoor", "indoor") if any(r.sensor == s and r.series == name for r in readings)] or ["outdoor", "indoor"]
        out += reading_lines(now, [name], sides)
    _, tag = _getters(now)
    for r in readings:
        if r.series in WEATHER or COVERED.get(r.name) in names:
            continue
        own = next((f for f in (now.get(r.sensor) or {}) if column_of(r.sensor, f) == (r.column, "value")), None)
        if own and (shown := tag(r.sensor, own)):
            out.append(f"• {r.label}: {shown}")
    return out


def own_air_lines(reading: dict, readings: list[Reading]) -> list[str]:
    """The lines of the air readings named: the report's (air_lines) for its six, then the sensor's own temperature and humidity."""
    keys = [r.column for r in readings]
    own = [f"• {r.label}: {air_shown(r.column, reading[r.column]['value'], reading[r.column].get('unit', ''))}"
           for r in readings if r.column in AIR_OWN_METRICS]
    return air_lines(reading, [k for k in keys if k in AIR]) + own


def tools(ctx: Context) -> list[ServiceTool]:
    """The current tool, when there is a sensor to read."""
    return [CurrentTool(ctx).tool()] if ctx.eco or ctx.air else []


__all__ = ["CurrentTool", "DESCRIPTION", "air_lines", "context_of", "gauge_of", "heading", "now_shaped", "parameters", "reading_lines",
           "station_lines", "tools", "weather_lines"]
