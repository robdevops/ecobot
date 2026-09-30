"""How a reading becomes a chart panel, in one place: its title, unit, the reading that picks its colour and, for one rated
air-quality reading on its own, its good / poor / very poor zones. Every chart producer (a history answer, plot_chart, the
weather links, the air-quality charts) builds its panels here, so a reading looks the same whichever path drew it."""

from .airgradient.metrics import CHART_UNITS, LABELS, RATINGS
from .series import WEATHER
from .specs import Bars, Line, Panel


def panel_for(name: str | list[str], lines: list[Line] | None = None, bars: Bars | None = None) -> Panel:
    """The panel for a weather reading ("humidity", "rain" ...), an air-quality metric ("pm2_5" ...) or several air metrics on one
    axis (the particles: their names as the title, peaks labelled beside the lines); with no lines, just its bars."""
    lines = lines or []
    if isinstance(name, str) and name in WEATHER:
        reading = WEATHER[name]
        return Panel(reading.label, reading.unit, lines, bars=bars, reading=name)
    names = [name] if isinstance(name, str) else name
    one = len(names) == 1
    return Panel(", ".join(LABELS[n] for n in names), CHART_UNITS[names[0]], lines, bars=bars, reading=names[0] if one else "",
                 zones=tuple(RATINGS[names[0]]) if one and len(lines) == 1 else None, peaks="pills" if one else "aside")
