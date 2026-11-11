"""The `days` tool (docs/ecobot-mcp.md 3.3): days found, ranked, counted or totalled by their figures, over the weather station's and
the air sensor's day figures, or over part of each day (`hours`). It replaces weather_days and air_days, on their engines:

  - the fields are theirs: the station's (lib/ecowitt/days.py FIELDS: temp_max, rain, humidity_avg, indoor_temp_min ...) and the air
    sensor's (lib/airgradient/days.py: pm2_5_max, co2_avg ...), in one list;
  - a whole day's figures are its day rows, read as those tools read them (ecowitt.days.day_values_on, AirReadings.day_figures); part of
    each day's are lib/partday.stretch_figures, mapped to the same fields (temp_max is the stretch's highest, temp_avg its mean, rain the
    mm that fell in it);
  - finding, ranking, counting, grouping and the bars are lib/daytable.py's (prepare, analyse), and the notes are the two tools' own.

Which sensor: the fields named (`where`, `sort_by`, `of`) say. Station fields read as weather_days did; air fields as air_days did; both
(a question mixing the two) by the merged rule of 3.3: the fields shown and the default sort come from the sensors named, `days_checked`
counts the days every named sensor has, and the days whose station figures come only from Ecowitt's daily records (10am to 10am, not
the air sensor's midnight to midnight) are left out and counted.

The result is today's day-tool result in 3.2's shape (period, as_of, then its fields), each listed day with its `day_look`, and `answer`:
the line the bot writes today for a days lookup (lib/report.py `_days`, ported here with the same words), which is also the finished
reply when `chart_answers_it` is set and a chart drawn (unless a note needs the model's words). `group_by` draws the bar chart, which
carries the call that drew it (`drawn_by`, 3.7).

It reads the file alone and never fetches, so the request's `fetch` and `priority` change nothing (and `chart_points` leaves bars as
they are: a bar per month or year is never bucketed).
"""

import operator
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from ..airgradient.days import ALWAYS_SHOWN as AIR_SHOWN, FIELDS as AIR_UNITS
from ..daytable import MAX_LIMIT, OPS, STATS, analyse, lists_days, prepare
from ..ecowitt.api import daily_run, unit_text
from ..public_holidays import PublicHolidays
from ..ecowitt.days import ALWAYS_SHOWN as STATION_SHOWN, AVERAGED, COARSER, FIELDS as STATION_FIELDS, PARTS, day_values_on
from ..partday import ALLOWED, CLOCK_CHANGE, NOT_HELD, NOT_OVER, PartDays, Stretch, over, parse_stretch, stretch_figures
from ..specs import AIR_SOURCE, CHART_SOURCE, ECOWITT_SOURCE
from ..storage.catalogue import column_of
from ..storage.read import Reader
from ..timeutil import day_text
from ..tools import Turn
from .common import Refusal, ServiceTool, answer, complete, period_properties, read_period, sends_period, with_charts
from .context import Context
from .daylook import FIGURES as LOOK_FIGURES, day_look
from .names import AIR_DEVICE, STATION

NAME = "days"
DEFAULT, DEFAULT_HOURS = "on record", "last 30 days"   # the period read when none is sent: every day held; with hours, a month
MAX_HOURS_DAYS = 400   # the most days `hours` reads (3.3: a stretch costs far more than a day row)
STATION_UNITS = {name: f[3] for name, f in STATION_FIELDS.items()}
DEVICE_OF = {**dict.fromkeys(STATION_UNITS, STATION), **dict.fromkeys(AIR_UNITS, AIR_DEVICE)}
SHOWN = {STATION: STATION_SHOWN, AIR_DEVICE: AIR_SHOWN}   # what every listed day shows, by the sensor named
DEVICES = (STATION, AIR_DEVICE)
ONLY = {"public_holiday": "public holidays only", "weekend": "weekends only"}   # (the chart's subtitle says which)
ORDERS = ("desc", "asc")
OP_WORDS = {"==": "=", "≥": ">=", "≤": "<=", "=>": ">=", "=<": "<="}   # ops written another way
GROUP_BY_NOTE = ("One figure for the whole period. If a figure per month or per year was asked for (\"per month\", \"by year\", "
                 "\"each year\"), call again with group_by \"month\" or \"year\".")
ARGUMENTS = ("where", "sort_by", "order", "count_only", "group_by", "stat", "of", "limit", "only", "hours", "period", "start", "end",
             "chart_answers_it")
