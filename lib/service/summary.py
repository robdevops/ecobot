"""The `summary` tool (docs/mcp-service.md 3.3): a period's figures from the weather station and the air sensor, each reading's lowest
and highest with when and that day's look, averages, a breakdown by day or by month, wind direction, the rain that fell, the air's
ratings and AQI, and a chart of any readings from either sensor or both. It replaces weather_history, air_quality with dates and
plot_chart, and wraps their engines, each given only what this tool's arguments say (a Turn built from them, never from words):

  - the station's readings: lib/ecowitt/query.py's HistoryQuery, as Ecowitt._history runs it (`_Query` keeps its chart to the readings
    drawn, and works out the rain that fell when no chart did);
  - the air sensor's: AirGradient.history (air_quality with dates);
  - a chart that mixes the two sensors, draws a rating (`air.pm2_5:rating`) or puts readings on one axis (`same_axis`):
    Composer.plot_chart, which draws whole days up to yesterday.

The result (3.3): {period, as_of, fresh, series: {"outdoor.temperature": {unit, low, low_text, low_when, low_date, low_day_look,
[low_note], high…, [average, average_text], [daily|monthly]}, "air.pm2_5": {…, average, high_rating, average_rating, high_aqi_us,
high_band, average_aqi_us, average_band}, "wind.wind_direction": {most_common, then, steadiness, [daily]}, "rainfall.rain": {unit,
total, total_text, [wettest_day], [daily|monthly]}}, [rain_total_mm], [monthly_note], [note_no_readings], [note_chart], [notes],
[missing, warning], [caption] or [text], [chart_note, charts], [reply, complete]}.

Words for the person, as the bot writes them today from weather_history's and air_quality's results (lib/report.py, its wording ported
here, as the bot's code goes): `caption` under a chart (report.chart_caption), and `text` when there is none (report.lookup's
"extremes"). Each chart carries `drawn_by`, the call that draws it again, and the periods it is worth drawing over (3.7).
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..captions import CHART_MIN_DAYS
from ..compose import MAX_PANELS, Composer
from ..ecowitt.api import CYCLES, daily_run, kept_from, unit_text
from ..ecowitt.days import day_values_on
from ..ecowitt.fetch import Fetcher
from ..ecowitt.query import DERIVED, HistoryQuery
from ..ecowitt.reads import Held, readings_on, slot_values_on
from ..periods_text import Period
from ..series import AIR, WEATHER, find
from ..specs import AIR_SOURCE, ECOWITT_SOURCE, Bars, Chart
from ..storage.catalogue import KINDS, field_of
from ..timeutil import clock_text, day_text, local_date, to_local
from ..tools import Turn
from .common import BACKGROUND, Refusal, ServiceTool, answer, complete, period_properties, read_period, with_charts
from .context import Context
from .daylook import FIGURES, day_look
from .names import AIR_DEVICE, STATION, Reading

NAME = "summary"
DEFAULT_READINGS = ("temperature", "humidity")   # the station's usual set: outdoor and indoor temperature and humidity
LAYOUTS = ("auto", "panels", "two_axes", "same_axis")
RATING = ":rating"
AIR_QUALITY = tuple(AIR)   # the air sensor's readings that have a rating (its own temperature and humidity have none)
# The periods a chart of readings over time is worth drawing over (3.7); one of each day's average needs two whole days at least
OVER_TIME = ({"period": "1d", "label": "1D"}, {"period": "7d", "label": "7D"}, {"period": "1m", "label": "1M"},
             {"period": "3m", "label": "3M"}, {"period": "1y", "label": "1Y"})
DAILY_MEANS = OVER_TIME[1:]
DAILY_ALL_DAYS = 7   # as HistoryQuery: a longer period lists only the days of its records, unless every_day
# The notes a caption carries itself (lib/bot.py CAPTIONED_NOTES for weather_history and air_quality): any other needs the model's words
CAPTIONED = {"missing", "warning"}
AIR_CAPTIONED = {"missing"}
DATE = re.compile(r"\d{4}-\d\d-\d\d")

DESCRIPTION = (
    "What was measured over a period: each reading's lowest and highest with when, "
    "averages, a by-day or by-month breakdown, wind direction, rain that fell, air ratings and AQI; a chart of any.\n"
    "Examples: \"how hot did it get this week\" {readings: [\"temperature\"], period: \"this week\"}; \"lowest and highest humidity\" "
    "{readings: [\"humidity\"]} (today); \"is today hotter than yesterday\" {readings: [\"temperature\"], period: \"yesterday to "
    "today\", every_day: true}; a record with when (\"hottest record\", \"highest ever\", \"use 5 minute data\") {readings: "
    "[\"outdoor.temperature\"], period: \"on record\"}; average {readings: [\"humidity\"], period: \"last "
    "month\", average: true}; \"plot pressure this week\" {readings: [\"pressure\"], period: \"this week\", chart: true, "
    "chart_answers_it: true}; a device or reading and a period is the past, charted: \"weather week\", \"ecowitt week\" {readings: "
    "[\"all\"], period: \"week\", chart: true}, \"wind 3m\" {readings: [\"wind\"], period: \"3m\", chart: true}; a rating and rain "
    "{readings: [\"pm2.5:rating\", \"rain\"]}.\n"
    "`period` is the person's words unchanged: \"past week\" is the last 7 days, \"last week\" the previous Monday to Sunday.\n"
    "Not for: days ranked, counted or filtered (wettest day, days over 35°C, rain per month): days. Values at set times: readings. "
    "A reading against rain: relate. Now: current. Ahead: forecast.\n"
    "Result: `series` by name; quote low_text, high_text with low_when, low_date, and day_look's emoji. A chart's `caption` is "
    "ready; with complete: true, `reply` is the answer.")


def _boolean(description: str) -> dict:
    return {"type": "boolean", "description": description}


ARGUMENTS = {   # every argument but the readings and the period, as a model reads them
    "average": _boolean("true when the question asks for an average or mean: adds each reading's average over the period (weighted "
                        "by time) and per day or month, and draws the chart as each day's average. Air readings always have theirs. "
                        "Default false."),
    "every_day": _boolean("true to list every day of a period of up to 31 days; otherwise a period over 7 days lists only the days of "
                          "its lows and highs. Default false."),
    "chart": _boolean("Draw a chart. Default: on for periods of 3 days or more."),
    "chart_readings": {"type": "array", "items": {"type": "string"},
                       "description": "What to draw, top to bottom, when not all of `readings`: names or words as for readings; \"all\" "
                                      "is every weather-station reading in the station's usual order; add \":rating\" to an air-quality "
                                      "reading to draw its traffic-light rating (\"air.pm2_5:rating\"). Default: all of readings."},
    "layout": {"type": "string", "enum": list(LAYOUTS),
               "description": "auto (or panels): one reading as a line for each sensor that has it (wind with its compass rose), several "
                              "as a panel each on one time axis. two_axes: two readings on one panel, a scale each (or more in exactly two "
                              "units). same_axis: readings in one unit over each other. Default auto."},
    "invert_second_axis": _boolean("With layout two_axes: the right-hand scale runs top to bottom."),
    "chart_answers_it": _boolean("true only when the question is just to see this chart and its figures: nothing else to answer (\"plot "
                                 "pressure this week\", not \"plot pressure this week: is a storm coming?\"). Then the result carries the "
                                 "finished reply (`reply`, `complete`: true)."),
}


def tools(ctx: Context) -> list[ServiceTool]:
    return [Summary(ctx).tool]


# ---------- the call as read ----------
@dataclass
class Call:
    """A call as the tool read it: every argument resolved and every default filled in."""
    readings: list[Reading]
    drawn: list[tuple[Reading, bool]]   # chart_readings: each reading, and whether it is drawn as its rating
    period: Period
    when: dict                          # the period as drawn_by states it: {"period": words} or {"start", ["end"]}
    average: bool
    every_day: bool
    chart: bool
    layout: str
    invert: bool
    answers: bool                       # chart_answers_it

    @property
    def replays(self) -> bool:
        """Can the period be swapped for another (drawn_by.periods)? Words can; a start or end with a time can't."""
        return "period" in self.when

    def arguments(self) -> dict:
        """The call as drawn_by states it: the readings' full names, the period in words, every default filled in."""
        return {"readings": [r.name for r in self.readings], **self.when, "average": self.average, "every_day": self.every_day,
                "chart": True, "chart_readings": [r.name + (RATING if rating else "") for r, rating in self.drawn],
                "layout": self.layout, "invert_second_axis": self.invert, "chart_answers_it": self.answers}


