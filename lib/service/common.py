"""What every service tool shares (docs/mcp-service.md 3.2, 3.7): how a tool is made and what it is sent, how it reads a period, and
the shape of what it answers.

A tool is a ServiceTool: its handler is `handler(args, meta)`, the arguments as sent and the request's metadata (`request_meta`), and
nothing else about the question. In the bot, `ServiceTool.tool()` makes it a lib.tools.Tool that reads only `Turn.meta` (and
`Turn.ahead`, the old name for a background request); over MCP, `meta` is the request's `_meta`.

Every answer is compact JSON (`answer`), and starts the same way:

    {"period": {"asked": "this week", "text": "the last 7 days: Wed 4 – Tue 10 Nov 2026",
                "start": "2026-11-04T00:00:00+11:00", "end": "2026-11-10T14:05:00+11:00", "cut": "to Mon 9 Nov: today isn't over"},
     "as_of": {"text": "Tue 10 Nov 2026 2:05pm", "time": "2026-11-10T14:05:00+11:00"},
     "fresh": FRESH,
     ... the tool's own fields ...,
     "chart_note": CHART_NOTE, "charts": [...]}

  - `period`: how the words were read (lib/periods_text.Period): `asked` the words (or "start/end"), `text` how they were read, ready
    to pass on, `start` and `end` ISO local times with their offset (the end not included), `cut` only when a whole-day tool cut
    today off, and `note` only when `start` and `end` were sent beside `period` and set aside (read_period). Left out by a tool that takes no period.
  - `as_of`: the time of the figures, `text` written as the tools write a time (timeutil.when), `time` ISO.
  - `fresh`: one fixed line for the model: the figures are as of `as_of`, and a follow-up or a challenge needs the tool called again.
  - `chart_note` and `charts` (`with_charts`): the charts as data (specs.to_json, bucketed to the request's `chart_points`), and the
    one line that tells the model what to do with them. Left out when there is no chart.
  - `reply` and `complete: true` (`complete`), only when the chart is the whole answer (3.7, `chart_answers_it`).

A refusal is `{"error": message, ...what teaches the right call}` (`refuse`). A handler can raise Refusal anywhere instead: the tool
answers with its JSON. A period that isn't one of the forms is refused with each form and what it means now (`refuse_period`).
"""

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, tzinfo

from .. import specs
from ..periods_text import Period, PeriodError, read_period as read_words
from ..timeutil import clock_text, day_text
from ..tools import Tool, Turn, json_result

# ---------- the request's metadata ----------
CHART_POINTS = 150        # the most points a line of a chart is sent in, unless the request asks for more (3.7): enough for a model to redraw
MAX_CHART_POINTS = 500    # the most a request may ask for: today's drawing budget (lines.POINT_BUDGET)
AS_DRAWN = "drawn"        # chart_points asking for every point as the chart was drawn, unbucketed: what the bot asks for, so its pictures
                          # are as they were (a chart of each day's average over years has more points than the budget)
NORMAL, BACKGROUND = "normal", "background"   # priority: someone is waiting, or the work is ahead of need (a chart drawn ahead)
META = {"chart_points": CHART_POINTS, "fetch": True, "priority": NORMAL}   # each key a handler reads, with its default


def request_meta(raw: dict | None = None) -> dict:
    """The request's metadata as a handler reads it: every key in META, each checked, defaulted where missing or not understood; any
    other key passed on as it came.
      - chart_points: the most points a chart's line is sent in, a whole number from 2 to MAX_CHART_POINTS (default CHART_POINTS), or
        AS_DRAWN ("drawn"): every point, as the chart was drawn (a client that draws for itself, as the bot does);
      - fetch: False answers from what is held, fetching nothing that is missing (default True);
      - priority: "background" for work nobody is waiting on (a chart drawn ahead), whose requests wait their turn; else "normal"."""
    raw = dict(raw) if isinstance(raw, dict) else {}
    points = raw.get("chart_points")
    try:
        points = AS_DRAWN if points == AS_DRAWN else CHART_POINTS if points is None or isinstance(points, bool) else \
            min(max(int(points), 2), MAX_CHART_POINTS)
    except (TypeError, ValueError):
        points = CHART_POINTS
    fetch = raw.get("fetch", True)
    fetch = not (fetch is False or str(fetch).strip().lower() in ("false", "0", "no", "off"))
    priority = BACKGROUND if str(raw.get("priority") or "").strip().lower() == BACKGROUND else NORMAL
    return {**raw, "chart_points": points, "fetch": fetch, "priority": priority}


