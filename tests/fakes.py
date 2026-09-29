"""Stand-ins for the Ecowitt and AirGradient HTTP APIs."""

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from lib.config import Config

TZ = ZoneInfo("Australia/Melbourne")
MAC = "AA:BB:CC:DD:EE:FF"
STEP = {"5min": 300, "30min": 1800, "4hour": 14400, "1day": 86400}


def config(tmp_path, **over) -> Config:
    base = dict(telegram_token="1:x", xai_api_key="k", xai_base_url="http://x", xai_model="m", tz=TZ,
                ecowitt_api_key="a", ecowitt_app_key="b", airgradient_token="t", airgradient_location="42",
                airgradient_dashboard="https://example.com/live", state_path=tmp_path / "state.json",
                cache_path=tmp_path / "cache.sqlite", air_cache_path=tmp_path / "air.sqlite")
    return Config(**{**base, **over})


def temp(ts: int) -> float:
    """Outdoor temperature: daily cycle peaking ~3pm local, slowly warming over the days."""
    local = datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ)
    return 14 + 6 * math.sin((local.hour + local.minute / 60 - 9) / 24 * 2 * math.pi) + ts / 86400 % 5 * 0.1


class FakeEcowitt:
    """Handles /device/list, /device/real_time and /device/history like api.ecowitt.net."""

    def __init__(self, busy_first: bool = False):
        self.calls: list[dict] = []
        self.busy = busy_first

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = dict(request.url.params)
        self.calls.append({"path": request.url.path.rsplit("/", 1)[-1], **p})
        if self.busy:
            self.busy = False
            return httpx.Response(200, json={"code": -1, "msg": "System is busy."})
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "list":
            return self._ok({"list": [{"mac": MAC.lower(), "name": "Fairleigh", "longitude": 145.0,
                                       "createtime": 1600000000}]})
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
            fields: dict = {}
            for ts in range(first, end + 1, step):
                samples = [temp(t) for t in range(ts, ts + step, 300 if step <= 1800 else 3600)]
                fields.setdefault("temperature", {"unit": "℃", "list": {}})["list"][str(ts)] = f"{samples[0]:.1f}"
                if p["cycle_type"] != "5min":
                    for name, val in (("temperature_low", min(samples)), ("temperature_high", max(samples))):
                        fields.setdefault(name, {"unit": "℃", "list": {}})["list"][str(ts)] = f"{val:.1f}"
            out[group] = fields
        return out


def ecowitt_transport(fake: FakeEcowitt | None = None) -> tuple[httpx.MockTransport, FakeEcowitt]:
    fake = fake or FakeEcowitt()
    return httpx.MockTransport(fake), fake


def air_row(ts: datetime, pm25: float = 5.0, pm10: float = 8.0) -> dict:
    return {"timestamp": ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "locationName": "Outdoor",
            "locationType": "outdoor", "pm02_corrected": pm25, "pm10_corrected": pm10, "rco2_corrected": 450,
            "tvocIndex": 100, "noxIndex": 1}


def air_transport(rows_per_day: int = 24, oldest: datetime | None = None) -> tuple[httpx.MockTransport, list]:
    """oldest: the sensor's first reading (nothing earlier exists)."""
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        kind = request.url.path.rsplit("/", 1)[-1]
        if kind == "current":
            return httpx.Response(200, json=air_row(datetime.now(timezone.utc), pm25=12.0))
        start = datetime.strptime(request.url.params["from"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        end = datetime.strptime(request.url.params["to"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        rows, t = [], start
        while t <= end:
            if oldest is None or t >= oldest:
                rows.append(air_row(t, pm25=5 + (t.hour % 12)))
            t += timedelta(hours=24 / rows_per_day)
        return httpx.Response(200, json=rows)

    return httpx.MockTransport(handler), calls