OLD_NAMES = {"start_date": "start", "end_date": "end"}   # weather_days' and air_days' names
CAPTIONED_NOTES = {"note_averaged"}   # the notes the answer line passes on itself (" Older days use averages ..."): any other needs words
AIR_PART = {"max": "hi", "min": "lo", "avg": "mean"}   # an air field's suffix -> where a day row keeps it
SOURCES = {STATION: ECOWITT_SOURCE, AIR_DEVICE: AIR_SOURCE}
# drawn_by.periods (3.7): a count or total per month over 3 months, a year and the whole record; per year, no other period
MONTH_PERIODS = (("3m", "3M"), ("1y", "1Y"), ("on record", "All"))
HOURS_HOW = ("Each day's figures are over this stretch of it, not the whole day: _max its highest, _min its lowest, _avg its mean, rain "
             "the mm that fell in it. A stretch across midnight belongs to the day it ends on. day_look is still the whole day's look.")

# the notes, worded as weather_days and air_days word them (lib/ecowitt/days.py, lib/airgradient/days.py)
NOTE_TRACE = ("These days rank higher but had less than {value:g} mm of rain (a trace), so they are not in \"days\". Mention the first "
              "as a short footnote.")
NOTE_DAILY = ("Days marked \"daily\" come from daily readings that run {run}, so their rain and dates are approximate (a morning shower "
              "can be counted on the day before). Say so briefly if any listed day is marked daily, or if the period reaches back before "
              "exact_from.")
NOTE_AVERAGED = ("For these readings, days from 30-minute or daily data use each interval's average, so a day's true peak (or low) may "
                 "be a little beyond what is listed: a count of days at or past a limit can fall slightly short near the limit. Say so "
                 "briefly.")
NOTE_HOURLY = ("{n} of these days only exist as hourly averages, so their highs and lows are hourly averages and a short spike is "
               "smoothed. Say so briefly if it matters.")
NOTE_MISSING = "{n} day(s) in this period have no stored readings (not downloaded yet, or the sensor was off) and are not counted."
NOTE_NOTHING = "No readings are held for this period yet (its history may still be downloading)."
NOTE_BEFORE = "No readings are held for this period: the record of what was asked about starts {first}."
NOTE_DAILY_LEFT_OUT = ("{n} day(s) are left out: their weather-station figures come only from Ecowitt's daily records, which run {run}, "
                       "not midnight to midnight as the air sensor's days do, so the two can't be set against each other. Say so "
                       "briefly.")
NOTE_LEFT_OUT = ("Some days aren't counted, as `left_out` says: \"not held finely enough\" (only Ecowitt's daily or 4-hour records, or "
                 "nothing, cover that stretch), \"no readings\" (the sensor was off), \"removed by the clock change\". Say so briefly "
                 "if it matters.")
LEFT_OUT = {NOT_HELD: "not held finely enough", CLOCK_CHANGE: CLOCK_CHANGE, NOT_OVER: NOT_OVER}

# ---------- the description and the input schema ----------
DESCRIPTION = (
    "Days found, ranked, counted or totalled by their figures, from the weather station's and the air sensor's days, midnight to "
    "midnight. sort_by is the field the days are ranked by: the wettest days, most rain {sort_by: \"rain\"}; the hottest "
    "day it rained {where: [{field: \"rain\", op: \">=\", value: 1}], sort_by: \"temp_max\"}. Days over 35°C this year: {where: "
    "[{field: \"temp_max\", op: \">\", value: 35}], period: \"this year\", count_only: true}. Per month or year (\"per year\", \"each "
    "year\", \"by month\"), group_by: rain per month {stat: \"sum\", of: \"rain\", group_by: \"month\"}; average temperature per "
    "year {stat: \"avg\", of: \"temp_avg\", group_by: \"year\"}. PM2.5 over 25: {where: [{field: \"pm2_5_max\", op: \">\", value: "
    "25}]}. The windiest weekend day: {sort_by: \"wind_gust\", only: \"weekend\"}. One day: {period: \"5 Sep\"}. Part of each day, "
    "hours: nights below 5°C {hours: \"night\", where: [{field: \"temp_min\", op: \"<\", value: 5}]}; 3pm in October {hours: "
    "\"15:00\", stat: \"avg\", of: \"temp_avg\", period: \"October\"}. No period: every day on record (with hours, 30 days). Not "
    "for: a period's highest or lowest reading with when, a record (\"hottest ever\"), wind direction or a chart over time (summary); values at set times (readings); rain "
    "against a reading (relate). Result: answer, a line to pass on; matching_days of days_checked; days, the top ones, each with "
    "day_look (word and emoji: put it before the day); by_month or by_year, drawn as bars (charts); note_*: mention "
    "briefly. A rainy day is rain >= 1.")

STATION_WHERE = ("Weather station: temp_max, temp_min, temp_avg (outdoor °C), rain (mm that fell), wind_gust (km/h, the highest); a "
                 "day's highest (_max), lowest (_min) and mean (_avg) of humidity, pressure, dew_point, feels_like, vpd, indoor_temp, "
                 "indoor_humidity; the _max and _avg of wind_speed, solar, uv, rain_rate.")
