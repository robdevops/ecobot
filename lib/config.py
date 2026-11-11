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


REQUIRED_BY_BOT = ("telegram_token",)   # read only by the bot: ecobot-mcp (python -m ecobot_mcp) runs without them


def _tokens(value: str, default: tuple, name: str) -> tuple[str, ...]:
    """"s3cret, t0ken" -> ("s3cret", "t0ken"): the bearer tokens ecobot-mcp accepts, one per client, so one can be revoked alone (take
    it out and restart). Blank entries are skipped; a token given twice counts once."""
    return tuple(dict.fromkeys(filter(None, (p.strip() for p in value.split(","))))) or default


def _bind(value: str, default: tuple[str, int], name: str) -> tuple[str, int]:
    """"0.0.0.0:8765" -> ("0.0.0.0", 8765), "[::1]:8765" -> ("::1", 8765), "8765" -> (the default host, 8765); anything unreadable
    keeps the default, with a warning."""
    if not value:
        return default
    host, sep, port = value.rpartition(":")
    host = host.strip().strip("[]") if sep else default[0]
    if port.strip().isdecimal() and 0 < int(port) < 65536 and host:
        return host, int(port)
    log.warning("%s: %r is not host:port; keeping %s:%d", name, value, *default)
    return default


def _names(value: str, default: tuple[str, ...], name: str) -> tuple[str, ...]:
    """"ecobot.lan, 192.168.1.5:8765" -> ("ecobot.lan", "192.168.1.5:8765"); unset or empty is the default."""
    return tuple(filter(None, (p.strip() for p in value.split(",")))) or default


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
_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)?")   # "8pm", "20:00", "20", "8:30pm"


def _clock(text: str) -> int | None:
    """A time of day in minutes past midnight ("8pm" -> 1200, "20:00" -> 1200, "7" -> 420); None if unreadable."""
    match = _CLOCK.fullmatch(text.strip().lower())
    if not match:
        return None
    hour, minute, half = int(match[1]), int(match[2] or 0), match[3]
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour == 24 and minute == 0:
        hour = 0                      # "20-24": until midnight
    return hour * 60 + minute if hour <= 23 and minute <= 59 else None


def _dark_mode(value: str, default: str | tuple[int, int], name: str) -> str | tuple[int, int]:
    """The charts' theme: "off" (light), "on" (dark), or a time range dark ("8pm-8am", "20:00-8:00", "20-8") -> (1200, 480) in minutes
    past midnight; anything else keeps the default, with a warning."""
    if value.lower() in ("off", "on"):
        return value.lower()
    start, _, end = value.partition("-")
    span = (_clock(start), _clock(end)) if end else (None, None)
    if None not in span and span[0] != span[1]:
        return span
    if value:
        log.warning("%s: %r is not off, on, or a time range like 8pm-8am, 20:00-8:00 or 20-8; keeping %s", name, value, default)
    return default


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


def _setting(name: str, default=None, read=None, old: str | None = None):
    """A Config field read from the environment variable `name` by `read` (a reader above; none: the text, blank being the default).
    old: the name it had before (docs/configuration.md, "Renamed settings"), still read when `name` is unset, with a warning."""
    return field(default=default, metadata={"env": name, "read": read, "old": old})


def _text(f, renamed: list | None = None) -> tuple[str, str]:
    """The setting's text (stripped) and the variable it was read from: its own name, else its old one (noted in `renamed`)."""
    name, old = f.metadata["env"], f.metadata.get("old")
    text = os.getenv(name, "").strip()
    if not text and old and (text := os.getenv(old, "").strip()):
        if renamed is not None:
            renamed.append((old, name))
        return text, old
    if text and old and os.getenv(old, "").strip() and renamed is not None:
        renamed.append((old, None))   # both set: the old one is ignored
    return text, name


def _from_env(f, renamed: list | None = None) -> object:
    text, name = _text(f, renamed)
    return f.metadata["read"](text, f.default, name) if f.metadata["read"] else text or f.default


def warn_renamed(renamed: list[tuple[str, str | None]]):
    """One warning naming the old variables found and their new names (never their values)."""
    used = [f"{old} -> {new}" for old, new in renamed if new]
    ignored = [old for old, new in renamed if not new]
    if used:
        log.warning("Old setting names, still read for now: rename %s (docs/configuration.md, \"Renamed settings\")", ", ".join(used))
    if ignored:
        log.warning("Ignoring %s: the new name%s set", ", ".join(ignored), " is" if len(ignored) == 1 else "s are")


