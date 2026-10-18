"""Charts sent alongside history answers, for both Ecowitt and AirGradient.

A history tool adds a specs.Chart to turn.charts (the question's Turn, made by the bot) when a chart was asked for; the
bot renders them after the answer is written and sends them with it. Every chart is one or more panels on a shared time
axis (see specs.py):
  - one panel of lines is drawn large, with the records marked as pills, an end dot and (for wind) the compass beside it,
    at exactly 1280x720, the size Telegram displays photos at, so nothing is rescaled;
  - several panels are stacked, the figure growing a little taller with each one past two (four panels: 1280x1160).
Rain (bars) sits behind the lines of the panel it belongs to, on a scale of its own read on the right.
"""

import glob
import io
import math
import threading
import time
from dataclasses import dataclass
from datetime import tzinfo
from statistics import fmean

import matplotlib

matplotlib.use("Agg")  # no display on the server
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.collections import PatchCollection, PolyCollection  # noqa: E402
from matplotlib.colors import to_rgb  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Polygon, Rectangle, Wedge  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, MaxNLocator  # noqa: E402
from matplotlib.transforms import ScaledTranslation  # noqa: E402
from PIL import Image  # noqa: E402

from .specs import Chart, Compass, Line, Panel  # noqa: E402
from .timeutil import to_local  # noqa: E402

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
W_IN, H_IN, DPI = 6.4, 3.6, 200  # 1280 x 720 px
AX_RECT = [0.075, 0.13, 0.905, 0.64]  # left, bottom, width, height (figure fraction) of a single chart
PANEL_IN = 1.1                   # each panel past two adds this much height (inches)
HEAD_IN, FOOT_IN = 0.9, 0.47     # room above and below the panels (inches)
LEADER_CLEARANCE = 0.05          # peak labels sit at least this share of the panel's width away from the peaks (shorter = closer)
BARS_SHARE = 0.95                # rain behind a line is drawn over the panel's full height (the tallest bar nearly reaches the top), under the line


@dataclass(frozen=True)
class Look:
    """How large the parts of a panel of lines are drawn. A chart of one panel is drawn BIG; the panels of a stack SMALL."""
    end_dot: float           # the dot on the end of each line: size, rim
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


BIG = Look(30, 1.5, 8.5, 18, 1.2, 9, (0.35, 0.8), 6, 17, 0.26, 10, 1, True, 5, False, True)
SMALL = Look(20, 1.2, 7.5, 12, 0.8, 5, (0.25, 0.6), 4, 14, 0.3, 8.5, 2, False, 4, True, False)


def _look_for(chart: Chart) -> Look:
    """A chart of one panel of lines is drawn BIG; anything else (a stack, rain alone) SMALL."""
    return BIG if len(chart.panels) == 1 and chart.panels[0].lines else SMALL


def _setup_fonts() -> str:
    """Use a modern sans font if one is installed (Inter, Roboto, Helvetica, Arial).
    Font files are registered directly, in case they were installed after
    matplotlib built its font list."""
    for pattern in ("/usr/share/fonts/**/Inter-*.[ot]tf", "/usr/share/fonts/**/Roboto-*.ttf",
                    "/usr/local/share/fonts/**/*.[ot]tf"):
        for path in glob.glob(pattern, recursive=True):
            if "Italic" not in path:
                try:
                    font_manager.fontManager.addfont(path)
                except Exception:
                    pass
    installed = {f.name for f in font_manager.fontManager.ttflist}
    family = next((n for n in ("Inter", "Roboto", "Helvetica Neue", "Arial", "Liberation Sans") if n in installed),
                  "DejaVu Sans")
    plt.rcParams.update({"font.family": family, "axes.unicode_minus": False})
    return family


FONT = _setup_fonts()
# Titles use semi-bold where the font has it (Inter, Roboto); plain bold otherwise (e.g. DejaVu Sans)
TITLE_WEIGHT = "semibold" if any(f.name == FONT and f.weight in (600, "semibold", "demibold")
                                 for f in font_manager.fontManager.ttflist) else "bold"


def _colour(line: Line, i: int, reading: str = "") -> str:
    """The line's reading's hue (indoors, its complement), the panel's reading when the line names none, else the next fallback."""
    reading = line.reading or reading
    if base := READING_COLOURS.get(reading):
        return (INDOOR_COLOURS.get(reading) or _mix(base, 0.4)) if line.indoor else base
    return FALLBACK[i % len(FALLBACK)]


def _mix(colour: str, share: float) -> str:
    """The colour with this share of white blended in."""
    return "#" + "".join(f"{round(255 * share + c * 255 * (1 - share)):02X}" for c in to_rgb(colour))


