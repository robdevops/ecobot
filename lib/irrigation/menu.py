"""The irrigation status message and its buttons. Pure: it only builds the text and the keyboard.

Collapsed: [💧 Water] [⏸ Pause timer] / [battery button] [🔄 Refresh]. A press on Water or Pause timer opens its options under the rows (▾ marks the
open one; pressing it again closes them). Water lists 5, 10, 20 and 30 minutes, then Off for the valve itself (a run is always timed). Callback data (short, self-contained, so an old message's buttons still work after
a restart):
  ir:open:<water|delay> | ir:close | ir:refresh (read the state again, change nothing)
  ir:water:<minutes> | ir:delay:<24h|48h|72h|cancel> (cancel = unpause) | ir:sw:off | ir:batt:<on|off> (on = subscribe)
"""

import math
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

TITLE = "🌱 Irrigation"
WATER_MINUTES = (5, 10, 20, 30)
DELAYS = ("24h", "48h", "72h", "cancel")
SECTIONS = ("water", "delay")
LOW_BATTERY = 10          # below this the battery shows as 🪫
KEYWORDS = re.compile(r"\b(irrigation|irrigate|sprinklers?|taps?|water(?:ing)?)\b", re.IGNORECASE)
MAX_WORDS = 6             # a longer message that happens to say "water" is a question for the model


def asked(text: str) -> bool:
    """Does this short message ask for the controller (irrigation, water, tap, sprinkler)? The same as pressing the Irrigation button."""
    return 0 < len(text.split()) <= MAX_WORDS and bool(KEYWORDS.search(text))


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def status_text(status: dict, alerts: bool | None = None) -> str:
    """The controller's state, in this order: on or off; the minutes left of a run (only while it is on and counting down); the pause
    (the weather delay, only when one is active); the mode; and the battery with whether this chat gets its alerts (`alerts`; left out
    when not given) in brackets."""
    on = bool(status.get("switch"))
    lines = [TITLE, f"state: {'on ✅' if on else 'off ❌'}"]
    countdown = status.get("countdown")
    if on and isinstance(countdown, (int, float)) and countdown > 0:
        lines.append(f"time until state off: {math.ceil(countdown / 60)} min")
    if status.get("weather_delay") not in (None, "cancel"):
        lines.append(f"pause: {status['weather_delay']}")
    if status.get("work_state"):
        lines.append(f"mode: {status['work_state']}")
    battery = status.get("battery_percentage")
    if battery is not None or alerts is not None:
        level = f"{battery}% {'🔋' if battery >= LOW_BATTERY else '🪫'}" if battery is not None else "unknown"
        lines.append(f"battery: {level}" + (f" (alerts: {'on' if alerts else 'off'})" if alerts is not None else ""))
    return "\n".join(lines)


def keyboard(subscribed: bool, open: str | None = None) -> InlineKeyboardMarkup:
    """The buttons under the status. `subscribed`: this chat gets the battery alerts; the button names what pressing it does (disable, or enable)."""
    def head(name: str, text: str) -> InlineKeyboardButton:
        return _button(("▾ " if open == name else "") + text, "ir:close" if open == name else f"ir:open:{name}")
    rows = [[head("water", "💧 Water"), head("delay", "⏸ Pause timer")],
            [_button("🔕 Disable battery alerts" if subscribed else "🔔 Enable battery alerts", f"ir:batt:{'off' if subscribed else 'on'}"),
             _button("🔄 Refresh", "ir:refresh")]]
    if open == "water":
        rows.append([_button(f"{m} min", f"ir:water:{m}") for m in WATER_MINUTES])
        rows.append([_button("Off ❌", "ir:sw:off")])
    elif open == "delay":
        rows.append([_button("unpause" if d == "cancel" else d, f"ir:delay:{d}") for d in DELAYS])
    return InlineKeyboardMarkup(rows)
