"""Settings, all from environment variables (systemd loads them from the unit's EnvironmentFile). Each is declared once, as
a field of Config: the variable it is read from, its default, and how its text is read."""

import logging
import os
import re
from dataclasses import dataclass, field, fields
from datetime import datetime, tzinfo
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


def data_dir() -> Path:
    """Where the state file and the SQLite caches live: DATA_DIR (a relative path is taken from the working directory), else the
    folder systemd gives a service that has StateDirectory= (STATE_DIRECTORY), else the app's own folder, where they have always been.
    Kept apart from the code so a container can mount it. Symlinks are left as written: /var/lib/ecobot, not the closed
    /var/lib/private/ecobot behind it."""
    value = os.getenv("DATA_DIR", "").strip() or os.getenv("STATE_DIRECTORY", "").split(":")[0].strip()
    return Path(os.path.abspath(Path(value).expanduser())) if value else ROOT


def local_tz() -> tzinfo:
    """The time zone when no weather station names its own (Ecowitt does, and it wins): TZ, else the system's, the zone
    /etc/localtime links to. Only a zone that has no name is a fixed offset, which daylight saving would put out."""
    name = os.getenv("TZ", "").lstrip(":")
    if name:
        try:
            return ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            log.warning("TZ: %r is not a time zone name; using the system's", name)
    linked = os.path.realpath("/etc/localtime")
    try:
        return ZoneInfo(linked.split("/zoneinfo/", 1)[1])
    except (IndexError, ValueError, ZoneInfoNotFoundError):
        return datetime.now().astimezone().tzinfo


ON, OFF = ("on", "yes", "true", "1"), ("off", "no", "false", "0")


# Each reader takes a setting's text (stripped; empty when unset or blank), its default and the variable's name.
def _flag(value: str, default: bool, name: str) -> bool:
    """An on/off setting: on, yes, true or 1; off, no, false or 0; unset or empty is the default. Any other value keeps the
    default too, with a warning, so a typo changes nothing."""
    if value.lower() in ON:
        return True
    if value.lower() in OFF:
        return False
    if value:
        log.warning("%s: %r is not on or off; keeping the default (%s)", name, value, "on" if default else "off")
    return default


def _required(value: str, default: str, name: str) -> str:
    """A setting the bot can't run without; it stops with a message naming it when it is unset or blank."""
    if value:
        return value
    raise SystemExit(f"{name} is not set: the bot can't start without it (see docs/configuration.md)")


def _url(value: str, default: str, name: str) -> str:
    return (value or default).rstrip("/")


SOURCES = ("auto", "cloud", "homeassistant")
AIR_SOURCES = ("auto", "local", "homeassistant", "cloud")


def _one_of(choices: tuple[str, ...], value: str, default: str, name: str) -> str:
    """One of `choices`, whatever its case; another value keeps the default, with a warning."""
    if value.lower() in choices:
        return value.lower()
    if value:
        log.warning("%s: %r is not %s; keeping %s", name, value, ", ".join(choices), default)
    return default


# Where the readings come from. The weather station's: auto (Home Assistant when it is set, Ecowitt's cloud as its fallback when that is set
# too, else whichever is), cloud, or homeassistant. The air monitor's: auto (each source that is set, the monitor itself first, then Home
# Assistant, then AirGradient's cloud), or one of local, homeassistant, cloud alone.
_source, _air_source = partial(_one_of, SOURCES), partial(_one_of, AIR_SOURCES)


def _hours(value: str, default: tuple[int, int] | None, name: str) -> tuple[int, int] | None:
    """"0-6" -> (0, 6); "off" or "none" -> None; anything unreadable -> the default."""
    if value.lower() in ("off", "none", "no", "false"):
        return None
    try:
        start, end = (int(x) for x in value.split("-"))
    except ValueError:
        return default
    return (start, end) if 0 <= start <= 23 and 0 <= end <= 24 and start != end else default


def _ids(value: str, default: frozenset[int], name: str) -> frozenset[int]:
    """"123, -1001 456" -> {123, -1001, 456}. An entry that is not a whole number is ignored (and logged): it must not stop the bot."""
    found, bad = set(), []
    for part in filter(None, re.split(r"[,\s]+", value)):
        try:
            found.add(int(part))
        except ValueError:
            bad.append(part)
    if bad:
        log.warning("%s: ignoring %s (not whole numbers)", name, ", ".join(map(repr, bad)))
    return frozenset(found) or default


def _whole(value: str, default: int, name: str) -> int:
    """"100" -> 100, "0" -> 0; anything but a whole number of 0 or more -> the default."""
    return int(value) if value.isdecimal() else default


def _between(low: int, high: int, clamp: bool = True):
    """A reader of a whole number from `low` to `high`; one outside is kept to them when `clamp` ("0" is `low`, like any number under it),
    else it is the default, as anything unreadable is."""
    def read(value: str, default: int, name: str) -> int:
        number = _number(value)
        if number is None or not clamp and not low <= number <= high:
            return default
        return int(min(high, max(low, number)))
    return read