def _deg(unit: str) -> str:
    if not unit:
        return ""
    return "°" if unit.replace("º", "°") in ("°C", "°F", "°") else f" {unit}"


def _ink_on(colour: str) -> str:
    """The text colour for a label on this fill: white, or near-black on a light one (yellow, amber)."""
    r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in to_rgb(colour))
    return TEXT if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.4 else "white"


def _box(colour: str, pad: float = 0.25, rounding: float = 0.6) -> dict:
    """The rounded coloured box behind a value label."""
    return {"boxstyle": f"round,pad={pad},rounding_size={rounding}", "fc": colour, "ec": "none"}


def _dot(ax, x: float, y: float, colour: str, size: float = 12, edge: float = 0.8, z: int = 4):
    """A small dot with a white rim, marking a point on a line (`size` is its area in pt², as scatter takes it). It is a one-point
    line's marker: drawn just as a one-point scatter is, at a fraction of the cost."""
    ax.add_line(Line2D([x], [y], marker="o", linestyle="", markersize=math.sqrt(size), color=colour, markeredgecolor="white",
                       markeredgewidth=edge, zorder=z))


def _to_dt(tz: tzinfo):
    """A function turning an epoch into a naive local datetime, for matplotlib's date axis."""
    return lambda t: to_local(t, tz).replace(tzinfo=None)


def _nums(tz: tzinfo, epochs) -> np.ndarray:
    """Epochs as matplotlib date numbers (local time)."""
    to_dt = _to_dt(tz)
    return mdates.date2num([to_dt(t) for t in epochs])


def _end_of(bx, width: float, default: float) -> float:
    """Where the last bar ends on the date axis (`width` in seconds), else `default` when there are no bars."""
    return float(bx.max() + width / 86400) if len(bx) else default


def _style_axis(ax, size: float = 7.5, pad: float = 5, grid: float = 0.8, below: bool = True):
    """The plain look shared by every chart: no frame but the baseline, faint horizontal grid, small muted ticks."""
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(axis="both", length=0, labelsize=size, labelcolor=MUTED, pad=pad)
    if grid:
        ax.grid(axis="y", color=GRID, linewidth=grid)
    if below:
        ax.set_axisbelow(True)


def _shade_zones(ax, zones, ybottom: float, ytop: float, alpha: float, zorder: float = 0):
    """Faint rating zones behind a line: good up to z1, poor up to z2, very poor above."""
    z1, z2 = zones
    for lo, hi, colour in ((ybottom, z1, ZONE_COLOURS[0]), (z1, z2, ZONE_COLOURS[1]), (z2, ytop, ZONE_COLOURS[2])):
        if hi > ybottom and lo < ytop:
            ax.axhspan(max(lo, ybottom), min(hi, ytop), color=colour, alpha=alpha, linewidth=0, zorder=zorder)


def _pad_limits(ax, lo: float, hi: float, top: float = 0.2, bottom: float = 0.16, floor: float | None = None):
    """Room above and below the data; `floor`: the axis does not go below it when the data does not (no negative wind)."""
    span = max(hi - lo, 1.0)
    y0 = lo - span * bottom
    ax.set_ylim(y0 if floor is None or lo < floor else max(y0, floor), hi + span * top)


def _extent(lines: list[Line], extra: list[float] = ()) -> tuple[float, float]:
    """The lowest and highest value drawn: the bands where there are any, else the lines (and `extra`, the records)."""
    return (min([min(s.low or s.y) for s in lines] + list(extra)), max([max(s.high or s.y) for s in lines] + list(extra)))


def _draw_lines(ax, lines: list[Line], tz: tzinfo, look: Look, first: int = 0, reading: str = "") -> list[tuple]:
    """Each line's low-to-high range behind it, then the line (crisp, no glow or fill) and a dot on its end: [(colour, xs, ys)].
    `first` is the colour index of the first line (the fallback colours cycle across a stack's panels)."""
    drawn = []
    dense = max(len(s.x) for s in lines) > 200
    for i, s in enumerate(lines, first):
        colour = _colour(s, i, reading)
        xs, ys = _nums(tz, s.x), np.asarray(s.y, dtype=float)
        if s.low:
            ax.fill_between(xs, s.low, s.high, color=colour, alpha=0.2, linewidth=0, zorder=3)
        w = 1.3 if len(lines) > 2 or s.low else 1.5 if dense else 2.2   # thinner where there are many lines, a band or many points
        ax.plot(xs, ys, color=colour, linewidth=w, solid_capstyle="round", solid_joinstyle="round", zorder=4)   # a crisp line: no glow
        _dot(ax, xs[-1], ys[-1], colour, look.end_dot, look.end_rim, 5)
        drawn.append((colour, xs, ys))
    return drawn


