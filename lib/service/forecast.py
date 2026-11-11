"""The `forecast` tool (docs/ecobot-mcp.md 3.3): today and the days ahead, from the forecast source (lib/forecast, Open-Meteo), for a
period in words that look ahead ("tomorrow", "next 3 days", "this weekend"), read here: lib/periods_text.py reads only what has been
measured, and refuses the future by pointing to this tool.

The answer is today's weather_forecast's, in the service's shape: the same `lines` (lib/forecast/source.py day_label and describe_day),
each day's figures in `days`, `place`, `source` and `fetched_at`, and `text`, the bot's forecast section ("Forecast (Melbourne,
6:00pm)" and a bullet per line, lib/report.py's report and lookup) written here so a client can send it as it is.
Request `_meta` `fetch: false` answers from the forecast held, fetching nothing (the report's `cached`).

`section` writes a heading and bullets as the bot does for the sources that bring their own lines (pollen.py uses it too), and
`held_days` and `day_lines` are what outlook.py reads the forecast with.
"""

import logging
import re
from datetime import date, datetime, time, timedelta, tzinfo

from ..forecast.source import SOURCE, day_label, describe_day
from ..periods_text import NUMBER_WORDS, WEEKDAY_NAMES, Period, PeriodError, read_period as read_past
from ..timeutil import clock_text, day_text, to_local
from .common import Refusal, ServiceTool, answer, iso

log = logging.getLogger(__name__)

DEFAULT = "the next 7 days"   # weather_forecast's default: today and the six days after it
HEADING = "Forecast"          # the bot's heading for it (lib/report.py SECTIONS)
NO_FORECAST = "No forecast is available right now."   # (weather_forecast's own words)
FOR_WHAT_WAS = "summary (or days, for days ranked or counted)"

