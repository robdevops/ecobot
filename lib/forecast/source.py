"""The forecast source: the daily forecast from Open-Meteo (open-meteo.com), kept warm and cached; fetched only from 6 am to 6 pm
local time (lib/warm.py SYNC_HOURS), every 15 minutes, and once just after the start (ecobot-mcp's warm-up, or the first question) when
nothing is cached."""

import logging
import math
import time
from collections.abc import Callable
from datetime import date

import httpx

from ..config import Config
from ..snapshots import Snapshots
from ..sources import Source
from ..tls import ssl_context
from ..warm import Warmer, in_sync_hours

log = logging.getLogger(__name__)

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
ENSEMBLE_MODEL = "ecmwf_ifs025"   # 51 members
RAIN_MM = 1.0                     # "chance of at least 1 mm", as the Bureau of Meteorology says it
DRIZZLE_MM = 0.2                  # a day with less than this has no amount in its line (dry)
HEAVY_MM = 30                     # a day of at least this (rounded) is called heavy
SPREAD = (0.1, 0.9)               # the range given is these quantiles of the ensemble members' day totals
FLOOR_LADDER = (2, 3, 5, 10, 15, 20, 25, 30, 40, 50, 75, 100)   # a high-confidence floor is stated as one of these mm, rounded down
MIN_HOURS = 20                    # a member's day counts only with this many hours of data
HEADERS = {"User-Agent": "ecobot/1.0 (personal weather service)"}
FORECAST_REFRESH_SECONDS = 15 * 60
FRESH_SECONDS = 10 * 60       # a question reuses a forecast this young

SOURCE = "Open-Meteo"
WMO = {0: "☀️ Clear.", 1: "🌤️ Mostly clear.", 2: "⛅ Partly cloudy.", 3: "☁️ Overcast.", 45: "🌫️ Fog.", 48: "🌫️ Fog.", 51: "🌧️ Light drizzle.",
       53: "🌧️ Drizzle.", 55: "🌧️ Heavy drizzle.", 61: "🌧️ Light rain.", 63: "🌧️ Rain.", 65: "🌧️ Heavy rain.", 71: "🌨️ Snow.", 73: "🌨️ Snow.",
       75: "🌨️ Heavy snow.", 80: "🌦️ Showers.", 81: "🌦️ Showers.", 82: "🌦️ Heavy showers.", 95: "⛈️ Thunderstorm.",
       96: "⛈️ Thunderstorm and hail.", 99: "⛈️ Thunderstorm and hail."}   # weather code -> the day's summary, emoji included


def whole(x: float) -> int:
    """The nearest whole degree, halves up (10.5 -> 11, -0.5 -> 0), unlike round()'s halves-to-even."""
    return math.floor(x + 0.5)


def quantile(values: list[float], q: float) -> float:
    """The q quantile of the values, interpolated between neighbours."""
    ordered = sorted(values)
    at = q * (len(ordered) - 1)
    low = math.floor(at)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (at - low)


def temps(d: dict) -> str | None:
    lo, hi = d.get("min_c"), d.get("max_c")
    if lo is not None and hi is not None:
        return f"{whole(lo)}–{whole(hi)}°C"
    if hi is not None:
        return f"max {whole(hi)}°C"
    return f"min {whole(lo)}°C" if lo is not None else None


def rounded_mm(mm: float) -> float:
    """A day's rain to a sensible step, halves up: under 5 mm the nearest 0.5, under 20 the nearest 1, from 20 the nearest 5."""
    step = 0.5 if mm < 5 else 1 if mm < 20 else 5
    return math.floor(mm / step + 0.5) * step


def mm_number(mm: float) -> str:
    return f"{rounded_mm(mm):g}"


def floor_mm(d: dict) -> int | None:
    """The day's high-confidence floor in mm: the largest FLOOR_LADDER step not above the ensemble's SPREAD[0] quantile of the day's
    total (that share of the runs, 1 - SPREAD[0], is at or above it). None when the day has no spread or no ensemble chance, when
    the quantile is under the ladder's first step (a showery or drizzle day), or when the step reaches the total's rounded value."""
    lo, hi, mm = d.get("rain_mm_lo"), d.get("rain_mm_hi"), d.get("rain_mm")
    if lo is None or hi is None or mm is None or d.get("rain_1mm_pct") is None:
        return None
    step = max((x for x in FLOOR_LADDER if x <= lo), default=None)
    return step if step is not None and step < rounded_mm(mm) else None


