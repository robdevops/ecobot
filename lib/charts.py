"""Charts sent alongside history answers, for both Ecowitt and AirGradient.

A history tool adds a chart spec to CHART_REQUESTS (a per-question list set by the
bot) when a chart was asked for; the bot renders them after the answer is written
and sends them with it. One line per reading type (e.g. outdoor and indoor):
  {"kind": "line", "title", "subtitle", "unit",
   "series": [{"label", "x": [epoch], "y": [float], "records": {"high": [epoch, value], "low": [...]},
               "low": [float], "high": [float]}]}   (low/high optional: each point's range, drawn as a band)

Several readings on one time axis, a panel each (weather_link, plot_chart, and "plot temperature and rain"):
  {"kind": "stack", "title", "subtitle", "panels": [
     {"label", "unit", "series": [{"label", "x", "y"[, "low", "high"]}][, "zones": [good limit, poor limit]]}   lines, or
     {"label", "unit", "bars": {"x": [bar start epoch], "y": [value], "width": seconds}}, or
     {"label", "unit", "shares": {"x": [bar start epoch], "width": seconds, "good": [%], "poor": [%], "very poor": [%]}}]}

A wind chart is a line spec that also carries the compass, a wind rose of the whole period drawn beside the line,
stacked by wind speed:
  {"kind": "line", ..., "rose": [[light, middle, strong] x 16, N first], "speeds": bool, "speed_steps": [10, 20]}

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
# True when the person's own words ask for a graph ("plot", "chart"...): the model sometimes forgets chart=true
CHART_ASKED: ContextVar[bool] = ContextVar("chart_asked", default=False)


# The reading a chart should plot, from the person's words ("humidity"); None means temperature
CHART_FIELD: ContextVar[str | None] = ContextVar("chart_field", default=None)
# The readings the person's words ask to see together ("plot temperature and rain"); empty means the usual chart
CHART_STACK: ContextVar[list] = ContextVar("chart_stack", default=[])
# True when the person asked for an average ("average temp 3m"): the caption then leads with the average
AVERAGE_ASKED: ContextVar[bool] = ContextVar("average_asked", default=False)
CHART_MIN_DAYS = 3  # a period of this many calendar days or more always gets a chart


def stack_spec(panels: list[dict], first, last) -> dict:
    """A stacked chart of these panels for the days first to last. What a bar covers (a "per" in a bars or shares panel)
    goes into the subtitle, not the panel."""
    per = None
    for p in panels:
        for kind in ("bars", "shares"):
            if kind in p:
                per = p[kind].pop("per", per)
    return {"kind": "stack", "title": " and ".join(p["label"] for p in panels),
            "subtitle": f"{first:%a} {first.day} {first:%b} – {last:%a} {last.day} {last:%b %Y}" + (f"  ·  per {per}" if per else ""),
            "panels": panels}


def wants_chart(args: dict, start: datetime | None = None, end: datetime | None = None) -> bool:
    """The model asked for one, the person's words did, or the period (naive local start/end) spans 3+ days."""
    long = bool(start and end and (end.date() - start.date()).days >= CHART_MIN_DAYS - 1)
    return bool(args.get("chart")) or CHART_ASKED.get() or long


# Added to a tool result when a chart was made, so the reply becomes a good caption
CHART_HINT = ("Your reply becomes the caption of a chart of this data, so keep it short: the period, then one line "
              "per series with its high and low, or its average if that is what was asked (for weather, one line each for Outdoor and Indoor when both were "
              "fetched; for air quality, the peak). No other lists or breakdowns; don't mention or describe the chart.")

AVERAGE_CHART_HINT = ("Your reply becomes the caption of a chart of this data, so keep it short: the period, then one line "
                      "per series (Outdoor and Indoor when both were fetched) with its AVERAGE, copied from the series' "
                      "\"average\" field, and its low and high in brackets. Lead with the average: that is what was asked. "
                      "Don't mention or describe the chart.")
