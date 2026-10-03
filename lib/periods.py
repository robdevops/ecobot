"""The period buttons under a chart: 7d, 30d, 90d and 365d, minus the one the chart already shows. Pressing one redraws the chart
for that period (Bot.on_period_button); the question behind each chart is remembered for that."""

from collections import OrderedDict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

PERIODS = (7, 30, 90, 365)
PREFIX = "pd:"
REMEMBERED = 300   # charts whose question is kept, newest first to stay


def keyboard(current: int | None) -> InlineKeyboardMarkup:
    """One row of the periods other than `current` (all four when the chart shows none of them, e.g. "this month")."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{days}d", callback_data=f"{PREFIX}{days}") for days in PERIODS if days != current]])


def days_in(data: str | None) -> int | None:
    """The period a button's callback data asks for, if it is one of ours."""
    if data and data.startswith(PREFIX) and data[len(PREFIX):].isdigit() and int(data[len(PREFIX):]) in PERIODS:
        return int(data[len(PREFIX):])
    return None


class Charted(OrderedDict):
    """{(chat_id, message_id): the question that drew that chart}, the newest REMEMBERED."""

    def remember(self, chat_id: int, message_id: int, question: str):
        self[(chat_id, message_id)] = question
        self.move_to_end((chat_id, message_id))
        while len(self) > REMEMBERED:
            self.popitem(last=False)
