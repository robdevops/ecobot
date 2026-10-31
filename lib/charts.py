"""Charts sent alongside history answers, for both Ecowitt and AirGradient.

A history tool adds a specs.Chart to turn.charts (the question's Turn, made by the bot) when a chart was asked for; the
bot renders them after the answer is written and sends them with it. Every chart is one or more panels on a shared time
axis (see specs.py):
  - one panel of lines is drawn large, with the records marked as pills, an end dot and (for wind) the compass beside it,
    at exactly 1280x720, the size Telegram displays photos at, so nothing is rescaled;
  - several panels are stacked, the figure growing a little taller with each one past two (four panels: 1280x1160).
Rain (bars) sits behind the lines of the panel it belongs to, on a scale of its own read on the right.

Drawn on lib/draw.py's Canvas (Pillow): positions are figure pixels (y down), sizes of lines and text are points at DPI. The
layout is given as it always was, in inches and shares of the figure.
"""

import math
import threading
import time
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from statistics import fmean

from . import draw
from .draw import DPI, PT, Canvas, blend, nice_ticks, rgb
from .specs import Chart, Compass, Line, Panel

# Palette. Slate neutrals for the furniture; one hue per reading (weather and air quality, kept clear of the green/yellow/red
# rating zones); the same reading indoors is its complementary hue (opposite on the colour wheel, as a painter pairs them).
BG, TEXT, MUTED, GRID, AXIS = "#FFFFFF", "#0F172A", "#64748B", "#E2E8F0", "#CBD5E1"
READING_COLOURS = {
    "temperature": "#FF9830",   # orange (indoors: purple)
    "feels_like": "#FF8C42",    # tangerine (beside the crimson, as blue sits beside violet)
    "solar": "#F5B83D",         # golden
    "uv": "#8B5CF6",            # violet
    "pressure": "#C04CE8",      # orchid
    "vpd": "#06B6D4",           # cyan
    "humidity": "#4F46E5",      # indigo
    "dew_point": "#5794F2",     # blue (indoors: yellow)
    "wind": "#64748B",          # slate grey
    "air_temperature": "#E11D48",   # the air sensor's own temperature and humidity: apart from the station's (orange, indigo)
    "air_humidity": "#0D9488",
    "pm2_5": "#0EA5E9", "pm10": "#8B5CF6", "pm1": "#14B8A6", "co2": "#475569", "voc_index": "#D97706", "nox_index": "#DB2777",
}
# Indoors: the pair of the outdoor line in its panel. Temperature is orange outdoors and purple indoors, dew point blue and yellow
# (Grafana's palette); humidity is indigo with gold, tangerine feels-like with azure.
INDOOR_COLOURS = {"temperature": "#B877D9", "humidity": "#CA8A04", "dew_point": "#EBB700", "feels_like": "#2B9BD6"}
ZONE_COLOURS = ("#22C55E", "#EAB308", "#EF4444")  # good / poor / very poor
FALLBACK = ["#10B981", "#EC4899", "#84CC16"]
RAIN = "#7CC3F7"                 # light blue: the rain sits behind the lines and stays clear of every reading's colour
RAIN_TEXT = "#2F86C8"            # the same blue, darker, for the rain scale's text
CARD = "#F8FAFC"                 # the faint tint behind each panel of a stack
WIND_STEPS = ("#CBD5E1", "#94A3B8", "#475569")  # light, middle and strong wind (the wind hue)
W_IN, H_IN = 6.4, 3.6            # 1280 x 720 px
AX_RECT = [0.075, 0.13, 0.905, 0.64]  # left, bottom, width, height (figure fraction) of a single chart
PANEL_IN = 1.1                   # each panel past two adds this much height (inches)
HEAD_IN, FOOT_IN = 0.9, 0.47     # room above and below the panels (inches)
STACKED_KEY_IN = 0.28            # more room above a big chart whose colour key is stacked below its title (see _stacked_key)
TITLE_IN, SUBTITLE_IN = 0.27, 0.52   # the headline's lines, centred this far below the top (the colour key beside a big chart's title too)
ROSE_RECT = [0.685, 0.12, 0.27, 0.62]   # the wind rose beside a wind chart (figure fraction); the line's plot ends before it, at:
BESIDE_ROSE_WIDTH = 0.57
BAR_FILL = 0.85                  # a bar fills this share of its slot; the rest is the gap to the next
LEADER_CLEARANCE = 0.05          # peak labels sit at least this share of the panel's width away from the peaks (shorter = closer)
BARS_SHARE = 0.95                # rain behind a line is drawn over the panel's full height (the tallest bar nearly reaches the top), under the line
TICK_FONT, TICK_PAD = 7.5, 5     # tick labels: size, and their distance from the axis (pt)
DOTTED = (1, 2)                  # a dotted line: one width drawn, two left


def _bar_width(seconds: float) -> float:
    """A bar's drawn width on the time axis (seconds), for bars `seconds` apart."""
    return seconds * BAR_FILL


@dataclass(frozen=True)
class Look:
    """How large the parts of a panel of lines are drawn. A chart of one panel is drawn BIG; the panels of a stack SMALL."""
    end_dot: float           # the dot on the end of each line: size (area, pt²), rim
    end_rim: float
    pill_font: float         # a value pill on a peak: font, dot, dot rim, lift above the point (pt), box (pad, rounding), draw order
    pill_dot: float
    pill_rim: float
    pill_lift: float
    pill_box: tuple[float, float]
    pill_z: int
    pitch: float             # the stacked pills beside several lines: row spacing (pt)
    pad: float               # room above and below the data for pills, as a share of its span
    legend_font: float
    key_from: int            # the colour key is drawn from this many entries (a big chart's names its one line too)
    units: bool              # pills carry a decimal and the unit; a stacked panel's are the bare number (its title has the unit)
    nbins: int               # about how many y ticks
    card: bool               # a faint tint behind the panel (a stack's panels; a chart of one panel is on white)
    in_headline: bool        # the panel's name and unit are the figure's headline, and its colour key sits beside it


BIG = Look(end_dot=30, end_rim=1.5, pill_font=8.5, pill_dot=18, pill_rim=1.2, pill_lift=9, pill_box=(0.35, 0.8), pill_z=6, pitch=17,
           pad=0.26, legend_font=10, key_from=1, units=True, nbins=5, card=False, in_headline=True)
