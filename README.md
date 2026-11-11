# ecobot

A Telegram bot for your Ecowitt weather station and AirGradient air sensor. Ask it questions to pull charts, or receive alerts on certain triggers.

**Data sources** (each one is optional)
- AirGradient air quality, from the sensor on your network, from AirGradient's cloud, or both
- Ecowitt weather station
- Open-Meteo weather forecast
- Melbourne Pollen: pollen and thunderstorm asthma forecast

**Outputs**
- Tuya irrigation controllers

## Contents

- [Features](#features)
- [Install](#install)
  - [systemd](#systemd)
  - [Docker](#docker)
    - [Docker logging](#docker-logging)
    - [Docker networking](#docker-networking)
- [Web dashboard](#web-dashboard)
- [Upgrading](#upgrading)
  - [Renamed settings](#renamed-settings)
  - [systemd](#systemd-1)
  - [Docker](#docker-1)
- [Configuration](#configuration)

## Features

- **Ask in plain words:** "how hot is it?", "will it rain?", "how many days was UV over 9 this year?"
- **Charts on request:** any reading, with 1D, 7D, 1M, 3M and 1Y buttons to change the period, or two readings on one chart with a scale each.
- **One-tap report:** `/report` shows weather, air quality, pollen and the forecast in one message, under gauges of the temperature, humidity and rain.
- **Web dashboard:** the same gauges as a live page in any browser on your network, laid out to fill a phone, a tablet or a screen.
- **Alerts:** rain, strong gusts, high UV, unhealthy air and temperature changes. Silent unless you turn sound on.
- **Irrigation:** water or pause a Tuya controller from the chat, and have it paused automatically when rain has fallen or is forecast.
- **Cheap to run:** most answers are worked out by the bot itself, and the AI model is only called when it's needed.

## Install

Already installed? See [Upgrading](#upgrading).

You need a Telegram bot token from [@BotFather](https://t.me/BotFather), and optionally an [OpenRouter API key](https://openrouter.ai/keys) for questions that need the AI model. Either way, start by putting your keys in one private file:

```
sudo install -m 600 -o root -g root /dev/null /etc/ecobot.env
```

```
sudoedit /etc/ecobot.env
```

```
# Telegram bot token from @BotFather
BOT_TELEGRAM_TOKEN=

# Your Telegram user id (ask @userinfobot), so the bot answers you
BOT_ADMIN_CHAT_IDS=

# AI model (optional)
BOT_OPENROUTER_API_KEY=

# Weather station (optional)
MCP_ECOWITT_API_KEY=
MCP_ECOWITT_APP_KEY=

# AirGradient sensor (optional)
MCP_AIRGRADIENT_API_TOKEN=
MCP_AIRGRADIENT_LOCATION_ID=
```

Settings that start with `MCP_` are read by ecobot-mcp (the part that fetches the readings and runs the alert rules), the ones that
start with `BOT_` by the Telegram bot; the few without either, such as `TZ`, by both.

### systemd

Needs Python 3.13.

```
sudo git clone https://github.com/robdevops/ecobot /opt/ecobot
```

```
sudo python3.13 -m venv /opt/ecobot/.venv
```

```
sudo /opt/ecobot/.venv/bin/pip install -r /opt/ecobot/requirements.txt -r /opt/ecobot/requirements-charts.txt
```

```
sudo cp /opt/ecobot/ecobot.service /etc/systemd/system/ && sudo systemctl daemon-reload
```

```
sudo systemctl enable --now ecobot
```

Watch the logs:

```
journalctl -fu ecobot
```

Warnings and errors alone: `journalctl -p warning -u ecobot`.

#### ecobot-mcp on its own (optional)

Not needed: without it the bot runs everything itself, as before. `ecobot-mcp.service` runs ecobot-mcp as its own program
(`python -m ecobot_mcp`, an MCP server at `http://127.0.0.1:8765/mcp`) for other MCP clients, with the bot as one of them. It uses the
bot's data folder, so back that up first: `sudo cp -a /var/lib/private/ecobot /var/lib/private/ecobot.bak`. Then:

1. Add to `/etc/ecobot.env`:
   ```
   BOT_MCP_URL=http://127.0.0.1:8765/mcp
   ```
   No token is needed: at its first start ecobot-mcp makes one and keeps it in the data folder (`/var/lib/private/ecobot/auth_token`),
   where the bot reads it. To choose your own instead, set `AUTH_TOKENS=<token>` (both read it; the bot sends the first).
2. Restart the bot, now ecobot-mcp's client (it waits for it): `sudo systemctl restart ecobot`
3. Start ecobot-mcp: `sudo cp /opt/ecobot/ecobot-mcp.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now ecobot-mcp`
4. Check that both run as the same user and the folder's owner is unchanged (the three numbers match):
   `systemctl show -p UID --value ecobot ecobot-mcp; stat -c %u /var/lib/private/ecobot`

Logs: `journalctl -fu ecobot-mcp`. To go back: `sudo systemctl disable --now ecobot-mcp`, remove the `BOT_MCP_URL` line, `sudo systemctl restart ecobot`.
Only one process runs ecobot-mcp's part on the data folder: started beside a bot without `BOT_MCP_URL`, it stops, saying so in its log.
The token for [other MCP clients](#other-mcp-clients): `sudo cat /var/lib/private/ecobot/auth_token`.

Then message `/report` to your Telegram bot.

The bot runs sandboxed as its own user, and keeps its data in `/var/lib/private/ecobot`.

### Docker

Docker runs two containers, each from its own image built from this repository: `ecobot-mcp` (the readings, the alerts, and an MCP
server that the bot and other AI clients use) and `ecobot`, the Telegram bot, which is optional. No token is needed between them: at
its first start ecobot-mcp makes one and keeps it in the data folder, where the bot reads it.

```
sudo mkdir -p /var/lib/private/ecobot && sudo chown -R 10001:10001 /var/lib/private/ecobot
```

```
sudo git clone https://github.com/robdevops/ecobot /opt/ecobot
```

```
cd /opt/ecobot
```

To run the Telegram bot too (leave this out for ecobot-mcp alone):

```
echo "COMPOSE_PROFILES=bot" | sudo tee -a /opt/ecobot/.env
```

```
sudo docker compose up -d --build
```

Watch the logs: `sudo docker logs -tf ecobot-mcp` shows `MCP: http://...:8765/mcp`, and `sudo docker logs -tf ecobot` shows
`ecobot-mcp: connected`. Then message `/report` to your Telegram bot.

Docker uses the same data folder as the systemd units, so you can switch between them, one at a time. To go back to systemd:
`sudo docker compose --profile bot down && sudo systemctl start ecobot`. Only one process runs ecobot-mcp's part on the data folder:
started beside a bot that runs it itself, it stops, saying so in its log.

#### Other MCP clients

ecobot-mcp is published on port 8765 of every address your machine has, so other devices on your network (Claude Code on a laptop,
say) can use it with a token: the one it generated (`sudo cat /var/lib/private/ecobot/auth_token`), or, better, one each: set
`AUTH_TOKENS=<bot's token>,<laptop's token>` in `/etc/ecobot.env` (made with `openssl rand -hex 16`; the bot sends the first, and a
token taken out is refused from the next start). Add your machine's IP address or name, as they write it in the URL, to
`MCP_ALLOWED_HOSTS`, e.g. `http://192.168.1.5:8765/mcp` needs `192.168.1.5`. For Claude Code:
`claude mcp add --transport http ecobot http://192.168.1.5:8765/mcp --header "Authorization: Bearer <token>"`. It is plain HTTP: keep it
on your own network. Docker's published ports go around ufw's rules. To keep it to this machine only, change `8765:8765` in
`compose.yaml` to `127.0.0.1:8765:8765`. A client that shows pictures can have the charts as images: add `?charts=png` to its URL.

#### Charts in LibreChat (and other chat apps): `?charts=link`

Some clients (LibreChat, with an Ollama endpoint, say) don't show an image a tool sends, but do show a markdown image in the model's
reply. For those, add `?charts=link` to the MCP URL (`https://api.example.org/ecobot/mcp?charts=link`): each chart then comes as a
link to its picture, `/charts/<id>.png` on ecobot-mcp's port, and the model is asked to put it in its reply as a markdown image. Your
browser loads the picture, so the link has to be one it can reach: set `MCP_CHART_BASE_URL` to where your reverse proxy serves
`/charts`, e.g. with nginx

```
location /ecobot-charts/ { proxy_pass http://127.0.0.1:8765/charts/; }
```

and `MCP_CHART_BASE_URL=https://api.example.org/ecobot-charts`. Unset, the link is built from the address the MCP request came to
(the proxy's `X-Forwarded-Proto` and `X-Forwarded-Host` when it sends them) with `/charts`. The pictures need no token and answer any
`Host`: anyone who has a link can open it until it expires, so treat links as you would the chart itself. Each link's id is random
and unguessable; the pictures are kept in memory for `MCP_CHART_LINK_HOURS` (24 by default), at most 500 of them and 64 MB in all
(the oldest go first), and a restart of ecobot-mcp forgets them all (an old link then answers 404).

#### Docker logging

Logs go to Docker's own rotated log; to send them to the journal instead (kept across upgrades, read with `journalctl -f CONTAINER_NAME=ecobot-mcp` or `CONTAINER_NAME=ecobot`):

```
echo "COMPOSE_FILE=compose.yaml:compose.journald.yaml" | sudo tee -a /opt/ecobot/.env
```

The bot writes warnings and errors to stderr and the rest to stdout, which Docker files in the journal at the priorities err and info, so `journalctl -p warning CONTAINER_NAME=ecobot` shows the warnings and errors alone.

To use both this and [Docker host network](#docker-networking), set `COMPOSE_FILE=compose.yaml:compose.host-network.yaml:compose.journald.yaml`.

#### Docker networking

`.local` names (mDNS) can't be found from Docker's private network: use IP addresses for config values, or put both containers on the host's network (then run the `docker compose` command again; the bot then reaches ecobot-mcp at 127.0.0.1):

```
echo "COMPOSE_FILE=compose.yaml:compose.host-network.yaml" | sudo tee -a /opt/ecobot/.env
```

`/opt/ecobot/.env` holds one `COMPOSE_FILE=` line: to use more than one of these files, list them all in it, separated by `:`.

## Web dashboard

ecobot-mcp also serves the station's and the air sensor's gauges as a live web page at `/dashboard` on its port, e.g. `http://deb1:8765/dashboard`
(your machine's name or IP address). It shows every gauge with the day's, month's and year's highs and lows, drawn by the browser (plain HTML5, nothing fetched from
the internet), and fits every tile on the screen in portrait or landscape where they stay readable (on a phone it scrolls). It
updates by itself when a new reading arrives, and says "reconnecting…" while ecobot-mcp is away.

After the weather station's gauges come the air sensor's, when there is one, with their bands on US standards:

- **US AQI** (from PM2.5), on a 0-500 dial with the EPA's six categories and colours (Good 0-50 green, Moderate 51-100 yellow,
  Unhealthy for Sensitive Groups 101-150 orange, Unhealthy 151-200 red, Very Unhealthy 201-300 purple, Hazardous 301-500 maroon), the
  category's name under the number;
- **PM2.5** (µg/m³) on the EPA's 2024 breakpoints (9.0, 35.4, 55.4, 125.4, 225.4) and **PM10** on the EPA's (54, 154, 254, 354,
  424), in the same colours;
- **CO₂** (ppm): there is no EPA standard, so the usual US indoor guidance (from ASHRAE's): under 800 good, 800-1000 fair, 1000-1500
  poor, 1500-2000 bad, 2000 and over very bad;
- **VOC index** and **NOx index**, when the monitor reports them (Sensirion's indices, learnt from its own air: VOC 100 is its
  average, NOx 1 its baseline): VOC under 100 low, 100-150 average, then elevated, high (250) and very high (400); NOx up to 20 low,
  then elevated, high (150) and very high (300).

On the air dials every category gets an equal share of the dial (as AirNow's AQI dial draws them), so "good" and "moderate" aren't
squeezed into a sliver of a scale that runs to 500; the numbers round the dial are the breakpoints. PM1 has no US standard and the
monitor's own temperature and humidity repeat the station's outdoor ones, so they have no gauge. The US AQI and each category's name
are of the reading now, so they always agree with the needle and with the PM2.5 dial beside it (the alerts and the bot's words rate
PM2.5 on the last two readings, so a passing spike isn't called poor). The page's data never asks the air sensor: with its reading
stale it shows the one it holds.

A tile whose reading is missing just now (the station or the air sensor not sending it) is dimmed, its gauge at its floor (0, or the
scale's low end where 0 isn't on it: pressure 950 hPa, CO₂ 400 ppm) with its highs and lows still beside it; when the reading comes
back the tile brightens and its gauge rises in place. A reading the station or the monitor has never sent has no tile.

It is read-only (the weather and air readings, nothing else), so it is open to anyone on your network: no token, and any name or address
in the URL (`MCP_ALLOWED_HOSTS` is only for `/mcp`, which keeps its token). ecobot-mcp has to listen beyond the machine itself:
Docker publishes port 8765 already; under systemd set `MCP_BIND=0.0.0.0:8765`. It is plain HTTP: keep it on your own network. To
turn it off, set `MCP_DASHBOARD=off`.

## Upgrading

Your keys in `/etc/ecobot.env` and the bot's data (`/var/lib/private/ecobot`) are kept; settings with an old name still work
([Renamed settings](#renamed-settings)). The log's first line, `Starting ecobot <commit>`, says which version is running.

### Renamed settings

In November 2026 every setting got the name of the program that reads it: `MCP_` for ecobot-mcp's, `BOT_` for the bot's, and none
for the few both read. The old names still work: when a new name is unset and its old one is set, the old one is used, and the log says
once at start which to rename (never their values). Rename them in `/etc/ecobot.env` when convenient, by hand from the table, or with the script, which shows what it
would change (secrets masked) and changes the file only with `--write`, keeping a backup beside it:

```
sudo python3 /opt/ecobot/scripts/migrate_env.py /etc/ecobot.env
```

```
sudo python3 /opt/ecobot/scripts/migrate_env.py /etc/ecobot.env --write
```

Then restart ecobot-mcp and the bot. Beyond the renames it moves `WEATHER_MCP_TOKEN` into `AUTH_TOKENS` (first, the token the bot
sends) and changes the old container name `ecobot-service` in a URL or allowed host to `ecobot-mcp`; it stops, changing nothing, where
an old and a new name are both set to different values.


| Old name | New name |
|---|---|
| `ECOWITT_API_KEY`, `ECOWITT_APP_KEY`, `ECOWITT_SOURCE` | `MCP_ECOWITT_API_KEY`, `MCP_ECOWITT_APP_KEY`, `MCP_ECOWITT_SOURCE` |
| `AIRGRADIENT_API_TOKEN`, `AIRGRADIENT_LOCATION_ID`, `AIRGRADIENT_DASHBOARD_URL`, `AIRGRADIENT_LOCAL_URL`, `AIRGRADIENT_SOURCE` | the same with `MCP_` in front |
| `HOMEASSISTANT_URL`, `HOMEASSISTANT_TOKEN` | `MCP_HOMEASSISTANT_URL`, `MCP_HOMEASSISTANT_TOKEN` |
| `PLACE`, `POLLEN`, `POLLEN_DISTRICT`, `FORECAST`, `FORECAST_LAT`, `FORECAST_LON` | the same with `MCP_` in front |
| `RAIN_QUIET_HOURS`, `ALERT_COOLDOWN_MINUTES`, `ANTISPIKE_REQUIRE_CONSECUTIVE` | the same with `MCP_` in front |
| `CHART_ALL_FEELS_LIKE`, `CHART_ALL_VPD` | `MCP_CHART_ALL_FEELS_LIKE`, `MCP_CHART_ALL_VPD` |
| `MCP_SERVICE_BIND`, `MCP_SERVICE_ALLOWED_HOSTS` | `MCP_BIND`, `MCP_ALLOWED_HOSTS` |
| `MCP_SERVICE_TOKENS` | `AUTH_TOKENS` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_PRELOAD_CHAT` | `BOT_TELEGRAM_TOKEN`, `BOT_TELEGRAM_PRELOAD_CHAT` |
| `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL` | `BOT_OPENROUTER_API_KEY`, `BOT_OPENROUTER_BASE_URL`, `BOT_OPENROUTER_MODEL` |
| `ADMIN_ONLY`, `ADMIN_CHAT_IDS`, `LOG_WIDTH` | `BOT_ADMIN_ONLY`, `BOT_ADMIN_CHAT_IDS`, `BOT_LOG_WIDTH` |
| `TUYA_DEVICE_ID`, `TUYA_CLIENT_ID`, `TUYA_CLIENT_SECRET`, `TUYA_BASE_URL` | the same with `BOT_` in front |
| `IRRIGATION_CHECK_HOUR` | `BOT_IRRIGATION_CHECK_HOUR` |
| `WEATHER_MCP_URL` | `BOT_MCP_URL` |
| `WEATHER_MCP_TOKEN` | retired: the bot sends the first of `AUTH_TOKENS`. Still sent, with a warning, while `AUTH_TOKENS` is unset |

`TZ`, `DATA_DIR`, `LOG_LEVEL` and `DARKMODE` keep their names. In Docker, `docker/bot.env` sets `BOT_MCP_URL`, so an old
`WEATHER_MCP_URL` in `/etc/ecobot.env` no longer overrides it: rename it if you set one.

### Your data

The weather station's and the air sensor's history lives in one file, `ecobot.sqlite`. A version from before 29 October 2026 kept it in `ecowitt_cache.sqlite`, the two files beside it, `airgradient_cache.sqlite` and `conditions_cache.sqlite`; the releases of that week moved them into the one file at start, so upgrade through one of those (commit 56a998c or earlier that week) if you still have the old files, then delete them.

### systemd

```
cd /opt/ecobot && sudo git pull
```

```
sudo /opt/ecobot/.venv/bin/pip install -r /opt/ecobot/requirements.txt -r /opt/ecobot/requirements-charts.txt
```

```
sudo systemctl restart ecobot
```

If you run [ecobot-mcp on its own](#ecobot-mcp-on-its-own-optional), copy its unit in again too, then restart it (before November
2026 the unit ran `python -m ecobot_service`, ecobot-mcp's old name, and dropped the bot's secrets only by their old names):

```
sudo cp /opt/ecobot/ecobot-mcp.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart ecobot-mcp
```

An old unit not yet copied in still starts, with a warning in its log saying to copy the new one.

### Docker

```
cd /opt/ecobot && sudo git pull
```

```
sudo docker rm -f ecobot-service 2>/dev/null; sudo docker compose up -d --build
```

(The first part removes the container ecobot-mcp had before November 2026, `ecobot-service`, which would keep port 8765; once it's gone
it does nothing.)

Compose replaces a container only if its image or settings changed. To remove the old images and build cache that upgrades leave behind:

```
sudo docker image prune -f && sudo docker builder prune -f
```

**From the layout with a container named `ecobot-service` (November 2026):** that container is now `ecobot-mcp`, from an image of
its own (`ecobot-mcp`; the bot's is still `ecobot`). The command above removes the old container first, which would otherwise keep port 8765.

Your token lines still work (`MCP_SERVICE_TOKENS` is read as `AUTH_TOKENS`, and `WEATHER_MCP_TOKEN` is ignored once that is set, with
a warning): rename `MCP_SERVICE_TOKENS` to `AUTH_TOKENS` and delete `WEATHER_MCP_TOKEN`, or delete both to use the token ecobot-mcp
generates. See [Renamed settings](#renamed-settings).

**From a version before ecobot-mcp ran on its own (November 2026):** the bot ran everything itself; now `ecobot-mcp` does, and the
bot is its client, under the `bot` profile. Back up first (`sudo cp -a /var/lib/private/ecobot /var/lib/private/ecobot.bak`), remove
any `BOT_MCP_URL` (or `WEATHER_MCP_URL`) line from `/etc/ecobot.env` (Docker sets it), replace any `COMPOSE_PROFILES=service` in
`/opt/ecobot/.env` with `COMPOSE_PROFILES=bot` (or add it), then:

```
sudo docker compose --profile bot down && sudo docker compose up -d --build
```

## Configuration

Settings are environment variables in `/etc/ecobot.env`. The ones you're most likely to change:

| Variable | What it does |
|---|---|
| `BOT_TELEGRAM_TOKEN` | Required. |
| `BOT_ADMIN_CHAT_IDS` | Telegram user ids allowed to use the bot, besides the admins of groups it is in. |
| `BOT_OPENROUTER_API_KEY` | Enables the AI model. Without it the bot still does reports, charts, lookups and alerts. |
| `MCP_ECOWITT_API_KEY`, `MCP_ECOWITT_APP_KEY` | Weather station through Ecowitt's cloud. |
| `MCP_HOMEASSISTANT_URL`, `MCP_HOMEASSISTANT_TOKEN` | Weather station through Home Assistant (instead of the cloud, or with it as a fallback; `MCP_ECOWITT_SOURCE`). |
| `MCP_AIRGRADIENT_API_TOKEN`, `MCP_AIRGRADIENT_LOCATION_ID` | Air sensor, via AirGradient's cloud. |
| `MCP_AIRGRADIENT_LOCAL_URL` | Read the AirGradient sensor directly on your network e.g. `http://airgradient_<serial>.local` (use a fixed IP address for the URL in Docker, or see [Docker networking](#docker-networking)) |
| `MCP_AIRGRADIENT_SOURCE` | Where the air sensor's readings come from: `auto` (the default) is each that is set, the sensor itself first, then Home Assistant (if it has the sensor), then the cloud, each filling only what the ones before it lack; `local`, `homeassistant` or `cloud` is that one alone. |
| `MCP_POLLEN`, `MCP_FORECAST` | `on` to add pollen and thunderstorm asthma, or the daily forecast. |
| `BOT_MCP_URL` | Optional. The bot as a client of ecobot-mcp running on its own (`python -m ecobot_mcp`, e.g. `http://127.0.0.1:8765/mcp`): the bot then fetches nothing and runs no alert rules itself, and needs none of the `MCP_` settings above. Unset (the default), the bot runs everything itself, as before. Docker sets it. See [DEVELOPMENT.md](DEVELOPMENT.md#the-bot-as-ecobot-mcps-client). |
| `AUTH_TOKENS` | Optional. The tokens ecobot-mcp accepts (`token,token,...`, one per client); the bot sends the first. Unset, ecobot-mcp generates one at its first start and keeps it in the data folder (`auth_token`), where the bot reads it. |

After editing the file, restart: `sudo systemctl restart ecobot`, or for Docker `sudo docker compose up -d --force-recreate` (`docker restart` keeps the old settings).

Every setting is in [docs/configuration.md](docs/configuration.md).

More: [what the bot does](docs/features.md), [how it works](docs/how-it-works.md), [developing](DEVELOPMENT.md).
