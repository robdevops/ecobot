"""The Status picture (ecobot://status, lib/status.py): the weather station's temperature, humidity and rain gauges on a dark card, its
readings now, for a chat client to send with its Status (the Telegram bot). The highs and lows are not drawn: they are in the
resource's text, and in the web page's tables (lib/web_dashboard.py, which draws every gauge in the browser).

A thermometer, a dial or a rain beaker per tile (specs.Tile), `per_row` to a row, drawn on one canvas laid out in pixels (the top left
is 0, 0), in lib/gauge_look.py's colours and sizes (the web page draws with them too), each tile CELL_W by TILE_H. A tile with no
reading is dimmed, its gauge at its floor (lib/gauge_look.py floor: the mercury at the scale's low end, the needle at its start, the
beaker empty) reading the floor's number. Only ecobot-mcp draws it (the bot draws nothing). Safe to call from several threads, as
charts.render is (they share its drawing lock).
"""

import math

from .charts import _DRAWING, _blend
from .draw import PT, Canvas, arc, clip_below, rounded_rect
from .gauge_look import (BAND_WIDTH, BAR_GAP, BAR_WIDTH, BG, BULB_RADIUS, CELL_W, FADE, FILL, INK, LABEL_RADIUS, MARGIN, MERCURY_GAP,
                         NEEDLE, RING_OUTER, SOFT, START, SWEEP, TUBE_HALF, WATER, faded, share, shown)
from .specs import Dashboard, Tile

DPI = 100
TILE_H = 390   # a tile's height: its title and gauge (the web page's CELL_H also holds the tables under them)


def _lw(width: float) -> float:
    """A line width given in points at DPI (as the dashboard was first laid out) in the canvas's points."""
    return width * DPI / 72 / PT


def _text(c: Canvas, x, y, text, size, colour=INK, ha="center", va="center", weight="normal"):
    """`size` in pixels."""
    c.text(text, x, y, size / PT, colour, weight, ha=ha, va=va, record=False)


def _figure(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}"


def _scale_bar(c: Canvas, x: float, y0: float, y1: float, tile: Tile, ink):
    """The coloured blocks beside a thermometer or beaker, high at the top, with the scale's numbers to their left."""
    n = len(tile.colours)
    step = (y1 - y0) / n
    for i, colour in enumerate(reversed(tile.colours)):
        c.rect(x, y0 + i * step + 1, x + BAR_WIDTH, y0 + (i + 1) * step - 1, ink(colour))
    for tick in tile.ticks:
        y = y0 + (tile.high - tick) / (tile.high - tile.low) * (y1 - y0)
        _text(c, x - 6, y, f"{tick:g}", 17, ink(SOFT), ha="right")


def _thermometer_shape(cx: float, top: float, half: float, bulb_y: float, radius: float) -> list[tuple[float, float]]:
    """A thermometer's outline as one closed line: the tube (`half` its width, round at the top, its top at `top`) running into the bulb
    (centred `bulb_y`), the two joined where the tube's sides meet the bulb, so no line crosses between them."""
    meet = math.degrees(math.atan2(math.sqrt(radius ** 2 - half ** 2), half))   # where the right side meets the bulb (0 = right, 90 = up)
    join = bulb_y - math.sqrt(radius ** 2 - half ** 2)
    cap = arc(cx, top + half, half, 180, 0, 36)
    bulb = arc(cx, bulb_y, radius, meet, -180 - meet, 72)
    return [(cx - half, join), *cap, (cx + half, join), *bulb]


def _thermometer(c: Canvas, tile: Tile, ox: float, oy: float, ink):
    top, bottom, cx, bulb = oy + 95, oy + 238, ox + 285, oy + 280   # the red rises against the scale beside it, which runs top to bottom
    half, radius, gap = TUBE_HALF, BULB_RADIUS, MERCURY_GAP
    _scale_bar(c, cx - half - BAR_GAP - BAR_WIDTH, top, bottom, tile, ink)
    inside = _thermometer_shape(cx, top - 10 + gap, half - gap, bulb, radius - gap)
    value = shown(tile)   # (no reading: the floor)
    level = top + (1 - share(tile, value)) * (bottom - top)
    c.polygon(clip_below(inside, level), ink(FILL))   # the column and the bulb, one shape, the same gap from the outline all round
    c.polygon(_thermometer_shape(cx, top - 10, half, bulb, radius), None, outline=ink(SOFT), width=_lw(2.5))
    _text(c, cx - 50, bulb, f"{_figure(value, tile.decimals)} {tile.unit}", 40, ink(SOFT), ha="right")


