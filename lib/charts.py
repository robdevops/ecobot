"""Charts sent alongside history answers, for both Ecowitt and AirGradient.

A history tool adds a specs.Chart to turn.charts (the question's Turn, made by the bot) when a chart was asked for; the
bot renders them after the answer is written and sends them with it. Every chart is one or more panels on a shared time
axis (see specs.py):
  - one panel of lines is drawn large, with the records marked as pills, an end dot and (for wind) the compass beside it,
    at exactly 1280x720, the size Telegram displays photos at, so nothing is rescaled;
  - several panels are stacked, the figure growing a little taller with each one past two.
Rain (bars) sits behind the lines of the panel it belongs to, on its own right-hand axis; a second unit gets a right-hand
axis of its own.
"""

import glob
import io
from datetime import datetime, timezone, tzinfo

import matplotlib

matplotlib.use("Agg")  # no display on the server
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.colors import to_rgb  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Polygon  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, MaxNLocator  # noqa: E402

from .specs import Chart, Compass, Line, Panel  # noqa: E402

CHART_MIN_DAYS = 3  # a period of this many calendar days or more always gets a chart


def wants_chart(args: dict, turn, start: datetime | None = None, end: datetime | None = None) -> bool:
    """The model asked for one, the person's words did (turn.chart_asked), or the period (naive local start/end) spans 3+ days."""
    long = bool(start and end and (end.date() - start.date()).days >= CHART_MIN_DAYS - 1)
    return bool(args.get("chart")) or turn.chart_asked or long


# Added to a tool result when a chart was made, so the reply becomes a good caption
CHART_HINT = ("Your reply becomes the caption of a chart of this data, so keep it short: the period, then one line "
              "per series with its high and low, or its average if that is what was asked (for weather, one line each for Outdoor and Indoor when both were "
              "fetched; for air quality, the peak). No other lists or breakdowns; don't mention or describe the chart.")

AVERAGE_CHART_HINT = ("Your reply becomes the caption of a chart of this data, so keep it short: the period, then one line "
                      "per series (Outdoor and Indoor when both were fetched) with its AVERAGE, copied from the series' "
                      "\"average\" field, and its low and high in brackets. Lead with the average: that is what was asked. "
                      "Don't mention or describe the chart.")
LINK_CHART_HINT = ("Your reply becomes the caption of a chart with the reading as a line and rain as bars behind it, so keep "
                   "it short: the period, then the finding in one or two lines (how much of the rain fell while the reading "
                   "was falling, and the correlation), citing the numbers. Don't mention or describe the chart.")
STACK_CHART_HINT = ("Your reply becomes the caption of a chart with these readings on one time axis, so keep it short: the "
                    "period, then one line per reading: temperature and other readings with their high and low (or their "
                    "average if that was asked), rain with its total (\"rain_total_mm\"). Don't mention or describe the chart.")
COMPOSED_CHART_HINT = ("Your reply becomes the caption of a chart of these readings on one time axis, so keep it short: the "
                       "period, then what the figures show about how they relate. Don't mention or describe the chart.")
DIRECTION_CHART_HINT = ("Your reply becomes the caption of a chart of wind: average speed with the gusts, and a compass of "
                        "where the wind came from. Keep it short: the period, the average speed and strongest gust, then the "
                        "most common direction and how steady it was. Don't mention or describe the chart.")

# Palette (slate neutrals, warm outdoor, cool indoor)
BG, TEXT, MUTED, GRID, AXIS = "#FFFFFF", "#0F172A", "#64748B", "#E2E8F0", "#CBD5E1"
COLOURS = {"Outdoor": "#F97316", "Indoor": "#6366F1",
           # air-quality metrics (kept clear of the green/yellow/red rating zones)
           "PM2.5": "#0EA5E9", "PM10": "#8B5CF6", "PM1": "#14B8A6", "CO₂": "#475569",
           "VOC index": "#D97706", "NOx index": "#DB2777"}
ZONE_COLOURS = ("#22C55E", "#EAB308", "#EF4444")  # good / poor / very poor
FALLBACK = ["#10B981", "#EC4899", "#0EA5E9"]
RAIN = "#0EA5E9"
WIND_STEPS = ("#FED7AA", "#FB923C", "#C2410C")  # light, middle and strong wind
W_IN, H_IN, DPI = 6.4, 3.6, 200  # 1280 x 720 px
AX_RECT = [0.075, 0.13, 0.905, 0.64]  # left, bottom, width, height (figure fraction) of a single chart
PANEL_IN = 1.1                   # each panel past two adds this much height (inches)
HEAD_IN, FOOT_IN = 0.9, 0.47     # room above and below the panels (inches)
BARS_SHARE = 0.3                 # rain behind a line never rises past this share of the panel's height


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


