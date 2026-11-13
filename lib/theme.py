"""DARKMODE: whether charts are drawn dark now. Held here, apart from the renderer (lib/charts.py, Pillow), so the bot, which draws
nothing, can ask for its own theme (request `_meta` `chart_dark`) and keep its picture cache's theme without importing the renderer;
lib/charts.py reads it when it draws, and ecobot-mcp's images (lib/chart_images.py) when the call doesn't say."""

from datetime import UTC, datetime, tzinfo

# "off" (light), "on" (dark), or (from, until): dark between those times of day (minutes past midnight) in ZONE
MODE: str | tuple[int, int] = "off"
ZONE: tzinfo | None = None


def configure(mode: str | tuple[int, int], tz: tzinfo | None = None):
    """The dark-mode setting, and the time zone a time range is read in."""
    global MODE, ZONE
    MODE, ZONE = mode, tz


def dark_now(at: datetime | None = None) -> bool:
    """Are charts drawn dark now?"""
    if MODE == "on":
        return True
    if not isinstance(MODE, tuple):
        return False
    start, end = MODE
    local = (at or datetime.now(UTC)).astimezone(ZONE)
    now = local.hour * 60 + local.minute
    return start <= now < end if start < end else now >= start or now < end   # a range past midnight wraps


__all__ = ["configure", "dark_now"]
