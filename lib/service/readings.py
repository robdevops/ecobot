"""The `readings` tool (docs/ecobot-mcp.md 3.3): one reading's values at an interval, a row per interval with its time, over a period in
the person's words. It replaces the bot's `readings` tool, and wraps its engine (lib/history.py): the same rows read the same way
(read_intervals, read_days: an interval's average with its lowest and highest, rain as the mm that fell, gusts as the highest, wind
direction as the reading at the interval's end, days from the day rows), the same cap of MAX_ROWS rows with the interval that fits when
there are more, over the service's periods (common.read_period) and its reading names (ctx.readings()).

It reads only the file: nothing is fetched, so a request's `fetch` and `priority` change nothing here, and it draws no chart.

Also here, for the tools over the file (relate, scan): the id of a reading's sensor in the file (sensor_id) and the first day held for
some readings (first_day), which "on record" starts from.
"""

from datetime import date

from ..history import (ALIASES, DAY_VALUE_IS, INTERVALS, MAX_ROWS, VALUE_IS, finest_fit, interval_starts, read_days, read_intervals,
                       row_count)
from ..storage.read import Reader
from ..timeutil import local_date
from .common import Refusal, ServiceTool, answer, period_properties, read_period
from .context import Context
from .names import AIR_DEVICE, Catalogue, Reading

DEFAULT_PERIOD = "last 24 hours"
NOTHING_STORED = "Nothing is stored for this sensor yet."
NO_ROWS = "No readings are stored for this period at this interval: say so; never fill the gap."

DESCRIPTION = (
    "The stored values of ONE reading at a fixed interval, a row per interval with its time: values at set times or per interval. "
    "Examples: \"humidity each hour for the last 5 hours\" {reading: \"outdoor.humidity\", period: \"last 5 "
    "hours\", interval: \"1hour\"}; \"PM2.5 every 30 minutes this morning\" {reading: \"pm2.5\", period: \"this morning\", interval: "
    "\"30min\"}; \"rain each hour today\" {reading: \"rain\", period: \"today\", interval: \"1hour\"}; \"the temperature at 3pm "
    "yesterday\" {reading: \"outdoor.temperature\", period: \"yesterday afternoon\", interval: \"5min\"}, then its 3:00pm row. One "
    "reading a call: outdoor and indoor are two calls. "
    f"At most {MAX_ROWS} rows: 5min for up to a day, 30min up to 6 days, 1hour up to 12 days, 4hour up to 7 weeks, 1day up to 300 "
    "days; more is refused with the interval that fits (`fits`). "
    "Not for: a period's highs and lows with when, a record (\"hottest ever\", even \"use 5 minute data\": summary, period \"on "
    "record\"), averages, totals or a chart (summary); days ranked, counted or totalled, a figure "
    "per month or year (days); how it is now (current). "
    "Result: `columns` names each row's values and `value_is` what `value` is: an interval's average with its lowest and highest; rain "
    "the mm that fell in it; gusts the highest; wind direction the reading at its end; at 1day the day's figures and when its lowest "
    "and highest happened. Only what is stored is read, nothing is fetched: an empty row is `missing` (say so, never fill it), and "
    "`note_coarser` with `fits` names the interval held.")


def parameters(catalogue: Catalogue) -> dict:
    return {
        "type": "object",
        "properties": {
            "reading": {"type": "string", "description": f"ONE reading. {catalogue.description()} A word that stands for several "
                                                         "(temperature: outdoor and indoor) reads the first, and the result names the "
                                                         "others to ask for."},
            **period_properties(DEFAULT_PERIOD),
            "interval": {"type": "string", "enum": list(INTERVALS),
                         "description": "One row per interval, a block of local time (1hour runs from :00 to :00, 1day from midnight; "
                                        "4hour follows the station's own 4-hour records, whose times say where they start). 1min goes "
                                        "back 10 days, and only where the sensor was read every minute."},
        },
        "required": ["reading", "interval"],
    }


# ---------- the file ----------
def sensor_id(ctx: Context, reading: Reading) -> int | None:
    """The id of the reading's sensor in the file: the station's Ecowitt group, or the air sensor; None when it isn't there."""
    if reading.device == AIR_DEVICE:
        store = getattr(ctx.air, "store", None)
        sensor = getattr(store, "sensor", None)
        sensor = sensor() if callable(sensor) else sensor   # (AirReadings.sensor is a property; a test's stand-in may be a function)
    else:
        sensor = ctx.storage.sensors.find(ctx.eco.mac, reading.sensor) if ctx.eco else None
    return sensor.id if sensor else None


def _first_days(r: Reader, wanted: list[tuple[str, int]]) -> list[date | None]:
    out = []
    for kind, sensor in wanted:
        (day,) = r.db.execute(f"SELECT MIN(day) FROM {kind}_day WHERE sensor = ?", (sensor,)).fetchone()
        out.append(date.fromisoformat(day) if day else None)
    return out