_hour = _between(0, 23, clamp=False)   # "18" -> 18; anything unreadable or outside 0 to 23 -> the default


def _readings_in_a_row(value: str, default: int, name: str) -> int:
    """"3" -> 3; "off", "0" or "1" -> 1 (no rule); anything unreadable -> the default."""
    if value.lower() in ("off", "no", "false", "none"):
        return 1
    return max(1, int(value)) if value.isdecimal() else default


def _chat_id(value: str, default: int | None, name: str) -> int | None:
    """A Telegram chat id (negative for a group or channel); None, with a warning, when it is not a whole number."""
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        log.warning("%s: ignoring %r (not a chat id, a whole number)", name, value)
        return None


def _number(value: str, default: float | None = None, name: str = "") -> float | None:
    """"6.5" -> 6.5; unset or blank -> the default; unreadable -> None."""
    try:
        return float(value) if value else default
    except ValueError:
        return None


LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _level(value: str, default: str, name: str) -> str:
    """One of LEVELS, whatever its case; another value keeps the default, with a warning."""
    if value.upper() in LEVELS:
        return value.upper()
    if value:
        log.warning("%s: %r is not %s or %s; keeping %s", name, value, ", ".join(LEVELS[:-1]), LEVELS[-1], default)
    return default


def _setting(name: str, default=None, read=None):
    """A Config field read from the environment variable `name` by `read` (a reader above; none: the text, blank being the default)."""
    return field(default=default, metadata={"env": name, "read": read})


def _from_env(f) -> object:
    text = os.getenv(f.metadata["env"], "").strip()
    return f.metadata["read"](text, f.default, f.metadata["env"]) if f.metadata["read"] else text or f.default


