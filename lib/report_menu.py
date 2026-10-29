"""The button under the full report ("status"): Refresh, which reads everything again and redraws the report in place
(Bot.on_report_button)."""

from telegram import InlineKeyboardMarkup

from .buttons import button

PREFIX = "rp:"
REFRESH = f"{PREFIX}refresh"


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[button("🔄", REFRESH)]])