def _columns(ax, x, heights, width: float, bottom=0.0, **style):
    """Bars from each x to x + width, as one collection of rectangles: what ax.bar draws, without a patch (and its bookkeeping)
    for every bar. The axes' limits are the caller's to set."""
    x = np.asarray(x, dtype=float)
    low = np.broadcast_to(np.asarray(bottom, dtype=float), x.shape)
    high = low + np.asarray(heights, dtype=float)
    corners = np.stack([np.column_stack(xy) for xy in ((x, low), (x + width, low), (x + width, high), (x, high))], axis=1)
    ax.add_collection(PolyCollection(corners, edgecolors="none", linewidths=0, **style), autolim=False)


def _draw_bars(ax, bx, ys, width: float, axis_top: float, zorder: int, alpha: float = 0.5, **kw):
    """Rain bars: a pale body (RAIN blended with white by `alpha`, so solid and nothing behind it shows through) with a
    brighter cap, so each reads as a little column. `kw` goes to both (for rain behind a line, the transform)."""
    ys = np.asarray(ys, dtype=float)
    _columns(ax, bx, ys, width, facecolors=_mix(RAIN, 1 - alpha), zorder=zorder, **kw)
    cap = np.sign(ys) * np.minimum(axis_top * 0.014, np.abs(ys))   # at the bar's far end, below zero as above it
    _columns(ax, bx, cap, width, ys - cap, facecolors=RAIN, alpha=0.95, zorder=zorder, **kw)


def _bars_behind(ax, bars, tz: tzinfo) -> float:
    """Bars (rain) behind the panel's lines and over its grid, on a scale of their own read on the right: the tallest reaches
    BARS_SHARE of the panel's height. They are drawn in the panel itself (x on its time axis, y as a share of its height), and
    the scale's two labels sit where a right-hand axis would put them. Returns where the last bar ends (matplotlib date number)."""
    bx = _nums(tz, bars.x)
    top = max([*bars.y, 1.0])
    _draw_bars(ax, bx, np.asarray(bars.y, dtype=float) * (BARS_SHARE / top), bars.width / 86400 * 0.85, 1.0, 1,
               transform=ax.get_xaxis_transform())
    ax.add_line(Line2D([0, 1], [BARS_SHARE, BARS_SHARE], transform=ax.transAxes, color=RAIN, linewidth=0.7, linestyle=(0, (1, 2)),
                       alpha=0.8, zorder=1))   # the top of the rain scale, on its own
    beside = ax.transAxes + ScaledTranslation(4 / 72, 0, ax.figure.dpi_scale_trans)   # 4 pt right of the panel, as tick labels sit
    for y, label in ((0, "0"), (BARS_SHARE, f"{top:g} {bars.unit}")):
        ax.text(1, y, label, transform=beside, ha="left", va="center_baseline", fontsize=7, color=RAIN_TEXT)
    return _end_of(bx, bars.width, 0.0)


END_LABELLED = ("pressure", "co2", "voc_index", "nox_index", "solar", "wind", "vpd")   # one line, and still its latest value beside it


def _axes_width(chart: Chart) -> float:
    """The plot's width: narrower when a panel has rain's scale or end labels on its right, to leave room for them."""
    return AX_RECT[2] - (0.07 if any((p.lines and p.bars) or len(p.lines) > 1 or (p.lines and p.reading in END_LABELLED)
                                     for p in chart.panels) else 0)


def _headline(fig, title: str, subtitle: str, height: float, unit: str = ""):
    """Title and subtitle at the top left; a unit that is not a degree goes in the title (ticks stay plain numbers)."""
    title = f"{title} ({unit})" if unit and _deg(unit) != "°" else title
    head = fig.text(AX_RECT[0], 1 - 0.27 / height, title, fontsize=13, fontweight=TITLE_WEIGHT, color=TEXT, va="center")
    room = AX_RECT[2] * fig.get_figwidth() * fig.dpi        # the width of the plots below
    renderer = fig.canvas.get_renderer()
    names = title.split(", ")
    kept = len(names)
    if head.get_window_extent(renderer).width > room:
        names = [n for n in names if n != "Rain"]   # cut: "N more" counts the panels with their own heading, and rain is drawn behind a line
        kept = len(names)
    while head.get_window_extent(renderer).width > room and kept > 2:   # a long list of readings: the first few "and N more"
        kept -= 1
        head.set_text(", ".join(names[:kept]) + f" and {len(names) - kept} more")
    fig.text(AX_RECT[0], 1 - 0.52 / height, subtitle, fontsize=8.5, color=MUTED, va="center")


