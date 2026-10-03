"""The period buttons under a chart: Week, Month and Quarter (7, 30 and 90 days), always all three, the chart's own marked with ➤. Pressing one redraws the chart
for that period (Bot.on_period_button); the question behind each chart is remembered for that."""

from collections import OrderedDict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

PERIODS = (7, 30, 90)
NAMES = {7: "Week", 30: "Month", 90: "Quarter"}   # what the buttons say
PREFIX = "pd:"
REMEMBERED = 300   # charts whose question is kept, newest first to stay


def keyboard(current: int | None = None) -> InlineKeyboardMarkup:
    """The one row of period buttons; the one for the chart's own period (`current`, in days) is marked with ➤."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"\u27a4 {NAMES[days]}" if days == current else NAMES[days],
                                                       callback_data=f"{PREFIX}{days}") for days in PERIODS]])


def days_in(data: str | None) -> int | None:
    """The period a button's callback data asks for, if it is one of ours."""
    if data and data.startswith(PREFIX) and data[len(PREFIX):].isdigit() and int(data[len(PREFIX):]) in PERIODS:
        return int(data[len(PREFIX):])
    return None


class Charted:
    """{chat_id:message_id -> the question that drew that chart}, the newest REMEMBERED. With a `store` (a dict the alert state saves
    to its file) the questions outlive a restart, so a chart's buttons keep working."""

    def __init__(self, store: dict | None = None, save=None):
        self.store = store if store is not None else {}
        self.save = save

    @staticmethod
    def _key(chat_id: int, message_id: int) -> str:
        return f"{chat_id}:{message_id}"

    def get(self, chat_id: int, message_id: int) -> str | None:
        return self.store.get(self._key(chat_id, message_id))

    def __getitem__(self, ids: tuple[int, int]) -> str:
        return self.store[self._key(*ids)]

    def __contains__(self, ids: tuple[int, int]) -> bool:
        return self._key(*ids) in self.store

    def __len__(self) -> int:
        return len(self.store)

    def remember(self, chat_id: int, message_id: int, question: str):
        key = self._key(chat_id, message_id)
        self.store.pop(key, None)   # re-added at the end: the newest
        self.store[key] = question
        while len(self.store) > REMEMBERED:
            del self.store[next(iter(self.store))]
        if self.save:
            self.save()


class ImageCache:
    """The charts drawn lately, by question: toggling between the period buttons brings a chart back at once instead of drawing it
    again. A chart is kept CHART_TTL seconds (the readings move slowly) and the newest CHART_IMAGES stay."""

    CHART_TTL = 10 * 60
    CHART_IMAGES = 60

    def __init__(self, clock=None):
        import time
        self.clock = clock or time.monotonic
        self.items: OrderedDict = OrderedDict()

    @staticmethod
    def key(question: str, days: int, now) -> str:
        """The question with its period made explicit, so "Temperature chart 30d" and a button's "…30d" are the one chart."""
        from . import intent
        return intent.with_period(question, days, now).lower()

    def get(self, key: str) -> tuple[bytes, str] | None:
        entry = self.items.get(key)
        if entry is None or self.clock() - entry[0] > self.CHART_TTL:
            self.items.pop(key, None)
            return None
        self.items.move_to_end(key)
        return entry[1], entry[2]

    def put(self, key: str, png: bytes, title: str):
        self.items[key] = (self.clock(), png, title)
        self.items.move_to_end(key)
        while len(self.items) > self.CHART_IMAGES:
            self.items.popitem(last=False)
