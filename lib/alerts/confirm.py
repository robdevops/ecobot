"""One rule for the alerts that must not fire on a single odd reading (UV, particulates): the condition has to hold for CONFIRM_READINGS
distinct readings in a row, "distinct" meaning by the reading's own timestamp, not by how many times it was looked at. A reading already
counted (the same or an older time) is ignored; a reading that does not meet the condition ends the run; a reading with no value is skipped
(it neither extends nor ends the run). The run lives in the monitor's saved dict, so it carries across checks and restarts.

(Rain and the temperature crossing use the back-off rule in backoff.py instead: a change that must stand for a cooldown.)"""

CONFIRM_READINGS = 2


def new_reading(saved: dict, stamp: float) -> bool:
    """Is `stamp` (the time of a reading) newer than the last one counted? If so it is remembered, and the caller counts the reading."""
    if stamp <= saved.get("stamp", float("-inf")):
        return False
    saved["stamp"] = stamp
    return True


def run(saved: dict, key: str, hit: bool | None) -> int:
    """Add a reading to the run kept in saved[key]: it grows when `hit`, ends (0) when not, and stays when `hit` is None. Returns the run."""
    if hit is not None:
        saved[key] = saved.get(key, 0) + 1 if hit else 0
    return saved.get(key, 0)
