"""The `about` tool (docs/mcp-service.md 3.3): what the service measures and what it doesn't, how far back each kind of history goes,
the alerts there are with their thresholds, and that alerts can't be added or changed by asking. It replaces what the bot's prompt told
its model outside the tools (lib/prompt.py: NEVER_AVAILABLE, the capabilities, ALERT_WORDS), whose lists are copied here (the prompt
goes with the bot), and the bot's Help list (lib/templates.py MEASURES: `lines`, the same words).

  - measures: each device and service there is (the sources in the Context), in words and, for the sensors, the readings the catalogue
    holds, each with its name, label and unit;
  - history: for each sensor, from when to when each resolution is held, and on how many days, from the coverage of the file (the
    minutes, which have none, from the readings themselves): what "on record" reaches, and how fine a record is in each stretch of it;
  - alerts: each kind there is for the sources there are (lib/alerts/menu.py TYPES, copied), with what it is, its rule in words and its
    thresholds as figures, from the alert rules' own constants and the settings (RAIN_QUIET_HOURS, ALERT_COOLDOWN_MINUTES).

It takes no arguments and reads only what is held, so `fetch` and `priority` change nothing.
"""

from datetime import datetime, tzinfo

from ..alerts.forecast import BASELINE_HOUR, DRY_BELOW_PCT, RAIN_FROM_PCT, TEMP_CHANGE_C
from ..alerts.particulates import CHECK_SECONDS, MASK_PM10, MASK_PM25, SAFE_PM10, SAFE_PM25
from ..alerts.pollen_and_asthma import ALERT_FROM, WARN_FROM
from ..alerts.rules import CONFIRM_READINGS
from ..alerts.weather import CROSS_MARGIN, GUST_ALERT_KMH, GUST_REARM_SECONDS, UV_REARM_SECONDS
from ..airgradient.metrics import ALL_METRICS, CHART_UNITS, LABELS as AIR_LABELS
from ..ecowitt.api import daily_run
from ..ecowitt.glance import UVI_ALERT
from ..pollen.parse import LEVELS
from ..storage.catalogue import KINDS
from ..storage.coverage import merge
from ..storage.read import Reader
from ..storage.write import MINUTES_KEPT
from ..timeutil import clock_text, day_text, local_date
from .common import Refusal, ServiceTool, answer
from .context import Context
from .names import AIR_DEVICE, STATION

NAME = "about"

# ---------- the settings the alerts follow ----------
# The rain alert's quiet hours and the alerts' cooldown are the service's config (RAIN_QUIET_HOURS, ALERT_COOLDOWN_MINUTES): set from it at
# startup, as names.FEELS_LIKE_IN_ALL is. These are the config's defaults.
RAIN_QUIET_HOURS: tuple[int, int] | None = (0, 6)   # local hours with no rain alerts, a summary after; None: none
ALERT_COOLDOWN_MINUTES = 60                         # dry this long before "the rain has stopped"; the temperature crossing's least gap

# ---------- what the bot's prompt said (lib/prompt.py), copied: the prompt goes with the bot ----------
NEVER_AVAILABLE = ["lightning", "soil or extra sensor channels", "indoor air quality", "other stations or places"]
WITHOUT = {"Pollen": "pollen and thunderstorm asthma", "Forecast": "forecasts (only a short read of the pressure trend)"}   # a source that is off
ALERT_WORDS = {"rain": "rain starting or stopping", "gusts": f"wind gusts over {GUST_ALERT_KMH} km/h",
               "uv": f"UV index of {UVI_ALERT} or more", "temps": "indoor/outdoor temperature crossing",
               "particulates": "air-quality mask alerts", "pollen_and_asthma": "pollen or thunderstorm asthma risk reaching High",
               "forecast": f"a forecast it sent being revised (rain, or max temperature over {TEMP_CHANGE_C:g} degrees)"}
