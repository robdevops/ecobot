"""Wind direction is circular: 2° and 349° are 13° apart, and a low/high or an average of degrees
means nothing (the mean of 359° and 1° is north, not south). So it is summarised by how often the
wind blew from each of the 16 compass points, plus a vector mean and how steady the wind was."""

import math
from collections import Counter
from datetime import datetime, time, tzinfo

from ..timeutil import local_date, local_epoch, to_local

POINTS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
TOP = 3  # compass points listed, most common first
MIN_SHARE = 0.05  # and only those that blew at least this often


def point(degrees: float) -> str:
    return POINTS[int((degrees % 360 + 11.25) // 22.5) % 16]


# The heatmap's columns: the smallest time step that keeps a period to about this many columns
BIN_STEPS = ((3600, "hour"), (6 * 3600, "6 hours"), (86400, "day"), (7 * 86400, "week"))
MAX_COLUMNS = 130
SPEED_STEPS = (10, 20)  # km/h: the rose's light, middle and strong wind (under 10, 10 to 20, 20 and over)


def _share(n: int, total: int) -> str:
    return f"{100 * n / total:.0f}%"


def vector_mean(degrees: list[float]) -> tuple[float, float]:
    """(mean direction in degrees, steadiness 0-1): 1 is the same direction throughout, near 0 is all over."""
    x = sum(math.cos(math.radians(d)) for d in degrees) / len(degrees)
    y = sum(math.sin(math.radians(d)) for d in degrees) / len(degrees)
    return math.degrees(math.atan2(y, x)) % 360, math.hypot(x, y)


def steadiness(r: float) -> str:
    return "steady" if r >= 0.6 else "fairly steady" if r >= 0.35 else "variable"


def summarise(readings: list[tuple[int, float, bool]], tz: tzinfo, by_day: bool, calm: int = 0) -> dict:
    """readings: (epoch, degrees, exact) where exact means a real reading, not an average of a bucket.
    calm: readings already left out because there was no wind. Returns the result for the model,
    e.g. {"most_common": "NE (34%)", "then": ["E (20%)"], ...}."""
    if not readings:
        return {}
    degrees = [d for _, d, _ in readings]
    counts = Counter(point(d) for d in degrees)
    ranked = [(p, n) for p, n in counts.most_common() if n / len(degrees) >= MIN_SHARE][:TOP]
    mean, r = vector_mean(degrees)
    out: dict = {"unit": "degrees, the direction the wind comes from",
                 "most_common": f"{ranked[0][0]} ({_share(ranked[0][1], len(degrees))})",
                 "then": [f"{p} ({_share(n, len(degrees))})" for p, n in ranked[1:]],
                 "average_direction": f"{point(mean)} ({mean:.0f}°)", "steadiness": steadiness(r),
                 "readings": len(degrees)}
    if not out["then"]:
        del out["then"]
    if calm:
        out["calm"] = f"{_share(calm, calm + len(degrees))} of readings had no wind and are left out (no direction to count)"
    inexact = sum(1 for _, _, exact in readings if not exact)
    if inexact:
        out["note"] = (f"{_share(inexact, len(readings))} of this is from averaged data, where directions near "
                       "north can be wrong; treat it as approximate")
    if by_day:
        days: dict = {}
        for ts, d, _ in readings:
            days.setdefault(local_date(ts, tz), []).append(d)
        out["daily"] = {day.strftime("%a %d %b"): f"{Counter(point(d) for d in ds).most_common(1)[0][0]} "
                                                    f"({_share(Counter(point(d) for d in ds).most_common(1)[0][1], len(ds))})"
                        for day, ds in sorted(days.items())}
    return out


def sector(degrees: float) -> int:
    """0-15, N is 0 and covers 348.75 to 11.25 degrees: 359 and 1 are in the same one."""
    return int((degrees % 360 + 11.25) // 22.5) % 16


def grid(readings: list[tuple[int, float, bool]], tz: tzinfo, first: datetime, last: datetime) -> dict:
    """Counts per compass point per time step, for the heatmap: {"step": seconds, "unit": "day", "start": epoch of
    the first column, "columns": [[16 counts], ...]}. The step follows the length of the period; steps of 6 hours
    or more start at local midnight, hours at the hour."""
    span = (last - first).total_seconds()
    step, unit = next(((s, u) for s, u in BIN_STEPS if span / s <= MAX_COLUMNS), BIN_STEPS[-1])
    origin = first.replace(minute=0, second=0, microsecond=0) if step == 3600 else datetime.combine(first.date(), time())
    columns = [[0] * 16 for _ in range(max(1, int(-(-(last - origin).total_seconds() // step))))]
    for ts, degrees, _ in readings:
        k = int((to_local(ts, tz).replace(tzinfo=None) - origin).total_seconds() // step)
        if 0 <= k < len(columns):
            columns[k][sector(degrees)] += 1
    return {"step": step, "unit": unit, "start": local_epoch(origin, tz), "columns": columns}


def rose(readings: list[tuple[int, float, bool, float | None]]) -> list[list[int]]:
    """Counts for the wind rose: for each of the 16 compass points [light, middle, strong] by wind speed
    (readings are (epoch, degrees, exact, km/h or None); a reading with no speed counts as light)."""
    out = [[0, 0, 0] for _ in range(16)]
    for _, degrees, _, speed in readings:
        step = 0 if speed is None else sum(speed >= s for s in SPEED_STEPS)
        out[sector(degrees)][step] += 1
    return out