SMALL = Look(end_dot=20, end_rim=1.2, pill_font=7.5, pill_dot=12, pill_rim=0.8, pill_lift=5, pill_box=(0.25, 0.6), pill_z=4, pitch=14,
             pad=0.3, legend_font=8.5, key_from=2, units=False, nbins=4, card=True, in_headline=False)


def _stacked_key(p: Panel, look: Look) -> bool:
    """Is the colour key of this big chart set in a column below the title (long names, or two scales), not in a row beside it?"""
    names = [s.label for s in p.lines] + ([p.bars.label] if p.bars else [])
    return look.in_headline and len(names) >= 2 and (any(s.right for s in p.lines) or sum(len(n) for n in names) > 24)


def _look_for(chart: Chart) -> Look:
    """A chart of one panel of lines is drawn BIG; anything else (a stack, rain alone) SMALL."""
    return BIG if chart.single else SMALL


FONT = draw.FONTS.family                 # the font the charts are drawn in (Inter, else Roboto, else what is installed)
TITLE_WEIGHT = draw.FONTS.title_weight   # titles in semi-bold where the font has it; plain bold otherwise


def _colour(line: Line, i: int, reading: str = "") -> str:
    """The line's reading's hue (indoors, its complement), the panel's reading when the line names none, else the next fallback."""
    reading = line.reading or reading
    if base := READING_COLOURS.get(reading):
        return (INDOOR_COLOURS.get(reading) or _blend(base, 0.4)) if line.indoor else base
    return FALLBACK[i % len(FALLBACK)]


def _blend(colour: str, share: float, towards: str = "#FFFFFF") -> str:
    """The colour with this share of `towards` (white, unless given) blended in; gauges dims a tile towards its card with it."""
    return blend(colour, share, towards)


def _deg(unit: str) -> str:
    if not unit:
        return ""
    return "°" if unit.replace("º", "°") in ("°C", "°F", "°") else f" {unit}"


def _ink_on(colour: str) -> str:
    """The text colour for a label on this fill: white, or near-black on a light one (yellow, amber)."""
    r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (v / 255 for v in rgb(colour)))
    return TEXT if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.4 else "#FFFFFF"


# ---------- an axes: a panel's box, its data limits, and what is drawn in it ----------
class Axes:
    """A panel: its box on the figure (`rect` in figure fractions, from the bottom left, as the layout is given), its x limits (epoch
    seconds: real time, so a day of 23 or 25 hours is drawn that long) and y limits, and the drawing in it, kept with a draw order
    and drawn by draw(), lowest first."""

    def __init__(self, canvas: Canvas, rect: list[float], x0: float = 0.0, x1: float = 1.0):
        self.canvas = canvas
        self.left, self.right = rect[0] * canvas.width, (rect[0] + rect[2]) * canvas.width
        self.top, self.bottom = (1 - rect[1] - rect[3]) * canvas.height, (1 - rect[1]) * canvas.height
        self.x0, self.x1, self.y0, self.y1 = x0, x1, 0.0, 1.0
        self.flipped = False
        self.ops: list[tuple[float, int, object]] = []
        canvas.panels += 1
        self.id = canvas.panels
        self.yticks: tuple | None = None   # (ticks, format, colour, side)

    def get_ylim(self) -> tuple[float, float]:
        return self.y0, self.y1

    def set_ylim(self, y0: float, y1: float):
        self.y0, self.y1 = y0, y1

    @property
    def height_pt(self) -> float:
        return (self.bottom - self.top) / PT

    def X(self, x: float) -> float:
        return self.left + (x - self.x0) / (self.x1 - self.x0) * (self.right - self.left)

    def Y(self, y: float) -> float:
        share = (y - self.y0) / ((self.y1 - self.y0) or 1)
        return self.top + share * (self.bottom - self.top) if self.flipped else self.bottom - share * (self.bottom - self.top)

    def fx(self, share: float) -> float:
        """A share of the panel's width, from its left."""
        return self.left + share * (self.right - self.left)

    def fy(self, share: float) -> float:
        """A share of the panel's height, from its bottom."""
        return self.bottom - share * (self.bottom - self.top)

    def add(self, z: float, op):
        """Draw `op` (a function of no arguments) in order `z` with the rest of this panel."""
        self.ops.append((z, len(self.ops), op))

    def draw(self):
        for _, seq, op in sorted(self.ops, key=lambda o: (o[0], o[1])):
            self.canvas.current = (self.id, seq)
            op()
        self.canvas.current = ()


def _style_axis(ax: Axes, ticks: list[float], fmt, colour: str = MUTED):
    """The plain look shared by every chart: no frame but the baseline, faint horizontal grid, small muted ticks."""
    c = ax.canvas
    shown = [t for t in ticks if min(ax.y0, ax.y1) - 1e-9 <= t <= max(ax.y0, ax.y1) + 1e-9]
    ax.add(0.5, lambda: [c.line([(ax.left, ax.Y(t)), (ax.right, ax.Y(t))], GRID, 0.8, round_ends=False) for t in shown])
    ax.add(0.5, lambda: [c.text(fmt(t), ax.left - TICK_PAD * PT, ax.Y(t), TICK_FONT, colour, ha="right", va="center", record=False)
                         for t in shown])
    ax.add(2.5, lambda: c.line([(ax.left, ax.bottom), (ax.right, ax.bottom)], AXIS, 0.8, round_ends=False))


def _right_ticks(ax: Axes, ticks: list[float], colour: str):
    shown = [t for t in ticks if min(ax.y0, ax.y1) - 1e-9 <= t <= max(ax.y0, ax.y1) + 1e-9]
    ax.add(0.5, lambda: [ax.canvas.text(f"{t + 0.0:g}", ax.right + TICK_PAD * PT, ax.Y(t), TICK_FONT, colour, va="center", record=False)
                         for t in shown])


def _shade_zones(ax: Axes, zones, ybottom: float, ytop: float, alpha: float, zorder: float = 0):
    """Faint rating zones behind a line: good up to z1, poor up to z2, very poor above."""
    z1, z2 = zones
    for lo, hi, colour in ((ybottom, z1, ZONE_COLOURS[0]), (z1, z2, ZONE_COLOURS[1]), (z2, ytop, ZONE_COLOURS[2])):
        if hi > ybottom and lo < ytop:
            a, b = max(lo, ybottom), min(hi, ytop)
            ax.add(zorder, lambda a=a, b=b, colour=colour: ax.canvas.rect(ax.left, ax.Y(b), ax.right, ax.Y(a), colour, alpha))