# The alert kinds (lib/alerts/menu.py TYPES, copied, without the irrigation controller's: that is a server of its own): kind -> (its name,
# the source that sends it)
TYPES = {"rain": ("rain", "Ecowitt"), "gusts": ("gusts", "Ecowitt"), "uv": ("UV", "Ecowitt"), "temps": ("temperature crossing", "Ecowitt"),
         "particulates": ("particulates", "AirGradient"), "pollen_and_asthma": ("pollen & asthma", "Pollen"),
         "forecast": ("forecast changes", "Forecast")}
# What each source measures, as the bot's Help lists it (lib/templates.py MEASURES)
MEASURES = {"Ecowitt": ["Temperature, humidity (indoor, outdoor)", "Dew point, vapour pressure deficit, pressure, wind speed",
                        "Rain, solar radiation, UV index"],
            "AirGradient": ["PM1, PM2.5, PM10, CO₂, VOC, NOx"],
            "Pollen": ["Pollen and thunderstorm asthma risk (October to December)"],
            "Forecast": ["Forecast"]}
STATION_TEXT = ("Weather station (Ecowitt): outdoor and indoor temperature and humidity, dew point, feels-like, vapour pressure deficit, "
                "pressure, wind speed, gust and direction, rain (daily total and rate), solar radiation and UV index.")
AIR_TEXT = ("Air quality (outdoor AirGradient): " + ", ".join(f"{AIR_LABELS[m]} ({CHART_UNITS[m] or 'index'})" for m in ALL_METRICS)
            + ", each with a traffic-light rating, and PM2.5 with its US AQI; also the sensor's own temperature and humidity "
              "(air.temperature, air.humidity).")
FORECAST_TEXT = "Forecast: today and the days ahead from Open-Meteo (summary, temperatures, chance of at least 1 mm of rain)."
POLLEN_TEXT = ("Pollen: Melbourne's grass pollen level and the thunderstorm asthma risk (Low, Moderate, High, Extreme). Both forecasts "
               "only run from October to December.")
ALERTS_NOTE = ("Alerts can't be added or changed by asking (\"tell me when the wind reaches 100\", another threshold, another reading): "
               "there are only these, at these thresholds. The service sends them to the apps that listen for them; which of them a "
               "person gets is set where they receive them.")
NOT_NOTE = "A reading, a place or a chart style not listed here can't be had by asking: say so."

# What each tool answers, for "what can you do": (tool, what, the sources it needs)
TOOLS = (("current", "the readings now, with today's, this month's and this year's highs and lows", ("Ecowitt", "AirGradient")),
         ("summary", "a period's highs and lows with when, averages, rain totals, air ratings, and charts of any readings",
          ("Ecowitt", "AirGradient")),
         ("readings", "any reading's values at an interval, from a minute to a day, each with its time", ("Ecowitt", "AirGradient")),
         ("days", "days found, ranked or counted by their figures (the wettest day, days over 35°C, rain per month), or over part of "
                  "each day (the overnight low)", ("Ecowitt", "AirGradient")),
         ("relate", "whether rain comes with a change in pressure, humidity or wind, and whether the air is cleaner after rain",
          ("Ecowitt",)),
         ("scan", "which air readings go with which weather readings", ("Ecowitt+AirGradient",)),
         ("outlook", "whether it will rain in the next few hours", ("Ecowitt",)),
         ("forecast", "today and the days ahead", ("Forecast",)),
         ("pollen", "grass pollen and thunderstorm asthma (October to December)", ("Pollen",)))

# The resolutions each sensor's history is held at, finest first, and what each is called
STATION_HELD = (("1min", "minute readings"), ("5min", "5-minute readings"), ("30min", "30-minute readings"), ("4hour", "4-hour records"),
                ("1day", "daily records"))
AIR_HELD = (("1min", "minute readings"), ("5min", "5-minute readings"), ("1hour", "hourly averages"))
MINUTE = "1min"   # held as readings only, for MINUTES_KEPT, with no coverage of their own

