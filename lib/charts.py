"""Charts sent alongside history answers, for both Ecowitt and AirGradient.

A history tool adds a chart spec to CHART_REQUESTS (a per-question list set by the
bot) when a chart was asked for; the bot renders them after the answer is written
and sends them with it. One line per reading type (e.g. outdoor and indoor):
  {"kind": "line", "title", "subtitle", "unit",
   "series": [{"label", "x": [epoch], "y": [float], "records": {"high": [epoch, value], "low": [...]}}]}

Wind direction has no line (it is circular), so it gets a scatter of each reading by hour of day:
  {"kind": "direction", "title", "subtitle", "points": [[local hour 0-24, degrees]]}

Rendered at exactly 1280x720, the size Telegram displays photos at, so nothing is
rescaled and the chart stays sharp.
"""

import glob
import io
from contextvars import ContextVar
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
from matplotlib.ticker import FuncFormatter  # noqa: E402

# Set per question by the bot; the history tools append chart specs to it
CHART_REQUESTS: ContextVar[list | None] = ContextVar("chart_requests", default=None)
# Added to a tool result when a chart was made, so the reply becomes a good caption
CHART_HINT = ("Your reply becomes the caption of a chart of this data, so keep it short: the period, then one line "
              "per series with its high and low (for weather, one line each for Outdoor and Indoor when both were "
              "fetched; for air quality, the peak). No other lists or breakdowns; don't mention or describe the chart.")

DIRECTION_CHART_HINT = ("Your reply becomes the caption of a chart of wind direction by hour of day, so keep it short: "
                        "the period, then the most common direction and how steady it was. Don't mention or describe the chart.")

# Palette (slate neutrals, warm outdoor, cool indoor)
BG, TEXT, MUTED, GRID, AXIS = "#FFFFFF", "#0F172A", "#64748B", "#E2E8F0", "#CBD5E1"
COLOURS = {"Outdoor": "#F97316", "Indoor": "#6366F1",
           # air-quality metrics (kept clear of the green/yellow/red rating zones)
           "PM2.5": "#0EA5E9", "PM10": "#8B5CF6", "PM1": "#14B8A6", "CO\u2082": "#475569",
           "VOC index": "#D97706", "NOx index": "#DB2777"}
ZONE_COLOURS = ("#22C55E", "#EAB308", "#EF4444")  # good / poor / very poor
FALLBACK = ["#10B981", "#EC4899", "#0EA5E9"]
W_IN, H_IN, DPI = 6.4, 3.6, 200  # 1280 x 720 px
AX_RECT = [0.075, 0.13, 0.905, 0.64]  # left, bottom, width, height (figure fraction)


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


def _frame(fig, ax, spec: dict, labels: list[str], colours: list[str]):
    """Title, subtitle, dot legend and axis styling shared by both chart kinds."""
    unit = spec.get("unit", "")
    title = spec.get("title", "")
    if unit and _deg(unit) != "\u00b0":  # non-degree units go in the title; ticks stay plain numbers
        title = f"{title} ({unit})"
    fig.text(AX_RECT[0], 0.925, title, fontsize=13, fontweight=TITLE_WEIGHT, color=TEXT, va="center")
    fig.text(AX_RECT[0], 0.855, spec.get("subtitle", ""), fontsize=8.5, color=MUTED, va="center")
    handles = [Line2D([], [], marker="o", linestyle="", markersize=6, color=c) for c in colours]
    fig.legend(handles, labels, loc="center right", bbox_to_anchor=(AX_RECT[0] + AX_RECT[2], 0.925), ncol=len(labels),
               frameon=False, fontsize=8.5, labelcolor=TEXT, handletextpad=0.2, columnspacing=1.1)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(axis="both", length=0, labelsize=7.5, labelcolor=MUTED, pad=5)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    tick_unit = "\u00b0" if _deg(unit) == "\u00b0" else ""
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}{tick_unit}"))
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10]))


def _pad_limits(ax, lo: float, hi: float, top: float = 0.2, bottom: float = 0.16):
    span = max(hi - lo, 1.0)
    ax.set_ylim(lo - span * bottom, hi + span * top)


