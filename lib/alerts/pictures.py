"""The chart that goes with an alert: the same picture a typed request for it would give ("Wind chart 1d" is what the Wind button sends), so what an
alert shows is defined once, by the button sentences, each a fixed service call (lib/intent.chart_call: "Wind chart 1d" is summary
{readings: ["wind"], period: "1d", chart: true}), which the service draws: the rule puts the chart's data, with its drawn_by, in the alert's
event (chart_data), and the bot has it drawn (picture_of: by the weather service it reaches, lib/weather.py, which a remote service draws
by replaying the chart's drawn_by). AlertCharts keeps those charts with the bot's other charts (the ImageCache,
by call), so one is drawn once and uploaded once however many times it is sent."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone, tzinfo
from typing import Callable, NamedTuple

from .. import intent
from ..periods import call_key
from ..specs import from_json
from ..timeutil import now_local

log = logging.getLogger(__name__)

# The alert types that carry a chart, and the request it answers: what the alert is sent with, and what its Refresh draws again
CHARTS = {"gusts": "Wind chart 1d", "uv": "UV chart 1d", "temps": "Temperature chart 1d", "particulates": "Particulates chart 1d"}
CHART_POINTS = "drawn"   # every point, as drawn (lib/service/common.py AS_DRAWN): the bot's picture


def render_data(data: dict, tz, *args, **kwargs) -> bytes:
    """lib/charts.render_data, imported when first drawn: the service imports this module for chart_data, and draws nothing here."""
    from ..charts import render_data as render
    return render(data, tz, *args, **kwargs)


class Picture(NamedTuple):
    """A chart drawn for an alert."""
    png: bytes
    title: str
    newest: int | None   # the time of the newest reading it draws (epoch), when the chart tells it
    drawn_by: dict | None = None   # the call that draws it again
    dark: bool | None = None       # drawn in the dark theme (None: the theme charts are drawn in now)


def call_of(text: str, now: datetime, tz: tzinfo) -> tuple[str, dict] | None:
    """The service call a chart's request is read as (`now`: the station's local time), or None."""
    return intent.chart_call(text, now, tz)


def _service_of(source, tz):
    """The service's tools over one source (the station or the air sensor), for a monitor that has only its source."""
    from ..service import Service
    from ..service.context import Context
    name = getattr(source, "name", "")
    return Service.of(Context(eco=source if name == "Ecowitt" else None, air=source if name == "AirGradient" else None, tz=tz))


async def chart_data(source, text: str, tz, service=None) -> dict | None:
    """The chart for the request `text` as data (3.7's JSON, with the `drawn_by` that draws it again), as the service gives it (every point,
    as drawn): what an alert event carries. None when it cannot be made (the alert then goes out as text). `source` is the station or the air
    sensor; `service` the service's tools, else built over `source`."""
    try:
        tool, args = call_of(text, now_local(tz), tz)
        service = service or _service_of(source, tz)
        out = json.loads(await service[tool].call(args, {"chart_points": CHART_POINTS}))
        if "error" in out:
            raise ValueError(out["error"])
        return out["charts"][0]
    except Exception as e:
        log.warning("The chart for the alert (%s) couldn't be made: %s: %s", text, type(e).__name__, e)
        return None


def _picture(image, tz) -> Picture:
    """A lib/weather.ChartImage as an alert's Picture."""
    spec = from_json(image.chart)
    return Picture(image.png, spec.caption(now_local(tz).date()), spec.newest, image.chart.get("drawn_by"), image.dark)


async def picture_of(data: dict, tz, weather=None) -> Picture | None:
    """A chart given as data, drawn: by `weather` (the service as the bot reaches it, lib/weather.py: a remote one draws it), else by
    the bot's renderer here; None when it cannot be."""
    try:
        if weather is not None:
            return _picture(await weather.picture(data), tz)
        spec = from_json(data)
        return Picture(await asyncio.to_thread(render_data, data, tz), spec.caption(now_local(tz).date()), spec.newest, data.get("drawn_by"))
    except Exception as e:
        log.warning("The alert's chart couldn't be drawn: %s: %s", type(e).__name__, e)
        return None


