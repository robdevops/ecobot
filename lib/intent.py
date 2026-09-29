"""What a message is asking for, decided with patterns rather than the model.

  - how much the model should think (only predictions and "describe it" questions get reasoning);
  - whether fresh data must be fetched first (weather / air questions);
  - the fast path: simple "period + highs/lows", chart and "air quality now" questions become a
    tool call built here, so the model's first round trip is skipped and it is only invoked once
    the data is in, to word the answer. Anything not clearly matched returns None and takes the
    normal path, so strictness is the point.
"""

import re
from datetime import date, datetime, timedelta

I = re.IGNORECASE

# ---------- reasoning and fetching ----------
EFFORT_DEFAULT, EFFORT_DESCRIBE, EFFORT_FORECAST = "none", "low", "medium"

# Messages about the weather or air must fetch fresh data; anything else (thanks, chat) needn't
WEATHER = re.compile(
    r"\b(weather|temp\w*|hot\w*|cold\w*|warm\w*|cool\w*|heat\w*|freez\w*|frost\w*|degrees?|celsius|"
    r"rain\w*|showers?|drizzle|storms?|thunder\w*|hail|snow|fog\w*|cloud\w*|sun\w*|uv|solar|"
    r"wind\w*|gusts?|breez\w*|humid\w*|dew|pressure|barometer|forecast\w*|umbrella|"
    r"highs?|lows?|max\w*|min\w*|records?|extremes?|average|chart\w*|graph\w*|plot\w*|trend\w*|"
    r"indoors?|outdoors?|inside|outside|conditions|feels?|today|tonight|tomorrow|yesterday|"
    r"week|month|year|now|currently|current|air|aqi|pm ?2\.?5|pm ?10|co2|voc\w*|nox|smok\w*|pollut\w*|stuffy)\b|°|"
    r"\b\d{1,2}(st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*|"
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}\b|\b\d{1,2}/\d{1,2}\b", I)

# Describing a day or playing along with a hypothetical benefits from a little thinking
DESCRIBE = re.compile(
    r"\b(what\b.*\blike|describe\w*|imagine\w*|picture|pretend|feel like|felt like|"
    r"if (we|i|you|someone|anyone) (were|was|had|lived))\b", I)

# Looking ahead, not weather words alone ("how much rain fell" is a lookup)
FORECAST = re.compile(
    r"\b(will it|will there|is it going to|going to (rain|be|get|stay)|gonna|forecast\w*|umbrella|"
    r"tonight|tomorrow|later|soon|coming|next (few )?(hour|hours)|this (afternoon|evening)|"
    r"should i|chance of|likely|expect\w*|predict\w*|outlook)\b", I)


# Combining readings across days or conditions ("the hottest day that also rained", "how many days ...")
ANALYSIS = re.compile(
    r"\b(also|both|same day|at the same time|coincid\w*|correlat\w*|combination|how many days|how often|"
    r"(days?|times?|when) (when|that|where|with|it)|(that|where|when) (it )?(also|and)|"
    r"(hottest|coldest|wettest|driest|windiest|warmest|coolest)\b.*\b(that|where|when|but|and)\b)", I)


def reasoning_effort(text: str) -> str:
    """Thinking is for judgement calls only: predictions (medium), and analysis across days or readings
    or describing a day (low). Lookups get none."""
    if FORECAST.search(text):
        return EFFORT_FORECAST
    return EFFORT_DESCRIBE if ANALYSIS.search(text) or DESCRIBE.search(text) else EFFORT_DEFAULT


def needs_data(text: str) -> bool:
    return bool(WEATHER.search(text))