def _pad_limits(ax: Axes, lo: float, hi: float, top: float, bottom: float, floor: float | None):
    """Room above and below the data; `floor`: the axis does not go below it when the data does not (no negative wind)."""
    span = max(hi - lo, 1.0)
    y0 = lo - span * bottom
    ax.set_ylim(y0 if floor is None or lo < floor else max(y0, floor), hi + span * top)


def _extent(lines: list[Line], extra: list[float] = ()) -> tuple[float, float]:
    """The lowest and highest value drawn: the bands where there are any, else the lines (and `extra`, the records)."""
    return (min([min(s.low or s.y) for s in lines] + list(extra)), max([max(s.high or s.y) for s in lines] + list(extra)))


def _interp(x: float, xs: list[float], ys: list[float]) -> float:
    """The line's value at `x` (straight between its points; its first or last value beyond them)."""
    i = bisect_left(xs, x)
    if i <= 0:
        return ys[0]
    if i >= len(xs):
        return ys[-1]
    x0, x1 = xs[i - 1], xs[i]
    return ys[i - 1] + (ys[i] - ys[i - 1]) * ((x - x0) / (x1 - x0) if x1 != x0 else 0)


def _dot(ax: Axes, x: float, y: float, colour: str, size: float = 12, edge: float = 0.8, z: float = 4):
    """A small dot with a white rim, marking a point on a line (`size` is its area in pt², as a marker's is given)."""
    ax.add(z, lambda: ax.canvas.dot(ax.X(x), ax.Y(y), math.sqrt(size), colour, edge))


def _draw_lines(ax: Axes, lines: list[Line], look: Look, first: int = 0, reading: str = "") -> list[tuple]:
    """Each line's low-to-high range behind it, then the line (crisp, no glow or fill) and a dot on its end: [(colour, xs, ys)].
    `first` is the colour index of the first line (the fallback colours cycle across a stack's panels)."""
    drawn = []
    dense = max(len(s.x) for s in lines) > 200
    for i, s in enumerate(lines, first):
        colour = _colour(s, i, reading)
        xs, ys = [float(x) for x in s.x], [float(y) for y in s.y]
        if s.low:
            ax.add(3, lambda s=s, colour=colour, xs=xs: ax.canvas.polygon(
                [(ax.X(x), ax.Y(h)) for x, h in zip(xs, s.high)] + [(ax.X(x), ax.Y(lo)) for x, lo in zip(reversed(xs), reversed(s.low))],
                colour, 0.2))
        w = 1.3 if len(lines) > 2 or s.low else 1.5 if dense else 2.2   # thinner where there are many lines, a band or many points
        ax.add(4, lambda xs=xs, ys=ys, colour=colour, w=w: ax.canvas.line([(ax.X(x), ax.Y(y)) for x, y in zip(xs, ys)], colour, w))
        _dot(ax, xs[-1], ys[-1], colour, look.end_dot, look.end_rim, 5)
        drawn.append((colour, xs, ys))
    return drawn


def _columns(ax: Axes, x, heights, width: float, bottom=0.0, colour: str = RAIN, zorder: float = 3, share: bool = False):
    """Bars from each x to x + width (seconds), `heights` up from `bottom` (one value, or one per bar); share=True: heights are shares
    of the panel's height instead of data."""
    bottoms = bottom if isinstance(bottom, (list, tuple)) else [bottom] * len(x)
    y = ax.fy if share else ax.Y

    def drawn():
        for xi, h, b in zip(x, heights, bottoms):
            if h:
                ax.canvas.rect(ax.X(xi), y(b), ax.X(xi + width), y(b + h), colour)
    ax.add(zorder, drawn)


def _draw_bars(ax: Axes, bx, ys, width: float, axis_top: float, zorder: float, alpha: float = 0.5, share: bool = False):
    """Rain bars: a pale body (RAIN blended with white by `alpha`) with a brighter cap, so each reads as a little column."""
    ys = [float(y) for y in ys]
    _columns(ax, bx, ys, width, 0.0, _blend(RAIN, 1 - alpha), zorder, share)
    cap = [math.copysign(min(axis_top * 0.014, abs(y)), y) for y in ys]   # at the bar's far end, below zero as above it
    _columns(ax, bx, cap, width, [y - c for y, c in zip(ys, cap)], _blend(RAIN, 0.05), zorder, share)


def _end_of(xs, width: float, default: float) -> float:
    """Where the last bar ends on the time axis (`width` in seconds), else `default` when there are no bars."""
    return float(max(xs) + width) if len(xs) else default


def _bars_behind(ax: Axes, bars) -> float:
    """Bars (rain) behind the panel's lines and over its grid, on a scale of their own read on the right: the tallest reaches
    BARS_SHARE of the panel's height. The scale's two labels sit where a right-hand axis would put them. Returns where the last
    bar ends."""
    top = max([*bars.y, 1.0])
    _draw_bars(ax, bars.x, [y * BARS_SHARE / top for y in bars.y], _bar_width(bars.width), 1.0, 1, share=True)
    ax.add(1, lambda: ax.canvas.line([(ax.left, ax.fy(BARS_SHARE)), (ax.right, ax.fy(BARS_SHARE))], RAIN, 0.7, dots=DOTTED, alpha=0.8))
    for y, label in ((0, "0"), (BARS_SHARE, f"{top:g} {bars.unit}")):
        ax.add(1, lambda y=y, label=label: ax.canvas.text(label, ax.right + 4 * PT, ax.fy(y), 7, RAIN_TEXT, va="center", record=False))
    return _end_of(bars.x, bars.width, 0.0)


def _axes_width(chart: Chart) -> float:
    """The plot's width: narrower when a panel has rain's scale or end labels on its right, to leave room for them."""
    return AX_RECT[2] - (0.07 if any(p.lines for p in chart.panels) else 0)


