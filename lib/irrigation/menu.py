"""The irrigation status message and its buttons. Pure: it only builds the text and the keyboard.

Collapsed: [💧 Water] [⏸ Pause timer] / [🔔 Alerts] [🔄 Refresh]. A press on Water, Pause timer or Alerts opens its options under the rows
(▾ marks the open one; pressing it again closes them). Water lists 5, 10, 20 and 30 minutes, then Off for the valve itself (a run is
always timed). Alerts has one button per alert type, each naming what pressing it does for this chat (enable or disable). Callback data
(short, self-contained, so an old message's buttons still work after a restart):
  ir:open:<water|delay|alerts> | ir:close | ir:refresh (read the state again, change nothing)
  ir:water:<minutes> | ir:delay:<24h|48h|72h|cancel> (cancel = unpause) | ir:sw:off
  ir:batt:<on|off> | ir:pause:<on|off>      (the irrigation battery and irrigation pause alerts; on = subscribe)
"""

import math
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

TITLE = "🌱 Irrigation"
WATER_MINUTES = (5, 10, 20, 30)
DELAYS = ("24h", "48h", "72h", "cancel")
SECTIONS = ("water", "delay", "alerts")
LOW_BATTERY = 10          # below this the battery shows as 🪫
PAUSED_ERROR = "error: can not turn on water while paused!"
KEYWORDS = re.compile(r"\b(irrigation|irrigate|sprinklers?|taps?|water(?:ing)?)\b", re.IGNORECASE)
MAX_WORDS = 6             # a longer message that happens to say "water" is a question for the model


def asked(text: str) -> bool:
    """Does this short message ask for the controller (irrigation, water, tap, sprinkler)? The same as pressing the Irrigation button."""
    return 0 < len(text.split()) <= MAX_WORDS and bool(KEYWORDS.search(text))


def with_error(text: str, error: str = PAUSED_ERROR) -> tuple[str, list[MessageEntity]]:
    """The status with an error under it as a bold footer. Plain text plus entities (no markup to escape); Telegram counts
    positions in UTF-16 code units, where an emoji is two."""
    units = lambda s: len(s.encode("utf-16-le")) // 2
    head = f"{text}\n\n"
    return head + error, [MessageEntity(MessageEntity.BOLD, units(head), units(error))]


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def status_text(status: dict, alerts: bool | None = None, pause_alerts: bool | None = None) -> str:
    """The controller's state, every line always there, in this order:
      mode: auto
      state: on ✅ (10 mins until off)        (the minutes only while it is on and counting down)
      pause time: 24h (alerts: on)            ("inactive" when there is no pause)
      battery: 80% 🔋 (alerts: on)
    `alerts` and `pause_alerts`: whether this chat gets the battery / pause alerts (the brackets are left out when not given)."""
    on = bool(status.get("switch"))
    countdown = status.get("countdown")
    left = ""
    if on and isinstance(countdown, (int, float)) and countdown > 0:
        minutes = math.ceil(countdown / 60)
        left = f" ({minutes} {'min' if minutes == 1 else 'mins'} until off)"
    delay = status.get("weather_delay")
    battery = status.get("battery_percentage")
    level = f"{battery}% {'🔋' if battery >= LOW_BATTERY else '🪫'}" if battery is not None else "unknown"
    bracket = lambda on_: "" if on_ is None else f" (alerts: {'on' if on_ else 'off'})"
    return "\n".join([TITLE,
                      f"mode: {status.get('work_state') or 'unknown'}",
                      f"state: {'on ✅' if on else 'off ❌'}{left}",
                      f"pause time: {'inactive' if delay in (None, 'cancel') else delay}{bracket(pause_alerts)}",
                      f"battery: {level}{bracket(alerts)}"])


def keyboard(battery: bool, pause: bool, open: str | None = None) -> InlineKeyboardMarkup:
    """The buttons under the status. `battery` and `pause`: this chat gets the irrigation battery / pause alerts; each Alerts button
    names what pressing it does (disable when it is on, enable when it is off)."""
    def head(name: str, text: str) -> InlineKeyboardButton:
        return _button(("▾ " if open == name else "") + text, "ir:close" if open == name else f"ir:open:{name}")
    rows = [[head("water", "💧 Water"), head("delay", "⏸ Pause timer")],
            [head("alerts", "🔔 Alerts"), _button("🔄 Refresh", "ir:refresh")]]
    if open == "water":
        rows.append([_button(f"{m} min", f"ir:water:{m}") for m in WATER_MINUTES])
        rows.append([_button("Off ❌", "ir:sw:off")])
    elif open == "delay":
        rows.append([_button("unpause" if d == "cancel" else d, f"ir:delay:{d}") for d in DELAYS])
    elif open == "alerts":
        for on, kind, name in ((battery, "batt", "battery"), (pause, "pause", "pause")):
            rows.append([_button(f"🔕 Disable {name} alerts" if on else f"🔔 Enable {name} alerts", f"ir:{kind}:{'off' if on else 'on'}")])
    return InlineKeyboardMarkup(rows)
