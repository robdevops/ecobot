"""The alert settings as buttons. Pure: it only builds the keyboards and the title from what a chat has muted.

/alerts has two buttons, 🔔 Alerts and (private chats) 🔊 Sound. Each opens a list of alert types as toggles (Sound lists only the types
that are on), the button that opened it kept above the list (▾; pressing it closes the list) and the other hidden meanwhile.
Under an alert there is one 🔔 that opens that type's two toggles, Alert and (while it is on, private chats) Sound.

Callback data is self-contained and short, so an old message's buttons still work after a restart. It is built and read here only (Press):
  al:open:<alerts|sound>        al:close                       the /alerts message
  al:on|off:<kind>:alerts       al:snd:<kind>:on|off           a type's alerts, or its sound, on or off
  al:open:bell:<kind>           al:close:<kind>                the bell under an alert of that type
  al:on|off:<kind>:bell:<kind>  al:snd:<kind>:on|off:<kind>    its two toggles
"""

from dataclasses import dataclass
from itertools import batched

from telegram import InlineKeyboardMarkup

from ..buttons import opener, toggle_button

# kind -> the name on the button, in the order listed
LABELS = {"rain": "rain", "gusts": "gusts", "uv": "UV", "temps": "temperature crossing",
          "particulates": "particulates", "pollen_and_asthma": "pollen & asthma", "forecast": "forecast changes",
          "irrigation_battery": "irrigation battery", "irrigation_pause": "irrigation pause"}
ALL = "all"
TITLE = "🔔 Alerts in this chat"
PER_ROW = 2
ALERTS, SOUND, BELL = "alerts", "sound", "bell"   # the menus that can be open
MENUS = {ALERTS: "🔔 Alerts", SOUND: "🔊 Sound"}   # the /alerts buttons


def available_kinds(sources: set[str]) -> list[str]:
    """The alert types the bot can send, from the names of its sources."""
    wanted = (["rain", "gusts", "uv", "temps"] if "Ecowitt" in sources else []) + (
        ["particulates"] if "AirGradient" in sources else []) + (["pollen_and_asthma"] if "Pollen" in sources else []) + (
        ["forecast"] if "Forecast" in sources else []) + (["irrigation_battery", "irrigation_pause"] if "Irrigation" in sources else [])
    return wanted


def kinds_for(source_names, irrigation: bool) -> list[str]:
    """The alert types the bot sends, from the names of its sources and whether an irrigation controller is configured."""
    return available_kinds(set(source_names) | ({"Irrigation"} if irrigation else set()))


@dataclass(frozen=True)
class Press:
    """What a button of these menus does, and its callback data both ways (data() builds it, parse() reads it back)."""
    action: str                  # open or close a menu, flip a type's alert (alert) or its sound (sound)
    kind: str | None = None      # the alert type a toggle flips
    section: str | None = None   # the menu to show: ALERTS, SOUND or BELL
    parent: str | None = None    # the alert type the buttons sit under (their menu is the bell), if any
    on: bool = False             # what a toggle sets

    def data(self) -> str:
        tail = f":{self.parent}" if self.parent else ""
        verb = "on" if self.on else "off"
        match self.action:
            case "open":
                return f"al:open:{self.section}{tail}"
            case "close":
                return f"al:close{tail}"
            case "alert":
                return f"al:{verb}:{self.kind}:{self.section}{tail}"
            case _:
                return f"al:snd:{self.kind}:{verb}{tail}"


def parse(data: str | None) -> Press | None:
    """The press that callback `data` is, or None when it is not the data of any button these menus draw."""
    parts = (data or "").split(":")
    if parts[0] != "al" or len(parts) < 2:
        return None
    action, rest = parts[1], parts[2:]
    if action == "close" and len(rest) <= 1:
        return Press("close", parent=rest[0] if rest else None)
    if action == "open" and len(rest) in (1, 2):
        return Press("open", section=rest[0], parent=rest[1] if len(rest) == 2 else None)
    if action in ("on", "off") and len(rest) in (2, 3):
        return Press("alert", rest[0], rest[1], rest[2] if len(rest) == 3 else None, action == "on")
    if action == "snd" and len(rest) in (2, 3) and rest[1] in ("on", "off"):
        parent = rest[2] if len(rest) == 3 else None
        return Press("sound", rest[0], BELL if parent else SOUND, parent, rest[1] == "on")
    return None


def alert_keyboard(kind: str, muted: set[str], loud: set[str] | None, open: str | None = None) -> InlineKeyboardMarkup:
    """The buttons under an alert of type `kind`: one bell (🔔 ▸, ▾ once `open` is BELL) that opens up to two toggles, Alert (this type's
    alerts in this chat) and, only while that is on and in a private chat (`loud` is not None), Sound (whether they make a notification
    sound)."""
    is_open = open == BELL
    rows = [[opener("🔔", is_open, Press("open", section=BELL, parent=kind).data(), Press("close", parent=kind).data())]]
    if is_open:
        on = kind not in muted
        rows.append([toggle_button("Alert", on, lambda turn_on: Press("alert", kind, BELL, kind, turn_on).data())])
        if on and loud is not None:
            rows.append([toggle_button("Sound", kind in loud, lambda turn_on: Press("sound", kind, BELL, kind, turn_on).data())])
    return InlineKeyboardMarkup(rows)


def _toggles(kinds: list[str], is_on, section: str, action: str) -> list[list]:
    """A toggle for each of these alert types, PER_ROW to a row. is_on(kind): its state."""
    return list(map(list, batched((toggle_button(LABELS[k], is_on(k), lambda turn_on, k=k: Press(action, k, section, on=turn_on).data())
                                   for k in kinds), PER_ROW)))


def keyboard(muted: set[str], available: list[str], open: str | None = None, loud: set[str] | None = None) -> InlineKeyboardMarkup:
    """The /alerts menu: 🔔 Alerts and, when some type is on in a private chat, 🔊 Sound. `open` (ALERTS or SOUND): that menu's list, with
    its own button above it and the other one hidden. `loud`: the types whose alerts make a notification sound; None (a group) has no Sound."""
    sound_types = [k for k in available if k not in muted] if loud is not None else []   # (a type that is off has no sound to set)
    head = lambda section: opener(MENUS[section], open == section, Press("open", section=section).data(), Press("close").data())
    if open == ALERTS:
        return InlineKeyboardMarkup([[head(ALERTS)], *_toggles(available, lambda k: k not in muted, ALERTS, "alert")])
    if open == SOUND and sound_types:
        return InlineKeyboardMarkup([[head(SOUND)], *_toggles(sound_types, lambda k: k in loud, SOUND, "sound")])
    return InlineKeyboardMarkup([[head(ALERTS), *([head(SOUND)] if sound_types else [])]])


def title(muted: set[str], available: list[str]) -> str:
    """The /alerts message: what is on and what is off."""
    on = [LABELS[k] for k in available if k not in muted]
    off = [LABELS[k] for k in available if k in muted]
    return f"{TITLE}\n✅ On: {', '.join(on) or 'nothing'}\n🔕 Off: {', '.join(off) or 'nothing'}"
