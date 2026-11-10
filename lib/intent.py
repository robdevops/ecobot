"""What the bot decides about a message with patterns rather than the model (docs/mcp-service.md 4):

  - how much the model should think (only predictions, analysis and "describe it" questions get reasoning);
  - the few shortcuts the bot answers in code, each a fixed call to a service tool: the report, the gauges, "weather now", and a
    named reading's chart and a period ("humidity chart 7d", "chart rain this month", "humidity 7d", the keyboard's and the alerts'
    sentences: `chart_call`). Anything not clearly matched goes to the model, so strictness is the point.

Nothing here reaches a tool except a shortcut's own call: the words are passed on as they are (the readings and the period), and
the service reads them.
"""

import logging
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from .series import AIR as AIR_TABLE

log = logging.getLogger(__name__)

I = re.IGNORECASE

# ---------- reasoning ----------
EFFORT_DEFAULT, EFFORT_DESCRIBE, EFFORT_THINK = "none", "low", "medium"
EFFORT_STEPS = ("none", "low", "medium", "high")


def lower_effort(effort: str) -> str | None:
    """One reasoning step down (medium > low > none), for asking again after a timeout; None when there is no lower step."""
    i = EFFORT_STEPS.index(effort) if effort in EFFORT_STEPS else 0
    return EFFORT_STEPS[i - 1] if i else None


# The two devices by name: "ecowitt" is the weather, "ag" / "airgradient" / "air gradient" the air quality
_ECOWITT = r"ecowitt"
_AG = r"air ?gradient|ag"
_AIR_METRIC = "|".join(m.words for m in AIR_TABLE.values())   # the words that name an air reading

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

EFFORT_RULES = ((EFFORT_THINK, (LINK, THINK)), (EFFORT_DESCRIBE, (FORECAST, ANALYSIS, DESCRIBE)))


def reasoning_effort(text: str) -> str:
    """Thinking is for judgement calls only: how one reading relates to another (medium), and looking ahead,
    analysis across days or readings or describing a day (low). Lookups get none, unless the person asks the bot to think,
    try, reason, predict, estimate, grind or whirl (medium)."""
    return next((effort for effort, patterns in EFFORT_RULES if any(p.search(text) for p in patterns)), EFFORT_DEFAULT)


# ---------- the report, the gauges, weather now ----------
def _only(words: str, lead: str = "") -> str:
    """A pattern for a message that is just these words (optionally with please, a lead-in and punctuation)."""
    return rf"^\s*(please\s+)?{lead}({words})(\s+please)?\s*[?.!]*\s*$"


_ASK = r"((give me|show me|show|get|what's|whats)\s+)?(the\s+)?"   # the usual lead-in: "show me the ...", "what's the ..."

# Any of these means the question names a period: the report, the gauges and "weather now" are the readings now
TIME_WORDS = re.compile(r"\b(hours?|days?|weeks?|months?|years?|quarter|fortnight|decade|since|between|until|ago|ytd|"
                        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(19|20)\d\d)\b", I)   # (a month and year: "oct 2025")

# "status", "report": the whole current picture from both devices (a period word or a subject makes it something else)
REPORT = re.compile(_only(r"status|report|sitrep|overview|summary|everything|what you'?ve got",
                          _ASK + r"(a\s+)?(full\s+|current\s+|complete\s+)?"), I)


def wants_report(text: str) -> bool:
    return bool(REPORT.search(text)) and not TIME_WORDS.search(text)


# "dashboard", "gauges": the station's readings as gauges, with each one's highs and lows (drawn in code from the station alone)
DASHBOARD = re.compile(_only(r"(weather\s+)?(dashboard|gauges?|dials?)(\s+(now|right now))?", _ASK), I)


def wants_dashboard(text: str) -> bool:
    return bool(DASHBOARD.search(text)) and not TIME_WORDS.search(text)


# "weather", "weather now", "current weather", "ecowitt": every reading the station has right now (the weather half of the report)
WEATHER_NOW = re.compile(_only(rf"(weather|{_ECOWITT}|conditions)(\s+(now|right now|currently|at the moment))?|(current|latest)\s+(weather|conditions)",
                               _ASK), I)


def wants_weather_now(text: str) -> bool:
    return bool(WEATHER_NOW.search(text)) and not TIME_WORDS.search(text)


# The report's calls: the readings now (both devices), the pollen and the forecast's next 3 days (what the report has always shown)
REPORT_CALLS = {"current": ("current", {}), "pollen": ("pollen", {}), "forecast": ("forecast", {"period": "next 3 days"})}
WEATHER_NOW_CALL = ("current", {})


def report_calls(sources: Collection[str]) -> list[tuple[str, dict]]:
    """The calls the report is written from, for these sources (by name)."""
    return [*([REPORT_CALLS["current"]] if {"Ecowitt", "AirGradient"} & set(sources) else []),
            *([REPORT_CALLS["pollen"]] if "Pollen" in sources else []),
            *([REPORT_CALLS["forecast"]] if "Forecast" in sources else [])]


# ---------- which source a question is about (each source's wants(): fetch its newest readings while the question is answered) ----------
POLLEN_WORDS = re.compile(r"\b(pollen|hay ?fever|thunderstorm asthma|asthma)\b", I)
AIR = re.compile(rf"\b(air|aqi|aq|{_AIR_METRIC}|smok\w*|pollut\w*|particulates?|particles?|{_AG})\b", I)
AIR_QUALITY = re.compile(r"\bair quality\b", I)


def mentions_pollen(text: str) -> bool:
    return bool(POLLEN_WORDS.search(text))


def mentions_air(text: str) -> bool:
    return bool(AIR.search(text) or AIR_QUALITY.search(text))


