"""Settings, all from environment variables (systemd loads them from the unit's EnvironmentFile)."""

import os
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def _tz() -> tzinfo:
    name = os.getenv("TZ", "").lstrip(":")
    return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo


def _on(value: str) -> bool:
    return value.lower() in ("on", "1", "true", "yes")


def _number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Config:
    telegram_token: str
    xai_api_key: str
    xai_base_url: str
    xai_model: str
    tz: tzinfo
    ecowitt_api_key: str
    ecowitt_app_key: str
    airgradient_token: str
    airgradient_location: str
    airgradient_dashboard: str
    state_path: Path = ROOT / "bot_state.json"
    cache_path: Path = ROOT / "ecowitt_cache.sqlite"
    air_cache_path: Path = ROOT / "airgradient_cache.sqlite"
    pollen: bool = False                 # POLLEN=on: Melbourne grass pollen and thunderstorm asthma risk (a scraped website)
    pollen_district: str = "Central"     # the Victorian forecast district whose thunderstorm asthma risk is used
    forecast: bool = False               # FORECAST=on: the BOM daily forecast (Open-Meteo as a fallback)
    forecast_lat: float | None = None    # FORECAST_LAT / FORECAST_LON; the weather station's own location when unset
    forecast_lon: float | None = None

    @property
    def ecowitt(self) -> bool:
        return bool(self.ecowitt_api_key and self.ecowitt_app_key)

    @property
    def airgradient(self) -> bool:
        return bool(self.airgradient_token and self.airgradient_location)

    @classmethod
    def from_env(cls) -> "Config":
        env = lambda k, default="": os.getenv(k, default).strip()
        cfg = cls(
            telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
            xai_api_key=os.environ["XAI_API_KEY"],
            xai_base_url=env("XAI_BASE_URL", "https://api.x.ai/v1"),
            xai_model=env("XAI_MODEL", "grok-4.3"),
            tz=_tz(),
            ecowitt_api_key=env("ECOWITT_API_KEY"),
            ecowitt_app_key=env("ECOWITT_APP_KEY"),
            airgradient_token=env("AIRGRADIENT_API_TOKEN"),
            airgradient_location=env("AIRGRADIENT_LOCATION_ID"),
            airgradient_dashboard=env("AIRGRADIENT_DASHBOARD_URL"),
            pollen=_on(env("POLLEN")),
            pollen_district=env("POLLEN_DISTRICT", "Central"),
            forecast=_on(env("FORECAST")),
            forecast_lat=_number(env("FORECAST_LAT")),
            forecast_lon=_number(env("FORECAST_LON")),
        )
        if not (cfg.ecowitt or cfg.airgradient):
            raise SystemExit("Set ECOWITT_API_KEY + ECOWITT_APP_KEY and/or AIRGRADIENT_API_TOKEN + AIRGRADIENT_LOCATION_ID")
        return cfg
