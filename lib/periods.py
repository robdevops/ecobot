"""The period buttons under a chart, and the charts drawn lately.

A chart the service drew carries `drawn_by` (docs/ecobot-mcp.md 3.7): the call that drew it and the periods it is worth drawing over,
each with its label ("7d", "7D"). Its buttons are those periods, the chart's own shaded (the blue of an alert toggle that is on), and
none when it lists none (but a 🔄, "pd:reload", that draws it again when its period runs to now); a button's data is the period
("pd:7d"), never the call, which Telegram's 64 bytes couldn't hold. Pressing one replays the call with that period
(Bot.on_period_button); what drew each chart is remembered for that (Charted). A chart remembered by its question (sent before every
chart carried its drawn_by) answers that it is out of date."""

import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo



PREFIX = "pd:"
REMEMBERED = 300   # charts whose call is kept, newest first to stay
WHOLE_DAY_TOOLS = ("days", "relate", "scan")   # the tools that read a period as whole days, up to yesterday
REFRESHED = "1d"   # the period whose own shaded button draws the chart again with the latest readings (longer ones barely change)
RELOAD = "reload"   # the 🔄 under a chart with no periods to offer whose period runs to now: its call replayed as it is ("pd:reload")


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


def _span(drawn_by: dict, now: datetime, tz: tzinfo):
    """(start, end) of the chart's call's period as the service reads it now; None when it can't be read here."""
    from .periods_text import read_period
    args = drawn_by.get("arguments") or {}
    period, start, end = args.get("period"), args.get("start"), args.get("end")
    if not (period or start):
        return None
    try:
        read = read_period(period or None, start, end, now=now, tz=tz, default=period or "today",
                           whole_days=drawn_by.get("tool") in WHOLE_DAY_TOOLS, first_held=date(2000, 1, 1))   # (only the end matters)
    except Exception:   # (PeriodError, or words only a tool with the file can read)
        return None
    return read.start, read.end


def ends_now(drawn_by: dict | None, now: datetime, tz: tzinfo) -> bool:
    """Whether the chart's period runs to the present and moves on with the clock, so drawing it again can show more: to now, or (a
    tool of whole days) through yesterday, the last whole day. Fixed dates don't ("October", "2026-11-01 to 2026-11-09", even on the
    10th), nor does "yesterday" on a chart of readings."""
    span = _span(drawn_by, now, tz) if drawn_by else None
    later = _span(drawn_by, now + timedelta(days=1), tz) if span else None
    if span is None or later is None or later[1] <= span[1]:   # fixed dates ("October", "2026-11-01 to 2026-11-09"): never more
        return False

    def aware(t: datetime) -> datetime:   # (naive: the station's clock)
        return t if t.tzinfo else t.replace(tzinfo=tz)
    end, now = aware(span[1]), aware(now).astimezone(aware(span[1]).tzinfo)
    whole = drawn_by.get("tool") in WHOLE_DAY_TOOLS
    return end >= (now.replace(hour=0, minute=0, second=0, microsecond=0) if whole else now - timedelta(minutes=1))


def periods_keyboard(drawn_by: dict, current: str | None = None, now: datetime | None = None,
                     tz: tzinfo | None = None) -> "InlineKeyboardMarkup | None":
    """One button for each period the chart's drawn_by lists, labelled as the service labels it, the chart's own (`current`) shaded.
    When it lists none: a 🔄 that draws it again if its period runs to now (ends_now; `now` and `tz` given), else None."""
    from telegram import InlineKeyboardMarkup   # (here, not at the top: the service imports this module's call_key, and knows no Telegram)
    from telegram.constants import KeyboardButtonStyle

    from .buttons import button
    listed = drawn_by.get("periods") or []
    if not listed:
        return InlineKeyboardMarkup([[button("🔄", f"{PREFIX}{RELOAD}")]]) if now and tz and ends_now(drawn_by, now, tz) else None
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


def dark_now() -> bool:
    """Are charts drawn dark now (DARKMODE: lib/theme.py, not the renderer: the bot draws nothing)?"""
    from . import theme
    return theme.dark_now()


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
    PRELOAD_TTL = 30 * 60   # a chart uploaded ahead (BOT_TELEGRAM_PRELOAD_CHAT) is used for this long, then drawn afresh
    FRESH = 330   # a chart made in code this lately answers the same call again as it is (the readings move slowly)

    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.items: OrderedDict = OrderedDict()

    def _kept(self, entry: Drawn | None) -> bool:
        """Is there a chart, still within its time to live, in the theme charts are drawn in now?"""
        return entry is not None and self.age(entry) <= entry.ttl and entry.dark == dark_now()

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
        BOT_TELEGRAM_PRELOAD_CHAT), with the text that went with it; None if there is none."""
        entry = self.items.get(key) if key else None
        if entry is None or entry.caption is None or self.age(entry) > self.FRESH or entry.dark != dark_now():
            return None
        self.items.move_to_end(key)
        return entry

    def age(self, entry: Drawn) -> float:
        return self.clock() - entry.at

    def put(self, key: str | None, png: bytes, title: str, caption: str | None = None, ttl: float | None = None, newest: int | None = None,
            drawn_by: dict | None = None, dark: bool | None = None):
        """caption: the answer's text, only for a chart made in code (then it can be ready). newest: the time of the newest reading
        drawn, when known. drawn_by: the call that drew it (it is kept under that call's key too). dark: the theme it was drawn in (the
        service says which, lib/weather.py); None: the one charts are drawn in now."""
        if key is None:
            return
        self.items[key] = Drawn(self.clock(), png, title, caption=caption, ttl=ttl or self.CHART_TTL, newest=newest,
                                dark=dark_now() if dark is None else dark,
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
