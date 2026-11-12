"""What both sides of the irrigation controller know: irrigation-mcp (lib/irrigation/tuya.py, server.py, check.py) and the bot's menu
(lib/irrigation_menu.py), which is its client. Nothing here loads the Tuya client (httpx) or anything else: the bot imports it alone.

  - `DELAYS`: the device's weather_delay range ("cancel" is none); `MAX_MINUTES`: the longest run;
  - `is_paused(status)`: a weather delay pauses the timer;
  - `IrrigationError`: the controller (or irrigation-mcp) could not be read or told something; its message is safe to show."""

DELAYS = ("24h", "48h", "72h", "cancel")    # the device's weather_delay range
MAX_MINUTES = 60                            # the longest run asked for


def is_paused(status: dict) -> bool:
    """Is the timer paused: a weather delay other than "cancel" (which is none)?"""
    return status.get("weather_delay") not in (None, "cancel")


class IrrigationError(Exception):
    """The controller could not be read or told something. The message is safe to log (no secrets, no raw response)."""


__all__ = ["DELAYS", "MAX_MINUTES", "IrrigationError", "is_paused"]
