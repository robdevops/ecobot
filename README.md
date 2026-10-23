# ecobot

A Telegram bot that answers questions about your Ecowitt weather station and AirGradient air-quality sensor, and sends you alerts when something changes.

- **Ask in plain words:** "how hot is it?", "will it rain?", "how many days was UV over 9 this year?"
- **Charts on request:** any reading, with 1D, 7D, 1M, 3M and 1Y buttons to change the period.
- **Instant reports:** `/report` (or the Status button in a private chat) shows weather, air quality, pollen and forecast in one message.
- **Alerts:** rain, strong gusts, high UV, unhealthy air and temperature changes, silent unless you want sound.
- **Optional extras:** Melbourne pollen and thunderstorm asthma, a daily forecast, and a Tuya irrigation controller.
- **Cheap to run:** most answers are worked out in code, and the AI model is called only when it is needed.

## Quick start

You need Python 3.13, a Telegram bot token (from [@BotFather](https://t.me/BotFather)) and an [xAI API key](https://x.ai/api) for the AI model.

```
git clone https://github.com/robdevops/ecobot && cd ecobot
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt

# a private file for your keys
sudo install -m 600 -o root -g root /dev/null /etc/ecobot.env
sudoedit /etc/ecobot.env

set -a; . <(sudo cat /etc/ecobot.env); set +a
.venv/bin/python ecobot.py
```

`/etc/ecobot.env` holds one `NAME=value` per line:

```
# Telegram: the token from BotFather
TELEGRAM_BOT_TOKEN=

# the AI model, for questions that need it
XAI_API_KEY=

# your weather station (optional)
ECOWITT_API_KEY=
ECOWITT_APP_KEY=

# your air sensor (optional)
AIRGRADIENT_API_TOKEN=
AIRGRADIENT_LOCATION_ID=
```

Then open your bot in Telegram and send `/report`. The same file is what systemd and Docker read when you run the bot for real, so there is nothing to redo ([deployment](docs/deployment.md)).

## More

- [What the bot does](docs/features.md): questions, charts, reports, alerts and who can use it
- [Configuration](docs/configuration.md): every setting
- [Running it for real](docs/deployment.md): systemd and Docker
- [How it works](docs/how-it-works.md): code layout and design notes
- [Developing](DEVELOPMENT.md): tests and the GitHub checks
