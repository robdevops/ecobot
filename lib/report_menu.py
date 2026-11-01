"""The 🔄 under the full report ("status") and under the dashboard: it reads everything again and redraws the message in place
(Bot.on_report_button)."""

from telegram import InlineKeyboardMarkup

from .buttons import button

PREFIX = "rp:"
REFRESH = f"{PREFIX}refresh"
DASHBOARD = f"{PREFIX}dashboard"


def keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[button("🔄", REFRESH)]])


def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[button("🔄", DASHBOARD)]])
