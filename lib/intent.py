"""What a message is asking for, decided with patterns rather than the model.

  - how much the model should think (only predictions and "describe it" questions get reasoning);
  - whether fresh data must be fetched first (weather / air questions);
  - the fast path: simple "period + highs/lows", chart and "air quality now" questions become a
    tool call built here, so the model's first round trip is skipped and it is only invoked once
    the data is in, to word the answer. Anything not clearly matched returns None and takes the
    normal path, so strictness is the point.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .series import PRESSURE_WORDS, SPECIFIC, TEMP_WORDS, WEATHER as WEATHER_READINGS

log = logging.getLogger(__name__)

I = re.IGNORECASE

# ---------- reasoning and fetching ----------
EFFORT_DEFAULT, EFFORT_DESCRIBE, EFFORT_FORECAST = "none", "low", "medium"
EFFORT_STEPS = ("none", "low", "medium", "high")


def lower_effort(effort: str) -> str | None:
    """One reasoning step down (medium > low > none), for asking again after a timeout; None when there is no lower step."""
    i = EFFORT_STEPS.index(effort) if effort in EFFORT_STEPS else 0
    return EFFORT_STEPS[i - 1] if i else None

# The words that name each reading, shared by every pattern below
_TEMP = TEMP_WORDS
# Which temperature words make a message a weather question that must fetch ("only that it's cold" is chat): "temp", "temperature",
# "how hot/cold ...", and the superlatives that rank days ("hottest weekends"). _TEMP (all the words) still names the reading.
_TEMP_ASK = r"temp\w*|how (?:hot|cold|warm|cool|freezing)|(?:hott|cold|warm|cool)est"
_WIND = r"wind\w*|gusts?|breez\w*"
_PRESSURE = PRESSURE_WORDS
# The two devices by name: "ecowitt" is the weather, "ag" / "airgradient" / "air gradient" the air quality
_ECOWITT = r"ecowitt"
_AG = r"air ?gradient|ag"
_OTHER_THAN_AIR = rf"temp\w*|rain\w*|wind\w*|humid\w*|hot|cold|warm|pressure|weather|{_ECOWITT}"  # a question that is not (only) about the air

# Messages about the weather or air must fetch fresh data; anything else (thanks, chat) needn't
WEATHER = re.compile(
    rf"\b(weather|{_ECOWITT}|{_AG}|{_TEMP_ASK}|frost\w*|pollen|hay ?fever|asthma|"
    rf"rain\w*|showers?|drizzle|storms?|thunder\w*|hail|snow|fog\w*|cloud\w*|sun\w*|uv|solar|"
    rf"{_WIND}|humid\w*|dew|{_PRESSURE}|forecast\w*|umbrella|"
    r"highs?|lows?|max\w*|min\w*|records?|extremes?|average|chart\w*|graph\w*|plot\w*|trend\w*|"
    r"indoors?|outdoors?|inside|outside|conditions|feels?|today|tonight|tomorrow|yesterday|"
    r"week|month|year|air|aqi|pm ?2\.?5|pm ?10|co2|voc\w*|nox|smok\w*|pollut\w*|stuffy)\b|°|"
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
    r"\b(also|both|same day|at the same time|coincid\w*|combination|how many days|how often|"
    r"(days?|times?|when) (when|that|where|with|it)|(that|where|when) (it )?(also|and)|"
    r"(hottest|coldest|wettest|driest|windiest|warmest|coolest)\b.*\b(that|where|when|but|and)\b)", I)


# Is one reading tied to another ("is there a correlation between pressure and rain"): the answer is a judgement
LINK = re.compile(
    r"\b(correlat\w*|(link|links|linked|connection|connected|relation|relationship|relationships|relate|related|relates)\b"
    r".*\b(between|to|with)\b|(between|with)\b.*\b(link|connection|relation\w*)|cross[- ]?referenc\w*|cross[- ]?check\w*|"
    r"(does|do|did|is|are)\b.*\b(affect\w*|influence\w*|predict\w*|cause\w*)\b.*\b(rain|pressure|humidity|wind|temperature)\w*)\b", I)


# Asking the model to think: "think about it", "try to work out why", "reason it through", "predict", "estimate", "grind", "whirl"
THINK = re.compile(r"\b(think\w*|try|trying|reason\w*|predict\w*|estimat\w*|grind\w*|whirl\w*)\b", I)

EFFORT_RULES = ((EFFORT_FORECAST, (LINK, THINK)), (EFFORT_DESCRIBE, (FORECAST, ANALYSIS, DESCRIBE)))


def reasoning_effort(text: str) -> str:
    """Thinking is for judgement calls only: how one reading relates to another (medium), and looking ahead,
    analysis across days or readings or describing a day (low). Lookups get none, unless the person asks the bot to think,
    try, reason, predict, estimate, grind or whirl (medium)."""
    return next((effort for effort, patterns in EFFORT_RULES if any(p.search(text) for p in patterns)), EFFORT_DEFAULT)


def _only(words: str, lead: str = "") -> str:
    """A pattern for a message that is just these words (optionally with please, a lead-in and punctuation)."""
    return rf"^\s*(please\s+)?{lead}({words})(\s+please)?\s*[?.!]*\s*$"


# "status", "report": the whole current picture from both devices (a period word or a subject makes it something else)
REPORT = re.compile(_only(r"status|report|sitrep|overview|dashboard|summary|everything|what you'?ve got",
                          r"((give me|show me|show|get|what's|whats)\s+)?(the\s+)?(a\s+)?(full\s+|current\s+|complete\s+)?"), I)


def wants_report(text: str) -> bool:
    return bool(REPORT.search(text)) and not TIME_WORDS.search(text)


def report_calls(ecowitt: bool, air: bool, pollen: bool = False, forecast: bool = False) -> list[tuple[str, dict]]:
    """What the full report needs, fetched together before the model sees the question."""
    return ([("weather_now", {"groups": NOW_GROUPS})] if ecowitt else []) + ([("air_quality", {})] if air else []) + (
        [("pollen_asthma", {"cached": True})] if pollen else []) + (
        [("weather_forecast", {"days": 3, "cached": True})] if forecast else [])


# Pollen and thunderstorm asthma (the Pollen source)
POLLEN_WORDS = re.compile(r"\b(pollen|hay ?fever|thunderstorm asthma|asthma)\b", I)
POLLEN_NOW = re.compile(_only(r"pollen( count| level| forecast| today| now)?|hay ?fever|(thunderstorm )?asthma( risk)?",
                              r"((give me|show me|show|get|what's|whats)\s+)?(the\s+)?"), I)


def mentions_pollen(text: str) -> bool:
    return bool(POLLEN_WORDS.search(text))


POLLEN_FILLER = frozenset("pollen hay fever hayfever asthma thunderstorm count counts level levels risk forecast today bad high like much "
                          "dangerous okay ok safe".split())


def pollen_ask(text: str) -> bool:
    """Is this only a plain question about the pollen or thunderstorm asthma ("is the pollen bad today", "how's the hay fever")?"""
    return bool(mentions_pollen(text)) and all(
        word in POLLEN_FILLER or word in FILLER for word in re.findall(r"[\w'’.]+", text.lower()))


# "weather", "weather now", "current weather", "ecowitt": every reading the station has right now (the weather half of the report)
NOW_GROUPS = "outdoor,indoor,pressure,wind,rainfall,solar_and_uvi"
WEATHER_NOW = re.compile(_only(rf"(weather|{_ECOWITT}|conditions)(\s+(now|right now|currently|at the moment))?|(current|latest)\s+(weather|conditions)",
                               r"((give me|show me|show|get|what's|whats)\s+)?(the\s+)?"), I)


def wants_weather_now(text: str) -> bool:
    return bool(WEATHER_NOW.search(text)) and not TIME_WORDS.search(text)


# Questions about the bot itself ("what metrics do you have", "list our sources"): answered from what it knows, no fetch
ABOUT_THE_BOT = re.compile(
    r"\b(what|which|list|show)\b.*\b(metrics?|sensors?|sources?|devices?)\b|"
    rf"\bwhat (can|do) you (do|measure|track|have|know|tell)\b|\bwhat can (i|we) ask\b|\bwhat (does|do) ({_ECOWITT}|{_AG}) (measure|track|have|report|tell)\b|"
    + _only(r"metrics?|sensors?|sources?|devices?", r"((our|the|my|available|all)\s+)*"), I)


def about_the_bot(text: str) -> bool:
    return bool(ABOUT_THE_BOT.search(text)) and not TIME_WORDS.search(text)


# Asking the bot to DO something ("add an alert if winds reach 100 km/h") is not a request for readings
COMMAND = re.compile(r"^\s*(please\s+)?(add|set( up)?|create|remind|schedule|turn (on|off)|enable|disable|mute|unmute|subscribe)\b", I)


# Asking for the bot's view ("what do you think of weather in general"): a conversation, not a lookup, unless it names a time or a reading now
OPINION = re.compile(r"\b(what do you think|your (view|opinion|thoughts?)|philosoph\w*|do you (like|prefer|love|hate|enjoy))\b", I)
RIGHT_NOW = re.compile(r"\b(today|tonight|tomorrow|yesterday|now|currently|current|right now|outside|forecast\w*|this (morning|afternoon|evening|week))\b", I)


def is_opinion(text: str) -> bool:
    return bool(OPINION.search(text)) and not RIGHT_NOW.search(text) and not TIME_WORDS.search(text)


def needs_data(text: str) -> bool:
    """Must the model fetch before answering? Weather and air words say yes, unless it is an instruction or a question about the bot
    or its opinion: those are not forced to call a tool."""
    return bool(WEATHER.search(text)) and not COMMAND.search(text) and not about_the_bot(text) and not is_opinion(text)


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


def _periods(text: str, now: datetime) -> dict[tuple[datetime, datetime], tuple[str, str]]:
    """{(start, end): (the words said, the period's name)} for every distinct period the text names."""
    found: dict = {}
    for phrase, name in PERIOD_PHRASES:
        if m := re.search(rf"\b({phrase})\b", text, I):
            found.setdefault(span(name, now), (m.group(0), name))
    for m in NUMBERED_PERIOD.finditer(text):
        if numbered := numbered_span(m.group(1), m.group(2), now):
            found.setdefault(numbered[1:], (m.group(0), numbered[0]))
    if not found and (bare := BARE_PERIOD.search(text)):  # "weather week", "aq month": as if "this ..."
        name = BARE_PERIODS[bare.group(1).lower()]
        found[span(name, now)] = (bare.group(0), name)
    return found


def spans_in(text: str, now: datetime) -> list[tuple[str, datetime, datetime]]:
    """Every distinct period the text names, as (name, start, end)."""
    return [(name, *window) for window, (_, name) in _periods(text, now).items()]


def period_hints(text: str, now: datetime) -> list[str]:
    """One line per period the person's words name, with its exact dates, for the model: "3m" = the last 3 months:
    2026-07-01 00:00:00 to 2026-09-30 23:59:59. The model otherwise guesses short forms (3m has been read as 3 days)."""
    return [f'"{said}" = {name}: {start:%Y-%m-%d %H:%M:%S} to {end:%Y-%m-%d %H:%M:%S}'
            for (start, end), (said, name) in _periods(text, now).items()]


# ---------- weather fast path ----------
EXTREMES = re.compile(r"\b(hottest|coldest|warmest|coolest|highest|lowest|highs?|lows?|max(imum)?|min(imum)?|"
                      r"extremes?|temperatures?|temps?)\b", I)
GRAPH = re.compile(r"\b(graph\w*|chart\w*|plot\w*|trend\w*|visuali[sz]\w*)\b", I)
# Anything that needs other data, a judgement, or a comparison goes the normal way
_NOT_SIMPLE = (r"rain\w*|pressure|humid\w*|uv|solar|lightning|pm ?2\.?5|pm2|pm ?10|pm ?1|"
               rf"air|air quality|aqi?|co2|co₂|voc\w*|nox|smok\w*|pollut\w*|{_AG}|"
               r"compare\w*|vs|versus|than|average|mean|median|why|how many|days (above|below|over|under)|"
               r"feels?|dew|forecast\w*|will|going to|tomorrow|tonight|later|now|current\w*|right now")
NOT_SIMPLE = re.compile(rf"\b(wind\w*|gusts?|{_NOT_SIMPLE})\b", I)
# Wind is fine for the fast path when it is a plain chart request ("wind direction plot 3m")
WIND = re.compile(rf"\b({_WIND})\b", I)
NOT_SIMPLE_WIND_OK = re.compile(rf"\b({_NOT_SIMPLE})\b", I)
# What a chart request with no period must name to default to a week ("chart it" refers back instead)
WEATHER_SUBJECT = re.compile(rf"\b(weather|{_ECOWITT}|{_TEMP}|highs?|lows?|indoors?|outdoors?|inside|outside|station)\b", I)
WEATHER_WORD = re.compile(rf"\b(weather|{_ECOWITT}|conditions)\b", I)
# A period named on its own ("weather week", "aq month") is the rolling week, month or year ending today, not the calendar
# one (on the 1st, "month" must not be just today)
BARE_PERIODS = {"week": "last 7 days", "month": "past month", "year": "past year"}
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


AVERAGE = re.compile(r"\b(averages?|avg|mean)\b", I)
ALL = re.compile(r"\b(all|every\w*|each)\b", I)
PER_DAY = re.compile(r"\b(each|every|per|by)\s+day|daily|day[- ]by[- ]day|which days|list|breakdown|one by one", I)


def _named(text: str) -> dict[str, int]:
    """The readings a question names, with where. "dew point temperature" names the dew point, not also the temperature."""
    found = {name: m.start() for name, r in WEATHER_READINGS.items() if (m := re.search(rf"\b({r.words})\b", text, I))}
    if "temperature" in found and any(name in found for name in SPECIFIC):
        del found["temperature"]
    return found


def chart_fields(text: str) -> list[str]:
    """The readings named in the text, in order, when it names two or more ("plot temperature and rain"); else []."""
    found = _named(text)
    if len(found) < 2 and ALL.search(text) and WEATHER_WORD.search(text):   # "weather all week": every reading, a panel each
        return list(WEATHER_READINGS)
    return sorted(found, key=found.get) if len(found) >= 2 else []


def chart_field(text: str) -> str | None:
    """The one reading a question is about, if it isn't temperature ("lowest and highest humidity"); None when it
    is about temperature, several readings, or none in particular."""
    found = list(_named(text))
    return WEATHER_READINGS[found[0]].field if len(found) == 1 and found[0] != "temperature" else None


# "rain chart 7d": a rain chart whose caption is just the least and most rain and whether more is expected
RAIN_CHART = re.compile(r"^\s*rain\s+(chart|graph|plot)\b[^,&+]*$", I)


def wants_rain_caption(text: str) -> bool:
    return bool(RAIN_CHART.search(text)) and not re.search(r"\band\b", text, I)


_NOT_CHART = re.compile(r"\b(compare\w*|vs|versus|than|why|how many|days (above|below|over|under)|will|going to|now|right now|current\w*)\b", I)
JUDGEMENT = (FORECAST, LINK, THINK, ANALYSIS, DESCRIBE)   # questions that want the model's thinking, not just a chart


def _sides(text: str) -> list[str]:
    indoor, outdoor = bool(INDOOR.search(text)), bool(OUTDOOR.search(text))
    return ["indoor"] if indoor and not outdoor else ["outdoor"] if outdoor and not indoor else ["outdoor", "indoor"]


def weather_chart(text: str, now: datetime) -> tuple[str, datetime, datetime, list[str]] | None:
    """(period name, start, end, Ecowitt groups) for a plain chart of named readings ("rain chart 7d", "plot temperature and
    humidity"), else None. A comparison, a forecast or a question that wants thinking is the model's."""
    if (not GRAPH.search(text) or mentions_air(text) or _NOT_CHART.search(text)
            or any(p.search(text) for p in JUDGEMENT) or not (fields := chart_fields(text) or list(_named(text)))):
        return None
    spans = spans_in(text, now)
    if not spans and not TIME_WORDS.search(text):
        spans = [("last 7 days", *span("last 7 days", now))]   # a chart with no period is a week
    if len(spans) != 1:
        return None
    groups = list(dict.fromkeys(WEATHER_READINGS[f].group for f in fields))
    if "outdoor" in groups and {"temperature", "humidity"} & set(fields):   # these two have an indoor sensor as well
        groups = [g for g in groups if g != "outdoor"] + _sides(text)
    return (*spans[0], list(dict.fromkeys(groups)))


AVERAGE_READINGS = ("temperature", "humidity", "pressure", "wind", "dew_point", "feels_like", "vpd", "solar", "uv")   # a rain total has no average


HILO = re.compile(r"\b(highest|lowest|highs?|lows?|max(imum)?|min(imum)?|peak|extremes?|strongest|fastest|hottest|coldest|warmest|coolest)\b", I)


def weather_average(text: str, now: datetime) -> tuple[str, datetime, datetime, list[str]] | None:
    """(period name, start, end, Ecowitt groups) for a plain "average temp last week" or "highest humidity yesterday" (the chart-less
    cousin of weather_chart)."""
    if (not (AVERAGE.search(text) or HILO.search(text)) or mentions_air(text) or _NOT_CHART.search(text) or any(p.search(text) for p in JUDGEMENT)
            or not (names := list(_named(text))) or not set(names) <= set(AVERAGE_READINGS)):
        return None
    spans = spans_in(text, now)
    if len(spans) != 1:
        return None
    groups = list(dict.fromkeys(WEATHER_READINGS[n].group for n in names))
    if "outdoor" in groups and {"temperature", "humidity"} & set(names):
        groups = [g for g in groups if g != "outdoor"] + _sides(text)
    return (*spans[0], list(dict.fromkeys(groups)))


def weather_groups(text: str) -> str:
    """Just indoor or just outdoor if only one is asked about, otherwise both. A wind chart is just wind."""
    if WIND.search(text):
        return "wind"
    indoor, outdoor = bool(INDOOR.search(text)), bool(OUTDOOR.search(text))
    return "indoor" if indoor and not outdoor else "outdoor" if outdoor and not indoor else "outdoor,indoor"


# ---------- air-quality fast path ----------
# "how's the air?", "what's the AQI", "report my airgradient aq". History, comparisons and mixed
# weather questions go the normal way.
AIR = re.compile(rf"\b(air|aqi|aq|pm ?2\.?5|pm ?10|pm ?1|co2|voc\w*|nox|smok\w*|pollut\w*|{_AG})\b", I)
AIR_QUALITY = re.compile(r"\bair quality\b", I)
AIR_NOT_NOW = re.compile(
    r"\b(yesterday|overnight|last|past|week|month|year|since|earlier|this morning|was|were|been|trend\w*|"
    r"compare\w*|vs|versus|than|graph\w*|chart\w*|plot\w*|why|forecast\w*|tomorrow|tonight|later|will|"
    r"average|highest|lowest|peak|max\w*|min\w*|record\w*|"
    rf"{_OTHER_THAN_AIR})\b", I)
AIR_CHART_NOT = re.compile(rf"\b(compare\w*|vs|versus|than|why|forecast\w*|tomorrow|will|{_OTHER_THAN_AIR})\b", I)
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


def air_named(text: str) -> list[str]:
    """The air readings the text names ("pm10", "co2"); all of them for "all"; none if it only says air."""
    metrics = [m for _, m in AIR_METRICS] if ALL.search(text) else []
    for pattern, metric in AIR_METRICS:
        if re.search(rf"\b({pattern})", text, I) and metric not in metrics:
            metrics.append(metric)
    return metrics


def air_metrics(text: str) -> list[str]:
    return air_named(text) or ["pm2_5"]


# ---------- dispatch ----------
# A particular date, weekday or time of day: the fast path only knows whole periods, so these go to the model
_MONTHS = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*"
SPECIFIC_MOMENT = re.compile(
    rf"\b\d{{1,2}}(st|nd|rd|th)?\s+(of\s+)?{_MONTHS}|\b{_MONTHS}\s+\d{{1,2}}\b|\b\d{{1,2}}/\d{{1,2}}\b|"
    r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b|\b\d{1,2}:\d{2}\b|\b(noon|midnight|morning|afternoon|evening|overnight|tonight)\b|"
    r"\b(mon|tues?|wed(nes)?|thu(rs?)?|fri|sat(ur)?|sun)(day)?\b", I)

# Plain lookups, answered in code from the tool's own result (no model): what is said is nothing but the reading asked for
FILLER = frozenset("what whats what's how hows how's is are it its it's the a an of tell me give show please like reading level speed "
                   "now currently current right at moment outside outdoors outdoor indoors indoor inside out there any we point".split())
FORECAST_PLAIN = re.compile(_only(r"(weather\s+)?forecast(\s+(for\s+)?(the\s+)?(this\s+|next\s+)?(week|7 days|seven days|coming days))?|"
                                  r"(the\s+)?(7|seven)[- ]day (weather )?forecast|(the\s+)?week ahead",
                                  r"((give me|show me|show|get|what's|whats)\s+)?(the\s+)?"), I)


def reading_now(text: str) -> list[str] | None:
    """The readings named when the text is only a plain question about them right now ("how hot is it", "is it raining", "uv"), else None."""
    if (GRAPH.search(text) or TIME_WORDS.search(text) or SPECIFIC_MOMENT.search(text) or mentions_air(text)
            or any(p.search(text) for p in JUDGEMENT) or not (names := list(_named(text))) or len(names) > 2):
        return None
    patterns = [re.compile(rf"({WEATHER_READINGS[n].words}|indoors?|outdoors?)", I) for n in names]
    for word in re.findall(r"[\w'’.]+", text.lower()):
        if word not in FILLER and not any(p.fullmatch(word) for p in patterns):
            return None
    return names


def fast_call(text: str, now: datetime, ecowitt: bool, air: bool, pollen: bool = False,
              forecast: bool = False) -> tuple[str, dict, str] | None:
    """(tool name, arguments, what it is) for a question the bot can fetch for without the model."""
    if SPECIFIC_MOMENT.search(text):  # "high on 5 Jan this year", "at 3pm today": a whole period would be the wrong data
        return None
    if air and (period := air_period(text, now)):
        name, start, end = period
        return "air_quality", {"chart": True, "metrics": air_metrics(text),
                               "start_date": start.strftime(FMT), "end_date": end.strftime(FMT)}, f"air quality chart, {name}"
    if air and mentions_air(text) and not AIR_NOT_NOW.search(text) and not TIME_WORDS.search(text):
        return "air_quality", {}, "air quality now"
    if pollen and (POLLEN_NOW.search(text) and not TIME_WORDS.search(text) or pollen_ask(text)):
        return "pollen_asthma", {}, "pollen and thunderstorm asthma"
    if ecowitt and wants_weather_now(text):
        return "weather_now", {"groups": NOW_GROUPS}, "weather now"
    if forecast and FORECAST_PLAIN.search(text):
        return "weather_forecast", {"days": 7}, "forecast"
    if ecowitt and reading_now(text):
        return "weather_now", {"groups": NOW_GROUPS}, "weather reading now"
    if ecowitt and (chart := weather_chart(text, now)):
        name, start, end, groups = chart
        return "weather_history", {"groups": ",".join(groups), "chart": True, "start_date": start.strftime(FMT),
                                   "end_date": end.strftime(FMT)}, f"weather chart, {name}"
    if ecowitt and (average := weather_average(text, now)):
        name, start, end, groups = average
        return "weather_history", {"groups": ",".join(groups), **({"average": True} if AVERAGE.search(text) else {}),
                                   "chart": (end - start).days >= 2 or name.endswith("hours"),
                                   "start_date": start.strftime(FMT), "end_date": end.strftime(FMT)}, f"weather history, {name}"
    if ecowitt and (period := weather_period(text, now)):
        name, start, end = period
        # 3+ days, an hours-long window, or whenever a graph is asked for
        chart = (end - start).days >= 2 or name.endswith("hours") or bool(GRAPH.search(text))
        return "weather_history", {"groups": weather_groups(text), "chart": chart, "start_date": start.strftime(FMT),
                                   "end_date": end.strftime(FMT)}, f"weather history, {name}"
    return None


# ---------- what the model needs for a question: guidance for the prompt, tools for the call ----------
DAYS_WORDS = re.compile(r"\b(days?|how many|how often|most|least|ranks?|ranking|top|worst|best|holidays?|weekends?|"
                        r"hottest|coldest|wettest|driest|windiest|warmest|coolest|compare\w*|versus|vs|yesterday)\b", I)
OUTLOOK_WORDS = re.compile(r"\b(rain\w*|umbrella|showers?|wet|storms?|later|soon|going to|will|about to)\b", I)
WIND_WORDS = re.compile(r"\b(wind\w*|gusts?|direction|breez\w*)\b", I)
BOT_WORDS = re.compile(r"\b(can|could|do|does|are|will)\s+(you|the bot)\b|\b(alerts?|notify|notification\w*|support\w*|capabilit\w*|able to|add|set up|remind)\b", I)
COMPARE = re.compile(r"\b(against|versus|vs|compare\w*|affect\w*|influence\w*|correlat\w*|relat\w*|link\w*|cause\w*)\b", I)
# Words that carry no topic of their own: a follow-up made only of these takes the topics of the messages before it
TOPIC_TOOLS = {"days": ["weather_days"], "link": ["weather_link"], "compose": ["plot_chart", "air_link", "air_scan"],
               "forecast": ["weather_forecast"], "pollen": ["pollen_asthma"]}
CORE_TOOLS = ("weather_now", "weather_history", "air_quality")


def topics(text: str, before: list[str] = ()) -> set[str]:
    """What guidance and tools the question needs beyond the basics, from its words and the messages just before it (a follow-up
    like "and indoors?" is about what came before). Generous on purpose: a topic that isn't needed costs a few tokens, one that
    is missing costs the answer."""
    found: set[str] = set()
    for said in (*before, text):
        found |= {name for name, hit in (
            ("air", mentions_air(said)), ("days", bool(DAYS_WORDS.search(said) or ANALYSIS.search(said) or SPECIFIC_MOMENT.search(said))),
            ("link", bool(LINK.search(said)) or bool(COMPARE.search(said)) and bool(OUTLOOK_WORDS.search(said))),
            ("outlook", bool(FORECAST.search(said)) or bool(OUTLOOK_WORDS.search(said))),
            ("forecast", bool(FORECAST.search(said)) or bool(OUTLOOK_WORDS.search(said))),
            ("wind", bool(WIND_WORDS.search(said))), ("describe", bool(DESCRIBE.search(said))),
            ("pollen", mentions_pollen(said)),
            ("bot", bool(BOT_WORDS.search(said)) or about_the_bot(said) or bool(COMMAND.search(said))),
            ("compose", mentions_air(said) and bool(COMPARE.search(said) or GRAPH.search(said) or LINK.search(said) or ANALYSIS.search(said))),
        ) if hit}
    return found


# "will it rain?", "do I need an umbrella?", "rain later?": the model reasons from the station, and the forecast (if on) comes with it
RAIN_AHEAD = re.compile(r"\b(umbrella|chance of rain|(will|going to|gonna|likely to|expect\w*)\b.*\brain\w*|"
                        r"rain\w*\b.*\b(later|soon|tonight|tomorrow|this (afternoon|evening)|coming|next)\b)", I)
WEEK_AHEAD = re.compile(r"\b(week|days|weekend|next \w+day)\b", I)


def forecast_prefetch(text: str, forecast: bool) -> list[tuple[str, dict]]:
    """The forecast fetched with the model's first step for a question about rain ahead (it is cached: no wait)."""
    if not forecast or not RAIN_AHEAD.search(text):
        return []
    return [("weather_forecast", {"days": 7 if WEEK_AHEAD.search(text) else 3, "cached": True})]


def tools_for(found: set[str]) -> list[str]:
    """The tool names a question can use: the basics, and what its topics add."""
    extra = ["air_days"] if {"days", "air"} <= found else []   # counting or ranking days by an air metric
    return [*CORE_TOOLS, *(t for topic in TOPIC_TOOLS if topic in found for t in TOPIC_TOOLS[topic]), *extra]


@dataclass
class Reading:
    """Everything decided about one message before the model sees it."""
    effort: str = EFFORT_DEFAULT
    needs_data: bool = False              # the model must call a tool first
    about_the_bot: bool = False           # answered without tools
    report: bool = False                  # "status": the full report
    hints: list[str] = field(default_factory=list)   # what the period words in the message mean
    fast: tuple[str, dict, str] | None = None        # (tool, arguments, what it is): fetched here, before the model
    chart_asked: bool = False             # "plot" means a chart, whatever the model calls
    chart_field: str | None = None        # humidity questions get a humidity chart
    chart_fields: list[str] = field(default_factory=list)   # "temperature and rain": one chart, a panel each
    average_asked: bool = False
    more: list[tuple[str, dict]] = field(default_factory=list)   # the rest of the report's calls (fast is the first)
    weather_now: bool = False             # "weather now": every reading the station has, in the report's layout
    rain_caption: bool = False            # "rain chart 7d": the caption is the least and most rain and whether rain is expected
    chart_in_code: bool = False           # a chart asked for plainly: fetched and captioned in code, no model
    readings: list[str] = field(default_factory=list)     # the readings the words name
    per_day: bool = False                 # figures day by day were asked for
    extra: list[tuple[str, dict]] = field(default_factory=list)   # fetched with the model's first step (the forecast, for "will it rain?")
    lookup: str = ""                      # "reading", "air", "pollen", "forecast" or "about": answered in code, no model
    lookup_arg: list[str] = field(default_factory=list)   # the readings named ("reading", "air"; none: all)
    sides: list[str] = field(default_factory=list)        # "indoor", "outdoor" or both, for a reading


def plain_lookup(text: str, fast: tuple | None) -> tuple[str, list[str]]:
    """(kind, readings named) for a question answered in code from one tool result, else ("", [])."""
    if about_the_bot(text):
        return "about", []
    if not fast:
        return "", []
    tool, args = fast[0], fast[1]
    if tool == "pollen_asthma":
        return "pollen", []
    if tool == "weather_forecast":
        return "forecast", []
    if any(p.search(text) for p in JUDGEMENT):
        return "", []
    if tool == "weather_history" and not args.get("chart") and not any(p.search(text) for p in JUDGEMENT):
        return "extremes", [n for n in _named(text)]
    if tool == "weather_now" and (names := reading_now(text)):
        return "reading", names
    if tool == "air_quality" and not args and len(text.split()) <= 6:
        return "air", air_named(text)
    return "", []


def read(text: str, now: datetime, ecowitt: bool = True, air: bool = True, pollen: bool = False,
         forecast: bool = False) -> Reading:
    """The decisions made in code for this message. A shortcut that fails is dropped: the model handles the question."""
    report = wants_report(text)
    calls = report_calls(ecowitt, air, pollen, forecast) if report else []
    try:
        fast = (*calls[0], "report") if calls else fast_call(text, now, ecowitt, air, pollen, forecast)
    except Exception:
        log.exception("Fast path failed; using the normal path")
        fast = None
    in_code = bool(fast and fast[0] in ("weather_history", "air_quality") and fast[1].get("chart")
                   and not any(p.search(text) for p in JUDGEMENT))
    rain_caption = ecowitt and wants_rain_caption(text)
    lookup, named = plain_lookup(text, fast) if not (report or in_code) else ("", [])
    if in_code and rain_caption:   # the caption also says whether rain is expected
        calls = [("", {}), ("weather_now", {"groups": "rainfall"})]
    return Reading(reasoning_effort(text), needs_data(text), about_the_bot(text), report, period_hints(text, now),
                   fast, bool(GRAPH.search(text)), chart_field(text), chart_fields(text), bool(AVERAGE.search(text)),
                   more=calls[1:], weather_now=ecowitt and wants_weather_now(text), rain_caption=rain_caption, chart_in_code=in_code,
                   lookup=lookup, lookup_arg=named, sides=_sides(text), extra=forecast_prefetch(text, forecast) if not fast else [], readings=list(_named(text)), per_day=bool(PER_DAY.search(text)))
