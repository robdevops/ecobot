"""Air quality from your own AirGradient sensor, as a data source (peer of Ecowitt):
start() checks the sensor, tools are what the model can call, warm()/poke() keep recent
readings ready, backfill() caches the sensor's whole history, current() feeds the alert monitor.

AirGradient's documented API:
  current: GET /public/api/v1/locations/{id}/measures/current?token=...
  history: GET /public/api/v1/locations/{id}/measures/past?from=...&to=...&token=...

Finished days are stored on disk (store.py), so questions are answered without asking
AirGradient unless a day is missing. Reports AirGradient's corrected (calibrated) values
where available, plus a US EPA AQI and band for PM2.5.
"""

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from .. import intent
from ..charts import CHART_HINT, CHART_REQUESTS
from ..config import Config
from ..tools import Tool
from ..warm import Warmer
from .metrics import ALL_METRICS, CHART_UNITS, LABELS, METRICS, RATINGS, epoch, normalise, pm25_aqi, rating, value_of
from .store import AirStore

log = logging.getLogger(__name__)

API = "https://api.airgradient.com/public/api/v1/locations/{loc}/measures/{kind}"
MAX_DAYS = 400                    # longest history period per question
MAX_INLINE_DAYS = 14              # missing days fetched while answering; more are left to the backfill
FRESH_SECONDS = 300               # the current reading and today's history are reused this long
WARM_DAYS = 7                     # finished days kept ready by every refresh
BACKFILL_PACE = 1.0               # seconds between backfill requests
BACKFILL_EMPTY_STOP = 30          # this many empty days in a row: the sensor's data starts here
BACKFILL_FAIL_STOP = 5            # this many failures in a row: give up until the next start
BACKFILL_MAX_DAYS = 1460
CHART_POINTS = 1500               # long charts are averaged down to about this many points

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
               "PM1, CO2, VOC and NOx indexes, each with a traffic-light rating. No dates: the current reading. "
               "With start_date/end_date (up to about a year): lowest, highest and average of each, with when they "
               "happened. chart=true sends a graph.")


