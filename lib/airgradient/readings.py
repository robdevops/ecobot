"""The AirGradient's readings in the bot's file (lib/storage), as the air source keeps and reads them: the cloud's 5-minute averages (hourly
for the days it keeps only those), the bot's own 5-minute averages of what it read from the sensor itself (ranked above the cloud's, so
the cloud only fills what they lack), each bucket's lowest and highest as the row's band (the bot's own, and Home Assistant's), and the
finished days fetched as the coverage. A reading is {"ts", metric..., "_range": {metric: (lowest, highest)}}, as the source has always
had it. The days' figures (air_days) are the day rows, worked out as the readings are written (storage.days: the glitches taken out).
"""

from datetime import date, tzinfo
from typing import NamedTuple

from ..storage import Storage
from ..storage.catalogue import BOT, CLOUD
from ..storage.read import Reader, Rows
from ..storage.schema import q, some_value
from ..storage.write import MINUTES_KEPT
from ..ecowitt.query import SLACK_SECONDS
from ..timeutil import day_bounds
from .metrics import STORED

RESOLUTIONS = ("5min", "1hour")   # the cloud's recent history, and its older one
COLUMNS = list(STORED)
HOURLY_GAP = 3600


class Held(NamedTuple):
    """What is held of a day: how many readings (None: not fetched; 0: fetched and empty), and whether they are hourly averages only (the
    cloud's older history: their highs and lows are hourly averages)."""
    n: int | None
    hourly: bool


NOT_HELD = Held(None, False)


class DayFigures(NamedTuple):
    """air_days' read: each field's figure per day, how many readings each day has (None: not fetched), the days held hourly."""
    values: dict[str, dict[date, float]]
    counts: dict[date, int | None]
    hourly: set[date]


