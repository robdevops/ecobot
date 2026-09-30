"""Charts sent alongside history answers, for both Ecowitt and AirGradient.

A history tool adds a specs.Chart to turn.charts (the question's Turn, made by the bot) when a chart was asked for; the
bot renders them after the answer is written and sends them with it. Every chart is one or more panels on a shared time
axis (see specs.py):
  - one panel of lines is drawn large, with the records marked as pills, an end dot and (for wind) the compass beside it,
    at exactly 1280x720, the size Telegram displays photos at, so nothing is rescaled;
  - several panels are stacked, the figure growing a little taller with each one past two (four panels: 1280x1160).
Rain (bars) sits behind the lines of the panel it belongs to, on its own right-hand axis.
"""

import glob
import io
import math
import threading
from dataclasses import dataclass
from datetime import tzinfo

import matplotlib

matplotlib.use("Agg")  # no display on the server
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib import patheffects as pe  # noqa: E402
from matplotlib.colors import to_rgb  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Polygon  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, MaxNLocator  # noqa: E402

from .specs import Chart, Compass, Line, Panel  # noqa: E402
from .timeutil import to_local  # noqa: E402

# Palette. Slate neutrals for the furniture; one hue per reading (weather and air quality, kept clear of the green/yellow/red
# rating zones); the same reading indoors is its complementary hue (opposite on the colour wheel, as a painter pairs them).
BG, TEXT, MUTED, GRID, AXIS = "#FFFFFF", "#0F172A", "#64748B", "#E2E8F0", "#CBD5E1"
READING_COLOURS = {
    "temperature": "#E5383B",   # crimson
    "feels_like": "#FF8C42",    # tangerine (beside the crimson, as blue sits beside violet)
    "solar": "#F5B83D",         # golden
    "uv": "#8B5CF6",            # violet
    "pressure": "#C04CE8",      # orchid
    "vpd": "#06B6D4",           # cyan
    "humidity": "#2747C9",      # deep blue
    "dew_point": "#13B8A6",     # teal
    "wind": "#64748B",          # slate grey
    "pm2_5": "#0EA5E9", "pm10": "#8B5CF6", "pm1": "#14B8A6", "co2": "#475569", "voc_index": "#D97706", "nox_index": "#DB2777",
}
# Indoors: red with peacock teal, blue with amber, teal with apricot, tangerine with azure.
INDOOR_COLOURS = {"temperature": "#0FA3B1", "humidity": "#F5A524", "dew_point": "#F28C3C", "feels_like": "#2B9BD6"}
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
BARS_SHARE = 0.3                 # rain behind a line never rises past this share of the panel's height


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
    label_all: bool          # label every line's peaks, not just the lines that carry records
    legend_font: float
    key_from: int            # the colour key is drawn from this many entries (a big chart's names its one line too)
    units: bool              # pills carry a decimal and the unit; a stacked panel's are the bare number (its title has the unit)
    nbins: int               # about how many y ticks
    card: bool               # a faint tint behind the panel (a stack's panels; a chart of one panel is on white)
    in_headline: bool        # the panel's name and unit are the figure's headline, and its colour key sits beside it


BIG = Look(30, 1.5, 7.5, 18, 1.2, 9, (0.35, 0.8), 6, 15, 0.26, True, 8.5, 1, True, 5, False, True)
SMALL = Look(20, 1.2, 6.5, 12, 0.8, 5, (0.25, 0.6), 4, 12, 0.3, False, 7, 2, False, 4, True, False)


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
        return (INDOOR_COLOURS.get(reading) or _mix(base, 0.4)) if line.label == "Indoor" else base
    return FALLBACK[i % len(FALLBACK)]


def _mix(colour: str, share: float) -> str:
    """The colour with this share of white blended in."""
    return "#" + "".join(f"{round(255 * share + c * 255 * (1 - share)):02X}" for c in to_rgb(colour))


def _deg(unit: str) -> str:
    if not unit:
        return ""
    return "°" if unit.replace("º", "°") in ("°C", "°F", "°") else f" {unit}"


def _box(colour: str, pad: float = 0.25, rounding: float = 0.6) -> dict:
    """The rounded coloured box behind a value label."""
    return {"boxstyle": f"round,pad={pad},rounding_size={rounding}", "fc": colour, "ec": "none"}


def _dot(ax, x: float, y: float, colour: str, size: float = 12, edge: float = 0.8, z: int = 4):
    """A small dot with a white rim, marking a point on a line."""
    ax.scatter([x], [y], s=size, color=colour, edgecolors="white", linewidths=edge, zorder=z)


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


