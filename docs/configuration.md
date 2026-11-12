# Configuration

ecobot is configured only through environment variables. They live in one file, `/etc/ecobot.env`, which the systemd units (`EnvironmentFile=`) and Docker (`env_file:`) read. Each data source switches on when its variables are set.

Each setting is named for the program that reads it:

- `MCP_...`: read by **ecobot-mcp** (`python -m ecobot_mcp`: the sources, the alert rules, the MCP server) alone. The bot, its client, doesn't read them.
- `IRRIGATION_...`: read by **irrigation-mcp** (`python -m irrigation_mcp`: the irrigation controller's MCP server, its watchdog and daily check) alone. The bot, its client (`BOT_IRRIGATION_MCP_URL`), doesn't read them.
- `BOT_...`: read by the **bot** (`ecobot.py`: Telegram, the model) alone.
- No prefix: read by **all** of them.

Settings were renamed in November 2026 (README: [Renamed settings](../README.md#renamed-settings)), the irrigation controller's twice (`TUYA_...`, then `BOT_TUYA_...`, now `IRRIGATION_TUYA_...`). An old name still works when the new one is unset: it is used, and one warning at start names the new name (never the value). Where both are set, the new one wins and the old one is named as ignored (of two old names, the newer wins). Only the program that reads a setting reads its old names too: since step 8b the bot reads none of the irrigation controller's, by any name (irrigation-mcp does), so a bot left with `BOT_TUYA_...` and no `BOT_IRRIGATION_MCP_URL` has no Irrigation button and says so at start.

## The environment file

It holds secrets, so create it private to root before putting anything in it:

```
sudo install -m 600 -o root -g root /dev/null /etc/ecobot.env
sudoedit /etc/ecobot.env
```

One `NAME=value` per line, no `export`; a line starting with `#` is a comment (not text after a value). systemd reads it as root before it drops to the unit's own user, so the programs themselves need no access to the file, and nobody else on the machine can read your keys. ecobot-mcp's unit drops the bot's secrets (`UnsetEnvironment=`). To run the bot by hand from your own shell, load it with `set -a; . <(sudo cat /etc/ecobot.env); set +a`. (Docker reads the file as the person who runs `docker`, so use `sudo docker compose`, or give your user read access with `sudo chown root:youruser /etc/ecobot.env && sudo chmod 640 /etc/ecobot.env`. It also keeps quotes around a value that systemd would drop.)

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
| `MCP_HOMEASSISTANT_URL`, `MCP_HOMEASSISTANT_TOKEN` | – | Home Assistant (e.g. `http://192.168.1.219:8123`) and a long-lived access token (your profile page there). With its Ecowitt and AirGradient integrations the readings are read back to shade the recent charts between each 5 minutes' lowest and highest, for the days ecobot was not watching too (the last 31 days at most; Home Assistant keeps 10 unless its recorder says otherwise). Both needed. They also make Home Assistant a source of the station's readings (see `MCP_ECOWITT_SOURCE`): minute lines, a live reading every few seconds and rain alerts as the gauge reports; without them the shading comes from ecobot's own readings |
| `MCP_AIRGRADIENT_DASHBOARD_URL` | – | "live chart" link in air replies and alerts |
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
| `MCP_DASHBOARD` | `on` | The gauges' live web page at `/dashboard` beside `/mcp` (`http://deb1:8765/dashboard`): read-only, so it needs no token and answers any `Host` (open it by the machine's name or address from the LAN); `/mcp` keeps both checks. `off`: `/dashboard`, `/dashboard/data` and `/dashboard/events` answer 404 |

Every request to `/mcp` needs a token (`AUTH_TOKENS`, below). Chart images are not a setting of ecobot-mcp but of each client: a client whose MCP URL ends `?charts=png` (`http://127.0.0.1:8765/mcp?charts=png`) gets each chart as a PNG beside the figures, drawn in `DARKMODE`'s theme; a client whose URL ends `?charts=link` gets each chart as a link to its PNG (`/charts/<id>.png`, served with no token and any `Host`, so anyone with the link can open it until it expires; `MCP_CHART_BASE_URL`, `MCP_CHART_LINK_HOURS`) and the model is asked to put it in its reply as a markdown image, for a chat app that shows those but not a tool's image (LibreChat); a call's request `_meta` `chart_image` (`"png"`, `"link"` or `"none"`) overrides its connection's ([ecobot-mcp.md](ecobot-mcp.md) 3.7). They need Pillow (`requirements-charts.txt`, in both Docker images): installed from `requirements-mcp.txt` alone ecobot-mcp answers with the charts' JSON and a note, and warns once.

## The bot's settings (`BOT_`)

### Telegram and the model

| Variable | Default | Description |
|---|---|---|
| `BOT_TELEGRAM_TOKEN` | – | Required by the bot (not by ecobot-mcp). Bot token (@BotFather) |
| `BOT_OPENROUTER_API_KEY` | – | [OpenRouter](https://openrouter.ai/keys) API key. Everything typed, and ecobot-mcp's prompts as commands (`/report`, `/chart` ...), go to the model. Unset → no model: the buttons and their commands (the charts, Status's gauges) and all alerts work; a typed question gets one line saying the model isn't set up |
| `BOT_OPENROUTER_MODEL` | `deepseek/deepseek-v4.1-flash-1017` | Model, by its OpenRouter name |
| `BOT_OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | API endpoint |
| `BOT_TELEGRAM_PRELOAD_CHAT` | off | Chat id (negative: group/channel) to pre-upload charts to. After a chart is sent, its other periods are always fetched from ecobot-mcp in the background; with this set they are uploaded here too, so a period button sends Telegram's copy. Bot must be able to post there. Copies used for 30 min. Refused upload → no retry until restart |
| `BOT_LOG_WIDTH` | `24` | Characters of a chat message kept in the log, then `…`; `0` keeps all. No other line is cut |

### Access

| Variable | Default | Description |
|---|---|---|
| `BOT_ADMIN_ONLY` | `off` | `on`: answer only admins/creators of groups the bot is in. Others get "only group admins" when addressed. `/alerts` is openable by anyone in a group (changes stay admin-only). Admin list cached 10 min |
| `BOT_ADMIN_CHAT_IDS` | – | Extra allowed ids, comma/space separated. User id: allowed before any group exists. Group id (negative): adds its admins and anonymous admins |


### The bot as ecobot-mcp's client

| Variable | Default | Description |
|---|---|---|
| `BOT_MCP_URL` | required (Docker: `http://ecobot-mcp:8765/mcp`) | ecobot-mcp's endpoint (systemd: `http://127.0.0.1:8765/mcp`; or its TLS address behind a proxy). The bot is only ever ecobot-mcp's client ([ecobot-mcp.md](ecobot-mcp.md) steps 5 and 6b): it starts no sources, syncers or alert rules of its own and never opens the readings file, so the `MCP_` settings are ecobot-mcp's and the bot reads none of them; its tools, the server's instructions, the station (time zone, sensors, which alerts there are), the alerts and every picture come from ecobot-mcp. Unset: the bot stops at start, saying to run ecobot-mcp and set it |

The bot asks ecobot-mcp for the picture of every chart it sends, the model's answers' included (request `_meta` `chart_image: "png"`, in its own `DARKMODE` theme), and for the Status picture (`ecobot://status`), so its `BOT_MCP_URL` needs no `?charts=png`; it draws nothing itself.

While ecobot-mcp can't be reached the bot tells whoever asks so, and reconnects by itself (1, 2, 5, 10, then every 30 seconds); the
alerts sent meanwhile that are still news are delivered once it is back.

### The bot as irrigation-mcp's client

| Variable | Default | Description |
|---|---|---|
| `BOT_IRRIGATION_MCP_URL` | none: no irrigation in the bot | irrigation-mcp's endpoint (below): systemd `http://127.0.0.1:8766/mcp`, Docker `http://irrigation-mcp:8766/mcp` (Docker's `docker/bot.env` leaves it empty, so set it in `/etc/ecobot.env` when you run the `irrigation` profile; on the host's network, `compose.host-network.yaml`, it is `http://127.0.0.1:8766/mcp`). Set, the bot is irrigation-mcp's client as it is ecobot-mcp's (the same token): the keyboard's Irrigation button (in Wind's place), typing "irrigation" or "water", the irrigation menu and its buttons call irrigation-mcp's tools, and its alerts (`irrigation://alerts`: `irrigation_battery`, `irrigation_pause`) go to the chats subscribed to them as ecobot-mcp's do; the alert menus, `/usage` and Help offer them. While irrigation-mcp can't be reached the button says it couldn't reach the controller and the rest of the bot carries on; it reconnects by itself, and alerts that are still news are delivered once it is back. The bot runs no controller, watchdog or daily check itself and reads none of the `IRRIGATION_` settings (a Tuya setting left with this unset is a warning at start) |

(Before step 8b, November 2026, the bot ran the controller itself from the `BOT_TUYA_*` settings; `scripts/migrate_env.py` renames them
and adds `BOT_IRRIGATION_MCP_URL=http://127.0.0.1:8766/mcp` when they are there.)

## irrigation-mcp's settings (`IRRIGATION_`)

irrigation-mcp (`python -m irrigation_mcp`, [ecobot-mcp.md](ecobot-mcp.md) 3.9) is the irrigation controller's own MCP server: the tools `irrigation_status`, `water`, `stop` and `delay`, and `irrigation://alerts`; it runs the valve's watchdog and the daily check whatever any client does. It stops at start, saying which, without the three Tuya settings. Its state is `DATA_DIR/irrigation_state.json` (at its first start, the bot's irrigation entries in `bot_state.json` are copied over once), its alert log `DATA_DIR/irrigation_alerts.sqlite`; one irrigation-mcp runs per data folder (`irrigation.lock`).

| Variable | Default | Description |
|---|---|---|
| `IRRIGATION_TUYA_DEVICE_ID`, `IRRIGATION_TUYA_CLIENT_ID`, `IRRIGATION_TUYA_CLIENT_SECRET` | – (required) | The irrigation controller (Tuya cloud). Old names: `BOT_TUYA_...`, and before those `TUYA_...` |
| `IRRIGATION_TUYA_BASE_URL` | `https://openapi.tuyaeu.com` | Tuya data centre |
| `IRRIGATION_CHECK_HOUR` | `18` | Local hour of the daily check (0–23): a battery under 5% is an `irrigation_battery` alert; more than 1 mm of rain in the last 24 hours, or a 50% or better chance of at least 1 mm today or tomorrow, pauses the timer for 24 hours (an `irrigation_pause` alert), leaving a delay someone set alone. Old name: `BOT_IRRIGATION_CHECK_HOUR` |
| `IRRIGATION_BIND` | `127.0.0.1:8766` | host:port its MCP server listens on (`http://IRRIGATION_BIND/mcp`, Streamable HTTP; put TLS in front of it to reach it from elsewhere) |
| `IRRIGATION_ALLOWED_HOSTS` | `127.0.0.1`, `localhost` | The Host names it answers (any port unless one is given), beside its own hostname, as `MCP_ALLOWED_HOSTS` is ecobot-mcp's |
| `IRRIGATION_ECOBOT_MCP_URL` | `http://127.0.0.1:8765/mcp` (Docker: `http://ecobot-mcp:8765/mcp`) | ecobot-mcp, which the daily check asks for the rain measured (`summary`) and the forecast (`ecobot://forecast`), and whose station names the time zone. It sends the first of `AUTH_TOKENS`, else the generated token in the data folder. While ecobot-mcp can't be reached the check goes on the forecast it last read, without the measured rain |

It accepts the same tokens as ecobot-mcp: one of `AUTH_TOKENS`, or with that unset the one generated in the data folder they share. Under
systemd it runs from `irrigation-mcp.service` (optional; the bot's unit wants it only if it is installed), in Docker from the
`irrigation-mcp` container (the `irrigation` profile, port 8766, its `IRRIGATION_ECOBOT_MCP_URL` set by `docker/irrigation.env`: leave
a systemd value of it out of `/etc/ecobot.env`, or it points the container at itself; its log says so).

## Shared settings (no prefix)

| Variable | Default | Description |
|---|---|---|
| `AUTH_TOKENS` | generated | `token,token`: the bearer tokens ecobot-mcp accepts (each client sends one as `Authorization: Bearer <token>`), one per client so one can be revoked alone (take it out and restart); the bot sends the first, to ecobot-mcp and to irrigation-mcp, which accepts the same ones, and irrigation-mcp sends the first to ecobot-mcp. Compared in constant time; a token is never logged. Unset: ecobot-mcp generates one at its first start and keeps it in the data folder (`DATA_DIR/auth_token`, mode 0600, the same one every start after; its path is logged, never its value), and the bot, with `AUTH_TOKENS` unset too, reads it from there (the same data folder: systemd's units and Docker's containers share it), warning when it can't. For another client: `sudo cat /var/lib/private/ecobot/auth_token`. (Replaces `MCP_SERVICE_TOKENS` and the bot's `WEATHER_MCP_TOKEN`; the latter, if set, is still sent while `AUTH_TOKENS` is unset, with a warning to move it into `AUTH_TOKENS`, and is ignored, with a warning, once `AUTH_TOKENS` is set) |
| `TZ` | system zone | Only used without Ecowitt (the station's zone wins; a mismatch is logged). IANA name, e.g. `Australia/Melbourne`. With `BOT_MCP_URL`, ecobot-mcp's station's zone wins once read (and is kept for a start while ecobot-mcp is down); `TZ` only until it has ever been read |
| `DATA_DIR` | `$STATE_DIRECTORY`, else app dir | The bot's state (`bot_state.json`); ecobot-mcp's alert log, SQLite caches and generated token; irrigation-mcp's state (`irrigation_state.json`) and alert log (`irrigation_alerts.sqlite`). Relative paths from the working dir. Created if missing. Every program uses the same one |
| `DARKMODE` | `off` | The charts' colours. `off`: light; `on`: dark; a time range, e.g. `8pm-8am`, `20:00-8:00` or `20-8`: dark between those times in the station's time zone, light the rest of the day. The gauges (the Status picture, the web dashboard) are dark whatever this says. ecobot-mcp reads it for the chart images it sends a client that asks for them (a call may say `chart_dark`); the bot as its client asks for its own theme, so this is the bot's setting for its pictures either way |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. `DEBUG` adds the program's own lines (chart timings) |
