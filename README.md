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
- [Upgrading](#upgrading)
  - [systemd](#systemd-1)
  - [Docker](#docker-1)
- [Configuration](#configuration)

## Features

- **Ask in plain words:** "how hot is it?", "will it rain?", "how many days was UV over 9 this year?"
- **Charts on request:** any reading, with 1D, 7D, 1M, 3M and 1Y buttons to change the period, or two readings on one chart with a scale each.
- **One-tap report:** `/report` shows weather, air quality, pollen and the forecast in one message, under gauges of the temperature, humidity and rain. `/dashboard` draws the station's gauges with the day's, month's and year's highs and lows.
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
TELEGRAM_BOT_TOKEN=

# Your Telegram user id (ask @userinfobot), so the bot answers you
ADMIN_CHAT_IDS=

# AI model (optional)
OPENROUTER_API_KEY=

# Weather station (optional)
ECOWITT_API_KEY=
ECOWITT_APP_KEY=

# AirGradient sensor (optional)
AIRGRADIENT_API_TOKEN=
AIRGRADIENT_LOCATION_ID=
```

### systemd

Needs Python 3.13.

```
sudo git clone https://github.com/robdevops/ecobot /opt/ecobot
```

```
sudo python3.13 -m venv /opt/ecobot/.venv
```

```
sudo /opt/ecobot/.venv/bin/pip install -r /opt/ecobot/requirements.txt
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

#### The weather service on its own (optional)

Not needed: without it the bot runs everything itself, as before. `ecobot-mcp.service` runs the weather service as its own program
(`python -m ecobot_service`, an MCP server at `http://127.0.0.1:8765/mcp`) for other MCP clients, with the bot as one of them. It uses the
bot's data folder, so back that up first: `sudo cp -a /var/lib/private/ecobot /var/lib/private/ecobot.bak`. Then:

1. Add to `/etc/ecobot.env` (one token, made with `openssl rand -hex 16`):
   ```
   MCP_SERVICE_TOKENS=<token>
   WEATHER_MCP_URL=http://127.0.0.1:8765/mcp
   WEATHER_MCP_TOKEN=<token>
   ```
2. Restart the bot, now the service's client (it waits for the service): `sudo systemctl restart ecobot`
3. Start the service: `sudo cp /opt/ecobot/ecobot-mcp.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now ecobot-mcp`
4. Check that both run as the same user and the folder's owner is unchanged (the three numbers match):
   `systemctl show -p UID --value ecobot ecobot-mcp; stat -c %u /var/lib/private/ecobot`

Logs: `journalctl -fu ecobot-mcp`. To go back: `sudo systemctl disable --now ecobot-mcp`, remove the three lines, `sudo systemctl restart ecobot`.
Only one process runs the service on the data folder: started beside a bot without `WEATHER_MCP_URL`, it stops, saying so in its log.

Then message `/report` to your Telegram bot.

The service runs sandboxed as its own user, and keeps its data in `/var/lib/private/ecobot`.

### Docker

```
sudo mkdir -p /var/lib/private/ecobot && sudo chown -R 10001:10001 /var/lib/private/ecobot
```

```
sudo git clone https://github.com/robdevops/ecobot /opt/ecobot
```

```
cd /opt/ecobot
```

```
sudo docker compose up -d --build
```

Watch the logs:

```
sudo docker logs -tf ecobot
```

Then message `/report` to your Telegram bot.

Docker uses the same data folder as the systemd service, so you can switch between them, one at a time. To go back to systemd: `sudo docker rm -f ecobot && sudo systemctl start ecobot`.

#### The weather service on its own in Docker (optional)

Not needed: without it the bot runs everything itself, as before. The `ecobot-service` container runs the weather service from the same
image (an MCP server for other clients, with the bot as one of them) on the bot's data folder, so back that up first:
`sudo cp -a /var/lib/private/ecobot /var/lib/private/ecobot.bak`. Then:

1. Add to `/etc/ecobot.env` (one token, made with `openssl rand -hex 16`), and the `WEATHER_MCP_URL` line that matches your network:
   ```
   MCP_SERVICE_TOKENS=<token>
   WEATHER_MCP_TOKEN=<token>
   # Docker's own network (the default):
   WEATHER_MCP_URL=http://ecobot-service:8765/mcp
   MCP_SERVICE_ALLOWED_HOSTS=127.0.0.1,localhost,ecobot-service
   # or, with compose.host-network.yaml in COMPOSE_FILE:
   WEATHER_MCP_URL=http://127.0.0.1:8765/mcp
   ```
2. Recreate the bot, now the service's client (it waits for the service): `sudo docker compose up -d --force-recreate ecobot`
3. Start the service: `sudo docker compose --profile service up -d ecobot-service`
4. Watch both: `sudo docker logs -tf ecobot-service` should show `MCP: http://.../mcp`, and `sudo docker logs -tf ecobot` with
   `Weather service: connected`. Then message `/report` to the bot.

From then on, `sudo docker compose --profile service up -d --build` updates both. To go back: `sudo docker compose --profile service down`
(it stops both), remove the lines from step 1, then `sudo docker compose up -d`.
Only one process runs the service on the data folder: started beside a bot without `WEATHER_MCP_URL`, it stops, saying so in its log.

#### Docker logging

Logs go to Docker's own rotated log; to send them to the journal instead (kept across upgrades, read with `journalctl -f CONTAINER_NAME=ecobot`):

```
echo "COMPOSE_FILE=compose.yaml:compose.journald.yaml" | sudo tee /opt/ecobot/.env
```

The bot writes warnings and errors to stderr and the rest to stdout, which Docker files in the journal at the priorities err and info, so `journalctl -p warning CONTAINER_NAME=ecobot` shows the warnings and errors alone.

To use both this and [Docker host network](#docker-networking), set `COMPOSE_FILE=compose.yaml:compose.host-network.yaml:compose.journald.yaml`.

#### Docker networking

`.local` names (mDNS) can't be found from Docker's private network: use IP addresses for config values, or reconfigure the container for host network (then run the `docker compose` command again):

```
echo "COMPOSE_FILE=compose.yaml:compose.host-network.yaml" | sudo tee /opt/ecobot/.env
```

## Upgrading

Your keys in `/etc/ecobot.env` and the bot's data (`/var/lib/private/ecobot`) are kept. The log's first line, `Starting ecobot <commit>`, says which version is running.

The weather station's and the air sensor's history lives in one file, `ecobot.sqlite`. A version from before 29 October 2026 kept it in `ecowitt_cache.sqlite`, the two files beside it, `airgradient_cache.sqlite` and `conditions_cache.sqlite`; the releases of that week moved them into the one file at start, so upgrade through one of those (commit 56a998c or earlier that week) if you still have the old files, then delete them.

### systemd

```
cd /opt/ecobot && sudo git pull
```

```
sudo /opt/ecobot/.venv/bin/pip install -r /opt/ecobot/requirements.txt
```

```
sudo systemctl restart ecobot
```

### Docker

```
cd /opt/ecobot && sudo git pull
```

```
sudo docker compose up -d --build
```

Compose replaces the container only if the image or the settings changed. To remove the old images and build cache that upgrades leave behind:

```
sudo docker image prune -f && sudo docker builder prune -f
```

## Configuration

Settings are environment variables in `/etc/ecobot.env`. The ones you're most likely to change:

| Variable | What it does |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Required. |
| `ADMIN_CHAT_IDS` | Telegram user ids allowed to use the bot, besides the admins of groups it is in. |
| `OPENROUTER_API_KEY` | Enables the AI model. Without it the bot still does reports, charts, lookups and alerts. |
| `ECOWITT_API_KEY`, `ECOWITT_APP_KEY` | Weather station through Ecowitt's cloud. |
| `HOMEASSISTANT_URL`, `HOMEASSISTANT_TOKEN` | Weather station through Home Assistant (instead of the cloud, or with it as a fallback; `ECOWITT_SOURCE`). |
| `AIRGRADIENT_API_TOKEN`, `AIRGRADIENT_LOCATION_ID` | Air sensor, via AirGradient's cloud. |
| `AIRGRADIENT_LOCAL_URL` | Read the AirGradient sensor directly on your network e.g. `http://airgradient_<serial>.local` (use a fixed IP address for the URL in Docker, or see [Docker networking](#docker-networking)) |
| `AIRGRADIENT_SOURCE` | Where the air sensor's readings come from: `auto` (the default) is each that is set, the sensor itself first, then Home Assistant (if it has the sensor), then the cloud, each filling only what the ones before it lack; `local`, `homeassistant` or `cloud` is that one alone. |
| `POLLEN`, `FORECAST` | `on` to add pollen and thunderstorm asthma, or the daily forecast. |
| `WEATHER_MCP_URL`, `WEATHER_MCP_TOKEN` | Optional. The bot as a client of the weather service running on its own (`python -m ecobot_service`, e.g. `http://127.0.0.1:8765/mcp`, and one of its `MCP_SERVICE_TOKENS`): the bot then fetches nothing and runs no alert rules itself, and needs none of the source settings above. Unset (the default), the bot runs everything itself, as before. See [DEVELOPMENT.md](DEVELOPMENT.md#the-bot-as-the-services-client). |

After editing the file, restart: `sudo systemctl restart ecobot`, or for Docker `sudo docker compose up -d --force-recreate` (`docker restart` keeps the old settings).

Every setting is in [docs/configuration.md](docs/configuration.md).

More: [what the bot does](docs/features.md), [how it works](docs/how-it-works.md), [developing](DEVELOPMENT.md).