def _shade_zones(ax, zones, ybottom: float, ytop: float, alpha: float):
    """Faint rating zones behind a line: good up to z1, poor up to z2, very poor above."""
    z1, z2 = zones
    for lo, hi, colour in ((ybottom, z1, ZONE_COLOURS[0]), (z1, z2, ZONE_COLOURS[1]), (z2, ytop, ZONE_COLOURS[2])):
        if hi > ybottom and lo < ytop:
            ax.axhspan(max(lo, ybottom), min(hi, ytop), color=colour, alpha=alpha, linewidth=0, zorder=0)


def _pad_limits(ax, lo: float, hi: float, top: float = 0.2, bottom: float = 0.16, floor: float | None = None):
    """Room above and below the data; `floor`: the axis does not go below it when the data does not (no negative wind)."""
    span = max(hi - lo, 1.0)
    y0 = lo - span * bottom
    ax.set_ylim(y0 if floor is None or lo < floor else max(y0, floor), hi + span * top)


def _extent(lines: list[Line], extra: list[float] = ()) -> tuple[float, float]:
    """The lowest and highest value drawn: the bands where there are any, else the lines (and `extra`, the records)."""
    return (min([min(s.low or s.y) for s in lines] + list(extra)), max([max(s.high or s.y) for s in lines] + list(extra)))


def _gradient_under(ax, xs, ys, colour: str, ybottom: float, alpha: float = 0.22):
    """A soft fade from the line down to the floor."""
    poly = Polygon([(xs[0], ybottom), *zip(xs, ys), (xs[-1], ybottom)], closed=True, fc="none", ec="none")
    ax.add_patch(poly)
    rgba = np.zeros((256, 1, 4))
    rgba[..., :3] = to_rgb(colour)
    rgba[..., 3] = np.linspace(alpha, 0.0, 256)[:, None]
    img = ax.imshow(rgba, aspect="auto", extent=[xs.min(), xs.max(), ybottom, ys.max()], origin="upper", zorder=2)
    img.set_clip_path(poly)


def _draw_lines(ax, lines: list[Line], tz: tzinfo, look: Look, first: int = 0, reading: str = "") -> list[tuple]:
    """Each line's low-to-high range behind it, then the line with a soft glow and a dot on its end: [(colour, xs, ys)].
    `first` is the colour index of the first line (the fallback colours cycle across a stack's panels)."""
    drawn = []
    dense = max(len(s.x) for s in lines) > 200
    for i, s in enumerate(lines, first):
        colour = _colour(s, i, reading)
        xs, ys = _nums(tz, s.x), np.asarray(s.y, dtype=float)
        if s.low:
            ax.fill_between(xs, s.low, s.high, color=colour, alpha=0.2, linewidth=0, zorder=3)
        w = 1.3 if len(lines) > 2 or s.low else 1.5 if dense else 2.2   # thinner where there are many lines, a band or many points
        (line,) = ax.plot(xs, ys, color=colour, linewidth=w, solid_capstyle="round", solid_joinstyle="round", zorder=4)
        line.set_path_effects([pe.Stroke(linewidth=w + 2.5, foreground=colour, alpha=0.10), pe.Normal()])
        _dot(ax, xs[-1], ys[-1], colour, look.end_dot, look.end_rim, 5)
        drawn.append((colour, xs, ys))
    return drawn


def _draw_bars(ax, bx, ys, width: float, axis_top: float, zorder: int, alpha: float = 0.5):
    """Rain bars: a pale body (RAIN blended with white by `alpha`, so solid and nothing behind it shows through) with a
    brighter cap, so each reads as a little column."""
    ys = np.asarray(ys, dtype=float)
    ax.bar(bx, ys, width=width, align="edge", color=_mix(RAIN, 1 - alpha), linewidth=0, zorder=zorder)
    cap = axis_top * 0.014
    ax.bar(bx, np.minimum(cap, ys), bottom=np.maximum(ys - cap, 0), width=width, align="edge", color=RAIN, alpha=0.95,
           linewidth=0, zorder=zorder)


