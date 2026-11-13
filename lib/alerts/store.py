"""The alert rules' own state (alert_state.json in DATA_DIR): what each rule last alerted and the runs it is counting, so a restart neither
forgets nor repeats. The service's, apart from the bot's chats and settings (bot_state.json), which the rules know nothing of.

Before step 4 of docs/ecobot-mcp.md the rules kept it in bot_state.json's "monitor": the first start with no alert_state.json moves it
here (MOVED), so nothing alerted before the upgrade alerts again. The irrigation controller's (irrigation_monitor, irrigation_watchdog)
stay with the bot until it moves to its own server; the forecast each chat was sent (forecast_sent) goes: the forecast alert is
station-wide now."""

import json
import logging
import os
import time

log = logging.getLogger(__name__)

MOVED = ("rain", "night", "cross", "gust", "uv", "particulates", "pollen_and_asthma")   # the rules' keys in the bot's old "monitor"
DROPPED = ("forecast_sent",)


def load(path) -> dict | None:
    """A JSON state file, None when there is none. One that can't be read is moved aside, as the next save would replace it, so what it
    held can still be recovered by hand ({} then)."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        aside = f"{path}.damaged-{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(path, aside)
        log.error("Couldn't read %s (%s): moved it to %s and starting afresh", path, e, aside)
        return {}


def write(path, data: dict):
    """Written in full to a new file that then replaces the old one, on disk first, so a crash leaves one or the other whole."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class AlertStore:
    """The rules' state: `monitor`, a dict each rule keeps its own key in, and save()."""

    def __init__(self, path, moved_from=None):
        """moved_from: the bot's state (lib/alerts/notify.AlertState), whose rules' state is moved here when this file is new."""
        self.path = path
        data = load(path)
        self.monitor: dict = (data or {}).get("monitor", {})
        if data is None:
            if moved_from is not None:
                self._move(moved_from)
            self.save()

    def _move(self, bot_state):
        old = bot_state.monitor
        moved = [key for key in MOVED if key in old]
        for key in moved:
            self.monitor[key] = old.pop(key)
        dropped = [key for key in DROPPED if old.pop(key, None) is not None]
        if moved or dropped:
            log.info("Alerts: the rules' state moved from %s to %s (%s)", bot_state.path, self.path, ", ".join(moved) or "nothing")
            self.save()          # (here first: a crash between the two saves leaves it in both, never in neither)
            bot_state.save()

    def save(self):
        write(self.path, {"monitor": self.monitor})
