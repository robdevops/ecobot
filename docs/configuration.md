# Configuration

ecobot is configured only through environment variables. They live in one file, `/etc/ecobot.env`, which the systemd units (`EnvironmentFile=`) and Docker (`env_file:`) read. Each data source switches on when its variables are set.

Each setting is named for the program that reads it:

- `MCP_...`: read by **ecobot-mcp** (`python -m ecobot_mcp`: the sources, the alert rules, the MCP server) alone.
- `IRRIGATION_...`: read by **irrigation-mcp** (`python -m irrigation_mcp`: the irrigation controller's MCP server, its watchdog and daily check) alone.
- No prefix: read by **both**.

Settings were renamed in November 2026 (README: [Renamed settings](../README.md#renamed-settings)), the irrigation controller's twice (`TUYA_...`, then `BOT_TUYA_...`, now `IRRIGATION_TUYA_...`). An old name still works when the new one is unset: it is used, and one warning at start names the new name (never the value). Where both are set, the new one wins and the old one is named as ignored (of two old names, the newer wins). Only the program that reads a setting reads its old names too. The Telegram bot's settings (`BOT_...`, `TELEGRAM_...`, `OPENROUTER_...`, `ADMIN_...`, `LOG_WIDTH`, `WEATHER_MCP_URL`) are not read by anything now: leave them or delete them (`scripts/migrate_env.py` does, README: [Upgrading](../README.md#from-the-telegram-bot)).

## The environment file

It holds secrets, so create it private to root before putting anything in it:

```
sudo install -m 600 -o root -g root /dev/null /etc/ecobot.env
sudoedit /etc/ecobot.env
```

One `NAME=value` per line, no `export`; a line starting with `#` is a comment (not text after a value). systemd reads it as root before it drops to the unit's own user, so the programs themselves need no access to the file, and nobody else on the machine can read your keys. The units drop the secrets a program does not need (`UnsetEnvironment=`: irrigation-mcp's Tuya keys from ecobot-mcp, and the old Telegram bot's and model's keys from both). To run a server by hand from your own shell, load it with `set -a; . <(sudo cat /etc/ecobot.env); set +a`. (Docker reads the file as the person who runs `docker`, so use `sudo docker compose`, or give your user read access with `sudo chown root:youruser /etc/ecobot.env && sudo chmod 640 /etc/ecobot.env`. It also keeps quotes around a value that systemd would drop.)

`on|off` values: `on yes true 1` / `off no false 0`. Any other value → default (logged). Blank → default.

## ecobot-mcp's settings (`MCP_`)

At least one of Ecowitt (cloud or Home Assistant) or AirGradient (cloud, local or Home Assistant) must be configured.

### Sources

| Variable | Default | Description |
|---|---|---|
| `MCP_ECOWITT_API_KEY`, `MCP_ECOWITT_APP_KEY` | – | Enables Ecowitt through its cloud. Station's time zone is used |
| `MCP_ECOWITT_SOURCE` | `auto` | Where the station's readings come from. `auto`: Home Assistant if `MCP_HOMEASSISTANT_URL`/`TOKEN` are set, with Ecowitt's cloud (if its keys are set too) making up what it lacks; `cloud`: Ecowitt's cloud only; `homeassistant`: Home Assistant only (no Ecowitt keys needed) |
| `MCP_AIRGRADIENT_API_TOKEN`, `MCP_AIRGRADIENT_LOCATION_ID` | – | Enables AirGradient via cloud API: history and gap fill |
| `MCP_AIRGRADIENT_LOCAL_URL` | – | Sensor's local server, e.g. `http://airgradient_<serial>.local` or an IP. Live reading polled every 60 s, recorded as 5-min means (`pm02Compensated` over `pm02`). Cloud, if configured, only fills gaps. Without cloud vars: sensor + SQLite only, no cloud requests. `.local` resolved in-process (mDNS); in Docker needs the host network (README: Docker networking) or an IP. Sensor down → cloud fallback, retry after 5 min |
| `MCP_AIRGRADIENT_SOURCE` | `auto` | `auto`: each that is set, the sensor itself first, then Home Assistant (if it has the sensor), then the cloud; `local`, `homeassistant` or `cloud`: that one alone |
| `MCP_HOMEASSISTANT_URL`, `MCP_HOMEASSISTANT_TOKEN` | – | Home Assistant (e.g. `http://192.168.1.219:8123`) and a long-lived access token (your profile page there). With its Ecowitt and AirGradient integrations the readings are read back to shade the recent charts between each 5 minutes' lowest and highest, for the days ecobot was not watching too (the last 31 days at most; Home Assistant keeps 10 unless its recorder says otherwise). Both needed. They also make Home Assistant a source of the station's readings (see `MCP_ECOWITT_SOURCE`): minute lines, a live reading every 30 seconds and rain alerts as the gauge reports; without them the shading comes from ecobot's own readings |
| `MCP_AIRGRADIENT_DASHBOARD_URL` | – | "live dashboard" link in air replies and alerts |
| `MCP_PLACE` | `Melbourne` | Name in the Pollen & asthma and Forecast headings |
| `MCP_POLLEN` | `off` | Melbourne grass pollen + thunderstorm asthma (melbournepollen.com.au): report, `pollen_asthma` tool, alert at High. Oct–Dec only |
| `MCP_POLLEN_DISTRICT` | `Central` | East Gippsland, Mallee, North Central, North East, Northern Country, South West, West and South Gippsland, Wimmera. Case-insensitive; unknown → warning |
| `MCP_FORECAST` | `off` | Open-Meteo daily forecast: report, `weather_forecast` tool, forecast-change alert |
| `MCP_FORECAST_LAT`, `MCP_FORECAST_LON` | station's | Forecast location |

### Alerts

| Variable | Default | Description |
|---|---|---|
| `MCP_ALERT_COOLDOWN_MINUTES` | `60` (5–150) | Rain: dry time before "stopped" (rain that starts again sooner is the same spell; "started" is never held). Temperature crossing: minimum gap between alerts; a change inside the cooldown is held, then sent if still different |
| `MCP_RAIN_QUIET_HOURS` | `0-6` | Local hours with no rain alerts; summarised afterwards. `22-6` wraps midnight, `off` disables |
| `MCP_ANTISPIKE_REQUIRE_CONSECUTIVE` | `2` | Readings in a row a PM1/PM2.5/PM10 rise needs to count (spike ≥ 2.5× and +5 µg/m³ over neighbours otherwise dropped from charts, figures, unhealthy-air alert). `1` / `off` disables. Live "air now" unaffected |

### Charts

| Variable | Default | Description |
|---|---|---|
| `MCP_CHART_ALL_FEELS_LIKE` | `off` | Add feels-like panel to "weather all week" |
| `MCP_CHART_ALL_VPD` | `off` | Add VPD panel to "weather all week" |

### The MCP server

`python -m ecobot_mcp` ([ecobot-mcp.md](ecobot-mcp.md) 3.8, `DEVELOPMENT.md`):

| Variable | Default | Description |
|---|---|---|
| `MCP_BIND` | `127.0.0.1:8765` | `host:port` it listens on (`[::1]:8765` for IPv6; Docker sets `0.0.0.0:8765`). Serve it to other machines through TLS (a reverse proxy such as Caddy) |
| `MCP_ALLOWED_HOSTS` | none | More `Host` names it answers, besides `127.0.0.1`, `localhost` and its own hostname (in Docker, `ecobot-mcp`), any port unless one is given (`weather.lan`, `192.168.1.5:8765`); any other is refused (421), against DNS rebinding. Add the name or IP address a proxy or a LAN client uses |
| `MCP_CHART_BASE_URL` | none | Where people's browsers reach the chart links' pictures (`?charts=link`, below): the URL a reverse proxy serves `/charts` at, e.g. `https://api.example.org/ecobot-charts` with nginx's `location /ecobot-charts/ { proxy_pass http://127.0.0.1:8765/charts/; }`. Unset: the address the MCP request came to (`X-Forwarded-Proto` and `X-Forwarded-Host` when a proxy sends them) with `/charts` |
| `MCP_CHART_LINK_HOURS` | `24` | How long a chart link's picture answers (1 to 720 hours). They are kept in memory (at most 500, 64 MB in all, the oldest dropped first): a restart forgets them, and an old link answers 404 |
| `MCP_DASHBOARD` | `on` | The gauges' live web page at `/dashboard` beside `/mcp` (`http://deb1:8765/dashboard`): read-only, so it needs no token and answers any `Host` (open it by the machine's name or address from the LAN); `/mcp` keeps both checks. `off`: `/dashboard`, `/dashboard/`, `/dashboard/icon.png`, `/dashboard/data` and `/dashboard/events` answer 404 |
| `MCP_DASHBOARD_ANIMATIONS` | `on` | How much moves on the dashboard page, for every viewer. `on`: everything: the gauges' needles sweep to each new reading, the weather is drawn behind the gauges (pixel-art rain, haze, UV, humidity, wind, heat and ice while the readings call for them, something blown across in a gust over 99 km/h, all of it blowing the way the wind direction reading says: leftward for a wind from the east side (0 to 180 degrees), rightward from the west side (180 to 360), as it was for due north or south or no reading, easing over a few seconds when it turns (the wind's eddies swirl on undisturbed and a leaf in one veers round in a U, up then down on alternate turns, rather than reversing along its line) (the `?fx=wind-east` and `?fx=wind-west` test links), with a "stop" link stopping them in that page view until a reload), zooming a tile and the tiles rearranging (one moved or hidden) are animated. `low`: the gauges sweep and the tiles rearrange animated as with `on`, but no weather at all (nothing drawn, no "stop" link, no test links) and zooming happens at once. `off`: no weather, and nothing animated: the gauges' needles and readings jump straight to each new reading, zooming and rearranging happen at once. New readings are applied as they come in each. A browser asking for reduced motion gets `off`'s, whatever this is |
| `MCP_DASHBOARD_INDOOR` | `on` | The page's indoor tiles (Indoor temperature, Indoor humidity: every indoor reading's). `off` (privacy, as the page needs no token): they are left out, neither drawn nor sent: `/dashboard/data` carries no indoor reading at all, as if the station had no indoor sensor, and `/dashboard/events` sends no event for an indoor change alone. Only the dashboard: the MCP tools, the Status picture and the alerts still read and show them. A browser's saved arrangement naming the indoor tiles does no harm. Any other value keeps `on`, with a warning |

