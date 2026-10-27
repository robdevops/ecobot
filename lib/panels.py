"""How a reading becomes a chart panel, in one place: its title, unit, the reading that picks its colour and, for one rated
air-quality reading on its own, its good / poor / very poor zones. Every chart producer (a history answer, plot_chart, the
weather links, the air-quality charts) builds its panels here, so a reading looks the same whichever path drew it."""

from dataclasses import replace

from .series import AIR, WEATHER
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
    return Panel(", ".join(AIR[n].label for n in names), AIR[names[0]].unit, lines, bars=bars, reading=names[0] if one else "",
                 zones=AIR[names[0]].limits if one and len(lines) == 1 else None, peaks="pills" if one else "aside")


def two_axes(panels: list[Panel]) -> Panel | None:
    """Two panels of lines as one panel with two scales: the first one's lines against the left axis, the second's against a
    right-hand axis of its own (a UV index of 0 to 12 beside a solar radiation of 0 to 1000). Each line's name carries its unit,
    as the panel's heading no longer can. None when they are not two panels that are only lines (rain bars and ratings can't share)."""
    if len(panels) != 2 or any(not p.lines or p.bars or p.shares for p in panels):
        return None
    lines = []
    for panel, right in zip(panels, (False, True)):
        for line in panel.lines:
            name = line.label if len(panel.lines) > 1 else panel.label
            lines.append(replace(line, label=f"{name} ({panel.unit})" if panel.unit and len(panel.lines) == 1 else name,
                                 right=right, reading=line.reading or panel.reading))
    return Panel(" and ".join(p.label for p in panels), "", lines, peaks="none")