def _headline(canvas: Canvas, title: str, subtitle: str, height: float, unit: str = ""):
    """Title and subtitle at the top left; a unit that is not a degree goes in the title (ticks stay plain numbers). A title too
    wide for the plots below loses "Rain" (drawn behind a line), then names the first readings "and N more"."""
    title = f"{title} ({unit})" if unit and _deg(unit) != "°" else title
    room = AX_RECT[2] * canvas.width
    weight = draw.FONTS.title_weight
    fits = lambda t: canvas.measure(t, 13, weight)[0] <= room
    names = title.split(", ")
    shown = title
    if not fits(shown):
        names = [n for n in names if n != "Rain"]   # cut: "N more" counts the panels with their own heading, and rain is drawn behind a line
        shown = ", ".join(names)
        kept = len(names)
        while not fits(shown) and kept > 2:   # a long list of readings: the first few "and N more"
            kept -= 1
            shown = ", ".join(names[:kept]) + f" and {len(names) - kept} more"
    canvas.text(shown, AX_RECT[0] * canvas.width, TITLE_IN * DPI, 13, TEXT, weight, va="center")
    canvas.text(subtitle, AX_RECT[0] * canvas.width, SUBTITLE_IN * DPI, 8.5, MUTED, va="center")


TIME_TICKS = (   # a period of up to this many days: where its ticks fall (local time) and the labels' strftime format
    (1.1, "hours", "%-I%p"),
    (4, "half days", "%a %-I%p"),
    (45, "days", "%a\n%-d %b"),
    (400, "months", "%b\n'%y"),
    (math.inf, "quarters", "%b\n'%y"),
)


def _local_ticks(x0: float, x1: float, span_days: float, tz: tzinfo, every: str) -> list[float]:
    """Epochs in [x0, x1] that fall on local hours (every 3), half days, days (every few, for a long span), months or quarters.
    Stepped by real hours or by local dates, so the night the clocks change has its ticks on the local hours all the same."""
    if every in ("hours", "half days"):
        hours = range(0, 24, 3) if every == "hours" else (0, 12)
        out, t = [], math.ceil(x0 / 3600) * 3600
        while t <= x1:
            local = datetime.fromtimestamp(t, tz)
            if local.minute == 0 and local.hour in hours:
                out.append(float(t))
            t += 3600
        return out
    day = datetime.fromtimestamp(x0, tz).date()
    step = max(1, math.ceil(span_days / 8)) if every == "days" else 1
    out = []
    while True:
        at = datetime(day.year, day.month, day.day, tzinfo=tz).timestamp()
        if at > x1:
            return out
        if at >= x0 and (every == "days" or day.day == 1 and (every == "months" or day.month in (1, 4, 7, 10))):
            out.append(at)
            if every == "days":
                day += timedelta(days=step)
                continue
        day += timedelta(days=1)


def _time_ticks(x0: float, x1: float, span_days: float, tz: tzinfo, bars=None) -> list[tuple[float, str]]:
    """(epoch, label) of each tick of a time axis, chosen by the length of the period. Bars of whole months or years (a count per
    month or year) are labelled under the middle of each bar instead. The ticks fall on local hours, days and months."""
    if bars and bars.per in ("year", "month") and bars.x and (bars.per == "year" or len(bars.x) <= 14):
        fmt = "%Y" if bars.per == "year" else "%b\n'%y"
        ticks = [x + _bar_width(bars.width) / 2 for x in bars.x]
    else:
        every, fmt = next((every, fmt) for limit, every, fmt in TIME_TICKS if span_days <= limit)
        ticks = _local_ticks(x0, x1, span_days, tz, every)
    return [(t, datetime.fromtimestamp(t, tz).strftime(fmt).replace("AM", "am").replace("PM", "pm")) for t in ticks if x0 <= t <= x1]


def _time_axis(ax: Axes, span_days: float, tz: tzinfo, bars=None):
    """The time axis's labels under the bottom panel."""
    ticks = _time_ticks(ax.x0, ax.x1, span_days, tz, bars)
    ax.add(0.5, lambda: [ax.canvas.text(label, ax.X(t), ax.bottom + TICK_PAD * PT, TICK_FONT, MUTED, ha="center", va="top", record=False)
                         for t, label in ticks])


def _legend(canvas: Canvas, entries: list[tuple[str, str]], at: tuple[float, float], loc: str, size: float = 6, marker: str = "o",
            ink: str = TEXT, spacing: float = 1.1, fontsize: float = 10, ncol: int = 1, pad: float = 0.5):
    """A row (or `ncol` columns) of coloured markers (dots; squares for shares and the rose's speeds), each with its name, unframed,
    placed by its corner or side `loc` ("center right", "upper right", "lower right") at `at` (pixels), `pad` font sizes in from it."""
    fs = fontsize * PT
    handle, gap, col_gap, row = 2.0 * fs, 0.2 * fs, spacing * fs, fs * 1.5
    widths = [handle + gap + canvas.measure(label, fontsize)[0] for _, label in entries]
    rows = math.ceil(len(entries) / ncol)
    columns = [widths[c * rows:(c + 1) * rows] for c in range(ncol)] if ncol > 1 and rows > 1 else \
        [[w] for w in widths] if ncol > 1 else [widths]
    col_w = [max(c) for c in columns if c]
    box_w = sum(col_w) + col_gap * (len(col_w) - 1) + 0.8 * fs
    box_h = rows * row + 0.8 * fs - 0.5 * fs
    right = at[0] - pad * fs
    if loc == "center right":
        top = at[1] - box_h / 2
    elif loc == "upper right":
        top = at[1] + pad * fs
    else:   # lower right
        top = at[1] - pad * fs - box_h
    x = right - box_w + 0.4 * fs
    k = 0
    for c, w in enumerate(col_w):
        for r in range(rows if (ncol > 1 and rows > 1) or ncol == 1 else 1):
            if k >= len(entries):
                break
            colour, label = entries[k]
            y = top + 0.4 * fs + r * row + fs / 2
            cx = x + handle / 2
            if marker == "s":
                half = size * PT / 2
                canvas.rect(cx - half, y - half, cx + half, y + half, colour)
            else:
                canvas.circle(cx, y, size * PT / 2, colour)
            canvas.text(label, x + handle + gap, y, fontsize, ink, va="center", record=False)
            k += 1
        x += w + col_gap