AIR_WHERE = "Air sensor: the _max, _min and _avg of pm2_5, pm10, pm1 (µg/m³), co2 (ppm), voc_index and nox_index."
HOURS = {"type": "string", "description": "Part of each day instead of the whole day: night (22:00-06:00, which belongs to the day it "
                                          "ends on), morning (06:00-12:00), afternoon (12:00-18:00), evening (18:00-24:00), daytime "
                                          "(06:00-18:00), two times on the half hour ('15:00-16:00', '22:30-06:00'), or a moment ('15:00'). "
                                          "Each day's figures are then over that stretch: temp_max its highest, temp_min its lowest, "
                                          "_avg its mean, rain the mm that fell in it. Today counts once its stretch is over. At most "
                                          f"{MAX_HOURS_DAYS} days."}
CHART_ANSWERS_IT = {"type": "boolean", "description": "true only when the question is just to see this chart (a figure per month or "
                                                      "year) and its figures: nothing else to answer (\"rain per month this year\", "
                                                      "not \"rain per month: is it drier than last year?\"). The result then carries "
                                                      "the finished reply (`reply`, `complete`)."}


def parameters(fields: list[str]) -> dict:
    """The input schema over these fields (those of the sensors there are)."""
    devices = {DEVICE_OF[n] for n in fields}
    where = " ".join([*([STATION_WHERE] if STATION in devices else []), *([AIR_WHERE] if AIR_DEVICE in devices else [])])
    default_sort = "temp_max" if STATION in devices else "pm2_5_max"
    one = {"type": "string", "enum": fields}
    return {
        "type": "object",
        "properties": {
            "where": {"type": "array", "description": "Conditions that must ALL hold on the same day, e.g. rain >= 1 and temp_max > 30. "
                                                      f"Fields. {where}",
                      "items": {"type": "object", "properties": {"field": one, "op": {"type": "string", "enum": list(OPS)},
                                                                 "value": {"type": "number"}},
                                "required": ["field", "op", "value"]}},
            "sort_by": {**one, "description": "The field the days are ranked by (\"most rain\", \"wettest\": rain; \"hottest\": temp_max; "
                                                "\"windiest\": wind_gust). Default: the first condition's field, else `of`, else "
                                                f"{default_sort}" + (" (pm2_5_max when only air fields are named)."
                                                                     if len(devices) > 1 else ".")},
            "order": {"type": "string", "enum": list(ORDERS), "description": "desc: highest first (default); asc: lowest first."},
            "count_only": {"type": "boolean", "description": "true for \"how many days\": the counts alone, no days listed."},
            "group_by": {"type": "string", "enum": ["month", "year"],
                         "description": "A figure per month or year (those with none included), drawn as a bar chart, whenever the "
                                        "question asks per month or per year (\"rain per month\", \"by month\", \"days over 35°C "
                                        "per year\", \"each year\", \"average temperature per year\")."},
            "stat": {"type": "string", "enum": list(STATS),
                     "description": "What the period, or each month or year, gives: count (default: the matching days), or the max, min, "
                                    "avg or sum of `of` over the matching days (\"rain per month\": sum of rain; \"the hottest day each "
                                    "year\": max of temp_max; \"average humidity per year\": avg of humidity_avg)."},
            "of": {**one, "description": "The field stat takes the max, min, avg or sum of; always with stat (not needed for count)."},
            "limit": {"type": "integer", "description": f"How many days to list (default 5, at most {MAX_LIMIT}). The total is always "
                                                       "counted."},
            "only": {"type": "string", "enum": list(ONLY),
                     "description": "Only the public holidays (the station's state's calendar) or only Saturdays and Sundays: days are "
                                    "then found and counted among those alone."},
            "hours": HOURS,
            **period_properties(f"{DEFAULT} (every day held); with hours, the {DEFAULT_HOURS}", whole_days=True),
            "chart_answers_it": CHART_ANSWERS_IT,
        },
    }


# ---------- the call, checked ----------
@dataclass(frozen=True)
class Call:
    """A call's arguments as the tool reads them, checked, with every default filled in."""
    where: list[dict]
    sort_by: str | None
    order: str
    count_only: bool
    group_by: str | None
    stat: str
    of: str | None
    limit: int
    only: str | None
    stretch: Stretch | None
    chart_answers_it: bool

    @property
    def named(self) -> list[str]:
        """The fields the call names, in order."""
        return list(dict.fromkeys([*(c["field"] for c in self.where), *filter(None, (self.sort_by, self.of))]))

    def engine(self, first: date, last: date) -> dict:
        """The arguments as lib/daytable.py reads them."""
        return {"start_date": first.isoformat(), "end_date": last.isoformat(), "where": self.where, "order": self.order,
                "count_only": self.count_only, "stat": self.stat, "limit": self.limit,
                **{k: v for k, v in (("sort_by", self.sort_by), ("group_by", self.group_by), ("of", self.of)) if v}}