Every request to `/mcp` needs a token (`AUTH_TOKENS`, below). Chart images are not a setting of ecobot-mcp but of each client: a client whose MCP URL ends `?charts=png` (`http://127.0.0.1:8765/mcp?charts=png`) gets each chart as a PNG beside the figures, drawn in `DARKMODE`'s theme; a client whose URL ends `?charts=link` gets each chart as a link to its PNG (`/charts/<id>.png`, served with no token and any `Host`, so anyone with the link can open it until it expires; `MCP_CHART_BASE_URL`, `MCP_CHART_LINK_HOURS`) and the model is asked to put it in its reply as a markdown image, for a chat app that shows those but not a tool's image (LibreChat); a call's request `_meta` `chart_image` (`"png"`, `"link"` or `"none"`) overrides its connection's ([ecobot-mcp.md](ecobot-mcp.md) 3.7). They need Pillow (`requirements-charts.txt`, in both Docker images): installed from `requirements-mcp.txt` alone ecobot-mcp answers with the charts' JSON and a note, and warns once.

## irrigation-mcp's settings (`IRRIGATION_`)

irrigation-mcp (`python -m irrigation_mcp`, [ecobot-mcp.md](ecobot-mcp.md) 3.9) is the irrigation controller's own MCP server: the tools `irrigation_status`, `water`, `stop` and `delay`, and `irrigation://feed`; it runs the valve's watchdog and the daily check whatever any client does. It stops at start, saying which, without the three Tuya settings. Its state is `DATA_DIR/irrigation_state.json`, its alert log `DATA_DIR/irrigation_alerts.sqlite`; one irrigation-mcp runs per data folder (`irrigation.lock`).