LINK_CHART_HINT = ("Your reply becomes the caption of a chart with the reading as a line and rain as bars below it, so keep "
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


def _band(ax, xs, s: dict, colour: str, zorder: int):
    """A series' low-to-high range shaded behind its line, where it has one."""
    if s.get("low"):
        ax.fill_between(xs, s["low"], s["high"], color=colour, alpha=0.2, linewidth=0, zorder=zorder)


def _shade_zones(ax, zones, ybottom: float, ytop: float, alpha: float):
    """Faint rating zones behind a line: good up to z1, poor up to z2, very poor above."""
    z1, z2 = zones
    for lo, hi, colour in ((ybottom, z1, ZONE_COLOURS[0]), (z1, z2, ZONE_COLOURS[1]), (z2, ytop, ZONE_COLOURS[2])):
        if hi > ybottom and lo < ytop:
            ax.axhspan(max(lo, ybottom), min(hi, ytop), color=colour, alpha=alpha, linewidth=0, zorder=0)


def _frame(fig, ax, spec: dict, labels: list[str], colours: list[str], legend: bool = True):
    """Title, subtitle, dot legend and axis styling shared by the chart kinds."""
    unit = spec.get("unit", "")
    title = spec.get("title", "")
    if unit and _deg(unit) != "\u00b0":  # non-degree units go in the title; ticks stay plain numbers
        title = f"{title} ({unit})"
    fig.text(AX_RECT[0], 0.925, title, fontsize=13, fontweight=TITLE_WEIGHT, color=TEXT, va="center")
    fig.text(AX_RECT[0], 0.855, spec.get("subtitle", ""), fontsize=8.5, color=MUTED, va="center")
    if legend:
        handles = [Line2D([], [], marker="o", linestyle="", markersize=6, color=c) for c in colours]
        fig.legend(handles, labels, loc="center right", bbox_to_anchor=(AX_RECT[0] + AX_RECT[2], 0.925), ncol=len(labels),
                   frameon=False, fontsize=8.5, labelcolor=TEXT, handletextpad=0.2, columnspacing=1.1)
    _style_axis(ax)
    tick_unit = "\u00b0" if _deg(unit) == "\u00b0" else ""
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}{tick_unit}"))
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10]))


def _pad_limits(ax, lo: float, hi: float, top: float = 0.2, bottom: float = 0.16):
    span = max(hi - lo, 1.0)
    ax.set_ylim(lo - span * bottom, hi + span * top)


def _render_line(fig, ax, spec: dict, tz: tzinfo):
    to_dt = _to_dt(tz)
    series = spec["series"]
    rec_vals = [float(r[1]) for s in series for r in (s.get("records") or {}).values()]
    lo = min([min(s.get("low") or s["y"]) for s in series] + rec_vals)
    hi = max([max(s.get("high") or s["y"]) for s in series] + rec_vals)
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
        _band(ax, xs, s, colour, 3)  # each bucket's low-to-high range behind its average: the chart shows the full swing
        if not s.get("low") and len(series) <= 2:  # soft gradient fill under the line (muddy with more lines)
            poly = Polygon([(xs[0], ybottom), *zip(xs, ys), (xs[-1], ybottom)], closed=True, fc="none", ec="none")
            ax.add_patch(poly)
            rgba = np.zeros((256, 1, 4))
            rgba[..., :3] = to_rgb(colour)
            rgba[..., 3] = np.linspace(0.22, 0.0, 256)[:, None]
            img = ax.imshow(rgba, aspect="auto", extent=[xs.min(), xs.max(), ybottom, ys.max()], origin="upper", zorder=2)
            img.set_clip_path(poly)
        width = 1.3 if len(series) > 2 or s.get("low") else 1.5 if dense else 2.2
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
            elif records:  # only some records given (wind: the strongest gust, no lowest): no label for the other
                continue
            else:
                idx = int(ys.argmax() if above else ys.argmin())
                rx, ry = xs[idx], ys[idx]
            line_y = float(np.interp(rx, xs, ys))
            if abs(ry - line_y) > (0.005 * (ax.get_ylim()[1] - ax.get_ylim()[0]) if s.get("smoothed") else 1e-6) and not s.get("low"):  # well off the line: dotted stem back to it (not with a band: the band shows why)
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
    if spec.get("zones"):
        _shade_zones(ax, spec["zones"], ybottom, ytop, 0.07)
    ax.set_ylim(ybottom, ytop)
    _time_axis(ax, (x_max - x_min) / 86400)
    _frame(fig, ax, spec, labels, colours)


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