def _flag(args: dict, name: str) -> bool | None:
    """A true-or-false argument: None when it isn't sent."""
    value = args.get(name)
    if value is None or isinstance(value, bool):
        return value
    if str(value).strip().lower() in ("true", "false"):
        return str(value).strip().lower() == "true"
    raise Refusal(f"`{name}` is true or false, not {value!r}.")


def _words(raw) -> list[str]:
    """The names in a readings argument: a list of them, or one string of them separated by commas or "and"."""
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return [w for item in items for w in (part.strip() for part in re.split(r"[,;+&]| and ", str(item))) if w]


def _period_words(args: dict) -> dict:
    """The period as drawn_by states it: words as sent; whole days ('YYYY-MM-DD' start and end, or a start alone) as words too ("2026-11-01
    to 2026-11-05", "2026-11-01 to now"), so another period can be swapped in; a start or end with a time as it came."""
    period, start, end = (str(args[k]).strip() if args.get(k) is not None and str(args[k]).strip() else None
                          for k in ("period", "start", "end"))
    if start is None or period:   # (`period` wins over start and end sent beside it: common.read_period)
        return {"period": period}
    if DATE.fullmatch(start) and (end is None or DATE.fullmatch(end)):
        return {"period": f"{start} to {end or 'now'}"}
    return {"start": start, **({"end": end} if end else {})}


# ---------- what each engine calls a reading ----------
def _engine_key(r: Reading) -> str:
    """HistoryQuery's name for a station reading: its "group.field", as the group names it (rain is the running daily total,
    "rainfall.daily"; indoor's apparent temperature "indoor.app_tempin")."""
    return f"{r.sensor}.{field_of(r.sensor, r.column)}"


def _station_series(r: Reading) -> str | None:
    """The station reading as HistoryQuery charts it (lib/series.py's name: temperature for both sides, wind for its speed, gusts and
    direction), None for one it draws only alone (rain rate, absolute pressure, apparent temperature)."""
    if r.sensor == "wind":
        return "wind"
    if r.combine == "rain":
        return "rain" if r.sensor == "rainfall" else None
    return r.series if r.series in WEATHER else None


def _composer_series(r: Reading) -> str | None:
    """The reading as plot_chart names it, None when it can't draw it: the station's outdoor readings only (WEATHER), the air sensor's
    all."""
    if r.device == AIR_DEVICE:
        return r.column
    if r.sensor == "wind":
        return "wind"
    if r.name == "rainfall.rain":
        return "rain"
    return r.series if r.series in WEATHER and WEATHER[r.series].group == r.sensor else None