@dataclass(frozen=True)
class Config:
    tz: tzinfo
    state_path: Path
    storage_path: Path                                             # the bot's one file of readings, the pollen page and the forecast (lib/storage)
    telegram_token: str = _setting("TELEGRAM_BOT_TOKEN", "", _required)
    openrouter_api_key: str = _setting("OPENROUTER_API_KEY", "")                   # unset: the bot runs without the model, answering only what it works out in code
    openrouter_base_url: str = _setting("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    openrouter_model: str = _setting("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash-1017")
    ecowitt_api_key: str = _setting("ECOWITT_API_KEY", "")
    ecowitt_app_key: str = _setting("ECOWITT_APP_KEY", "")
    airgradient_token: str = _setting("AIRGRADIENT_API_TOKEN", "")
    airgradient_location: str = _setting("AIRGRADIENT_LOCATION_ID", "")
    airgradient_dashboard: str = _setting("AIRGRADIENT_DASHBOARD_URL", "")
    airgradient_local_url: str = _setting("AIRGRADIENT_LOCAL_URL", "")   # the sensor's own server for the live reading, e.g. http://airgradient_<serial>.local
    airgradient_source: str = _setting("AIRGRADIENT_SOURCE", "auto", _air_source)   # auto: the monitor itself, then Home Assistant, then the cloud, each that is set; or one alone
    homeassistant_url: str = _setting("HOMEASSISTANT_URL", "", _url)       # Home Assistant, where the station's readings are read from (its Ecowitt integration), e.g. http://192.168.1.219:8123
    homeassistant_token: str = _setting("HOMEASSISTANT_TOKEN", "")         # a long-lived access token (your profile page in Home Assistant)
    ecowitt_source: str = _setting("ECOWITT_SOURCE", "auto", _source)      # auto: Home Assistant if set, Ecowitt's cloud as its fallback if set too; cloud: the cloud only; homeassistant: Home Assistant only
    place: str = _setting("PLACE", "Melbourne")                     # named after the Pollen & asthma and Forecast headings in the report
    pollen: bool = _setting("POLLEN", False, _flag)                 # Melbourne grass pollen and thunderstorm asthma risk (a scraped website)
    pollen_district: str = _setting("POLLEN_DISTRICT", "Central")   # the Victorian forecast district whose thunderstorm asthma risk is used
    forecast: bool = _setting("FORECAST", False, _flag)             # the daily forecast from Open-Meteo
    forecast_lat: float | None = _setting("FORECAST_LAT", None, _number)   # the weather station's own location when unset
    forecast_lon: float | None = _setting("FORECAST_LON", None, _number)
    rain_quiet_hours: tuple[int, int] | None = _setting("RAIN_QUIET_HOURS", (0, 6), _hours)   # "0-6", local hours ("off" for none): no rain alerts, a summary after
    chart_all_feels_like: bool = _setting("CHART_ALL_FEELS_LIKE", False, _flag)   # "weather all week" also draws the feels-like panel (off: it nearly repeats temperature)
    chart_all_vpd: bool = _setting("CHART_ALL_VPD", False, _flag)   # "weather all week" also draws the vapour pressure deficit panel (off: it is temperature and humidity combined)
    tuya_device_id: str = _setting("TUYA_DEVICE_ID", "")            # all three switch the irrigation controller on
    tuya_client_id: str = _setting("TUYA_CLIENT_ID", "")
    tuya_client_secret: str = _setting("TUYA_CLIENT_SECRET", "")
    tuya_base_url: str = _setting("TUYA_BASE_URL", "https://openapi.tuyaeu.com", _url)   # the data centre's address (EU by default)
    irrigation_check_hour: int = _setting("IRRIGATION_CHECK_HOUR", 18, _hour)   # the local hour of the daily check (0 to 23)
    admin_only: bool = _setting("ADMIN_ONLY", False, _flag)         # on: the bot answers nobody but the admins of the groups it is in (and ADMIN_CHAT_IDS)
    admin_chat_ids: frozenset[int] = _setting("ADMIN_CHAT_IDS", frozenset(), _ids)   # more ids allowed besides those admins (a user id, or a group id for its anonymous admins)
    alert_cooldown_minutes: int = _setting("ALERT_COOLDOWN_MINUTES", 60, _between(5, 150))   # the rain and temperature-crossing alerts: dry this long before "the rain has stopped", and this long between alerts
    telegram_preload_chat: int | None = _setting("TELEGRAM_PRELOAD_CHAT", None, _chat_id)   # a chat the other periods of each chart are silently uploaded to ahead of the press (none: off)
    antispike_require_consecutive: int = _setting("ANTISPIKE_REQUIRE_CONSECUTIVE", 2, _readings_in_a_row)   # readings in a row a rise of the particles needs to be the air, not a glitch of the sensor (1: off)
    log_width: int = _setting("LOG_WIDTH", 24, _whole)             # the most characters of a chat message kept in the log, then an ellipsis (…); 0 keeps all
    log_level: str = _setting("LOG_LEVEL", "INFO", _level)          # DEBUG adds the bot's own debug lines; the libraries keep theirs

    @property
    def homeassistant(self) -> bool:
        return bool(self.homeassistant_url and self.homeassistant_token)

    @property
    def ecowitt_cloud(self) -> bool:
        """The station's readings are asked of Ecowitt's cloud (its keys are set, and ECOWITT_SOURCE does not say Home Assistant only)."""
        return bool(self.ecowitt_api_key and self.ecowitt_app_key) and self.ecowitt_source != "homeassistant"

    @property
    def ecowitt_home_assistant(self) -> bool:
        """... of Home Assistant (it is set, and ECOWITT_SOURCE does not say the cloud only)."""
        return self.homeassistant and self.ecowitt_source != "cloud"

    @property
    def ecowitt(self) -> bool:
        """The weather station is on: from Home Assistant, Ecowitt's cloud, or both (the cloud then making up what Home Assistant lacks)."""
        return self.ecowitt_cloud or self.ecowitt_home_assistant

    @property
    def irrigation(self) -> bool:
        return bool(self.tuya_device_id and self.tuya_client_id and self.tuya_client_secret)

    @property
    def airgradient_cloud(self) -> bool:
        """The air monitor's readings are asked of AirGradient's cloud (its token and location are set, and AIRGRADIENT_SOURCE allows)."""
        return bool(self.airgradient_token and self.airgradient_location) and self.airgradient_source in ("auto", "cloud")

    @property
    def airgradient_local(self) -> bool:
        """... of the monitor itself (its address is set, and AIRGRADIENT_SOURCE allows)."""
        return bool(self.airgradient_local_url) and self.airgradient_source in ("auto", "local")

    @property
    def airgradient_homeassistant(self) -> bool:
        """... of Home Assistant (it is set, and AIRGRADIENT_SOURCE allows; whether it has the monitor is found at start)."""
        return self.homeassistant and self.airgradient_source in ("auto", "homeassistant")

    @property
    def airgradient(self) -> bool:
        """AirGradient is on with any of its sources: the monitor's own server, Home Assistant, or its cloud account."""
        return self.airgradient_cloud or self.airgradient_local or self.airgradient_homeassistant

    @classmethod
    def from_env(cls) -> "Config":
        """Every setting from its variable, the files in the data folder. Stops, saying why, when one the bot can't run without is
        missing or the folder can't be written."""
        data = data_dir()
        try:
            data.mkdir(parents=True, exist_ok=True)
            writable = os.access(data, os.W_OK | os.X_OK)
        except OSError:
            writable = False
        if not writable:
            raise SystemExit(f"{data} is not writable by this user: set DATA_DIR to a writable folder "
                             "(under systemd with StateDirectory=ecobot that is /var/lib/ecobot)")
        cfg = cls(tz=local_tz(), state_path=data / "bot_state.json", storage_path=data / "ecobot.sqlite",
                  **{f.name: _from_env(f) for f in fields(cls) if "env" in f.metadata})
        if not (cfg.ecowitt or cfg.airgradient):
            raise SystemExit("Set ECOWITT_API_KEY + ECOWITT_APP_KEY and/or HOMEASSISTANT_URL + HOMEASSISTANT_TOKEN and/or AIRGRADIENT_API_TOKEN + "
                             "AIRGRADIENT_LOCATION_ID and/or AIRGRADIENT_LOCAL_URL")
        return cfg