def _render_stack(fig, spec: dict, tz: tzinfo):
    """Several readings on one time axis, one panel each, top to bottom: lines (with a shaded range where there is
    one, and rating zones for air quality), bars (rain) or traffic-light shares. The rain lines up with what the other
    readings were doing at that moment."""
    to_dt = _to_dt(tz)
    panels = spec["panels"]
    n = len(panels)
    left, width, bottom, top = AX_RECT[0], AX_RECT[2], 0.13, 0.75
    gap = 0.075 if n > 2 else 0.06
    height = (top - bottom - gap * (n - 1)) / n
    xs_all = [mdates.date2num(to_dt(t)) for p in panels for s in (p.get("series") or [p.get("bars") or p["shares"]]) for t in s["x"]]
    x0, x1 = min(xs_all), max(xs_all)
    axes = []
    for i, p in enumerate(panels):
        ax = fig.add_axes([left, top - (i + 1) * height - i * gap, width, height], facecolor=BG,
                          sharex=axes[0] if axes else None)
        axes.append(ax)
        handles = []
        if "bars" in p:
            bars = p["bars"]
            bx = mdates.date2num([to_dt(t) for t in bars["x"]])
            ax.bar(bx, bars["y"], width=bars["width"] / 86400 * 0.85, align="edge", color="#0EA5E9", linewidth=0, zorder=3)
            ax.set_ylim(0, max([*bars["y"], 1.0]) * 1.15)
            x1 = max(x1, (bx.max() + bars["width"] / 86400) if len(bx) else x1)
        elif "shares" in p:  # the share of each bar's time in good / poor / very poor: traffic-light bars stacked to 100%
            shares = p["shares"]
            bx = mdates.date2num([to_dt(t) for t in shares["x"]])
            base = np.zeros(len(bx))
            for key, colour in zip(("good", "poor", "very poor"), ZONE_COLOURS):
                ax.bar(bx, shares[key], bottom=base, width=shares["width"] / 86400 * 0.85, align="edge", color=colour,
                       linewidth=0, zorder=3)
                base += np.asarray(shares[key], dtype=float)
            ax.set_ylim(0, 100)
            ax.legend([Line2D([], [], marker="s", linestyle="", markersize=5, color=c) for c in ZONE_COLOURS],
                      ["good", "poor", "very poor"], loc="lower right", bbox_to_anchor=(1.0, 1.0), frameon=False, fontsize=7,
                      labelcolor=TEXT, ncol=3, handletextpad=0.2, columnspacing=0.9, borderaxespad=0.1)
            x1 = max(x1, (bx.max() + shares["width"] / 86400) if len(bx) else x1)
        else:
            lows = highs = None
            for s in p["series"]:
                colour = _colour(s["label"], i)
                xs = mdates.date2num([to_dt(t) for t in s["x"]])
                _band(ax, xs, s, colour, 2)
                ax.plot(xs, s["y"], color=colour, linewidth=1.5, solid_joinstyle="round", zorder=3)
                lows = min(lows if lows is not None else 1e18, min(s.get("low") or s["y"]))
                highs = max(highs if highs is not None else -1e18, max(s.get("high") or s["y"]))
                handles.append(Line2D([], [], marker="o", linestyle="", markersize=5, color=colour))
            _pad_limits(ax, float(lows), float(highs), top=0.12, bottom=0.12)
            if p.get("zones"):  # an air-quality reading: its good / poor / very poor zones behind the line
                _shade_zones(ax, p["zones"], *ax.get_ylim(), 0.07)
            if len(p["series"]) > 1:
                ax.legend(handles, [s["label"] for s in p["series"]], loc="upper right", frameon=False, fontsize=7,
                          labelcolor=TEXT, ncol=len(handles), handletextpad=0.2, columnspacing=0.9, borderaxespad=0.1)
        _style_axis(ax)
        ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(nbins=3 if n > 2 else 4))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.set_title(f"{p['label']} ({p['unit']})" if p.get("unit") else p["label"], loc="left", fontsize=8.5,
                     fontweight=TITLE_WEIGHT, color=TEXT, pad=4)
        if i < n - 1:
            plt.setp(ax.get_xticklabels(), visible=False)
    axes[0].set_xlim(x0, x1)
    _frame(fig, axes[0], {**spec, "unit": ""}, [], [], legend=False)
    axes[0].grid(axis="y", color=GRID, linewidth=0.8)
    axes[0].yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    _time_axis(axes[-1], x1 - x0)


