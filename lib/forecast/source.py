"""The forecast source: the daily forecast from Open-Meteo (open-meteo.com), kept warm and cached; fetched only from 6 am to 6 pm
local time (lib/warm.py SYNC_HOURS), every 15 minutes, and once at the start (or the first question) when nothing is cached."""

import json
import logging
import math
import re
import time
from datetime import date, datetime, timedelta

import httpx

from .. import intent
from ..config import Config
from ..snapshots import Snapshots
from ..timeutil import now_local
from ..tools import Tool, Turn
from ..warm import Warmer, in_sync_hours

log = logging.getLogger(__name__)

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
HEADERS = {"User-Agent": "ecobot/1.0 (personal weather bot)"}
FORECAST_REFRESH_SECONDS = 15 * 60
FRESH_SECONDS = 10 * 60       # a question reuses a forecast this young

DESCRIPTION = ("The weather forecast for the owner's location: today and the days ahead, "
               "each with a summary, the lowest and highest temperature and the chance of rain. Use it for forecast "
               "questions ('what's tomorrow like', 'will it rain this week'). days = how many days from today (default 2, "
               "at most 7). The result's \"lines\" are ready-made, emoji included: copy them as they are, and put the result's "
               "\"place\" in parentheses after the forecast heading, e.g. (Melbourne).")
PARAMETERS = {"type": "object", "properties": {"days": {"type": "integer", "description": "Days from today (default 2, at most 7)."}}}

# The first match wins; each sentence of a summary gets its own emoji ("Showers. Possible storm." -> "🌦️ Showers. ⛈️ Possible storm.")
EMOJI = ((r"thunder|storm", "⛈️"), (r"shower", "🌦️"), (r"rain|drizzle", "🌧️"), (r"snow|hail", "🌨️"), (r"fog|mist", "🌫️"),
         (r"partly cloudy", "⛅"), (r"mostly (sunny|clear)", "🌤️"), (r"cloud|overcast", "☁️"), (r"sunny|clear", "☀️"),
         (r"wind", "🌬️"), (r"frost", "🥶"), (r"hot", "🥵"))
WMO = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Fog", 51: "Light drizzle", 53: "Drizzle",
       55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain", 71: "Snow", 73: "Snow", 75: "Heavy snow",
       80: "Showers", 81: "Showers", 82: "Heavy showers", 95: "Thunderstorm", 96: "Thunderstorm and hail", 99: "Thunderstorm and hail"}


def forecast_emoji(sentence: str) -> str:
    return next((emoji for pattern, emoji in EMOJI if re.search(pattern, sentence, re.I)), "")


def decorate(summary: str) -> str:
    """The summary with an emoji before each sentence that has one, ending in a full stop: "Showers. ⛈️ Possible storm."."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", (summary or "").strip()) if s.strip(". ")]
    return ". ".join(f"{e} {s}" if (e := forecast_emoji(s)) else s for s in (s.rstrip(".") for s in sentences)) + ("." if sentences else "")


def whole(x: float) -> int:
    """The nearest whole degree, halves up (10.5 -> 11, -0.5 -> 0), unlike round()'s halves-to-even."""
    return math.floor(x + 0.5)


def temps(d: dict) -> str | None:
    lo, hi = d.get("min_c"), d.get("max_c")
    if lo is not None and hi is not None:
        return f"{whole(lo)}–{whole(hi)}°C"
    if hi is not None:
        return f"max {whole(hi)}°C"
    return f"min {whole(lo)}°C" if lo is not None else None


def describe_day(d: dict) -> str:
    """"🌧️ Rain. 11–17°C, 90% chance of rain" """
    details = [temps(d)] + ([f"{d['rain_chance_pct']}% chance of rain"] if d.get("rain_chance_pct") is not None else [])
    rest = ", ".join(x for x in details if x)
    return " ".join(x for x in (decorate(d.get("summary") or ""), rest) if x) or "no data"


def day_label(day: date, today: date) -> str:
    """"Today", then each later day by its weekday ("Friday"): within the week it can't be mistaken for another day."""
    return "Today" if day == today else f"{day:%A}"