async def chart(source, text: str, tz, service=None) -> Picture | None:
    """The chart for the request `text`, drawn by the service the way the bot would answer it; None when it cannot be made. `service`
    may be the weather service as the bot reaches it (lib/weather.py), which draws it (a remote one, on the service)."""
    if getattr(service, "is_weather", False):
        try:
            tool, args = call_of(text, now_local(tz), tz)
            out, image = await service.chart_image(tool, args, {"chart_points": CHART_POINTS})
            if image is None:
                raise ValueError(out.get("error") or "no chart")
            return _picture(image, tz)
        except Exception as e:
            log.warning("The chart for the alert (%s) couldn't be made: %s: %s", text, type(e).__name__, e)
            return None
    data = await chart_data(source, text, tz, service)
    return await picture_of(data, tz) if data else None


class AlertCharts:
    """The charts alerts carry (CHARTS), kept in the bot's ImageCache under the call their request is read as (the keyboard's "Wind chart
    1d" is the same chart), so each is drawn once and uploaded once:
    - an alert sends the chart the bot holds when it has the alert's data (drawn with the newest reading the alert is about, `since`, or a
      later one): Telegram's copy once it has been sent, to this chat or another, or by a button; else its chart is drawn and kept, and
      Telegram's copy remembered from the first chat it goes to;
    - the chart of every type alerted in the last WARM_SECONDS is drawn ahead with the keyboard's (Bot.draw_buttons_ahead, and uploaded
      ahead with them), so the Refresh under the alert is answered at once;
    - Refresh sends the held chart when it is newer than the picture shown and fresh (ImageCache.FRESH), else draws one."""

    WARM_SECONDS = 6 * 3600

    def __init__(self, images, now: Callable[[], datetime], clock: Callable[[], float] = time.monotonic, tz: tzinfo | None = None):
        """images: the bot's ImageCache. now: the local time (for the requests' calls). clock: the seconds the warm time is counted in.
        tz: the station's zone (default: now()'s own)."""
        self.images, self.now, self.clock, self.tz = images, now, clock, tz
        self.alerted: dict[str, float] = {}   # alert type -> the clock when an alert carrying its chart last went out

    def call(self, kind: str) -> tuple[str, dict] | None:
        """The service call an alert type's chart is drawn by; None for a type without one."""
        text, now = CHARTS.get(kind), self.now()
        return call_of(text, now, self.tz or now.tzinfo) if text else None

    def key(self, kind: str) -> str | None:
        """The image key of an alert type's chart (its call); None for a type without one."""
        call = self.call(kind)
        return call_key(*call) if call else None

    def warm(self) -> list[tuple[str, dict]]:
        """The calls whose charts are kept drawn ahead: those of the alert types sent in the last WARM_SECONDS."""
        now = self.clock()
        self.alerted = {kind: at for kind, at in self.alerted.items() if now - at <= self.WARM_SECONDS}
        return [call for kind in self.alerted if (call := self.call(kind))]

    def held(self, kind: str, since: int | None) -> bytes | str | None:
        """The chart the bot holds for an alert of `kind` when it draws the alert's data (its newest reading is `since` or later): Telegram's
        copy once sent, else the PNG. None without one, or when the alert's reading is not known."""
        entry = self.images.held(self.key(kind)) if since is not None else None
        return entry.photo if entry and entry.newest is not None and entry.newest >= since else None

    def keep(self, kind: str, picture: Picture, since: int | None = None):
        """A chart just drawn for an alert of `kind` (about the reading `since`: it draws that reading, whatever its own time says)."""
        newest = max((t for t in (picture.newest, since) if t is not None), default=None)
        self.images.put(self.key(kind), picture.png, picture.title, newest=newest, drawn_by=picture.drawn_by, dark=picture.dark)

    def sent(self, kind: str, message, alerted: bool = True):
        """The chart of `kind` went out in `message` (as Telegram returned it): later sends use Telegram's copy. An alert (not a Refresh)
        starts the WARM_SECONDS its chart is drawn ahead for."""
        self.images.sent(self.key(kind), message)
        if alerted:
            self.alerted[kind] = self.clock()

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