def env(name: str) -> str:
    """A setting's text by its name, or by its old one when that is all that is set (for the scripts, which read a few directly)."""
    for f in fields(Config):
        if f.metadata.get("env") == name:
            return _text(f)[0]
    return os.getenv(name, "").strip()


RETIRED_TOKEN = "WEATHER_MCP_TOKEN"   # the bot's token before AUTH_TOKENS: still sent when AUTH_TOKENS is unset, with a warning


@dataclass(frozen=True)
class Config:
    tz: tzinfo
    state_path: Path
    storage_path: Path                                             # the one file of readings, the pollen page and the forecast (lib/storage)
    # ecobot-mcp's (python -m ecobot_mcp): the sources, the alert rules and the MCP server. The bot reads them too when it runs all of
    # that in-process (BOT_MCP_URL unset)
    ecowitt_api_key: str = _setting("MCP_ECOWITT_API_KEY", "", old="ECOWITT_API_KEY")
    ecowitt_app_key: str = _setting("MCP_ECOWITT_APP_KEY", "", old="ECOWITT_APP_KEY")
    ecowitt_source: str = _setting("MCP_ECOWITT_SOURCE", "auto", _source, old="ECOWITT_SOURCE")   # auto: Home Assistant if set, Ecowitt's cloud as its fallback if set too; cloud: the cloud only; homeassistant: Home Assistant only
    airgradient_token: str = _setting("MCP_AIRGRADIENT_API_TOKEN", "", old="AIRGRADIENT_API_TOKEN")
    airgradient_location: str = _setting("MCP_AIRGRADIENT_LOCATION_ID", "", old="AIRGRADIENT_LOCATION_ID")
    airgradient_dashboard: str = _setting("MCP_AIRGRADIENT_DASHBOARD_URL", "", old="AIRGRADIENT_DASHBOARD_URL")
    airgradient_local_url: str = _setting("MCP_AIRGRADIENT_LOCAL_URL", "", old="AIRGRADIENT_LOCAL_URL")   # the sensor's own server for the live reading, e.g. http://airgradient_<serial>.local
    airgradient_source: str = _setting("MCP_AIRGRADIENT_SOURCE", "auto", _air_source, old="AIRGRADIENT_SOURCE")   # auto: the monitor itself, then Home Assistant, then the cloud, each that is set; or one alone
    homeassistant_url: str = _setting("MCP_HOMEASSISTANT_URL", "", _url, old="HOMEASSISTANT_URL")       # Home Assistant, where the station's readings are read from (its Ecowitt integration), e.g. http://192.168.1.219:8123
    homeassistant_token: str = _setting("MCP_HOMEASSISTANT_TOKEN", "", old="HOMEASSISTANT_TOKEN")         # a long-lived access token (your profile page in Home Assistant)
    place: str = _setting("MCP_PLACE", "Melbourne", old="PLACE")                     # named after the Pollen & asthma and Forecast headings in the report
    pollen: bool = _setting("MCP_POLLEN", False, _flag, old="POLLEN")                 # Melbourne grass pollen and thunderstorm asthma risk (a scraped website)
    pollen_district: str = _setting("MCP_POLLEN_DISTRICT", "Central", old="POLLEN_DISTRICT")   # the Victorian forecast district whose thunderstorm asthma risk is used
    forecast: bool = _setting("MCP_FORECAST", False, _flag, old="FORECAST")             # the daily forecast from Open-Meteo
    forecast_lat: float | None = _setting("MCP_FORECAST_LAT", None, _number, old="FORECAST_LAT")   # the weather station's own location when unset
    forecast_lon: float | None = _setting("MCP_FORECAST_LON", None, _number, old="FORECAST_LON")
    rain_quiet_hours: tuple[int, int] | None = _setting("MCP_RAIN_QUIET_HOURS", (0, 6), _hours, old="RAIN_QUIET_HOURS")   # "0-6", local hours ("off" for none): no rain alerts, a summary after
    alert_cooldown_minutes: int = _setting("MCP_ALERT_COOLDOWN_MINUTES", 60, _between(5, 150), old="ALERT_COOLDOWN_MINUTES")   # dry this long before "the rain has stopped"; and the temperature-crossing alert's minimum gap
    antispike_require_consecutive: int = _setting("MCP_ANTISPIKE_REQUIRE_CONSECUTIVE", 2, _readings_in_a_row, old="ANTISPIKE_REQUIRE_CONSECUTIVE")   # readings in a row a rise of the particles needs to be the air, not a glitch of the sensor (1: off)
    chart_all_feels_like: bool = _setting("MCP_CHART_ALL_FEELS_LIKE", False, _flag, old="CHART_ALL_FEELS_LIKE")   # "weather all week" also draws the feels-like panel (off: it nearly repeats temperature)
    chart_all_vpd: bool = _setting("MCP_CHART_ALL_VPD", False, _flag, old="CHART_ALL_VPD")   # "weather all week" also draws the vapour pressure deficit panel (off: it is temperature and humidity combined)
    mcp_bind: tuple[str, int] = _setting("MCP_BIND", ("127.0.0.1", 8765), _bind, old="MCP_SERVICE_BIND")    # host:port ecobot-mcp listens on
    mcp_allowed_hosts: tuple[str, ...] = _setting("MCP_ALLOWED_HOSTS", ("127.0.0.1", "localhost"), _names, old="MCP_SERVICE_ALLOWED_HOSTS")   # the Host names it answers (any port, unless one is given)
    # The bot's (ecobot.py)
    telegram_token: str = _setting("BOT_TELEGRAM_TOKEN", "", old="TELEGRAM_BOT_TOKEN")                       # (REQUIRED_BY_BOT: it stops without it)
    telegram_preload_chat: int | None = _setting("BOT_TELEGRAM_PRELOAD_CHAT", None, _chat_id, old="TELEGRAM_PRELOAD_CHAT")   # a chat the other periods of each chart are silently uploaded to ahead of the press (none: off)
    openrouter_api_key: str = _setting("BOT_OPENROUTER_API_KEY", "", old="OPENROUTER_API_KEY")                   # unset: the bot runs without the model, answering only what it works out in code
    openrouter_base_url: str = _setting("BOT_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1", old="OPENROUTER_BASE_URL")
    openrouter_model: str = _setting("BOT_OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash-1017", old="OPENROUTER_MODEL")
    admin_only: bool = _setting("BOT_ADMIN_ONLY", False, _flag, old="ADMIN_ONLY")         # on: the bot answers nobody but the admins of the groups it is in (and BOT_ADMIN_CHAT_IDS)
    admin_chat_ids: frozenset[int] = _setting("BOT_ADMIN_CHAT_IDS", frozenset(), _ids, old="ADMIN_CHAT_IDS")   # more ids allowed besides those admins (a user id, or a group id for its anonymous admins)
    log_width: int = _setting("BOT_LOG_WIDTH", 24, _whole, old="LOG_WIDTH")             # the most characters of a chat message kept in the log, then an ellipsis (…); 0 keeps all
    tuya_device_id: str = _setting("BOT_TUYA_DEVICE_ID", "", old="TUYA_DEVICE_ID")            # all three switch the irrigation controller on
    tuya_client_id: str = _setting("BOT_TUYA_CLIENT_ID", "", old="TUYA_CLIENT_ID")
    tuya_client_secret: str = _setting("BOT_TUYA_CLIENT_SECRET", "", old="TUYA_CLIENT_SECRET")
    tuya_base_url: str = _setting("BOT_TUYA_BASE_URL", "https://openapi.tuyaeu.com", _url, old="TUYA_BASE_URL")   # the data centre's address (EU by default)
    irrigation_check_hour: int = _setting("BOT_IRRIGATION_CHECK_HOUR", 18, _hour, old="IRRIGATION_CHECK_HOUR")   # the local hour of the daily check (0 to 23)
    mcp_url: str = _setting("BOT_MCP_URL", "", _url, old="WEATHER_MCP_URL")    # ecobot-mcp's endpoint, e.g. http://127.0.0.1:8765/mcp: set, the bot is its client and runs no sources, syncers or alert rules of its own; unset: all of that runs inside the bot
    retired_mcp_token: str = _setting(RETIRED_TOKEN, "")            # retired: sent only when AUTH_TOKENS is unset (ecobot.mcp_token)
    # Both read them
    dark_mode: str | tuple[int, int] = _setting("DARKMODE", "off", _dark_mode)   # off: light charts; on: dark; "8pm-8am" (or 20:00-8:00, 20-8): dark those hours in the station's time zone
    log_level: str = _setting("LOG_LEVEL", "INFO", _level)          # DEBUG adds the program's own debug lines; the libraries keep theirs
    auth_tokens: tuple[str, ...] = _setting("AUTH_TOKENS", (), _tokens, old="MCP_SERVICE_TOKENS")   # "token,token,...": ecobot-mcp accepts each, the bot sends the first; unset: the one ecobot-mcp generates in the data folder (lib/auth_token.py)

    @property
    def alert_state_path(self) -> Path:
        """The alert rules' own state (lib/alerts/store.py), beside the bot's."""
        return self.state_path.parent / "alert_state.json"

    @property
    def alert_log_path(self) -> Path:
        """The alert log (lib/alert_log.py), beside the bot's state."""
        return self.state_path.parent / "alerts.sqlite"

    @property
    def homeassistant(self) -> bool:
        return bool(self.homeassistant_url and self.homeassistant_token)

    @property
    def ecowitt_cloud(self) -> bool:
        """The station's readings are asked of Ecowitt's cloud (its keys are set, and MCP_ECOWITT_SOURCE does not say Home Assistant only)."""
        return bool(self.ecowitt_api_key and self.ecowitt_app_key) and self.ecowitt_source != "homeassistant"

    @property
    def ecowitt_home_assistant(self) -> bool:
        """... of Home Assistant (it is set, and MCP_ECOWITT_SOURCE does not say the cloud only)."""
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
        """The air monitor's readings are asked of AirGradient's cloud (its token and location are set, and MCP_AIRGRADIENT_SOURCE allows)."""
        return bool(self.airgradient_token and self.airgradient_location) and self.airgradient_source in ("auto", "cloud")

    @property
    def airgradient_local(self) -> bool:
        """... of the monitor itself (its address is set, and MCP_AIRGRADIENT_SOURCE allows)."""
        return bool(self.airgradient_local_url) and self.airgradient_source in ("auto", "local")

    @property
    def airgradient_homeassistant(self) -> bool:
        """... of Home Assistant (it is set, and MCP_AIRGRADIENT_SOURCE allows; whether it has the monitor is found at start)."""
        return self.homeassistant and self.airgradient_source in ("auto", "homeassistant")

    @property
    def airgradient(self) -> bool:
        """AirGradient is on with any of its sources: the monitor's own server, Home Assistant, or its cloud account."""
        return self.airgradient_cloud or self.airgradient_local or self.airgradient_homeassistant

    @classmethod
    def from_env(cls, bot: bool = True) -> "Config":
        """Every setting from its variable, the files in the data folder. Stops, saying why, when one the bot can't run without is
        missing or the folder can't be written. bot: False for ecobot-mcp (python -m ecobot_mcp), which has no Telegram token. Old
        names still read are named in one warning (never their values)."""
        data = data_dir()
        try:
            data.mkdir(parents=True, exist_ok=True)
            writable = os.access(data, os.W_OK | os.X_OK)
        except OSError:
            writable = False
        if not writable:
            raise SystemExit(f"{data} is not writable by this user: set DATA_DIR to a writable folder "
                             "(under systemd with StateDirectory=ecobot that is /var/lib/ecobot)")
        renamed: list = []
        cfg = cls(tz=local_tz(), state_path=data / "bot_state.json", storage_path=data / "ecobot.sqlite",
                  **{f.name: _from_env(f, renamed) for f in fields(cls) if "env" in f.metadata})
        warn_renamed(renamed)
        for f in fields(cls):
            if bot and f.name in REQUIRED_BY_BOT:
                _required(getattr(cfg, f.name), f.default, f.metadata["env"])
        if not (cfg.ecowitt or cfg.airgradient) and not (bot and cfg.mcp_url):   # (a client of ecobot-mcp needs no source)
            raise SystemExit("Set MCP_ECOWITT_API_KEY + MCP_ECOWITT_APP_KEY and/or MCP_HOMEASSISTANT_URL + MCP_HOMEASSISTANT_TOKEN and/or "
                             "MCP_AIRGRADIENT_API_TOKEN + MCP_AIRGRADIENT_LOCATION_ID and/or MCP_AIRGRADIENT_LOCAL_URL"
                             + ("" if not bot else ", or BOT_MCP_URL to use ecobot-mcp"))
        return cfg