# ---------- the description ----------
DESCRIPTION = (
    "What this service is: what it measures and what it doesn't (no lightning, soil sensors, indoor air quality or other places), how "
    "far back each kind of history goes and how fine it is, which alerts there are with their thresholds, and what each tool answers. "
    "For \"what can you do\", \"what do you measure\", \"can you measure lightning\", \"how far back does the history go\", \"what "
    "alerts are there\", \"alert me when the wind reaches 100\": {} (no arguments). Alerts can't be added or changed by asking: there "
    "are only those listed, at their thresholds; say so. Not for any reading or figure: current (now), summary (a period), days, "
    "readings. Result: measures (each device in words, and its readings: each name with its label and unit); not_available and "
    "not_note; history (each sensor's text, ready to pass on, and each resolution held: from, to, days); alerts (each with what it is, "
    "its rule in words and its thresholds as figures) and alerts_note; tools (what each answers); lines (a short list of what is "
    "measured, to pass on as it is).")
PARAMETERS = {"type": "object", "properties": {}}


# ---------- what is held ----------
def _minutes(r: Reader, sensors: list) -> tuple[int, int] | None:
    """(the first, the last) minute held of these sensors (epochs), None for none: one query per kind."""
    found = []
    for kind in dict.fromkeys(s.kind for s in sensors):
        ids = [s.id for s in sensors if s.kind == kind]
        found.append(r.db.execute(f"SELECT MIN(ts), MAX(ts) FROM {kind} WHERE sensor IN ({', '.join('?' * len(ids))}) AND res = ?",
                                  (*ids, MINUTE)).fetchone())
    firsts, lasts = [a for a, _ in found if a is not None], [b for _, b in found if b is not None]
    return (min(firsts), max(lasts)) if firsts else None