def _time_axis(ax, span_days: float, bars=None, tz: tzinfo | None = None):
    """Tick positions and labels for a date axis, chosen by the length of the period. Bars of whole months or years (a count per
    month or year) are labelled under the middle of each bar instead."""
    if bars and tz and bars.per in ("year", "month") and bars.x and (bars.per == "year" or len(bars.x) <= 14):
        centres = _nums(tz, bars.x) + bars.width / 86400 * 0.85 / 2
        fmt = "%Y" if bars.per == "year" else "%b\n'%y"
        ax.xaxis.set_major_locator(FixedLocator(list(centres)))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: mdates.num2date(v).strftime(fmt)))
        return
    if span_days <= 1.1:
        ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3)))
        fmt = "%-I%p"
    elif span_days <= 4:
        ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 12]))
        fmt = "%a %-I%p"
    elif span_days <= 45:
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, math.ceil(span_days / 8))))
        fmt = "%a\n%-d %b"
    elif span_days <= 400:
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        fmt = "%b\n'%y"
    else:
        ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=[1, 4, 7, 10]))
        fmt = "%b\n'%y"
    ax.xaxis.set_major_formatter(FuncFormatter(
        lambda v, _: mdates.num2date(v).strftime(fmt).replace("AM", "am").replace("PM", "pm")))


def _legend_dots(ax_or_fig, colours: list[str], labels: list[str], **kw):
    handles = [Line2D([], [], marker="o", linestyle="", markersize=6, color=c) for c in colours]
    ax_or_fig.legend(handles, labels, frameon=False, labelcolor=TEXT, handletextpad=0.2, columnspacing=1.1, **kw)


def _pills(ax, lines: list[Line], drawn: list[tuple], tz: tzinfo, x0: float, x1: float, look: Look, deg: str = ""):
    """A dot on the highest and the lowest point of each line with a pill of its value above or below it. The point is the top
    (bottom) of the line's band when it has one, else its true record at its actual time (a dotted stem joins it to a line that
    does not reach it). A low on the floor says nothing and is not labelled; pills that land close
    together go side by side."""
    y_lo, y_hi = ax.get_ylim()
    edge = lambda x: "left" if (x - x0) / (x1 - x0) < 0.06 else "right" if (x - x0) / (x1 - x0) > 0.94 else "center"
    pills = []
    for s, (colour, xs, ys) in zip(lines, drawn):
        for want, above in (("high", True), ("low", False)):
            if want in s.records and s.low is not None:  # a banded line: the label sits on the top (bottom) of its band
                rx, ry = _extreme(s, want, xs)
            elif want in s.records:  # the true record, at its actual time (may sit off an averaged line)
                rx, ry = float(_nums(tz, [s.records[want][0]])[0]), float(s.records[want][1])
                rx = min(max(rx, x0), x1)
            else:  # only some records given (wind: the strongest gust, no lowest): no label for the other
                continue
            if want == "low" and (ry - y_lo) < 0.08 * (y_hi - y_lo):
                continue  # a low on the floor (0 mm, 0 km/h) says nothing
            line_y = float(np.interp(rx, xs, ys))
            if abs(ry - line_y) > (0.005 * (y_hi - y_lo) if s.smoothed else 1e-6) and not s.low:
                ax.vlines(rx, min(ry, line_y), max(ry, line_y), colors=colour, linestyles=(0, (1, 2)),  # well off the line: a dotted stem back to it
                          linewidth=1.2, alpha=0.8, zorder=3)
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
        ax.annotate(text, (rx, ry), xytext=(0, look.pill_lift if above else -look.pill_lift), textcoords="offset points", ha=ha,
                    va="bottom" if above else "top", fontsize=look.pill_font, fontweight="bold", color=_ink_on(colour),
                    bbox=_box(colour, *look.pill_box), zorder=look.pill_z + 1)


def _rose_radius(outer: np.ndarray) -> float:
    """The rose's radius for wedges ending at `outer` (16 compass points x 3 speed steps, out from the centre): the one matplotlib
    gives bars that long, 5% beyond the longest wedge, or right at its end when that wedge ends in an empty step (the base of a
    bar is a sticky edge). Worked out as matplotlib does, so the rose is the size it always was."""
    longest = float(outer[:, -1].max())
    if not longest:
        return 1.0   # nothing to draw
    ends = outer[:, :-1][outer[:, :-1] >= longest - 1e-5 * longest]   # a step's base at the end of a longest wedge: nothing beyond it
    return float(ends.min()) if ends.size else longest + longest * 0.05


