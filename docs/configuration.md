# Configuration

The bot is configured only through environment variables. They live in one file, `/etc/ecobot.env`, which the systemd unit (`EnvironmentFile=`) and Docker (`--env-file`) both read. Each data source switches on when its variables are set.

## The environment file

It holds secrets, so create it private to root before putting anything in it:

```
sudo install -m 600 -o root -g root /dev/null /etc/ecobot.env
sudoedit /etc/ecobot.env
```

One `NAME=value` per line, no `export`; a line starting with `#` is a comment (not text after a value). systemd reads it as root before it drops to the service's own user, so the service itself needs no access to the file, and nobody else on the machine can read your keys. To run the bot by hand from your own shell, load it with `set -a; . <(sudo cat /etc/ecobot.env); set +a`. (Docker reads the file as the person who runs `docker`, so use `sudo docker run`, or give your user read access with `sudo chown root:youruser /etc/ecobot.env && sudo chmod 640 /etc/ecobot.env`. It also keeps quotes around a value that systemd would drop.)

## Settings

`on|off` values: `on yes true 1` / `off no false 0`. Any other value → default (logged). Blank → default.

### Required

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | – | Bot token (@BotFather) |
| `XAI_API_KEY` | – | xAI API key |
| `XAI_MODEL` | `grok-4.3` | Model |
| `XAI_BASE_URL` | `https://api.x.ai/v1` | API endpoint |

At least one of Ecowitt, AirGradient (cloud or local) must be configured.

### Sources

| Variable | Default | Description |
|---|---|---|
| `ECOWITT_API_KEY`, `ECOWITT_APP_KEY` | – | Enables Ecowitt. Station's time zone is used |
| `AIRGRADIENT_API_TOKEN`, `AIRGRADIENT_LOCATION_ID` | – | Enables AirGradient via cloud API: history and gap fill |
| `AIRGRADIENT_LOCAL_URL` | – | Sensor's local server, e.g. `http://airgradient_<serial>.local` or an IP. Live reading polled every 60 s, recorded as 5-min means (`pm02Compensated` over `pm02`). Cloud, if configured, only fills gaps. Without cloud vars: sensor + SQLite only, no cloud requests. `.local` resolved in-process (mDNS); in Docker needs `--network host` or an IP. Sensor down → cloud fallback, retry after 5 min |
| `AIRGRADIENT_DASHBOARD_URL` | – | "live chart" link in air replies and alerts |
| `TZ` | system zone | Only used without Ecowitt (the station's zone wins; a mismatch is logged). IANA name, e.g. `Australia/Melbourne` |
| `PLACE` | `Melbourne` | Name in the Pollen & asthma and Forecast headings |
| `POLLEN` | `off` | Melbourne grass pollen + thunderstorm asthma (melbournepollen.com.au): report, `pollen_asthma` tool, alert at High. Oct–Dec only |
| `POLLEN_DISTRICT` | `Central` | East Gippsland, Mallee, North Central, North East, Northern Country, South West, West and South Gippsland, Wimmera. Case-insensitive; unknown → warning |
| `FORECAST` | `off` | Open-Meteo daily forecast: report, `weather_forecast` tool, forecast-change alert |
| `FORECAST_LAT`, `FORECAST_LON` | station's | Forecast location |
| `TUYA_DEVICE_ID`, `TUYA_CLIENT_ID`, `TUYA_CLIENT_SECRET` | – | Enables the irrigation controller (Tuya cloud): Irrigation button, rain delay check, pause/battery alerts |
| `TUYA_BASE_URL` | `https://openapi.tuyaeu.com` | Tuya data centre |
| `IRRIGATION_CHECK_HOUR` | `18` | Local hour of the daily rain-delay check (0–23) |

### Access

| Variable | Default | Description |
|---|---|---|
| `ADMIN_ONLY` | `on` | Answer only admins/creators of groups the bot is in. Others get "only group admins" when addressed. `/alerts` is openable by anyone in a group (changes stay admin-only). Admin list cached 10 min |
| `ADMIN_CHAT_IDS` | – | Extra allowed ids, comma/space separated. User id: allowed before any group exists. Group id (negative): adds its admins and anonymous admins |

### Alerts

| Variable | Default | Description |
|---|---|---|
| `ALERT_COOLDOWN_MINUTES` | `60` (5–150) | Rain: dry time before "stopped". Rain and temperature crossing: minimum gap between alerts. A change inside the cooldown is held, then sent if still different |
| `RAIN_QUIET_HOURS` | `0-6` | Local hours with no rain alerts; summarised afterwards. `22-6` wraps midnight, `off` disables |
| `ANTISPIKE_REQUIRE_CONSECUTIVE` | `2` | Readings in a row a PM1/PM2.5/PM10 rise needs to count (spike ≥ 2.5× and +5 µg/m³ over neighbours otherwise dropped from charts, figures, unhealthy-air alert). `1` / `off` disables. Live "air now" unaffected |

### Charts

| Variable | Default | Description |
|---|---|---|
| `CHART_ALL_FEELS_LIKE` | `off` | Add feels-like panel to "weather all week" |
| `CHART_ALL_VPD` | `off` | Add VPD panel to "weather all week" |
| `TELEGRAM_PRELOAD_CHAT` | off | Chat id (negative: group/channel) to pre-upload the other periods of each chart to. Bot must be able to post there. Copies used for 30 min. Refused upload → no retry until restart |

### Storage and logging

| Variable | Default | Description |
|---|---|---|
| `DATA_DIR` | `$STATE_DIRECTORY`, else app dir | State (`bot_state.json`) and SQLite caches. Relative paths from the working dir. Created if missing. Matplotlib cache in `matplotlib/` (or `MPLCONFIGDIR`) |
| `LOG_WIDTH` | `71` | Max log line length, cut with `…`; `0` = no cut |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. `DEBUG` adds the bot's own lines (chart timings) |