class Forecast:
    name = "Forecast"

    def __init__(self, cfg: Config, location: tuple[float, float] | None = None, transport=None):
        """location: (lat, lon), used when the config has none (the weather station's own)."""
        self.tz, self.place = cfg.tz, cfg.place
        lat = cfg.forecast_lat if cfg.forecast_lat is not None else (location[0] if location else None)
        lon = cfg.forecast_lon if cfg.forecast_lon is not None else (location[1] if location else None)
        self.location = (lat, lon) if lat is not None and lon is not None else None
        self.client = httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True, transport=transport)
        self.days: list[dict] | None = None
        self.source = ""
        self.fetched_at = 0.0
        self.requests = 0
        self.store = Snapshots.open(cfg.conditions_cache_path)
        self._load()
        self.warmer = Warmer(self.warm, FORECAST_REFRESH_SECONDS)
        self.tools = [Tool("weather_forecast", DESCRIPTION, PARAMETERS, self.handle)]

    async def start(self):
        if not self.location:
            raise RuntimeError("no location: set FORECAST_LAT and FORECAST_LON (or the weather station supplies one)")
        try:
            await self.warm(True)
            log.info("Forecast: %s, %d days", self.source, len(self.days or []))
        except Exception as e:
            log.warning("Forecast not readable yet: %s", e)

    def describe(self) -> str:
        return "Weather forecast for the owner's location (Open-Meteo) (people say \"forecast\")"

    def wants(self, text: str) -> bool:
        """Only forecast questions start a refresh; the report always uses what is cached."""
        return bool(re.search(r"\bforecast\w*|\btomorrow\b|\bthis week\b", text, re.I))

    def poke(self):
        self.warmer.poke()

    async def close(self):
        await self.client.aclose()
        self.store.close()

    # ---------- kept on disk ----------
    def _load(self):
        """Start from the last forecast fetched (before the restart), so a night-time start needs no fetch."""
        if last := self.store.latest("forecast"):
            self.fetched_at, saved = last
            self.source = saved.get("source", "Open-Meteo")
            self.days = [{**d, "date": date.fromisoformat(d["date"])} for d in saved["days"]]

    def _save(self):
        self.store.save("forecast", {"source": self.source,
                                     "days": [{**d, "date": d["date"].isoformat()} for d in self.days]},
                        now=self.fetched_at)

    def now(self) -> datetime:
        return now_local(self.tz)

    # ---------- fetching ----------
    async def _json(self, url: str, **params) -> dict:
        self.requests += 1
        res = await self.client.get(url, params=params)
        res.raise_for_status()
        return res.json()

    async def _open_meteo(self) -> tuple[str, list[dict]]:
        lat, lon = self.location
        d = await self._json(OPEN_METEO, latitude=lat, longitude=lon, timezone=str(self.tz),
                             daily="temperature_2m_min,temperature_2m_max,precipitation_probability_max,weather_code")
        dd = d["daily"]
        return "Open-Meteo", [
            {"date": date.fromisoformat(dd["time"][i]), "min_c": dd["temperature_2m_min"][i], "max_c": dd["temperature_2m_max"][i],
             "summary": WMO.get(dd["weather_code"][i], ""), "rain_chance_pct": dd["precipitation_probability_max"][i]}
            for i in range(len(dd["time"]))]

    async def warm(self, fresh: bool = True) -> str:
        """Fetch when due: never outside the sync hours (unless nothing is cached), and not while the forecast is young."""
        age = time.time() - self.fetched_at
        if self.days is not None and (not in_sync_hours(self.now()) or age < FRESH_SECONDS and not fresh):
            return "Forecast cached"
        before = self.requests
        self.source, self.days = await self._open_meteo()   # a failure raises: the warmer logs it
        self.fetched_at = time.time()
        self._save()
        return f"Forecast {self.requests - before} req"

    # ---------- reading ----------
    def lines(self, count: int = 2) -> list[str]:
        today = self.now().date()
        wanted = [d for d in (self.days or []) if d["date"] >= today][:count]
        return [f"{day_label(d['date'], today)}: {describe_day(d)}" for d in wanted]

    async def handle(self, args: dict, turn: Turn | None = None) -> str:
        if self.days is None and not args.get("cached"):   # the report ("cached") never fetches
            try:
                await self.warm(True)
            except Exception as e:
                log.warning("Forecast not readable: %s", e)
        try:
            count = max(1, min(7, int(args.get("days") or 2)))
        except (TypeError, ValueError):
            count = 2
        lines = self.lines(count)
        if not lines:
            return json.dumps({"error": "No forecast is available right now."})
        return json.dumps({"lines": lines, "place": self.place, "source": self.source}, ensure_ascii=False)
