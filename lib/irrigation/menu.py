"""The irrigation status message and its buttons. Pure: it only builds the text and the keyboard.

Collapsed: [💧 Water] [⏸ Delay] / [battery toggle] [⏻ Switch]. A press on Water, Delay or Switch opens its options under the rows
(▾ marks the open one); pressing it again closes them. Callback data (short, self-contained, so an old message's buttons still work after
a restart):
  ir:open:<water|delay|switch> | ir:close
  ir:water:<minutes> | ir:delay:<24h|48h|72h|cancel> | ir:sw:<on|off> | ir:batt:<on|off>
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

TITLE = "🌱 Irrigation"
WATER_MINUTES = (5, 10, 20, 30)
DELAYS = ("24h", "48h", "72h", "cancel")
SECTIONS = ("water", "delay", "switch")
LOW_BATTERY = 10          # below this the battery shows as 🪫


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def status_text(status: dict) -> str:
    """The controller's state: switch, battery, mode, and the weather delay when one is active."""
    lines = [TITLE, f"switch: {'on ✅' if status.get('switch') else 'off ❌'}"]
    battery = status.get("battery_percentage")
    if battery is not None:
        lines.append(f"battery: {battery}% {'🔋' if battery >= LOW_BATTERY else '🪫'}")
    if status.get("work_state"):
        lines.append(f"mode: {status['work_state']}")
    if status.get("weather_delay") not in (None, "cancel"):
        lines.append(f"weather delay: {status['weather_delay']}")
    return "\n".join(lines)


def keyboard(subscribed: bool, open: str | None = None) -> InlineKeyboardMarkup:
    """The buttons under the status. `subscribed`: this chat gets the battery warnings (the toggle then offers to unsubscribe)."""
    def head(name: str, text: str) -> InlineKeyboardButton:
        return _button(("▾ " if open == name else "") + text, "ir:close" if open == name else f"ir:open:{name}")
    rows = [[head("water", "💧 Water"), head("delay", "⏸ Delay")],
            [_button("🔕 Stop battery warnings" if subscribed else "🔔 Battery warnings", f"ir:batt:{'off' if subscribed else 'on'}"),
             head("switch", "⏻ Switch")]]
    if open == "water":
        rows.append([_button(f"{m} min", f"ir:water:{m}") for m in WATER_MINUTES])
    elif open == "delay":
        rows.append([_button(d if d == "cancel" else f"Delay {d}", f"ir:delay:{d}") for d in DELAYS])
    elif open == "switch":
        rows.append([_button("On ✅", "ir:sw:on"), _button("Off ❌", "ir:sw:off")])
    return InlineKeyboardMarkup(rows)
