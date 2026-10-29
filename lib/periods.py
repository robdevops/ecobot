"""The period buttons under a chart: 1D, 7D, 1M, 3M and 1Y (1, 7, 30, 90 and 365 days), always all five, the chart's own shaded (the blue of an alert toggle that is on). Pressing one redraws the chart
for that period (Bot.on_period_button); the question behind each chart is remembered for that."""

import time
from collections import OrderedDict
from dataclasses import dataclass

from telegram import InlineKeyboardMarkup

from . import intent
from telegram.constants import KeyboardButtonStyle

from .buttons import button

NAMES = {1: "1D", 7: "7D", 30: "1M", 90: "3M", 365: "1Y"}   # each period, in days, and what its button says
PERIODS = tuple(NAMES)
PREFIX = "pd:"
REMEMBERED = 300   # charts whose question is kept, newest first to stay


def keyboard(current: int | None = None) -> InlineKeyboardMarkup:
    """The one row of period buttons; the one for the chart's own period (`current`, in days) is shaded."""
    return InlineKeyboardMarkup([[button(NAMES[days], f"{PREFIX}{days}", KeyboardButtonStyle.PRIMARY if days == current else None)
                                  for days in PERIODS]])


def days_in(data: str | None) -> int | None:
    """The period a button's callback data asks for, if it is one of ours."""
    if not data or not data.startswith(PREFIX):
        return None
    days = data.removeprefix(PREFIX)
    return int(days) if days.isdigit() and int(days) in PERIODS else None


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

    def remember(self, chat_id: int, message_id: int, question: str):
        key = self._key(chat_id, message_id)
        self.store.pop(key, None)   # re-added at the end: the newest
        self.store[key] = question
        while len(self.store) > REMEMBERED:
            del self.store[next(iter(self.store))]
        if self.save:
            self.save()


@dataclass
class Drawn:
    """A chart in the ImageCache."""
    at: float                    # when it was drawn (the cache's clock)
    png: bytes
    title: str
    file_id: str | None = None   # Telegram's copy, once the chart has been sent: it is sent again by this, not uploaded
    caption: str | None = None   # the answer's text (kept in the chat's history), when it was made in code for exactly the key's words
    ttl: float = 10 * 60         # how long it is kept (ImageCache.CHART_TTL; PRELOAD_TTL for one uploaded ahead)
    newest: int | None = None    # the time of the newest reading it draws (epoch), when known: an alert about that reading can send it

    @property
    def photo(self) -> bytes | str:
        return self.file_id or self.png


class ImageCache:
    """The charts drawn lately, by question: toggling between the period buttons brings a chart back at once instead of drawing it
    again, and once it has been sent, as Telegram's copy instead of an upload. A chart is kept CHART_TTL seconds (the readings move
    slowly) and the newest CHART_IMAGES stay. A chart drawn in the last FRESH
    seconds is the answer to its question (ready)."""

    CHART_TTL = 10 * 60
    CHART_IMAGES = 60
    PRELOAD_TTL = 30 * 60   # a chart uploaded ahead (TELEGRAM_PRELOAD_CHAT) is used for this long, then drawn afresh
    FRESH = 330   # a chart made in code this lately answers the same words again as it is (the readings move slowly)

    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.items: OrderedDict = OrderedDict()

    @staticmethod
    def key(question: str, days: int, now) -> str:
        """The question with its period made explicit, so "Temperature chart 30d" and a button's "…30d" are the one chart."""
        return intent.with_period(question, days, now).lower()

    @classmethod
    def canonical(cls, question: str, now) -> str | None:
        """The key of a question that is its own key, word for word ("Temperature chart 7d", a button's): its chart is the same
        whoever asks it. None for any other ("temperature chart today" is kept under the 1d key, but it is another chart)."""
        key = cls.key(question, intent.period_days(question, now), now)
        return key if key == " ".join(question.split()).lower() else None

    def _kept(self, entry: Drawn | None) -> bool:
        """Is there a chart, and is it still within its time to live?"""
        return entry is not None and self.age(entry) <= entry.ttl

    def get(self, key: str) -> tuple[bytes | str, str] | None:
        """(the picture: Telegram's file id once it has been sent, else the PNG; its title), or None."""
        entry = self.items.get(key)
        if not self._kept(entry):
            self.items.pop(key, None)
            return None
        self.items.move_to_end(key)
        return entry.photo, entry.title

    def age_of(self, key: str) -> float | None:
        """How long ago this chart was drawn, if it is held."""
        entry = self.items.get(key)
        return self.age(entry) if self._kept(entry) else None

    def held(self, key: str | None) -> Drawn | None:
        """The chart kept under `key`, while it is within its time to live."""
        entry = self.items.get(key)
        return entry if self._kept(entry) else None

    def has_copy(self, key: str) -> bool:
        """Does Telegram hold this chart (drawn lately and already sent, so it has a file id)?"""
        entry = self.items.get(key)
        return self._kept(entry) and entry.file_id is not None

    def ready(self, question: str, now) -> Drawn | None:
        """The chart made in code for exactly these words in the last FRESH seconds (for someone's question, or uploaded ahead to
        TELEGRAM_PRELOAD_CHAT), with the text that went with it; None if there is none."""
        key = self.canonical(question, now)
        entry = self.items.get(key) if key else None
        if entry is None or entry.caption is None or self.age(entry) > self.FRESH:
            return None
        self.items.move_to_end(key)
        return entry

    def age(self, entry: Drawn) -> float:
        return self.clock() - entry.at

    def put(self, key: str, png: bytes, title: str, caption: str | None = None, ttl: float | None = None, newest: int | None = None):
        """caption: the answer's text, only for a chart made in code for its canonical question (then it can be ready).
        newest: the time of the newest reading drawn, when known."""
        self.items[key] = Drawn(self.clock(), png, title, caption=caption, ttl=ttl or self.CHART_TTL, newest=newest)
        self.items.move_to_end(key)
        while len(self.items) > self.CHART_IMAGES:
            self.items.popitem(last=False)

    def sent(self, key: str | None, message):
        """`message` (as Telegram returned it) carries the chart kept under `key`: later sends of it use Telegram's copy."""
        if (entry := self.items.get(key)) is not None and (sizes := getattr(message, "photo", None)):
            entry.file_id = sizes[-1].file_id   # the largest size: the picture as it was sent