def _stretch_words(s: Stretch) -> str:
    """A stretch as `hours` takes it: "night", "15:00-16:00", "15:00"."""
    clock = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    return s.name or (clock(s.start) if s.moment else f"{clock(s.start)}-{clock(s.end)}")


def _flag(args: dict, name: str) -> bool:
    value = args.get(name)
    if value is None or isinstance(value, bool):
        return bool(value)
    if str(value).strip().lower() in ("true", "1", "yes"):
        return True
    if str(value).strip().lower() in ("false", "0", "no", ""):
        return False
    raise Refusal(f"`{name}` is true or false, not {value!r}.")


def _near(word: str, fields: list[str]) -> list[str]:
    """The fields a name that isn't one may have meant ("temperature": temp_max, temp_min, temp_avg; "pm2.5": pm2_5_max ...)."""
    key = re.sub(r"[^a-z0-9]+", "_", str(word).lower()).strip("_")
    key = {"temperature": "temp", "outdoor_temp": "temp", "outdoor_temperature": "temp", "indoor_temperature": "indoor_temp",
           "pm25": "pm2_5", "uvi": "uv", "uv_index": "uv", "gust": "wind_gust", "gusts": "wind_gust", "rainfall": "rain",
           "precipitation": "rain", "voc": "voc_index", "nox": "nox_index", "wind": "wind"}.get(key, key)
    for suffix in ("_max", "_min", "_avg", "_high", "_low", "_mean", "_total"):
        key = key.removesuffix(suffix)
    return [n for n in fields if n == key or n.startswith(key + "_")]