def _box_of(canvas: Canvas, text: str, x: float, y: float, size: float, ha: str, va: str, pad: float):
    """The extent of a label (with `pad` pt around it) whose text is placed at (x, y)."""
    w, _ = canvas.measure(text, size, "bold")
    h = size * PT
    left = x if ha == "left" else x - w if ha == "right" else x - w / 2
    top = y if va == "top" else y - h if va == "bottom" else y - h / 2
    p = pad * PT
    return left - p, top - p, left + w + p, top + h + p


def _leader(canvas: Canvas, box, to: tuple[float, float], colour: str, width: float, dots=None, shrink_a: float = 0.0,
            shrink_b: float = 2.0, alpha: float = 1.0):
    """A thin line from the edge of a label's box to the point it labels, a little short of both."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    dx, dy = to[0] - cx, to[1] - cy
    length = math.hypot(dx, dy)
    if not length:
        return
    exits = [t for t in ((box[0] - cx) / dx if dx < 0 else (box[2] - cx) / dx if dx > 0 else math.inf,
                         (box[1] - cy) / dy if dy < 0 else (box[3] - cy) / dy if dy > 0 else math.inf)]
    t = min(exits)
    if t >= 1:
        return
    start = t * length + shrink_a * PT
    end = length - shrink_b * PT
    if end <= start:
        return
    ux, uy = dx / length, dy / length
    canvas.line([(cx + ux * start, cy + uy * start), (cx + ux * end, cy + uy * end)], colour, width, dots=dots, alpha=alpha,
                round_ends=False)


def _pill(ax: Axes, text: str, x: float, y: float, colour: str, look: Look, ha: str = "center", va: str = "center", z: float | None = None,
          leader_to: tuple[float, float] | None = None, va_name: str | None = None):
    """A value on a rounded pill of its line's colour, its text placed at (x, y) pixels; a dotted leader to `leader_to` when given."""
    c = ax.canvas
    size, (pad, rounding) = look.pill_font, look.pill_box

    def drawn():
        box = _box_of(c, text, x, y, size, ha, va, pad * size)
        if leader_to:
            _leader(c, box, leader_to, colour, 0.8, DOTTED, 1, 2)
        c.polygon(draw.rounded_rect(*box, rounding * size * PT), colour)
        middle = (box[1] + box[3]) / 2
        c.text(text, {"left": box[0] + pad * size * PT, "right": box[2] - pad * size * PT}.get(ha, (box[0] + box[2]) / 2), middle,
               size, _ink_on(colour), "bold", ha=ha, va="center", record=False)
        c.texts.append(draw.Text(text, x, y, ha, va_name or va, box, c.current))
    ax.add(look.pill_z + 1 if z is None else z, drawn)


def _pills(ax: Axes, lines: list[Line], drawn: list[tuple], x0: float, x1: float, look: Look, deg: str = ""):
    """A dot on the highest and the lowest point of each line with a pill of its value above or below it. The point is the top
    (bottom) of the line's band when it has one, else its true record at its actual time (a dotted stem joins it to a line that
    does not reach it). A low on the floor says nothing and is not labelled; pills that land close
    together go side by side."""
    y_lo, y_hi = ax.get_ylim()
    edge = lambda x: "left" if (x - x0) / (x1 - x0) < 0.06 else "right" if (x - x0) / (x1 - x0) > 0.94 else "center"
    pills = []
    for s, (colour, xs, ys) in zip(lines, drawn):
        for want, above in (("high", True), ("low", False)):
            if want in s.records and s.low is not None and not s.on_line:  # a banded line: the label sits on the top (bottom) of its band
                rx, ry = _extreme(s, want, xs)
            elif want in s.records:  # the true record, at its actual time (may sit off an averaged line)
                rx, ry = float(s.records[want][0]), float(s.records[want][1])
                rx = min(max(rx, x0), x1)
            else:  # only some records given (wind: the strongest gust, no lowest): no label for the other
                continue
            if want == "low" and (ry - y_lo) < 0.08 * (y_hi - y_lo):
                continue  # a low on the floor (0 mm, 0 km/h) says nothing
            line_y = _interp(rx, xs, ys)
            if abs(ry - line_y) > (0.005 * (y_hi - y_lo) if s.smoothed else 1e-6) and not s.low:
                a, b = min(ry, line_y), max(ry, line_y)   # well off the line: a dotted stem back to it
                ax.add(3, lambda rx=rx, a=a, b=b, colour=colour: ax.canvas.line([(ax.X(rx), ax.Y(a)), (ax.X(rx), ax.Y(b))], colour, 1.2,
                                                                             dots=DOTTED, alpha=0.8))
            _dot(ax, rx, ry, colour, look.pill_dot, look.pill_rim, look.pill_z)
            text = f"{ry:.1f}{deg}" if look.units else f"{round(ry, 1):g}"
            pills.append([rx, ry, text, colour, above or (ry - y_lo) < 0.16 * (y_hi - y_lo), edge(rx)])  # a low near the floor: pill above
    for a in range(len(pills)):
        for b in range(a + 1, len(pills)):
            p, q = pills[a], pills[b]
            if p[4] == q[4] and abs(p[0] - q[0]) < (x1 - x0) * 0.08 and abs(p[1] - q[1]) < (y_hi - y_lo) * 0.15:
                left, right = (p, q) if p[0] <= q[0] else (q, p)
                if (left[0] - x0) / (x1 - x0) < 0.12 or (x1 - right[0]) / (x1 - x0) < 0.12:  # no room to push sideways
                    upper, lower = (p, q) if p[1] >= q[1] else (q, p)   # one above its point, the other below, whatever side both began on
                    upper[4] = True
                    if lower[1] - y_lo >= 0.16 * (y_hi - y_lo):   # not on the floor, where a pill below would run under the axis
                        lower[4] = False
                else:
                    left[5], right[5] = "right", "left"
    for rx, ry, text, colour, above, ha in pills:
        lift = look.pill_lift * PT
        _pill(ax, text, ax.X(rx), ax.Y(ry) - lift if above else ax.Y(ry) + lift, colour, look, ha=ha, va="bottom" if above else "top")


def _rose_radius(outer: list[list[float]]) -> float:
    """The rose's radius for wedges ending at `outer` (16 compass points x 3 speed steps, out from the centre): 5% beyond the
    longest wedge."""
    longest = max(row[-1] for row in outer)
    return longest * 1.05 if longest else 1.0


