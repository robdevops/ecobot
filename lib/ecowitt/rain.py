"""Rain from the station's recent 5-minute readings, for the rain alerts: when it rained, how much fell, and how long it took."""

type Rows = list[tuple[int, dict]]  # [(epoch, {"group.field": value})], oldest first


def wet_flags(rows: Rows) -> list[tuple[int, bool, float]]:
    """[(ts, rained in this reading, rain rate)]: rate above zero, or the daily total rising."""
    out, prev_daily = [], None
    for ts, r in rows:
        rate = r.get("rainfall.rain_rate", 0.0)
        daily = r.get("rainfall.daily")
        rising = daily is not None and prev_daily is not None and daily > prev_daily  # ignores the midnight reset
        if daily is not None:
            prev_daily = daily
        out.append((ts, rate > 0 or rising, rate))
    return out


def rain_amount(rows: Rows, since: int, last_wet: int) -> float | None:
    """Rain that fell between since and last_wet: the rise in the daily total (allowing for its
    midnight reset). Ecowitt's 'event' counter isn't used, as it spans several showers."""
    total, prev = 0.0, None
    for ts, r in rows:
        daily = r.get("rainfall.daily")
        if daily is None or ts > last_wet:
            continue
        if ts >= since and prev is not None:
            total += daily - prev if daily >= prev else daily  # a drop means the midnight reset
        prev = daily
    return round(total, 1) if prev is not None else None


def duration(seconds: float) -> str:
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
