"""The period buttons under a chart, and the charts drawn lately.

A chart the service drew carries `drawn_by` (docs/mcp-service.md 3.7): the call that drew it and the periods it is worth drawing over,
each with its label ("7d", "7D"). Its buttons are those periods, the chart's own shaded (the blue of an alert toggle that is on), and
none when it lists none; a button's data is the period ("pd:7d"), never the call, which Telegram's 64 bytes couldn't hold. Pressing one
replays the call with that period (Bot.on_period_button); what drew each chart is remembered for that (Charted).

A chart the model drew through the old tools whose call has no service equivalent keeps the old buttons, 1D / 7D / 1M / 3M / 1Y (1,
7, 30, 90 and 365 days, "pd:7"), which ask its question again with that period."""

import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, tzinfo

from telegram import InlineKeyboardMarkup
from telegram.constants import KeyboardButtonStyle

from . import charts
from .buttons import button

NAMES = {1: "1D", 7: "7D", 30: "1M", 90: "3M", 365: "1Y"}   # each period, in days, and what its button says (a question asked again)
PERIODS = tuple(NAMES)
PREFIX = "pd:"
REMEMBERED = 300   # charts whose call is kept, newest first to stay
WHOLE_DAY_TOOLS = ("days", "relate", "scan")   # the tools that read a period as whole days, up to yesterday


def keyboard(current: int | None = None) -> InlineKeyboardMarkup:
    """The old row of period buttons, for a question asked again; the one for the chart's own period (`current`, in days) is shaded."""
    return InlineKeyboardMarkup([[button(NAMES[days], f"{PREFIX}{days}", KeyboardButtonStyle.PRIMARY if days == current else None)
                                  for days in PERIODS]])


def days_in(data: str | None) -> int | None:
    """The period an old button's callback data asks for ("pd:7"), if it is one of ours."""
    if not data or not data.startswith(PREFIX):
        return None
    days = data.removeprefix(PREFIX)
    return int(days) if days.isdigit() and int(days) in PERIODS else None


def period_in(data: str | None) -> str | None:
    """The period a button under a service's chart asks for ("pd:7d" -> "7d")."""
    return data.removeprefix(PREFIX) if data and data.startswith(PREFIX) and data != PREFIX else None


def call_key(tool: str, arguments: dict) -> str:
    """A call as the picture cache keys it: the tool and its arguments as canonical JSON (sorted keys)."""
    return f"{tool} {json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(',', ':'))}"


def drawn_key(drawn_by: dict | None) -> str | None:
    """The key of the call a chart's drawn_by states."""
    return call_key(drawn_by["tool"], drawn_by["arguments"]) if drawn_by else None


def with_period(drawn_by: dict, period: str) -> dict:
    """The arguments of a chart's call with its period swapped for `period` (any start and end taken out)."""
    return {**{k: v for k, v in drawn_by["arguments"].items() if k not in ("period", "start", "end")}, "period": period}


def own_period(drawn_by: dict, now: datetime, tz: tzinfo) -> str | None:
    """Which of the chart's periods it was drawn over (its button is shaded): the one the service reads as the same span as its call's
    period; None when none is."""
    from .periods_text import read_period
    asked = (drawn_by.get("arguments") or {}).get("period")
    if not asked:
        return None
    whole = drawn_by.get("tool") in WHOLE_DAY_TOOLS

    def span(words: str):
        try:
            read = read_period(words, None, None, now=now, tz=tz, default=words, whole_days=whole, first_held=now.date())
        except Exception:   # (PeriodError, or words only a tool with the file can read)
            return None
        return read.start, read.end
    own = span(asked)
    for p in drawn_by.get("periods") or ():
        if p["period"] == asked or own is not None and span(p["period"]) == own:
            return p["period"]
    return None


def periods_keyboard(drawn_by: dict, current: str | None = None) -> InlineKeyboardMarkup | None:
    """One button for each period the chart's drawn_by lists, labelled as the service labels it, the chart's own (`current`) shaded;
    None when it lists none."""
    listed = drawn_by.get("periods") or []
    if not listed:
        return None
    return InlineKeyboardMarkup([[button(p["label"], f"{PREFIX}{p['period']}", KeyboardButtonStyle.PRIMARY if p["period"] == current else None)
                                  for p in listed]])


