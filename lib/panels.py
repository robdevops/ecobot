"""How a reading becomes a chart panel, in one place: its title, unit, the reading that picks its colour and, for one rated
air-quality reading on its own, its good / poor / very poor zones. Every chart producer (a history answer, plot_chart, the
weather links, the air-quality charts) builds its panels here, so a reading looks the same whichever path drew it."""

from .airgradient.metrics import CHART_UNITS, LABELS, RATINGS
from .series import WEATHER
from .specs import Bars, Line, Panel


def panel_for(name: str, lines: list[Line] | None = None, bars: Bars | None = None) -> Panel:
    """The panel for a weather reading ("humidity", "rain" ...) or an air-quality metric ("pm2_5" ...); with no lines, just its bars."""
    lines = lines or []
    if name in WEATHER:
        reading = WEATHER[name]
        return Panel(reading.label, reading.unit, lines, bars=bars, reading=name)
    return Panel(LABELS[name], CHART_UNITS[name], lines, bars=bars, reading=name, zones=tuple(RATINGS[name]) if len(lines) == 1 else None)


def air_group_panel(names: list[str], lines: list[Line]) -> Panel:
    """Several air metrics on one axis (the particles): their names as the title, peaks labelled beside the lines."""
    return Panel(", ".join(LABELS[n] for n in names), CHART_UNITS[names[0]], lines,
                 zones=tuple(RATINGS[names[0]]) if len(names) == 1 else None, aside=len(names) > 1)