def _render_rose(canvas: Canvas, compass: Compass) -> Axes:
    """The whole period as a wind rose (N up, clockwise): each of the 16 wedges is the share of readings from that way, stacked
    by wind speed. It sits beside the wind speed line, in a square centred in ROSE_RECT."""
    ax = Axes(canvas, ROSE_RECT)
    size = min(ax.right - ax.left, ax.bottom - ax.top)
    cx, cy, r_px = (ax.left + ax.right) / 2, (ax.top + ax.bottom) / 2, size / 2
    total = max(sum(map(sum, compass.rose)), 1)
    outer = []
    for counts in compass.rose:                                            # where each step ends, out from the centre
        acc, row = 0.0, []
        for v in counts:
            acc += v / total * 100
            row.append(acc)
        outer.append(row)
    radius = _rose_radius(outer)
    scale = r_px / radius

    def wedges():
        for i in range(3):                                                 # step by step, each over the one before
            for k in range(16):
                inner, end = (outer[k][i - 1] if i else 0.0), outer[k][i]
                if end <= inner:
                    continue
                a0, a1 = 90 - (22.5 * k + 10), 90 - (22.5 * k - 10)       # 20° wide around each compass point, clockwise from N
                shape = draw.arc(cx, cy, end * scale, a0, a1, 12) + draw.arc(cx, cy, inner * scale, a1, a0, 12 if inner else 0)
                canvas.polygon(shape, WIND_STEPS[i], outline=BG, width=0.4)

    def grid():
        for ring in nice_ticks(0, radius, 4)[1:]:
            canvas.circle(cx, cy, ring * scale, outline=GRID, width=0.6)
        for a in (0, 90, 180, 270):
            canvas.line([(cx, cy), (cx + r_px * math.sin(math.radians(a)), cy - r_px * math.cos(math.radians(a)))], GRID, 0.6,
                        round_ends=False)
        for a, name in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
            far = r_px + 9 * PT
            canvas.text(name, cx + far * math.sin(math.radians(a)), cy - far * math.cos(math.radians(a)), 8, MUTED, ha="center",
                        va="center", record=False)
    ax.add(1, wedges)
    ax.add(1.5, grid)
    if compass.speeds:
        a, b = compass.speed_steps
        entries = list(zip(WIND_STEPS, [f"under {a}", f"{a}–{b}", f"{b}+ km/h"]))
        ax.add(5, lambda: _legend(canvas, entries, (0.985 * canvas.width, canvas.height), "lower right", marker="s", ink=MUTED,
                                  spacing=0.9, fontsize=8.5, ncol=3))
    return ax


# ---------- several panels on one time axis ----------
def _extreme(line: Line, want: str, xs) -> tuple[float, float]:
    """Where the drawn line is highest (or lowest): the top (bottom) of its band when it has one, else the line itself. A
    label sits on what is drawn, never on a raw reading the chart does not reach. `xs` are the line's x."""
    ys = (line.high if want == "high" else line.low) if line.low is not None and not line.on_line else line.y
    pick = max if want == "high" else min
    i = pick(range(len(ys)), key=lambda k: ys[k])
    return float(list(xs)[i]), float(ys[i])


def _mark_highs(ax: Axes, lines: list[Line], drawn: list[tuple], x0: float, x1: float, look: Look):
    """The peak of each line in a multi-line panel: a dot on the peak, and its value in a pill placed in empty space (the
    top of the panel where no line reaches, else the margin) joined to the dot by a thin dotted line. `lines` and `drawn` are
    the lines with a peak record and what was drawn for them."""
    y_lo, y_hi = ax.get_ylim()
    per_pt = (y_hi - y_lo) / ax.height_pt                                 # data units in one point
    peaks = []
    for line, (colour, xs, _) in zip(lines, drawn):
        if "high" in line.records:
            mx, my = _extreme(line, "high", xs)
            peaks.append((mx, my, colour))
    peaks.sort(key=lambda p: -p[1])                                       # the highest peak gets the top pill
    pitch = look.pitch                                                    # the pills' row spacing, in points
    groups = []                                                           # peaks at about the same time share a column, stacked
    for peak in peaks:                                                    # (highest first)
        near = next((g for g in groups if abs(g[0][0] - peak[0]) < 0.05 * (x1 - x0)), None)
        near.append(peak) if near else groups.append([peak])
    block = (10 + pitch * max(len(g) for g in groups)) * per_pt if groups else 0   # the height the tallest stack of pills needs
    reach = 0.06 * (x1 - x0)                                              # half a pill's width, in x units
    tops = [(xs, list(line.high if line.low is not None else line.y)) for line, (_, xs, _) in zip(lines, drawn)]   # bands included
    free = []
    for k in range(3, 98, 2):                                             # candidate columns across the panel
        cx = x0 + (x1 - x0) * k / 100
        highest = max((max([y for x, y in zip(xs, ys) if cx - reach < x < cx + reach], default=y_lo) for xs, ys in tops), default=y_lo)
        if highest < y_hi - block - 4 * per_pt:
            free.append(cx)
    columns, taken = [], []                                               # each group in the empty column nearest its own peaks
    for g in groups:
        mx = fmean(p[0] for p in g)
        options = [c for c in free if abs(c - mx) > LEADER_CLEARANCE * (x1 - x0) and all(abs(c - t) > 2.2 * reach for t in taken)]
        if not options:
            break
        columns.append(min(options, key=lambda c: abs(c - mx)))
        taken.append(columns[-1])
    if len(columns) == len(groups):
        for g, cx in zip(groups, columns):
            for i, (mx, my, colour) in enumerate(g):
                _dot(ax, mx, my, colour, look.pill_dot, look.pill_rim, look.pill_z)
                _pill(ax, f"{round(my, 1):g}", ax.X(cx), ax.Y(y_hi - (10 + pitch * i) * per_pt), colour, look,
                      leader_to=(ax.X(mx), ax.Y(my)))
        return
    placed = []                                                           # no empty column: the right margin, at the peaks' heights
    for mx, my, colour in peaks:
        ly = min(my, placed[-1] - pitch * per_pt) if placed else my
        placed.append(ly)
        _dot(ax, mx, my, colour, look.pill_dot, look.pill_rim, look.pill_z)
        _pill(ax, f"{round(my, 1):g}", ax.fx(1.03), ax.Y(ly), colour, look, ha="left", leader_to=(ax.X(mx), ax.Y(my)))