def turn_meta(turn: Turn | None) -> dict:
    """The metadata of a question the bot asks: Turn.meta, a chart drawn ahead (Turn.ahead) being a background request."""
    if turn is None:
        return request_meta()
    raw = dict(turn.meta)
    if turn.ahead and "priority" not in raw:
        raw["priority"] = BACKGROUND
    return request_meta(raw)


# ---------- refusals ----------
def refuse(message: str, **teach) -> str:
    """A refusal as a tool answers it: {"error": message, ...teach}, where `teach` is whatever lets a model correct its call once, from
    the error alone (the names it could have sent, the forms of a period, the interval that fits)."""
    if "error" in teach:
        raise ValueError("a refusal's message is its `error`")
    return json_result({"error": message, **teach})


class Refusal(ValueError):
    """A call refused, with what teaches the right one. Raised anywhere in a handler, it becomes the tool's answer (`text`)."""

    def __init__(self, message: str, **teach):
        super().__init__(message)
        self.message, self.teach = message, teach

    @property
    def text(self) -> str:
        return refuse(self.message, **self.teach)

    @classmethod
    def of_period(cls, e: PeriodError) -> "Refusal":
        """A period that can't be read: its message (which ends with the time now) and, when the words were none of the forms, each form
        with what it means now, as {form: meaning}."""
        return cls(e.message, **({"forms": dict(e.forms)} if e.forms else {}))


def refuse_period(e: PeriodError) -> str:
    """A PeriodError as a tool answers it: {"error": message, ["forms": {form: what it means now}]}."""
    return Refusal.of_period(e).text


# ---------- a tool ----------
Handler = Callable[[dict, dict], Awaitable[str] | str]


@dataclass
class ServiceTool:
    """A tool of the service: its name, the description a model reads, its input schema, and `handler(args, meta)`, which answers with
    a string (`answer`, `refuse`) or raises Refusal. It may be a coroutine function. It is sent nothing else about the question."""
    name: str
    description: str
    parameters: dict           # the input schema: {"type": "object", "properties": {...}, "required": [...]}
    handler: Handler
    read_only: bool = True     # MCP's readOnlyHint: every tool of the service only reads
    open_world: bool = False   # MCP's openWorldHint: it asks a device or a cloud for what the file lacks (unless meta `fetch` is False)

    @property
    def annotations(self) -> dict:
        return {"readOnlyHint": self.read_only, "openWorldHint": self.open_world}

    async def call(self, args: dict | None = None, meta: dict | None = None) -> str:
        """Run it with these arguments and this request metadata (request_meta fills in the rest); a Refusal comes back as its JSON."""
        if args is not None and not isinstance(args, dict):
            return refuse("The arguments must be a JSON object of the tool's parameters.")
        try:
            out = self.handler(dict(args or {}), request_meta(meta))
            return await out if inspect.isawaitable(out) else out
        except Refusal as e:
            return e.text

    def tool(self) -> Tool:
        """The tool as the bot's Tools runs it: its handler reads only the Turn's metadata (turn_meta)."""
        async def handle(args: dict, turn: Turn | None = None) -> str:
            return await self.call(args, turn_meta(turn))
        return Tool(self.name, self.description, self.parameters, handle)


