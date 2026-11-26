"""The alert rules' own state (alert_state.json in DATA_DIR): what each rule last alerted and the runs it is counting, so a restart neither
forgets nor repeats."""

import json
import logging
import os
import time

log = logging.getLogger(__name__)


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

    def __init__(self, path):
        self.path = path
        data = load(path)
        self.monitor: dict = (data or {}).get("monitor", {})
        if data is None:
            self.save()

    def save(self):
        write(self.path, {"monitor": self.monitor})