def _colour(label: str, i: int) -> str:
    return COLOURS.get(label, FALLBACK[i % len(FALLBACK)])


def _deg(unit: str) -> str:
    if not unit:
        return ""
    return "°" if unit.replace("º", "°") in ("°C", "°F", "°") else f" {unit}"


def _pill(ax, x, y, text, colour, above: bool, ha: str = "center"):
    ax.annotate(text, (x, y), xytext=(0, 9 if above else -9), textcoords="offset points",
                ha=ha, va="bottom" if above else "top", fontsize=7.5, fontweight="bold", color="white",
                bbox={"boxstyle": "round,pad=0.35,rounding_size=0.8", "fc": colour, "ec": "none"}, zorder=6)


def _to_dt(tz: tzinfo):
    """A function turning an epoch into a naive local datetime, for matplotlib's date axis."""
    return lambda t: datetime.fromtimestamp(t, timezone.utc).astimezone(tz).replace(tzinfo=None)


def _style_axis(ax, size: float = 7.5, pad: float = 5, grid: float = 0.8, below: bool = True):
    """The plain look shared by every chart: no frame but the baseline, faint horizontal grid, small muted ticks."""
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(axis="both", length=0, labelsize=size, labelcolor=MUTED, pad=pad)
    ax.grid(axis="y", color=GRID, linewidth=grid)
    if below:
        ax.set_axisbelow(True)


def _shade_zones(ax, zones, ybottom: float, ytop: float, alpha: float):
    """Faint rating zones behind a line: good up to z1, poor up to z2, very poor above."""
    z1, z2 = zones
    for lo, hi, colour in ((ybottom, z1, ZONE_COLOURS[0]), (z1, z2, ZONE_COLOURS[1]), (z2, ytop, ZONE_COLOURS[2])):
        if hi > ybottom and lo < ytop:
            ax.axhspan(max(lo, ybottom), min(hi, ytop), color=colour, alpha=alpha, linewidth=0, zorder=0)


def _pad_limits(ax, lo: float, hi: float, top: float = 0.2, bottom: float = 0.16):
    span = max(hi - lo, 1.0)
    ax.set_ylim(lo - span * bottom, hi + span * top)


def _extent(lines: list[Line], extra: list[float] = ()) -> tuple[float, float]:
    """The lowest and highest value drawn: the bands where there are any, else the lines (and `extra`, the records)."""
    return (min([min(s.low or s.y) for s in lines] + list(extra)), max([max(s.high or s.y) for s in lines] + list(extra)))


def _draw_lines(ax, lines: list[Line], tz: tzinfo, width, band_z: int, line_z: int, first: int = 0) -> list[tuple]:
    """Each line's low-to-high range behind it, then the line: [(colour, xs, ys)]. `width` is a number or a function of
    the line; `first` is the colour index of the first line."""
    to_dt, drawn = _to_dt(tz), []
    for i, s in enumerate(lines, first):
        colour = _colour(s.label, i)
        xs, ys = mdates.date2num([to_dt(t) for t in s.x]), np.asarray(s.y, dtype=float)
        if s.low:
            ax.fill_between(xs, s.low, s.high, color=colour, alpha=0.2, linewidth=0, zorder=band_z)
        ax.plot(xs, ys, color=colour, linewidth=width(s) if callable(width) else width, solid_capstyle="round",
                solid_joinstyle="round", zorder=line_z)
        drawn.append((colour, xs, ys))
    return drawn


