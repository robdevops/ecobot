# ecobot

A Telegram bot that answers questions about your Ecowitt weather station and AirGradient air-quality sensor, and sends you alerts when something changes.

- **Ask in plain words:** "how hot is it?", "will it rain?", "how many days was UV over 9 this year?"
- **Charts on request:** any reading, with 1D, 7D, 1M, 3M and 1Y buttons to change the period.
- **Instant reports:** `/report` (or the Status button in a private chat) shows weather, air quality, pollen and forecast in one message.
- **Alerts:** rain, strong gusts, high UV, unhealthy air and temperature changes, silent unless you want sound.
- **Optional extras:** Melbourne pollen and thunderstorm asthma, a daily forecast, and a Tuya irrigation controller.
- **Cheap to run:** most answers are worked out in code, and the AI model is called only when it is needed.

## Install

Create a private file for your keys and fill it in:

```
sudo install -m 600 -o root -g root /dev/null /etc/ecobot.env
sudoedit /etc/ecobot.env
```

```
# Telegram: the token from BotFather
TELEGRAM_BOT_TOKEN=

# the AI model, for questions that need it (optional)
XAI_API_KEY=

# your weather station (optional)
ECOWITT_API_KEY=
ECOWITT_APP_KEY=

# your air sensor (optional)
AIRGRADIENT_API_TOKEN=
AIRGRADIENT_LOCATION_ID=
```

### Systemd method

You need Python 3.13, a Telegram bot token (from [@BotFather](https://t.me/BotFather)) and, optionally, an [xAI API key](https://x.ai/api) for the AI model.

```
sudo git clone https://github.com/robdevops/ecobot /opt/ecobot
sudo python3.13 -m venv /opt/ecobot/.venv
sudo /opt/ecobot/.venv/bin/pip install -r /opt/ecobot/requirements.txt
```

Then start it:

```
sudo cp /opt/ecobot/ecobot.service /etc/systemd/system/
sudo systemctl enable --now ecobot
```

Open your bot in Telegram and send `/report`. If it doesn't answer, [the log](#logging) shows why.

The unit runs the bot sandboxed as a dynamic user, with its state and caches in `/var/lib/private/ecobot`. The header of `ecobot.service` explains the install and how to move an older install's data there.

### Docker method

The `Dockerfile` builds a small python:3.13-slim image (98 MB compressed). It shares the service's data folder, so you can switch between the two with nothing to migrate, as long as only one runs at a time:

```
sudo systemctl stop ecobot
sudo mkdir -p /var/lib/private/ecobot && sudo chown -R 10001:10001 /var/lib/private/ecobot

sudo git clone https://github.com/robdevops/ecobot /opt/ecobot      # skip if you did the systemd method
cd /opt/ecobot
sudo docker build --build-arg COMMIT=$(sudo git rev-parse HEAD) -t ecobot .
sudo docker run -d --name ecobot --restart unless-stopped --env-file /etc/ecobot.env -v /var/lib/private/ecobot:/data \
  --log-driver $(test -S /run/systemd/journal/socket && echo journald || echo local) ecobot
```

The image runs as uid 10001, hence the `chown`; the folder is `/data` inside the container (its `DATA_DIR`). To read the air sensor by its `.local` name (`AIRGRADIENT_LOCAL_URL`), add `--network host` to `docker run`. Docker reads the env file as it is, so a value in quotes keeps its quotes, where systemd drops them. To go back: `sudo docker rm -f ecobot && sudo systemctl start ecobot`, which gives the folder back to the service.

## Logging

```
journalctl -fu ecobot     # systemd
docker logs -f ecobot     # Docker
```

Under Docker the log goes to the journal when there is one (`journalctl -f CONTAINER_NAME=ecobot` reads it too), else to Docker's `local` driver, which rotates its files on its own (20 MB, 5 files).

## More

- [What the bot does](docs/features.md): questions, charts, reports, alerts and who can use it
- [Configuration](docs/configuration.md): every setting
- [How it works](docs/how-it-works.md): code layout and design notes
- [Developing](DEVELOPMENT.md): tests and the GitHub checks