async def first_day(ctx: Context, readings: list[Reading]) -> date | None:
    """The first day these readings are all held (each sensor's first day row, the latest of them): where "on record" starts. None
    when one of them has nothing held."""
    wanted = [(r.kind, sensor_id(ctx, r)) for r in readings]
    if not wanted or ctx.storage is None or any(sensor is None for _, sensor in wanted):
        return None
    days = await ctx.storage.read(_first_days, wanted)
    return None if None in days else max(days)


# ---------- the arguments ----------
def one_reading(catalogue: Catalogue, words) -> tuple[Reading, list[Reading]]:
    """(the reading these words name, the others they name too). A sensor's wind is its speed: its direction goes with it only on a
    chart (the compass rose). Raises UnknownReading, or Refusal when nothing is named."""
    if not [w for w in (words if isinstance(words, (list, tuple)) else [words]) if str(w or "").strip()]:
        raise Refusal("Send `reading`: one name from `names`, or a word from `words` (each with the names it stands for).",
                      names=catalogue.names, words=catalogue.words())
    found = catalogue.resolve(words, default=None)
    found = [r for r in found if r.drawn != "rose"] or found
    return found[0], found[1:]


def interval_of(value) -> str:
    """The interval sent, as INTERVALS names it ("hourly" is 1hour). Raises Refusal for one it can't read."""
    text = str(value or "").strip().lower()
    interval = ALIASES.get(text, text)
    if interval not in INTERVALS:
        said = f"`interval` {value!r} isn't one this tool reads" if text else "Send `interval`"
        raise Refusal(f"{said}: one of {', '.join(INTERVALS)} (a row each; at most {MAX_ROWS} rows).", intervals=list(INTERVALS))
    return interval


def others_note(reading: Reading, others: list[Reading]) -> str:
    names = ", ".join(r.name for r in others)
    return f"Read {reading.name} only: this tool reads one reading a call. For {names}, call it again, once for each, in the same round."


# ---------- the tool ----------
def tools(ctx: Context) -> list[ServiceTool]:
    async def handle(args: dict, meta: dict) -> str:
        catalogue = ctx.readings()
        reading, others = one_reading(catalogue, args.get("reading"))
        interval = interval_of(args.get("interval"))
        tz, now = ctx.zone, ctx.now()
        sensor = sensor_id(ctx, reading) if ctx.storage is not None else None
        period = read_period(args, tz=tz, now=now, default=DEFAULT_PERIOD,
                             first_held=await first_day(ctx, [reading]) if sensor is not None else None)
        m, now_ts = reading.metric, int(now.timestamp())
        start, end = int(period.start.timestamp()), int(period.end.timestamp()) - 1   # (the end not included)
        by_day = interval == "1day" and m.how != "last"   # (a compass point has no day figures: the last reading of each day)
        n = row_count(start, end, interval, tz)
        too_many = n > (MAX_ROWS if by_day else MAX_ROWS + 1)   # (an interval's count is exact only once listed: the first starts before `start`)
        marks = [] if too_many or by_day else interval_starts(start, end, INTERVALS[interval], tz)
        if too_many or len(marks) > MAX_ROWS:
            fits = finest_fit(start, end, tz)
            rows = max(n, len(marks))
            raise Refusal(f"Too many rows: {period.text} at {interval} is {rows} rows, and this tool gives at most {MAX_ROWS}. "
                          + (f"Ask again with interval {fits}." if fits else
                             "Too long even for days: split the period, or for its highs, lows and averages use summary, for a figure "
                             "per day, month or year days."),
                          rows=rows, max_rows=MAX_ROWS, fits=fits)
        out = {"reading": reading.name, "label": reading.label, "unit": reading.unit, "interval": interval,
               "value_is": (DAY_VALUE_IS if by_day else VALUE_IS)[m.how]}
        if sensor is None:
            return answer(period=period, as_of=now, **out, rows=[], note=NOTHING_STORED,
                          **({"note_others": others_note(reading, others)} if others else {}))
        if by_day:
            first, last = local_date(start, tz), local_date(end, tz)
            out |= await ctx.storage.read(read_days, m, sensor, first, last, tz)
            partial = last == local_date(now_ts, tz)
        else:
            out |= await ctx.storage.read(read_intervals, m, sensor, interval, marks, end, now_ts, tz)
            partial = marks[-1] + INTERVALS[interval] > now_ts
        if partial:
            out["note_last"] = "The last row is still filling: it runs up to now."
        if not any(row[1] is not None for row in out["rows"]):
            out["note"] = (f"No readings at this interval for this period: ask again with interval {out['fits']}." if out.get("fits") else
                           NO_ROWS)
        if others:
            out["note_others"] = others_note(reading, others)
        return answer(period=period, as_of=now, **out)

    return [ServiceTool("readings", DESCRIPTION, parameters(ctx.readings()), handle)]


__all__ = ["DEFAULT_PERIOD", "DESCRIPTION", "first_day", "interval_of", "one_reading", "parameters", "sensor_id", "tools"]