def _render_line(fig, ax, spec: dict, tz: tzinfo):
    to_dt = lambda t: datetime.fromtimestamp(t, timezone.utc).astimezone(tz).replace(tzinfo=None)
    series = spec["series"]
    rec_vals = [float(r[1]) for s in series for r in (s.get("records") or {}).values()]
    lo = min([min(s["y"]) for s in series] + rec_vals)
    hi = max([max(s["y"]) for s in series] + rec_vals)
    _pad_limits(ax, lo, hi, top=0.26, bottom=0.26)  # room for pills
    ybottom = ax.get_ylim()[0]
    x_min = min(min(s["x"]) for s in series)
    x_max = max(max(s["x"]) for s in series)
    labels, colours, pills = [], [], []
    dense = max(len(s["x"]) for s in series) > 200
    for i, s in enumerate(series):
        colour = _colour(s["label"], i)
        labels.append(s["label"]); colours.append(colour)
        xs = mdates.date2num([to_dt(t) for t in s["x"]])
        ys = np.asarray(s["y"], dtype=float)
        if len(series) <= 2:  # soft gradient fill under the line (muddy with more lines)
            poly = Polygon([(xs[0], ybottom), *zip(xs, ys), (xs[-1], ybottom)], closed=True, fc="none", ec="none")
            ax.add_patch(poly)
            rgba = np.zeros((256, 1, 4))
            rgba[..., :3] = to_rgb(colour)
            rgba[..., 3] = np.linspace(0.22, 0.0, 256)[:, None]
            img = ax.imshow(rgba, aspect="auto", extent=[xs.min(), xs.max(), ybottom, ys.max()], origin="upper", zorder=2)
            img.set_clip_path(poly)
        width = 1.3 if len(series) > 2 else 1.5 if dense else 2.2
        ax.plot(xs, ys, color=colour, linewidth=width, solid_capstyle="round", solid_joinstyle="round", zorder=4)
        ax.scatter([xs[-1]], [ys[-1]], s=30, color=colour, edgecolors="white", linewidths=1.5, zorder=5)
        deg = _deg(spec.get("unit", ""))
        x0, x1 = mdates.date2num(to_dt(x_min)), mdates.date2num(to_dt(x_max))
        edge = lambda x: "left" if (x - x0) / (x1 - x0) < 0.06 else "right" if (x - x0) / (x1 - x0) > 0.94 else "center"
        records = s.get("records") or {}
        for want, above in (("high", True), ("low", False)):
            if want in records:  # the true record, at its actual time (may sit off an averaged line)
                rx, ry = mdates.date2num(to_dt(records[want][0])), float(records[want][1])
                rx = min(max(rx, x0), x1)
            else:
                idx = int(ys.argmax() if above else ys.argmin())
                rx, ry = xs[idx], ys[idx]
            line_y = float(np.interp(rx, xs, ys))
            if abs(ry - line_y) > 1e-6:  # record is off an averaged line: dotted stem back to it
                ax.vlines(rx, min(ry, line_y), max(ry, line_y), colors=colour, linestyles=(0, (1, 2)),
                          linewidth=1.2, alpha=0.8, zorder=3)
            ax.scatter([rx], [ry], s=18, color=colour, edgecolors="white", linewidths=1.2, zorder=6)
            pills.append([rx, ry, f"{ry:.1f}{deg}", colour, above, edge(rx)])
    # Records that land close together (e.g. outdoor and indoor on the same hot day) go side by side
    x0, x1 = mdates.date2num(to_dt(x_min)), mdates.date2num(to_dt(x_max))
    y_lo, y_hi = ax.get_ylim()
    for a in range(len(pills)):
        for b in range(a + 1, len(pills)):
            p, q = pills[a], pills[b]
            if (p[4] == q[4] and abs(p[0] - q[0]) < (x1 - x0) * 0.08
                    and abs(p[1] - q[1]) < (y_hi - y_lo) * 0.15):
                left, right = (p, q) if p[0] <= q[0] else (q, p)
                near_edge = (left[0] - x0) / (x1 - x0) < 0.12 or (x1 - right[0]) / (x1 - x0) < 0.12
                if near_edge:  # no room to push sideways: put the lower one's pill below its point
                    lower = p if p[1] <= q[1] else q
                    lower[4] = False
                else:
                    left[5], right[5] = "right", "left"
    for rx, ry, text, colour, above, ha in pills:
        _pill(ax, rx, ry, text, colour, above=above, ha=ha)
    ax.set_xlim(x0 - (x1 - x0) * 0.015, x1 + (x1 - x0) * 0.015)  # room for the end dot
    ytop = ax.get_ylim()[1]
    if spec.get("zones"):  # faint rating zones behind the line: good up to z1, poor up to z2, very poor above
        z1, z2 = spec["zones"]
        for lo_z, hi_z, colour in ((ybottom, z1, ZONE_COLOURS[0]), (z1, z2, ZONE_COLOURS[1]), (z2, ytop, ZONE_COLOURS[2])):
            if hi_z > ybottom and lo_z < ytop:
                ax.axhspan(max(lo_z, ybottom), min(hi_z, ytop), color=colour, alpha=0.07, linewidth=0, zorder=0)
    ax.set_ylim(ybottom, ytop)
    span_days = (x_max - x_min) / 86400
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
    _frame(fig, ax, spec, labels, colours)


def _render_direction(fig, ax, spec: dict):
    """Each reading as a dot: hour of day across, compass direction up. Days overlay each other, so a
    sea breeze or an evening turn shows as a band."""
    hours, degrees = (np.asarray(v, dtype=float) for v in zip(*spec["points"]))
    colour = _colour("Outdoor", 0)
    ax.scatter(hours, degrees, s=9, color=colour, alpha=0.3, linewidths=0, zorder=3)
    _frame(fig, ax, {**spec, "unit": ""}, [spec.get("label", "Wind direction")], [colour])
    ax.set_xlim(0, 24)
    ax.set_ylim(-12, 372)
    ax.set_yticks([0, 90, 180, 270, 360])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: {0: "N", 90: "E", 180: "S", 270: "W", 360: "N"}.get(int(v), "")))
    ax.set_xticks(range(0, 24, 3))
    ax.xaxis.set_major_formatter(FuncFormatter(
        lambda v, _: f"{int(v) % 12 or 12}{'am' if int(v) % 24 < 12 else 'pm'}"))