def _bars_behind(ax, bars, tz: tzinfo) -> float:
    """Bars (rain) on their own right-hand axis, behind the panel's lines and never taller than BARS_SHARE of it. Returns
    where the last bar ends (matplotlib date number)."""
    to_dt = _to_dt(tz)
    bx = mdates.date2num([to_dt(t) for t in bars.x])
    ax2 = ax.twinx()
    ax2.bar(bx, bars.y, width=bars.width / 86400 * 0.85, align="edge", color=RAIN, alpha=0.55, linewidth=0, zorder=1)
    top = max([*bars.y, 1.0])
    ax2.set_ylim(0, top / BARS_SHARE)
    ax2.yaxis.set_major_locator(FixedLocator([top]))
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g} {bars.unit}"))
    for side in ("top", "right", "left", "bottom"):
        ax2.spines[side].set_visible(False)
    ax2.tick_params(axis="y", length=0, labelsize=7, labelcolor=RAIN, pad=4)
    ax.set_zorder(ax2.get_zorder() + 1)  # the lines above the bars
    ax.patch.set_visible(False)
    return float(bx.max() + bars.width / 86400) if len(bx) else 0.0


def _right_axis(ax, lines: list[Line], tz: tzinfo, first: int):
    """Lines in another unit on a right-hand axis; each axis takes the colour of its line when it has just one."""
    ax2 = ax.twinx()
    _draw_lines(ax2, lines, tz, 1.5, 2, 3, first)
    _pad_limits(ax2, *_extent(lines), top=0.12, bottom=0.12)
    for side in ("top", "right", "left", "bottom"):
        ax2.spines[side].set_visible(False)
    ax2.tick_params(axis="y", length=0, labelsize=7.5, labelcolor=_colour(lines[0].label, first) if len(lines) == 1 else MUTED, pad=5)
    ax2.yaxis.set_major_locator(MaxNLocator(nbins=4))
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))


def _axes_width(chart: Chart) -> float:
    """The plot's width: narrower when a panel has a right-hand axis, to leave room for its labels."""
    return AX_RECT[2] - (0.07 if any(p.right or (p.lines and p.bars) for p in chart.panels) else 0)


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
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, round(span_days / 10))))
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


