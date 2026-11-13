"""The alert settings as buttons. Pure: it only builds the keyboards and the title from what a chat has muted.

/alerts has two buttons, 🔔 Alerts and (private chats) 🔊 Sound. Each opens a list of alert types as toggles (Sound lists only the types
that are on), the button that opened it kept above the list (▾; pressing it closes the list) and the other hidden meanwhile.
Under an alert there is one 🔔 that opens that type's two toggles, Alert and (while it is on, private chats) Sound, and beside it, under an
alert that carries a chart, 🔄, which draws the chart again with the latest readings.

Callback data is self-contained and short, so an old message's buttons still work after a restart. It is built here and read by
bot.on_alert_button:
  al:open:<alerts|sound>        al:close                       the /alerts message
  al:on|off:<kind>:alerts       al:snd:<kind>:on|off           a type's alerts, or its sound, on or off
  al:open:bell:<kind>           al:close:<kind>                the bell under an alert of that type
  al:on|off:<kind>:bell:<kind>  al:snd:<kind>:on|off:<kind>    its two toggles
  al:refresh:<kind>[:<id>]                                     the chart of an alert of that type, drawn again: the alert <id>'s
                                                               (in the alert log), its chart's drawn_by replayed; without one (an
                                                               alert sent before the log), the type's chart
"""

from itertools import batched

from telegram import InlineKeyboardMarkup

from ..buttons import button, opener, toggle_button
from .kinds import LABELS, TYPES, available_kinds, kinds_of  # noqa: F401 (named here too: the menus are built from them)

ALL = "all"
TITLE = "🔔 Alerts in this chat"
PER_ROW = 2
ALERTS, SOUND, BELL = "alerts", "sound", "bell"   # the menus that can be open
MENUS = {ALERTS: "🔔 Alerts", SOUND: "🔊 Sound"}   # the /alerts buttons


def _verb(on: bool) -> str:
    return "on" if on else "off"


def alert_keyboard(kind: str, muted: set[str], loud: set[str] | None, open: str | None = None, refresh: bool = False,
                   event: int | None = None) -> InlineKeyboardMarkup:
    """The buttons under an alert of type `kind`: one bell (🔔 ▸, ▾ once `open` is BELL) that opens up to two toggles, Alert (this type's
    alerts in this chat) and, only while that is on and in a private chat (`loud` is not None), Sound (whether they make a notification
    sound). refresh (the alert carries a chart): 🔄 beside the bell, which draws the chart again (event: the alert's id in the log, whose
    chart's drawn_by it replays)."""
    is_open = open == BELL
    rows = [[opener("🔔", is_open, f"al:open:{BELL}:{kind}", f"al:close:{kind}"), *([button("🔄", f"al:refresh:{kind}" + (f":{event}" if event is not None else ""))] if refresh else [])]]
    if is_open:
        on = kind not in muted
        rows.append([toggle_button("Alert", on, lambda turn_on: f"al:{_verb(turn_on)}:{kind}:{BELL}:{kind}")])
        if on and loud is not None:
            rows.append([toggle_button("Sound", kind in loud, lambda turn_on: f"al:snd:{kind}:{_verb(turn_on)}:{kind}")])
    return InlineKeyboardMarkup(rows)


def _toggles(kinds: list[str], is_on, data) -> list[list]:
    """A toggle for each of these alert types, PER_ROW to a row. is_on(kind): its state; data(kind, turn_on): the callback data that sets it."""
    return list(map(list, batched((toggle_button(LABELS[k], is_on(k), lambda turn_on, k=k: data(k, turn_on)) for k in kinds), PER_ROW)))


def keyboard(muted: set[str], available: list[str], open: str | None = None, loud: set[str] | None = None) -> InlineKeyboardMarkup:
    """The /alerts menu: 🔔 Alerts and, when some type is on in a private chat, 🔊 Sound. `open` (ALERTS or SOUND): that menu's list, with
    its own button above it and the other one hidden. `loud`: the types whose alerts make a notification sound; None (a group) has no Sound."""
    sound_types = [k for k in available if k not in muted] if loud is not None else []   # (a type that is off has no sound to set)
    head = lambda section: opener(MENUS[section], open == section, f"al:open:{section}", "al:close")
    if open == ALERTS:
        return InlineKeyboardMarkup([[head(ALERTS)], *_toggles(available, lambda k: k not in muted, lambda k, turn_on: f"al:{_verb(turn_on)}:{k}:{ALERTS}")])
    if open == SOUND and sound_types:
        return InlineKeyboardMarkup([[head(SOUND)], *_toggles(sound_types, lambda k: k in loud, lambda k, turn_on: f"al:snd:{k}:{_verb(turn_on)}")])
    return InlineKeyboardMarkup([[head(ALERTS), *([head(SOUND)] if sound_types else [])]])


def title(muted: set[str], available: list[str]) -> str:
    """The /alerts message: what is on and what is off."""
    on = [LABELS[k] for k in available if k not in muted]
    off = [LABELS[k] for k in available if k in muted]
    return f"{TITLE}\n✅ On: {', '.join(on) or 'nothing'}\n🔕 Off: {', '.join(off) or 'nothing'}"
