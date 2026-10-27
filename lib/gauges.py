"""The dashboard: the station's readings as gauges on a dark card, each with its highest and lowest of the day, month and year.

A thermometer, a dial or a rain beaker per tile (specs.Tile), three tiles to a row, drawn on one figure laid out in pixels (the top left
is 0, 0). A tile with no reading is dimmed. Safe to call from several threads, as charts.render is (they share its drawing lock).
"""

import math

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import to_rgb
from matplotlib.figure import Figure
from matplotlib.patches import Circle, FancyBboxPatch, Polygon, Rectangle

from .charts import _DRAWING, _png
from .specs import Dashboard, Tile

BG = "#14191D"
INK = "#E8EAED"             # the titles, the figures
SOFT = "#B8BDC2"            # scale numbers, outlines
CHIP = "#3A4046"            # the row names' backing
MAX_CHIP, MIN_CHIP, TOTAL_CHIP = "#C62828", "#4FB3F6", "#5BC8F0"
CHIPS = {"Max": MAX_CHIP, "Min": MIN_CHIP, "Total": TOTAL_CHIP}
MARKS = {"Max": "▲", "Min": "▼", "Total": "+"}   # in front of a column's name
FILL = "#D32F27"            # the thermometer's mercury
WATER = "#4FB3F6"           # the rain beaker's water
NEEDLE = "#3E86C0"
CELL_W, CELL_H, MARGIN = 480, 575, 20
DPI = 100
FADE = 0.4                  # how much of its colour a tile with no reading keeps
SWEEP, START = 250.0, 215.0   # the dial runs clockwise from START degrees (0 = right, 90 = up) through SWEEP degrees
ROWS = ("Daily", "Monthly", "Yearly")


def _pt(px: float) -> float:
    """A size in pixels as the points matplotlib's fonts are set in."""
    return px * 72 / DPI


def _mix(colour: str, keep: float) -> tuple:
    """The colour with only `keep` of itself over the card's background: how a tile with no reading is dimmed."""
    r, g, b = (keep * c + (1 - keep) * k for c, k in zip(to_rgb(colour), to_rgb(BG)))
    return r, g, b


def _text(ax, x, y, text, size, colour=INK, ha="center", va="center", weight="normal"):
    ax.text(x, y, text, fontsize=_pt(size), color=colour, ha=ha, va=va, fontweight=weight)


def _figure(value: float, decimals: int) -> str:
    return f"{value:.{decimals}f}"


def _scale_bar(ax, x: float, y0: float, y1: float, tile: Tile, ink):
    """The coloured blocks beside a thermometer or beaker, high at the top, with the scale's numbers to their left."""
    n = len(tile.colours)
    step = (y1 - y0) / n
    for i, colour in enumerate(reversed(tile.colours)):
        ax.add_patch(FancyBboxPatch((x, y0 + i * step + 1), 26, step - 2, boxstyle="square,pad=0", fc=ink(colour), ec="none"))
    for tick in tile.ticks:
        y = y0 + (tile.high - tick) / (tile.high - tile.low) * (y1 - y0)
        _text(ax, x - 8, y, f"{tick:g}", 17, ink(SOFT), ha="right")


def _thermometer(ax, tile: Tile, ox: float, oy: float, ink):
    top, bottom, cx, bulb = oy + 95, oy + 250, ox + 290, oy + 280
    _scale_bar(ax, ox + 150, top, oy + 238, tile, ink)
    outline = ink(SOFT)
    ax.add_patch(FancyBboxPatch((cx - 28, top - 10), 56, bulb - top, boxstyle="round,pad=0,rounding_size=28", fc="none", ec=outline, lw=2.5))
    ax.add_patch(Circle((cx, bulb), 38, fc=BG, ec=outline, lw=2.5, zorder=2))
    ax.add_patch(Circle((cx, bulb), 31, fc=ink(FILL), ec="none", zorder=3))
    if tile.value is not None:
        level = top + (1 - min(max((tile.value - tile.low) / (tile.high - tile.low), 0), 1)) * (bottom - top)
        ax.add_patch(FancyBboxPatch((cx - 15, level), 30, bulb - level, boxstyle="square,pad=0", fc=ink(FILL), ec="none", zorder=3))
        _text(ax, cx - 50, bulb, f"{_figure(tile.value, tile.decimals)} {tile.unit}", 40, ink(SOFT), ha="right")


def _beaker(ax, tile: Tile, ox: float, oy: float, ink):
    top, bottom, left, right = oy + 90, oy + 300, ox + 235, ox + 345
    _scale_bar(ax, ox + 150, top, bottom - 10, tile, ink)
    outline, round_ = ink(SOFT), 34
    jar = FancyBboxPatch((left, top - round_), right - left, bottom - top + round_, boxstyle=f"round,pad=0,rounding_size={round_}",
                         fc="none", ec=outline, lw=2.5, zorder=3)
    ax.add_patch(jar)
    ax.add_patch(Rectangle((left - 8, top - round_ - 2), right - left + 16, round_ + 2, fc=BG, ec="none", zorder=4))   # the jar has no lid
    if tile.value is not None and tile.value > 0:
        level = bottom - max((tile.value - tile.low) / (tile.high - tile.low), 0.02) * (bottom - 10 - top)
        water = Rectangle((left, level), right - left, bottom - level, fc=ink(WATER), ec="none", zorder=2)
        ax.add_patch(water)
        water.set_clip_path(jar)
    if tile.value is not None:
        _text(ax, ox + 290, oy + 345, f"{_figure(tile.value, tile.decimals)} {tile.unit}", 46, ink(SOFT))