# ---------- one panel of lines, drawn large ----------
def _render_single(fig, chart: Chart, tz: tzinfo):
    """The whole chart is one line panel: records as pills on the line, an end dot, a soft fill under one or two lines."""
    to_dt = _to_dt(tz)
    panel = chart.panels[0]
    lines = panel.lines
    ax = fig.add_axes([*AX_RECT[:2], _axes_width(chart), AX_RECT[3]], facecolor=BG)
    _pad_limits(ax, *_extent(lines, [float(r[1]) for s in lines for r in s.records.values()]), top=0.26, bottom=0.26)  # room for pills
    ybottom = ax.get_ylim()[0]
    x_min, x_max = min(t for s in lines for t in s.x), max(t for s in lines for t in s.x)
    dense = max(len(s.x) for s in lines) > 200
    width = lambda s: 1.3 if len(lines) > 2 or s.low else 1.5 if dense else 2.2
    drawn = _draw_lines(ax, lines, tz, width, 3, 4)
    pills = []
    x0, x1 = mdates.date2num(to_dt(x_min)), mdates.date2num(to_dt(x_max))
    edge = lambda x: "left" if (x - x0) / (x1 - x0) < 0.06 else "right" if (x - x0) / (x1 - x0) > 0.94 else "center"
    deg = _deg(panel.unit)
    for s, (colour, xs, ys) in zip(lines, drawn):
        if not s.low and len(lines) <= 2:  # soft gradient fill under the line (muddy with more lines)
            poly = Polygon([(xs[0], ybottom), *zip(xs, ys), (xs[-1], ybottom)], closed=True, fc="none", ec="none")
            ax.add_patch(poly)
            rgba = np.zeros((256, 1, 4))
            rgba[..., :3] = to_rgb(colour)
            rgba[..., 3] = np.linspace(0.22, 0.0, 256)[:, None]
            img = ax.imshow(rgba, aspect="auto", extent=[xs.min(), xs.max(), ybottom, ys.max()], origin="upper", zorder=2)
            img.set_clip_path(poly)
        ax.scatter([xs[-1]], [ys[-1]], s=30, color=colour, edgecolors="white", linewidths=1.5, zorder=5)
        for want, above in (("high", True), ("low", False)):
            if want in s.records:  # the true record, at its actual time (may sit off an averaged line)
                rx, ry = mdates.date2num(to_dt(s.records[want][0])), float(s.records[want][1])
                rx = min(max(rx, x0), x1)
            elif s.records:  # only some records given (wind: the strongest gust, no lowest): no label for the other
                continue
            else:
                idx = int(ys.argmax() if above else ys.argmin())
                rx, ry = xs[idx], ys[idx]
            line_y = float(np.interp(rx, xs, ys))
            if abs(ry - line_y) > (0.005 * (ax.get_ylim()[1] - ax.get_ylim()[0]) if s.smoothed else 1e-6) and not s.low:
                ax.vlines(rx, min(ry, line_y), max(ry, line_y), colors=colour, linestyles=(0, (1, 2)),  # well off the line: a dotted stem back to it
                          linewidth=1.2, alpha=0.8, zorder=3)
            ax.scatter([rx], [ry], s=18, color=colour, edgecolors="white", linewidths=1.2, zorder=6)
            pills.append([rx, ry, f"{ry:.1f}{deg}", colour, above, edge(rx)])
    # Records that land close together (e.g. outdoor and indoor on the same hot day) go side by side
    y_lo, y_hi = ax.get_ylim()
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
        _pill(ax, rx, ry, text, colour, above=above, ha=ha)
    if panel.bars:
        x1 = max(x1, _bars_behind(ax, panel.bars, tz))
    ax.set_xlim(x0 - (x1 - x0) * 0.015, x1 + (x1 - x0) * 0.015)  # room for the end dot
    ytop = ax.get_ylim()[1]
    if panel.zones:
        _shade_zones(ax, panel.zones, ybottom, ytop, 0.07)
    ax.set_ylim(ybottom, ytop)
    _time_axis(ax, (x_max - x_min) / 86400)
    _headline(fig, panel.label, chart.subtitle, H_IN, panel.unit)
    legend = [(c, s.label) for (c, _, _), s in zip(drawn, lines)] + ([(RAIN, panel.bars.label)] if panel.bars else [])
    _legend_dots(fig, [c for c, _ in legend], [label for _, label in legend], loc="center right",
                 bbox_to_anchor=(AX_RECT[0] + AX_RECT[2], 1 - 0.27 / H_IN), ncol=len(legend), fontsize=8.5)
    _style_axis(ax)
    tick_unit = "°" if _deg(panel.unit) == "°" else ""
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}{tick_unit}"))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10]))
    if chart.compass:  # beside the line
        ax.set_position([AX_RECT[0], AX_RECT[1], 0.57, AX_RECT[3]])
        _render_rose(fig, chart.compass)


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
def _mark_records(ax, line: Line, colour: str, tz: tzinfo, x0: float, x1: float):
    """A small labelled dot on each true record of a line."""
    to_dt = _to_dt(tz)
    for want, above in (("high", True), ("low", False)):
        if want not in line.records:
            continue
        mx, my = mdates.date2num(to_dt(line.records[want][0])), float(line.records[want][1])
        frac = (mx - x0) / max(x1 - x0, 1e-9)
        ax.scatter([mx], [my], s=12, color=colour, edgecolors="white", linewidths=0.8, zorder=4)
        ax.annotate(f"{round(my, 1):g}", (mx, my), xytext=(0, 5 if above else -5), textcoords="offset points",
                    ha="left" if frac < 0.08 else "right" if frac > 0.92 else "center", va="bottom" if above else "top",
                    fontsize=6.5, fontweight="bold", color="white", zorder=5,
                    bbox={"boxstyle": "round,pad=0.25,rounding_size=0.6", "fc": colour, "ec": "none"})


