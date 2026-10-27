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
- **One-tap report:** `/report` shows weather, air quality, pollen and the forecast in one message.
- **Alerts:** rain, strong gusts, high UV, unhealthy air and temperature changes. Silent unless you turn sound on.
- **Irrigation:** water or pause a Tuya controller from the chat, and have it paused automatically when rain has fallen or is forecast.
- **Cheap to run:** most answers are worked out by the bot itself, and the AI model is only called when it's needed.

## Install

You need a Telegram bot token from [@BotFather](https://t.me/BotFather), and optionally an [xAI API key](https://x.ai/api) for questions that need the AI model. Either way, start by putting your keys in one private file:

```
sudo install -m 600 -o root -g root /dev/null /etc/ecobot.env
sudoedit /etc/ecobot.env
```

```
# Telegram bot token from @BotFather
TELEGRAM_BOT_TOKEN=

# Your Telegram user id (ask @userinfobot), so the bot answers you
ADMIN_CHAT_IDS=

# AI model (optional)
XAI_API_KEY=

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
sudo python3.13 -m venv /opt/ecobot/.venv
sudo /opt/ecobot/.venv/bin/pip install -r /opt/ecobot/requirements.txt

sudo cp /opt/ecobot/ecobot.service /etc/systemd/system/
sudo systemctl enable --now ecobot
```

Then send `/report` to your bot. Logs: `journalctl -fu ecobot`.

The service runs sandboxed as its own user, and keeps its data in `/var/lib/private/ecobot`.

### Docker

```
sudo mkdir -p /var/lib/private/ecobot && sudo chown -R 10001:10001 /var/lib/private/ecobot

sudo git clone https://github.com/robdevops/ecobot /opt/ecobot
cd /opt/ecobot
sudo docker build --build-arg COMMIT=$(sudo git rev-parse HEAD) -t ecobot .
sudo docker run -d --name ecobot --restart unless-stopped --env-file /etc/ecobot.env -v /var/lib/private/ecobot:/data \
  --log-driver $(test -S /run/systemd/journal/socket && echo journald || echo local) ecobot
```

Then send `/report` to your bot. Logs: `docker logs -f ecobot`.

Docker uses the same data folder as the systemd service, so you can switch between them, one at a time. To go back to systemd: `sudo docker rm -f ecobot && sudo systemctl start ecobot`.

## Configuration

Settings are environment variables in `/etc/ecobot.env`. The ones you're most likely to change:

| Variable | What it does |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Required. |
| `ADMIN_CHAT_IDS` | Telegram user ids allowed to use the bot, besides the admins of groups it is in. |
| `XAI_API_KEY` | Enables the AI model. Without it the bot still does reports, charts, lookups and alerts. |
| `ECOWITT_API_KEY`, `ECOWITT_APP_KEY` | Weather station. |
| `AIRGRADIENT_API_TOKEN`, `AIRGRADIENT_LOCATION_ID` | Air sensor, via AirGradient's cloud. |
| `AIRGRADIENT_LOCAL_URL` | Read the AirGradient sensor directly on your network instead, e.g. `http://airgradient_<serial>.local` (.local mDNS requires `--network host` in `docker run`, or use a fixed IP address in `AIRGRADIENT_LOCAL_URL`). |
| `POLLEN`, `FORECAST` | `on` to add pollen and thunderstorm asthma, or the daily forecast. |

After editing the file, restart: `sudo systemctl restart ecobot` or `sudo docker restart ecobot`.

Every setting is in [docs/configuration.md](docs/configuration.md). More: [what the bot does](docs/features.md), [how it works](docs/how-it-works.md), [developing](DEVELOPMENT.md).
