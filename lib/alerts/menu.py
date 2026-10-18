"""The alert settings as buttons. /alerts has two: 🔔 Alerts and (private chats) 🔊 Sound, each opening the full list of alert types as
toggles (struck through while off) with a Back button, the other hidden meanwhile. Under an alert there is one gear that opens that type's
two toggles, Alert and Sound. Pure: it only builds the keyboard and the title from what a chat has muted.

Callback data (self-contained and short, so an old message's buttons still work after a restart):
  al:open:alerts | al:open:sound | al:close (Back) | al:noop (the heading rows of messages sent by an earlier version)
  al:on:<kind>:alerts | al:off:<kind>:alerts      (alerts of one type on or off in the chat)
  al:snd:<kind>:<on|off>      (notification sound for one type; private chats only)
An alert's buttons end with the alert's own type: al:open:gear:rain, al:close:rain, al:off:rain:gear:rain, al:snd:rain:on:rain. A chat's
/alerts message has no such field.
"""

from itertools import batched

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..buttons import menu_text

# kind -> the name on the button, in the order listed
LABELS = {"rain": "rain", "gusts": "gusts", "uv": "UV", "temps": "temperature crossing",
          "particulates": "particulates", "pollen_and_asthma": "pollen & asthma", "forecast": "forecast changes",
          "irrigation_battery": "irrigation battery", "irrigation_pause": "irrigation pause"}
ALL = "all"
TITLE = "🔔 Alerts in this chat"
PER_ROW = 2
SETTINGS = ("set", "snd_on", "snd_off", "snd_on_o", "snd_off_o")   # (_o: the lists of an earlier version)


def available_kinds(sources: set[str]) -> list[str]:
    """The alert types the bot can send, from the names of its sources."""
    wanted = (["rain", "gusts", "uv", "temps"] if "Ecowitt" in sources else []) + (
        ["particulates"] if "AirGradient" in sources else []) + (["pollen_and_asthma"] if "Pollen" in sources else []) + (
        ["forecast"] if "Forecast" in sources else []) + (["irrigation_battery", "irrigation_pause"] if "Irrigation" in sources else [])
    return wanted


def kinds_for(source_names, irrigation: bool) -> list[str]:
    """The alert types the bot sends, from the names of its sources and whether an irrigation controller is configured."""
    return available_kinds(set(source_names) | ({"Irrigation"} if irrigation else set()))


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def struck(text: str) -> str:
    """The text with a line through it (a button cannot be styled, so each letter gets the combining long stroke)."""
    return "".join(f"{c}\u0336" for c in text)


def _toggle(word: str, on: bool, data: str) -> InlineKeyboardButton:
    """A button that flips a setting: just its word, struck through while the setting is off."""
    return _button(word if on else struck(word), data)


def alert_keyboard(kind: str, muted: set[str], loud: set[str] | None, open: str | None = None) -> InlineKeyboardMarkup:
    """The buttons under an alert of type `kind`: one gear (⚙️ ▸, ▾ once open) that opens up to two toggles, Alert (this type's alerts
    in this chat) and, only while that is on and in a private chat (`loud` is not None), Sound (whether they make a notification sound).
    A toggle is struck through while it is off. Every button carries the type: al:open:gear:<kind>, al:close:<kind>,
    al:on|off:<kind>:gear:<kind>, al:snd:<kind>:on|off:<kind>."""
    is_open = open == "gear"
    rows = [[_button(menu_text("⚙️", is_open), f"al:close:{kind}" if is_open else f"al:open:gear:{kind}")]]
    if is_open:
        on = kind not in muted
        rows.append([_toggle("Alert", on, f"al:{'off' if on else 'on'}:{kind}:gear:{kind}")])
        if on and loud is not None:
            sound = kind in loud
            rows.append([_toggle("Sound", sound, f"al:snd:{kind}:{'off' if sound else 'on'}:{kind}")])
    return InlineKeyboardMarkup(rows)


def _list(kinds: list[str], is_on, data) -> list[list]:
    """A toggle for each of the alert types, PER_ROW to a row, a type struck through while it is off. is_on(kind): its state;
    data(kind): the callback data that flips it."""
    return list(map(list, batched((_toggle(LABELS[k], is_on(k), data(k)) for k in kinds), PER_ROW)))


def keyboard(muted: set[str], available: list[str], open: str | None = None, parent: str | None = None,
             loud: set[str] | None = None) -> InlineKeyboardMarkup:
    """The /alerts menu: two buttons, 🔔 Alerts and (private chats) 🔊 Sound. Each opens a menu with the full list of alert types, each a
    toggle struck through while it is off (alerts on or off in this chat; notification sound on or off), and ⬅️ Back. While one is open
    the other is hidden, its button included, until Back. `open`: alerts, sound (or one of an earlier version's sections). `loud`: the
    types whose alerts make a notification sound; None (a group) has no Sound. `parent`: the type of the alert these buttons sit under,
    which has a menu of its own (alert_keyboard)."""
    if parent:
        return alert_keyboard(parent, muted, loud, open)
    open = {"sub": "alerts", "unsub": "alerts", "other": "alerts", **dict.fromkeys(SETTINGS, "sound")}.get(open, open)
    if open == "alerts":
        rows = _list(available, lambda k: k not in muted, lambda k: f"al:{'off' if k not in muted else 'on'}:{k}:alerts")
    elif open == "sound" and loud is not None:
        rows = _list(available, lambda k: k in loud, lambda k: f"al:snd:{k}:{'off' if k in loud else 'on'}")
    else:
        return InlineKeyboardMarkup([[_button(menu_text("🔔 Alerts", False), "al:open:alerts"),
                                      *([_button(menu_text("🔊 Sound", False), "al:open:sound")] if loud is not None else [])]])
    return InlineKeyboardMarkup([*rows, [_button("⬅️ Back", "al:close")]])


def title(muted: set[str], available: list[str]) -> str:
    """The /alerts message: what is on and what is off."""
    on = [LABELS[k] for k in available if k not in muted]
    off = [LABELS[k] for k in available if k in muted]
    return f"{TITLE}\n✅ On: {', '.join(on) or 'nothing'}\n🔕 Off: {', '.join(off) or 'nothing'}"
