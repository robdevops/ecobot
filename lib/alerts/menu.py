"""The alert settings as buttons. /alerts has the full menu: Sub | Unsub (and Settings in private chats), each opening its options below: Sub
lists the types that are off, Unsub the types that are on, so each list says by itself whether its alerts are on. Under an alert there
is one gear that opens that type's two toggles, Alert and Sound. Pure: it only builds the keyboard and the title from what a chat has muted.

Callback data (self-contained and short, so an old message's buttons still work after a restart):
  al:open:sub | al:open:unsub | al:open:set | al:open:snd_on | al:open:snd_off | al:close | al:noop (the heading rows of messages sent by an earlier version)
  al:snd:<kind|all>:<on|off>      (notification sound for one type; Settings is in private chats only)
  al:on:<kind|all>:<section> | al:off:<kind|all>:<section>      (section: sub or unsub, the part left open)
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
# the emoji on each type's button: the one its own alerts start with (every type has one: a test holds the two lists together)
EMOJI = {"rain": "\U0001f327️", "gusts": "\U0001f4a8", "uv": "\U0001f9f4", "temps": "\U0001f321️",
         "particulates": "\U0001f637", "pollen_and_asthma": "\U0001f33c", "forecast": "\U0001f504", "irrigation_battery": "\U0001faab", "irrigation_pause": "☔"}
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


def label(kind: str) -> str:
    """The name of an alert type on its button: its emoji, then its name."""
    return f"{EMOJI[kind]} {LABELS[kind]}"


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _type_rows(items: list[str], button, every: InlineKeyboardButton) -> list[list]:
    """A list of alert types as button rows, the same under Unsubscribe, Subscribe and both sound menus: `every` ("all") last when it has
    more than one type. `button(kind)` draws one type."""
    rows = list(map(list, batched(map(button, items), PER_ROW)))
    if len(items) > 1:
        rows.append([every])
    return rows


def struck(text: str) -> str:
    """The text with a line through it (a button cannot be styled, so each letter gets the combining long stroke)."""
    return "".join(f"{c}\u0336" for c in text)


def _toggle(emoji: str, word: str, on: bool, data: str) -> InlineKeyboardButton:
    """A button that flips a setting: its emoji and word, the word struck through while the setting is off."""
    return _button(f"{emoji} {word if on else struck(word)}", data)


def alert_keyboard(kind: str, muted: set[str], loud: set[str] | None, open: str | None = None) -> InlineKeyboardMarkup:
    """The buttons under an alert of type `kind`: one gear (⚙️ ▸, ▾ once open) that opens up to two toggles, 🔔 Alert (this type's alerts
    in this chat) and, only while that is on and in a private chat (`loud` is not None), 🔊 Sound (whether they make a notification sound).
    A toggle is struck through while it is off. Every button carries the type: al:open:gear:<kind>, al:close:<kind>,
    al:on|off:<kind>:gear:<kind>, al:snd:<kind>:on|off:<kind>."""
    is_open = open == "gear"
    rows = [[_button(menu_text("⚙️", is_open), f"al:close:{kind}" if is_open else f"al:open:gear:{kind}")]]
    if is_open:
        on = kind not in muted
        rows.append([_toggle("🔔", "Alert", on, f"al:{'off' if on else 'on'}:{kind}:gear:{kind}")])
        if on and loud is not None:
            sound = kind in loud
            rows.append([_toggle("🔊", "Sound", sound, f"al:snd:{kind}:{'off' if sound else 'on'}:{kind}")])
    return InlineKeyboardMarkup(rows)


def keyboard(muted: set[str], available: list[str], open: str | None = None, parent: str | None = None,
             loud: set[str] | None = None) -> InlineKeyboardMarkup:
    """The /alerts menu: Sub, Unsub and (private chats) Settings, one section open at a time (`open`: sub, unsub, set, snd_on, snd_off) with
    its options under it. A ▸ after the text marks a button that opens a menu, ▾ (in the same place) the open one; the rest do something.
    `loud`: the types whose alerts make a notification sound; None (a group) leaves out Settings, which holds two menus, Enable and Disable
    notification sounds, over the subscribed types. Sub is left out when every type is on. `parent`: the type of the alert these buttons
    sit under, which has a menu of its own (alert_keyboard)."""
    if parent:
        return alert_keyboard(parent, muted, loud, open)
    open = {"other": "unsub", "snd_on_o": "snd_on", "snd_off_o": "snd_off"}.get(open, open)   # (lists of messages from an earlier version)
    subscribed = options(muted, available, "unsub")
    def head(is_open: bool, text: str, section: str) -> InlineKeyboardButton:
        return _button(menu_text(text, is_open), "al:close" if is_open else f"al:open:{section}")
    no_sub = not options(muted, available, "sub")   # subscribed to every type: no Sub button
    if no_sub and open == "sub":
        open = None
    rows = [[*([] if no_sub else [head(open == "sub", "🔔", "sub")]), head(open == "unsub", "🔕", "unsub")]]
    if loud is not None:
        rows[0].append(head(open in SETTINGS, "⚙️", "set"))
    if open in SETTINGS and loud is not None:
        for section, heading, items in (("snd_on", "🔔 Enable notification sounds", [k for k in subscribed if k not in loud]),
                                        ("snd_off", "🔕 Disable notification sounds", [k for k in subscribed if k in loud])):
            if not items:
                continue
            here = open == section
            rows.append([_button(menu_text(heading, here), "al:open:set" if here else f"al:open:{section}")])
            if here:
                verb = "on" if section == "snd_on" else "off"
                rows += _type_rows(items, lambda k: _button(label(k), f"al:snd:{k}:{verb}"),
                                   _button("🔔 Enable all" if verb == "on" else "🔕 Disable all", f"al:snd:{ALL}:{verb}"))
    if open in ("sub", "unsub"):
        verb = "on" if open == "sub" else "off"
        rows += _type_rows(options(muted, available, open), lambda k: _button(label(k), f"al:{verb}:{k}:{open}"),
                           _button("🔔 Subscribe to all" if verb == "on" else "🔕 Unsubscribe from all", f"al:{verb}:{ALL}:{open}"))
    return InlineKeyboardMarkup(rows)


def has_options(muted: set[str], available: list[str], section: str) -> bool:
    """Would this section show any button under its heading?"""
    return len(keyboard(muted, available, section).inline_keyboard) > 1


def options(muted: set[str], available: list[str], section: str) -> list[str]:
    """What a section lists: Subscribe the types that are off, Unsubscribe the types that are on."""
    return [k for k in available if (k in muted) == (section == "sub")]


def title(muted: set[str], available: list[str]) -> str:
    """The /alerts message: what is on and what is off."""
    on = [LABELS[k] for k in available if k not in muted]
    off = [LABELS[k] for k in available if k in muted]
    return f"{TITLE}\n✅ On: {', '.join(on) or 'nothing'}\n🔕 Off: {', '.join(off) or 'nothing'}"