def downsample(pts: list[tuple[int, float]], target: int = CHART_POINTS) -> list[tuple[int, float]]:
    """Average long series into about `target` points (the true peak is drawn separately)."""
    if len(pts) <= target:
        return pts
    width = max(1, (pts[-1][0] - pts[0][0]) // target)
    bins: dict = {}
    for t, v in pts:
        bins.setdefault(t // width, []).append((t, v))
    return [(sum(t for t, _ in b) // len(b), sum(v for _, v in b) / len(b)) for _, b in sorted(bins.items())]


class AirGradient:
    """AirGradient client with its data kept warm and cached:
      - the current reading and today's history are refreshed every few minutes (warm);
      - finished days are fetched once and stored on disk, so they never need fetching again."""

    name = "AirGradient"

    def __init__(self, cfg: Config, transport=None):
        self.loc, self.tz = cfg.airgradient_location, cfg.tz
        self.token = cfg.airgradient_token
        self.link = ("live chart", cfg.airgradient_dashboard) if cfg.airgradient_dashboard else None
        self.client = httpx.AsyncClient(timeout=15, transport=transport)
        self.store = AirStore(cfg.air_cache_path, self.loc, self.tz)
        self._current: tuple[dict, float] | None = None       # (raw row, fetched at)
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
        self.store.close()

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

    def _when(self, ts: int) -> str:
        dt = datetime.fromtimestamp(ts, timezone.utc).astimezone(self.tz)
        return f"{dt:%a} {dt.day} {dt:%b %Y} {dt:%I:%M%p}".replace(" 0", " ").replace("AM", "am").replace("PM", "pm")

    # ---------- cached data ----------
    async def _current_row(self, refresh: bool = False) -> dict:
        async with self._lock("current"):
            if refresh or not self._current or time.time() - self._current[1] > FRESH_SECONDS:
                self._current = (await self._get("current"), time.time())
            return self._current[0]

    def _final(self, day: date) -> bool:
        """Over for more than an hour, so its data won't change."""
        return self._now() > datetime.combine(day, datetime.min.time()) + timedelta(days=1, hours=1)

    async def _stored(self, day: date) -> bool:
        return self._final(day) and await asyncio.to_thread(self.store.day_count, day) is not None

    async def _day_rows(self, day: date, refresh: bool = False) -> list[dict]:
        """All of one local day's readings, normalised. Finished days come from the disk cache (and
        are fetched once if missing); today, and a just-finished day, are refetched when a few
        minutes old."""
        start = datetime.combine(day, datetime.min.time())
        final = self._final(day)
        async with self._lock(day):
            if final and await asyncio.to_thread(self.store.day_count, day) is not None:
                return await asyncio.to_thread(self.store.load_day, day)
            cached = self._recent.get(day)
            if not final and cached and not refresh and time.time() - cached[1] <= FRESH_SECONDS:
                return cached[0]
            utc = lambda d: d.replace(tzinfo=self.tz).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            data = await self._get("past", **{"from": utc(start), "to": utc(min(start + timedelta(days=1), self._now()))})
            raw = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
            rows = sorted((normalise(r) for r in raw if isinstance(r, dict) and r.get("timestamp")), key=lambda r: r["ts"])
            if final:
                await asyncio.to_thread(self.store.save_day, day, rows)
                self._recent.pop(day, None)
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

    async def backfill(self, pace: float | None = None, empty_stop: int | None = None,
                       max_days: int = BACKFILL_MAX_DAYS) -> tuple[int, int, int]:
        """Cache every finished day back to where the sensor's data starts (newest first, so recent
        charts are ready soonest). Returns (days fetched, days failed, days cached in total)."""
        pace = BACKFILL_PACE if pace is None else pace
        empty_stop = BACKFILL_EMPTY_STOP if empty_stop is None else empty_stop
        today = self._now().date()
        fetched = failed = failed_run = empty_run = total = 0
        for n in range(1, max_days + 1):
            day = today - timedelta(days=n)
            count = await asyncio.to_thread(self.store.day_count, day) if self._final(day) else None
            if count is None:
                try:
                    count = len(await self._day_rows(day))
                    fetched, failed_run = fetched + 1, 0
                    if fetched % 30 == 0:
                        log.info("AirGradient archive: %d day(s) fetched so far, now at %s", fetched, day)
                except Exception as e:
                    failed, failed_run = failed + 1, failed_run + 1
                    log.warning("AirGradient backfill of %s failed: %s", day, e)
                    if failed_run >= BACKFILL_FAIL_STOP:
                        break
                    continue
                finally:
                    await asyncio.sleep(pace)
            total += 1
            empty_run = empty_run + 1 if count == 0 else 0
            if empty_run >= empty_stop:
                break
        return fetched, failed, total

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
               "time": self._when(epoch(row["timestamp"])) if row.get("timestamp") else None}
        if row.get("timestamp"):  # for the air alerts' staleness check (removed before the model sees it)
            out["_time_utc"] = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        for name, (_, unit) in METRICS.items():
            v = value_of(row, name)
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
        rows, skipped, inline, day = [], 0, 0, t0.date()
        while day <= t1.date():
            if not await self._stored(day):
                inline += self._final(day)
                if inline > MAX_INLINE_DAYS:
                    skipped, day = skipped + 1, day + timedelta(days=1)
                    continue
            rows += await self._day_rows(day)
            day += timedelta(days=1)
        lo_ts, hi_ts = t0.replace(tzinfo=self.tz).timestamp(), t1.replace(tzinfo=self.tz).timestamp()
        rows = [r for r in rows if lo_ts <= r["ts"] <= hi_ts]
        log.info("AirGradient history %s -> %s: %d readings, %d request(s)", t0, t1, len(rows), self.requests - before)
        out = {"sensor_type": "outdoor", "period": f"{t0:%a} {t0.day} {t0:%b %Y} - {t1:%a} {t1.day} {t1:%b %Y}",
               "readings": len(rows)}
        for name, (_, unit) in METRICS.items():
            pts = [(r["ts"], r[name]) for r in rows if name in r]
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
            out["note_period"] = f"Only the last {MAX_DAYS} days can be covered per question; this covers {out['period']}."
        if skipped:
            out["note_missing"] = (f"{skipped} older day(s) aren't in the bot's cache yet and were left out; it is still "
                                   "downloading the sensor's history. Say so briefly.")
        if not rows:
            out["note"] = "No readings for this period."
        holder = CHART_REQUESTS.get()
        if chart and rows and holder is not None:
            wanted = [m for m in ALL_METRICS if m in (metrics or ["pm2_5"])] or ["pm2_5"]
            specs = [sp for sp in (self._chart_spec(m, rows, out["period"]) for m in wanted) if sp]
            spec = specs[0] if len(specs) == 1 else {  # several metrics: one image, a panel each
                "kind": "panels", "title": "Air quality", "subtitle": f"{out['period']}  ·  AirGradient readings",
                "panels": [{"label": sp["title"], "unit": sp["unit"], "zones": sp["zones"],
                            "x": sp["series"][0]["x"], "y": sp["series"][0]["y"]} for sp in specs]} if specs else None
            if spec:
                holder.append(spec)
                out["chart"] = CHART_HINT
        return out

    @staticmethod
    def _chart_spec(name: str, rows: list[dict], period: str) -> dict | None:
        """One metric, in its own units. The record high/low are the true readings; the line is
        averaged down when the period is long."""
        pts = [(r["ts"], r[name]) for r in rows if name in r]
        if len(pts) < 2:
            return None
        lo, hi = min(pts, key=lambda p: p[1]), max(pts, key=lambda p: p[1])
        line = downsample(pts)
        label = LABELS[name]
        return {"kind": "line", "title": label, "subtitle": f"{period}  ·  AirGradient readings",
                "unit": CHART_UNITS[name], "zones": list(RATINGS[name]),
                "series": [{"label": label, "x": [t for t, _ in line], "y": [v for _, v in line],
                            "records": {"low": [lo[0], lo[1]], "high": [hi[0], hi[1]]}}]}
