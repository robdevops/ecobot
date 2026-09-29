"""Stand-ins for the Ecowitt and AirGradient HTTP APIs."""

import math
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from lib.config import Config

TZ = ZoneInfo("Australia/Melbourne")
MAC = "AA:BB:CC:DD:EE:FF"
HISTORY_DAYS = 548  # the fake station is 18 months old: enough for every test, and far quicker to archive than years
STEP = {"5min": 300, "30min": 1800, "4hour": 14400, "1day": 86400}


def config(tmp_path, **over) -> Config:
    base = dict(telegram_token="1:x", xai_api_key="k", xai_base_url="http://x", xai_model="m", tz=TZ,
                ecowitt_api_key="a", ecowitt_app_key="b", airgradient_token="t", airgradient_location="42",
                airgradient_dashboard="https://example.com/live", state_path=tmp_path / "state.json",
                cache_path=tmp_path / "cache.sqlite", air_cache_path=tmp_path / "air.sqlite")
    return Config(**{**base, **over})


@lru_cache(maxsize=None)  # pure and called for every sample of every request
def temp(ts: int) -> float:
    """Outdoor temperature: daily cycle peaking ~3pm local, slowly warming over the days."""
    local = datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ)
    return 14 + 6 * math.sin((local.hour + local.minute / 60 - 9) / 24 * 2 * math.pi) + ts / 86400 % 5 * 0.1


@lru_cache(maxsize=None)  # pure and called for every sample of every request
def rain_day(ts: int) -> float:
    """The rain counter, from 2pm to midnight: every 8th day of the year is wet (5 mm), the 4th after
    it is a trace (0.5 mm), the rest are dry."""
    local = datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ)
    amount = {0: 5.0, 4: 0.5}.get(local.timetuple().tm_yday % 8, 0.0)
    return amount if local.hour >= 14 else 0.0


@lru_cache(maxsize=None)  # pure and called for every sample of every request
def gust(ts: int) -> float:
    local = datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ)
    return 20.0 + local.day % 7 * 3 + (10 if local.hour == 15 else 0)


@lru_cache(maxsize=None)  # pure and called for every sample of every request
def direction(ts: int) -> float:
    """Wind direction that keeps crossing north: 350, 0, 10 degrees in turn (a plain average would say south)."""
    return (350 + ts // 300 % 3 * 10) % 360


@lru_cache(maxsize=None)  # pure and called for every sample of every request
def wind_speed(ts: int) -> float:
    """Calm from midnight to 6am local, 10 km/h otherwise."""
    return 0.0 if datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ).hour < 6 else 10.0


class FakeEcowitt:
    """Handles /device/list, /device/real_time and /device/history like api.ecowitt.net."""

    def __init__(self, busy_first: bool = False, history_days: int = HISTORY_DAYS):
        self.calls: list[dict] = []
        self.busy = busy_first
        self.history_days = history_days

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = dict(request.url.params)
        self.calls.append({"path": request.url.path.rsplit("/", 1)[-1], **p})
        if self.busy:
            self.busy = False
            return httpx.Response(200, json={"code": -1, "msg": "System is busy."})
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "list":
            return self._ok({"list": [{"mac": MAC.lower(), "name": "Fairleigh", "longitude": 145.0,
                                       "createtime": int((datetime.now(timezone.utc) - timedelta(days=self.history_days)).timestamp())}]})
        if path == "real_time":
            now = int(datetime.now(timezone.utc).timestamp())
            return self._ok({"outdoor": {"temperature": {"time": str(now), "unit": "℃", "value": "12.3"}}})
        return self._ok(self._history(p))

    @staticmethod
    def _ok(data):
        return httpx.Response(200, json={"code": 0, "msg": "success", "data": data})

    @staticmethod
    def _history(p: dict) -> dict:
        step = STEP[p["cycle_type"]]
        parse = lambda s: int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp())
        start, end = parse(p["start_date"]), parse(p["end_date"])
        first = start - start % step if p["cycle_type"] == "1day" else start  # 1day buckets are UTC days
        out: dict = {}
        for group in p["call_back"].split(","):
            named = {"rainfall": [("daily", rain_day)], "wind": [("wind_gust", gust), ("wind_direction", direction), ("wind_speed", wind_speed)]}
            fields: dict = {}
            for name, fn in named.get(group, [("temperature", temp)]):
                for ts in range(first, end + 1, step):
                    samples = [fn(t) for t in range(ts, ts + step, 300 if step <= 1800 else 3600)]
                    fields.setdefault(name, {"unit": "℃", "list": {}})["list"][str(ts)] = f"{samples[0]:.1f}"
                    if p["cycle_type"] != "5min":
                        for suffix, val in (("_low", min(samples)), ("_high", max(samples))):
                            fields.setdefault(name + suffix, {"unit": "℃", "list": {}})["list"][str(ts)] = f"{val:.1f}"
            out[group] = fields
        return out


