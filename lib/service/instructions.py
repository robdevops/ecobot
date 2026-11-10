"""The server's instructions (docs/mcp-service.md 3.2): a short summary of what the tools read, for a client that passes it to its model
(over MCP a client gets it by asking: `server/discover`). Nothing depends on it: every rule a choice of tool or the reading of a result
depends on is in the tool's description or in the result, and the evals are run with and without it (`--live --service
--instructions`, docs 7).

It says: the devices there are (the Context's sources) and the nicknames people use for them, which readings are Indoor and Outdoor,
the readings' names, the units each reading comes in (from the catalogue: only the readings held), the station's time zone, that a period is passed on
in the person's own words, and that every question is fetched afresh (the results say so too, `fresh`, but a client may drop
earlier tool turns from its history). `instructions(ctx)` builds it; INSTRUCTIONS is it for every device and every reading the service knows.
"""

from .names import AIR_DEVICE, CATALOGUE, STATION, Catalogue

PLAIN = ("Outdoor ", "Indoor ", "AirGradient ", "Piezo ", "Absolute ")   # (a label's side or device, left out where units are listed)
KEEP_CASE = ("PM", "CO", "UV", "VOC", "NOx")                              # (labels that keep their capitals)


def _plain(label: str) -> str:
    for prefix in PLAIN:
        label = label.removeprefix(prefix)
    return label if label.startswith(KEEP_CASE) else label[:1].lower() + label[1:]


def units(catalogue: Catalogue) -> str:
    """"°C: temperature, dew point …; %: humidity; …; no unit: UV index …": each unit with what comes in it, in the catalogue's
    order, sides and devices aside."""
    by_unit: dict[str, list[str]] = {}
    for r in catalogue:
        what = by_unit.setdefault(r.unit, [])
        if (plain := _plain(r.label)) not in what:
            what.append(plain)
    listed = [f"{unit}: {', '.join(what)}" for unit, what in by_unit.items() if unit]
    if "" in by_unit:
        listed.append(f"no unit (an index): {', '.join(by_unit[''])}")
    return "; ".join(listed)


def names(catalogue: Catalogue) -> str:
    """"outdoor and indoor: temperature, humidity …; pressure: relative, absolute; …": each sensor with its readings' names, in the
    catalogue's order, sensors with the same readings together (a model must not make up a name: rain.rain_rate)."""
    by_sensor: dict[str, list[str]] = {}
    for r in catalogue:
        sensor, _, reading = r.name.partition(".")
        by_sensor.setdefault(sensor, []).append(reading)
    alike: dict[tuple[str, ...], list[str]] = {}
    for sensor, readings in by_sensor.items():
        alike.setdefault(tuple(readings), []).append(sensor)
    return "; ".join(f"{' and '.join(sensors)}: {', '.join(readings)}" for readings, sensors in alike.items())


def instructions(ctx=None) -> str:
    """The instructions for the service over this Context (lib/service/context.py): its sources, the readings its catalogue holds and
    its zone. With no ctx: every source, every reading the service knows, no zone named."""
    catalogue = ctx.readings() if ctx is not None else CATALOGUE
    station = ctx.eco is not None if ctx is not None else True
    air = ctx.air is not None if ctx is not None else True
    forecast = ctx.forecast is not None if ctx is not None else True
    pollen = ctx.pollen is not None if ctx is not None else True
    station = station and any(r.device == STATION for r in catalogue)
    air = air and any(r.device == AIR_DEVICE for r in catalogue)

    devices = []
    if station:
        devices.append("the weather station (Ecowitt; \"ecowitt\", \"the weather station\", \"the station\"), outdoors, with an "
                       "indoor sensor")
    if air:
        devices.append("the air sensor (AirGradient, for air quality; \"ag\", \"airgradient\", \"air gradient\")")
    extra = [*(["a forecast (Open-Meteo)"] if forecast else []),
             *(["grass pollen and thunderstorm asthma (Melbourne, Oct–Dec)"] if pollen else [])]
    lines = [f"The devices: {'; '.join(devices) or 'none'}." + (f" Also: {'; '.join(extra)}." if extra else "")]
    if station:
        sides = ("Indoor and Outdoor: a bare reading is the station's; temperature and humidity are both "
                 "(outdoor.temperature, indoor.temperature), the rest outdoor.")
        if air:
            sides += (" The air sensor is outdoors: its own temperature and humidity are only air.temperature and air.humidity; "
                      "no indoor air quality.")
        lines.append(sides)
    held = Catalogue(r for r in catalogue if (r.device == STATION and station) or (r.device == AIR_DEVICE and air))
    if held.names:
        lines.append(f"Names (sensor.reading): {names(held)}.")
        lines.append(f"Units: {units(held)}. Rain: mm that fell.")
    zone = getattr(ctx.zone, "key", None) if ctx is not None and (ctx.tz or ctx.eco or ctx.air) else None
    when = f"The station's clock is {zone} time; " if zone else "Times are the station's local time; "
    lines.append(when + "pass a period in the person's own words (`period`: \"past week\", \"since 6am\"); the tools "
                        "read it.")
    lines.append("Fetch for every question; never reuse earlier figures: call again at once, unasked, on a follow-up or a "
                 "challenge (\"that's wrong\").")
    return "\n".join(lines)


INSTRUCTIONS = instructions()   # every device and every reading the service knows (the evals; a server that hasn't read its file)

__all__ = ["INSTRUCTIONS", "instructions", "names", "units"]
