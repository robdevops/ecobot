"""The chart that goes with an alert: a one-day chart of its type's fixed call (CHARTS: summary {readings: [...], period: "1d",
chart: true}), which the service draws. The rule puts the chart's data, with its drawn_by, in the alert's event (chart_data); a client
draws it again by replaying the chart's drawn_by."""

import json
import logging

log = logging.getLogger(__name__)


def day_chart(*readings: str) -> tuple[str, dict]:
    """summary of these readings (the service's words) over the last day, as a chart."""
    return "summary", {"readings": list(readings), "period": "1d", "chart": True}


# The alert types that carry a chart, and the service call that draws it: what an alert event's chart is made by, and what a client
# draws again
CHARTS = {"gusts": day_chart("wind"), "uv": day_chart("uv"), "temps": day_chart("temperature"),
          "particulates": day_chart("pm2.5", "pm10", "pm1")}
CHART_POINTS = "drawn"   # every point, as drawn (lib/service/common.py AS_DRAWN)


def _service_of(source, tz):
    """The service's tools over one source (the station or the air sensor), for a monitor that has only its source."""
    from ..service import Service
    from ..service.context import Context
    name = getattr(source, "name", "")
    return Service.of(Context(eco=source if name == "Ecowitt" else None, air=source if name == "AirGradient" else None, tz=tz))


async def chart_data(source, call: tuple[str, dict], tz, service=None) -> dict | None:
    """The chart of the service `call` (one of CHARTS) as data (3.7's JSON, with the `drawn_by` that draws it again), as the service gives it
    (every point, as drawn): what an alert event carries. None when it cannot be made (the alert then goes out as text). `source` is the
    station or the air sensor; `service` the service's tools, else built over `source`."""
    tool, args = call
    try:
        service = service or _service_of(source, tz)
        out = json.loads(await service[tool].call(args, {"chart_points": CHART_POINTS}))
        if "error" in out:
            raise ValueError(out["error"])
        return out["charts"][0]
    except Exception as e:
        log.warning("The chart for the alert (%s %s) couldn't be made: %s: %s", tool, args, type(e).__name__, e)
        return None