def amount_text(d: dict) -> str | None:
    """The day's volume with how sure it is, "<amount> (<range> likely; ≥<N> at <P>%)": "under 1 mm", "~3 mm (1–3.5 likely; ≥1 at 80%)",
    "~12 mm (6–20 likely; ≥5 at 90%)", "heavy: ~40 mm (15–60 likely; ≥15 at 90%)"; None for a dry day (under DRIZZLE_MM, or no chance at all
    of 1 mm) and for a day with no total. The range is the ensemble members' spread (SPREAD), given when it is wider than the total's own
    rounding; the last item is the floor_mm step at 1 - SPREAD[0] when the day has one, else 1 mm at the chance of it. A part with no
    figure is left out, and with nothing to put in the brackets there are none."""
    mm = d.get("rain_mm")
    if mm is None or mm < DRIZZLE_MM:
        return None
    if mm < 1:
        return None if d.get("rain_1mm_pct") == 0 else "under 1 mm"
    text = f"~{mm_number(mm)} mm"
    if rounded_mm(mm) >= HEAVY_MM:
        text = f"heavy: {text}"
    items = []
    lo, hi = d.get("rain_mm_lo"), d.get("rain_mm_hi")
    if lo is not None and hi is not None:
        lo, hi = min(lo, mm), max(hi, mm)
        if rounded_mm(lo) != rounded_mm(hi) and hi >= 1:
            items.append(f"{mm_number(lo) if lo >= 1 else 0}–{mm_number(hi)} likely")
    if (floor := floor_mm(d)) is not None:
        items.append(f"≥{floor} at {whole(100 * (1 - SPREAD[0]))}%")
    elif d.get("rain_1mm_pct") is not None:
        items.append(f"≥1 at {d['rain_1mm_pct']}%")
    return f"{text} ({'; '.join(items)})" if items else text


def describe_day(d: dict) -> str:
    """"🌧️ Rain. 11–17°C, 90% chance of rain"; with the day's total: "🌦️ Showers. 19–23°C, ~3 mm (1–3.5 likely; ≥1 at 80%)"; a wet day
    with a floor: "🌦️ Showers. 19–23°C, heavy: ~40 mm (15–60 likely; ≥15 at 90%)" """
    if d.get("rain_1mm_pct") is not None:
        chance = f"{d['rain_1mm_pct']}% chance of at least 1 mm"
    else:   # a day saved before the ensemble figure, or the ensemble was unavailable: Open-Meteo's own (more than 0.1 mm in the wettest hour)
        chance = f"{d['rain_chance_pct']}% chance of rain" if d.get("rain_chance_pct") is not None else None
    if amount := amount_text(d):
        chance = amount if "(" in amount or chance is None else f"{amount} ({chance})"
    details = [temps(d)] + ([chance] if chance else [])
    rest = ", ".join(x for x in details if x)
    return " ".join(x for x in (d.get("summary") or "", rest) if x) or "no data"


def day_label(day: date, today: date) -> str:
    """"Today", then each later day by its weekday ("Friday"): within the week it can't be mistaken for another day."""
    return "Today" if day == today else f"{day:%A}"


