"""The chart that goes with an alert: the same picture a typed request for it would give ("Wind chart 1d" is what the Wind button sends), so what an
alert shows is defined once, by the button sentences and lib/intent. AlertCharts keeps those charts with the bot's other charts (the ImageCache),
so one is drawn once and uploaded once however many times it is sent."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable, NamedTuple

from .. import intent
from ..charts import render
from ..timeutil import now_local
from ..tools import Turn

log = logging.getLogger(__name__)

# The alert types that carry a chart, and the request it answers: what the alert is sent with, and what its Refresh draws again
CHARTS = {"gusts": "Wind chart 1d", "uv": "UV chart 1d", "temps": "Temperature chart 1d", "particulates": "Particulates chart 1d"}


class Picture(NamedTuple):
    """A chart drawn for an alert."""
    png: bytes
    title: str
    newest: int | None   # the time of the newest reading it draws (epoch), when the chart tells it


async def chart(source, text: str, tz) -> Picture | None:
    """The chart for the request `text`, made from `source`'s tool the way the bot would answer it; None when it cannot be made (the alert then
    goes out as text). `source` is the station or the air sensor: the tool is found in its `tools` by the name the request resolves to."""
    try:
        now = now_local(tz)
        read = intent.read(text, now)
        tool, args = read.fast[0], read.fast[1]
        turn = Turn.of(read, text)
        handler = next(t for t in source.tools if t.name == tool).handler
        await handler(args, turn)
        spec = turn.charts[0]
        return Picture(await asyncio.to_thread(render, spec, tz), spec.caption(now.date()), spec.newest)
    except Exception as e:
        log.warning("The chart for the alert (%s) couldn't be made: %s: %s", text, type(e).__name__, e)
        return None


class AlertCharts:
    """The charts alerts carry (CHARTS), kept in the bot's ImageCache under their request's key (the keyboard's "Wind chart 1d" is the same
    chart), so each is drawn once and uploaded once:
    - an alert sends the chart the bot holds when it has the alert's data (drawn with the newest reading the alert is about, `since`, or a
      later one): Telegram's copy once it has been sent, to this chat or another, or by a button; else its chart is drawn and kept, and
      Telegram's copy remembered from the first chat it goes to;
    - the chart of every type alerted in the last WARM_SECONDS is drawn ahead with the keyboard's (Bot.draw_buttons_ahead, and uploaded
      ahead with them), so the Refresh under the alert is answered at once;
    - Refresh sends the held chart when it is newer than the picture shown and fresh (ImageCache.FRESH), else draws one."""

    WARM_SECONDS = 6 * 3600

    def __init__(self, images, now: Callable[[], datetime], clock: Callable[[], float] = time.monotonic):
        """images: the bot's ImageCache. now: the local time (for the requests' keys). clock: the seconds the warm time is counted in."""
        self.images, self.now, self.clock = images, now, clock
        self.alerted: dict[str, float] = {}   # request -> the clock when an alert carrying its chart last went out

    def key(self, kind: str) -> str | None:
        """The image key of an alert type's chart; None for a type without one."""
        text = CHARTS.get(kind)
        return self.images.canonical(text, self.now()) if text else None

    def warm(self) -> list[str]:
        """The requests whose charts are kept drawn ahead: those of the alert types sent in the last WARM_SECONDS."""
        now = self.clock()
        self.alerted = {text: at for text, at in self.alerted.items() if now - at <= self.WARM_SECONDS}
        return list(self.alerted)

    def held(self, kind: str, since: int | None) -> bytes | str | None:
        """The chart the bot holds for an alert of `kind` when it draws the alert's data (its newest reading is `since` or later): Telegram's
        copy once sent, else the PNG. None without one, or when the alert's reading is not known."""
        entry = self.images.held(self.key(kind)) if since is not None else None
        return entry.photo if entry and entry.newest is not None and entry.newest >= since else None

    def keep(self, kind: str, picture: Picture, since: int | None = None):
        """A chart just drawn for an alert of `kind` (about the reading `since`: it draws that reading, whatever its own time says)."""
        newest = max((t for t in (picture.newest, since) if t is not None), default=None)
        self.images.put(self.key(kind), picture.png, picture.title, newest=newest)

    def sent(self, kind: str, message, alerted: bool = True):
        """The chart of `kind` went out in `message` (as Telegram returned it): later sends use Telegram's copy. An alert (not a Refresh)
        starts the WARM_SECONDS its chart is drawn ahead for."""
        self.images.sent(self.key(kind), message)
        if alerted:
            self.alerted[CHARTS[kind]] = self.clock()

    def newer_than(self, kind: str, message) -> bytes | str | None:
        """What the Refresh under `message` can send without drawing: the held chart of `kind` when it was drawn since the message was last
        changed, and in the last ImageCache.FRESH seconds; Telegram's copy when it has one. None otherwise, or for a message whose time
        is not known."""
        shown = getattr(message, "edit_date", None) or getattr(message, "date", None)
        key = self.key(kind)
        age = self.images.age_of(key)
        if shown is None or age is None or age > self.images.FRESH:
            return None
        if shown.tzinfo is None:
            shown = shown.replace(tzinfo=timezone.utc)
        return self.images.held(key).photo if age < (datetime.now(timezone.utc) - shown).total_seconds() else None
