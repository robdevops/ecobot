"""The two repetition rules of the alerts: a change between two states must stand for a cooldown (back-off), and a condition must hold
for a run of readings (confirmation); either keeps its memory in the monitor's saved dict, so it carries across checks and restarts.

Back-off, for the alerts that announce a change between two states (rain: raining / dry; temperatures: outdoor warmer / cooler than
indoor). Each such alert keeps `alerted_state` (what the chat was last told) and `alerted_time` (when). At every check the caller works out
the state *now* from the latest readings, and an alert is due only if it differs from `alerted_state` and at least the cooldown has passed
since `alerted_time`. A change that comes inside the cooldown is not lost: it is announced when the cooldown ends, if the state is still
different. If it flipped back and forth meanwhile and is back on `alerted_state`, there is nothing to say.

Confirmation, for the alerts that must not fire on a single odd reading (UV, particulates): the condition has to hold for CONFIRM_READINGS
distinct readings in a row, "distinct" meaning by the reading's own timestamp, not by how many times it was looked at. A reading already
counted (the same or an older time) is ignored; a reading that does not meet the condition ends the run; a reading with no value is skipped
(it neither extends nor ends the run).

Once an alert (gusts, UV) has fired it waits to re-arm: rearm() lets it fire again after its condition has stopped holding for a while."""

CONFIRM_READINGS = 2


def due(saved: dict, state: str, now: int, cooldown: int) -> bool:
    """Is `state` (now) worth an alert: different from the last one alerted, and the cooldown since it is over?"""
    return state != saved.get("alerted_state") and (saved.get("alerted_time") is None or now - saved["alerted_time"] >= cooldown)


def remember(saved: dict, state: str, now: int, alerted: bool = True):
    """Record `state` as the one the chat knows. `alerted=False` for a quiet update (no alert went out): the time of the last real
    alert stays."""
    saved["alerted_state"] = state
    if alerted:
        saved["alerted_time"] = now


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


def rearm(saved: dict, hit: bool, now: float, after: float):
    """The alert in `saved` (saved["active"]: it has fired) can fire again once its condition has not held (`hit`) for `after`
    seconds; while it holds, the calm hasn't begun."""
    if hit:
        saved["calm_since"] = None
    elif saved["active"]:
        saved["calm_since"] = saved["calm_since"] or now
        if now - saved["calm_since"] >= after:
            saved.update(active=False, calm_since=None)
