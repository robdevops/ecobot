"""Stand-ins for the Ecowitt and AirGradient HTTP APIs."""

import math
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from lib.config import Config
from lib.tools import Tools

TZ = ZoneInfo("Australia/Melbourne")
MAC = "AA:BB:CC:DD:EE:FF"
EVERY_SOURCE = ("Ecowitt", "AirGradient", "Pollen", "Forecast")   # every source, by name
HISTORY_DAYS = 548  # the fake station is 18 months old: enough for every test, and far quicker to archive than years
STEP = {"5min": 300, "30min": 1800, "4hour": 14400, "1day": 86400}


class StandInTools:
    """A test's stand-in for lib.tools.Tools: it answers call(name, raw, turn) itself, and call_all is the real one over it."""
    call_all = Tools.call_all


def config(tmp_path, **over) -> Config:
    base = dict(telegram_token="1:x", xai_api_key="k", xai_base_url="http://x", xai_model="m", tz=TZ,
                ecowitt_api_key="a", ecowitt_app_key="b", airgradient_token="t", airgradient_location="42",
                airgradient_dashboard="https://example.com/live", state_path=tmp_path / "state.json",
                cache_path=tmp_path / "cache.sqlite", air_cache_path=tmp_path / "air.sqlite",
                conditions_cache_path=tmp_path / "conditions.sqlite", admin_only=False)   # tests that are about ADMIN_ONLY switch it on
    return Config(**{**base, **over})


@lru_cache(maxsize=None)  # pure and called for every sample of every request
def temp(ts: int) -> float:
    """Outdoor temperature: daily cycle peaking ~3pm local, slowly warming over the days."""
    local = datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ)
    return 14 + 6 * math.sin((local.hour + local.minute / 60 - 9) / 24 * 2 * math.pi) + ts / 86400 % 5 * 0.1


@lru_cache(maxsize=None)  # pure and called for every sample of every request
def humidity(ts: int) -> float:
    """Outdoor humidity: the mirror of the temperature, so it is highest at night."""
    return 70 - (temp(ts) - 14) * 2.5


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
@lru_cache(maxsize=None)
def pressure(ts: int) -> float:
    """Relative pressure: a slow swing over about a week, with a small twice-daily tide."""
    return 1015 + 8 * math.sin(ts / 86400 / 1.1) + 0.7 * math.sin(ts / 3600 / 12 * 2 * math.pi)


def direction(ts: int) -> float:
    """Wind direction that keeps crossing north: 350, 0, 10 degrees in turn (a plain average would say south)."""
    return (350 + ts // 300 % 3 * 10) % 360


@lru_cache(maxsize=None)  # pure and called for every sample of every request
def wind_speed(ts: int) -> float:
    """Calm from midnight to 6am local, 10 km/h to 6pm, then 25 km/h."""
    hour = datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ).hour
    return 0.0 if hour < 6 else 10.0 if hour < 18 else 25.0


@lru_cache(maxsize=None)
def uv_index(ts: int) -> float:
    """Dark until 7am local, a daytime hump peaking at 11 around 1pm, dark again from 7pm."""
    local = datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ)
    hours = local.hour + local.minute / 60
    return 0.0 if hours < 7 or hours > 19 else round(11 * (1 - ((hours - 13) / 6) ** 2), 1)


class FakeEcowitt:
    """Handles /device/list, /device/real_time and /device/history like api.ecowitt.net."""

    def __init__(self, busy_first: bool = False, history_days: int = HISTORY_DAYS, zone: str | None = "Australia/Melbourne"):
        """zone: the time zone the device list names (date_zone_id), None for none."""
        self.calls: list[dict] = []
        self.busy = busy_first
        self.history_days = history_days
        self.zone = zone

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = dict(request.url.params)
        self.calls.append({"path": request.url.path.rsplit("/", 1)[-1], **p})
        if self.busy:
            self.busy = False
            return httpx.Response(200, json={"code": -1, "msg": "System is busy."})
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "list":
            return self._ok({"list": [{"mac": MAC.lower(), "name": "Fairleigh", "longitude": 145.0, "latitude": -37.8,
                                       "createtime": int((datetime.now(timezone.utc) - timedelta(days=self.history_days)).timestamp()),
                                       **({"date_zone_id": self.zone} if self.zone else {})}]})
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
            named = {"outdoor": [("temperature", temp), ("humidity", humidity), ("dew_point", temp)], "rainfall": [("daily", rain_day)], "pressure": [("relative", pressure)], "wind": [("wind_gust", gust), ("wind_direction", direction), ("wind_speed", wind_speed)], "solar_and_uvi": [("uvi", uv_index), ("solar", lambda ts: uv_index(ts) * 80)]}
            fields: dict = {}
            for name, fn in named.get(group, [("temperature", temp)]):
                for ts in range(first, end + 1, step):
                    samples = [fn(t) for t in range(ts, ts + step, 300 if step <= 1800 else 3600)]
                    fields.setdefault(name, {"unit": "%" if name == "humidity" else "℃", "list": {}})["list"][str(ts)] = f"{samples[0]:.1f}"
                    if p["cycle_type"] != "5min":
                        for suffix, val in (("_low", min(samples)), ("_high", max(samples))):
                            fields.setdefault(name + suffix, {"unit": "%" if name == "humidity" else "℃", "list": {}})["list"][str(ts)] = f"{val:.1f}"
            out[group] = fields
        return out