class DaysTool:
    """The `days` tool over a Context: its fields are those of the sensors there are."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.units = {**(STATION_UNITS if ctx.eco else {}), **(AIR_UNITS if ctx.air else {})}
        self.fields = list(self.units)

    def tool(self) -> ServiceTool:
        return ServiceTool(NAME, DESCRIPTION, parameters(self.fields), self.handle)

    # ---------- reading the arguments ----------
    def _field(self, value, what: str) -> str:
        name = str(value).strip() if value is not None else ""
        if name in self.units:
            return name
        if name in DEVICE_OF:
            device = "weather station" if DEVICE_OF[name] == STATION else "air sensor"
            raise Refusal(f"{what} {name!r} is the {device}'s, and this service has no {device}.", fields=self.fields)
        near = _near(name, self.fields)
        hint = f" Did you mean {', '.join(near)}?" if near else ""
        raise Refusal(f"{what} {value!r} isn't a field of days.{hint} Send one of `fields`.", fields=self.fields)

    def _condition(self, c) -> dict:
        if not isinstance(c, dict) or not {"field", "op", "value"} <= set(c):
            raise Refusal(f"A condition is {{\"field\", \"op\", \"value\"}}, such as {{\"field\": \"rain\", \"op\": \">=\", \"value\": 1}}, "
                          f"not {c!r}.", fields=self.fields, ops=list(OPS))
        name = self._field(c["field"], "Field")
        op = OP_WORDS.get(str(c["op"]).strip(), str(c["op"]).strip())
        if op not in OPS:
            raise Refusal(f"op {c['op']!r} isn't one of {' '.join(OPS)}.", ops=list(OPS))
        value = c["value"]
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                pass
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise Refusal(f"A condition's value is a number, in the field's unit ({name}: {self.units[name] or 'an index'}), not "
                          f"{c['value']!r}.")
        return {"field": name, "op": op, "value": value}

    def read(self, args: dict) -> Call:
        """The call checked: a Refusal, saying what to send instead, for anything the tool can't use."""
        if unknown := [k for k in args if k not in ARGUMENTS]:
            renamed = [f"`{k}` is `{OLD_NAMES[k]}`" for k in unknown if k in OLD_NAMES]
            raise Refusal(f"{', '.join(map(repr, unknown))} {'is not an argument' if len(unknown) == 1 else 'are not arguments'} of days"
                          + (f" ({'; '.join(renamed)}, or send a `period` in words)" if renamed else "") + ".", arguments=list(ARGUMENTS))
        where = args.get("where") or []
        where = [where] if isinstance(where, dict) else where
        if not isinstance(where, list):
            raise Refusal("`where` is a list of conditions, such as [{\"field\": \"rain\", \"op\": \">=\", \"value\": 1}].",
                          fields=self.fields)
        where = [self._condition(c) for c in where]
        sort_by = self._field(args["sort_by"], "sort_by") if args.get("sort_by") else None
        order = str(args.get("order") or "desc").strip().lower()
        if order not in ORDERS:
            raise Refusal(f"order is desc (highest first) or asc (lowest first), not {args['order']!r}.")
        group_by = str(args["group_by"]).strip().lower() if args.get("group_by") else None
        if group_by not in (None, "month", "year"):
            raise Refusal(f"group_by is month or year, not {args['group_by']!r}.")
        if args.get("of") and not args.get("stat"):   # (else it counts days and `of` is lost: "rain per month" as "313 of 313 days")
            raise Refusal(f"`of` names the field `stat` works on: send `stat` too ({', '.join(s for s in STATS if s != 'count')}; "
                          f"\"rain per month\": stat sum, of rain), or leave `of` out to count days.", stats=list(STATS))
        stat = str(args.get("stat") or "count").strip().lower()
        if stat not in STATS:
            raise Refusal(f"stat is one of {', '.join(STATS)}, not {args['stat']!r}.", stats=list(STATS))
        of = self._field(args["of"], "of") if args.get("of") else None
        if stat != "count" and of is None:
            raise Refusal(f"stat {stat} needs `of`: the field to take the {stat} of (\"rain per month\": stat sum, of rain).",
                          fields=self.fields)
        try:
            limit = max(1, min(int(args.get("limit") or 5), MAX_LIMIT))
        except (TypeError, ValueError):
            raise Refusal(f"limit is a whole number of days to list, 1 to {MAX_LIMIT}, not {args['limit']!r}.") from None
        only = str(args["only"]).strip().lower() if args.get("only") else None
        if only not in (None, *ONLY):
            raise Refusal(f"only is {' or '.join(ONLY)}, not {args['only']!r}.")
        stretch = None
        if args.get("hours") not in (None, ""):
            try:
                stretch = parse_stretch(args["hours"])
            except ValueError as e:
                raise Refusal(str(e), hours=ALLOWED) from None
        return Call(where, sort_by, order, _flag(args, "count_only"), group_by, stat, of, limit, only, stretch,
                    _flag(args, "chart_answers_it"))

    def devices(self, call: Call) -> list[str]:
        """The sensors the call is about: those of the fields it names, else the station's (the air sensor's when there is no station)."""
        named = {DEVICE_OF[n] for n in call.named}
        return [d for d in DEVICES if d in named] or [STATION if self.ctx.eco else AIR_DEVICE]

    # ---------- the period ----------
    def period(self, args: dict, call: Call, now: datetime, first_held: date | None):
        """(the Period, its first day, its last day): whole days to yesterday; with `hours`, today too once its stretch is over."""
        tz, today, stretch = self.ctx.zone, now.date(), call.stretch
        read = lambda whole: read_period(args, tz=tz, now=now, default=DEFAULT_HOURS if stretch else DEFAULT, whole_days=whole,
                                         first_held=first_held)
        try:
            whole, refused = read(True), None
        except Refusal as e:
            whole, refused = None, e
        if stretch and (whole is None or whole.cut) and over(stretch, today, tz, now.timestamp()):
            try:   # the same words to now: whole days still, ending today
                timed = read(False)
            except Refusal:
                timed = None
            if timed and timed.start.time() == time() and timed.last_day == today:
                return timed, timed.first_day, today
        if refused:
            raise refused
        return whole, whole.first_day, whole.last_day

    # ---------- reading the file ----------
    def _station_sensors(self, r: Reader, shown: list[str]) -> dict:
        """{group: its Sensor} of the groups these station fields are in, those the file has."""
        groups = dict.fromkeys(STATION_FIELDS[n][0] for n in shown if DEVICE_OF[n] == STATION)
        return {g: s for g in groups if (s := r.sensor(self.ctx.eco.mac, g)) is not None}

    def _first_held(self, r: Reader, devices: list[str], shown: list[str]) -> date | None:
        """The first day held by every sensor asked about ("on record"): the latest of their first day rows; None when one holds none."""
        firsts = []
        for device in devices:
            sensors = list(self._station_sensors(r, shown).values()) if device == STATION else [self.ctx.air.store.sensor]
            by_kind: dict[str, list[int]] = {}
            for s in filter(None, sensors):
                by_kind.setdefault(s.kind, []).append(s.id)
            found = [r.db.execute(f"SELECT MIN(day) FROM {kind}_day WHERE sensor IN ({', '.join('?' * len(ids))})", ids).fetchone()[0]
                     for kind, ids in by_kind.items()]
            found = [date.fromisoformat(d) for d in found if d]
            if not found:
                return None
            firsts.append(min(found))
        return max(firsts) if firsts else None

    def _stretches(self, r: Reader, shown: list[str], first: date, last: date, stretch: Stretch, now: float) -> dict[str, PartDays]:
        """{group (or "air"): its sensor's figures over the stretch of each day}, in the columns these fields need."""
        columns: dict[str, list[str]] = {}
        for n in shown:
            if DEVICE_OF[n] == STATION:
                group, reading, _, _ = STATION_FIELDS[n]
                columns.setdefault(group, []).append(column_of(group, reading)[0])
            else:
                columns.setdefault(AIR_DEVICE, []).append(n.rsplit("_", 1)[0])
        sensors = {**(self._station_sensors(r, shown) if self.ctx.eco else {}),
                   **({AIR_DEVICE: self.ctx.air.store.sensor} if self.ctx.air and AIR_DEVICE in columns else {})}
        return {key: stretch_figures(r, sensor.kind, sensor, first, last, stretch, list(dict.fromkeys(columns[key])), self.ctx.zone, now)
                for key, sensor in sensors.items() if sensor is not None}

    async def _held(self, call: Call, shown: list[str], first: date, last: date, now: float) -> "Held":
        """Each shown field's figure per day, and what is known of where they came from."""
        days = [first + timedelta(days=k) for k in range((last - first).days + 1)]
        held = Held(values={n: {} for n in shown})
        station = [n for n in shown if DEVICE_OF[n] == STATION]
        air = [n for n in shown if DEVICE_OF[n] == AIR_DEVICE]
        storage = self.ctx.storage
        if call.stretch is None:
            if station:
                values, held.station = await storage.read(day_values_on, self.ctx.eco.mac, station, days)
                held.values.update(values)
            if air:
                figures = await self.ctx.air.store.day_figures(first, last, air)
                held.values.update(figures.values)
                held.hourly, held.counts = set(figures.hourly), figures.counts
        else:
            parts = await storage.read(self._stretches, shown, first, last, call.stretch, now)
            for n in shown:
                if DEVICE_OF[n] == STATION:
                    group, reading, kind, _ = STATION_FIELDS[n]
                    part, column = PARTS[kind], column_of(group, reading)[0]
                else:
                    group, (column, suffix) = AIR_DEVICE, n.rsplit("_", 1)
                    part = AIR_PART[suffix]
                got = parts.get(group)
                if got is not None:
                    held.values[n] = {d: v for d, v in zip(got.days.day, getattr(got.days, part).get(column, ())) if v is not None}
            for group, got in parts.items():
                for d, src in zip(got.days.day, got.days.source):
                    if group == AIR_DEVICE:
                        if src == "1hour":
                            held.hourly.add(d)
                    else:
                        held.station[d] = max(held.station.get(d, src), src, key=COARSER.get)
                for d, why in got.left_out.items():
                    held.left_out.setdefault(d, why)
                held.missing.update(got.missing)
                if got.note:
                    held.notes.append(got.note)
            held.missing -= set(held.left_out)
        if self.ctx.eco:   # each day's look, from its whole day's figures
            held.look = (await storage.read(day_values_on, self.ctx.eco.mac, list(LOOK_FIGURES), days))[0]
        return held

    # ---------- the answer ----------
    async def handle(self, args: dict, meta: dict) -> str:
        call = self.read(args)
        devices = self.devices(call)   # (each one the service has: read() refuses a field of a sensor it hasn't)
        calendar = PublicHolidays(self.ctx.zone)
        if call.only == "public_holiday" and not calendar.available:
            raise Refusal(calendar.problem())
        tz, now = self.ctx.zone, self.ctx.now()
        shown = list(dict.fromkeys([n for d in devices for n in SHOWN[d]] + call.named))
        first_held = await self.ctx.storage.read(self._first_held, devices, shown)
        period, first, last = self.period(args, call, now, first_held)
        if call.stretch and (last - first).days + 1 > MAX_HOURS_DAYS:
            raise Refusal(f"With hours, a period is at most {MAX_HOURS_DAYS} days ({period.text} is {(last - first).days + 1}): send a "
                          "shorter one, such as \"past year\".", fits="past year")
        asked = prepare(call.engine(first, last), self.units, tuple(n for d in devices for n in SHOWN[d]),
                        last + timedelta(days=1))   # ("today" for the engine: the day after the last, so it cuts nothing)
        if isinstance(asked, str):   # (the call was checked: the engine finds nothing more to refuse)
            raise Refusal(asked)
        held = await self._held(call, asked.shown, first, last, now.timestamp())
        merged = len(devices) > 1
        kind: Callable[[date], bool] = {None: lambda d: True, "weekend": lambda d: d.weekday() >= 5,
                                        "public_holiday": lambda d: calendar.name(d) is not None}[call.only]
        has = {device: {d for n in asked.shown if DEVICE_OF[n] == device for d in held.values[n]} for device in devices}
        days = [d for d in asked.days if kind(d)]
        both = [d for d in days if all(d in has[device] for device in devices)]
        daily_out = {d for d in both if merged and held.station.get(d) == "daily"}
        checked = [d for d in both if d not in daily_out]

        def row_extra(d: date) -> dict:
            out = {}
            if held.look is not None and (look := day_look({n: held.look[n].get(d) for n in LOOK_FIGURES}, latitude=self.ctx.eco.latitude,
                                                           day=d, so_far=d >= now.date())):
                out["day_look"] = look
            if call.only == "public_holiday":
                out["holiday"] = calendar.name(d)
            station = "daily" if held.station.get(d) == "daily" else "exact"
            air = "hourly" if d in held.hourly else "exact"
            out["source"] = {STATION: station, AIR_DEVICE: air} if merged else station if devices == [STATION] else air
            return out

        turn = Turn()   # (only its charts: the bars analyse draws)
        scope = "  ·  ".join(filter(None, [call.stretch.text if call.stretch else "", ONLY.get(call.only, "")]))
        sourced = CHART_SOURCE.set(" and ".join(SOURCES[d] for d in devices))
        try:
            got = analyse(held.values, asked, checked, tz, turn, row_extra, scope)
        finally:
            CHART_SOURCE.reset(sourced)
        out = got.out
        engine_period = out.pop("period")
        out.pop("chart", None)   # (the bot's hint: chart_note says what to do with a chart)
        out.update(self._notes(call, asked, held, got, checked, days, daily_out, devices, merged, first_held))
        if (call.stat or call.count_only) and not call.group_by and (last - first).days >= 31:   # one figure over months: was a breakdown meant?
            out["note_group_by"] = GROUP_BY_NOTE
        line = answer_line(out, engine_period, call.stretch)
        fields = {"answer": line, **({"hours": {"text": call.stretch.text, "how_to_read": HOURS_HOW}} if call.stretch else {}), **out}
        charts = with_charts(turn.charts, tz, meta)
        if charts:
            drawn = self.drawn_by(call, args, period, first, last, first_held, now.date())
            for chart in charts["charts"]:
                chart["drawn_by"] = drawn
        finished = complete(line) if call.chart_answers_it and charts and not needs_words(out) and not period.note else {}
        return answer(period=period, as_of=now, **fields, **charts, **finished)

    def _notes(self, call: Call, asked, held: "Held", got, checked: list[date], days: list[date], daily_out: set[date],
               devices: list[str], merged: bool, first_held: date | None) -> dict:
        """The notes weather_days and air_days add, in their order, and those of a question mixing the two and of part of each day."""
        out: dict = {}
        where, values, sort_by = asked.where, held.values, asked.sort_by
        station = STATION in devices
        if station:
            # Days that would have ranked higher had a trace of rain counted: the answer should mention them
            trace: list[date] = []
            rain_cond = next((c for c in where if c["field"] == "rain" and c["op"] in (">", ">=") and c["value"] > 0), None)
            if rain_cond and lists_days(asked.args):
                relaxed = [{"field": "rain", "op": ">", "value": 0} if c is rain_cond else c for c in where]
                matched = set(got.matches)
                better = operator.lt if call.order == "asc" else operator.gt
                for d in got.rank(d for d in checked if got.holds(d, relaxed)):
                    if d in matched:
                        break   # from here on the real matches rank at least as high
                    if not got.ranked or better(values[sort_by][d], values[sort_by][got.ranked[0]]):   # strictly ahead, not a tie
                        trace.append(d)
                    if len(trace) == 2:
                        break
            if trace:
                out["trace_rain_days"] = [got.row(d) for d in trace]
                out["note_trace"] = NOTE_TRACE.format(value=rain_cond["value"])
            run = daily_run(self.ctx.zone, asked.first, asked.last)
            if daily_out:
                out["daily_left_out"] = len(daily_out)
                out["note_daily_left_out"] = NOTE_DAILY_LEFT_OUT.format(n=len(daily_out), run=run)
            elif any(held.station.get(d) == "daily" for d in checked):
                exact = sorted(d for d in checked if held.station.get(d) != "daily")
                out["exact_from"] = day_text(exact[0]) if exact else None
                out["note_daily"] = NOTE_DAILY.format(run=run)
            if AVERAGED & set(asked.shown) and any(held.station.get(d) != "5min" for d in checked):
                out["note_averaged"] = NOTE_AVERAGED
        if AIR_DEVICE in devices:
            hourly = [d for d in checked if d in held.hourly]
            if hourly and any(n.endswith(("_max", "_min")) for n in asked.shown if DEVICE_OF[n] == AIR_DEVICE):
                out["note_hourly"] = NOTE_HOURLY.format(n=len(hourly))
            if call.stretch is None and (missing := sum(1 for d in days if not held.counts.get(d))):
                out["note_missing"] = NOTE_MISSING.format(n=missing)
        if call.stretch:
            reasons = [LEFT_OUT[held.left_out[d]] for d in days if d in held.left_out] + ["no readings" for d in days if d in held.missing]
            if reasons:
                out["left_out"] = {why: reasons.count(why) for why in dict.fromkeys(reasons)}
                out["note_left_out"] = NOTE_LEFT_OUT
            if held.notes:
                out["note_moment"] = " ".join(dict.fromkeys(held.notes))
        if not checked:
            before = first_held is not None and asked.last < first_held   # (what the data does reach: 3.2, errors that teach)
            out["note"] = NOTE_BEFORE.format(first=day_text(first_held)) if before else NOTE_NOTHING
        return out

    def drawn_by(self, call: Call, args: dict, period, first: date, last: date, first_held: date | None, today: date) -> dict:
        """The call that drew the chart as the tool read it (3.7), every default filled in, the period in words, and the periods it is
        worth drawing over: per month, 3 months, a year and the whole record (with hours, only while that is within its 400 days); per
        year, none."""
        words = period.asked
        if args.get("start") and not sends_period(args):   # dates as words; one that runs to today or later as "to now", so a replay moves on with the clock
            open_ended = not args.get("end") or str(args["end"])[:10] >= today.isoformat()
            words = f"{first.isoformat()} to {'now' if open_ended else last.isoformat()}"
        arguments = {"where": call.where, "sort_by": self.sort_of(call), "order": call.order, "count_only": call.count_only,
                     "group_by": call.group_by, "stat": call.stat, **({"of": call.of} if call.of else {}), "limit": call.limit,
                     **({"only": call.only} if call.only else {}), **({"hours": _stretch_words(call.stretch)} if call.stretch else {}),
                     "period": words, "chart_answers_it": call.chart_answers_it}
        periods = []
        if call.group_by == "month":
            fits = not call.stretch or (first_held is not None and (today - first_held).days + 1 <= MAX_HOURS_DAYS)
            periods = [{"period": p, "label": label} for p, label in MONTH_PERIODS if p != DEFAULT or fits]
        return {"tool": NAME, "arguments": arguments, "periods": periods}

    def sort_of(self, call: Call) -> str:
        """The field the days are ranked by: sort_by, else the first condition's field, else `of`, else the first shown (daytable)."""
        return call.sort_by or (call.where[0]["field"] if call.where else call.of or SHOWN[self.devices(call)[0]][0])