def _render_rose(fig, compass: Compass):
    """The whole period as a wind rose (N up, clockwise): each of the 16 wedges is the share of readings from that
    way, stacked by wind speed. It sits beside the wind speed line. The wedges are one collection in the polar axes' own
    units (the centre at 0.5, 0.5 and the edge at radius 0.5): 48 polar bars cost several times as much to make and draw."""
    counts = np.asarray(compass.rose, dtype=float)                        # 16 compass points x 3 speed steps
    ax = fig.add_axes([0.685, 0.12, 0.27, 0.62], projection="polar")
    outer = np.cumsum(counts / max(counts.sum(), 1) * 100, axis=1)        # where each step ends, out from the centre
    radius = _rose_radius(outer)
    ax.set_ylim(0, radius)                                                # the grid's rings follow it
    half = 0.5 / radius
    steps = [(k, i) for i in range(3) for k in range(16) if outer[k, i]]  # step by step, each over the one before; nothing at the centre
    wedges = [Wedge((0.5, 0.5), outer[k, i] * half, 80 - 22.5 * k, 100 - 22.5 * k, width=(outer[k, i] - (outer[k, i - 1] if i else 0)) * half)
              for k, i in steps]                                          # 20° wide around each compass point, clockwise from N
    ax.add_collection(PatchCollection(wedges, facecolors=[WIND_STEPS[i] for _, i in steps], edgecolors=BG, linewidths=0.4,
                                      transform=ax.transAxes), autolim=False)
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_xticks(np.deg2rad([0, 90, 180, 270]))
    ax.set_xticklabels(["N", "E", "S", "W"], color=MUTED, fontsize=8)
    ax.set_yticklabels([])
    ax.grid(color=GRID, linewidth=0.6)
    ax.spines["polar"].set_visible(False)
    ax.set_facecolor(BG)
    if compass.speeds:
        a, b = compass.speed_steps
        fig.legend([Line2D([], [], marker="s", linestyle="", markersize=6, color=c) for c in WIND_STEPS],
                   [f"under {a}", f"{a}–{b}", f"{b}+ km/h"], loc="lower right", bbox_to_anchor=(0.985, 0.0), ncol=3,
                   frameon=False, fontsize=8.5, labelcolor=MUTED, handletextpad=0.2, columnspacing=0.9)


# ---------- several panels on one time axis ----------
def _extreme(line: Line, want: str, xs) -> tuple[float, float]:
    """Where the drawn line is highest (or lowest): the top (bottom) of its band when it has one, else the line itself. A
    label sits on what is drawn, never on a raw reading the chart does not reach. `xs` are the line's x as date numbers."""
    ys = (line.high if want == "high" else line.low) if line.low is not None else line.y
    i = int(np.argmax(ys) if want == "high" else np.argmin(ys))
    return float(xs[i]), float(ys[i])


def _mark_highs(ax, lines: list[Line], drawn: list[tuple], x0: float, x1: float, look: Look):
    """The peak of each line in a multi-line panel: a dot on the peak, and its value in a pill placed in empty space (the
    top of the panel where no line reaches, else the margin) joined to the dot by a thin dotted line. `lines` and `drawn` are
    the lines with a peak record and what was drawn for them."""
    y_lo, y_hi = ax.get_ylim()
    box = ax.get_position()
    height_pt = box.height * ax.figure.get_figheight() * 72
    per_pt = (y_hi - y_lo) / height_pt                                    # data units in one point
    peaks = []
    for line, (colour, xs, _) in zip(lines, drawn):
        if "high" in line.records:
            mx, my = _extreme(line, "high", xs)
            peaks.append((mx, my, colour))
    peaks.sort(key=lambda p: -p[1])                                       # the highest peak gets the top pill
    size, pitch = look.pill_font, look.pitch                              # the pills' font and row spacing, in points
    groups = []                                                           # peaks at about the same time share a column, stacked
    for peak in peaks:                                                    # (highest first)
        near = next((g for g in groups if abs(g[0][0] - peak[0]) < 0.05 * (x1 - x0)), None)
        near.append(peak) if near else groups.append([peak])
    block = (10 + pitch * max(len(g) for g in groups)) * per_pt if groups else 0   # the height the tallest stack of pills needs
    reach = 0.06 * (x1 - x0)                                              # half a pill's width, in x units
    tops = [(xs, np.asarray(line.high if line.low is not None else line.y, float))
            for line, (_, xs, _) in zip(lines, drawn)]                    # what is drawn, bands included
    free = []
    for k in range(3, 98, 2):                                             # candidate columns across the panel
        cx = x0 + (x1 - x0) * k / 100
        highest = max((float(np.max(ys[(xs > cx - reach) & (xs < cx + reach)], initial=y_lo)) for xs, ys in tops), default=y_lo)
        if highest < y_hi - block - 4 * per_pt:
            free.append(cx)
    arrow = lambda colour: {"arrowstyle": "-", "color": colour, "linewidth": 0.8, "linestyle": (0, (1, 2)), "shrinkA": 1, "shrinkB": 2}
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
                _dot(ax, mx, my, colour)
                ax.annotate(f"{round(my, 1):g}", (mx, my), xytext=(cx, y_hi - (10 + pitch * i) * per_pt), textcoords="data", ha="center",
                            va="center", fontsize=size, fontweight="bold", color=_ink_on(colour), zorder=5, arrowprops=arrow(colour),
                            bbox=_box(colour))
        return
    placed = []                                                           # no empty column: the right margin, at the peaks' heights
    for mx, my, colour in peaks:
        ly = min(my, placed[-1] - pitch * per_pt) if placed else my
        placed.append(ly)
        _dot(ax, mx, my, colour)
        ax.annotate(f"{round(my, 1):g}", (mx, my), xytext=(1.03, ly), textcoords=("axes fraction", "data"), ha="left", va="center",
                    fontsize=size, fontweight="bold", color=_ink_on(colour), zorder=5, annotation_clip=False, arrowprops=arrow(colour),
                    bbox=_box(colour))