# ---------- a named reading's chart and a period ----------
CHART_WORDS = ("chart", "charts", "graph", "plot")
# The keyboard's and the alerts' sentences whose readings aren't the service's words (lib/templates.py, lib/alerts/pictures.py):
# their readings, as the service's words
SENTENCE_READINGS = {"air quality all metrics": ["air"], "particulates": ["pm2.5", "pm10", "pm1"]}
BARE_MIN = timedelta(days=1)   # "<reading> <period>" with no chart word is a chart only over more than a day ("humidity 7d"; not
                               # "rain today" or "rain yesterday": a figure to read), or over a day given as a number ("temperature 1d",
                               # "co2 24h": the chart's own 1D, as before step 3)
NUMERIC = re.compile(r"\d+\s*(d|h|days?|hours?|hrs?)", I)
APOSTROPHES = str.maketrans("’‘", "''")   # a phone's typographic apostrophes ("what’s"), read as the plain one every pattern uses


def _readings(words: list[str], catalogue) -> list[str] | None:
    """The readings these words name, as the person said them (each one the service's catalogue reads), else None."""
    said = " ".join(words)
    from .service.names import UnknownReading
    parts = list(SENTENCE_READINGS[said]) if said in SENTENCE_READINGS else [p.strip() for p in re.split(r",|&|\band\b", said)]
    if not all(parts):
        return None
    try:
        return parts if all(catalogue.resolve(p, default=None) for p in parts) else None
    except UnknownReading:
        return None


def _period(words: list[str], now: datetime, tz: tzinfo, bare: bool) -> str | None:
    """These words as a period the service reads (summary's, not whole days), passed on as they are; None when it can't read them (or,
    bare: when they are a day or less, but for a day given as a number: "1d", "24h")."""
    from .periods_text import PeriodError, read_period
    said = " ".join(words)
    if not said:
        return None
    try:
        read = read_period(said, None, None, now=now, tz=tz, default=said)
    except (PeriodError, ValueError):
        return None
    span = read.end - read.start
    return said if not bare or span > BARE_MIN or span == BARE_MIN and NUMERIC.fullmatch(said) else None


def chart_call(text: str, now: datetime, tz: tzinfo, catalogue=None) -> tuple[str, dict] | None:
    """The service call of a message that is only a named reading's chart and a period: "<readings> chart <period>", "chart <readings>
    <period>" or "<readings> <period>" (over more than a day, or "1d"): summary {readings: the words, period: the words, chart: true}; None
    for anything else (a question beside it, an average, two axes, no period: the model's). `catalogue`: the service's readings (default:
    every reading it knows)."""
    if catalogue is None:
        from .service.names import CATALOGUE as catalogue
    said = text.translate(APOSTROPHES).strip().rstrip("?.!").split()   # (the period goes on as it was said, case and all)
    words = [w.lower() for w in said]
    chart = [i for i, w in enumerate(words) if w in CHART_WORDS]
    if len(chart) > 1:
        return None
    if chart and chart[0] == 0:   # "chart <readings> <period>"
        splits = [(words[1:i], said[i:]) for i in range(2, len(words))]
    elif chart:                   # "<readings> chart <period>"
        splits = [(words[:chart[0]], said[chart[0] + 1:])]
    else:                         # "<readings> <period>"
        splits = [(words[:i], said[i:]) for i in range(1, len(words))]
    for named, period_words in splits:
        bare = not chart and " ".join(named) not in SENTENCE_READINGS   # ("Air quality all metrics 1d": a button's chart)
        if (readings := _readings(named, catalogue)) and (period := _period(period_words, now, tz, bare)):
            return "summary", {"readings": readings, "period": period, "chart": True}
    return None


# ---------- one message ----------
@dataclass
class Intent:
    """Everything decided about one message before the model sees it."""
    effort: str = EFFORT_DEFAULT
    report: bool = False                  # "status": the report, written in code
    dashboard: bool = False               # "dashboard": the station's gauges, one picture
    weather_now: bool = False             # "weather now": every reading the station has now, written in code
    chart: tuple[str, dict] | None = None   # a named reading's chart and a period: the service call that draws it

    @property
    def shortcut(self) -> bool:
        """Is it answered in code (no model)?"""
        return bool(self.report or self.dashboard or self.weather_now or self.chart)


def read(text: str, now: datetime, sources: Collection[str] = ("Ecowitt", "AirGradient"), tz: tzinfo | None = None,
         catalogue=None) -> Intent:
    """The decisions made in code for this message, given the sources the bot has (by name: "Ecowitt", "AirGradient", "Pollen",
    "Forecast"), at `now` (aware, or naive on the station's clock `tz`), over the service's catalogue (default: every reading)."""
    text = text.translate(APOSTROPHES)
    ecowitt = "Ecowitt" in sources
    tz = tz or now.tzinfo
    report = wants_report(text) and bool(report_calls(sources))
    shortcut = report or (ecowitt and (wants_dashboard(text) or wants_weather_now(text)))
    try:
        chart = None if shortcut or tz is None or not ({"Ecowitt", "AirGradient"} & set(sources)) else chart_call(text, now, tz, catalogue)
    except Exception as e:
        log.error("Couldn't read %r as a chart (%s: %s); the model takes it", text, type(e).__name__, e)
        chart = None
    return Intent(effort=reasoning_effort(text), report=report, dashboard=ecowitt and wants_dashboard(text),
                  weather_now=ecowitt and wants_weather_now(text) and not report, chart=chart)


__all__ = ["EFFORT_DEFAULT", "Intent", "chart_call", "lower_effort", "mentions_air", "mentions_pollen", "read", "reasoning_effort",
           "report_calls", "wants_dashboard", "wants_report", "wants_weather_now"]
