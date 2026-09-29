"""Air quality from your own AirGradient sensor, as a data source (peer of Ecowitt):
start() checks the sensor, tools are what the model can call, warm()/poke() keep recent
readings ready, current() feeds the air alert monitor.

AirGradient's documented API:
  current: GET /public/api/v1/locations/{id}/measures/current?token=...
  history: GET /public/api/v1/locations/{id}/measures/past?from=...&to=...&token=...

Reports AirGradient's corrected (calibrated) values where available, plus a US EPA AQI
and band for PM2.5.
"""

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from . import intent
from .charts import CHART_HINT, CHART_REQUESTS
from .config import Config
from .tools import Tool
from .warm import Warmer

log = logging.getLogger(__name__)

API = "https://api.airgradient.com/public/api/v1/locations/{loc}/measures/{kind}"
MAX_DAYS = 14                     # longest history period per question
FRESH_SECONDS = 300               # the current reading and today's history are reused this long
WARM_DAYS = 7                     # finished days fetched ahead of questions
KEEP_DAYS = MAX_DAYS + 2          # finished days kept in memory

# name -> (fields to try, in order: corrected first), unit
METRICS = {
    "pm2_5": (("pm02_corrected", "pm02"), "\u00b5g/m\u00b3"),
    "pm10": (("pm10_corrected", "pm10"), "\u00b5g/m\u00b3"),
    "pm1": (("pm01_corrected", "pm01"), "\u00b5g/m\u00b3"),
    "co2": (("rco2_corrected", "rco2"), "ppm"),
    "voc_index": (("tvocIndex", "tvoc_index"), "relative index (100 = this sensor's recent average)"),
    "nox_index": (("noxIndex", "nox_index"), "relative index (1 = baseline)"),
}

# Traffic-light ratings: value <= first -> good, <= second -> poor, above -> very poor.
# Particles follow the US AQI (very poor = AQI 151+, the same level as the mask alerts);
# PM1 has no standard, so it uses PM2.5's; CO2, VOC and NOx follow AirGradient's colour scales.
RATINGS = {
    "pm2_5": (9.0, 55.4),
    "pm10": (54.0, 254.0),
    "pm1": (9.0, 55.4),
    "co2": (799.0, 1499.0),
    "voc_index": (150.0, 250.0),
    "nox_index": (20.0, 150.0),
}


def rating(name: str, value: float) -> str | None:
    limits = RATINGS.get(name)
    if limits is None:
        return None
    good, poor = limits
    return "\U0001f7e2 good" if value <= good else "\U0001f7e1 poor" if value <= poor else "\U0001f534 very poor"


# US EPA 2024 PM2.5 breakpoints: (conc low, conc high, AQI low, AQI high, band)
PM25_AQI = [
    (0.0, 9.0, 0, 50, "good"),
    (9.1, 35.4, 51, 100, "moderate"),
    (35.5, 55.4, 101, 150, "unhealthy for sensitive groups"),
    (55.5, 125.4, 151, 200, "unhealthy"),
    (125.5, 225.4, 201, 300, "very unhealthy"),
    (225.5, 325.4, 301, 500, "hazardous"),
]

LABELS = {"pm2_5": "PM2.5", "pm10": "PM10", "pm1": "PM1", "co2": "CO\u2082",
          "voc_index": "VOC index", "nox_index": "NOx index"}
CHART_UNITS = {"pm2_5": "\u00b5g/m\u00b3", "pm10": "\u00b5g/m\u00b3", "pm1": "\u00b5g/m\u00b3", "co2": "ppm",
               "voc_index": "", "nox_index": ""}
ALL_METRICS = list(LABELS)

PARAMETERS = {
    "type": "object",
    "properties": {
        "chart": {"type": "boolean", "description": "Set true to send a chart (graph) with the answer. Without dates, "
                                                    "it covers the last 24 hours."},
        "metrics": {"type": "array", "items": {"type": "string", "enum": list(LABELS)},
                    "description": "Which metrics to chart (several are drawn as panels in one image; "
                                   "for 'all', list all six). Default: pm2_5."},
        "start_date": {"type": "string", "description": "Start of a past period, 'YYYY-MM-DD HH:MM:SS' local time. "
                                                        "Omit (with end_date) for the current reading."},
        "end_date": {"type": "string", "description": "End of the past period, 'YYYY-MM-DD HH:MM:SS' local time."},
    },
}
DESCRIPTION = ("Air quality from the owner's AirGradient outdoor sensor: PM2.5 (with US AQI and band), PM10, "
               "PM1, CO2, VOC and NOx indexes, each with a traffic-light rating. No dates: the current reading. With start_date/end_date (up to "
               "14 days): lowest, highest and average of each, with when they happened. chart=true sends a graph.")


