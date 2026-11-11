"""The 🔄 under the full report ("status"): it reads everything again and redraws the message in place (Bot.on_report_button). Any
other button of the family (the 🔄 under the Telegram dashboard, which is no more) is out of date."""

from telegram import InlineKeyboardMarkup

from .buttons import button

PREFIX = "rp:"
REFRESH = f"{PREFIX}refresh"


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[button("🔄", REFRESH)]])