def _figure(value: float) -> str:
    """A latest value as written beside a line: three significant figures, but never in exponent form (1014.8, not 1.01e+03)."""
    return f"{value:.1f}".rstrip("0").rstrip(".") if abs(value) >= 100 else f"{value:.3g}"


def _end_labels(ax, drawn: list[tuple], show_floor: bool = False):
    """The latest value of each line in the margin beside it, nudged apart where they would touch. Lines that all sit on the
    floor have nothing to read off and get no label, unless `show_floor` (a zero wind or solar reading is worth saying)."""
    y_lo, y_hi = ax.get_ylim()
    if not show_floor and max(d[2][-1] for d in drawn) - y_lo < 0.05 * (y_hi - y_lo):
        return
    height_pt = ax.get_position().height * ax.figure.get_figheight() * 72
    gap = 10 * (y_hi - y_lo) / height_pt                    # 10 points, in data units
    placed = []
    for colour, xs, ys in sorted(drawn, key=lambda d: d[2][-1]):
        y = max(ys[-1], placed[-1] + gap) if placed else ys[-1]
        placed.append(y)
        shade = colour
        ax.annotate(_figure(ys[-1]), (xs[-1], ys[-1]), xytext=(1.014, y), textcoords=("axes fraction", "data"), va="center", ha="left",
                    fontsize=8.5, fontweight="bold", color=shade, annotation_clip=False,
                    arrowprops={"arrowstyle": "-", "color": shade, "linewidth": 0.6, "alpha": 0.6, "shrinkA": 0, "shrinkB": 2},
                    bbox={"boxstyle": "round,pad=0.15", "fc": "none", "ec": "none"})


def _draw_panel(ax, p: Panel, tz: tzinfo, first: int, x0: float, x1: float, look: Look):
    """One panel: lines (with bands and zones, peaks labelled, rain behind them), rain on its own, or shares. Returns the
    number of colours used, where the drawing ends on the x axis and the legend's entries (colour, name)."""
    if p.shares:
        s = p.shares  # the share of each bar's time in good / poor / very poor: traffic-light bars stacked to 100%
        bx = _nums(tz, s.x)
        base = np.zeros(len(bx))
        for column, colour in zip((s.good, s.poor, s.very_poor), ZONE_COLOURS):
            _columns(ax, bx, column, s.width / 86400 * 0.85, base, facecolors=colour, zorder=3)
            base += np.asarray(column, dtype=float)
        ax.set_ylim(0, 100)
        ax.legend([Line2D([], [], marker="s", linestyle="", markersize=5, color=c) for c in ZONE_COLOURS],
                  ["good", "poor", "very poor"], loc="lower right", bbox_to_anchor=(1.0, 1.0), frameon=False, fontsize=8.5,
                  labelcolor=TEXT, ncol=3, handletextpad=0.2, columnspacing=0.9, borderaxespad=0.1)
        return 0, _end_of(bx, s.width, x1), []
    if not p.lines:  # rain on its own
        b = p.bars
        bx = _nums(tz, b.x)
        top = max([*b.y, 1.0]) * 1.15
        bottom = min([*b.y, 0.0]) * 1.3   # a figure can be below zero (a coldest night)
        _draw_bars(ax, bx, b.y, b.width / 86400 * 0.85, top, 3, alpha=0.75)
        if b.values:   # a few bars of counts or figures: the number at the end of each
            for x, y in zip(bx, b.y):
                ax.text(x + b.width / 86400 * 0.85 / 2, y + (top - bottom) * (0.015 if y >= 0 else -0.015), f"{y:g}", ha="center",
                        va="bottom" if y >= 0 else "top", fontsize=8, color=TEXT, zorder=5)
        ax.set_ylim(bottom, top)
        return 0, _end_of(bx, b.width, x1), []
    lines = p.lines
    marked = [i for i, s in enumerate(lines) if s.records and p.peaks != "none"]
    pilled = bool(marked) and p.peaks == "pills"
    _pad_limits(ax, *_extent(lines, [float(r[1]) for s in lines for r in s.records.values()] if pilled else []),
                top=look.pad if pilled else 0.18 if marked else 0.12, bottom=look.pad if pilled else 0.12,
                floor=None if _deg(p.unit) == "°" else 0)   # a temperature can go below zero; nothing else drawn as a line can
    ybottom, ytop = ax.get_ylim()
    drawn = _draw_lines(ax, lines, tz, look, first, p.reading)
    if p.zones:  # a rated reading: its good / poor / very poor zones behind the line (and over any rain behind it, as always)
        _shade_zones(ax, p.zones, ybottom, ytop, 0.07, 1.5 if p.bars else 0)
    ax.set_ylim(ybottom, ytop)
    if marked and p.peaks == "aside":
        _mark_highs(ax, [lines[i] for i in marked], [drawn[i] for i in marked], x0, x1, look)
    elif marked:
        _pills(ax, [lines[i] for i in marked], [drawn[i] for i in marked], tz, x0, x1, look, _deg(p.unit))
    if p.bars:
        x1 = max(x1, _bars_behind(ax, p.bars, tz))
    if len(lines) > 1:
        _end_labels(ax, drawn)
    elif p.reading in END_LABELLED and drawn[0][2][-1] != 0:   # a latest value of zero (night, calm) has nothing to say
        _end_labels(ax, drawn, show_floor=True)
    entries = [(_colour(s, first + i, p.reading), s.label) for i, s in enumerate(lines)] + ([(RAIN, p.bars.label)] if p.bars else [])
    return len(lines), x1, entries


