"""python -m ecobot_service: the old name of ecobot-mcp (python -m ecobot_mcp), kept so that an ecobot-mcp.service copied into
/etc/systemd/system before the rename keeps starting until the new unit is copied in (README, Upgrading > systemd). That old unit
drops the bot's secrets only by their old names (UnsetEnvironment=), so this drops them by their new ones too before ecobot-mcp
starts, and logs a warning saying what to do."""

import logging
import os

# The bot's secrets by their new names, which the old unit's UnsetEnvironment= doesn't list (the new unit lists them itself)
BOT_SECRETS = ("BOT_TELEGRAM_TOKEN", "BOT_OPENROUTER_API_KEY", "BOT_TUYA_CLIENT_ID", "BOT_TUYA_CLIENT_SECRET")


def drop_bot_secrets(environ=os.environ) -> None:
    for name in BOT_SECRETS:
        environ.pop(name, None)


def run():
    drop_bot_secrets()
    from ecobot_mcp.__main__ import run as run_mcp
    logging.getLogger("ecobot.mcp").warning(
        "Started as python -m ecobot_service, ecobot-mcp's old name: copy the new unit in "
        "(sudo cp /opt/ecobot/ecobot-mcp.service /etc/systemd/system/ && sudo systemctl daemon-reload && "
        "sudo systemctl restart ecobot-mcp), which runs python -m ecobot_mcp")
    run_mcp()


if __name__ == "__main__":
    run()
