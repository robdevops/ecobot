"""The irrigation status message and its buttons. Pure: it only builds the text and the keyboard.

Collapsed: [💧 Water] [⏸ Pause] / [🔔 Alerts] [🔄]. While the valve is on, a [Water off ❌] row follows. While the timer is paused,
Pause is [▶️ Unpause] (one press ends the pause) and Water is hidden. A press on Water, Pause or Alerts opens its options under the
rows (▸ after the text marks a menu, ▾ the open one; pressing it again closes them). Water lists 5, 10, 20 and 30 minutes (a run is always timed). Alerts has
one toggle per alert type, blue while this chat gets it, struck through and uncoloured while it does not; pressing it flips it.
Callback data
(short, self-contained, so an old message's buttons still work after a restart):
  ir:open:<water|delay|alerts> | ir:close | ir:refresh (read the state again, change nothing)
  ir:water:<minutes> | ir:delay:<24h|48h|72h|cancel> (cancel = unpause, the Unpause button) | ir:sw:off
  ir:batt:<on|off> | ir:pause:<on|off>      (the irrigation battery and irrigation pause alerts; on = subscribe)
"""

import math
import re

from telegram import InlineKeyboardMarkup, MessageEntity

from ..buttons import button, opener, toggle_button
from ..entities import footer
from .tuya import DELAYS, is_paused

TITLE = "🌱 Irrigation"
WATER_MINUTES = (5, 10, 20, 30)
SECTIONS = ("water", "delay", "alerts")
ALERTS = {"batt": ("irrigation_battery", "battery"), "pause": ("irrigation_pause", "pause")}   # an Alerts toggle's word -> (its alert type, its name)
LOW_BATTERY = 10          # below this the battery shows as 🪫
PAUSED_ERROR = "error: can not turn on water while paused!"
KEYWORDS = re.compile(r"\b(irrigation|irrigate|sprinklers?|taps?|water(?:ing)?)\b", re.IGNORECASE)
MAX_WORDS = 6             # a longer message that happens to say "water" is a question for the model


def asked(text: str) -> bool:
    """Does this short message ask for the controller (irrigation, water, tap, sprinkler)? The same as pressing the Irrigation button."""
    return 0 < len(text.split()) <= MAX_WORDS and bool(KEYWORDS.search(text))


def paused_in(text: str | None) -> bool:
    """Does a status message (as status_text wrote it) say the timer is paused? For redrawing only the buttons, without asking the controller."""
    line = next((l for l in (text or "").splitlines() if l.startswith("pause:")), "")
    value = line.removeprefix("pause:").strip()
    return bool(value) and not value.startswith("not paused")


def running_in(text: str | None) -> bool:
    """Does a status message say the valve is on? (For redrawing only the buttons, as paused_in.)"""
    return any(line.startswith("state: on") for line in (text or "").splitlines())


def with_error(text: str, error: str = PAUSED_ERROR) -> tuple[str, list[MessageEntity]]:
    """The status with an error under it, after a blank line, in bold."""
    return footer(f"{text}\n", error, MessageEntity.BOLD)


def status_text(status: dict, alerts: bool | None = None, pause_alerts: bool | None = None) -> str:
    """The controller's state, every line always there, in this order:
      mode: auto
      state: on ✅ (10 mins until off)        (the minutes only while it is on and counting down)
      pause: 24h (alerts: on)                 ("not paused" when there is no pause)
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
                      f"pause: {delay if is_paused(status) else 'not paused'}{bracket(pause_alerts)}",
                      f"battery: {level}{bracket(alerts)}"])


def keyboard(battery: bool, pause: bool, open: str | None = None, paused: bool = False, running: bool = False) -> InlineKeyboardMarkup:
    """The buttons under the status. `battery` and `pause`: this chat gets the irrigation battery / pause alerts; each Alerts button
    is a toggle (blue when on, struck through and uncoloured when off). `paused`: the timer is paused, so its
    button unpauses and Water is hidden. `running`: the valve is on, so Water off is always shown. While a menu is open only its own button (the heading) and its list are shown."""
    head = lambda name, text: opener(text, open == name, f"ir:open:{name}", "ir:close")
    delay = button("▶️ Unpause", "ir:delay:cancel") if paused else head("delay", "⏸ Pause")
    if open == "water" and not paused:
        rows = [[head("water", "💧 Water")], [button(f"{m} min", f"ir:water:{m}") for m in WATER_MINUTES]]
    elif open == "delay" and not paused:
        rows = [[delay], [button(d, f"ir:delay:{d}") for d in DELAYS if d != "cancel"]]
    elif open == "alerts":
        rows = [[head("alerts", "🔔 Alerts")]]
        for on, (word, (_, name)) in zip((battery, pause), ALERTS.items()):
            rows.append([toggle_button(f"{name} alerts", on, lambda turn_on, word=word: f"ir:{word}:{'on' if turn_on else 'off'}")])
    else:
        rows = [[delay] if paused else [head("water", "💧 Water"), delay],
                [head("alerts", "🔔 Alerts"), button("🔄", "ir:refresh")]]
    if running:   # the valve is open: off is one press away, whatever else is open
        rows.insert(1 if rows[0][0].text.endswith(("▾",)) else len(rows), [button("Water off ❌", "ir:sw:off")])
    return InlineKeyboardMarkup(rows)