# ---------- periods ----------
PERIOD_FORMS = ("today, yesterday, last N hours/days/weeks/months/years (or Nh, Nd, Nw, Nm, Ny), this week, last week, week to date, "
                "this month, last month, this year, last year, a month or a year (October, October 2025, 2025), a date (5 Sep, "
                "2026-09-05), a weekday (Saturday), this morning, yesterday afternoon, last night, since 6am, a public holiday "
                "(Christmas Day), on record, A to B (June to August)")
WHOLE_DAY_FORMS = ("yesterday, last N days/weeks/months/years (or Nd, Nw, Nm, Ny), this week, last week, week to date, this month, "
                   "last month, this year, last year, a month or a year (October, October 2025, 2025), a date (5 Sep, 2026-09-05), "
                   "a weekday (Saturday), a public holiday (Christmas Day), on record, A to B (June to August)")


def period_properties(default: str, whole_days: bool = False) -> dict:
    """The `period`, `start` and `end` of a tool's input schema, described for a model: the person's words passed on unchanged, the
    forms the service reads, or else dates on the station's clock; for a whole-day tool, whole days up to yesterday. `default` is what
    the description says is read when nothing is sent: usually the words given to read_period as its default ("last 24 hours", "on
    record"), or more ("today; with a chart, the last 24 hours")."""
    if whole_days:
        forms, days = WHOLE_DAY_FORMS, (" Whole days only, up to yesterday, as today isn't over: a rolling period is that many whole days "
                                        "to yesterday (\"last 7 days\" is the 7 days before today).")
        start = "Instead of `period`, never with it: the first day, 'YYYY-MM-DD'."
        end = "With `start`: the last day, 'YYYY-MM-DD', up to yesterday. Default: yesterday."
    else:
        forms, days = PERIOD_FORMS, ""
        start = ("Instead of `period`, never with it: when it starts, the station's local time, 'YYYY-MM-DD' (from midnight) or "
                 "'YYYY-MM-DD HH:MM'.")
        end = "With `start`: when it ends, local time, 'YYYY-MM-DD' (to the end of that day) or 'YYYY-MM-DD HH:MM'. Default: now."
    return {
        "period": {"type": "string", "description": "The period in the person's own words, passed on unchanged (\"this week\", \"in the "
                                                    "last year\", \"past two years\"): the service reads them, and the result says how. \"past week\" (the "
                                                    "last 7 days) is never sent as \"last week\" (the previous Monday to Sunday). "
                                                    f"Forms: {forms}.{days} Default: {default}."},
        "start": {"type": "string", "description": start},
        "end": {"type": "string", "description": end},
    }


IGNORED_DATES = ("`start` and `end` were ignored: `period` was sent too, and they can't be read as dates or times. The period is read "
                 "from `period`.")
SAME_DATES = "`start` and `end` were set aside: `period` was sent too, and they read as the same span. The period is read from `period`."
SET_ASIDE = ("`start` and `end` were set aside because `period` was sent too: send them without `period` to use them, or send a "
             "`period` in one of the forms.")
SAME_WITHIN = timedelta(minutes=15)   # start/end this close to the period's span at both ends read as the same span


def sends_period(args: dict) -> bool:
    """Whether the call sends `period` (words, not empty): a call read at all then reads its period from it (read_period)."""
    return args.get("period") is not None and str(args["period"]).strip() != ""


def _sent(x) -> str | None:
    return str(x).strip() if x is not None and str(x).strip() else None


def _a_moment(text: str | None) -> bool:
    """Is it readable as a date or a time ('YYYY-MM-DD', 'YYYY-MM-DD HH:MM', ISO with an offset)?"""
    try:
        datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return False
    return True


def _moment_text(t: datetime, whole_days: bool, words: bool = False) -> str:
    """A start as a call sends it ('YYYY-MM-DD', or with ' HH:MM' when it isn't midnight and the tool takes times), or in words."""
    at_midnight = t.time() == datetime.min.time()
    if words:
        return day_text(t) if at_midnight else f"{day_text(t)} {clock_text(t)}"
    return f"{t:%Y-%m-%d}" if whole_days or at_midnight else f"{t:%Y-%m-%d %H:%M}"


