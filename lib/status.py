"""Status (ecobot://status, docs/ecobot-mcp.md step 6): its report in words (report: the readings now, today's forecast and the
pollen, written here from the tools' own answers, as the report always was: no model), and its picture, the weather station's temperature, humidity and rain gauges in a
row (gauge_sheet.STATUS), drawn as a PNG by lib/gauges.py, with their readings as JSON beside it: each gauge's reading now and its
highs and lows (rain: its wettest day and total) of today, the month and the year, which the picture leaves out (the web page's
dashboard, lib/web_dashboard.py, has them as tables). Laid out from the service's `current` with context, read as a call to it is (the
live reading fetched when none is held fresh: unlike the other resources, this one is read when someone asks for Status). A chat client sends the picture with the report as its caption (the Telegram bot's Status button and /status).

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


async def _answer(service, name: str, args: dict) -> dict | None:
    """A tool's answer, None when it has none (no such tool, a refusal: pollen out of season, an error)."""
    tool = service.get(name)
    if tool is None:
        return None
    try:
        out = json.loads(await tool.call(args, dict(META)))
    except Exception as e:
        log.warning("Status: %s failed (%s: %s); the report goes without it", name, type(e).__name__, e)
        return None
    return out if isinstance(out, dict) and "error" not in out else None


async def report(service) -> str:
    """Status's words, written here from the service's own answers, as the report always was (no model): each device's section now
    (current's headings and lines: the weather station's, the air sensor's), then today's forecast and the pollen (their `text`), those
    there are, a blank line between."""
    now, forecast, pollen = await asyncio.gather(_answer(service, "current", {}), _answer(service, "forecast", {"period": "today"}),
                                                 _answer(service, "pollen", {}))
    sections = []
    for device, lines in ((now or {}).get("lines") or {}).items():
        heading = ((now or {}).get("headings") or {}).get(device)
        sections.append("\n".join([*([heading] if heading else []), *lines]))
    sections += [out["text"] for out in (forecast, pollen) if out and out.get("text")]
    return "\n\n".join(sections)


async def status(service) -> tuple[dict, bytes | None]:
    """(the report and the gauges' readings as JSON, the PNG) of Status over the service's tools (`get(name)`, as lib.service.Service
    gives them); the PNG None without the station's gauges (no station, or it isn't answering) or without Pillow."""
    words = asyncio.ensure_future(report(service))
    data, png = await _picture(service)
    return {"report": await words, **data}, png


async def _picture(service) -> tuple[dict, bytes | None]:
    tool = service.get("current")
    if tool is None:
        return {"gauges": [], "unavailable": NO_STATION}, None
    try:
        result = json.loads(await tool.call(gauge_sheet.call(STATUS), dict(META)))
    except Exception as e:   # (the report still goes, without its picture)
        log.error("Status: the gauges failed (%s: %s); the report goes without them", type(e).__name__, e)
        return {"gauges": [], "unavailable": NOT_ANSWERING}, None
    sheet = gauge_sheet.sheet(result, STATUS) if isinstance(result, dict) else None
    if sheet is None:
        why = (result.get("error") or (result.get("unavailable") or {}).get("station")) if isinstance(result, dict) else None
        return {"gauges": [], "unavailable": why or NOT_ANSWERING}, None
    data = {"gauges": [gauge(t) for t in sheet.tiles], "lines": [line(t) for t in sheet.tiles]}
    gauges = _renderer()
    if gauges is None:
        return {**data, "image_note": UNAVAILABLE_NOTE}, None
    return data, await asyncio.to_thread(gauges.render, sheet)


__all__ = ["STATUS", "UNAVAILABLE_NOTE", "gauge", "line", "report", "status"]
