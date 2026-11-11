"""How the gauges look: the dashboard's colours and the shapes' sizes, shared by the PNG (lib/gauges.py, drawn with Pillow) and the
live web page (lib/web_dashboard.py, drawn by the browser from these same figures). Sizes are pixels of a CELL_W by CELL_H tile, the
top left 0, 0. Nothing here imports Pillow."""

from dataclasses import asdict

from .specs import Tile

BG = "#14191D"
INK = "#E8EAED"             # the titles, the figures
SOFT = "#B8BDC2"            # scale numbers, outlines
CHIP = "#3A4046"            # the row names' backing
MAX_CHIP, MIN_CHIP, TOTAL_CHIP = "#C62828", "#4FB3F6", "#5BC8F0"
CHIPS = {"Max": MAX_CHIP, "Min": MIN_CHIP, "Total": TOTAL_CHIP}
MARKS = {"Max": "▲", "Min": "▼", "Total": "+"}   # in front of a column's name
FILL = "#D32F27"            # the thermometer's mercury
TUBE_HALF, BULB_RADIUS = 28, 38   # the thermometer: half the tube's width, the bulb's radius (pixels, to the outline)
MERCURY_GAP = 7             # the gap between the outline and the red, the same round the bulb as up the tube
BAR_WIDTH, BAR_GAP = 13, 12   # the scale's coloured blocks beside a thermometer or beaker: how wide, and how far from its outline
RING_OUTER, BAND_WIDTH = 130, 22   # a dial's coloured scale: its outer radius and how wide it is (the outline is at 138)
LABEL_RADIUS = 84           # where a dial's numbers sit, just inside its scale
BAND_Y, BAND_SIZE = 157, 19  # a dial's band name ("Moderate"): how far below its centre (under its outline), and its size
WATER = "#4FB3F6"           # the rain beaker's water
NEEDLE = "#3E86C0"
CELL_W, CELL_H, MARGIN = 480, 575, 20
FADE = 0.4                  # how much of its colour a tile with no reading keeps
SWEEP, START = 250.0, 215.0   # the dial runs clockwise from START degrees (0 = right, 90 = up) through SWEEP degrees
ROWS = ("Daily", "Monthly", "Yearly")


def faded(tile: Tile) -> bool:
    """A tile drawn dimmed (FADE): it has no reading now (only its figures: lib/gauge_sheet.py leaves out a tile with neither)."""
    return tile.value is None


def floor(tile: Tile) -> float:
    """Where a gauge with no reading rests, and the number it reads: 0, or the scale's low end where 0 isn't on it (pressure: 950)."""
    return 0 if tile.low <= 0 <= tile.high else tile.low


def shown(tile: Tile) -> float:
    """The reading a gauge shows: its reading now, else its floor."""
    return floor(tile) if tile.value is None else tile.value


def share(tile: Tile, value: float) -> float:
    """How far along its scale (0 to 1) a reading sits: in proportion from the low end to the high one, or, on a scale of bands each an
    equal share (`bounds`: the air gauges), its band's share and in proportion within it. Beyond the ends: at the end."""
    bounds = tile.bounds
    if not bounds:
        return min(max((value - tile.low) / (tile.high - tile.low), 0), 1)
    if value <= bounds[0]:
        return 0.0
    n = len(bounds) - 1
    for i, (lo, hi) in enumerate(zip(bounds, bounds[1:])):
        if value <= hi:
            return (i + (value - lo) / (hi - lo)) / n
    return 1.0


def look() -> dict:
    """The figures above as JSON, for the web page to draw with."""
    return {"bg": BG, "ink": INK, "soft": SOFT, "chip": CHIP, "chips": CHIPS, "marks": MARKS, "fill": FILL, "water": WATER,
            "needle": NEEDLE, "tube_half": TUBE_HALF, "bulb_radius": BULB_RADIUS, "mercury_gap": MERCURY_GAP, "bar_width": BAR_WIDTH,
            "bar_gap": BAR_GAP, "ring_outer": RING_OUTER, "band_width": BAND_WIDTH, "label_radius": LABEL_RADIUS,
            "band_y": BAND_Y, "band_size": BAND_SIZE, "cell": [CELL_W, CELL_H],
            "fade": FADE, "sweep": SWEEP, "start": START, "rows": list(ROWS)}


def tile_json(tile: Tile) -> dict:
    """A tile as JSON: specs.Tile's fields, whether it is dimmed and its floor (where it rests with no reading)."""
    out = asdict(tile)
    out["rows"] = {name: list(figures) for name, figures in tile.rows.items()}
    return {**out, "colours": list(tile.colours), "columns": list(tile.columns), "ticks": list(tile.ticks), "bounds": list(tile.bounds),
            "faded": faded(tile), "floor": floor(tile)}