# ---------- the period, looking ahead ----------
_N = rf"(?P<n>\d+|{'|'.join(NUMBER_WORDS)})"
_WEEKDAYS = {name.lower(): i for i, name in enumerate(WEEKDAY_NAMES)} | {
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
# "over the weekend", "for tomorrow", "in the next 3 days" (but not "in 3 days", which is one day)
LEAD = re.compile(r"(?:in (?=the |next |coming |upcoming )|(?:over|during|for|within|across|throughout|on|from) )(?P<rest>.+)")
TODAY = re.compile(r"today|(?:the )?rest of (?:the|to)day|later(?: today| on)?|tonight|this (?:afternoon|evening)")
TOMORROW = re.compile(r"tomorrow(?: (?:morning|afternoon|evening|night))?")
AFTER_TOMORROW = re.compile(r"(?:the )?day after tomorrow")
NEXT_DAYS = re.compile(rf"(?:(?:the )?(?:next|coming|upcoming|following) )?{_N} ?(?:days?|d)")
WEEK_AHEAD = re.compile(r"this week|(?:the )?week ahead|(?:the )?(?:coming|upcoming|following) week|(?:the )?week|1 ?w")
WEEKEND = re.compile(r"(?:this |the )?weekend")
NEXT_WEEK = re.compile(r"next week")
WEEKDAY = re.compile(rf"(?:(?P<next>next|this|coming) )?(?P<wd>{'|'.join(sorted(_WEEKDAYS, key=len, reverse=True))})")
ISO_DAY = re.compile(r"\d{4}-\d\d-\d\d")
FORMS = (   # (form, an example read now): what a refusal lists
    ("today (also tonight, later today: the forecast is by day)", "today"),
    ("tomorrow", "tomorrow"),
    ("the day after tomorrow", "the day after tomorrow"),
    ("next N days, N days, Nd (N from 1: today and the N-1 days after it)", "next 3 days"),
    ("the next 7 days, this week, the week ahead, the coming week", "the next 7 days"),
    ("this weekend, the weekend (Saturday and Sunday; from today when it is one)", "this weekend"),
    ("next week (Monday to Sunday)", "next week"),
    ("Monday … Sunday, this Saturday, next Saturday (the next one; a bare weekday is today when it is one)", "Saturday"),
    ("a date, YYYY-MM-DD", None),
)


def days_text(first: date, last: date) -> str:
    """"Tue 10 Nov 2026", "Tue 10 – Mon 16 Nov 2026", "Mon 28 Dec 2026 – Sun 3 Jan 2027": days as the periods' texts write them."""
    if first == last:
        return day_text(first)
    if (first.year, first.month) == (last.year, last.month):
        return f"{first:%a} {first.day} – {day_text(last)}"
    return f"{day_text(first, year=first.year != last.year)} – {day_text(last)}"


def _ahead(p: str, today: date) -> tuple[str | None, date, date] | None:
    """(its label, the first day, the last day) of words that look ahead, or None when they are none of the forms."""
    if TODAY.fullmatch(p):
        return "today", today, today
    if TOMORROW.fullmatch(p):
        return "tomorrow", today + timedelta(days=1), today + timedelta(days=1)
    if AFTER_TOMORROW.fullmatch(p):
        return "the day after tomorrow", today + timedelta(days=2), today + timedelta(days=2)
    if m := NEXT_DAYS.fullmatch(p):
        n = int(m["n"]) if m["n"].isdigit() else NUMBER_WORDS[m["n"]]
        if n < 1:
            return None
        return ("today" if n == 1 else f"the next {n} days"), today, today + timedelta(days=n - 1)
    if WEEK_AHEAD.fullmatch(p):
        return "the next 7 days", today, today + timedelta(days=6)
    if WEEKEND.fullmatch(p):
        saturday = today + timedelta(days=(5 - today.weekday()) % 7) if today.weekday() < 5 else today - timedelta(days=today.weekday() - 5)
        return "this weekend", max(saturday, today), saturday + timedelta(days=1)
    if NEXT_WEEK.fullmatch(p):
        monday = today + timedelta(days=7 - today.weekday())
        return "next week", monday, monday + timedelta(days=6)
    if m := WEEKDAY.fullmatch(p):
        wd = _WEEKDAYS[m["wd"]]
        ahead = (wd - today.weekday()) % 7
        if m["next"] == "next" and not ahead:
            ahead = 7
        day = today + timedelta(days=ahead)
        return WEEKDAY_NAMES[wd], day, day
    if ISO_DAY.fullmatch(p):
        try:
            day = date.fromisoformat(p)
        except ValueError:
            return None
        return None, day, day
    return None


def _forms(today: date) -> dict[str, str]:
    """{form: what it means now}: every form a refusal lists, read today."""
    out = {}
    for form, example in FORMS:
        example = example or (today + timedelta(days=2)).isoformat()
        label, first, last = _ahead(example.lower(), today)
        out[form] = f"{label}: {days_text(first, last)}" if label else days_text(first, last)
    return out


def read_ahead(words: str | None, now: datetime, tz: tzinfo) -> Period:
    """The days a forecast is asked for, from words that look ahead (DEFAULT when none), as a Period: `start` the first day's midnight,
    `end` the midnight after the last, `text` how the words were read ("this weekend: Sat 14 – Sun 15 Nov 2026"). Raises Refusal: for
    words that are in the past (pointing to summary), and for words that are none of the forms (listing each with what it means now)."""
    asked = str(words).strip() if words is not None and str(words).strip() else DEFAULT
    local = now.astimezone(tz) if now.tzinfo else now
    today = local.date()
    it_is_now = f"It is now {day_text(local)} {clock_text(local)}."
    p = " ".join(asked.replace("’", "'").lower().split())
    lead = LEAD.fullmatch(p)
    read = _ahead(p, today) or (lead and _ahead(lead["rest"], today))
    if read and read[1] < today:
        raise Refusal(f"{asked!r} is in the past ({days_text(read[1], read[2])}): the forecast is for today and the days ahead. For what "
                      f"was measured, use {FOR_WHAT_WAS}. {it_is_now}", forms=_forms(today))
    if not read:
        try:   # words for what has been measured ("yesterday", "last week"): the tool for them is another
            past = read_past(asked, None, None, now=now, tz=tz, default=asked)
        except PeriodError:
            past = None
        if past is not None:
            raise Refusal(f"{asked!r} reads as a period that has been measured ({past.text}), not days ahead: the forecast is for today "
                          f"and the days ahead. For what was measured, use {FOR_WHAT_WAS}. {it_is_now}", forms=_forms(today))
        raise Refusal(f"{asked!r} isn't a period the forecast can read. Send `period` as one of these forms, as a whole phrase (N is a "
                      f"number, in digits or a word up to twelve). {it_is_now}", forms=_forms(today))
    label, first, last = read
    text = f"{label}: {days_text(first, last)}" if label else days_text(first, last)
    return Period(asked, datetime.combine(first, time(), tzinfo=tz), datetime.combine(last + timedelta(days=1), time(), tzinfo=tz), text)


# ---------- the forecast held ----------
async def held_days(forecast, today: date, fetch: bool) -> list[dict]:
    """The forecast's days from today on, as held; when it holds none of them and `fetch` allows, it is fetched first (Forecast.warm,
    which keeps to its own hours and pace), as weather_forecast does when nothing is held."""
    if fetch and not any(d["date"] >= today for d in forecast.days or []):
        try:
            await forecast.warm(True)
        except Exception as e:
            log.warning("Forecast not readable: %s", e)
    return [d for d in forecast.days or [] if d["date"] >= today]


def day_lines(days: list[dict], today: date) -> list[str]:
    """weather_forecast's lines for these days: "Today: 🌧️ Rain. 11–17°C, 90% chance of at least 1 mm", then each day by its
    weekday."""
    return [f"{day_label(d['date'], today)}: {describe_day(d)}" for d in days]


def day_figures(d: dict) -> dict:
    """A day of the forecast as figures: {date, min, max, summary, rain_chance, rain_1mm_pct} (°C and %; None where it has none)."""
    return {"date": d["date"].isoformat(), "min": d.get("min_c"), "max": d.get("max_c"), "summary": d.get("summary") or None,
            "rain_chance": d.get("rain_chance_pct"), "rain_1mm_pct": d.get("rain_1mm_pct")}


def section(heading: str, lines: list[str], *, place: str | None, fetched_at: float | None, tz: tzinfo, today: date) -> str:
    """A source's lines as the bot writes them in the report and alone (lib/report.py _heading, _own_lines): "Forecast (Melbourne,
    6:00pm)", the place and the time it was fetched (with its day when that isn't `today`), then "• " and each line."""
    stamp = None
    if fetched_at:
        at = to_local(int(fetched_at), tz)
        stamp = clock_text(at) if at.date() == today else f"{day_text(at, year=False)} {clock_text(at)}"
    note = ", ".join(x for x in (place, stamp) if x)
    return "\n".join([f"{heading} ({note})" if note else heading, *(f"• {line}" for line in lines)])


# ---------- the tool ----------
DESCRIPTION = (
    "The weather forecast (Open-Meteo) for the station's place: today and the days ahead, each day's summary with its emoji, its lowest "
    "and highest temperature and the chance of at least 1 mm of rain. For \"what's tomorrow like\": {\"period\": \"tomorrow\"}; \"the "
    "forecast for the weekend\": {\"period\": \"this weekend\"}; \"will it rain this week\", \"forecast\": {} (the next 7 days). A "
    "report (\"report\", \"status\", \"sitrep\") is current, forecast {\"period\": \"today\"} and pollen, called together.\n"
    "Not for: the next few hours from the station's own readings (\"will it rain soon\", \"do I need an umbrella\": outlook); what was "
    "measured, a device or reading with a period (\"weather week\", \"ecowitt week\": the past week, summary; days, for days "
    "ranked or counted). Only what is ahead is forecast.\n"
    "Result: `period` (how the words were read, with the dates); `text`, the whole answer for the person, ready to send; `lines`, one a "
    "day, emoji included: copy them as they are, with the place in parentheses after the heading, e.g. Forecast (Melbourne); `days`, "
    "each day's figures (min and max °C, rain_chance: Open-Meteo's chance of rain, rain_1mm_pct: the chance of at least 1 mm, the one "
    "the lines give when there is one); `place`, `fetched_at`; `note` when the forecast doesn't reach the end of the period.")
PARAMETERS = {"type": "object", "properties": {"period": {
    "type": "string",
    "description": "The days ahead in the person's own words, passed on unchanged: today, tomorrow, the day after tomorrow, next N days "
                   "(N from 1: today and the N-1 days after it; also N days, Nd), the next 7 days or this week, this weekend, next week "
                   "(Monday to Sunday), a weekday (Saturday: the next one, today when it is one), or a date (YYYY-MM-DD). The "
                   f"forecast reaches 7 days from today. Default: {DEFAULT}."}}}


def tools(ctx) -> list[ServiceTool]:
    """The `forecast` tool, when the forecast is switched on."""
    if ctx.forecast is None:
        return []

    async def handle(args: dict, meta: dict) -> str:
        fc = ctx.forecast
        tz, now = clock_of(ctx, fc.tz)
        today = now.date()
        if args.get("start") or args.get("end"):
            raise Refusal("The forecast takes `period` in words, not `start` and `end`: send one of these forms.", forms=_forms(today))
        period = read_ahead(args.get("period"), now, tz)
        first, last = period.first_day, period.last_day
        held = await held_days(fc, today, meta["fetch"])
        if not held:
            raise Refusal(NO_FORECAST)
        days = [d for d in held if first <= d["date"] <= last]
        reaches = held[-1]["date"]
        if not days:
            raise Refusal(f"The days asked for ({period.text}) are beyond the forecast, which reaches {day_text(reaches)}: ask for days "
                          f"up to then (the next 7 days is all of it).", reaches=reaches.isoformat())
        lines = day_lines(days, today)
        note = {"note": f"The forecast reaches only {day_text(reaches, year=False)}: the days after it aren't forecast yet."} \
            if reaches < last else {}
        fetched = to_local(int(fc.fetched_at), tz) if fc.fetched_at else None
        return answer(period=period, as_of=fetched,
                      text=section(HEADING, lines, place=fc.place, fetched_at=fc.fetched_at, tz=tz, today=today),
                      lines=lines, days=[day_figures(d) for d in days], **note,
                      **({"place": fc.place} if fc.place else {}), source=SOURCE,
                      **({"fetched_at": iso(fetched)} if fetched else {}))

    return [ServiceTool("forecast", DESCRIPTION, PARAMETERS, handle, open_world=True)]


def clock_of(ctx, fallback: tzinfo) -> tuple[tzinfo, datetime]:
    """(the station's zone, the time now in it) from the context; the source's own zone when the context has no device to name one
    (Context.zone asks the station or the air sensor)."""
    try:
        tz = ctx.zone
    except AttributeError:
        tz = fallback
    return tz, to_local(int(ctx.clock()), tz)


__all__ = ["DEFAULT", "DESCRIPTION", "PARAMETERS", "clock_of", "day_figures", "day_lines", "days_text", "held_days", "read_ahead",
           "section", "tools"]
