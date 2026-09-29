"""Fast path: recognise simple "period + highs/lows" questions and build the history
tool call directly, skipping the model's first round trip. Anything not clearly
matched returns None and takes the normal path, so strictness is the point here.
"""

import re
from datetime import date, datetime, timedelta


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
EXTREMES = re.compile(r"\b(hottest|coldest|warmest|coolest|highest|lowest|highs?|lows?|max(imum)?|min(imum)?|"
                      r"extremes?|temperatures?|temps?)\b", re.IGNORECASE)
GRAPH = re.compile(r"\b(graph\w*|chart\w*|plot\w*|trend\w*|visuali[sz]\w*)\b", re.IGNORECASE)
# Anything that needs other data, a judgement, or a comparison goes the normal way
NOT_SIMPLE = re.compile(r"\b(rain\w*|wind\w*|gusts?|pressure|humid\w*|uv|solar|lightning|pm ?2\.?5|pm2|pm ?10|pm ?1|"
                        r"air|air quality|aqi?|co2|co\u2082|voc\w*|nox|smok\w*|pollut\w*|airgradient|"
                        r"compare\w*|vs|versus|than|average|mean|median|why|how many|days (above|below|over|under)|"
                        r"feels?|dew|forecast\w*|will|going to|tomorrow|tonight|later|now|current\w*|right now)\b",
                        re.IGNORECASE)


# What a chart request with no period must name to default to a week ("chart it" refers back instead)
WEATHER_SUBJECT = re.compile(r"\b(weather|temp\w*|hot\w*|cold\w*|warm\w*|cool\w*|highs?|lows?|indoors?|outdoors?|"
                             r"inside|outside|station)\b", re.IGNORECASE)


def match(text: str, today: date, now: datetime | None = None) -> tuple[str, date | datetime, date | datetime] | None:
    """(period name, first, last) for a simple highs/lows or chart request, else None. first/last
    are days, or exact times for the rolling "last 24 hours"."""
    # "chart the past week" counts too: the chart plus a highs/lows summary is the answer
    if not (EXTREMES.search(text) or GRAPH.search(text)) or NOT_SIMPLE.search(text):
        return None
    found = {name for phrase, name in PERIOD_PHRASES if re.search(rf"\b({phrase})\b", text, re.IGNORECASE)}
    if not found and GRAPH.search(text) and WEATHER_SUBJECT.search(text):
        found = {"last 7 days"}  # "chart weather": a week is the natural default ("chart it" is left to the model)
    if len(found) != 1:  # no period, or several ("this week vs last week"): let the model decide
        return None
    name = found.pop()
    if name == "last 24 hours":
        now = now or datetime.combine(today, datetime.now().time())
        return name, now - timedelta(hours=24), now
    first, last = period_ranges(today)[name]
    return name, first, last




INDOOR = re.compile(r"\b(indoors?|inside)\b", re.IGNORECASE)
OUTDOOR = re.compile(r"\b(outdoors?|outside)\b", re.IGNORECASE)


def groups(text: str) -> str:
    """Just indoor or just outdoor if only one is asked about, otherwise both."""
    indoor, outdoor = bool(INDOOR.search(text)), bool(OUTDOOR.search(text))
    if indoor and not outdoor:
        return "indoor"
    if outdoor and not indoor:
        return "outdoor"
    return "outdoor,indoor"


def wants_chart(text: str, first: date, last: date) -> bool:
    """Chart for periods of 3+ days, or whenever a graph is asked for."""
    return (last - first).days >= 2 or bool(GRAPH.search(text))


def tool_args(mac: str, first: date | datetime, last: date | datetime, chart: bool = False,
              callback: str = "outdoor,indoor") -> dict:
    fmt = "%Y-%m-%d %H:%M:%S"
    start = first if isinstance(first, datetime) else datetime.combine(first, datetime.min.time())
    end = last.strftime(fmt) if isinstance(last, datetime) else f"{last:%Y-%m-%d} 23:59:59"
    return {"mac": mac, "callback": callback, "chart": chart, "start_date": start.strftime(fmt), "end_date": end}


# Air quality right now: "how's the air?", "what's the AQI", "report my airgradient aq".
# History, comparisons and mixed weather questions go the normal way.
AIR = re.compile(r"\b(air|aqi|aq|pm ?2\.?5|pm ?10|pm ?1|co2|voc\w*|nox|smok\w*|pollut\w*|airgradient)\b", re.IGNORECASE)
AIR_NOT_NOW = re.compile(
    r"\b(yesterday|overnight|last|past|week|month|year|since|earlier|this morning|was|were|been|trend\w*|"
    r"compare\w*|vs|versus|than|graph\w*|chart\w*|plot\w*|why|forecast\w*|tomorrow|tonight|later|will|"
    r"average|highest|lowest|peak|max\w*|min\w*|record\w*|"
    r"temp\w*|rain\w*|wind\w*|humid\w*|hot|cold|warm|pressure|weather)\b", re.IGNORECASE)


def air_now(text: str) -> bool:
    """A plain "what's the air quality right now" question."""
    return bool(AIR.search(text)) and not AIR_NOT_NOW.search(text)


# Air-quality charts: "graph PM2.5 this week", "chart the air quality", "plot CO2 yesterday"
AIR_METRICS = [  # word -> metric; checked in order, so PM10 wins over PM1
    (r"pm ?2\.?5|pm25", "pm2_5"), (r"pm ?10", "pm10"), (r"pm ?1(?![0-9])", "pm1"), (r"co2|co\u2082", "co2"),
    (r"voc\w*", "voc_index"), (r"nox", "nox_index"),
]
AIR_CHART_NOT = re.compile(r"\b(compare\w*|vs|versus|than|why|forecast\w*|tomorrow|will|"
                           r"temp\w*|rain\w*|wind\w*|humid\w*|hot|cold|warm|pressure|weather)\b", re.IGNORECASE)


def air_chart(text: str, now: datetime) -> tuple[str, dict] | None:
    """(period description, air_quality tool args) for a plain air-quality chart request, else None.
    No period named: the last 24 hours. Longer than 14 days: the tool trims it and says so."""
    if not (AIR.search(text) or re.search(r"\bair quality\b", text, re.I)) or not GRAPH.search(text):
        return None
    if AIR_CHART_NOT.search(text):
        return None
    found = {name for phrase, name in PERIOD_PHRASES if re.search(rf"\b({phrase})\b", text, re.IGNORECASE)}
    if len(found) > 1:
        return None
    fmt = "%Y-%m-%d %H:%M:%S"
    if found:
        name = found.pop()
        first, last = period_ranges(now.date())[name]
        start, end = datetime.combine(first, datetime.min.time()), datetime.combine(last, datetime.max.time().replace(microsecond=0))
    else:
        name, start, end = "last 24 hours", now - timedelta(hours=24), now
    metrics = []
    if re.search(r"\b(all|every\w*|each)\b", text, re.IGNORECASE):
        metrics = [m for _, m in AIR_METRICS]  # "chart all airgradient" -> every metric
    for pattern, metric in AIR_METRICS:
        if re.search(rf"\b({pattern})", text, re.IGNORECASE) and metric not in metrics:
            metrics.append(metric)
    return name, {"chart": True, "metrics": metrics or ["pm2_5"],
                  "start_date": start.strftime(fmt), "end_date": end.strftime(fmt)}
