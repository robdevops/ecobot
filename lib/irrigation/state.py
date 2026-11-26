"""irrigation-mcp's state: DATA_DIR/irrigation_state.json. It holds what the watchdog and the daily check keep:

    {"irrigation_watchdog": {"off_at": <epoch>}, "irrigation_monitor": {"checked": <date>, "paused": <date>, "battery_warned": <date>}}
"""

from pathlib import Path

from ..alerts.store import load, write

KEYS = ("irrigation_watchdog", "irrigation_monitor")   # (the watchdog's, the daily check's)


class IrrigationState:
    """`monitor`: the watchdog's and the check's entries; `save()` writes them (whole, to a new file that then replaces the old)."""

    def __init__(self, path):
        self.path = Path(path)
        data = load(self.path)
        self.monitor: dict = {k: v for k, v in data.items() if k in KEYS and isinstance(v, dict)} if isinstance(data, dict) else {}

    def save(self):
        write(self.path, self.monitor)