def ecowitt_transport(fake: FakeEcowitt | None = None, history_days: int = HISTORY_DAYS) -> tuple[httpx.MockTransport, FakeEcowitt]:
    """history_days: how old the fake station is; tests of the archive's behaviour use a young one, which is much quicker."""
    fake = fake or FakeEcowitt(history_days=history_days)
    return httpx.MockTransport(fake), fake


def air_row(ts: datetime, pm25: float = 5.0, pm10: float = 8.0) -> dict:
    return {"timestamp": ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "locationName": "Outdoor",
            "locationType": "outdoor", "pm02_corrected": pm25, "pm10_corrected": pm10, "rco2_corrected": 450,
            "tvocIndex": 100, "noxIndex": 1}


def air_transport(rows_per_day: int = 24, oldest: datetime | None = None, hourly_before: datetime | None = None,
                  outage: tuple[datetime, datetime] | None = None) -> tuple[httpx.MockTransport, list]:
    """Like AirGradient's v1 API: `past` takes at most 10 days (422 otherwise), answers 404 when
    there is no data, and gives 5-minute buckets (rows_per_day = 288) or hourly ones for anything
    before hourly_before. oldest: the sensor's first reading; outage: (from, to) with no readings. transport.spans lists each past request."""
    calls: list = []
    spans: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        kind = request.url.path.rsplit("/", 1)[-1]
        calls.append(kind)
        if kind == "current":
            return httpx.Response(200, json=air_row(datetime.now(timezone.utc), pm25=12.0))
        start = datetime.strptime(request.url.params["from"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        end = datetime.strptime(request.url.params["to"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        spans.append((start, end))
        if end - start > timedelta(days=10):
            return httpx.Response(422, json={"message": "from .. to interval is too large"})
        rows, t = [], start
        while t <= end:
            hourly = hourly_before is not None and t < hourly_before
            if (oldest is None or t >= oldest) and not (outage and outage[0] <= t < outage[1]):
                rows.append(air_row(t, pm25=5 + (t.hour % 12)))
            t += timedelta(hours=1 if hourly else 24 / rows_per_day)
        return httpx.Response(200, json=rows) if rows else httpx.Response(404, json={"message": "No data available"})

    transport = httpx.MockTransport(handler)
    transport.spans = spans
    return transport, calls


# ---------- a station whose whole history is already archived (built once per test session) ----------
async def archived_station(tmp_path, cache_file):
    """(Ecowitt, fake) with a copy of the session's archived cache: what `await Archive(eco).run_once()` would
    leave, without paying for it in every test. fake.calls is empty."""
    import shutil

    from lib.ecowitt import Ecowitt
    transport, fake = ecowitt_transport()
    cfg = config(tmp_path)
    shutil.copy(cache_file, cfg.cache_path)
    eco = Ecowitt(cfg, transport=transport)
    await eco.start()
    fake.calls.clear()
    return eco, fake
