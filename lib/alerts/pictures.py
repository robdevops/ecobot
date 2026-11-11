"""The chart that goes with an alert: the same picture a keyboard button gives (the Wind button's fixed call is the gusts alert's), so
what an alert shows is defined once, by those calls (lib/templates.py CALLS: the Wind button is summary {readings: ["wind"], period: "1d",
chart: true}), which the service draws: the rule puts the chart's data, with its drawn_by, in the alert's event (chart_data), and the bot
has it drawn (picture_of: by ecobot-mcp, lib/weather.py, replaying the chart's drawn_by). AlertCharts keeps those charts with the bot's
other charts (the ImageCache, by call), so one is drawn once and uploaded once however many times it is sent."""

import json
import logging
from datetime import datetime, timezone, tzinfo
from typing import NamedTuple

from .. import templates
from ..periods import call_key
from ..specs import from_json
from ..timeutil import now_local

log = logging.getLogger(__name__)

# The alert types that carry a chart, and the service call that draws it: what the alert is sent with, and what its Refresh draws again
# (the Wind, Temperature and Particulates buttons' own calls; UV's has no button)
CHARTS = {"gusts": templates.CALLS["Wind chart 1d"], "uv": ("summary", {"readings": ["uv"], "period": "1d", "chart": True}),
          "temps": templates.CALLS["Temperature chart 1d"], "particulates": templates.CALLS["Particulates chart 1d"]}
CHART_POINTS = "drawn"   # every point, as drawn (lib/service/common.py AS_DRAWN): the bot's picture


class Picture(NamedTuple):
    """A chart drawn for an alert."""
    png: bytes
    title: str
    newest: int | None   # the time of the newest reading it draws (epoch), when the chart tells it
    drawn_by: dict | None = None   # the call that draws it again
    dark: bool | None = None       # drawn in the dark theme (None: the theme charts are drawn in now)


def _service_of(source, tz):
    """The service's tools over one source (the station or the air sensor), for a monitor that has only its source."""
    from ..service import Service
    from ..service.context import Context
    name = getattr(source, "name", "")
    return Service.of(Context(eco=source if name == "Ecowitt" else None, air=source if name == "AirGradient" else None, tz=tz))


async def chart_data(source, call: tuple[str, dict], tz, service=None) -> dict | None:
    """The chart of the service `call` (one of CHARTS) as data (3.7's JSON, with the `drawn_by` that draws it again), as the service gives it
    (every point, as drawn): what an alert event carries. None when it cannot be made (the alert then goes out as text). `source` is the
    station or the air sensor; `service` the service's tools, else built over `source`."""
    tool, args = call
    try:
        service = service or _service_of(source, tz)
        out = json.loads(await service[tool].call(args, {"chart_points": CHART_POINTS}))
        if "error" in out:
            raise ValueError(out["error"])
        return out["charts"][0]
    except Exception as e:
        log.warning("The chart for the alert (%s %s) couldn't be made: %s: %s", tool, args, type(e).__name__, e)
        return None


def _picture(image, tz) -> Picture:
    """A lib/weather.ChartImage as an alert's Picture."""
    spec = from_json(image.chart)
    return Picture(image.png, spec.caption(now_local(tz).date()), spec.newest, image.chart.get("drawn_by"), image.dark)


async def picture_of(data: dict, tz, weather) -> Picture | None:
    """A chart given as data, drawn again by `weather` (ecobot-mcp as the bot reaches it, lib/weather.py: it replays the chart's
    drawn_by); None when it cannot be. The bot draws nothing itself."""
    try:
        return _picture(await weather.picture(data), tz)
    except Exception as e:
        log.warning("The alert's chart couldn't be drawn: %s: %s", type(e).__name__, e)
        return None


async def chart(call: tuple[str, dict], tz, weather) -> Picture | None:
    """The chart of the service `call` (one of CHARTS), drawn by ecobot-mcp (`weather`, as the bot reaches it: lib/weather.py); None when
    it cannot be made."""
    tool, args = call
    try:
        out, image = await weather.chart_image(tool, args, {"chart_points": CHART_POINTS})
        if image is None:
            raise ValueError(out.get("error") or "no chart")
        return _picture(image, tz)
    except Exception as e:
        log.warning("The chart for the alert (%s %s) couldn't be made: %s: %s", tool, args, type(e).__name__, e)
        return None


class AlertCharts:
    """The charts alerts carry (CHARTS), kept in the bot's ImageCache under their call (the Wind button's is the same chart), so each is
    drawn once and uploaded once:
    - an alert sends the chart the bot holds when it has the alert's data (drawn with the newest reading the alert is about, `since`, or a
      later one): Telegram's copy once it has been sent, to this chat or another, or by a button; else its chart is drawn and kept, and
      Telegram's copy remembered from the first chat it goes to;
    - Refresh sends the held chart when it is newer than the picture shown and fresh (ImageCache.FRESH), else draws one."""

    def __init__(self, images, tz: tzinfo | None = None):
        """images: the bot's ImageCache. tz: the station's zone (its titles' dates)."""
        self.images, self.tz = images, tz

    @staticmethod
    def call(kind: str) -> tuple[str, dict] | None:
        """The service call an alert type's chart is drawn by; None for a type without one."""
        return CHARTS.get(kind)

    def key(self, kind: str) -> str | None:
        """The image key of an alert type's chart (its call); None for a type without one."""
        call = self.call(kind)
        return call_key(*call) if call else None

    def held(self, kind: str, since: int | None) -> bytes | str | None:
        """The chart the bot holds for an alert of `kind` when it draws the alert's data (its newest reading is `since` or later): Telegram's
        copy once sent, else the PNG. None without one, or when the alert's reading is not known."""
        entry = self.images.held(self.key(kind)) if since is not None else None
        return entry.photo if entry and entry.newest is not None and entry.newest >= since else None

    def keep(self, kind: str, picture: Picture, since: int | None = None):
        """A chart just drawn for an alert of `kind` (about the reading `since`: it draws that reading, whatever its own time says)."""
        newest = max((t for t in (picture.newest, since) if t is not None), default=None)
        self.images.put(self.key(kind), picture.png, picture.title, newest=newest, drawn_by=picture.drawn_by, dark=picture.dark)

    def sent(self, kind: str, message):
        """The chart of `kind` went out in `message` (as Telegram returned it): later sends use Telegram's copy."""
        self.images.sent(self.key(kind), message)

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
