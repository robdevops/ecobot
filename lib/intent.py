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


def reasoning_effort(text: str) -> str:
    return EFFORT_FORECAST if FORECAST.search(text) else EFFORT_DESCRIBE if DESCRIBE.search(text) else EFFORT_DEFAULT


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
        "on record": (max(today - timedelta(days=1459), today.replace(year=today.year - 4)), today),
    }


# Phrase -> period name. Order matters: longer / more specific phrases first.
PERIOD_PHRASES = [
    # "the last year" / "in the last year" = rolling; bare "last year" = previous calendar year
    (r"(on record|all[- ]time|of all time|ever recorded|ever)", "on record"),
    (r"(past|last) (12|twelve) months|(the )?past year|the last year|last 365 days", "past year"),
    (r"(past|last) (30|thirty) days|(the )?past month|the last month", "past month"),
    (r"(past|last) (7|seven) days|(the )?past week|the last week|this week", "last 7 days"),
    (r"(?<!the )last week", "last week"),
    (r"this month", "this month"),
    (r"(?<!the )last month", "last month"),
    (r"this year", "this year"),
    (r"(?<!the )last year", "last year"),
    (r"(last|past) 24 ?(h|hrs?|hours?)|24 ?(h|hrs?|hours?)|(the )?(last|past) day", "last 24 hours"),
    (r"yesterday", "yesterday"),
    (r"today|so far today", "today"),
]


def periods_named(text: str) -> set[str]:
    return {name for phrase, name in PERIOD_PHRASES if re.search(rf"\b({phrase})\b", text, I)}


# ---------- weather fast path ----------
EXTREMES = re.compile(r"\b(hottest|coldest|warmest|coolest|highest|lowest|highs?|lows?|max(imum)?|min(imum)?|"
                      r"extremes?|temperatures?|temps?)\b", I)
GRAPH = re.compile(r"\b(graph\w*|chart\w*|plot\w*|trend\w*|visuali[sz]\w*)\b", I)
# Anything that needs other data, a judgement, or a comparison goes the normal way
NOT_SIMPLE = re.compile(r"\b(rain\w*|wind\w*|gusts?|pressure|humid\w*|uv|solar|lightning|pm ?2\.?5|pm2|pm ?10|pm ?1|"
                        r"air|air quality|aqi?|co2|co₂|voc\w*|nox|smok\w*|pollut\w*|airgradient|"
                        r"compare\w*|vs|versus|than|average|mean|median|why|how many|days (above|below|over|under)|"
                        r"feels?|dew|forecast\w*|will|going to|tomorrow|tonight|later|now|current\w*|right now)\b", I)
# What a chart request with no period must name to default to a week ("chart it" refers back instead)
WEATHER_SUBJECT = re.compile(r"\b(weather|temp\w*|hot\w*|cold\w*|warm\w*|cool\w*|highs?|lows?|indoors?|outdoors?|"
                             r"inside|outside|station)\b", I)
WEATHER_WORD = re.compile(r"\b(weather|conditions)\b", I)
# A period named on its own ("weather week") counts as "this week/month/year"
BARE_PERIODS = {"week": "last 7 days", "month": "this month", "year": "this year"}
BARE_PERIOD = re.compile(r"\b(week|month|year)\b", I)
INDOOR = re.compile(r"\b(indoors?|inside)\b", I)
OUTDOOR = re.compile(r"\b(outdoors?|outside)\b", I)
FMT = "%Y-%m-%d %H:%M:%S"


def weather_period(text: str, today: date, now: datetime) -> tuple[str, datetime, datetime] | None:
    """(period name, start, end) for a simple highs/lows or chart request, else None. "chart the
    past week" counts too: the chart plus a highs/lows summary is the answer."""
    if NOT_SIMPLE.search(text):
        return None
    found = periods_named(text)
    if not found and (bare := BARE_PERIOD.search(text)):
        found = {BARE_PERIODS[bare.group(1).lower()]}  # "weather week": as if "this week"
    # "weather <period>" is a summary request too, but "weather today" also wants current conditions
    named_weather = bool(WEATHER_WORD.search(text)) and found not in (set(), {"today"})
    if not (EXTREMES.search(text) or GRAPH.search(text) or named_weather):
        return None
    if not found and GRAPH.search(text) and WEATHER_SUBJECT.search(text):
        found = {"last 7 days"}  # "chart weather": a week is the natural default
    if len(found) != 1:  # no period, or several ("this week vs last week"): let the model decide
        return None
    name = found.pop()
    if name == "last 24 hours":
        return name, now - timedelta(hours=24), now
    first, last = period_ranges(today)[name]
    return name, datetime.combine(first, datetime.min.time()), datetime.combine(last, datetime.max.time()).replace(microsecond=0)


def weather_groups(text: str) -> str:
    """Just indoor or just outdoor if only one is asked about, otherwise both."""
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
    """(period name, start, end) for a plain air-quality chart request, else None. No period
    named: the last 24 hours. Longer than 14 days: the tool trims it and says so."""
    if not mentions_air(text) or not GRAPH.search(text) or AIR_CHART_NOT.search(text):
        return None
    found = periods_named(text)
    if len(found) > 1:
        return None
    if not found:
        return "last 24 hours", now - timedelta(hours=24), now
    name = found.pop()
    first, last = period_ranges(now.date())[name]
    return name, datetime.combine(first, datetime.min.time()), datetime.combine(last, datetime.max.time()).replace(microsecond=0)


def air_metrics(text: str) -> list[str]:
    metrics = [m for _, m in AIR_METRICS] if re.search(r"\b(all|every\w*|each)\b", text, I) else []
    for pattern, metric in AIR_METRICS:
        if re.search(rf"\b({pattern})", text, I) and metric not in metrics:
            metrics.append(metric)
    return metrics or ["pm2_5"]


# ---------- dispatch ----------
def fast_call(text: str, now: datetime, ecowitt: bool, air: bool) -> tuple[str, dict, str] | None:
    """(tool name, arguments, what it is) for a question the bot can fetch for without the model."""
    if air and (period := air_period(text, now)):
        name, start, end = period
        return "air_quality", {"chart": True, "metrics": air_metrics(text),
                               "start_date": start.strftime(FMT), "end_date": end.strftime(FMT)}, f"air quality chart, {name}"
    if air and mentions_air(text) and not AIR_NOT_NOW.search(text):
        return "air_quality", {}, "air quality now"
    if ecowitt and (period := weather_period(text, now.date(), now)):
        name, start, end = period
        chart = (end - start).days >= 2 or bool(GRAPH.search(text))  # 3+ days, or whenever a graph is asked for
        return "weather_history", {"groups": weather_groups(text), "chart": chart, "start_date": start.strftime(FMT),
                                   "end_date": end.strftime(FMT)}, f"weather history, {name}"
    return None