def _bars_behind(ax, bars, tz: tzinfo) -> float:
    """Bars (rain) on their own right-hand axis, behind the panel's lines and never taller than BARS_SHARE of it. Returns
    where the last bar ends (matplotlib date number)."""
    bx = _nums(tz, bars.x)
    ax2 = ax.twinx()
    weight = ax.yaxis.get_gridlines()[0].get_linewidth() if ax.yaxis.get_gridlines() else 0.8
    ax.grid(False)       # the grid is drawn by an axes of its own under the rain (sharing this one's scale): grid, rain, lines
    under = ax.figure.add_axes(ax.get_position(), sharex=ax, sharey=ax, facecolor=ax.get_facecolor())
    for side in ("top", "right", "left", "bottom"):
        under.spines[side].set_visible(False)
    under.tick_params(axis="both", length=0, labelleft=False, labelbottom=False)
    under.grid(axis="y", color=GRID, linewidth=weight)
    under.set_axisbelow(True)
    under.set_zorder(ax2.get_zorder() - 1)
    top = max([*bars.y, 1.0])
    _draw_bars(ax2, bx, bars.y, bars.width / 86400 * 0.85, top / BARS_SHARE, 1)
    ax2.set_ylim(0, top / BARS_SHARE)
    ax2.axhline(top, color=RAIN, linewidth=0.7, linestyle=(0, (1, 2)), alpha=0.8, zorder=1)  # the top of the rain scale, on its own
    ax2.yaxis.set_major_locator(FixedLocator([0, top]))
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g} {bars.unit}" if v else "0"))
    for side in ("top", "right", "left", "bottom"):
        ax2.spines[side].set_visible(False)
    ax2.tick_params(axis="y", length=0, labelsize=7, labelcolor=RAIN_TEXT, pad=4)
    ax.set_zorder(ax2.get_zorder() + 1)  # the lines above the bars
    ax.patch.set_visible(False)
    ax2.patch.set_visible(False)     # so the grid beneath shows between the bars (set last: the line above re-shows it)
    return _end_of(bx, bars.width, 0.0)


def _axes_width(chart: Chart) -> float:
    """The plot's width: narrower when a panel has rain's scale or end labels on its right, to leave room for them."""
    return AX_RECT[2] - (0.07 if any((p.lines and p.bars) or len(p.lines) > 1 for p in chart.panels) else 0)


def _headline(fig, title: str, subtitle: str, height: float, unit: str = ""):
    """Title and subtitle at the top left; a unit that is not a degree goes in the title (ticks stay plain numbers)."""
    title = f"{title} ({unit})" if unit and _deg(unit) != "°" else title
    fig.text(AX_RECT[0], 1 - 0.27 / height, title, fontsize=13, fontweight=TITLE_WEIGHT, color=TEXT, va="center")
    fig.text(AX_RECT[0], 1 - 0.52 / height, subtitle, fontsize=8.5, color=MUTED, va="center")


def _time_axis(ax, span_days: float):
    """Tick positions and labels for a date axis, chosen by the length of the period."""
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
    does not reach it), else the line's own extreme. A low on the floor says nothing and is not labelled; pills that land close
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
            elif s.records:  # only some records given (wind: the strongest gust, no lowest): no label for the other
                continue
            else:
                idx = int(ys.argmax() if above else ys.argmin())
                rx, ry = xs[idx], ys[idx]
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
                    (p if p[1] <= q[1] else q)[4] = False  # the lower one's pill goes below its point
                else:
                    left[5], right[5] = "right", "left"
    for rx, ry, text, colour, above, ha in pills:
        ax.annotate(text, (rx, ry), xytext=(0, look.pill_lift if above else -look.pill_lift), textcoords="offset points", ha=ha,
                    va="bottom" if above else "top", fontsize=look.pill_font, fontweight="bold", color="white",
                    bbox=_box(colour, *look.pill_box), zorder=look.pill_z + 1)


