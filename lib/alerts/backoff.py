"""One back-off rule for the alerts that announce a change between two states (rain: raining / dry; temperatures: outdoor warmer /
cooler than indoor).

Each such alert keeps two things in its saved dict: `alerted_state` (what the chat was last told) and `alerted_time` (when). At
every check the caller works out the state *now* from the latest readings, and an alert is due only if it differs from
`alerted_state` and at least the cooldown has passed since `alerted_time`. A change that comes inside the cooldown is not lost: it
is announced when the cooldown ends, if the state is still different. If it flipped back and forth meanwhile and is back on
`alerted_state`, there is nothing to say."""


def due(saved: dict, state: str, now: int, cooldown: int) -> bool:
    """Is `state` (now) worth an alert: different from the last one alerted, and the cooldown since it is over?"""
    return state != saved.get("alerted_state") and (saved.get("alerted_time") is None or now - saved["alerted_time"] >= cooldown)


def remember(saved: dict, state: str, now: int, alerted: bool = True):
    """Record `state` as the one the chat knows. `alerted=False` for a quiet update (no alert went out): the time of the last real
    alert stays."""
    saved["alerted_state"] = state
    if alerted:
        saved["alerted_time"] = now