class Forecast(Source):
    name = "Forecast"

    def __init__(self, cfg: Config, location: tuple[float, float] | Callable[[], tuple[float, float] | None] | None = None, transport=None):
        """location: (lat, lon), used when the config has none (the weather station's own); or what gives it when asked (the station's,
        which a first start learns only once its device list answers)."""
        self.tz, self.place = cfg.tz, cfg.place
        configured = (cfg.forecast_lat, cfg.forecast_lon)
        if configured != (None, None) and None in configured:
            log.warning("MCP_FORECAST_LAT and MCP_FORECAST_LON go together: with only one set, the weather station's location is used")
        self._location = configured if None not in configured else location
        self.client = httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True, transport=transport, verify=ssl_context())
        self.days: list[dict] | None = None
        self.fetched_at = 0.0
        self.requests = 0
        self.store = Snapshots(cfg.storage_path)
        self._load()
        self.warmer = Warmer(self.warm, FORECAST_REFRESH_SECONDS)

    @property
    def location(self) -> tuple[float, float] | None:
        """(lat, lon): the configured one, else the weather station's (None while it has none)."""
        found = self._location() if callable(self._location) else self._location
        return tuple(found) if found and None not in found else None

    async def start(self, wait: bool = True):
        """From the last forecast fetched (kept in the file), nothing asked: the first fetch, if that one is due again, is the warm-up's
        (ecobot_mcp/core.py Core.warm_up). Without a location, nor a station that may yet give one, it can't start."""
        if not self.location and not callable(self._location):
            raise RuntimeError("no location: set MCP_FORECAST_LAT and MCP_FORECAST_LON (or the weather station supplies one)")
        self.summary = self.describe()

    def describe(self) -> str:
        """The start-up line: how many days it holds, and how often it is fetched."""
        held = f"{len(self.days)} days" if self.days is not None else "none kept yet"
        return f"{SOURCE}: {held} (every {self.warmer.interval:g}s)"

    # ---------- kept on disk ----------
    def _load(self):
        """Start from the last forecast fetched (before the restart), so a night-time start needs no fetch."""
        if last := self.store.latest("forecast"):
            self.fetched_at, saved = last   # (a row saved by an older version names its source too, and has its summaries without the emoji)
            self.days = [{**d, "date": date.fromisoformat(d["date"])} for d in saved["days"]]

    def _save(self):
        self.store.save("forecast", {"days": [{**d, "date": d["date"].isoformat()} for d in self.days]}, now=self.fetched_at)

    # ---------- fetching ----------
    async def _json(self, url: str, **params) -> dict:
        self.requests += 1
        res = await self.client.get(url, params=params)
        res.raise_for_status()
        return res.json()

    async def _open_meteo(self) -> list[dict]:
        lat, lon = self.location
        d = await self._json(OPEN_METEO, latitude=lat, longitude=lon, timezone=str(self.tz),
                             daily="temperature_2m_min,temperature_2m_max,precipitation_probability_max,precipitation_sum,weather_code")
        dd = d["daily"]
        sums = dd.get("precipitation_sum") or [None] * len(dd["time"])
        return [
            {"date": date.fromisoformat(dd["time"][i]), "min_c": dd["temperature_2m_min"][i], "max_c": dd["temperature_2m_max"][i],
             "summary": WMO.get(dd["weather_code"][i], ""), "rain_chance_pct": dd["precipitation_probability_max"][i],
             "rain_mm": sums[i]}
            for i in range(len(dd["time"]))]

    async def _ensemble(self, days: int) -> dict[date, dict]:
        """{local day: {"pct": % of ensemble members with at least RAIN_MM that day, to the nearest 5, "lo", "hi": the SPREAD quantiles of
        the members' totals in mm}}; a day with no usable members is left out.
        Every `hourly` series named precipitation* is one member (the response names them by member and model)."""
        lat, lon = self.location
        d = await self._json(ENSEMBLE, latitude=lat, longitude=lon, timezone=str(self.tz), hourly="precipitation",
                             models=ENSEMBLE_MODEL, forecast_days=min(days, 7))
        hourly = d.get("hourly") or {}
        days_of = [t[:10] for t in hourly.get("time", [])]
        sums: dict[str, list[float]] = {}   # day -> each usable member's total
        for key, series in hourly.items():
            if not key.startswith("precipitation") or len(series) != len(days_of):
                continue
            totals: dict[str, list[float]] = {}
            for day, mm in zip(days_of, series):
                if mm is not None:
                    totals.setdefault(day, []).append(mm)
            for day, got in totals.items():
                if len(got) >= MIN_HOURS:
                    sums.setdefault(day, []).append(sum(got))
        return {date.fromisoformat(day): {"pct": 5 * round(100 * sum(t >= RAIN_MM for t in totals) / len(totals) / 5),
                                          "lo": round(quantile(totals, SPREAD[0]), 1), "hi": round(quantile(totals, SPREAD[1]), 1)}
                for day, totals in sums.items()}

    async def _with_chance(self, days: list[dict]) -> list[dict]:
        """The days with the ensemble's chance of at least 1 mm added; Open-Meteo's own figure stays where the ensemble has none."""
        try:
            chance = await self._ensemble(len(days)) if days else {}
        except Exception as e:   # the forecast still works without it
            log.warning("Ensemble rain chance unavailable (%s: %s); using Open-Meteo's own", type(e).__name__, e)
            return days
        return [{**d, "rain_1mm_pct": chance[d["date"]]["pct"], "rain_mm_lo": chance[d["date"]]["lo"], "rain_mm_hi": chance[d["date"]]["hi"]}
                if d["date"] in chance else d for d in days]

    async def warm(self, fresh: bool = True) -> str:
        """Fetch when due: never outside the sync hours (unless nothing is cached), and not while the forecast is young."""
        age = time.time() - self.fetched_at
        if self.days is not None and (not in_sync_hours(self.now()) or age < FRESH_SECONDS and not fresh):
            return "Forecast cached"
        before = self.requests
        if not self.location:   # (the station's, not given yet: a first start whose device list hasn't answered)
            raise RuntimeError("no location yet: the weather station hasn't given one (or set MCP_FORECAST_LAT and MCP_FORECAST_LON)")
        self.days = await self._open_meteo()   # a failure raises: the warmer logs it
        self.days = await self._with_chance(self.days)
        self.fetched_at = time.time()
        self._save()
        return f"Forecast {self.requests - before} req"

    # ---------- reading ----------
    def upcoming(self, count: int = 2) -> list[dict]:
        """Today and the next days, up to `count`."""
        today = self.now().date()
        return [d for d in (self.days or []) if d["date"] >= today][:count]

    def lines(self, count: int = 2) -> list[str]:
        today = self.now().date()
        return [f"{day_label(d['date'], today)}: {describe_day(d)}" for d in self.upcoming(count)]