def held_on(r: Reader, sensors: list, resolutions: tuple) -> dict[str, tuple[int, int, int]]:
    """{resolution: (first, last, whole days held)} of these sensors (epochs; merged over them), for the resolutions held."""
    out = {}
    for res, _ in resolutions:
        if res == MINUTE:
            if span := _minutes(r, sensors):
                out[res] = (*span, round((span[1] - span[0]) / 86400))
            continue
        spans = merge([span for s in sensors for span in r.storage.coverage.of(s.id, res)])
        if spans:
            out[res] = (spans[0][0], spans[-1][1], sum(e - s + 1 for s, e in spans) // 86400)
    return out


def history_of(held: dict[str, tuple[int, int, int]], resolutions: tuple, device: str, tz: tzinfo) -> dict:
    """{text, held: {resolution: {from, to, days}}} of one sensor: its text ready to pass on ("Weather station: 5-minute readings from
    Mon 12 May 2025, ..."), and each resolution's first and last day held (ISO dates) and its days held."""
    if not held:
        return {"text": f"{'Weather station' if device == STATION else 'Air sensor'}: nothing held yet.", "held": {}}
    parts, out = [], {}
    for res, what in resolutions:
        if res not in held:
            continue
        first, last, days = held[res]
        a, b = local_date(first, tz), local_date(last, tz)
        out[res] = {"from": a.isoformat(), "to": b.isoformat(), "days": days}
        if res == MINUTE:
            parts.append(f"{what} from {day_text(a)} (kept {MINUTES_KEPT // 86400} days)")
        elif res == "1day":
            parts.append(f"{what} from {day_text(a)}, each {daily_run(tz, a, b)}")
        else:
            parts.append(f"{what} from {day_text(a)}")
    name = "Weather station" if device == STATION else "Air sensor"
    return {"text": f"{name}: {', '.join(parts)}. The tools use the finest held for each part of a period.", "held": out}


# ---------- the alerts ----------
def _clock(hour: int, minute: int = 0) -> str:
    """"12am", "7:30am": a time of day as the service writes one."""
    return clock_text(datetime(2000, 1, 1, hour % 24, minute), whole_hours=True)


def alert_rules() -> dict[str, tuple[str, dict]]:
    """{kind: (its rule in words, its thresholds as figures)}, from the rules' constants and the settings."""
    cooldown, quiet = ALERT_COOLDOWN_MINUTES, RAIN_QUIET_HOURS
    rain = (f"Started: the first tip of the gauge, told at once. Stopped: dry for {cooldown} minutes, with how much fell."
            + (f" None from {_clock(quiet[0])} to {_clock(quiet[1])}: one summary of the rain in those hours after them." if quiet else ""))
    return {
        "rain": (rain, {"dry_minutes": cooldown, "quiet_hours": f"{quiet[0]:02d}:00-{quiet[1]:02d}:00" if quiet else None}),
        "gusts": (f"A gust over {GUST_ALERT_KMH} km/h; then none until the gusts have stayed at or under it for "
                  f"{GUST_REARM_SECONDS // 60} minutes.", {"gust_kmh": GUST_ALERT_KMH, "rearm_minutes": GUST_REARM_SECONDS // 60}),
        "uv": (f"A UV index of {UVI_ALERT} or more in {CONFIRM_READINGS} readings in a row; then none until it has stayed below {UVI_ALERT} "
               f"for {UV_REARM_SECONDS // 60} minutes.",
               {"uv_index": UVI_ALERT, "readings_in_a_row": CONFIRM_READINGS, "rearm_minutes": UV_REARM_SECONDS // 60}),
        "temps": (f"Outdoor becoming warmer than indoor, or cooler, by more than {CROSS_MARGIN:g}°C; at most one every {cooldown} minutes.",
                  {"margin_c": CROSS_MARGIN, "cooldown_minutes": cooldown}),
        "particulates": (f"Wear a mask: PM2.5 at {MASK_PM25:g} µg/m³ or more, or PM10 at {MASK_PM10:g} µg/m³ or more (US AQI 151 and up: "
                         f"unhealthy), in {CONFIRM_READINGS} readings in a row. Safe again: PM2.5 at {SAFE_PM25:g} or less and PM10 at "
                         f"{SAFE_PM10:g} or less (AQI 100 or better), in {CONFIRM_READINGS} readings in a row. Checked every "
                         f"{CHECK_SECONDS // 60} minutes.",
                         {"mask_pm2_5": MASK_PM25, "mask_pm10": MASK_PM10, "safe_pm2_5": SAFE_PM25, "safe_pm10": SAFE_PM10,
                          "readings_in_a_row": CONFIRM_READINGS, "check_minutes": CHECK_SECONDS // 60}),
        "pollen_and_asthma": (f"Grass pollen or the thunderstorm asthma risk forecast {' or '.join(LEVELS[WARN_FROM:])}: once a day from "
                              f"{_clock(*ALERT_FROM)} while it stays there, and again when it rises. October to December only.",
                              {"from_level": LEVELS[WARN_FROM]}),
        "forecast": (f"Today's or tomorrow's forecast revised from the first one fetched after {_clock(BASELINE_HOUR)} that day: its highest "
                     f"temperature by more than {TEMP_CHANGE_C:g}°C, or the chance of at least 1 mm of rain rising from under {RAIN_FROM_PCT}% "
                     f"to {RAIN_FROM_PCT}% or more (rain likely), or falling from {RAIN_FROM_PCT}% or more to {DRY_BELOW_PCT}% or less (rain "
                     "unlikely). The forecast alerted on is the one later forecasts are compared with.",
                     {"max_change_c": TEMP_CHANGE_C, "rain_likely_from_pct": RAIN_FROM_PCT, "rain_unlikely_at_pct": DRY_BELOW_PCT,
                      "baseline_from": f"{BASELINE_HOUR:02d}:00"}),
    }


# ---------- the tool ----------
class AboutTool:
    """The about tool over a Context."""

    def __init__(self, ctx: Context):
        self.ctx = ctx

    @property
    def sources(self) -> list[str]:
        """The sources there are, by the names the bot's lists use."""
        c = self.ctx
        return [name for name, there in (("Ecowitt", c.eco), ("AirGradient", c.air), ("Pollen", c.pollen), ("Forecast", c.forecast)) if there]

    def tool(self) -> ServiceTool:
        return ServiceTool(NAME, DESCRIPTION, PARAMETERS, self.handle)

    async def handle(self, args: dict, meta: dict) -> str:
        if args:
            raise Refusal(f"about takes no arguments, not {', '.join(map(repr, args))}: call it with {{}}.", arguments=[])
        sources = self.sources
        return answer(period=None, as_of=None, measures=self.measures(), not_available=not_available(sources), not_note=NOT_NOTE,
                      history=await self.history(), alerts=alerts(sources), alerts_note=ALERTS_NOTE,
                      tools={name: what for name, what, needs in TOOLS if any(_has(sources, n) for n in needs)},
                      lines=measure_lines(sources))

    def measures(self) -> dict:
        """Each device and service there is, in words; the sensors with their readings (name, label, unit)."""
        catalogue = self.ctx.readings()
        out = {}
        for device, there, text in ((STATION, self.ctx.eco, STATION_TEXT), (AIR_DEVICE, self.ctx.air, AIR_TEXT)):
            if there:
                out[device] = {"text": text, "readings": {r.name: f"{r.label} ({r.unit})" if r.unit else r.label
                                                          for r in catalogue if r.device == device}}
        if self.ctx.forecast:
            out["forecast"] = {"text": FORECAST_TEXT}
        if self.ctx.pollen:
            out["pollen"] = {"text": POLLEN_TEXT}
        return out

    async def history(self) -> dict:
        """Each sensor's history: from when to when each resolution is held (history_of)."""
        storage, tz, out = self.ctx.storage, self.ctx.zone, {}
        if storage is None:
            return out
        if self.ctx.eco:
            sensors = [s for s in storage.station(self.ctx.eco.mac).values() if s.kind in KINDS]
            out[STATION] = history_of(await storage.read(held_on, sensors, STATION_HELD), STATION_HELD, STATION, tz)
        if self.ctx.air:
            sensor = self.ctx.air.store.sensor
            held = await storage.read(held_on, [sensor], AIR_HELD) if sensor is not None else {}
            out[AIR_DEVICE] = history_of(held, AIR_HELD, AIR_DEVICE, tz)
        return out


def _has(sources: list[str], need: str) -> bool:
    return all(n in sources for n in need.split("+"))


def not_available(sources: list[str]) -> list[str]:
    """What the service can't give: never (NEVER_AVAILABLE), and what a source that is off leaves out (WITHOUT)."""
    return [*NEVER_AVAILABLE, *(what for name, what in WITHOUT.items() if name not in sources),
            *(["weather station readings"] if "Ecowitt" not in sources else []), *(["air quality"] if "AirGradient" not in sources else [])]


def alerts(sources: list[str]) -> list[dict]:
    """The alerts there are for these sources, each {kind, name, what, rule, thresholds}."""
    rules = alert_rules()
    return [{"kind": kind, "name": name, "what": ALERT_WORDS[kind], "rule": rules[kind][0], "thresholds": rules[kind][1]}
            for kind, (name, source) in TYPES.items() if source in sources]


def measure_lines(sources: list[str]) -> list[str]:
    """What is measured, as the bot's Help lists it (lib/templates.py capabilities_text): a short bullet each."""
    return [f"• {line}" for line in [*(line for name, measured in MEASURES.items() if name in sources for line in measured),
                                           "History charts"]]


def tools(ctx: Context) -> list[ServiceTool]:
    """The about tool: always there."""
    return [AboutTool(ctx).tool()]


__all__ = ["ALERT_COOLDOWN_MINUTES", "ALERT_WORDS", "AboutTool", "DESCRIPTION", "MEASURES", "NEVER_AVAILABLE", "RAIN_QUIET_HOURS",
           "TYPES", "WITHOUT", "alert_rules", "alerts", "held_on", "history_of", "measure_lines", "not_available", "tools"]