def _draw_panel(ax, p: Panel, tz: tzinfo, first: int, x0: float, x1: float):
    """One panel of a stack: lines (with bands, zones, a right-hand axis), rain behind them, or shares. Returns the
    number of colours used and where the drawing ends on the x axis."""
    to_dt = _to_dt(tz)
    if p.shares:
        s = p.shares  # the share of each bar's time in good / poor / very poor: traffic-light bars stacked to 100%
        bx = mdates.date2num([to_dt(t) for t in s.x])
        base = np.zeros(len(bx))
        for column, colour in zip((s.good, s.poor, s.very_poor), ZONE_COLOURS):
            ax.bar(bx, column, bottom=base, width=s.width / 86400 * 0.85, align="edge", color=colour, linewidth=0, zorder=3)
            base += np.asarray(column, dtype=float)
        ax.set_ylim(0, 100)
        ax.legend([Line2D([], [], marker="s", linestyle="", markersize=5, color=c) for c in ZONE_COLOURS],
                  ["good", "poor", "very poor"], loc="lower right", bbox_to_anchor=(1.0, 1.0), frameon=False, fontsize=7,
                  labelcolor=TEXT, ncol=3, handletextpad=0.2, columnspacing=0.9, borderaxespad=0.1)
        return 0, float(bx.max() + s.width / 86400) if len(bx) else x1
    if not p.lines:  # rain on its own
        b = p.bars
        bx = mdates.date2num([to_dt(t) for t in b.x])
        ax.bar(bx, b.y, width=b.width / 86400 * 0.85, align="edge", color=RAIN, linewidth=0, zorder=3)
        ax.set_ylim(0, max([*b.y, 1.0]) * 1.15)
        return 0, float(bx.max() + b.width / 86400) if len(bx) else x1
    drawn = _draw_lines(ax, p.lines, tz, 1.5, 2, 3, first)
    marked = len(p.lines) + len(p.right) == 1 and bool(p.lines[0].records)  # a lone line has its records labelled
    _pad_limits(ax, *_extent(p.lines, [float(r[1]) for r in p.lines[0].records.values()] if marked else []),
                top=0.3 if marked else 0.12, bottom=0.3 if marked else 0.12)
    if p.zones:  # a rated reading: its good / poor / very poor zones behind the line
        _shade_zones(ax, p.zones, *ax.get_ylim(), 0.07)
    if p.right:
        _right_axis(ax, p.right, tz, first + len(p.lines))
        if len(p.lines) == 1:
            ax.tick_params(axis="y", labelcolor=drawn[0][0])
    if marked:
        _mark_records(ax, p.lines[0], drawn[0][0], tz, x0, x1)
    if p.bars:
        x1 = max(x1, _bars_behind(ax, p.bars, tz))
    entries = [(_colour(s.label, first + i), s.label) for i, s in enumerate((*p.lines, *p.right))]
    if len(entries) + bool(p.bars) > 1:
        legend = entries + ([(RAIN, p.bars.label)] if p.bars else [])
        _legend_dots(ax, [c for c, _ in legend], [label for _, label in legend], loc="lower right",
                     bbox_to_anchor=(1.0, 1.0), ncol=len(legend), fontsize=7, borderaxespad=0.1)
    return len(entries), x1


def _render_stack(fig, chart: Chart, tz: tzinfo, height: float):
    """The panels top to bottom on one time axis, so the rain (or another reading) lines up with what the others were doing."""
    to_dt = _to_dt(tz)
    panels, n = chart.panels, len(chart.panels)
    _headline(fig, chart.title, chart.subtitle, height)
    width = _axes_width(chart)
    body = height - HEAD_IN - FOOT_IN
    gap = 0.27 if n > 2 else 0.22
    each = (body - gap * (n - 1)) / n
    xs = [mdates.date2num(to_dt(t)) for p in panels for t in p.xs]
    x0, x1 = min(xs), max(xs)
    axes, used = [], 0
    for i, p in enumerate(panels):
        bottom = (FOOT_IN + (n - 1 - i) * (each + gap)) / height
        ax = fig.add_axes([AX_RECT[0], bottom, width, each / height], facecolor=BG, sharex=axes[0] if axes else None)
        axes.append(ax)
        _style_axis(ax)
        count, x1 = _draw_panel(ax, p, tz, used, x0, x1)
        used += count
        ax.yaxis.set_major_locator(MaxNLocator(nbins=3 if n > 2 else 4))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.set_title(f"{p.label} ({p.unit})" if p.unit else p.label, loc="left", fontsize=8.5, fontweight=TITLE_WEIGHT, color=TEXT, pad=4)
        if i < n - 1:
            plt.setp(ax.get_xticklabels(), visible=False)
    axes[0].set_xlim(x0, x1)
    _time_axis(axes[-1], x1 - x0)


def _png(fig) -> bytes:
    try:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=DPI, facecolor=BG)
        return buf.getvalue()
    finally:
        plt.close(fig)


def render(chart: Chart, tz: tzinfo) -> bytes:
    """PNG bytes for one chart (1280x720; a stack of more than two panels is taller)."""
    first = chart.panels[0]
    single = len(chart.panels) == 1 and bool(first.lines) and not first.right
    height = H_IN if single or len(chart.panels) <= 2 else H_IN + PANEL_IN * (len(chart.panels) - 2)
    fig = plt.figure(figsize=(W_IN, height), dpi=DPI, facecolor=BG)
    try:
        (_render_single(fig, chart, tz) if single else _render_stack(fig, chart, tz, height))
    except Exception:
        plt.close(fig)
        raise
    return _png(fig)
