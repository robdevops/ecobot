"""Chart images: a chart's JSON (schemas/chart.json) drawn as a PNG by the bot's renderer (lib/charts.render_data, Pillow), for the
service's tools when a client asks for images (docs/ecobot-mcp.md 3.7) and for the bot's interface to the service (lib/weather.py).

Pillow is optional on the service (requirements-charts.txt): nothing here imports it, nor lib.charts or lib.draw, until the first image
is asked for (`renderer`). Without it `renderer()` is None, said once as a warning, and the service answers with the charts' JSON as if
no image had been asked for, with UNAVAILABLE_NOTE.

  - `configure(mode, tz)`: the service's DARKMODE (lib/config.py's dark_mode) and the zone a time range is read in (lib/theme.py,
    which lib.charts reads when it draws);
  - `wanted(meta, query)`: does a tool call want images, and how: the request's `_meta` `chart_image` ("png", "link" or "none"),
    else the connection's MCP URL's query (`?charts=png`, `?charts=link`), else not: "png", "link" or None;
  - `render(chart, tz, dark)`: (PNG, dark), as the bot draws it; dark None: as DARKMODE says now (theme.dark_now);
  - `Links`: the PNGs of link mode, kept in memory for their lifetime (MCP_CHART_LINK_HOURS) under a random id each, at most
    LINKS_MOST of them and LINKS_BYTES in all (the oldest dropped first), served at GET /charts/<id>.png (lib/mcp_server.py) and gone
    at a restart; `markdown(title, url)`: the image as the model puts it in its reply."""

import importlib
import logging
import secrets
import time
from collections import OrderedDict
from datetime import tzinfo

from . import theme

log = logging.getLogger(__name__)

PNG = "image/png"
IMAGE, LINK, NO_IMAGE = "png", "link", "none"   # request _meta chart_image, and the URL's ?charts=: images, links to them, or the JSON
                                                # alone (the default)
CHARTS_META = "ecobot/charts"            # result _meta with images: the full chart JSON, each with its drawn_by (3.7)
DARK_META = "ecobot/chart_dark"          # result _meta with images: drawn in the dark theme (true) or the light (false)
IMAGE_NOTE = ("The chart is the image attached to this result: show it with your reply. Keep your text to the figures; don't describe "
              "the chart.")
LINK_NOTE = ("Each chart is a picture at its `url`: put each in your reply as its `markdown`, exactly as given (a markdown image), so "
             "the person sees it. Keep your text to the figures; don't describe the chart.")
UNAVAILABLE_NOTE = "Chart images aren't available on this service: the chart's data is in `charts`."
NOT_DRAWN_NOTE = "The chart's image couldn't be drawn: the chart's data is in `charts`."
_warned = False


def configure(mode: str | tuple[int, int], tz: tzinfo | None = None):
    """The service's DARKMODE and the zone it is read in, for its images (lib/theme.py, which lib.charts reads when it draws)."""
    theme.configure(mode, tz)


def renderer():
    """lib.charts, imported now if it hasn't been; None when Pillow isn't installed (said once)."""
    global _warned
    try:
        charts = importlib.import_module("lib.charts")
    except ImportError as e:
        if not _warned:
            _warned = True
            log.warning("Chart images were asked for but can't be drawn here (%s): install requirements-charts.txt (Pillow); "
                        "the charts are sent as JSON alone", e)
        return None
    return charts


def wanted(meta: dict | None, query: str | None = None) -> str | None:
    """Images for this call, and how: IMAGE (attached), LINK (a URL each) or None. The request's _meta `chart_image` ("png", "link" or
    "none") decides; else the connection's (the MCP URL's `?charts=png` or `?charts=link`, `query`); else none."""
    asked = str((meta or {}).get("chart_image") or "").strip().lower()
    if asked in (IMAGE, LINK, NO_IMAGE):
        return None if asked == NO_IMAGE else asked
    query = str(query or "").strip().lower()
    return query if query in (IMAGE, LINK) else None


def asked_dark(meta: dict | None) -> bool | None:
    """The request's _meta `chart_dark` (true or false); None when not sent (DARKMODE decides)."""
    dark = (meta or {}).get("chart_dark")
    return dark if isinstance(dark, bool) else None


def render(chart: dict, tz: tzinfo, dark: bool | None = None) -> tuple[bytes, bool]:
    """(PNG, dark) of a chart given as its JSON, as the bot draws it (lib.charts.render_data). Raises ImportError without Pillow,
    ValueError for JSON that isn't a chart."""
    charts = renderer()
    if charts is None:
        raise ImportError("Pillow isn't installed: chart images can't be drawn")
    dark = theme.dark_now() if dark is None else dark
    return charts.render_data(chart, tz, dark=dark), dark


# ---------- link mode: the pictures served at /charts/<id>.png ----------
LINK_HOURS = 24                 # MCP_CHART_LINK_HOURS' default: how long a chart's link answers
LINKS_MOST = 500                # the most pictures kept ...
LINKS_BYTES = 64 << 20          # ... and the most bytes in all: past either, the oldest go first
ID_BYTES = 16                   # an id's randomness (secrets.token_urlsafe): unguessable, the link's only key


class Links:
    """The pictures of link mode, in memory: `put(png)` keeps one under a new random id (the oldest dropped while there are more than
    `most` or more than `most_bytes` in all); `get(id)` is (png, seconds left) until `hours` have passed, else None. Nothing is kept
    on disk: a restart forgets them all. `clock`: time.time, or a test's."""

    def __init__(self, hours: float = LINK_HOURS, most: int = LINKS_MOST, most_bytes: int = LINKS_BYTES, clock=time.time):
        self.seconds, self.most, self.most_bytes, self.clock = hours * 3600, most, most_bytes, clock
        self._kept: OrderedDict[str, tuple[bytes, float]] = OrderedDict()   # id: (png, when it expires), oldest first
        self.bytes = 0

    def __len__(self) -> int:
        return len(self._kept)

    def put(self, png: bytes) -> str:
        self._expire()
        key = secrets.token_urlsafe(ID_BYTES)
        self._kept[key] = (png, self.clock() + self.seconds)
        self.bytes += len(png)
        while self._kept and (len(self._kept) > self.most or self.bytes > self.most_bytes):
            self._drop(next(iter(self._kept)))
        return key

    def get(self, key: str) -> tuple[bytes, float] | None:
        self._expire()
        kept = self._kept.get(key)
        return (kept[0], kept[1] - self.clock()) if kept else None

    def _expire(self):
        now = self.clock()
        while self._kept and next(iter(self._kept.values()))[1] <= now:   # (in the order kept: the oldest expire first)
            self._drop(next(iter(self._kept)))

    def _drop(self, key: str):
        png, _ = self._kept.pop(key)
        self.bytes -= len(png)


def markdown(title, url: str) -> str:
    """A chart as a markdown image: ![title](url), the title's brackets and line breaks made harmless."""
    text = " ".join(str(title or "Chart").split())
    return f"![{text.replace('[', '(').replace(']', ')')}]({url})"


__all__ = ["CHARTS_META", "DARK_META", "IMAGE", "IMAGE_NOTE", "LINK", "LINK_HOURS", "LINK_NOTE", "LINKS_BYTES", "LINKS_MOST", "Links",
           "NOT_DRAWN_NOTE", "NO_IMAGE", "PNG", "UNAVAILABLE_NOTE", "asked_dark", "configure", "markdown", "render", "renderer", "wanted"]