def _beaker(c: Canvas, tile: Tile, ox: float, oy: float, ink):
    top, bottom, left, right = oy + 90, oy + 300, ox + 235, ox + 345
    _scale_bar(c, left - BAR_GAP - BAR_WIDTH, top, bottom - 10, tile, ink)
    round_ = 34
    jar = rounded_rect(left, top - round_, right, bottom, round_, 12)
    value = shown(tile)   # (no reading: the floor, empty)
    if value > tile.low:
        level = bottom - max(share(tile, value), 0.02) * (bottom - 10 - top)
        c.polygon(clip_below(jar, level), ink(WATER))
    c.polygon(jar, None, outline=ink(SOFT), width=_lw(2.5))
    c.rect(left - 8, top - round_ - 2, right + 8, top, BG)   # the jar has no lid
    _text(c, ox + 290, oy + 345, f"{_figure(value, tile.decimals)} {tile.unit}", 46, ink(SOFT))


def _polar(cx: float, cy: float, radius: float, degrees: float) -> tuple[float, float]:
    """The point at this angle (0 = right, 90 = up) and radius from the centre, in pixels (down is up the screen)."""
    return cx + radius * math.cos(math.radians(degrees)), cy - radius * math.sin(math.radians(degrees))


def _angle(tile: Tile, value: float) -> float:
    return START - SWEEP * share(tile, value)


def _band(cx: float, cy: float, inner: float, outer: float, a0: float, a1: float) -> list[tuple[float, float]]:
    """A ring segment between two angles (a0 > a1, clockwise) as polygon points."""
    n = max(int(abs(a0 - a1) / 3), 2)
    return arc(cx, cy, outer, a0, a1, n) + arc(cx, cy, inner, a1, a0, n)


def _dial(c: Canvas, tile: Tile, ox: float, oy: float, ink):
    cx, cy = ox + 240, oy + 203
    c.circle(cx, cy, 138, outline=ink(SOFT), width=_lw(3))
    n = len(tile.colours)
    step = SWEEP / n
    for i, colour in enumerate(tile.colours):
        a0 = START - i * step - 0.8
        c.polygon(_band(cx, cy, RING_OUTER - BAND_WIDTH, RING_OUTER, a0, a0 - step + 1.6), ink(colour))
    for tick in tile.ticks:
        x, y = _polar(cx, cy, LABEL_RADIUS, _angle(tile, tick))
        _text(c, x, y, f"{tick:g}", 17, ink(SOFT))
    _text(c, cx, cy + 64, tile.unit, 18, ink(SOFT))
    value = shown(tile)   # (no reading: the floor)
    a = _angle(tile, value)
    tip, tail = _polar(cx, cy, RING_OUTER - BAND_WIDTH - 6, a), _polar(cx, cy, -24, a)   # the needle reaches nearly to the scale
    left, right = _polar(cx, cy, 10, a + 90), _polar(cx, cy, 10, a - 90)
    c.polygon([tip, left, tail, right], ink(NEEDLE))
    _text(c, cx, cy + 102, _figure(value, tile.decimals), 50, ink(INK))
    c.circle(cx, cy, 25, BG, outline=ink(SOFT), width=_lw(3))


DRAW = {"thermometer": _thermometer, "dial": _dial, "beaker": _beaker}


def _tile(c: Canvas, tile: Tile, ox: float, oy: float):
    keep = FADE if faded(tile) else 1.0
    ink = (lambda colour: _blend(colour, 1 - keep, BG)) if keep < 1 else (lambda colour: colour)   # dimmed towards the card
    _text(c, ox + CELL_W / 2, oy + 38, tile.title, 38, ink(INK))
    DRAW[tile.kind](c, tile, ox, oy, ink)


def render(dashboard: Dashboard) -> bytes:
    """PNG bytes for the gauges: CELL_W by TILE_H pixels a tile, `per_row` to a row."""
    per_row = dashboard.per_row
    rows = math.ceil(len(dashboard.tiles) / per_row)
    width, height = per_row * CELL_W + 2 * MARGIN, rows * TILE_H + 2 * MARGIN
    with _DRAWING:
        c = Canvas(width, height, BG)
        for i, tile in enumerate(dashboard.tiles):
            _tile(c, tile, MARGIN + (i % per_row) * CELL_W, MARGIN + (i // per_row) * TILE_H)
        return c.png()