def _figure(value: float) -> str:
    """A latest value as written beside a line: three significant figures, but never in exponent form (1014.8, not 1.01e+03)."""
    return f"{value:.1f}".rstrip("0").rstrip(".") if abs(value) >= 100 else f"{value:.3g}"


def _end_labels(ax: Axes, drawn: list[tuple]):
    """The latest value of each line in the margin beside it, nudged apart where they would touch, with a thin leader to its end."""
    y_lo, y_hi = ax.get_ylim()
    gap = 10 * (y_hi - y_lo) / ax.height_pt                 # 10 points, in data units
    placed = []
    for colour, xs, ys in sorted(drawn, key=lambda d: d[2][-1]):
        y = max(ys[-1], placed[-1] + gap) if placed else ys[-1]
        placed.append(y)

        def label(colour=colour, xs=xs, ys=ys, y=y):
            text, at = _figure(ys[-1]), (ax.fx(1.014), ax.Y(y))
            box = _box_of(ax.canvas, text, *at, 8.5, "left", "center", 0.15 * 8.5)
            _leader(ax.canvas, box, (ax.X(xs[-1]), ax.Y(ys[-1])), colour, 0.6, alpha=0.6)
            ax.canvas.text(text, *at, 8.5, colour, "bold", va="center")
        ax.add(3, label)


def _draw_panel(ax: Axes, p: Panel, first: int, x0: float, x1: float, look: Look, canvas: Canvas):
    """One panel: shares, bars on their own (rain, counts), or lines (with bands and zones, peaks labelled, rain behind them).
    Returns the number of colours used, the legend's entries (colour, name) and the panel's second axis (or None)."""
    if p.shares:
        return _draw_shares(ax, p.shares)
    if p.bars_only:
        return _draw_bars_alone(ax, p.bars)
    return _draw_readings(ax, p, first, x0, x1, look, canvas)


def _draw_shares(ax: Axes, s):
    """The share of each bar's time in good / poor / very poor: traffic-light bars stacked to 100%."""
    base = [0.0] * len(s.x)
    for column, colour in zip((s.good, s.poor, s.very_poor), ZONE_COLOURS):
        _columns(ax, s.x, list(column), _bar_width(s.width), list(base), colour, 3)
        base = [b + float(v) for b, v in zip(base, column)]
    ax.set_ylim(0, 100)
    entries = list(zip(ZONE_COLOURS, ["good", "poor", "very poor"]))
    ax.add(5, lambda: _legend(ax.canvas, entries, (ax.right, ax.top), "lower right", marker="s", size=5, spacing=0.9, fontsize=8.5,
                              ncol=3, pad=0.1))
    return 0, [], None


def _draw_bars_alone(ax: Axes, b):
    """Rain on its own, or a count or figure per month or year (a few bars of them get their number on the end)."""
    top = max([*b.y, 1.0]) * 1.15
    bottom = min([*b.y, 0.0]) * 1.3   # a figure can be below zero (a coldest night)
    ax.set_ylim(bottom, top)
    _draw_bars(ax, b.x, b.y, _bar_width(b.width), top, 3, alpha=0.75)
    if b.values:
        for x, y in zip(b.x, b.y):
            ax.add(5, lambda x=x, y=y: ax.canvas.text(f"{y:g}", ax.X(x + _bar_width(b.width) / 2),
                                                      ax.Y(y + (top - bottom) * (0.015 if y >= 0 else -0.015)), 8, TEXT, ha="center",
                                                      va="bottom" if y >= 0 else "top"))
    return 0, [], None


def _right_axis(ax: Axes, lines: list[Line], first: int, look: Look, flip: bool = False) -> tuple:
    """The lines of a panel's second scale, on an axis of their own at the right (ticks in the first line's colour, no frame, no
    grid: the left axis keeps the grid). Returns (the axis, what was drawn on it as _draw_lines gives it)."""
    twin = Axes(ax.canvas, [0, 0, 1, 1], ax.x0, ax.x1)
    twin.left, twin.right, twin.top, twin.bottom = ax.left, ax.right, ax.top, ax.bottom
    twin.flipped = flip
    _pad_limits(twin, *_extent(lines), top=0.12, bottom=0.12, floor=0)
    drawn = _draw_lines(twin, lines, look, first)
    _right_ticks(twin, nice_ticks(twin.y0, twin.y1, look.nbins), drawn[0][0])
    return twin, drawn


def _own_end_labels(ax: Axes, drawn: list[tuple], above: bool):
    """The latest value of each line beside its own end dot, above it (the left scale) or below it (the right): a panel with two scales
    has its right margin taken by the second scale's ticks."""
    for colour, xs, ys in drawn:
        ax.add(3, lambda colour=colour, xs=xs, ys=ys: ax.canvas.text(
            _figure(ys[-1]), ax.X(xs[-1]) - 5 * PT, ax.Y(ys[-1]) + (-8 if above else 8) * PT, 8.5, colour, "bold", ha="right",
            va="bottom" if above else "top"))


def _draw_readings(ax: Axes, p: Panel, first: int, x0: float, x1: float, look: Look, canvas: Canvas):
    """The panel's lines, each with its band, the rating zones of a rated reading behind it, the peaks labelled (as pills on the
    lines, or aside), rain behind them on a scale of its own, and the latest values beside them. Lines marked `right` are on a
    second scale of their own."""
    lines = [s for s in p.lines if not s.right]
    on_right = [s for s in p.lines if s.right]
    marked = [i for i, s in enumerate(lines) if s.records and p.peaks != "none"]
    pilled = bool(marked) and p.peaks == "pills"
    _pad_limits(ax, *_extent(lines, [float(r[1]) for s in lines for r in s.records.values()] if pilled else []),
                top=look.pad if pilled else 0.18 if marked else 0.12, bottom=look.pad if pilled else 0.12,
                floor=None if _deg(p.unit) == "°" else 0)   # a temperature can go below zero; nothing else drawn as a line can
    ybottom, ytop = ax.get_ylim()
    drawn = _draw_lines(ax, lines, look, first, p.reading)
    if p.zones:  # a rated reading: its good / poor / very poor zones behind the line (and over any rain behind it, as always)
        _shade_zones(ax, p.zones, ybottom, ytop, 0.07, 1.5 if p.bars else 0)
    if marked and p.peaks == "aside":
        _mark_highs(ax, [lines[i] for i in marked], [drawn[i] for i in marked], x0, x1, look)
    elif marked:
        _pills(ax, [lines[i] for i in marked], [drawn[i] for i in marked], x0, x1, look, _deg(p.unit))
    if p.bars:
        _bars_behind(ax, p.bars)
    twin = None
    if on_right:
        twin, drawn_right = _right_axis(ax, on_right, first + len(lines), look, p.flip_right)
        _own_end_labels(ax, drawn, above=True)
        _own_end_labels(twin, drawn_right, above=False)
        drawn += drawn_right
    else:
        _end_labels(ax, drawn)   # every line ends with its latest value, a zero (night, calm) included
    entries = [(c, s.label) for (c, _, _), s in zip(drawn, [*lines, *on_right])] + ([(RAIN, p.bars.label)] if p.bars else [])
    return len(lines) + len(on_right), entries, twin