def _render(chart: Chart, tz: tzinfo, times: dict | None = None) -> bytes:
    """The panels top to bottom on one time axis, so the rain (or another reading) lines up with what the others were doing.
    A chart of one panel of lines is the same thing drawn BIG (and with its wind rose beside it, when it has one)."""
    started = time.perf_counter()
    panels, n = chart.panels, len(chart.panels)
    look = _look_for(chart)
    height = H_IN if n <= 2 else H_IN + PANEL_IN * (n - 2)
    fig = plt.figure(figsize=(W_IN, height), dpi=DPI, facecolor=BG)
    try:
        _headline(fig, panels[0].label if look.in_headline else chart.title, chart.subtitle, height, panels[0].unit if look.in_headline else "")
        width = _axes_width(chart)
        body = height - HEAD_IN - FOOT_IN
        gap = 0.27 if n > 2 else 0.22
        each = (body - gap * (n - 1)) / n
        xs = _nums(tz, {t for p in panels for t in p.xs})   # each time once: only the range is wanted
        x0, x1 = float(xs.min()), float(xs.max())
        axes, used = [], 0
        for i, p in enumerate(panels):
            bottom = (FOOT_IN + (n - 1 - i) * (each + gap)) / height
            rect = [AX_RECT[0], bottom, width, each / height]
            if look.card:   # the faint tint behind the panel: a rectangle on the figure (an axes of its own costs far more)
                fig.add_artist(Rectangle(rect[:2], rect[2], rect[3], transform=fig.transFigure, facecolor=CARD, edgecolor="none",
                                         linewidth=0, zorder=-1))
            ax = fig.add_axes(rect, facecolor="none" if look.card else BG, sharex=axes[0] if axes else None)
            axes.append(ax)
            _style_axis(ax)
            count, x1, entries = _draw_panel(ax, p, tz, used, x0, x1, look)
            used += count
            if len(entries) >= look.key_from:   # the colour key: beside the headline for a big chart, above the panel in a stack
                key = dict(loc="center right", bbox_to_anchor=(AX_RECT[0] + AX_RECT[2], 1 - 0.27 / height)) if look.in_headline else \
                    dict(loc="lower right", bbox_to_anchor=(1.0, 1.0), borderaxespad=0.1)
                _legend_dots(fig if look.in_headline else ax, [c for c, _ in entries], [label for _, label in entries], ncol=len(entries),
                             fontsize=look.legend_font, **key)
            deg = _deg(p.unit)
            tick_unit = deg if deg == "°" else ""
            ax.yaxis.set_major_locator(MaxNLocator(nbins=3 if n > 2 else look.nbins, steps=[1, 2, 2.5, 5, 10],
                                                   integer=bool(p.bars and not p.lines and p.bars.unit == "days")))
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _, u=tick_unit: f"{v:g}{u}"))
            if not look.in_headline:
                title = f"{p.label} ({p.unit})" if p.unit and not tick_unit else p.label
                ax.set_title(title, loc="left", fontsize=8.5, fontweight=TITLE_WEIGHT, color=TEXT, pad=4, y=1.0)   # y: placed, not searched for
            if i < n - 1:
                ax.xaxis.set_visible(False)   # the time is read off the bottom panel (these ticks have no length and no grid)
        margin = (x1 - x0) * 0.015  # room for the end dots
        axes[0].set_xlim(x0 - margin, x1 + margin)
        _time_axis(axes[-1], x1 - x0, panels[-1].bars if not panels[-1].lines else None, tz)
        if chart.compass:  # beside the wind line
            pos = axes[0].get_position()
            axes[0].set_position([pos.x0, pos.y0, 0.57, pos.height])
            _render_rose(fig, chart.compass)
    except Exception:
        plt.close(fig)
        raise
    return _png(fig, times, started)


