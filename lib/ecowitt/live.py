"""The station's lowest and highest in each 5-minute stretch, from the live readings the bot takes itself.

Ecowitt's own 5-minute history gives one number per reading and no range (its lows and highs come only with the 30-minute readings and
longer). Home Assistant gets a shaded range on its recent charts by keeping every reading the station reports and, every 5 minutes, boiling
them down to a mean, a lowest and a highest. The bot does the same: each live reading (about one a minute: Ecowitt's real_time, which
the keep-warm already fetches) is counted in the 5-minute stretch around the nearest mark (source.LiveMarks), and once a stretch has
MIN_READINGS or more its lowest and highest are written to that 5-minute reading's band (storage.write.Writer.put_ranges), which the
chart of it is shaded between. Nothing here is ever asked of Ecowitt: it only has the readings the bot saw.
"""

from ..series import derives_range

BUCKET = 300
MIN_READINGS = 2           # a stretch with fewer live readings than this has no range


def readings_in(data: dict) -> list[tuple[str, int | None, float]]:
    """(series "group.field", when it was read (epoch, None when the response does not say), value) for each reading of a real_time
    response that a chart can shade (series.derives_range; not the rain)."""
    found = []
    for group, fields in data.items():
        for field, obj in (fields.items() if isinstance(fields, dict) else ()):
            if group == "rainfall" or not (isinstance(obj, dict) and derives_range(field)):   # (rain is a running count: bars, not a line)
                continue
            try:
                value = float(obj["value"])
            except (KeyError, TypeError, ValueError):
                continue
            try:
                when = int(obj.get("time") or 0) or None
            except (TypeError, ValueError):
                when = None
            found.append((f"{group}.{field}", when, value))
    return found
