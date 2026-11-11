"""Chart images: a chart's JSON (schemas/chart.json) drawn as a PNG by the bot's renderer (lib/charts.render_data, Pillow), for the
service's tools when a client asks for images (docs/mcp-service.md 3.7) and for the bot's interface to the service (lib/weather.py).

Pillow is optional on the service (requirements-charts.txt): nothing here imports it, nor lib.charts or lib.draw, until the first image
is asked for (`renderer`). Without it `renderer()` is None, said once as a warning, and the service answers with the charts' JSON as if
no image had been asked for, with UNAVAILABLE_NOTE.

  - `configure(mode, tz)`: the service's DARKMODE (lib/config.py's dark_mode) and the zone a time range is read in, handed to
    lib.charts (charts.configure) when it is first imported here, or at once when it already is (the bot's process configures it itself);
  - `wanted(meta, query)`: does a tool call want images: the request's `_meta` `chart_image` ("png" or "none"), else the connection's
    MCP URL's query (`?charts=png`), else not;
  - `render(chart, tz, dark)`: (PNG, dark), as the bot draws it; dark None: as DARKMODE says now (charts.dark_now)."""

import importlib
import logging
import sys
from datetime import tzinfo

log = logging.getLogger(__name__)

PNG = "image/png"
IMAGE, NO_IMAGE = "png", "none"          # request _meta chart_image, and the URL's ?charts=: images, or the JSON alone (the default)
CHARTS_META = "ecobot/charts"            # result _meta with images: the full chart JSON, each with its drawn_by (3.7)
DARK_META = "ecobot/chart_dark"          # result _meta with images: drawn in the dark theme (true) or the light (false)
IMAGE_NOTE = ("The chart is the image attached to this result: show it with your reply. Keep your text to the figures; don't describe "
              "the chart.")
UNAVAILABLE_NOTE = "Chart images aren't available on this service: the chart's data is in `charts`."
NOT_DRAWN_NOTE = "The chart's image couldn't be drawn: the chart's data is in `charts`."
MODE: str | tuple[int, int] | None = None   # the service's DARKMODE, once configured (None: as lib.charts is)
ZONE: tzinfo | None = None
_warned = False
_handed: tuple | None = None   # the (MODE, ZONE) last handed to lib.charts


def configure(mode: str | tuple[int, int], tz: tzinfo | None = None):
    """The service's DARKMODE and the zone it is read in, for its images (lib.charts.configure once lib.charts is imported)."""
    global MODE, ZONE
    MODE, ZONE = mode, tz
    if (charts := sys.modules.get("lib.charts")) is not None:
        _hand(charts)


def _hand(charts):
    global _handed
    if MODE is not None and _handed != (MODE, ZONE):
        charts.configure(MODE, ZONE)
        _handed = (MODE, ZONE)


def renderer():
    """lib.charts, imported now if it hasn't been (and given the service's DARKMODE); None when Pillow isn't installed (said once)."""
    global _warned
    try:
        charts = importlib.import_module("lib.charts")
    except ImportError as e:
        if not _warned:
            _warned = True
            log.warning("Chart images were asked for but can't be drawn here (%s): install requirements-charts.txt (Pillow); "
                        "the charts are sent as JSON alone", e)
        return None
    _hand(charts)
    return charts


def wanted(meta: dict | None, query: str | None = None) -> bool:
    """Images for this call? The request's _meta `chart_image` ("png" or "none") decides; else the connection's (the MCP URL's
    `?charts=png`, `query`); else not."""
    asked = str((meta or {}).get("chart_image") or "").strip().lower()
    if asked in (IMAGE, NO_IMAGE):
        return asked == IMAGE
    return str(query or "").strip().lower() == IMAGE


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
    dark = charts.dark_now() if dark is None else dark
    return charts.render_data(chart, tz, dark=dark), dark


__all__ = ["CHARTS_META", "DARK_META", "IMAGE", "IMAGE_NOTE", "NOT_DRAWN_NOTE", "NO_IMAGE", "PNG", "UNAVAILABLE_NOTE", "asked_dark",
           "configure", "render", "renderer", "wanted"]