def _value(row: dict, fields: tuple) -> float | None:
    for f in fields:
        v = row.get(f)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def pm25_aqi(conc: float) -> tuple[int, str]:
    c = round(conc, 1)
    for lo, hi, a_lo, a_hi, band in PM25_AQI:
        if c <= hi:
            return round(a_lo + (a_hi - a_lo) * (max(c, lo) - lo) / (hi - lo)), band
    return 500, "hazardous"


class AirGradient:
    """AirGradient client with its data kept warm:
      - the current reading and today's history are refreshed every few minutes (warm);
      - finished days are fetched once and kept in memory (they no longer change)."""

    name = "AirGradient"

    def __init__(self, cfg: Config, transport=None):
        self.loc, self.tz = cfg.airgradient_location, cfg.tz
        self.token = cfg.airgradient_token
        self.link = ("live chart", cfg.airgradient_dashboard) if cfg.airgradient_dashboard else None
        self.client = httpx.AsyncClient(timeout=15, transport=transport)
        self._current: tuple[dict, float] | None = None       # (row, fetched at)
        self._days: dict[date, list[dict]] = {}                # finished days
        self._recent: dict[date, tuple[list[dict], float]] = {}  # today / just-finished days, refreshed
        self._locks: dict = {}
        self.requests = 0
        self.warmer = Warmer(self.warm)
        self.tools = [Tool("air_quality", DESCRIPTION, PARAMETERS, self.handle)]

    async def start(self):
        try:
            row = await self._current_row(refresh=True)
            log.info("Air quality: AirGradient '%s' (location %s)", row.get("locationName"), self.loc)
        except Exception as e:  # keep going: the sensor may just be briefly unreachable
            log.warning("AirGradient location %s not readable yet: %s", self.loc, e)

    def describe(self) -> str:
        return f"AirGradient outdoor air-quality sensor (location {self.loc})"

    def wants(self, text: str) -> bool:
        """Should a question start refreshing this source? Only air-quality questions."""
        return intent.mentions_air(text)

    def poke(self):
        self.warmer.poke()

    async def close(self):
        await self.client.aclose()

    async def _get(self, kind: str, **params):
        self.requests += 1
        res = await self.client.get(API.format(loc=self.loc, kind=kind), params={**params, "token": self.token})
        if res.status_code != 200:
            raise RuntimeError(f"AirGradient {kind} failed: HTTP {res.status_code}")
        return res.json()

    def _lock(self, key) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _now(self) -> datetime:
        return datetime.now(self.tz).replace(tzinfo=None)

    def _when(self, iso: str) -> str:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(self.tz)
        return f"{dt:%a} {dt.day} {dt:%b %Y} {dt:%I:%M%p}".replace(" 0", " ").replace("AM", "am").replace("PM", "pm")

    # ---------- cached data ----------
    async def _current_row(self, refresh: bool = False) -> dict:
        async with self._lock("current"):
            if refresh or not self._current or time.time() - self._current[1] > FRESH_SECONDS:
                self._current = (await self._get("current"), time.time())
            return self._current[0]

    async def _day_rows(self, day: date, refresh: bool = False) -> list[dict]:
        """All of one local day's readings. Days finished over an hour ago are fetched once;
        today (and a just-finished day) is refetched once it's a few minutes old."""
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        final = self._now() > end + timedelta(hours=1)
        async with self._lock(day):
            if final and day in self._days:
                return self._days[day]
            cached = self._recent.get(day)
            if not final and cached and not refresh and time.time() - cached[1] <= FRESH_SECONDS:
                return cached[0]
            utc = lambda d: d.replace(tzinfo=self.tz).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            data = await self._get("past", **{"from": utc(start), "to": utc(min(end, self._now()))})
            rows = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
            rows = [r for r in rows if isinstance(r, dict) and r.get("timestamp")]
            if final:
                self._days[day] = rows
                self._recent.pop(day, None)
                for old in [d for d in self._days if d < day - timedelta(days=KEEP_DAYS)]:
                    del self._days[old]
            else:
                self._recent[day] = (rows, time.time())
            return rows

    async def warm(self, fresh: bool = True) -> str:
        """Current reading, today, and the last week of finished days."""
        before, today = self.requests, self._now().date()
        await self._current_row(fresh)
        await self._day_rows(today, fresh)
        for n in range(1, WARM_DAYS + 1):
            await self._day_rows(today - timedelta(days=n))
        return f"AirGradient {self.requests - before} request(s)"

    # ---------- the tool ----------
    async def handle(self, args: dict) -> str:
        """The air_quality tool: current reading, or a summary of a past period."""
        try:
            if args.get("start_date") or args.get("end_date") or args.get("chart"):
                return json.dumps(await self.history(args.get("start_date"), args.get("end_date"),
                                                     chart=bool(args.get("chart")), metrics=args.get("metrics")),
                                  ensure_ascii=False)
            reading = await self.current()
            reading.pop("_time_utc", None)
            return json.dumps(reading, ensure_ascii=False)
        except Exception as e:
            log.warning("AirGradient request failed: %s", e)
            return f"Error: couldn't read the air-quality sensor ({e})"

    async def current(self) -> dict:
        row = await self._current_row()
        out = {"sensor": row.get("locationName"), "sensor_type": row.get("locationType"),
               "time": self._when(row["timestamp"]) if row.get("timestamp") else None}
        if row.get("timestamp"):  # for the air alerts' staleness check (removed before the model sees it)
            out["_time_utc"] = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        for name, (fields, unit) in METRICS.items():
            v = _value(row, fields)
            if v is not None:
                out[name] = {"value": v, "unit": unit, "rating": rating(name, v)}
        if "pm2_5" in out:
            aqi, band = pm25_aqi(out["pm2_5"]["value"])
            out["pm2_5"].update(aqi_us=aqi, band=band)
        return out

    async def history(self, start: str | None, end: str | None, chart: bool = False,
                      metrics: list[str] | None = None) -> dict:
        now = self._now()
        parse = lambda s, d: datetime.fromisoformat(str(s).replace("T", " ")) if s else d
        t0, t1 = parse(start, now - timedelta(days=1)), min(parse(end, now), now)
        trimmed = t0 < t1 - timedelta(days=MAX_DAYS)
        t0 = max(t0, t1 - timedelta(days=MAX_DAYS))
        if t0 >= t1:
            return {"error": "start must be before end"}
        before = self.requests
        rows, day = [], t0.date()
        while day <= t1.date():
            rows += await self._day_rows(day)
            day += timedelta(days=1)
        lo_ts, hi_ts = t0.replace(tzinfo=self.tz).timestamp(), t1.replace(tzinfo=self.tz).timestamp()
        ts_of = lambda r: datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()
        rows = [r for r in rows if lo_ts <= ts_of(r) <= hi_ts]
        log.info("AirGradient history %s -> %s: %d readings, %d request(s)", t0, t1, len(rows), self.requests - before)
        out = {"sensor_type": "outdoor", "period": f"{t0:%a} {t0.day} {t0:%b %Y} - {t1:%a} {t1.day} {t1:%b %Y}",
               "readings": len(rows)}
        for name, (fields, unit) in METRICS.items():
            pts = [(r["timestamp"], v) for r in rows if (v := _value(r, fields)) is not None]
            if not pts:
                continue
            lo, hi = min(pts, key=lambda p: p[1]), max(pts, key=lambda p: p[1])
            entry = {"unit": unit, "low": lo[1], "low_time": self._when(lo[0]),
                     "high": hi[1], "high_time": self._when(hi[0]),
                     "average": round(sum(v for _, v in pts) / len(pts), 1)}
            entry["high_rating"], entry["average_rating"] = rating(name, hi[1]), rating(name, entry["average"])
            if name == "pm2_5":
                entry["high_aqi_us"], entry["high_band"] = pm25_aqi(hi[1])
                entry["average_aqi_us"], entry["average_band"] = pm25_aqi(entry["average"])
            out[name] = entry
        if trimmed:
            out["note_period"] = f"Only the last {MAX_DAYS} days can be fetched per question; this covers {out['period']}."
        if not rows:
            out["note"] = "No readings for this period."
        holder = CHART_REQUESTS.get()
        if chart and rows and holder is not None:
            wanted = [m for m in ALL_METRICS if m in (metrics or ["pm2_5"])] or ["pm2_5"]
            specs = [sp for sp in (self._chart_spec(m, rows, out["period"]) for m in wanted) if sp]
            spec = specs[0] if len(specs) == 1 else {  # several metrics: one image, a panel each
                "kind": "panels", "title": "Air quality", "subtitle": f"{out['period']}  \u00b7  AirGradient readings",
                "panels": [{"label": sp["title"], "unit": sp["unit"], "zones": sp["zones"],
                            "x": sp["series"][0]["x"], "y": sp["series"][0]["y"]} for sp in specs]} if specs else None
            if spec:
                holder.append(spec)
                out["chart"] = CHART_HINT
        return out

    @staticmethod
    def _points(name: str, rows: list[dict]) -> list[tuple[int, float]]:
        fields = METRICS[name][0]
        pts = []
        for r in rows:
            v = _value(r, fields)
            if v is not None:
                pts.append((int(datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()), v))
        return sorted(pts)

    def _chart_spec(self, name: str, rows: list[dict], period: str) -> dict | None:
        """One metric, in its own units."""
        pts = self._points(name, rows)
        if len(pts) < 2:
            return None
        lo, hi = min(pts, key=lambda p: p[1]), max(pts, key=lambda p: p[1])
        label = LABELS[name]
        return {"kind": "line", "title": label, "subtitle": f"{period}  \u00b7  AirGradient readings",
                "unit": CHART_UNITS[name], "zones": list(RATINGS[name]),
                "series": [{"label": label, "x": [t for t, _ in pts], "y": [v for _, v in pts],
                            "records": {"low": [lo[0], lo[1]], "high": [hi[0], hi[1]]}}]}