| Variable | Default | Description |
|---|---|---|
| `IRRIGATION_TUYA_DEVICE_ID`, `IRRIGATION_TUYA_CLIENT_ID`, `IRRIGATION_TUYA_CLIENT_SECRET` | – (required) | The irrigation controller (Tuya cloud). Old names: `BOT_TUYA_...`, and before those `TUYA_...` |
| `IRRIGATION_TUYA_BASE_URL` | `https://openapi.tuyaeu.com` | Tuya data centre |
| `IRRIGATION_CHECK_HOUR` | `18` | Local hour of the daily check (0–23): a battery under 5% is an `irrigation_battery` alert; more than 1 mm of rain in the last 24 hours, or a 50% or better chance of at least 1 mm today or tomorrow, pauses the timer for 24 hours (an `irrigation_pause` alert), leaving a delay someone set alone. Old name: `BOT_IRRIGATION_CHECK_HOUR` |
| `IRRIGATION_BIND` | `127.0.0.1:8766` | host:port its MCP server listens on (`http://IRRIGATION_BIND/mcp`, Streamable HTTP; put TLS in front of it to reach it from elsewhere) |
| `IRRIGATION_ALLOWED_HOSTS` | `127.0.0.1`, `localhost` | The Host names it answers (any port unless one is given), beside its own hostname, as `MCP_ALLOWED_HOSTS` is ecobot-mcp's |
| `IRRIGATION_ECOBOT_MCP_URL` | `http://127.0.0.1:8765/mcp` (Docker: `http://ecobot-mcp:8765/mcp`) | ecobot-mcp, which the daily check asks for the rain measured (`summary`) and the forecast (`ecobot://forecast`), and whose station names the time zone. It sends the first of `AUTH_TOKENS`, else the generated token in the data folder. While ecobot-mcp can't be reached the check goes on the forecast it last read, without the measured rain |

