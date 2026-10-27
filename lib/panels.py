"""How a reading becomes a chart panel, in one place: its title, unit, the reading that picks its colour and, for one rated
air-quality reading on its own, its good / poor / very poor zones. Every chart producer (a history answer, plot_chart, the
weather links, the air-quality charts) builds its panels here, so a reading looks the same whichever path drew it."""

from dataclasses import replace

from .series import AIR, AIR_EXTRA, WEATHER
from .specs import Bars, Line, Panel


def panel_for(name: str | list[str], lines: list[Line] | None = None, bars: Bars | None = None) -> Panel:
    """The panel for a weather reading ("humidity", "rain" ...), an air-quality metric ("pm2_5" ...) or several air metrics on one
    axis (the particles: their names as the title, peaks labelled beside the lines); with no lines, just its bars."""
    lines = lines or []
    if isinstance(name, str) and name in WEATHER:
        reading = WEATHER[name]
        return Panel(reading.label, reading.unit, lines, bars=bars, reading=name)
    if isinstance(name, str) and name in AIR_EXTRA:   # the sensor's own temperature or humidity: a line on its own axis, no zones
        return Panel(AIR_EXTRA[name].label, AIR_EXTRA[name].unit, lines, bars=bars, reading=name)
    names = [name] if isinstance(name, str) else name
    one = len(names) == 1
    return Panel(", ".join(AIR[n].label for n in names), AIR[names[0]].unit, lines, bars=bars, reading=names[0] if one else "",
                 zones=AIR[names[0]].limits if one and len(lines) == 1 else None, peaks="pills" if one else "aside")


def two_axes(panels: list[Panel]) -> Panel | None:
    """Panels of lines as one panel with two scales: the lines of the first against the left axis, those of the second against a
    right-hand axis of its own (a UV index of 0 to 12 beside a solar radiation of 0 to 1000). With more than two panels, those in the
    first panel's unit share the left axis and those in the one other unit the right (two humidities left, two temperatures right).
    Each line's name carries its unit, as the panel's heading no longer can. None when they are not panels that are only lines
    (rain bars and ratings can't share), or are more than two but not in exactly two units."""
    if len(panels) < 2 or any(not p.lines or p.bars or p.shares for p in panels):
        return None
    if len(panels) == 2:
        sides = [False, True]
    else:
        units = list(dict.fromkeys(p.unit for p in panels))
        if len(units) != 2:
            return None
        sides = [p.unit != units[0] for p in panels]
    lines = []
    for panel, right in zip(panels, sides):
        for line in panel.lines:
            name = line.label if len(panel.lines) > 1 else panel.label
            lines.append(replace(line, label=f"{name} ({panel.unit})" if panel.unit and len(panel.lines) == 1 else name,
                                 right=right, reading=line.reading or panel.reading))
    return Panel(" and ".join(p.label for p in panels), "", lines, peaks="none")


def same_axis(panels: list[Panel]) -> Panel | None:
    """Panels of lines in one unit as one panel with one axis, the lines over each other ("AirGradient humidity over Ecowitt humidity"):
    each line keeps its own colour and is named for the panel it came from. None unless there are two or more panels that are only lines,
    all in the same unit."""
    if len(panels) < 2 or any(not p.lines or p.bars or p.shares for p in panels) or len({p.unit for p in panels}) != 1:
        return None
    lines = [replace(line, label=line.label if len(panel.lines) > 1 else panel.label, reading=line.reading or panel.reading)
             for panel in panels for line in panel.lines]
    return Panel(" and ".join(p.label for p in panels), panels[0].unit, lines, peaks="none")