def _render_panels(spec: dict, tz: tzinfo):
    """Several metrics in one image: a small line chart each (own axis and units), in one
    column for up to 3, otherwise two columns. Rating zones and each panel's peak shown."""
    to_dt = lambda t: datetime.fromtimestamp(t, timezone.utc).astimezone(tz).replace(tzinfo=None)
    panels = spec["panels"]
    n = len(panels)
    cols = 1 if n <= 3 else 2
    rows = -(-n // cols)
    height = 1.05 + rows * 1.7
    fig = plt.figure(figsize=(W_IN, height), dpi=DPI, facecolor=BG)
    fig.text(0.075, 1 - 0.38 / height, spec.get("title", ""), fontsize=13, fontweight=TITLE_WEIGHT, color=TEXT, va="center")
    fig.text(0.075, 1 - 0.68 / height, spec.get("subtitle", ""), fontsize=8.5, color=MUTED, va="center")
    grid = fig.add_gridspec(rows, cols, left=0.085, right=0.975, top=1 - 1.05 / height, bottom=0.45 / height,
                            hspace=0.85, wspace=0.22)
    for i, p in enumerate(panels):
        ax = fig.add_subplot(grid[i // cols, i % cols], facecolor=BG)
        colour = _colour(p["label"], i)
        xs = mdates.date2num([to_dt(t) for t in p["x"]])
        ys = np.asarray(p["y"], dtype=float)
        lo, hi = float(ys.min()), float(ys.max())
        span = max(hi - lo, 1.0)
        ybottom, ytop = lo - span * 0.12, hi + span * 0.35
        if p.get("zones"):
            z1, z2 = p["zones"]
            for a, b, zc in ((ybottom, z1, ZONE_COLOURS[0]), (z1, z2, ZONE_COLOURS[1]), (z2, ytop, ZONE_COLOURS[2])):
                if b > ybottom and a < ytop:
                    ax.axhspan(max(a, ybottom), min(b, ytop), color=zc, alpha=0.08, linewidth=0, zorder=0)
        ax.plot(xs, ys, color=colour, linewidth=1.2, solid_joinstyle="round", zorder=3)
        h = int(ys.argmax())
        ax.scatter([xs[h]], [ys[h]], s=12, color=colour, edgecolors="white", linewidths=0.8, zorder=4)
        frac = (xs[h] - xs[0]) / max(xs[-1] - xs[0], 1e-9)
        ax.annotate(f"{ys[h]:g}", (xs[h], ys[h]), xytext=(0, 5), textcoords="offset points",
                    ha="left" if frac < 0.08 else "right" if frac > 0.92 else "center", va="bottom",
                    fontsize=6.5, fontweight="bold", color="white", zorder=5,
                    bbox={"boxstyle": "round,pad=0.25,rounding_size=0.6", "fc": colour, "ec": "none"})
        ax.set_xlim(xs[0], xs[-1])
        ax.set_ylim(ybottom, ytop)
        unit = p.get("unit", "")
        ax.set_title(f"{p['label']} ({unit})" if unit else p["label"], loc="left", fontsize=8.5,
                     fontweight=TITLE_WEIGHT, color=TEXT, pad=4)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(AXIS)
        ax.tick_params(axis="both", length=0, labelsize=6.5, labelcolor=MUTED, pad=3)
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=3))
        days = (p["x"][-1] - p["x"][0]) / 86400
        if days <= 1.5:
            ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 6)))
            fmt = "%-I%p"
        else:
            ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, round(days / (4 if cols > 1 else 7)))))
            fmt = "%a %-d"
        ax.xaxis.set_major_formatter(FuncFormatter(
            lambda v, _, f=fmt: mdates.num2date(v).strftime(f).replace("AM", "am").replace("PM", "pm")))
    return fig


def render(spec: dict, tz: tzinfo) -> bytes:
    """PNG bytes for one chart spec (1280x720, or taller for several panels)."""
    if spec["kind"] == "panels":
        fig = _render_panels(spec, tz)
        try:
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=DPI, facecolor=BG)
            return buf.getvalue()
        finally:
            plt.close(fig)
    fig = plt.figure(figsize=(W_IN, H_IN), dpi=DPI, facecolor=BG)
    ax = fig.add_axes(AX_RECT, facecolor=BG)
    try:
        if spec["kind"] == "direction":
            _render_direction(fig, ax, spec)
        else:
            _render_line(fig, ax, spec, tz)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=DPI, facecolor=BG)
        return buf.getvalue()
    finally:
        plt.close(fig)