def _palette(rgb: np.ndarray, counts: np.ndarray, size: int = 256) -> tuple[list[int], np.ndarray]:
    """Up to `size` of these colours (rows of `rgb`, each with its count of pixels) for a palette, and the nearest of them for every
    colour. It starts from the commonest; each next one is the colour the palette so far draws worst (its pixels times its squared
    distance to the nearest colour taken), so the colours that cover an area are kept exactly and the in-between shades of
    anti-aliased edges come out within a few levels. With `size` colours or fewer, every one is kept. Whole numbers throughout, so
    the picks are exact (ties go to the first colour)."""
    red, green, blue = (np.ascontiguousarray(rgb[:, i], dtype=np.int64) for i in range(3))
    weight = np.asarray(counts, dtype=np.int64)

    def distance_to(k: int) -> np.ndarray:   # every colour's squared distance to colour k
        distance = (red - red[k]) ** 2
        distance += (green - green[k]) ** 2
        distance += (blue - blue[k]) ** 2
        return distance
    taken = [int(np.argmax(weight))]
    error = distance_to(taken[0])                                         # each colour's squared distance to its nearest taken one
    score = weight * error                                                # how badly the palette so far draws its pixels
    nearest = np.zeros(len(rgb), np.uint8)
    while len(taken) < min(size, len(rgb)):
        pick = int(np.argmax(score))
        if not error[pick]:
            break
        distance = distance_to(pick)
        closer = np.flatnonzero(distance < error)                         # only these colours' error and score change
        error[closer] = distance[closer]
        score[closer] = weight[closer] * distance[closer]
        nearest[closer] = len(taken)
        taken.append(pick)
    return taken, nearest


def _png(fig, times: dict | None = None, started: float = 0.0) -> bytes:
    """The figure as a PNG of at most 256 colours (see _palette): well under half the size of a full-colour one, and quicker to
    write. A chart has a few thousand colours, nearly all of them the in-between shades on the edges of lines and letters.
    times: the seconds since `started` (the clock when the figure was begun) are added to its "draw", and the PNG's to its "png"."""
    try:
        fig.canvas.draw()
        drawn = time.perf_counter()
        width, height = fig.canvas.get_width_height(physical=True)
        pixels = np.frombuffer(fig.canvas.buffer_rgba(), "<u4") & 0xFFFFFF   # each pixel as one number: red + green·256 + blue·65536 (it is opaque)
        colours, counts = np.unique(pixels, return_counts=True)
        rgb = np.stack([colours & 255, colours >> 8 & 255, colours >> 16], axis=1)
        taken, nearest = _palette(rgb, counts)
        lookup = np.empty(1 << 24, np.uint8)                                 # each colour's place in the palette
        lookup[colours] = nearest
        image = Image.frombuffer("P", (width, height), lookup[pixels], "raw", "P", 0, 1)
        image.putpalette(rgb[taken].astype(np.uint8).tobytes())
        buf = io.BytesIO()
        image.save(buf, format="png", dpi=(DPI, DPI))
        if times is not None:
            times["draw"] = times.get("draw", 0.0) + drawn - started
            times["png"] = times.get("png", 0.0) + time.perf_counter() - drawn
        return buf.getvalue()
    finally:
        plt.close(fig)


_DRAWING = threading.Lock()   # pyplot keeps global state (current figure, rcParams): one chart is drawn at a time


def render(chart: Chart, tz: tzinfo, times: dict | None = None) -> bytes:
    """PNG bytes for one chart (1280x720; a stack of more than two panels is taller). Safe to call from several threads.
    times (a Turn's): the seconds spent drawing the figure and writing the PNG are added to its "draw" and "png"."""
    with _DRAWING:
        return _render(chart, tz, times)