def _render_rose(fig, compass: Compass):
    """The whole period as a wind rose (N up, clockwise): each of the 16 wedges is the share of readings from that
    way, stacked by wind speed. It sits beside the wind speed line."""
    counts = np.asarray(compass.rose, dtype=float)                        # 16 compass points x 3 speed steps
    ax = fig.add_axes([0.685, 0.12, 0.27, 0.62], projection="polar")
    theta = np.deg2rad(np.arange(16) * 22.5)
    percent = counts / max(counts.sum(), 1) * 100
    bottom = np.zeros(16)
    for i, colour in enumerate(WIND_STEPS):
        ax.bar(theta, percent[:, i], width=np.deg2rad(20), bottom=bottom, color=colour, edgecolor=BG, linewidth=0.4)
        bottom += percent[:, i]
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
                   frameon=False, fontsize=7, labelcolor=MUTED, handletextpad=0.2, columnspacing=0.9)


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
        mx = sum(p[0] for p in g) / len(g)
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
                            va="center", fontsize=size, fontweight="bold", color="white", zorder=5, arrowprops=arrow(colour),
                            bbox=_box(colour))
        return
    placed = []                                                           # no empty column: the right margin, at the peaks' heights
    for mx, my, colour in peaks:
        ly = min(my, placed[-1] - pitch * per_pt) if placed else my
        placed.append(ly)
        _dot(ax, mx, my, colour)
        ax.annotate(f"{round(my, 1):g}", (mx, my), xytext=(1.03, ly), textcoords=("axes fraction", "data"), ha="left", va="center",
                    fontsize=size, fontweight="bold", color="white", zorder=5, annotation_clip=False, arrowprops=arrow(colour),
                    bbox=_box(colour))


def _end_labels(ax, drawn: list[tuple]):
    """The latest value of each line in the margin beside it, nudged apart where they would touch."""
    y_lo, y_hi = ax.get_ylim()
    if max(d[2][-1] for d in drawn) - y_lo < 0.05 * (y_hi - y_lo):
        return  # everything sits on the floor: nothing to read off
    height_pt = ax.get_position().height * ax.figure.get_figheight() * 72
    gap = 8 * (y_hi - y_lo) / height_pt                     # 8 points, in data units
    placed = []
    for colour, xs, ys in sorted(drawn, key=lambda d: d[2][-1]):
        y = max(ys[-1], placed[-1] + gap) if placed else ys[-1]
        placed.append(y)
        ax.annotate(f"{ys[-1]:.3g}", (xs[-1], ys[-1]), xytext=(1.014, y), textcoords=("axes fraction", "data"), va="center", ha="left",
                    fontsize=7, fontweight="bold", color=colour, annotation_clip=False,
                    arrowprops={"arrowstyle": "-", "color": colour, "linewidth": 0.6, "alpha": 0.6, "shrinkA": 0, "shrinkB": 2},
                    bbox={"boxstyle": "round,pad=0.15", "fc": "none", "ec": "none"})


def _draw_panel(ax, p: Panel, tz: tzinfo, first: int, x0: float, x1: float, look: Look):
    """One panel: lines (with bands and zones, peaks labelled, rain behind them), rain on its own, or shares. Returns the
    number of colours used, where the drawing ends on the x axis and the legend's entries (colour, name)."""
    if p.shares:
        s = p.shares  # the share of each bar's time in good / poor / very poor: traffic-light bars stacked to 100%
        bx = _nums(tz, s.x)
        base = np.zeros(len(bx))
        for column, colour in zip((s.good, s.poor, s.very_poor), ZONE_COLOURS):
            ax.bar(bx, column, bottom=base, width=s.width / 86400 * 0.85, align="edge", color=colour, linewidth=0, zorder=3)
            base += np.asarray(column, dtype=float)
        ax.set_ylim(0, 100)
        ax.legend([Line2D([], [], marker="s", linestyle="", markersize=5, color=c) for c in ZONE_COLOURS],
                  ["good", "poor", "very poor"], loc="lower right", bbox_to_anchor=(1.0, 1.0), frameon=False, fontsize=7,
                  labelcolor=TEXT, ncol=3, handletextpad=0.2, columnspacing=0.9, borderaxespad=0.1)
        return 0, _end_of(bx, s.width, x1), []
    if not p.lines:  # rain on its own
        b = p.bars
        bx = _nums(tz, b.x)
        top = max([*b.y, 1.0]) * 1.15
        _draw_bars(ax, bx, b.y, b.width / 86400 * 0.85, top, 3, alpha=0.75)
        ax.set_ylim(0, top)
        return 0, _end_of(bx, b.width, x1), []
    lines = p.lines
    marked = [i for i, s in enumerate(lines) if look.label_all or s.records]  # peaks labelled: beside the lines (aside), else as pills
    pilled = bool(marked) and not p.aside
    _pad_limits(ax, *_extent(lines, [float(r[1]) for s in lines for r in s.records.values()] if pilled else []),
                top=look.pad if pilled else 0.18 if marked else 0.12, bottom=look.pad if pilled else 0.12,
                floor=0)
    ybottom, ytop = ax.get_ylim()
    drawn = _draw_lines(ax, lines, tz, look, first, p.reading)
    if p.zones:  # a rated reading: its good / poor / very poor zones behind the line
        _shade_zones(ax, p.zones, ybottom, ytop, 0.07)
    for s, (colour, xs, ys) in zip(lines, drawn):
        if not s.low and len(lines) <= 2:  # a soft fade from the line to the floor (muddy with more lines, and a band says enough)
            _gradient_under(ax, xs, ys, colour, ybottom)
    ax.set_ylim(ybottom, ytop)
    if marked and p.aside:
        _mark_highs(ax, [lines[i] for i in marked], [drawn[i] for i in marked], x0, x1, look)
    elif marked:
        _pills(ax, [lines[i] for i in marked], [drawn[i] for i in marked], tz, x0, x1, look, _deg(p.unit))
    if p.bars:
        x1 = max(x1, _bars_behind(ax, p.bars, tz))
    if len(lines) > 1:
        _end_labels(ax, drawn)
    entries = [(_colour(s, first + i, p.reading), s.label) for i, s in enumerate(lines)] + ([(RAIN, p.bars.label)] if p.bars else [])
    return len(lines), x1, entries


