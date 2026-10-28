# ecobot

A Telegram bot for your Ecowitt weather station and AirGradient air sensor. Ask it questions to pull charts, or receive alerts on certain triggers.

**Data sources** (each one is optional)
- AirGradient air quality, from the sensor on your network, from AirGradient's cloud, or both
- Ecowitt weather station
- Open-Meteo weather forecast
- Melbourne Pollen: pollen and thunderstorm asthma forecast

**Outputs**
- Tuya irrigation controllers

## Features

- **Ask in plain words:** "how hot is it?", "will it rain?", "how many days was UV over 9 this year?"
- **Charts on request:** any reading, with 1D, 7D, 1M, 3M and 1Y buttons to change the period, or two readings on one chart with a scale each.
- **One-tap report:** `/report` shows weather, air quality, pollen and the forecast in one message, under gauges of the temperature, humidity and rain. `/dashboard` draws the station's gauges with the day's, month's and year's highs and lows.
- **Alerts:** rain, strong gusts, high UV, unhealthy air and temperature changes. Silent unless you turn sound on.
- **Irrigation:** water or pause a Tuya controller from the chat, and have it paused automatically when rain has fallen or is forecast.
- **Cheap to run:** most answers are worked out by the bot itself, and the AI model is only called when it's needed.

## Install

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
sudo cp /opt/ecobot/ecobot.service /etc/systemd/system/
```

```
sudo systemctl enable --now ecobot
```

Then send `/report` to your bot. Logs: `journalctl -fu ecobot`.

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

Then send `/report` to your bot. Logs, with timestamps:

```
sudo docker logs -tf ecobot
```

Docker keeps at most 5 files of 10 MB of them, compressed, so they do not fill the disk.

To upgrade, see [Upgrading](#upgrading).

#### Docker networking

By default the container is on a private bridge network, where `.local` names (mDNS) can't be found. Either use IP addresses in `HOMEASSISTANT_URL` and `AIRGRADIENT_LOCAL_URL` (reserve them on your router), or let the container use the host's network, where `.local` names work as they do for the systemd service. To do that, create `compose.override.yaml` next to `compose.yaml` (Compose reads it automatically, and `git pull` leaves it alone):

```
printf 'services:\n  ecobot:\n    network_mode: host\n' | sudo tee /opt/ecobot/compose.override.yaml
```

Then run the `docker compose up -d --build` command again. To go back, delete the file and run it again. (Removing the `#` from `# network_mode: host` in `compose.yaml` does the same, but the next `git pull` then has a local change to merge.)

Docker uses the same data folder as the systemd service, so you can switch between them, one at a time. To go back to systemd: `sudo docker rm -f ecobot && sudo systemctl start ecobot`.

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
| `AIRGRADIENT_LOCAL_URL` | Read the AirGradient sensor directly on your network e.g. `http://airgradient_<serial>.local`<br><br>Docker note: mDNS for .local requires `network_mode: host` under the service in `compose.yaml`. Alternatively use a fixed IP address e.g. `http://192.168.5.5` |
| `POLLEN`, `FORECAST` | `on` to add pollen and thunderstorm asthma, or the daily forecast. |

After editing the file, restart: `sudo systemctl restart ecobot`, or for Docker `sudo docker compose up -d --force-recreate` (`docker restart` keeps the old settings).

Every setting is in [docs/configuration.md](docs/configuration.md). More: [what the bot does](docs/features.md), [how it works](docs/how-it-works.md), [developing](DEVELOPMENT.md).

## Upgrading

Your keys in `/etc/ecobot.env` and the bot's data (`/var/lib/private/ecobot`) are kept. The log's first line, `Starting ecobot <commit>`, says which version is running.

### systemd

```
cd /opt/ecobot && sudo git pull
```

```
sudo /opt/ecobot/.venv/bin/pip install -r /opt/ecobot/requirements.txt
```

```
sudo cp /opt/ecobot/ecobot.service /etc/systemd/system/ && sudo systemctl daemon-reload
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