def _number(raw):
    """A figure as the station writes it ("12.3", "62") as a number."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return raw
    return int(value) if str(raw).lstrip("-").isdigit() else value


def _text(value, unit: str) -> str:
    return f"{value} {unit}".strip()


# ---------- the station's engine ----------
class _HeldOnly(Fetcher):
    """A Fetcher that asks Ecowitt for nothing: request _meta fetch false answers from what is held."""

    async def _fetch(self, cycle, start, end, groups):
        return None

    def sibling(self, groups: list[str]) -> "Fetcher":
        return _HeldOnly(self.api, self.storage, self.recent, self.mac, groups, self.tz, self.urgent, self.has_minutes)


def _rain_on(r, mac: str, plan: list[tuple[str, int, int]], start: int, end: int) -> Held:
    """The rain counter's reads for its bars, as history_on makes them when a chart has rain: its readings of the plan, its settled
    30-minute readings and its daily records."""
    held = Held({}, {})
    for cycle, a, b in plan:
        readings_on(r, mac, ["rainfall"], cycle, a, b, held.store, ["daily"])
    held.rain = slot_values_on(r, mac, "rainfall", "daily", "30min", start, end)[0]
    held.rain_daily = slot_values_on(r, mac, "rainfall", "daily", "1day", start - 86400, end + 86400)[0]
    return held


class _Query(HistoryQuery):
    """HistoryQuery as summary runs it: a chart only when the call draws one here (`drawn`), of the readings drawn alone (`keys`, its
    "group.field" names; a reading that isn't drawn gives no line), and the rain's bars whenever rain is summarised, chart or not."""

    def __init__(self, fetcher: Fetcher, args: dict, turn: Turn, *, drawn: bool, keys: set[str] | None):
        super().__init__(fetcher, args, turn)
        self.drawn, self.keys = drawn, keys
        self.plan: list[tuple] = []

    async def _hold_period(self):
        self.plan = await super()._hold_period()
        return self.plan

    def _drawing(self, plan):
        return super()._drawing(plan) if self.drawn else ("none", 0)

    def _add_charts(self, out: dict, plottable: dict):
        if self.drawn:
            super()._add_charts(out, plottable)

    def _series_entry(self, k: str):
        return None if self.keys is not None and k not in self.keys else super()._series_entry(k)

    async def rain(self) -> Bars:
        """The rain that fell, in the bars a chart of the period draws (rain_bars): the chart's own, else worked out the same way."""
        if not self.rain_bars.x:
            f = self.f
            plan = [(cycle, f.epoch(a), f.epoch(b)) for cycle, a, b in self.plan]
            held = await f.stored(_rain_on, f.mac, plan, f.epoch(self.start), f.epoch(self.end))
            self.store.setdefault("rainfall.daily", held.store.get("rainfall.daily", {"unit": "mm", "pts": {}}))
            self.rain_bars = self._rain_bars(held)
        return self.rain_bars


# ---------- the caption (lib/report.py's chart_caption, its wording kept) ----------
def _year(text: str | None) -> str:
    """"Tue 29 Sep 2026" -> "Tue 29 Sep"."""
    return re.sub(r" 20\d\d\b", "", text or "")


def _period_line(text: str) -> str:
    """"Tue 29 Sep 2026 - Tue 6 Oct 2026" -> "Tue 29 Sep – Tue 6 Oct 2026" (the first year is dropped only when it is the same year)."""
    years = re.findall(r" (20\d\d)\b", text)
    if len(years) == 2 and years[0] == years[1]:
        text = re.sub(r" 20\d\d\b(?= - )", "", text)
    return text.replace(" - ", " – ")


def _at(entry: dict, which: str) -> str:
    """"24.3 °C, Tue 29 Sep around 3pm" for one record of a series."""
    when = _year(f"{entry.get(f'{which}_date', '')} {entry.get(f'{which}_when', '')}".strip())
    return f"{entry[which]} {unit_text(entry.get('unit')) or ''}".strip() + (f", {when}" if when else "")


def _range(label: str, entry: dict, average: bool) -> str:
    unit = unit_text(entry.get("unit")) or ""
    if average and entry.get("average"):
        return f"{label}: average {entry['average']} {unit} (low {entry['low']}, high {entry['high']})".replace(" )", ")")
    return f"{label}: low {_at(entry, 'low')} · high {_at(entry, 'high')}"


def _rain_line(charts: list[Chart], tz, bars: Bars | None = None) -> list[str]:
    """Least and most rain in a period (the chart's own bars, else `bars`: those a chart of the period would draw)."""
    bars = next((p.bars for c in charts for p in c.panels if p.bars and p.bars.x), bars)
    if not bars or not bars.x:
        return []

    def day(i: int) -> str:
        start = to_local(bars.x[i], tz)
        return day_text(start, year=False) + (f" {clock_text(start, whole_hours=True)}" if bars.width < 86400 else "")
    lo, hi = min(range(len(bars.y)), key=bars.y.__getitem__), max(range(len(bars.y)), key=bars.y.__getitem__)
    per = f" per {bars.per}" if bars.per else ""
    return [f"☔ Rain{per}: least {bars.y[lo]:g} mm ({day(lo)}) · most {bars.y[hi]:g} mm ({day(hi)}) · {sum(bars.y):.1f} mm in all"]


def _weather_lines(series: dict, names: list[str], average: bool, charts: list[Chart], tz) -> list[str]:
    """A line per series of each reading drawn (HistoryQuery's series: "group.field" names, figures as the station writes them)."""
    lines: list[str] = []
    for name in [n for n in dict.fromkeys(names) if find(n)]:
        reading = find(name)
        keys = [k for k in series if k.split(".", 1)[1] == reading.field]
        if reading.field == "daily":   # rain: the amounts the chart draws
            lines += _rain_line(charts, tz)
            continue
        for key in keys:
            group = key.split(".", 1)[0]
            label = reading.label + (f" ({group})" if len(keys) > 1 and group in ("outdoor", "indoor") else "")
            if reading.field == "wind_speed":
                gust = series.get(f"{group}.wind_gust") or series.get("wind.wind_gust")
                lines.append(f"🌬️ Wind: strongest gust {_at(gust, 'high')}" if gust else _range(label, series[key], average))
            else:
                lines.append(_range(label, series[key], average))
        if reading.field == "wind_speed" and (d := series.get("wind.wind_direction")):
            lines.append(f"🧭 Mostly from the {d['most_common']}" + (f", {d['steadiness']}" if d.get("steadiness") else ""))
    return lines


def _air_lines(result: dict, metrics: list[str]) -> list[str]:
    """A line per air-quality metric drawn (AirGradient.history's result): its peak, the peak's rating and the average."""
    lines = []
    for key, label in ((name, m.label) for name, m in AIR.items()):
        entry = result.get(key)
        if key not in (metrics or ["pm2_5"]) or not isinstance(entry, dict):
            continue
        unit = unit_text(entry.get("unit")) or ""
        rating = entry.get("high_rating")
        lines.append(f"• {label}: peak {entry['high']} {unit} ({_year(entry.get('high_time'))})".replace(" )", ")")
                     + (f" · {rating}" if rating else "") + f" · average {entry['average']}")
    return lines


def _series_label(field: str) -> str:
    """A series' name: its reading's label ("Temperature"), the gusts their own ("Wind gust")."""
    if field == "wind_gust":
        return "Wind gust"
    return reading.label if (reading := find(field)) else field.replace("_", " ").capitalize()


def _extremes_lines(series: dict, names: list[str]) -> list[str]:
    """The low and high (or the average) of each reading named, each with its day and time: wind, its speed and its gusts. A reading on
    both sides (indoor, outdoor) has a line for each (lib/report.py's plain lookup of a weather_history result, "extremes")."""
    wanted = [f for r in filter(None, map(find, names)) for f in (r.field, r.band_field) if f] or ["temperature"]
    keys = [k for k in series if k.split(".", 1)[1] in wanted and "low" in series[k]]
    fields = [k.split(".", 1)[1] for k in keys]
    lines = []
    for key in keys:
        group, field = key.split(".", 1)
        label = _series_label(field) + (f" ({group})" if fields.count(field) > 1 else "")
        lines.append(f"• {_range(label, series[key], 'average' in series[key])}")
    return lines


def _caption(period: str, lines: list[str], note_missing: bool, warning: bool, more: list[str] = ()) -> str | None:
    """The caption: the period, a line per reading, and what the figures lack; None with no line to write."""
    if not lines:
        return None
    notes = (["(Older days are still downloading, so they are left out.)"] if note_missing else
             ["(Some data couldn't be fetched, so this may be incomplete.)"] if warning else [])
    return "\n".join([_period_line(period), *lines, *more, *notes]).strip()


# ---------- the tool ----------
class Summary:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.tool = ServiceTool(NAME, DESCRIPTION, self.parameters(), self.handle, open_world=True)

    def parameters(self) -> dict:
        names = self.ctx.readings().description()
        rating = (" An air-quality reading with \":rating\" (\"air.pm2_5:rating\") is summarised and drawn as its traffic-light "
                  "rating.") if self.ctx.air is not None else ""
        return {"type": "object", "properties": {
            "readings": {"type": "array", "items": {"type": "string"},
                         "description": f"What to summarise. {names}{rating} Default: outdoor and indoor temperature and humidity."},
            **period_properties("today; with chart true and no period, the last 24 hours"),
            **ARGUMENTS}}

    # ---------- reading the call ----------
    async def read(self, args: dict) -> Call:
        """The call as read, or a Refusal that teaches the right one."""
        ctx = self.ctx
        props = self.tool.parameters["properties"]
        if unknown := [k for k in args if k not in props]:
            hint = " Send the period as `period` (words) or `start` and `end`." if {"start_date", "end_date"} & set(unknown) else \
                " Send the readings as `readings`." if {"metrics", "groups", "metric"} & set(unknown) else ""
            raise Refusal(f"summary doesn't take {', '.join(map(repr, unknown))}.{hint}", arguments=list(props))
        catalogue = ctx.readings()
        default = list(DEFAULT_READINGS) if ctx.eco else "air"
        sent = _words(args.get("readings"))
        rated = any(w.lower().endswith(RATING) for w in sent)   # "air.pm2_5:rating" in readings: summarised, and drawn as its rating
        plain = [w[:-len(RATING)].strip() if w.lower().endswith(RATING) else w for w in sent]
        readings = catalogue.resolve(plain if rated else args.get("readings"), default=default)
        drawn = self._drawn(args.get("chart_readings") if _words(args.get("chart_readings")) or not rated else sent, readings)
        for r in {r for r in [*readings, *(r for r, _ in drawn)]}:
            if (r.device == STATION and ctx.eco is None) or (r.device == AIR_DEVICE and ctx.air is None):
                device = "weather station" if r.device == STATION else "air sensor"
                raise Refusal(f"There is no {device} here, so {r.name} can't be read.", names=catalogue.names)
        layout = str(args.get("layout") or "auto").strip().lower()
        if layout not in LAYOUTS:
            raise Refusal(f"`layout` is one of {', '.join(LAYOUTS)}, not {args.get('layout')!r}.", layouts=list(LAYOUTS))
        chart = _flag(args, "chart")
        if chart is None and any(rating for _, rating in drawn):   # a rating is only ever drawn
            chart = True
        asked = any(str(args.get(k) or "").strip() for k in ("period", "start", "end"))
        period = read_period(args, tz=ctx.zone, now=ctx.now(), default="last 24 hours" if chart and not asked else "today",
                             first_held=await self._first_held([*readings, *(r for r, _ in drawn)]))
        long = (period.last_day - period.first_day).days >= CHART_MIN_DAYS - 1
        when = _period_words(args) if asked else {"period": period.asked}
        return Call(readings, drawn, period, when, bool(_flag(args, "average")), bool(_flag(args, "every_day")),
                    long if chart is None else chart, layout, bool(_flag(args, "invert_second_axis")), bool(_flag(args, "chart_answers_it")))

    def _drawn(self, raw, readings: list[Reading]) -> list[tuple[Reading, bool]]:
        """chart_readings as read: each reading named (words too; "all"), in order, each once, with whether it is drawn as its rating."""
        words = _words(raw)
        if not words:
            return [(r, False) for r in readings]
        out: dict[tuple[str, bool], tuple[Reading, bool]] = {}
        for word in words:
            rating = word.lower().endswith(RATING)
            base = word[:-len(RATING)].strip() if rating else word
            for r in self.ctx.readings().resolve(base, default=base):
                if rating and (r.device != AIR_DEVICE or r.column not in AIR_QUALITY):
                    raise Refusal(f"Only the air sensor's air-quality readings have a rating ({', '.join(f'air.{m}' for m in AIR_QUALITY)}): "
                                  f"{r.name} can't be drawn as one. Draw it as it is: {r.name!r}.")
                out.setdefault((r.name, rating), (r, rating))
        if not out:
            raise Refusal(f"chart_readings names no reading: send names or words as for readings, such as \"pressure\" or "
                          f"\"air.pm2_5{RATING}\".", names=self.ctx.readings().names)
        return list(out.values())

    async def _first_held(self, readings: list[Reading]) -> date | None:
        """The first day held of these readings' sensors ("on record" starts there)."""
        ctx, storage = self.ctx, self.ctx.storage
        if storage is None:
            return None
        groups = sorted({r.sensor for r in readings if r.device == STATION})
        air = any(r.device == AIR_DEVICE for r in readings) and ctx.air is not None

        def first(r) -> date | None:
            sensors = [s for g in groups if ctx.eco and (s := r.sensor(ctx.eco.mac, g)) is not None and s.kind in KINDS]
            if air and (s := ctx.air.store.sensor) is not None:
                sensors.append(s)
            days = [r.db.execute(f"SELECT MIN(day) FROM {s.kind}_day WHERE sensor = ?", (s.id,)).fetchone()[0] for s in sensors]
            return min((date.fromisoformat(d) for d in days if d), default=None)
        return await storage.read(first)

    def _route(self, call: Call) -> str | None:
        """Which engine draws the chart: "station" (HistoryQuery), "air" (AirGradient.history), "composer" (plot_chart), or None
        (no chart). Refuses a chart that can't be drawn as asked, saying what can."""
        if not call.chart:
            return None
        station = [r for r, _ in call.drawn if r.device == STATION]
        air = [r for r, _ in call.drawn if r.device == AIR_DEVICE]
        rating = any(rating for _, rating in call.drawn)
        if call.invert and call.layout != "two_axes":
            raise Refusal("invert_second_axis needs layout two_axes: it turns the right-hand scale upside down.")
        lines = [(r, rating) for r, rating in call.drawn if r.reading != "wind_direction"]
        if call.layout in ("two_axes", "same_axis") and (rating or any(r.combine == "rain" for r, _ in lines)):
            raise Refusal(f"layout {call.layout} draws lines: rain (bars) and ratings can't share a panel. Use layout auto, a panel each.")
        composer = (station and air) or rating or call.layout == "same_axis" or (air and call.layout == "two_axes")
        if composer:
            if self.ctx.eco is None:
                raise Refusal(f"layout {call.layout} is drawn only where there is a weather station: use layout auto.")
            if bad := [r.name for r, _ in lines if _composer_series(r) is None]:
                drawable = [r.name for r in self.ctx.readings() if _composer_series(r)]
                raise Refusal(f"{', '.join(bad)} can't go on a chart that mixes the two sensors, draws a rating or uses same_axis: such a "
                              f"chart draws only these readings. Draw {', '.join(bad)} in a call of its own.", drawable=drawable)
            series = list(dict.fromkeys((_composer_series(r), rating) for r, rating in lines))
            if len(series) > MAX_PANELS:
                raise Refusal(f"A chart that mixes the two sensors, draws a rating or uses same_axis takes at most {MAX_PANELS} readings, "
                              f"not {len(series)}: draw the rest in another call.")
            if call.layout in ("two_axes", "same_axis") and len(series) < 2:
                raise Refusal(f"layout {call.layout} needs two readings or more in chart_readings (it puts them on one panel).")
            return "composer"
        if station:
            if any(r.sensor == "rainfall_piezo" for r in station):
                raise Refusal("The piezo rain gauge isn't drawn: draw rainfall.rain, the main gauge.")
            series = list(dict.fromkeys(_station_series(r) or r.name for r in station))
            alone = [r.name for r in station if _station_series(r) is None]
            if alone and len(series) > 1:
                raise Refusal(f"{', '.join(alone)} can only be drawn on a chart of its own: send it alone in chart_readings.")
            if call.layout == "two_axes" and len(series) < 2:
                raise Refusal("layout two_axes needs two readings or more in chart_readings (it puts them on one panel, a scale each); "
                              "outdoor and indoor of one reading are already one panel.")
            return "station"
        return "air"

    # ---------- answering ----------
    async def handle(self, args: dict, meta: dict) -> str:
        ctx = self.ctx
        call = await self.read(args)
        route = self._route(call)
        if route == "composer" and self._whole_days(call) is None:
            yesterday = ctx.now().date() - timedelta(days=1)
            raise Refusal(f"A chart that mixes the two sensors, draws a rating or uses same_axis is drawn over whole days, up to "
                          f"yesterday ({day_text(yesterday)}): {call.period.text} has none. Ask for a period with whole days ('last 24 "
                          f"hours' draws yesterday, 'last 7 days'), or leave the chart out.")
        background, fetch = meta.get("priority") == BACKGROUND, meta.get("fetch", True)
        station = [r for r in call.readings if r.device == STATION]
        air = [r for r in call.readings if r.device == AIR_DEVICE]
        drawn_station = [r for r, _ in call.drawn if r.device == STATION] if route in ("station", "composer") else []
        drawn_air = [r for r, _ in call.drawn if r.device == AIR_DEVICE] if route in ("air", "composer") else []
        charts: list[Chart] = []
        found: dict = {"missing": [], "warning": False, "notes": {}}
        q = out = None
        if station or drawn_station:
            q, out = await self._station(call, station, drawn_station, route == "station", background, fetch, charts, found)
        air_out = None
        if air or drawn_air:
            figures = list(dict.fromkeys([*air, *(drawn_air if route == "composer" else [])]))
            air_out = await self._air(call, figures, drawn_air if route == "air" else [], charts, found)
        if route == "composer":
            await self._compose(call, charts, found)
        looks = await self._looks(call.period.first_day, call.period.last_day)
        series, no_readings = {}, []
        rain_total = None
        for r in call.readings:
            if r.device == STATION:
                entry = await self._station_entry(r, q, out, looks, call) if q else None
                if r.name == "rainfall.rain" and entry:
                    rain_total = entry["total"]
            else:
                entry = self._air_entry(r, air_out, looks)
            if entry:
                series[r.name] = entry
            else:
                no_readings.append(r.name)
        if not series and not charts:
            first = await self._first_held(call.readings)
            raise Refusal(f"Nothing is held of {', '.join(no_readings)} for {call.period.text}."
                          + (f" What is held starts on {day_text(first)}." if first else ""),
                          **({"held_from": first.isoformat()} if first else {}),
                          **({"missing": found["missing"][:10]} if found["missing"] else {}))
        fields: dict = {"series": series}
        if rain_total is not None:
            fields["rain_total_mm"] = rain_total
        if out and out.get("monthly_note"):
            fields["monthly_note"] = out["monthly_note"]
        if no_readings:
            fields["note_no_readings"] = (f"Nothing held of {', '.join(no_readings)} for this period: say so." if not series else
                                          f"Nothing held of {', '.join(no_readings)} for this period; the others are summarised.")
        if call.chart and not charts and "chart" not in found["notes"]:
            fields["note_chart"] = ("No chart: an average chart draws each whole day's average, and this period has fewer than two whole "
                                    "days before today." if call.average else "No chart: too few readings to draw for this period.")
        if found["notes"]:
            fields["notes"] = found["notes"]
        if found["missing"]:
            fields["missing"] = found["missing"][:10]
        if found["warning"]:
            fields["warning"] = "Some data could not be fetched; the answer may be incomplete. Say so."
        caption = self._caption(call, route, charts, out, air_out, found) if charts else None
        if caption:
            fields["caption"] = caption
        elif not charts and (text := self._text(call, q, out, air_out)):
            fields["text"] = text
        finished = call.answers and charts and caption and not self._needs_words(fields) and not call.period.note   # (the model reads it)
        fields.update(self._charts(call, route, charts, meta))
        if finished:
            fields.update(complete(caption))
        return answer(period=call.period, as_of=ctx.now(), **fields)

    def _span(self, call: Call) -> tuple[str, str]:
        """The period as the engines take it ('YYYY-MM-DD HH:MM:SS', both ends included): to its last second, or, for a period that runs
        to now, to now itself (the reading of this very minute is in it, as it always was)."""
        start, end = call.period.as_naive_strings()
        now = self.ctx.now().replace(microsecond=0)
        if call.period.end >= now:
            end = f"{now.astimezone(self.ctx.zone):%Y-%m-%d %H:%M:%S}"
        return start, end

    async def _station(self, call: Call, station: list[Reading], drawn: list[Reading], here: bool, background: bool, fetch: bool,
                       charts: list[Chart], found: dict) -> tuple[_Query | None, dict | None]:
        """HistoryQuery over the station's readings summarised and drawn, drawing its chart when `here`."""
        eco = self.ctx.eco
        every = [*station, *drawn]
        groups = list(dict.fromkeys(r.sensor for r in every))
        named = [s for s in dict.fromkeys(_station_series(r) or r.column for r in drawn if here)]
        series_names = list(dict.fromkeys(_station_series(r) or r.column for r in every))
        turn = Turn(chart_asked=here, chart_field=named[0] if len(named) == 1 else None, chart_fields=named if len(named) > 1 else [],
                    two_axes=here and call.layout == "two_axes", average_asked=call.average, per_day=call.every_day,
                    readings=series_names, text="", ahead=background)
        start, end = self._span(call)
        args = {"start_date": start, "end_date": end, "average": call.average,
                "include_derived": sorted({r.column for r in every if r.column in DERIVED})}
        make = Fetcher if fetch else _HeldOnly
        fetcher = make(eco.api, eco.storage, eco.fetches, eco.mac, groups, eco.tz, not background, eco.has_minutes)
        keys = {k for r in drawn for k in (("wind.wind_speed",) if r.sensor == "wind" else (_engine_key(r),))} if here else None
        q = _Query(fetcher, args, turn, drawn=here, keys=keys)
        text = await q.run()
        if text.startswith("Error"):
            if "within the last" in text:   # (the period's start is checked against the history kept: only one before it all is refused)
                start = kept_from("1day", self.ctx.now().date())
                raise Refusal(f"The weather station's history goes back {CYCLES['1day'].kept // 365} years, to {day_text(start)}: "
                              f"{call.period.text} is before it.", held_from=start.isoformat())
            if "Details: " in text:   # nothing held, and what was asked for couldn't be fetched
                found["missing"] += text.split("Details: ", 1)[1].split("; ")
                found["warning"] = True
            return None, None
        out = json.loads(text)
        for chart in turn.charts:
            chart.source = chart.source or ECOWITT_SOURCE
            if call.invert:
                for panel in chart.panels:
                    panel.flip_right = panel.flip_right or any(line.right for line in panel.lines)
        charts += turn.charts
        if out.get("missing"):
            found["missing"] += out["missing"]
        found["warning"] = found["warning"] or bool(out.get("warning"))
        if any(r.name == "rainfall.rain" for r in station):
            await q.rain()
        return q, out

    async def _air(self, call: Call, figures: list[Reading], drawn: list[Reading], charts: list[Chart], found: dict) -> dict | None:
        """AirGradient.history over the air readings whose figures are wanted (and drawn, when the chart is the air sensor's alone). Its
        `metrics` are what it draws, and bring in its own temperature and humidity: a reading summarised but not drawn that is one of
        those takes a call of its own."""
        start, end = self._span(call)
        metrics = [r.column for r in drawn]
        wanted = [r.column for r in figures]
        out: dict = {}
        try:
            if drawn:
                turn = Turn(air_named=metrics)
                out = await self.ctx.air.history(start, end, chart=True, metrics=metrics, turn=turn, daily=call.average)
                for chart in turn.charts:
                    chart.source = chart.source or AIR_SOURCE
                charts += turn.charts
            if not drawn or (out.get("readings") and any(m not in out for m in wanted)):
                more = await self.ctx.air.history(start, end, chart=False, metrics=wanted or None, turn=Turn())
                out = {**more, **out} if drawn else more
        except Exception as e:   # the sensor or its cloud is down: the station's figures still stand
            found["missing"].append(f"air sensor: {type(e).__name__}: {e}")
            found["warning"] = True
            return None
        if "error" in out:
            found["missing"].append(f"air sensor: {out['error']}")
            return None
        notes = {k.removeprefix("note_") if k != "note" else "none": v for k, v in out.items() if k == "note" or k.startswith("note_")}
        if notes:
            found["notes"]["air"] = notes
        return out

    async def _compose(self, call: Call, charts: list[Chart], found: dict):
        """The chart plot_chart draws: whole days up to yesterday, so a period that runs into today is cut, and said so."""
        ctx = self.ctx
        composer = ctx.composer or Composer(ctx.eco, ctx.air)
        first, last = self._whole_days(call)
        panels, seen = [], set()
        for r, rating in call.drawn:
            name = _composer_series(r)
            if name is None or (r.reading == "wind_direction") or (name, rating) in seen:
                continue
            seen.add((name, rating))
            panels.append({"series": name, "style": "rating" if rating else "bars" if name == "rain" else "line"})
        args = {"panels": panels, "start_date": first.isoformat(), "end_date": last.isoformat(),
                "two_axes": call.layout == "two_axes", "same_axis": call.layout == "same_axis", "invert_second_axis": call.invert}
        turn = Turn()
        result = json.loads(await composer.plot_chart(args, turn))
        if "error" in result:   # (what plot_chart refuses is checked first: this is a reading it has nothing of)
            found["notes"]["chart"] = f"No chart: {result['error'].rstrip('.')}."
            return
        for chart in turn.charts:   # the devices its readings come from (plot_chart leaves out a rating's)
            chart.source = " and ".join(name for name, device in ((ECOWITT_SOURCE, STATION), (AIR_SOURCE, AIR_DEVICE))
                                        if any(r.device == device for r, _ in call.drawn))
        charts += turn.charts
        cut, early = last < call.period.last_day, call.period.start.time() != time()
        if cut or early:   # (the air sensor's own notes on the days it lacks are in its figures' notes already)
            found["notes"]["chart"] = (f"The chart draws whole days, {day_text(first, year=False)} to {day_text(last, year=False)}"
                                       + (": a chart that mixes the two sensors, draws a rating or uses same_axis is drawn over whole days "
                                          "up to yesterday, as today isn't over" if cut else "") + ".")

    def _whole_days(self, call: Call) -> tuple[date, date] | None:
        """The first and last day plot_chart draws for this period: its days up to yesterday (None when that leaves none)."""
        first, last = call.period.first_day, min(call.period.last_day, self.ctx.now().date() - timedelta(days=1))
        return (first, last) if first <= last else None

    async def _looks(self, first: date, last: date) -> Callable[[date | None], dict | None]:
        """day -> its look (daylook.day_look over its day row's figures), for the days of the period; nothing without the station."""
        eco = self.ctx.eco
        if eco is None:
            return lambda day: None
        days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
        values, _ = await eco.storage.read(day_values_on, eco.mac, list(FIGURES), days)
        today = self.ctx.now().date()

        def look(day: date | None) -> dict | None:
            if day is None:
                return None
            return day_look({n: values[n].get(day) for n in FIGURES}, latitude=eco.latitude, day=day, so_far=day >= today)
        return look

    # ---------- the figures ----------
    async def _station_entry(self, r: Reading, q: _Query, out: dict, looks, call: Call) -> dict | None:
        """One station reading as the service gives it."""
        key = _engine_key(r)
        if r.name == "rainfall.rain":
            return await self._rain_entry(q, looks, call) if key in q.units else None
        got = (out.get("series") or {}).get(key) or q.direction.get(key)
        if not got:
            return None
        if r.combine == "rain":   # the piezo gauge: its running daily total, whose highest is a day's total
            got = {k: v for k, v in got.items() if not k.startswith("low")} | {
                "how_to_read": "The gauge's running daily total: its high is the most rain in a day. Its total over the period is "
                               "rainfall.rain's, the main gauge."}
        if r.reading == "wind_direction":
            return {k: got[k] for k in ("most_common", "then", "average_direction", "steadiness", "calm", "note", "note_period", "daily")
                    if k in got}
        unit = unit_text(got.get("unit")) or r.unit
        entry = {"unit": unit}
        for want in ("low", "high"):
            if want not in got:
                continue
            ext = q.overall.get(key, {}).get(want)
            entry[want] = _number(got[want])
            entry[f"{want}_text"] = _text(got[want], unit)
            entry[f"{want}_when"] = got[f"{want}_when"]
            if got.get(f"{want}_date"):
                entry[f"{want}_date"] = got[f"{want}_date"]
            if ext and (look := looks(local_date(ext.ts, q.tz))):
                entry[f"{want}_day_look"] = look
            if f"{want}_note" in got:
                entry[f"{want}_note"] = got[f"{want}_note"]
        if "average" in got:
            entry["average"], entry["average_text"] = _number(got["average"]), _text(got["average"], unit)
        if "how_to_read" in got:
            entry["how_to_read"] = got["how_to_read"]
        days = {q._label(d.day): d.day for d in q.period[key].days} if key in q.period else {}
        if "daily" in got:
            entry["daily"] = {label: {**{w: _number(v) for w, v in row.items() if w in ("low", "high")},
                                      **({"average": _number(row["avg"])} if "avg" in row else {}),
                                      **({"day_look": look} if (look := looks(days.get(label))) else {})}
                              for label, row in got["daily"].items()}
        if "monthly" in got:
            monthly = {}
            for label, row in got["monthly"].items():
                month = {w: _number(row[w]) for w in ("low", "high") if w in row}
                for w in ("low", "high"):
                    if row.get(f"{w}_date"):
                        month[f"{w}_when"], month[f"{w}_date"] = row[f"{w}_when"], row[f"{w}_date"]
                        ext = q.monthly.get(key, {}).get(label, {}).get(w)
                        if ext and (look := looks(local_date(ext.ts, q.tz))):
                            month[f"{w}_day_look"] = look
                if "avg" in row:
                    month["average"] = _number(row["avg"])
                monthly[label] = month
            entry["monthly"] = monthly
        return entry

    async def _rain_entry(self, q: _Query, looks, call: Call) -> dict:
        """Rain as the mm that fell: the total, the wettest day, and a row per day or month (each the sum of the chart's bars in it)."""
        bars = await q.rain()
        total = round(sum(bars.y), 1)
        per_day: dict[date, float] = {}
        rows: dict[str, float] = {}
        for t, mm in zip(bars.x, bars.y):
            day = local_date(t, q.tz)
            per_day[day] = per_day.get(day, 0.0) + mm
            rows[q._label(day)] = rows.get(q._label(day), 0.0) + mm
        entry: dict = {"unit": "mm", "total": total, "total_text": f"{total:.1f} mm"}
        wettest = max(per_day, key=per_day.get) if per_day and max(per_day.values()) > 0 else None
        if wettest:
            mm = round(per_day[wettest], 1)
            entry["wettest_day"] = {"date": day_text(wettest), "mm": mm, "text": f"{mm:.1f} mm",
                                    **({"day_look": look} if (look := looks(wettest)) else {})}
        if q.by_day:
            keep = None if len(per_day) <= DAILY_ALL_DAYS or call.every_day else {wettest}
            entry["daily"] = {q._label(day): {"total": round(mm, 1), **({"day_look": look} if (look := looks(day)) else {})}
                              for day, mm in per_day.items() if keep is None or day in keep}
        else:
            entry["monthly"] = {label: {"total": round(mm, 1)} for label, mm in rows.items()}
        if q.rain_from_daily:
            entry["note"] = (f"Older days come from Ecowitt's daily records, which run {daily_run(q.tz, q.start.date(), q.end.date())}, "
                             f"so their rain may fall on the day before.")
        return entry

    def _air_entry(self, r: Reading, out: dict | None, looks) -> dict | None:
        """One air reading as the service gives it: AirGradient.history's figures with their wording."""
        got = (out or {}).get(r.column)
        if not isinstance(got, dict):
            return None
        unit = unit_text(got.get("unit")) or r.unit
        entry = {"unit": unit}
        for want in ("low", "high"):
            entry[want] = got[want]
            entry[f"{want}_text"] = _text(got[want], unit)
            try:
                at = datetime.strptime(got.get(f"{want}_time") or "", "%a %d %b %Y %I:%M%p")
            except ValueError:
                continue
            entry[f"{want}_when"], entry[f"{want}_date"] = f"at {clock_text(at, whole_hours=True)}", day_text(at)
            if look := looks(at.date()):
                entry[f"{want}_day_look"] = look
        entry["average"], entry["average_text"] = got["average"], _text(got["average"], unit)
        for k in ("high_rating", "average_rating", "high_aqi_us", "high_band", "average_aqi_us", "average_band"):
            if k in got:
                entry[k] = got[k]
        return entry

    # ---------- the chart's words and its replay ----------
    def _caption(self, call: Call, route: str, charts: list[Chart], out: dict | None, air_out: dict | None, found: dict) -> str | None:
        """What lib/report.chart_caption writes under this chart today: the period, a line per reading drawn, and what the figures
        lack. A chart plot_chart draws (no caption today) is written the same way, each reading's line as its own tool's, with the
        chart's period when it is cut."""
        tz = self.ctx.zone
        series = (out or {}).get("series") or {}
        drawn_keys = {k for r, _ in call.drawn if r.device == STATION
                      for k in ((("wind.wind_speed", "wind.wind_gust", "wind.wind_direction") if r.sensor == "wind" else (_engine_key(r),)))}
        series = {k: v for k, v in series.items() if k in drawn_keys}
        lines: list[str] = []
        if route == "air":
            lines = _air_lines(air_out or {}, [r.column for r, _ in call.drawn])
        elif route == "station":
            names = list(dict.fromkeys(_station_series(r) or r.column for r, _ in call.drawn))
            lines = _weather_lines(series, names, call.average, charts, tz)
        else:
            for r, _ in call.drawn:
                if r.device == AIR_DEVICE:
                    line = _air_lines(air_out or {}, [r.column])
                else:
                    line = _weather_lines(series, [_composer_series(r) or ""], call.average, charts, tz)
                lines += [x for x in line if x not in lines]
        period = (out or air_out or {}).get("period", "")
        more = [f"({found['notes']['chart']})"] if route == "composer" and "chart" in found["notes"] else []
        return _caption(period, lines, bool(air_out and air_out.get("note_missing")), bool(found["warning"]), more)

    def _text(self, call: Call, q: _Query | None, out: dict | None, air_out: dict | None) -> str | None:
        """The figures in words, ready to send, when no chart is drawn: the period, then each station reading's low and high (or its
        average) with its day and time, as the bot writes a weather_history lookup today ("extremes"); rain as the least and most in a
        day (or hour, or 6 hours) and its total, as under a rain chart; each air-quality reading's peak, rating and average."""
        station = [r for r in call.readings if r.device == STATION]
        ranged = [r for r in station if r.combine != "rain" and r.reading != "wind_direction"]
        names = list(dict.fromkeys(name for r in ranged if (name := _station_series(r))))
        series = (out or {}).get("series") or {}
        lines = []
        if names:
            keys = {k for r in ranged for k in (("wind.wind_speed", "wind.wind_gust") if r.name == "wind.wind_speed" else (_engine_key(r),))}
            lines += _extremes_lines({k: v for k, v in series.items() if k in keys}, names)
        if (any(r.reading == "wind_direction" for r in station) and not any(r.name == "wind.wind_speed" for r in station)
                and (d := series.get("wind.wind_direction"))):   # (with the speed, as under a wind chart, the direction is the chart's)
            lines.append(f"🧭 Mostly from the {d['most_common']}" + (f", {d['steadiness']}" if d.get("steadiness") else ""))
        if q and any(r.name == "rainfall.rain" for r in station) and "rainfall.daily" in q.units:
            lines += _rain_line([], q.tz, q.rain_bars)
        if air_out:
            lines += _air_lines(air_out, [r.column for r in call.readings if r.device == AIR_DEVICE])
        period = (out or air_out or {}).get("period", "")
        return "\n".join([_period_line(period), *lines]) if lines else None

    @staticmethod
    def _needs_words(fields: dict) -> bool:
        """Does the result carry a note the caption leaves out (lib/bot.py _needs_words)? Then the model answers."""
        top = {k for k in fields if k != "notes" and (k.startswith("note") or k.endswith("_note") or k in ("warning", "missing"))}
        notes = fields.get("notes") or {}
        air = set(notes.get("air") or {})
        return bool(top - CAPTIONED or air - AIR_CAPTIONED or set(notes) - {"air", "chart"})

    def _charts(self, call: Call, route: str | None, charts: list[Chart], meta: dict) -> dict:
        """The charts as data (common.with_charts), each with drawn_by: the call that draws it again and the periods it is worth
        drawing over (3.7): readings over time, 1D to 1Y (a chart of each day's average from 7D); none for a start or end with a time."""
        fields = with_charts(charts, self.ctx.zone, meta)
        if not fields:
            return {}
        daily = call.average and route in ("station", "air")   # (plot_chart draws its readings, average or not)
        periods = list(DAILY_MEANS if daily else OVER_TIME) if call.replays else []
        drawn_by = {"tool": NAME, "arguments": call.arguments(), "periods": periods}
        for chart in fields["charts"]:
            chart["drawn_by"] = drawn_by
        return fields


__all__ = ["DESCRIPTION", "Summary", "tools"]