def resolution_of(rows: list[dict]) -> str:
    """The resolution a day's readings are at: hourly when they are an hour apart (the cloud's older history), else 5 minutes."""
    gaps = sorted(b["ts"] - a["ts"] for a, b in zip(rows, rows[1:]))
    return "1hour" if gaps and gaps[len(gaps) // 2] >= HOURLY_GAP else "5min"


def storage_rows(rows: list[dict], final: bool) -> list[dict]:
    """The source's readings as the writer takes them: each metric's value, and its band where the reading has one."""
    out = []
    for r in rows:
        row = {"ts": r["ts"], "final": int(final)}
        for c in COLUMNS:
            if (v := r.get(c)) is not None:
                row[c] = v
                if band := (r.get("_range") or {}).get(c):
                    row[c + "_lo"], row[c + "_hi"] = band
        out.append(row)
    return out


def readings(got: Rows | None) -> list[dict]:
    """A sensor's rows as the source's readings."""
    out = []
    if got is None:
        return out
    for i, ts in enumerate(got.ts):
        row, band = {"ts": ts}, {}
        for c, values in got.values.items():
            if (v := values[i]) is not None:
                row[c] = v
                lo, hi = got.lo[c][i], got.hi[c][i]
                if lo is not None and hi is not None:
                    band[c] = (lo, hi)
        if band:
            row["_range"] = band
        out.append(row)
    return out


class AirReadings:
    def __init__(self, storage: Storage, loc: str, tz: tzinfo, owned: bool = False):
        """owned: the storage is this source's to close (else the station's, shared)."""
        self.storage, self.loc, self.tz, self._owned = storage, str(loc), tz, owned

    @property
    def sensor(self):
        return self.storage.air_sensor(self.loc)

    def close(self):
        if self._owned:
            self.storage.close()

    # ---------- what is held ----------
    async def held(self, days: list[date]) -> dict[date, Held]:
        """What is held of each of these days (Held), in one read."""
        if not days:
            return {}
        return await self.storage.read(self._held_on, sorted(days))

    def _held_on(self, r: Reader, days: list[date]) -> dict[date, Held]:
        sensor = self.sensor
        if sensor is None:
            return {d: NOT_HELD for d in days}
        return self._held_of(r, sensor.id, days, r.days("air", [sensor.id], days[0], days[-1]).get(sensor.id))

    def _held_of(self, r: Reader, sensor: int, days: list[date], got) -> dict[date, Held]:
        """Each day's Held, from its day row (`got`: the sensor's day rows (read.Days) over these days, or None) and the coverage."""
        n_of = dict(zip(got.day, got.n)) if got else {}
        out = {}
        for d in days:
            start, end = day_bounds(d, self.tz, True)
            held = {res for res in RESOLUTIONS if r.held(sensor, res, start, end)}
            out[d] = Held(n_of.get(d, 0), held == {"1hour"}) if held else NOT_HELD
        return out

    async def counts(self, days: list[date]) -> dict[date, int | None]:
        """Readings held for each of these days, in one read: None for a day not fetched (0 for one fetched and empty)."""
        return {d: h.n for d, h in (await self.held(days)).items()}

    async def has_own(self, day: date) -> bool:
        """Does the bot hold any of this day's readings from the monitor itself or Home Assistant? (Such a day is completed by the cloud
        filling what they lack, never replaced.)"""
        lo, hi = day_bounds(day, self.tz)
        return await self.storage.read(lambda r: self.sensor is not None and r.db.execute(
            "SELECT 1 FROM air WHERE sensor = ? AND src > ? AND ts >= ? AND ts < ? LIMIT 1", (self.sensor.id, CLOUD, lo, hi)).fetchone() is not None)

    async def lacking(self) -> dict[str, int]:
        """How many readings held have no value for each column, and "all": how many are held."""
        def count(r: Reader):
            if self.sensor is None:
                return {"all": 0, **dict.fromkeys(COLUMNS, 0)}
            total, *nulls = r.db.execute(f"SELECT COUNT(*), {', '.join(f'COUNT(*) - COUNT({q(c)})' for c in COLUMNS)} FROM air "
                                         f"WHERE sensor = ? AND res IN ('5min', '1hour') AND {some_value(COLUMNS)}", (self.sensor.id,)).fetchone()
            return {"all": total, **dict(zip(COLUMNS, nulls))}
        return await self.storage.read(count)

    # ---------- writing ----------
    async def save_day(self, day: date, rows: list[dict]) -> int:
        """Keep one finished day's readings from the cloud, in place of those it had from there (the bot's own stay, and the bands only
        widen), at the resolution they are at; the day is held. Returns what it holds (Held)."""
        lo, hi = day_bounds(day, self.tz)
        rows = sorted((r for r in rows if lo <= r["ts"] < hi), key=lambda r: r["ts"])
        res = resolution_of(rows)
        held = Held(len(rows), res == "1hour")

        def write(w):
            sensor = w.air_sensor(self.loc)
            for other in RESOLUTIONS:   # (a day fetched again at another resolution: the earlier one goes)
                if other != res:
                    w.db.execute("DELETE FROM air WHERE sensor = ? AND res = ? AND src = ? AND ts >= ? AND ts < ?", (sensor.id, other, CLOUD, lo, hi))
                    w.uncover_span(sensor, other, lo, hi - 1)
            w.fields(sensor, res, {c: STORED[c][1] for c in COLUMNS})
            w.put(sensor, res, storage_rows(rows, True), CLOUD)
            w.cover(sensor, res, lo, hi - 1)
        await self.storage.write(write)
        return held

    async def add_recorded(self, row: dict, src: int = BOT) -> None:
        """Keep a reading the bot took itself (a 5-minute average of the monitor's readings, or of Home Assistant's states of it: `src`;
        with the lowest and highest of the readings averaged as its band: what a chart shades), in place of the cloud's for that bucket."""
        await self.storage.write(lambda w: w.put(w.air_sensor(self.loc), "5min", storage_rows([row], True), src))

    async def add_spreads(self, found: dict[str, dict[int, tuple[float, float]]]) -> int:
        """Widen the 5-minute buckets' bands by these ({metric: {bucket start: (lowest, highest)}}, Home Assistant's). Returns how many."""
        return await self.storage.write(lambda w: w.widen(w.air_sensor(self.loc), "5min", found))

    async def merge(self, rows: list[dict], final: bool, src: int = CLOUD) -> int:
        """Keep these readings of a source's (the cloud's, else `src`) where the sources ranked above it lack them (a bucket the bot
        recorded keeps its own); `final`: of a finished day, else they are replaced by the next fetch. Returns how many were written."""
        if not rows:
            return 0
        return await self.storage.write(lambda w: w.put(w.air_sensor(self.loc), "5min", storage_rows(rows, final), src))

    async def add_minutes(self, rows: list[dict], src: int, now: float) -> int:
        """Keep these minutes ([{"ts", metric...}]: the monitor's reading each minute, or Home Assistant's state at the end of each),
        MINUTES_KEPT long. Returns how many."""
        if not rows:
            return 0

        def write(w):
            n = w.put(w.air_sensor(self.loc), "1min", storage_rows(rows, True), src)
            w.prune_minutes(int(now - MINUTES_KEPT))
            return n
        return await self.storage.write(write)

    async def load_minutes(self, start_ts: int, end_ts: int) -> list[dict]:
        """The minutes with start_ts <= ts < end_ts, oldest first; [] when there are none or they do not reach back to the start (a
        chart then draws the 5-minute readings)."""
        def read(r: Reader):
            if self.sensor is None:
                return []
            rows = readings(r.rows("air", [self.sensor.id], "1min", start_ts, end_ts - 1).get(self.sensor.id))
            return rows if rows and rows[0]["ts"] <= start_ts + SLACK_SECONDS else []
        return await self.storage.read(read)

    async def forget_days(self) -> None:
        """Note no day as fetched, so the next backfill fetches every finished day again (the readings stay, filled in as they come)."""
        def write(w):
            sensor = w.air_sensor(self.loc)
            for res in RESOLUTIONS:
                w.uncover(sensor, res)
        await self.storage.write(write)

    async def mark_day(self, day: date) -> None:
        """Note that this finished day of the bot's own readings is complete."""
        lo, hi = day_bounds(day, self.tz)
        await self.storage.write(lambda w: w.cover(w.air_sensor(self.loc), "5min", lo, hi - 1))

    # ---------- reading ----------
    async def load(self, start_ts: int, end_ts: int, srcs: tuple[int, ...] | None = None) -> list[dict]:
        """Readings with start_ts <= ts < end_ts, oldest first, with their bands; srcs: only those these sources gave (a row's source is
        the highest that gave any of it)."""
        return await self.storage.read(self._load_on, start_ts, end_ts, srcs)

    def _load_on(self, r: Reader, start_ts: int, end_ts: int, srcs: tuple[int, ...] | None = None) -> list[dict]:
        sensor = self.sensor
        if sensor is None:
            return []
        rows = [row for res in RESOLUTIONS for row in readings(r.rows("air", [sensor.id], res, start_ts, end_ts - 1, srcs=srcs).get(sensor.id))]
        return sorted(rows, key=lambda row: row["ts"])

    async def load_day(self, day: date) -> list[dict]:
        return await self.load(*day_bounds(day, self.tz))

    async def spreads(self, start_ts: int, end_ts: int) -> dict[int, dict[str, tuple[float, float]]]:
        """{bucket start: {metric: (lowest, highest)}} of the 5-minute bands held with start_ts <= ts < end_ts (those of a bucket whose
        reading is not held yet too)."""
        def read(r: Reader):
            if self.sensor is None:
                return {}
            cells = ", ".join(f"{q(c + '_lo')}, {q(c + '_hi')}" for c in COLUMNS)
            out: dict[int, dict] = {}
            for ts, *bands in r.db.execute(f"SELECT ts, {cells} FROM air WHERE sensor = ? AND res = '5min' AND ts >= ? AND ts < ? "
                                            f"AND {some_value([c + '_lo' for c in COLUMNS])}", (self.sensor.id, start_ts, end_ts)):
                for i, c in enumerate(COLUMNS):
                    if bands[2 * i] is not None:
                        out.setdefault(ts, {})[c] = (bands[2 * i], bands[2 * i + 1])
            return out
        return await self.storage.read(read)

    async def day_figures(self, first: date, last: date, shown: list[str]) -> DayFigures:
        """Each day's figures for air_days: each field ("pm2_5_max": the day's highest, _min lowest, _avg mean) from the day rows."""
        def read(r: Reader) -> DayFigures:
            sensor = self.sensor
            days = [first.fromordinal(n) for n in range(first.toordinal(), last.toordinal() + 1)]
            if sensor is None:
                return DayFigures({n: {} for n in shown}, {d: None for d in days}, set())
            got = r.days("air", [sensor.id], first, last).get(sensor.id)
            values: dict[str, dict[date, float]] = {n: {} for n in shown}
            if got:
                for name in shown:
                    metric, kind = name.rsplit("_", 1)
                    figures = {"max": got.hi, "min": got.lo, "avg": got.mean}[kind].get(metric)
                    if figures:
                        values[name] = {d: v for d, v in zip(got.day, figures) if v is not None}
            held = self._held_of(r, sensor.id, days, got)
            return DayFigures(values, {d: h.n for d, h in held.items()}, {d for d, h in held.items() if h.hourly})
        return await self.storage.read(read)


__all__ = ["AirReadings", "DayFigures", "Held", "NOT_HELD", "readings", "resolution_of", "storage_rows"]