def _span(text: str) -> str:
    """A Period's text without its label: "the last 7 days: Wed 4 – Tue 10 Nov 2026" -> "Wed 4 – Tue 10 Nov 2026"."""
    return text.rsplit(": ", 1)[-1]


def read_period(args: dict, *, tz: tzinfo, now: datetime, default: str, whole_days: bool = False,
                first_held: date | datetime | None = None) -> Period:
    """The period a call asks for, from its `period`, or `start` and `end`, else `default` (words), read by lib/periods_text.py at `now`
    (aware, or naive on the station's clock) in the station's zone. `first_held`: the first reading held for the readings asked about
    ("on record"). Raises Refusal (refuse_period) when the period can't be read.

    `period` with `start`/`end` beside it (a model may fill in every argument it sees, sometimes with junk such as 'count_only'):
    - start/end that aren't dates or times (each sent one read by datetime.fromisoformat): the period is read, its `note` says the
      dates were ignored (IGNORED_DATES);
    - start/end that read as the same span as the period (within SAME_WITHIN at each end; "on record" from the first reading held):
      the period is read, with the note SAME_DATES;
    - start/end that read as another span: refused, naming both spans as read, so the model sends the one it meant;
    - a period that can't be read beside readable start/end: refused, its forms listed and SET_ASIDE added.
    A note on the period keeps a chart from answering the question by itself (summary, days: the model reads the note)."""
    period, start, end = (_sent(args.get(k)) for k in ("period", "start", "end"))
    both = period is not None and (start is not None or end is not None)
    dates = both and all(_a_moment(x) for x in (start, end) if x is not None)

    def read(p, s, e) -> Period:
        return read_words(p, s, e, now=now, tz=tz, default=default, whole_days=whole_days, first_held=first_held)

    try:
        words = read(period, None, None) if both else read(period, start, end)
    except PeriodError as e:
        if dates and e.forms:   # the words aren't a form: say why the dates weren't used
            raise Refusal(f"{e.message} {SET_ASIDE}", forms=dict(e.forms)) from None
        raise Refusal.of_period(e) from None
    if not both:
        return words
    if not dates:
        return replace(words, note=IGNORED_DATES)
    try:
        as_dates = read(None, start or _moment_text(words.start, whole_days), end)   # (`end` alone: from the period's start)
    except PeriodError as e:
        why = e.message if start else f"`end` {end!r} is before the period's start."
        raise Refusal(f"`period` {period!r} is {_span(words.text)}, and `start`/`end` can't be read as this tool takes them: "
                      f"{why} Send one of them.") from None
    floor = None if first_held is None else first_held if isinstance(first_held, datetime) else \
        datetime.combine(first_held, datetime.min.time(), tzinfo=tz)
    if floor is not None and floor.tzinfo is None:
        floor = floor.replace(tzinfo=tz)
    starts = [p.start if floor is None else max(p.start, floor) for p in (words, as_dates)]
    if abs(starts[0] - starts[1]) <= SAME_WITHIN and abs(words.end - as_dates.end) <= SAME_WITHIN:
        return replace(words, note=SAME_DATES)
    said = f"{_moment_text(as_dates.start, False, words=True)} – now" if not end and not whole_days else _span(as_dates.text)
    raise Refusal(f"`period` {period!r} is {_span(words.text)} but `start`/`end` say {said}: send one of them (`period` alone, or "
                  f"`start` and `end` without it).")


# ---------- an answer ----------
FRESH = ("These figures are as of `as_of`. A follow-up or a challenge (\"that's wrong, it was cooler\") needs this tool called again: "
         "never answer it from these.")
HEAD = ("period", "as_of", "fresh")   # the keys every answer starts with (a tool's own fields can't take them)


