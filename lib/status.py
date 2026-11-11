"""The Status picture (ecobot://status, docs/ecobot-mcp.md step 6): the weather station's temperature, humidity and rain gauges in a
row (gauge_sheet.STATUS), drawn as a PNG by lib/gauges.py, with their readings as JSON beside it: each gauge's reading now and its
highs and lows (rain: its wettest day and total) of today, the month and the year, which the picture leaves out (the web page's
dashboard, lib/web_dashboard.py, has them as tables). Laid out from the service's `current` with context, read as a call to it is (the
live reading fetched when none is held fresh: unlike the other resources, this one is read when someone asks for Status). A chat client sends the picture with its Status (the Telegram bot's /status and /report).

Pillow is imported at the first picture (lib/gauges.py): without it the readings come alone, with `image_note` saying so, and one
warning."""

import asyncio
import json
import logging

from . import gauge_sheet

log = logging.getLogger(__name__)

STATUS = gauge_sheet.STATUS
META = {}   # as a call to current: the live reading fetched when none is held fresh (a Status asked for after a restart has one)
NO_STATION = "There is no weather station here."
NOT_ANSWERING = "The weather station isn't answering right now."
UNAVAILABLE_NOTE = "The Status picture can't be drawn on this service (no Pillow): its readings are in `gauges`."
_warned = False


def _figure(value, decimals: int) -> str:
    return "-" if value is None else f"{value:.{decimals}f}"


def gauge(tile) -> dict:
    """A tile as the resource's JSON: its title, reading now and unit, and its rows by name ("Daily": {"Max": 14.0, "Min": 9.5})."""
    return {"title": tile.title, "value": tile.value, "unit": tile.unit,
            "rows": {row: dict(zip(tile.columns, figures)) for row, figures in tile.rows.items()}}


def line(tile) -> str:
    """A tile in words: "Temperature: 12.3 °C (Daily: Max 14.0, Min 9.5; Monthly: ...)"; a reading not there now: "-"."""
    rows = "; ".join(f"{row}: " + ", ".join(f"{column} {_figure(figure, tile.decimals)}" for column, figure in zip(tile.columns, figures))
                     for row, figures in tile.rows.items() if any(f is not None for f in figures))
    return f"{tile.title}: {_figure(tile.value, tile.decimals)} {tile.unit}" + (f" ({rows})" if rows else "")


def _renderer():
    """lib/gauges.py, imported now if it hasn't been; None without Pillow (said once)."""
    global _warned
    try:
        from . import gauges
    except ImportError as e:
        if not _warned:
            _warned = True
            log.warning("The Status picture was asked for but can't be drawn here (%s): install requirements-charts.txt (Pillow); "
                        "its readings are sent alone", e)
        return None
    return gauges


async def status(service) -> tuple[dict, bytes | None]:
    """(the readings as JSON, the PNG) of the Status picture over the service's tools (`get(name)`, as lib.service.Service gives them);
    the PNG None without the station's gauges (no station, or it isn't answering) or without Pillow."""
    tool = service.get("current")
    if tool is None:
        return {"gauges": [], "unavailable": NO_STATION}, None
    result = json.loads(await tool.call(gauge_sheet.call(STATUS), dict(META)))
    sheet = gauge_sheet.sheet(result, STATUS) if isinstance(result, dict) else None
    if sheet is None:
        why = (result.get("error") or (result.get("unavailable") or {}).get("station")) if isinstance(result, dict) else None
        return {"gauges": [], "unavailable": why or NOT_ANSWERING}, None
    data = {"gauges": [gauge(t) for t in sheet.tiles], "lines": [line(t) for t in sheet.tiles]}
    gauges = _renderer()
    if gauges is None:
        return {**data, "image_note": UNAVAILABLE_NOTE}, None
    return data, await asyncio.to_thread(gauges.render, sheet)


__all__ = ["STATUS", "UNAVAILABLE_NOTE", "gauge", "line", "status"]