class Charted:
    """{chat_id:message_id -> what drew that chart}, the newest REMEMBERED: {"drawn_by": the service's call, "words": whether its
    caption carries the answer's text}, or the question (a string) for a chart whose call the service can't replay. With a `store` (a
    dict the alert state saves to its file) they outlive a restart, so a chart's buttons keep working."""

    def __init__(self, store: dict | None = None, save=None):
        self.store = store if store is not None else {}
        self.save = save

    @staticmethod
    def _key(chat_id: int, message_id: int) -> str:
        return f"{chat_id}:{message_id}"

    def get(self, chat_id: int, message_id: int) -> str | dict | None:
        return self.store.get(self._key(chat_id, message_id))

    def remember(self, chat_id: int, message_id: int, record: str | dict):
        key = self._key(chat_id, message_id)
        self.store.pop(key, None)   # re-added at the end: the newest
        self.store[key] = record
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
    caption: str | None = None   # the answer's text (kept in the chat's history), when it was made in code
    ttl: float = 10 * 60         # how long it is kept (ImageCache.CHART_TTL; PRELOAD_TTL for one uploaded ahead)
    newest: int | None = None    # the time of the newest reading it draws (epoch), when known: an alert about that reading can send it
    dark: bool = False           # drawn in the dark theme: kept only while the charts are (a DARKMODE time range turns at its ends)
    drawn_by: dict | None = None   # the call that drew it, as the service states it

    @property
    def photo(self) -> bytes | str:
        return self.file_id or self.png


class ImageCache:
    """The charts drawn lately, by the call that drew them (call_key: the tool and its arguments), so two wordings of one chart, or two
    chats asking for it, share one picture: toggling between the period buttons brings a chart back at once instead of drawing it
    again, and once it has been sent, as Telegram's copy instead of an upload. A chart is kept CHART_TTL seconds (the readings move
    slowly) and the newest CHART_IMAGES stay. A chart made in code in the last FRESH seconds is the answer to its call (ready). One
    chart may be kept under several keys (alias): the call a question was read as, and the call the service says drew it."""

    CHART_TTL = 10 * 60
    CHART_IMAGES = 60
    PRELOAD_TTL = 30 * 60   # a chart uploaded ahead (TELEGRAM_PRELOAD_CHAT) is used for this long, then drawn afresh
    FRESH = 330   # a chart made in code this lately answers the same call again as it is (the readings move slowly)

    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.items: OrderedDict = OrderedDict()

    def _kept(self, entry: Drawn | None) -> bool:
        """Is there a chart, still within its time to live, in the theme charts are drawn in now?"""
        return entry is not None and self.age(entry) <= entry.ttl and entry.dark == charts.dark_now()

    def get(self, key: str | None) -> tuple[bytes | str, str] | None:
        """(the picture: Telegram's file id once it has been sent, else the PNG; its title), or None."""
        entry = self.items.get(key)
        if not self._kept(entry):
            self.items.pop(key, None)
            return None
        self.items.move_to_end(key)
        return entry.photo, entry.title

    def age_of(self, key: str | None) -> float | None:
        """How long ago this chart was drawn, if it is held."""
        entry = self.items.get(key)
        return self.age(entry) if self._kept(entry) else None

    def held(self, key: str | None) -> Drawn | None:
        """The chart kept under `key`, while it is within its time to live."""
        entry = self.items.get(key)
        return entry if self._kept(entry) else None

    def has_copy(self, key: str | None) -> bool:
        """Does Telegram hold this chart (drawn lately and already sent, so it has a file id)?"""
        entry = self.items.get(key)
        return self._kept(entry) and entry.file_id is not None

    def ready(self, key: str | None) -> Drawn | None:
        """The chart made in code for this call in the last FRESH seconds (for someone's question, or uploaded ahead to
        TELEGRAM_PRELOAD_CHAT), with the text that went with it; None if there is none."""
        entry = self.items.get(key) if key else None
        if entry is None or entry.caption is None or self.age(entry) > self.FRESH or entry.dark != charts.dark_now():
            return None
        self.items.move_to_end(key)
        return entry

    def age(self, entry: Drawn) -> float:
        return self.clock() - entry.at

    def put(self, key: str | None, png: bytes, title: str, caption: str | None = None, ttl: float | None = None, newest: int | None = None,
            drawn_by: dict | None = None):
        """caption: the answer's text, only for a chart made in code (then it can be ready). newest: the time of the newest reading
        drawn, when known. drawn_by: the call that drew it (it is kept under that call's key too)."""
        if key is None:
            return
        self.items[key] = Drawn(self.clock(), png, title, caption=caption, ttl=ttl or self.CHART_TTL, newest=newest, dark=charts.dark_now(),
                                drawn_by=drawn_by)
        self.items.move_to_end(key)
        self._trim()
        self.alias(key, drawn_key(drawn_by))

    def alias(self, key: str | None, other: str | None):
        """Keep the chart held under `key` under `other` too (one picture, one Telegram copy)."""
        if key is None or other is None or key == other or (entry := self.items.get(key)) is None:
            return
        self.items[other] = entry
        self.items.move_to_end(other)
        self._trim()

    def _trim(self):
        while len(self.items) > self.CHART_IMAGES:
            self.items.popitem(last=False)

    def sent(self, key: str | None, message):
        """`message` (as Telegram returned it) carries the chart kept under `key`: later sends of it use Telegram's copy."""
        if (entry := self.items.get(key)) is not None and (sizes := getattr(message, "photo", None)):
            entry.file_id = sizes[-1].file_id   # the largest size: the picture as it was sent