It accepts the same tokens as ecobot-mcp: one of `AUTH_TOKENS`, or with that unset the one generated in the data folder they share. Under
systemd it runs from `irrigation-mcp.service` (optional), in Docker from the
`irrigation-mcp` container (the `irrigation` profile, port 8766, its `IRRIGATION_ECOBOT_MCP_URL` set by `docker/irrigation.env`: leave
a systemd value of it out of `/etc/ecobot.env`, or it points the container at itself; its log says so).

## Shared settings (no prefix)

| Variable | Default | Description |
|---|---|---|
| `AUTH_TOKENS` | generated | `token,token`: the bearer tokens ecobot-mcp accepts (each client sends one as `Authorization: Bearer <token>`), one per client so one can be revoked alone (take it out and restart). irrigation-mcp accepts the same ones, and sends the first to ecobot-mcp. Compared in constant time; a token is never logged. Unset: ecobot-mcp generates one at its first start and keeps it in the data folder (`DATA_DIR/auth_token`, mode 0600, the same one every start after; its path is logged, never its value), and irrigation-mcp, with `AUTH_TOKENS` unset too, reads it from there (the same data folder: systemd's units and Docker's containers share it), warning when it can't. For another client: `sudo cat /var/lib/private/ecobot/auth_token`. (Replaces `MCP_SERVICE_TOKENS`; the old `WEATHER_MCP_TOKEN` is not read) |
| `TZ` | system zone | Only used without Ecowitt (the station's zone wins; a mismatch is logged). IANA name, e.g. `Australia/Melbourne`. irrigation-mcp takes ecobot-mcp's station's zone once read (and keeps it for a start while ecobot-mcp is down); `TZ` only until it has ever been read |
| `DATA_DIR` | `$STATE_DIRECTORY`, else app dir | ecobot-mcp's alert log, SQLite caches and generated token; irrigation-mcp's state (`irrigation_state.json`) and alert log (`irrigation_alerts.sqlite`). An old `bot_state.json` here is not read. Relative paths from the working dir. Created if missing. Both programs use the same one |
| `DARKMODE` | `off` | The charts' colours. `off`: light; `on`: dark; a time range, e.g. `8pm-8am`, `20:00-8:00` or `20-8`: dark between those times in the station's time zone, light the rest of the day. The gauges (the Status picture, the web dashboard) are dark whatever this says. ecobot-mcp reads it for the chart images it sends a client that asks for them (a call may say `chart_dark`) |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. `DEBUG` adds the program's own lines (chart timings) |