WIND_STEPS = ("#FED7AA", "#FB923C", "#C2410C")  # light, middle and strong wind


def _render_rose(fig, spec: dict):
    """The whole period as a wind rose (N up, clockwise): each of the 16 wedges is the share of readings from that
    way, stacked by wind speed. It sits beside the wind speed line."""
    counts = np.asarray(spec["rose"], dtype=float)                        # 16 compass points x 3 speed steps
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
    if spec.get("speeds"):
        a, b = spec.get("speed_steps", (10, 20))
        fig.legend([Line2D([], [], marker="s", linestyle="", markersize=6, color=c) for c in WIND_STEPS],
                   [f"under {a}", f"{a}–{b}", f"{b}+ km/h"], loc="lower right", bbox_to_anchor=(0.985, 0.0), ncol=3,
                   frameon=False, fontsize=7, labelcolor=MUTED, handletextpad=0.2, columnspacing=0.9)


def _render_panels(spec: dict, tz: tzinfo):
    """Several metrics in one image: a small line chart each (own axis and units), in one
    column for up to 3, otherwise two columns. Rating zones and each panel's peak shown."""
    to_dt = _to_dt(tz)
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
        lo, hi = float(min(p.get("low") or ys)), float(max(p.get("high") or ys))
        span = max(hi - lo, 1.0)
        ybottom, ytop = lo - span * 0.3, hi + span * 0.35
        if p.get("zones"):
            _shade_zones(ax, p["zones"], ybottom, ytop, 0.08)
        _band(ax, xs, p, colour, 2)  # each day's low-to-high range behind its mean
        ax.plot(xs, ys, color=colour, linewidth=1.2, solid_joinstyle="round", zorder=3)
        records = p.get("records") or {}
        marks = [(mdates.date2num(to_dt(records[w][0])), float(records[w][1]), above) for w, above in
                 (("high", True), ("low", False)) if w in records]
        if not marks:  # no records given: the highest point of the line
            h = int(ys.argmax())
            marks = [(xs[h], ys[h], True)]
        for mx, my, above in marks:
            ax.scatter([mx], [my], s=12, color=colour, edgecolors="white", linewidths=0.8, zorder=4)
            frac = (mx - xs[0]) / max(xs[-1] - xs[0], 1e-9)
            ax.annotate(f"{round(my, 1):g}", (mx, my), xytext=(0, 5 if above else -5), textcoords="offset points",
                        ha="left" if frac < 0.08 else "right" if frac > 0.92 else "center",
                        va="bottom" if above else "top", fontsize=6.5, fontweight="bold", color="white", zorder=5,
                        bbox={"boxstyle": "round,pad=0.25,rounding_size=0.6", "fc": colour, "ec": "none"})
        ax.set_xlim(xs[0], xs[-1])
        ax.set_ylim(ybottom, ytop)
        unit = p.get("unit", "")
        ax.set_title(f"{p['label']} ({unit})" if unit else p["label"], loc="left", fontsize=8.5,
                     fontweight=TITLE_WEIGHT, color=TEXT, pad=4)
        _style_axis(ax, size=6.5, pad=3, grid=0.6, below=False)
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
        if spec["kind"] == "stack":
            fig.delaxes(ax)
            _render_stack(fig, spec, tz)
        else:
            _render_line(fig, ax, spec, tz)
            if "rose" in spec:  # the compass beside the line
                ax.set_position([AX_RECT[0], AX_RECT[1], 0.57, AX_RECT[3]])
                _render_rose(fig, spec)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=DPI, facecolor=BG)
        return buf.getvalue()
    finally:
        plt.close(fig)