# ---------- periods ----------
def period_ranges(today: date) -> dict[str, tuple[date, date]]:
    """Named periods. The prompt's date list and the fast path both use these."""
    week_start = today - timedelta(days=today.weekday())  # Monday
    month_start = today.replace(day=1)
    prev_month_end = month_start - timedelta(days=1)
    return {
        "today": (today, today),
        "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
        "last 7 days": (today - timedelta(days=6), today),
        "current calendar week": (week_start, today),
        "last week": (week_start - timedelta(days=7), week_start - timedelta(days=1)),
        "this month": (month_start, today),
        "last month": (prev_month_end.replace(day=1), prev_month_end),
        "this year": (today.replace(month=1, day=1), today),
        "last year": (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
        "past month": (today - timedelta(days=29), today),
        "past year": (today - timedelta(days=364), today),
        "on record": (today - timedelta(days=1459), today),  # what Ecowitt keeps (also safe on 29 Feb)
    }


# Phrase -> period name. Numbered periods ("4 months", "24h", "last 7 days") are parsed separately.
PERIOD_PHRASES = [
    # "the last year" / "in the last year" = rolling; bare "last year" = previous calendar year
    (r"(on record|all[- ]time|of all time|ever recorded|ever)", "on record"),
    (r"(the )?past year|the last year", "past year"),
    (r"(the )?past month|the last month", "past month"),
    (r"(the )?past week|the last week|this week", "last 7 days"),
    (r"(?<!the )last week", "last week"),
    (r"this month", "this month"),
    (r"(?<!the )last month", "last month"),
    (r"this year", "this year"),
    (r"(?<!the )last year", "last year"),
    (r"(the )?(last|past) day", "last 24 hours"),
    (r"yesterday", "yesterday"),
    (r"today|so far today", "today"),
]
NUMBER_WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve".split())}
NUMBERED_PERIOD = re.compile(
    r"(?<![\d.])(\d+|" + "|".join(list(NUMBER_WORDS)[1:]) + r") ?(hours?|hrs?|h|days?|d|weeks?|w|months?|mo|m|years?|y)\b(?! ago)", I)
# Any of these left unrecognised means the question names a period we can't read: let the model decide
TIME_WORDS = re.compile(r"\b(hours?|days?|weeks?|months?|years?|quarter|fortnight|decade|since|between|until|ago|ytd)\b", I)


def span(name: str, now: datetime) -> tuple[datetime, datetime]:
    """Start and end of a named period. The rolling "last 24 hours" ends now; others cover whole days."""
    if name == "last 24 hours":
        return now - timedelta(hours=24), now
    first, last = period_ranges(now.date())[name]
    return datetime.combine(first, datetime.min.time()), datetime.combine(last, datetime.max.time()).replace(microsecond=0)


MAX_PERIOD_DAYS = 3650  # ten years: well past the four Ecowitt keeps, which the tools trim to what exists


def numbered_span(count: str, unit: str, now: datetime) -> tuple[str, datetime, datetime] | None:
    """"4 months", "24h", "2 weeks": a rolling period ending now. One day means the last 24 hours;
    longer ones cover whole days up to today (a month is 30 days, a year 365, as in the named periods)."""
    n = int(count[:6]) if count.isdigit() else NUMBER_WORDS[count.lower()]  # 6 digits at most: nothing sane is longer
    u = unit.lower()
    if n == 0:
        return None
    if u[0] == "h" or (u[0] == "d" and n == 1):
        hours = n if u[0] == "h" else 24
        if hours > 24 * MAX_PERIOD_DAYS:
            return None
        return f"last {hours} hours", now - timedelta(hours=hours), now
    days = n if u[0] == "d" else 7 * n if u[0] == "w" else 365 * n if u[0] == "y" else round(n * 365 / 12)
    if days > MAX_PERIOD_DAYS or n > 99999:  # beyond what any sensor has: the model can explain, dates would be nonsense
        return None
    start = datetime.combine(now.date() - timedelta(days=days - 1), datetime.min.time())
    label = {"d": "days", "w": "weeks", "y": "years", "m": "months"}[u[0]]
    return f"last {n} {label[:-1] if n == 1 else label}", start, datetime.combine(now.date(), datetime.max.time()).replace(microsecond=0)


def spans_in(text: str, now: datetime) -> list[tuple[str, datetime, datetime]]:
    """Every distinct period the text names, as (name, start, end)."""
    found: dict = {}
    for phrase, name in PERIOD_PHRASES:
        if re.search(rf"\b({phrase})\b", text, I):
            found.setdefault(span(name, now), name)
    for m in NUMBERED_PERIOD.finditer(text):
        if numbered := numbered_span(m.group(1), m.group(2), now):
            found.setdefault(numbered[1:], numbered[0])
    if not found and (bare := BARE_PERIOD.search(text)):  # "weather week", "aq month": as if "this ..."
        name = BARE_PERIODS[bare.group(1).lower()]
        found[span(name, now)] = name
    return [(name, *window) for window, name in found.items()]


# ---------- weather fast path ----------
EXTREMES = re.compile(r"\b(hottest|coldest|warmest|coolest|highest|lowest|highs?|lows?|max(imum)?|min(imum)?|"
                      r"extremes?|temperatures?|temps?)\b", I)
GRAPH = re.compile(r"\b(graph\w*|chart\w*|plot\w*|trend\w*|visuali[sz]\w*)\b", I)
# Anything that needs other data, a judgement, or a comparison goes the normal way
NOT_SIMPLE = re.compile(r"\b(rain\w*|wind\w*|gusts?|pressure|humid\w*|uv|solar|lightning|pm ?2\.?5|pm2|pm ?10|pm ?1|"
                        r"air|air quality|aqi?|co2|co₂|voc\w*|nox|smok\w*|pollut\w*|airgradient|"
                        r"compare\w*|vs|versus|than|average|mean|median|why|how many|days (above|below|over|under)|"
                        r"feels?|dew|forecast\w*|will|going to|tomorrow|tonight|later|now|current\w*|right now)\b", I)
# Wind is fine for the fast path when it is a plain chart request ("wind direction plot 3m")
WIND = re.compile(r"\b(wind\w*|gusts?|breez\w*)\b", I)
NOT_SIMPLE_WIND_OK = re.compile(NOT_SIMPLE.pattern.replace(r"wind\w*|gusts?|", ""), I)
# What a chart request with no period must name to default to a week ("chart it" refers back instead)
WEATHER_SUBJECT = re.compile(r"\b(weather|temp\w*|hot\w*|cold\w*|warm\w*|cool\w*|highs?|lows?|indoors?|outdoors?|"
                             r"inside|outside|station)\b", I)
WEATHER_WORD = re.compile(r"\b(weather|conditions)\b", I)
# A period named on its own ("weather week", "aq month") counts as "this week/month/year"
BARE_PERIODS = {"week": "last 7 days", "month": "this month", "year": "this year"}
BARE_PERIOD = re.compile(r"\b(week|month|year)\b", I)
INDOOR = re.compile(r"\b(indoors?|inside)\b", I)
OUTDOOR = re.compile(r"\b(outdoors?|outside)\b", I)
FMT = "%Y-%m-%d %H:%M:%S"


def weather_period(text: str, now: datetime) -> tuple[str, datetime, datetime] | None:
    """(period name, start, end) for a simple highs/lows or chart request, else None. "chart the
    past week" counts too: the chart plus a highs/lows summary is the answer."""
    if (NOT_SIMPLE_WIND_OK if GRAPH.search(text) else NOT_SIMPLE).search(text):
        return None
    spans = spans_in(text, now)
    # "weather <period>" is a summary request too, but "weather today" also wants current conditions
    named_weather = bool(WEATHER_WORD.search(text)) and bool(spans) and {n for n, _, _ in spans} != {"today"}
    if not (EXTREMES.search(text) or GRAPH.search(text) or named_weather):
        return None
    if not spans and GRAPH.search(text) and WEATHER_SUBJECT.search(text) and not TIME_WORDS.search(text):
        spans = [("last 7 days", *span("last 7 days", now))]  # "chart weather": a week is the natural default
    return spans[0] if len(spans) == 1 else None  # none, or several ("this week vs last week"): the model decides


def weather_groups(text: str) -> str:
    """Just indoor or just outdoor if only one is asked about, otherwise both. A wind chart is just wind."""
    if WIND.search(text):
        return "wind"
    indoor, outdoor = bool(INDOOR.search(text)), bool(OUTDOOR.search(text))
    return "indoor" if indoor and not outdoor else "outdoor" if outdoor and not indoor else "outdoor,indoor"


# ---------- air-quality fast path ----------
# "how's the air?", "what's the AQI", "report my airgradient aq". History, comparisons and mixed
# weather questions go the normal way.
AIR = re.compile(r"\b(air|aqi|aq|pm ?2\.?5|pm ?10|pm ?1|co2|voc\w*|nox|smok\w*|pollut\w*|airgradient)\b", I)
AIR_QUALITY = re.compile(r"\bair quality\b", I)
AIR_NOT_NOW = re.compile(
    r"\b(yesterday|overnight|last|past|week|month|year|since|earlier|this morning|was|were|been|trend\w*|"
    r"compare\w*|vs|versus|than|graph\w*|chart\w*|plot\w*|why|forecast\w*|tomorrow|tonight|later|will|"
    r"average|highest|lowest|peak|max\w*|min\w*|record\w*|"
    r"temp\w*|rain\w*|wind\w*|humid\w*|hot|cold|warm|pressure|weather)\b", I)
AIR_CHART_NOT = re.compile(r"\b(compare\w*|vs|versus|than|why|forecast\w*|tomorrow|will|"
                           r"temp\w*|rain\w*|wind\w*|humid\w*|hot|cold|warm|pressure|weather)\b", I)
AIR_METRICS = [  # word -> metric; checked in order, so PM10 wins over PM1
    (r"pm ?2\.?5|pm25", "pm2_5"), (r"pm ?10", "pm10"), (r"pm ?1(?![0-9])", "pm1"), (r"co2|co₂", "co2"),
    (r"voc\w*", "voc_index"), (r"nox", "nox_index"),
]


def mentions_air(text: str) -> bool:
    return bool(AIR.search(text) or AIR_QUALITY.search(text))


def air_period(text: str, now: datetime) -> tuple[str, datetime, datetime] | None:
    """(period name, start, end) for a plain air-quality chart request, else None. Any air question
    with a period ("aq 1d", "air quality this week") is a chart; a graph word alone means the last
    24 hours."""
    if not mentions_air(text) or AIR_CHART_NOT.search(text):
        return None
    spans = spans_in(text, now)
    if not spans and GRAPH.search(text) and not TIME_WORDS.search(text):
        spans = [("last 24 hours", *numbered_span("24", "h", now)[1:])]
    return spans[0] if len(spans) == 1 else None


def air_metrics(text: str) -> list[str]:
    metrics = [m for _, m in AIR_METRICS] if re.search(r"\b(all|every\w*|each)\b", text, I) else []
    for pattern, metric in AIR_METRICS:
        if re.search(rf"\b({pattern})", text, I) and metric not in metrics:
            metrics.append(metric)
    return metrics or ["pm2_5"]


# ---------- dispatch ----------
# A particular date, weekday or time of day: the fast path only knows whole periods, so these go to the model
_MONTHS = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*"
SPECIFIC_MOMENT = re.compile(
    rf"\b\d{{1,2}}(st|nd|rd|th)?\s+(of\s+)?{_MONTHS}|\b{_MONTHS}\s+\d{{1,2}}\b|\b\d{{1,2}}/\d{{1,2}}\b|"
    r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b|\b\d{1,2}:\d{2}\b|\b(noon|midnight|morning|afternoon|evening|overnight|tonight)\b|"
    r"\b(mon|tues?|wed(nes)?|thu(rs?)?|fri|sat(ur)?|sun)(day)?\b", I)

def fast_call(text: str, now: datetime, ecowitt: bool, air: bool) -> tuple[str, dict, str] | None:
    """(tool name, arguments, what it is) for a question the bot can fetch for without the model."""
    if SPECIFIC_MOMENT.search(text):  # "high on 5 Jan this year", "at 3pm today": a whole period would be the wrong data
        return None
    if air and (period := air_period(text, now)):
        name, start, end = period
        return "air_quality", {"chart": True, "metrics": air_metrics(text),
                               "start_date": start.strftime(FMT), "end_date": end.strftime(FMT)}, f"air quality chart, {name}"
    if air and mentions_air(text) and not AIR_NOT_NOW.search(text) and not TIME_WORDS.search(text):
        return "air_quality", {}, "air quality now"
    if ecowitt and (period := weather_period(text, now)):
        name, start, end = period
        # 3+ days, an hours-long window, or whenever a graph is asked for
        chart = (end - start).days >= 2 or name.endswith("hours") or bool(GRAPH.search(text))
        return "weather_history", {"groups": weather_groups(text), "chart": chart, "start_date": start.strftime(FMT),
                                   "end_date": end.strftime(FMT)}, f"weather history, {name}"
    return None