@dataclass
class Held:
    """What DaysTool._held read: each shown field's figure per day; the station's source of each day ("5min", "30min", "daily"); the air
    sensor's days held only as hourly averages, and (whole days) its readings per day; with `hours`, the days left out (and why) and
    those with no readings in the stretch, and what a moment was on coarser days; and each day's figures for its look."""
    values: dict[str, dict[date, float]]
    station: dict[date, str] = field(default_factory=dict)
    hourly: set[date] = field(default_factory=set)
    counts: dict[date, int | None] = field(default_factory=dict)
    left_out: dict[date, str] = field(default_factory=dict)
    missing: set[date] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    look: dict[str, dict[date, float]] | None = None


def needs_words(out: dict) -> bool:
    """Does the result carry a note the person should hear that the answer line leaves out (the older days held only as daily records
    or hourly averages, a rarer day that nearly counted, days left out ...)? Then it is never complete (3.7)."""
    notes = {k for k in out if k.startswith("note") or k.endswith("_note") or k in ("warning", "missing", "exact_from", "trace_rain_days")}
    return bool(notes - CAPTIONED_NOTES)


# ---------- the answer line (lib/report.py's days lookup, ported with its words) ----------
def period_words(text: str) -> str:
    """"Tue 29 Sep 2026 - Tue 6 Oct 2026" -> "Tue 29 Sep – Tue 6 Oct 2026" (the first year is dropped only when it is the same year)."""
    years = re.findall(r" (20\d\d)\b", text)
    if len(years) == 2 and years[0] == years[1]:
        text = re.sub(r" 20\d\d\b(?= - )", "", text)
    return text.replace(" - ", " – ")