# series -> (the entity's name after sensor.ws2900_, Home Assistant's unit, its number at a time): what a Home Assistant with the Ecowitt integration holds
HA_PREFIX = "sensor.ws2900_"
HA_STATES = {
    "outdoor_temperature": ("°C", temp, 1), "humidity": ("%", humidity, 0), "dewpoint": ("°C", lambda t: temp(t) - 3, 1),
    "feels_like_temperature": ("°C", lambda t: temp(t) - 1, 1), "indoor_temperature": ("°C", lambda t: 21.0, 1),
    "indoor_humidity": ("%", lambda t: 55.0, 0), "relative_pressure": ("hPa", pressure, 1), "wind_speed": ("km/h", wind_speed, 1),
    "wind_gust": ("km/h", gust, 1), "wind_direction": ("°", direction, 0), "daily_rain": ("mm", rain_day, 1), "rain_rate": ("mm/h", lambda t: 0.0, 1),
    "solar_radiation": ("W/m²", lambda t: uv_index(t) * 80, 1), "uv_index": ("UV index", uv_index, 1),
}
HA_STEP = 64   # the gateway reports about once a minute


class FakeHomeAssistant:
    """Handles /api/config, /api/states and /api/history/period like Home Assistant with the Ecowitt integration set up, from the same weather
    functions as FakeEcowitt. It has recorded `history_days` days; a state is recorded only when its number changes, as Home Assistant does.
    `off`: it answers 503 (it is down); `units`: another unit for an entity (what its owner chose to see)."""

    def __init__(self, history_days: float = 10, begins: float | None = None, units: dict | None = None, token: str = "tok"):
        self.begins = begins if begins is not None else datetime.now(timezone.utc).timestamp() - history_days * 86400
        self.units, self.token, self.off = units or {}, token, False
        self.calls: list[dict] = []

    def number(self, name: str, ts: float) -> float:
        unit, fn, decimals = HA_STATES[name]
        value = round(fn(int(ts)), decimals)
        return self.convert(name, value)

    def convert(self, name: str, value: float) -> float:
        """The number in the unit its owner chose to see (Fahrenheit, mph ... as `units` says)."""
        unit = self.units.get(name)
        return {"°F": lambda v: round(v * 9 / 5 + 32, 1), "mph": lambda v: round(v / 1.609344, 1), "inHg": lambda v: round(v / 33.8639, 2)}[unit](value) if unit else value

    def unit(self, name: str) -> str:
        return self.units.get(name, HA_STATES[name][0])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append({"path": request.url.path, **dict(request.url.params)})
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"message": "401: Unauthorized"})
        if self.off:
            return httpx.Response(503, text="unavailable")
        path = request.url.path
        now = datetime.now(timezone.utc).timestamp()
        if path == "/api/config":
            return httpx.Response(200, json={"time_zone": "Australia/Melbourne", "latitude": -37.8, "longitude": 145.0, "location_name": "Home"})
        if path == "/api/states":
            at = datetime.fromtimestamp(now - 20, timezone.utc).isoformat()
            states = [{"entity_id": HA_PREFIX + n, "state": str(self.number(n, now)), "attributes": {"unit_of_measurement": self.unit(n)},
                       "last_changed": at, "last_updated": at} for n in HA_STATES]
            return httpx.Response(200, json=[*states, {"entity_id": "light.kitchen", "state": "on", "attributes": {}}])
        start = datetime.fromisoformat(path.rsplit("/", 1)[-1]).timestamp()
        end = datetime.fromisoformat(request.url.params["end_time"]).timestamp() if "end_time" in request.url.params else start + 86400
        out = []
        for entity in request.url.params["filter_entity_id"].split(","):
            name = entity.removeprefix(HA_PREFIX)
            if name not in HA_STATES:
                continue
            rows, last = [], None
            first = int(max(start, self.begins) // HA_STEP * HA_STEP)
            if self.begins < start:   # held before the start: its state at the start is the first row
                last = self.number(name, int(start) // HA_STEP * HA_STEP)
                rows.append({"entity_id": entity, "state": str(last), "last_changed": datetime.fromtimestamp(start, timezone.utc).isoformat()})
            for ts in range(first, int(min(end, now)), HA_STEP):
                if ts < self.begins:
                    continue
                value = self.number(name, ts)
                if value != last:
                    row = {"state": str(value), "last_changed": datetime.fromtimestamp(ts + 0.25, timezone.utc).isoformat()}
                    rows.append({"entity_id": entity, **row} if not rows else row)
                    last = value
            if rows:
                out.append(rows)
        return httpx.Response(200, json=out)


def home_assistant_transport(fake: FakeHomeAssistant | None = None, **kw) -> tuple[httpx.MockTransport, FakeHomeAssistant]:
    fake = fake or FakeHomeAssistant(**kw)
    return httpx.MockTransport(fake), fake


def ecowitt_transport(fake: FakeEcowitt | None = None, history_days: int = HISTORY_DAYS) -> tuple[httpx.MockTransport, FakeEcowitt]:
    """history_days: how old the fake station is; tests of the archive's behaviour use a young one, which is much quicker."""
    fake = fake or FakeEcowitt(history_days=history_days)
    return httpx.MockTransport(fake), fake


def air_row(ts: datetime, pm25: float = 5.0, pm10: float = 8.0, pm1: float = 3.0) -> dict:
    return {"timestamp": ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "locationName": "Outdoor",
            "locationType": "outdoor", "pm02_corrected": pm25, "pm10_corrected": pm10, "pm01_corrected": pm1, "rco2_corrected": 450,
            "tvocIndex": 100, "noxIndex": 1, "atmp_corrected": 18.0 + ts.hour / 4, "rhum_corrected": 60.0 - ts.hour}


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