def iso(t: datetime | int | float, tz: tzinfo | None = None) -> str:
    """A time as the service writes it for an application: ISO local time with its UTC offset, to the second ("2026-11-10T14:05:00+11:00").
    `t` is an aware datetime (written in its own zone), or an epoch written in `tz`."""
    if isinstance(t, datetime):
        if t.tzinfo is None:
            raise ValueError(f"a time needs its zone: {t!r}")
        return t.isoformat(timespec="seconds")
    if tz is None:
        raise ValueError("an epoch needs the zone to write it in")
    return datetime.fromtimestamp(t, tz).isoformat(timespec="seconds")


def period_field(period: Period) -> dict:
    """{"asked", "text", "start", "end", ["cut"], ["note"]}: how the period was read."""
    out = {"asked": period.asked, "text": period.text, "start": iso(period.start), "end": iso(period.end)}
    if period.cut:
        out["cut"] = period.cut
    if period.note:
        out["note"] = period.note
    return out


def as_of_field(as_of: datetime) -> dict:
    """{"text": "Tue 10 Nov 2026 2:05pm", "time": ISO}: the time of the figures (aware, written in its own zone: the station's)."""
    return {"text": f"{day_text(as_of)} {clock_text(as_of)}", "time": iso(as_of)}


def answer(*, period: Period | None, as_of: datetime | None, **fields) -> str:
    """A tool's answer as compact JSON: `period` (period_field) and `as_of` (as_of_field) with the FRESH line, then the tool's own fields
    in the order given. A tool with no period (current, outlook) passes None, and one with no figures (about) None for `as_of`."""
    if clash := set(HEAD) & set(fields):
        raise ValueError(f"an answer's own fields can't be named {', '.join(sorted(clash))}")
    head = {}
    if period is not None:
        head["period"] = period_field(period)
    if as_of is not None:
        head["as_of"], head["fresh"] = as_of_field(as_of), FRESH
    return json_result({**head, **fields})


# ---------- charts ----------
CHART_NOTE = "The chart's data is in `charts`: draw it if you can, and keep your text to the figures. Don't describe the chart."


def charts_field(charts: list[specs.Chart], tz: tzinfo, meta: dict | None = None) -> list[dict]:
    """The charts as data (specs.to_json, schemas/chart.json), each line bucketed to the request's `chart_points` (default CHART_POINTS,
    at most MAX_CHART_POINTS; AS_DRAWN: as drawn), times local to `tz`."""
    points = request_meta(meta)["chart_points"]
    return [specs.to_json(chart, tz, max_points=None if points == AS_DRAWN else points) for chart in charts]


def with_charts(charts: list[specs.Chart], tz: tzinfo, meta: dict | None = None) -> dict:
    """The fields an answer with charts ends with, {"chart_note": CHART_NOTE, "charts": charts_field(...)}; {} with no chart:
    answer(..., **with_charts(charts, tz, meta))."""
    return {"chart_note": CHART_NOTE, "charts": charts_field(charts, tz, meta)} if charts else {}


def complete(reply: str) -> dict:
    """The fields of an answer that is complete (3.7: `chart_answers_it` set and a chart made, with no error and no note that needs
    the model's words): {"reply": the finished reply for the person, "complete": True}. A client may send `reply` with the chart and end
    the turn; over MCP the server sends it as a text block for the user and marks the result `ecobot/complete`."""
    return {"reply": reply, "complete": True}


__all__ = ["AS_DRAWN", "BACKGROUND", "CHART_NOTE", "CHART_POINTS", "FRESH", "IGNORED_DATES", "SAME_DATES", "SET_ASIDE", "MAX_CHART_POINTS", "META", "NORMAL", "PERIOD_FORMS", "Refusal",
           "ServiceTool", "WHOLE_DAY_FORMS", "answer", "as_of_field", "charts_field", "complete", "iso", "period_field", "period_properties",
           "read_period", "refuse", "sends_period", "refuse_period", "request_meta", "turn_meta", "with_charts"]
