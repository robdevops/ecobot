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
import statistics
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from .. import intent
from ..captions import CHART_HINT, wants_chart
from ..config import Config
from ..lines import Plotted, build_line
from ..specs import Chart, Line, Panel
from ..timeutil import local_date, now_local, to_local
from ..tools import Tool, Turn
from ..warm import Warmer
from .metrics import AIR_PANELS, ALL_METRICS, CHART_UNITS, MARK_LOW, LABELS, METRICS, RATINGS, epoch, normalise, pm25_aqi, rating, value_of
from .store import AirStore

log = logging.getLogger(__name__)

API = "https://api.airgradient.com/public/api/v1/locations/{loc}/measures/{kind}"
MAX_DAYS = 400                    # longest history period per question
MAX_REQUEST_DAYS = 9              # the API allows 10 days per request; a day of margin for clock changes
MAX_INLINE_DAYS = 30              # missing days fetched while answering; more are left to the backfill
HOURLY_MAX_READINGS = 30          # a day with this few readings holds hourly averages (24), not 5-minute (288)
FRESH_SECONDS = 300               # the current reading and today's history are reused this long
WARM_DAYS = 7                     # finished days kept ready by every refresh
BACKFILL_PACE = 1.0               # seconds between backfill requests
BACKFILL_EMPTY_STOP = 60          # this many empty days in a row, after some data: the sensor's data starts here
BACKFILL_EMPTY_BEFORE_DATA = 365  # how far back to look for any data at all (a long recent outage isn't the start)
BACKFILL_FAIL_STOP = 3            # this many failed requests in a row: give up until the next start
BACKFILL_MAX_DAYS = 1460

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
        if res.status_code == 404 and kind == "past":  # "No data available" for the period
            return []
        if res.status_code != 200:
            raise RuntimeError(f"AirGradient {kind} failed: HTTP {res.status_code}")
        return res.json()

    def _lock(self, key) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _now(self) -> datetime:
        return now_local(self.tz)

    def _when(self, ts: int) -> str:
        dt = to_local(ts, self.tz)
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

    async def _held(self, days: list[date]) -> dict[date, int | None]:
        """Readings stored per finished day, in one query (None: not fetched yet, or not finished)."""
        stored = await asyncio.to_thread(self.store.counts, days)
        return {d: stored[d] if self._final(d) else None for d in days}

    def _utc(self, local: datetime) -> str:
        return local.replace(tzinfo=self.tz).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _past(self, start: datetime, end: datetime) -> list[dict]:
        """Readings between two local times, normalised and oldest first."""
        data = await self._get("past", **{"from": self._utc(start), "to": self._utc(end)})
        raw = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
        return sorted((normalise(r) for r in raw if isinstance(r, dict) and r.get("timestamp")), key=lambda r: r["ts"])

    async def _fetch_days(self, days: list[date]):
        """Fetch finished days and store them, in as few requests as the API allows (it takes up to
        10 days at a time; we use 9)."""
        days = sorted(days)
        while days:
            window = [d for d in days if (d - days[0]).days < MAX_REQUEST_DAYS]
            days = days[len(window):]
            start = datetime.combine(window[0], datetime.min.time())
            end = datetime.combine(window[-1], datetime.min.time()) + timedelta(days=1)
            by_day: dict = {d: [] for d in window}
            for r in await self._past(start, min(end, self._now())):
                by_day.get(local_date(r["ts"], self.tz), []).append(r)
            for d, day_rows in by_day.items():
                await asyncio.to_thread(self.store.save_day, d, day_rows)
                self._recent.pop(d, None)

    async def _day_rows(self, day: date, refresh: bool = False) -> list[dict]:
        """All of one local day's readings, normalised. Finished days come from the disk cache (and
        are fetched once if missing); today, and a just-finished day, are refetched when a few
        minutes old."""
        final = self._final(day)
        async with self._lock(day):
            if final:
                if await asyncio.to_thread(self.store.day_count, day) is None:
                    await self._fetch_days([day])
                return await asyncio.to_thread(self.store.load_day, day)
            cached = self._recent.get(day)
            if cached and not refresh and time.time() - cached[1] <= FRESH_SECONDS:
                return cached[0]
            start = datetime.combine(day, datetime.min.time())
            rows = await self._past(start, min(start + timedelta(days=1), self._now()))
            self._recent[day] = (rows, time.time())
            return rows

    async def warm(self, fresh: bool = True) -> str:
        """Current reading, today, and the last week of finished days."""
        before, today = self.requests, self._now().date()
        await self._current_row(fresh)
        await self._day_rows(today, fresh)
        week = [today - timedelta(days=n) for n in range(1, WARM_DAYS + 1)]
        held = await self._held(week)
        await self._fetch_days([d for d in week if self._final(d) and held[d] is None])  # one request for all of them
        for day in week:
            if not self._final(day):  # a day that just ended is still refetched for a while; finished ones are stored
                await self._day_rows(day)
        return f"AirGradient {self.requests - before} req"

    async def backfill(self, pace: float | None = None, empty_stop: int | None = None,
                       empty_before_data: int | None = None, max_days: int = BACKFILL_MAX_DAYS) -> tuple[int, int, int]:
        """Cache every finished day back to where the sensor's data starts (newest first, so recent
        charts are ready soonest; up to 9 days per request). Returns (days fetched, days failed,
        days cached in total)."""
        pace = BACKFILL_PACE if pace is None else pace
        empty_stop = BACKFILL_EMPTY_STOP if empty_stop is None else empty_stop
        empty_before_data = BACKFILL_EMPTY_BEFORE_DATA if empty_before_data is None else empty_before_data
        today = self._now().date()
        fetched = failed = failed_run = empty_run = total = 0
        seen_data = False
        n = 1
        while n <= max_days:
            days = [today - timedelta(days=k) for k in range(n, min(n + MAX_REQUEST_DAYS, max_days + 1))]
            n += MAX_REQUEST_DAYS
            counts = await self._held(days)
            missing = [d for d in days if self._final(d) and counts[d] is None]
            if missing:
                try:
                    await self._fetch_days(missing)
                    fetched, failed_run = fetched + len(missing), 0
                    counts.update(await asyncio.to_thread(self.store.counts, missing))
                    if fetched // 30 != (fetched - len(missing)) // 30:
                        log.info("AirGradient archive: %d day(s) fetched so far, now at %s", fetched, days[-1])
                except Exception as e:
                    failed, failed_run = failed + len(missing), failed_run + 1
                    log.warning("AirGradient backfill of %s to %s failed: %s", missing[-1], missing[0], e)
                    if failed_run >= BACKFILL_FAIL_STOP:
                        break
                    continue
                finally:
                    await asyncio.sleep(pace)
            for d in days:  # newest first
                if counts[d] is None:
                    continue
                total += 1
                seen_data = seen_data or counts[d] > 0
                empty_run = empty_run + 1 if counts[d] == 0 else 0
                # A gap after data means the sensor's history starts before it; a gap before any data is
                # most likely a recent outage, so look much further back before giving up
                if empty_run >= (empty_stop if seen_data else empty_before_data):
                    return fetched, failed, total
        return fetched, failed, total

    # ---------- the tool ----------
    async def handle(self, args: dict, turn: Turn | None = None) -> str:
        """The air_quality tool: current reading, or a summary of a past period."""
        turn = turn or Turn()
        try:
            chart = wants_chart(args, turn)
            if args.get("start_date") or args.get("end_date") or chart:
                return json.dumps(await self.history(args.get("start_date"), args.get("end_date"),
                                                     chart=chart, metrics=args.get("metrics"), turn=turn),
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

    async def rows(self, t0: datetime, t1: datetime) -> tuple[list[dict], int, list[date]]:
        """(readings in [t0, t1] (naive local times), days that only exist as hourly averages, older days still waiting for
        the backfill and left out). Finished days not yet stored are fetched first, up to MAX_INLINE_DAYS."""
        days = [t0.date() + timedelta(days=k) for k in range((t1.date() - t0.date()).days + 1)]
        held = await self._held(days)
        missing = [d for d in days if self._final(d) and held[d] is None]
        skipped = missing[:-MAX_INLINE_DAYS] if len(missing) > MAX_INLINE_DAYS else []  # the oldest wait for the backfill
        await self._fetch_days([d for d in missing if d not in skipped])
        rows = []
        for d in days:
            if d not in skipped:  # finished days are all stored by now; today and a just-ended day come via _day_rows
                rows += await (asyncio.to_thread(self.store.load_day, d) if self._final(d) else self._day_rows(d))
        counts = await self._held([d for d in days if d not in skipped])
        hourly = sum(1 for c in counts.values() if c and c <= HOURLY_MAX_READINGS)
        lo_ts, hi_ts = t0.replace(tzinfo=self.tz).timestamp(), t1.replace(tzinfo=self.tz).timestamp()
        return [r for r in rows if lo_ts <= r["ts"] <= hi_ts], hourly, skipped

    async def history(self, start: str | None, end: str | None, chart: bool = False,
                      metrics: list[str] | None = None, turn: Turn | None = None) -> dict:
        turn = turn or Turn()
        now = self._now()
        parse = lambda s, d: datetime.fromisoformat(str(s).replace("T", " ")) if s else d
        t0, t1 = parse(start, now - timedelta(days=1)), min(parse(end, now), now)
        trimmed = t0 < t1 - timedelta(days=MAX_DAYS)
        t0 = max(t0, t1 - timedelta(days=MAX_DAYS))
        if t0 >= t1:
            return {"error": "start must be before end"}
        chart = wants_chart({"chart": chart}, turn, t0, t1)
        before = self.requests
        rows, hourly, skipped = await self.rows(t0, t1)
        log.info("AirGradient %s to %s: %d readings, %d req", f"{t0:%Y-%m-%d}", f"{t1:%m-%d %H:%M}", len(rows),
                 self.requests - before)
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
        if hourly:
            out["note_resolution"] = (f"{hourly} of the days in this period only exist as hourly averages (AirGradient keeps "
                                      "5-minute readings for recent months), so their highs and lows are hourly averages and "
                                      "short spikes are smoothed. Say so briefly if it matters to the answer.")
        if skipped:
            out["note_missing"] = (f"{len(skipped)} older day(s) aren't in the bot's cache yet and were left out; it is still "
                                   "downloading the sensor's history. Say so briefly.")
        if not rows:
            out["note"] = "No readings for this period."
        if chart and rows:
            chart_spec = self._chart([m for m in ALL_METRICS if m in (metrics or ["pm2_5"])] or ["pm2_5"], rows, out["period"])
            if chart_spec:
                turn.charts.append(chart_spec)
                out["chart"] = CHART_HINT
        return out

    def _line(self, name: str, rows: list[dict]) -> tuple[Line, Plotted] | None:
        """One metric as a chart line, in its own units, and how it was drawn. The record high/low are the true readings.
        The readings themselves while they fit the point budget; more than that are bucketed (30 minutes ... a day): each
        bucket's mean, its range shaded."""
        pts = [(r["ts"], r[name]) for r in rows if name in r]
        if len(pts) < 2:
            return None
        gap = max(60, round(statistics.median(b[0] - a[0] for a, b in zip(pts, pts[1:]))))
        plotted = build_line([(t, v, None, None, gap) for t, v in pts], self.tz, pts[-1][0] - pts[0][0], native_band=True)
        if plotted is None:
            return None
        lo, hi = min(pts, key=lambda p: p[1]), max(pts, key=lambda p: p[1])
        return plotted.spec(LABELS[name], {"high": hi, **({"low": lo} if name in MARK_LOW else {})}, name), plotted

    def _chart(self, names: list[str], rows: list[dict], period: str) -> Chart | None:
        """These metrics as one chart. One metric is drawn large, with its rating zones. Several go into panels on a
        shared time axis, grouped by AIR_PANELS (the particles together, the rest each alone)."""
        drawn = {n: got for n in names if (got := self._line(n, rows))}
        if not drawn:
            return None
        if len(drawn) == 1:
            (name, (line, plotted)), = drawn.items()
            subtitle = (f"{period}  ·  {plotted.name}" + (", range shaded" if plotted.low else "") + "  ·  records marked"
                        if not plotted.raw else f"{period}  ·  AirGradient readings")
            return Chart(LABELS[name], subtitle, [Panel(LABELS[name], CHART_UNITS[name], [line], zones=tuple(RATINGS[name]))])
        panels = []
        for group in AIR_PANELS:
            members = [m for m in group if m in drawn]
            if members:
                panels.append(Panel(", ".join(LABELS[m] for m in members), CHART_UNITS[members[0]], [drawn[m][0] for m in members],
                                    zones=tuple(RATINGS[members[0]]) if len(members) == 1 else None, aside=len(members) > 1))
        return Chart("Air quality", f"{period}  ·  AirGradient readings"
                     + ("  ·  range shaded" if any(plotted.low for _, plotted in drawn.values()) else ""), panels)