def _render(chart: Chart, tz: tzinfo) -> bytes:
    """The panels top to bottom on one time axis, so the rain (or another reading) lines up with what the others were doing.
    A chart of one panel of lines is the same thing drawn BIG (and with its wind rose beside it, when it has one)."""
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
        xs = _nums(tz, [t for p in panels for t in p.xs])
        x0, x1 = float(xs.min()), float(xs.max())
        axes, used = [], 0
        for i, p in enumerate(panels):
            bottom = (FOOT_IN + (n - 1 - i) * (each + gap)) / height
            rect = [AX_RECT[0], bottom, width, each / height]
            if look.card:
                card = fig.add_axes(rect, facecolor=CARD, zorder=-1)  # the faint tint behind the panel
                card.set_xticks([])
                card.set_yticks([])
                for side in card.spines.values():
                    side.set_visible(False)
            ax = fig.add_axes(rect, facecolor="none" if look.card else BG, sharex=axes[0] if axes else None)
            axes.append(ax)
            _style_axis(ax, grid=0 if p.bars and p.lines else 0.8)   # with rain behind, the grid is drawn under it (_bars_behind)
            count, x1, entries = _draw_panel(ax, p, tz, used, x0, x1, look)
            used += count
            if len(entries) >= look.key_from:   # the colour key: beside the headline for a big chart, above the panel in a stack
                key = dict(loc="center right", bbox_to_anchor=(AX_RECT[0] + AX_RECT[2], 1 - 0.27 / height)) if look.in_headline else \
                    dict(loc="lower right", bbox_to_anchor=(1.0, 1.0), borderaxespad=0.1)
                _legend_dots(fig if look.in_headline else ax, [c for c, _ in entries], [label for _, label in entries], ncol=len(entries),
                             fontsize=look.legend_font, **key)
            deg = _deg(p.unit)
            tick_unit = deg if deg == "°" else ""
            ax.yaxis.set_major_locator(MaxNLocator(nbins=3 if n > 2 else look.nbins, steps=[1, 2, 2.5, 5, 10]))
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _, u=tick_unit: f"{v:g}{u}"))
            if not look.in_headline:
                title = f"{p.label} ({p.unit})" if p.unit and not tick_unit else p.label
                ax.set_title(title, loc="left", fontsize=8.5, fontweight=TITLE_WEIGHT, color=TEXT, pad=4)
            if i < n - 1:
                plt.setp(ax.get_xticklabels(), visible=False)
        margin = (x1 - x0) * 0.015  # room for the end dots
        axes[0].set_xlim(x0 - margin, x1 + margin)
        _time_axis(axes[-1], x1 - x0)
        if chart.compass:  # beside the wind line
            pos = axes[0].get_position()
            axes[0].set_position([pos.x0, pos.y0, 0.57, pos.height])
            _render_rose(fig, chart.compass)
    except Exception:
        plt.close(fig)
        raise
    return _png(fig)


def _png(fig) -> bytes:
    try:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=DPI, facecolor=BG)
        return buf.getvalue()
    finally:
        plt.close(fig)


_DRAWING = threading.Lock()   # pyplot keeps global state (current figure, rcParams): one chart is drawn at a time


def render(chart: Chart, tz: tzinfo) -> bytes:
    """PNG bytes for one chart (1280x720; a stack of more than two panels is taller). Safe to call from several threads."""
    with _DRAWING:
        return _render(chart, tz)
