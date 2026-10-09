"""Settings, all from environment variables (systemd loads them from the unit's EnvironmentFile)."""

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


def _tz() -> tzinfo:
    name = os.getenv("TZ", "").lstrip(":")
    return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo


def _on(value: str) -> bool:
    return value.lower() in ("on", "1", "true", "yes")


def _hours(value: str, default: tuple[int, int] | None) -> tuple[int, int] | None:
    """"0-6" -> (0, 6); "off" or "none" -> None; anything unreadable -> the default."""
    if value.lower() in ("off", "none", "no", "false"):
        return None
    try:
        start, end = (int(x) for x in value.split("-"))
    except ValueError:
        return default
    return (start, end) if 0 <= start <= 23 and 0 <= end <= 24 and start != end else default


def _hour(value: str, default: int) -> int:
    """"18" -> 18; anything unreadable or outside 0 to 23 -> the default."""
    number = _number(value)
    return int(number) if number is not None and 0 <= number <= 23 else default


def _flag(value: str, default: bool) -> bool:
    """A setting that is on or off, with a default when it is unset or empty."""
    return _on(value) if value else default


def _ids(value: str) -> frozenset[int]:
    """"123, -1001 456" -> {123, -1001, 456}. An entry that is not a whole number is ignored (and logged): it must not stop the bot."""
    found, bad = set(), []
    for part in filter(None, re.split(r"[,\s]+", value)):
        try:
            found.add(int(part))
        except ValueError:
            bad.append(part)
    if bad:
        log.warning("ADMIN_CHAT_IDS: ignoring %s (not whole numbers)", ", ".join(map(repr, bad)))
    return frozenset(found)


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
    conditions_cache_path: Path = ROOT / "conditions_cache.sqlite"   # the last pollen page and forecast, kept across restarts
    place: str = "Melbourne"             # PLACE: named after the Pollen & asthma and Forecast headings in the report
    pollen: bool = False                 # POLLEN=on: Melbourne grass pollen and thunderstorm asthma risk (a scraped website)
    pollen_district: str = "Central"     # the Victorian forecast district whose thunderstorm asthma risk is used
    forecast: bool = False               # FORECAST=on: the daily forecast from Open-Meteo
    forecast_lat: float | None = None    # FORECAST_LAT / FORECAST_LON; the weather station's own location when unset
    forecast_lon: float | None = None
    rain_quiet_hours: tuple[int, int] | None = (0, 6)   # RAIN_QUIET_HOURS="0-6" (local hours; "off" for none): no rain alerts, a summary after
    chart_all_feels_like: bool = False   # CHART_ALL_FEELS_LIKE=on: "weather all week" also draws the feels-like panel (off: it nearly repeats temperature)
    chart_all_vpd: bool = False          # CHART_ALL_VPD=on: "weather all week" also draws the vapour pressure deficit panel (off: it is temperature and humidity combined)
    tuya_device_id: str = ""             # TUYA_DEVICE_ID, TUYA_CLIENT_ID, TUYA_CLIENT_SECRET: all three switch the irrigation controller on
    tuya_client_id: str = ""
    tuya_client_secret: str = ""
    tuya_base_url: str = "https://openapi.tuyaeu.com"   # TUYA_BASE_URL: the data centre's address (EU by default)
    irrigation_check_hour: int = 18      # IRRIGATION_CHECK_HOUR: the local hour of the daily check (0 to 23)
    admin_only: bool = True              # ADMIN_ONLY (on by default): the bot answers nobody but the admins of the groups it is in
    admin_chat_ids: frozenset[int] = frozenset()   # ADMIN_CHAT_IDS: more ids allowed besides those admins (a user id, or a group id for its anonymous admins)
    alert_cooldown_minutes: int = 30     # ALERT_COOLDOWN_MINUTES: the rain and temperature-crossing alerts: dry this long before "the rain has stopped", and this long between alerts (5 to 150)

    @property
    def ecowitt(self) -> bool:
        return bool(self.ecowitt_api_key and self.ecowitt_app_key)

    @property
    def irrigation(self) -> bool:
        return bool(self.tuya_device_id and self.tuya_client_id and self.tuya_client_secret)

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
            place=env("PLACE", "Melbourne"),
            pollen=_on(env("POLLEN")),
            pollen_district=env("POLLEN_DISTRICT", "Central"),
            forecast=_on(env("FORECAST")),
            forecast_lat=_number(env("FORECAST_LAT")),
            forecast_lon=_number(env("FORECAST_LON")),
            rain_quiet_hours=_hours(env("RAIN_QUIET_HOURS"), (0, 6)),
            chart_all_feels_like=_on(env("CHART_ALL_FEELS_LIKE")),
            chart_all_vpd=_on(env("CHART_ALL_VPD")),
            tuya_device_id=env("TUYA_DEVICE_ID"),
            tuya_client_id=env("TUYA_CLIENT_ID"),
            tuya_client_secret=env("TUYA_CLIENT_SECRET"),
            tuya_base_url=env("TUYA_BASE_URL", "https://openapi.tuyaeu.com").rstrip("/"),
            irrigation_check_hour=_hour(env("IRRIGATION_CHECK_HOUR"), 18),
            admin_only=_flag(env("ADMIN_ONLY"), True),
            admin_chat_ids=_ids(env("ADMIN_CHAT_IDS")),
            alert_cooldown_minutes=int(min(150, max(5, _number(env("ALERT_COOLDOWN_MINUTES")) or 30))),
        )
        if not (cfg.ecowitt or cfg.airgradient):
            raise SystemExit("Set ECOWITT_API_KEY + ECOWITT_APP_KEY and/or AIRGRADIENT_API_TOKEN + AIRGRADIENT_LOCATION_ID")
        return cfg