def answer_line(out: dict, period: str, stretch: Stretch | None = None) -> str:
    """"137 of 1,454 days with UV index ≥ 10 (period).", or a total, average, highest or lowest over the period ("Total rain: 49.8 mm
    over 30 days (period).") as the bot writes a days lookup today; `period` is the days' "first - last". Over part of each day the line
    says which part first ("Each night (22:00–06:00): ...", "Each day at 15:00: ...")."""
    when = period_words(period)
    if out.get("stat") and out.get("value") is not None:
        unit = unit_text(out["stat"].get("unit")) or ""
        line = f"{out['stat']['what']}: {out['value']:g} {unit}".rstrip() + f" over {out['days_checked']:,} days ({when})."
    else:
        what = f" with {out['condition']}" if out.get("condition") else ""
        note = " Older days use averages, so a count near the limit may fall slightly short." if out.get("note_averaged") else ""
        line = f"{out['matching_days']:,} of {out['days_checked']:,} days{what} ({when}).{note}"
    if stretch is None:
        return line
    each = f"Each {stretch.text}" if stretch.name else f"Each day at {stretch.text}" if stretch.moment else f"Each day {stretch.text}"
    return f"{each}: {line}"


def tools(ctx: Context) -> list[ServiceTool]:
    """The days tool, when there is a sensor to read."""
    return [DaysTool(ctx).tool()] if ctx.eco or ctx.air else []


__all__ = ["DESCRIPTION", "DaysTool", "answer_line", "needs_words", "parameters", "tools"]