def _x_range(panels: list[Panel]) -> tuple[float, float]:
    """The first and last time drawn: every point, and the end of the last bar of any panel's bars."""
    xs = [t for p in panels for t in p.xs]
    x0, x1 = float(min(xs)), float(max(xs))
    for p in panels:
        for b in (p.bars, p.shares):
            if b and b.x:
                x1 = max(x1, _end_of(b.x, b.width, x1))
    return x0, x1


def _build(chart: Chart, tz: tzinfo) -> Canvas:
    """The panels top to bottom on one time axis, so the rain (or another reading) lines up with what the others were doing.
    A chart of one panel of lines is the same thing drawn BIG (and with its wind rose beside it, when it has one)."""
    panels, n = chart.panels, len(chart.panels)
    look = _look_for(chart)
    height = H_IN if n <= 2 else H_IN + PANEL_IN * (n - 2)
    canvas = Canvas(round(W_IN * DPI), round(height * DPI), BG)
    width = BESIDE_ROSE_WIDTH if chart.compass else _axes_width(chart)
    head = HEAD_IN + (STACKED_KEY_IN if chart.single and _stacked_key(panels[0], look) else 0)
    body = height - head - FOOT_IN
    gap = 0.27 if n > 2 else 0.22
    each = (body - gap * (n - 1)) / n
    x0, x1 = _x_range(panels)
    margin = (x1 - x0) * 0.015 or 1.0   # room for the end dots
    axes, used, later = [], 0, []
    for i, p in enumerate(panels):
        bottom = (FOOT_IN + (n - 1 - i) * (each + gap)) / height
        ax = Axes(canvas, [AX_RECT[0], bottom, width, each / height], x0 - margin, x1 + margin)
        if look.card:   # the faint tint behind the panel
            canvas.rect(ax.left, ax.top, ax.right, ax.bottom, CARD)
        axes.append(ax)
        count, entries, twin = _draw_panel(ax, p, used, x0, x1, look, canvas)
        used += count
        deg = _deg(p.unit)
        tick_unit = deg if deg == "°" else ""
        ticks = nice_ticks(ax.y0, ax.y1, 3 if n > 2 else look.nbins, integer=p.bars_only and p.bars.unit == "days")
        _style_axis(ax, ticks, lambda v, u=tick_unit: f"{v + 0.0:g}{u}", twin_colour(entries) if twin else MUTED)
        if twin:
            later.append(twin)
        if len(entries) >= look.key_from:   # the colour key: beside the headline for a big chart, above the panel in a stack
            stacked = _stacked_key(p, look)   # long names or two scales: a column under the title, clear of it
            right = (AX_RECT[0] + AX_RECT[2]) * canvas.width
            if stacked:
                key = dict(at=(right, (TITLE_IN + 0.17) * DPI), loc="upper right", ncol=1)
            elif look.in_headline:
                key = dict(at=(right, TITLE_IN * DPI), loc="center right", ncol=len(entries))
            else:
                key = dict(at=(ax.right, ax.top), loc="lower right", ncol=len(entries), pad=0.1)
            later.append(("legend", entries, key))
        if not look.in_headline:
            title = f"{p.label} ({p.unit})" if p.unit and not tick_unit else p.label
            ax.add(5, lambda ax=ax, title=title: canvas.text(title, ax.left, ax.top - 4 * PT, 8.5, TEXT, draw.FONTS.title_weight,
                                                             record=False))
    _time_axis(axes[-1], (x1 - x0) / 86400, tz, panels[-1].bars if panels[-1].bars_only else None)
    for ax in axes:
        ax.draw()
    for item in later:
        if isinstance(item, Axes):
            item.draw()
    if chart.compass:  # beside the wind line
        _render_rose(canvas, chart.compass).draw()
    _headline(canvas, panels[0].label if look.in_headline else chart.title, chart.subtitle, height, panels[0].unit if look.in_headline else "")
    for item in later:
        if not isinstance(item, Axes):
            _, entries, key = item
            _legend(canvas, entries, fontsize=look.legend_font, **key)
    return canvas


def twin_colour(entries: list[tuple[str, str]]) -> str:
    """The left scale's tick colour in a panel with two scales: its first line's."""
    return entries[0][0]


# Pillow's fonts and the drawing caches are shared between threads: one chart at a time. Its PNG is written under the lock too:
# with Python's one-thread-at-a-time interpreter, writing it beside the next chart's drawing only slows both.
_DRAWING = threading.Lock()


def render(chart: Chart, tz: tzinfo, times: dict | None = None) -> bytes:
    """PNG bytes for one chart (1280x720; a stack of more than two panels is taller). Safe to call from several threads.
    times (a Turn's): the seconds spent drawing the figure and writing the PNG are added to its "draw" and "png"."""
    started = time.perf_counter()
    with _DRAWING:
        canvas = _build(chart, tz)
        drawn = time.perf_counter()
        png = canvas.png()
    if times is not None:
        times["draw"] = times.get("draw", 0.0) + drawn - started
        times["png"] = times.get("png", 0.0) + time.perf_counter() - drawn
    return png


def draw_texts(chart: Chart, tz: tzinfo) -> list[draw.Text]:
    """Every label recorded while drawing the chart (its values, pills and bar numbers), in the order drawn: for tests."""
    with _DRAWING:
        return sorted(_build(chart, tz).texts, key=lambda t: t.order)
