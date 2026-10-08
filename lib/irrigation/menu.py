"""The irrigation status message and its buttons. Pure: it only builds the text and the keyboard.

Collapsed: [💧 Water] [⏸ Pause schedule] / [battery toggle]. A press on Water or Pause schedule opens its options under the rows (▾ marks the open
one; pressing it again closes them). Water lists 5, 10, 20 and 30 minutes, then On and Off for the valve itself. Callback data (short, self-contained, so an old message's buttons still work after
a restart):
  ir:open:<water|delay> | ir:close
  ir:water:<minutes> | ir:delay:<24h|48h|72h|cancel> | ir:sw:<on|off> | ir:batt:<on|off>
"""

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


def status_text(status: dict) -> str:
    """The controller's state: on or off, battery, mode, and the weather delay when one is active."""
    lines = [TITLE, f"state: {'on ✅' if status.get('switch') else 'off ❌'}"]
    battery = status.get("battery_percentage")
    if battery is not None:
        lines.append(f"battery: {battery}% {'🔋' if battery >= LOW_BATTERY else '🪫'}")
    if status.get("work_state"):
        lines.append(f"mode: {status['work_state']}")
    if status.get("weather_delay") not in (None, "cancel"):
        lines.append(f"weather delay: {status['weather_delay']}")
    return "\n".join(lines)


def keyboard(subscribed: bool, open: str | None = None) -> InlineKeyboardMarkup:
    """The buttons under the status. `subscribed`: this chat gets the battery alerts (the toggle shows that, "on", and turns them off)."""
    def head(name: str, text: str) -> InlineKeyboardButton:
        return _button(("▾ " if open == name else "") + text, "ir:close" if open == name else f"ir:open:{name}")
    rows = [[head("water", "💧 Water"), head("delay", "⏸ Pause schedule")],
            [_button("🔔 Battery alerts on" if subscribed else "🔕 Battery alerts off", f"ir:batt:{'off' if subscribed else 'on'}")]]
    if open == "water":
        rows.append([_button(f"{m} min", f"ir:water:{m}") for m in WATER_MINUTES])
        rows.append([_button("On ✅", "ir:sw:on"), _button("Off ❌", "ir:sw:off")])
    elif open == "delay":
        rows.append([_button(d, f"ir:delay:{d}") for d in DELAYS])
    return InlineKeyboardMarkup(rows)