def _polar(cx: float, cy: float, radius: float, degrees: float) -> tuple[float, float]:
    """The point at this angle (0 = right, 90 = up) and radius from the centre, in pixels (down is up the screen)."""
    return cx + radius * math.cos(math.radians(degrees)), cy - radius * math.sin(math.radians(degrees))


def _angle(tile: Tile, value: float) -> float:
    return START - SWEEP * min(max((value - tile.low) / (tile.high - tile.low), 0), 1)


def _band(cx: float, cy: float, inner: float, outer: float, a0: float, a1: float) -> list[tuple[float, float]]:
    """A ring segment between two angles (a0 > a1, clockwise) as polygon points."""
    n = max(int(abs(a0 - a1) / 3), 2)
    angles = np.linspace(a0, a1, n)
    return [_polar(cx, cy, outer, a) for a in angles] + [_polar(cx, cy, inner, a) for a in angles[::-1]]


def _dial(ax, tile: Tile, ox: float, oy: float, ink):
    cx, cy = ox + 240, oy + 203
    ax.add_patch(Circle((cx, cy), 138, fc="none", ec=ink(SOFT), lw=3))
    n = len(tile.colours)
    step = SWEEP / n
    for i, colour in enumerate(tile.colours):
        a0 = START - i * step - 0.8
        ax.add_patch(Polygon(_band(cx, cy, 98, 130, a0, a0 - step + 1.6), fc=ink(colour), ec="none"))
    for tick in tile.ticks:
        x, y = _polar(cx, cy, 74, _angle(tile, tick))
        _text(ax, x, y, f"{tick:g}", 17, ink(SOFT))
    _text(ax, cx, cy + 64, tile.unit, 18, ink(SOFT))
    if tile.value is not None:
        a = _angle(tile, tile.value)
        tip, tail = _polar(cx, cy, 106, a), _polar(cx, cy, -24, a)
        left, right = _polar(cx, cy, 10, a + 90), _polar(cx, cy, 10, a - 90)
        ax.add_patch(Polygon([tip, left, tail, right], fc=ink(NEEDLE), ec="none", zorder=3))
        _text(ax, cx, cy + 102, _figure(tile.value, tile.decimals), 50, ink(INK))
    ax.add_patch(Circle((cx, cy), 25, fc=BG, ec=ink(SOFT), lw=3, zorder=4))


def _chip(ax, x: float, y: float, w: float, h: float, colour, text: str, ink, size=19, weight="normal", text_colour=INK):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=9", fc=colour, ec="none"))
    _text(ax, x + w / 2, y + h / 2 + 1, text, size, ink(text_colour), weight=weight)


def _table(ax, tile: Tile, ox: float, oy: float, ink):
    """The unit, a head for each column (Max, Min, Total) and a row each of Daily, Monthly and Yearly."""
    left, top, w, h, gap = ox + 60, oy + 378, 128, 38, 4
    columns = [left + (w + gap) * (i + 1) for i in range(len(tile.columns))]
    _text(ax, left + w / 2, top + h / 2, tile.unit, 17, ink(INK), weight="bold")
    for x, name in zip(columns, tile.columns):
        _chip(ax, x, top, w, h, ink(CHIPS[name]), f"{MARKS[name]} {name}", ink, weight="bold", text_colour="#FFFFFF")
    for r, name in enumerate(ROWS, start=1):
        y = top + r * (h + 10)
        _chip(ax, left, y, w, h, ink(CHIP), name, ink, text_colour=SOFT)
        for x, figure in zip(columns, tile.rows.get(name, ())):
            _text(ax, x + w / 2, y + h / 2 + 1, "-" if figure is None else _figure(figure, tile.decimals), 21, ink(INK), weight="bold")


DRAW = {"thermometer": _thermometer, "dial": _dial, "beaker": _beaker}


def _tile(ax, tile: Tile, ox: float, oy: float):
    keep = 1.0 if tile.value is not None or any(any(f is not None for f in figures) for figures in tile.rows.values()) else FADE
    ink = (lambda colour: _mix(colour, keep)) if keep < 1 else (lambda colour: colour)
    _text(ax, ox + CELL_W / 2, oy + 38, tile.title, 38, ink(INK))
    DRAW[tile.kind](ax, tile, ox, oy, ink)
    _table(ax, tile, ox, oy, ink)


def render(dashboard: Dashboard) -> bytes:
    """PNG bytes for the dashboard: CELL_W by CELL_H pixels a tile, `per_row` to a row."""
    per_row = dashboard.per_row
    rows = math.ceil(len(dashboard.tiles) / per_row)
    width, height = per_row * CELL_W + 2 * MARGIN, rows * CELL_H + 2 * MARGIN
    with _DRAWING:
        fig = Figure(figsize=(width / DPI, height / DPI), dpi=DPI, facecolor=BG)
        FigureCanvasAgg(fig)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, width)
        ax.set_ylim(height, 0)
        ax.axis("off")
        for i, tile in enumerate(dashboard.tiles):
            _tile(ax, tile, MARGIN + (i % per_row) * CELL_W, MARGIN + (i // per_row) * CELL_H)
        fig.canvas.draw()
        w, h = fig.canvas.get_width_height(physical=True)
        return _png(np.frombuffer(fig.canvas.buffer_rgba(), "<u4") & 0xFFFFFF, w, h)
