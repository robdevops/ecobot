# Configuration

The bot is configured only through environment variables. They live in one file, `/etc/ecobot.env`, which the systemd unit (`EnvironmentFile=`) and Docker (`--env-file`) both read. Each data source switches on when its variables are set.

## The environment file

It holds secrets, so create it private to root before putting anything in it:

```
sudo install -m 600 -o root -g root /dev/null /etc/ecobot.env
sudoedit /etc/ecobot.env
```

One `NAME=value` per line, no `export`; a line starting with `#` is a comment (not text after a value). systemd reads it as root before it drops to the service's own user, so the service itself needs no access to the file, and nobody else on the machine can read your keys. To run the bot by hand from your own shell, load it with `set -a; . <(sudo cat /etc/ecobot.env); set +a`. (Docker reads the file as the person who runs `docker`, so use `sudo docker compose`, or give your user read access with `sudo chown root:youruser /etc/ecobot.env && sudo chmod 640 /etc/ecobot.env`. It also keeps quotes around a value that systemd would drop.)

## Settings

`on|off` values: `on yes true 1` / `off no false 0`. Any other value → default (logged). Blank → default.

### Required and model

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | – | Required. Bot token (@BotFather) |
| `OPENROUTER_API_KEY` | – | [OpenRouter](https://openrouter.ai/keys) API key. Unset → no model: `/report`, chart commands, plain lookups ("how hot is it?", "rain chart 7d", "forecast") and all alerts work; anything needing the model gets a "no model" reply |
| `OPENROUTER_MODEL` | `deepseek/deepseek-v4.1-flash-1017` | Model, by its OpenRouter name |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | API endpoint |

At least one of Ecowitt, AirGradient (cloud or local) must be configured.

### Sources

| Variable | Default | Description |
|---|---|---|
| `ECOWITT_API_KEY`, `ECOWITT_APP_KEY` | – | Enables Ecowitt through its cloud. Station's time zone is used |
| `ECOWITT_SOURCE` | `auto` | Where the station's readings come from. `auto`: Home Assistant if `HOMEASSISTANT_URL`/`TOKEN` are set, with Ecowitt's cloud (if its keys are set too) making up what it lacks; `cloud`: Ecowitt's cloud only; `homeassistant`: Home Assistant only (no Ecowitt keys needed) |
| `AIRGRADIENT_API_TOKEN`, `AIRGRADIENT_LOCATION_ID` | – | Enables AirGradient via cloud API: history and gap fill |
| `AIRGRADIENT_LOCAL_URL` | – | Sensor's local server, e.g. `http://airgradient_<serial>.local` or an IP. Live reading polled every 60 s, recorded as 5-min means (`pm02Compensated` over `pm02`). Cloud, if configured, only fills gaps. Without cloud vars: sensor + SQLite only, no cloud requests. `.local` resolved in-process (mDNS); in Docker needs the host network (README: Docker networking) or an IP. Sensor down → cloud fallback, retry after 5 min |
| `HOMEASSISTANT_URL`, `HOMEASSISTANT_TOKEN` | – | Home Assistant (e.g. `http://192.168.1.219:8123`) and a long-lived access token (your profile page there). With its Ecowitt and AirGradient integrations the bot reads the readings back to shade the recent charts between each 5 minutes' lowest and highest, for the days it was not watching too (the last 31 days at most; Home Assistant keeps 10 unless its recorder says otherwise). Both needed. They also make Home Assistant a source of the station's readings (see `ECOWITT_SOURCE`): minute lines, a live reading every few seconds and rain alerts as the gauge reports; without them the shading comes from the bot's own readings |
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
| `ADMIN_ONLY` | `off` | `on`: answer only admins/creators of groups the bot is in. Others get "only group admins" when addressed. `/alerts` is openable by anyone in a group (changes stay admin-only). Admin list cached 10 min |
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
| `TELEGRAM_PRELOAD_CHAT` | off | Chat id (negative: group/channel) to pre-upload charts to: the other periods of each chart asked for, and, while the bot has been used in the last 15 minutes, the keyboard's charts every 5 minutes. Bot must be able to post there. Copies used for 30 min. Refused upload → no retry until restart |

### Storage and logging

| Variable | Default | Description |
|---|---|---|
| `DATA_DIR` | `$STATE_DIRECTORY`, else app dir | State (`bot_state.json`) and SQLite caches. Relative paths from the working dir. Created if missing. Matplotlib cache in `matplotlib/` (or `MPLCONFIGDIR`) |
| `LOG_WIDTH` | `24` | Characters of a chat message kept in the log, then `…`; `0` keeps all. No other line is cut |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. `DEBUG` adds the bot's own lines (chart timings) |
