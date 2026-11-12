"""irrigation-mcp's state: DATA_DIR/irrigation_state.json. It holds what the watchdog and the daily check keep, under the names they
kept it under in the bot's bot_state.json ("monitor"), so the two read it the same way from either:

    {"irrigation_watchdog": {"off_at": <epoch>}, "irrigation_monitor": {"checked": <date>, "paused": <date>, "battery_warned": <date>}}

At its first start (no irrigation_state.json yet) the entries the bot kept in bot_state.json, when it ran the controller itself, are
copied over, once, and that is logged: a run the bot armed is still closed, and a day is neither checked twice nor skipped."""

import json
import logging
from pathlib import Path

from ..alerts.store import load, write

log = logging.getLogger(__name__)

KEYS = ("irrigation_watchdog", "irrigation_monitor")   # (the watchdog's, the daily check's)


class IrrigationState:
    """`monitor`: the watchdog's and the check's entries; `save()` writes them (whole, to a new file that then replaces the old)."""

    def __init__(self, path, bot_state_path=None):
        self.path = Path(path)
        data = load(self.path)
        if data is None:
            data = self._from_the_bot(bot_state_path)
        self.monitor: dict = {k: v for k, v in data.items() if k in KEYS and isinstance(v, dict)} if isinstance(data, dict) else {}

    def _from_the_bot(self, bot_state_path) -> dict:
        """The bot's entries in bot_state.json, written here at once (so they are copied only this once); {} when it kept none."""
        try:   # (read only: the bot's file is the bot's, left as it is even when it can't be read)
            bot = json.loads(Path(bot_state_path).read_text()) if bot_state_path else {}
        except (OSError, ValueError):
            bot = {}
        kept = bot.get("monitor") if isinstance(bot, dict) and isinstance(bot.get("monitor"), dict) else {}
        copied = {k: dict(kept[k]) for k in KEYS if isinstance(kept.get(k), dict) and kept[k]}
        if copied:
            write(self.path, copied)
            log.info("Irrigation: the watchdog's and the daily check's state copied from %s to %s (%s), once: from now on it is kept "
                     "here", bot_state_path, self.path, ", ".join(copied))
        return copied

    def save(self):
        write(self.path, self.monitor)
