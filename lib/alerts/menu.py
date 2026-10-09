"""The alert settings as buttons: Subscribe | Unsubscribe, each opening its options below: Subscribe lists the types that are off,
Unsubscribe the types that are on, so each list says by itself whether its alerts are on. Pure: it only builds the keyboard and
the title from what a chat has muted (the same menu goes under every alert and under /alerts).

Callback data (self-contained and short, so an old message's buttons still work after a restart):
  al:open:sub | al:open:unsub | al:open:other | al:open:set | al:open:snd_on | al:open:snd_off | al:close | al:noop (the heading rows of messages sent by an earlier version)
  al:snd:<kind>:<on|off>[:parent]      (notification sound for one type; Settings is in private chats only)
  al:on:<kind|all>:<section> | al:off:<kind|all>:<section>      (section: sub or unsub, the part left open)
An alert's buttons end with the alert's own type (parent): under an alert the Unsubscribe list is its own type (marked ●) and "Other", which holds the rest and "all": al:open:unsub:rain, al:close:rain,
al:off:uv:unsub:rain. A chat's /alerts message has no parent and no such field.
"""

from itertools import batched

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# kind -> the name on the button, in the order listed
LABELS = {"rain": "rain", "rain_likely": "rain predicted", "gusts": "gusts", "uv": "UV", "temps": "temperature crossing",
          "air": "particulates", "pollen": "pollen & asthma", "forecast": "forecast changes",
          "irrigation": "irrigation battery", "irrigation_pause": "irrigation pause"}
# the emoji on each type's button: the one its own alerts start with (every type has one: a test holds the two lists together)
EMOJI = {"rain": "\U0001f327️", "rain_likely": "\U0001f326️", "gusts": "\U0001f4a8", "uv": "\U0001f9f4", "temps": "\U0001f321️",
         "air": "\U0001f637", "pollen": "\U0001f33c", "forecast": "\U0001f504", "irrigation": "\U0001faab", "irrigation_pause": "☔"}
ALL = "all"
TITLE = "🔔 Alerts in this chat"
PER_ROW = 2
SETTINGS = ("set", "snd_on", "snd_off", "snd_on_o", "snd_off_o")   # (_o: the "Other" list inside a sound menu)


def available_kinds(sources: set[str]) -> list[str]:
    """The alert types the bot can send, from the names of its sources."""
    wanted = (["rain", "rain_likely", "gusts", "uv", "temps"] if "Ecowitt" in sources else []) + (
        ["air"] if "AirGradient" in sources else []) + (["pollen"] if "Pollen" in sources else []) + (
        ["forecast"] if "Forecast" in sources else []) + (["irrigation", "irrigation_pause"] if "Irrigation" in sources else [])
    return wanted


def label(kind: str) -> str:
    """The name of an alert type on its button: its emoji, then its name."""
    return f"{EMOJI[kind]} {LABELS[kind]}"


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def keyboard(muted: set[str], available: list[str], open: str | None = None, parent: str | None = None,
             loud: set[str] | None = None) -> InlineKeyboardMarkup:
    """The collapsed menu, or with one section open (open = "sub" or "unsub") and its options under it. `parent` is the type of the
    alert the buttons sit under and is carried in every button. Under an alert that is on, the Unsubscribe list is that type
    (marked) and an "Other" menu (open = "other") holding the rest and "Unsubscribe from all"; on the /alerts message it lists every type
    that is on, with "all" when more than one. `loud`: the types whose alerts make a notification sound; None (a group) leaves out the Settings button,
    which opens two sub-menus, enable and disable notification sounds, each listing the subscribed types it applies to."""
    tail = f":{parent}" if parent else ""
    listed = options(muted, available, "unsub")
    nested = parent in listed and len(listed) > 1   # under an alert that is still on: its own type, then the others under "Other"
    if open == "other" and not nested:
        open = "unsub"
    unsub_open = open in ("unsub", "other")
    rows = [[_button(("▾ " if open == "sub" else "") + "➕ Subscribe", f"al:close{tail}" if open == "sub" else f"al:open:sub{tail}"),
             _button(("▾ " if unsub_open else "") + "➖ Unsubscribe", f"al:close{tail}" if unsub_open else f"al:open:unsub{tail}")]]
    if loud is not None:
        in_settings = open in SETTINGS
        rows[0].append(_button(("▾ " if in_settings else "") + "⚙️ Settings" + ("" if in_settings else " ▸"),
                               f"al:close{tail}" if in_settings else f"al:open:set{tail}"))
    if open in SETTINGS and loud is not None:
        pool = [k for k in available if k not in muted]   # only the types this chat gets
        for section, heading, items in (("snd_on", "🔔 Enable notification sounds", [k for k in pool if k not in loud]),
                                        ("snd_off", "🔕 Disable notification sounds", [k for k in pool if k in loud])):
            if not items:
                continue
            here = open in (section, f"{section}_o")
            rows.append([_button(f"▾ {heading}" if here else f"{heading} ▸",
                                 f"al:open:set{tail}" if here else f"al:open:{section}{tail}")])   # ▸ marks a menu, not an action
            if here:
                button = lambda k: _button(f"● {label(k)}" if k == parent else label(k), f"al:snd:{k}:{'on' if section == 'snd_on' else 'off'}{tail}")
                if parent in items and len(items) > 1:   # under an alert: its own type, then "Other" with the rest
                    more = open == f"{section}_o"
                    rows.append([button(parent)])
                    rows.append([_button("▾ Other" if more else "Other ▸", f"al:open:{section}{tail}" if more else f"al:open:{section}_o{tail}")])
                    items = [k for k in items if k != parent] if more else []
                rows += map(list, batched([button(k) for k in items], PER_ROW))
    if open in ("sub", "unsub", "other"):
        verb = "on" if open == "sub" else "off"
        listed = options(muted, available, "sub" if open == "sub" else "unsub")
        mark = lambda k: f"● {label(k)}" if open != "sub" and k == parent else label(k)   # the alert these buttons are under
        if nested and open != "sub":
            rows.append([_button(mark(parent), f"al:off:{parent}:{open}{tail}")])
            rows.append([_button("▾ Other" if open == "other" else "Other ▸", f"al:open:unsub{tail}" if open == "other" else f"al:open:other{tail}")])
            listed = listed if open == "other" else []
            listed = [k for k in listed if k != parent]
        buttons = [_button(mark(k), f"al:{verb}:{k}:{open}{tail}") for k in listed]
        rows += map(list, batched(buttons, PER_ROW))
        if len(listed) > 1 or (open == "other" and listed):
            rows.append([_button("🔔 Subscribe to all" if open == "sub" else "🔕 Unsubscribe from all", f"al:{verb}:{ALL}:{open}{tail}")])
    return InlineKeyboardMarkup(rows)


def has_options(muted: set[str], available: list[str], section: str, parent: str | None = None) -> bool:
    """Would this section show any button under its heading?"""
    return len(keyboard(muted, available, section, parent).inline_keyboard) > 1


def options(muted: set[str], available: list[str], section: str) -> list[str]:
    """What a section lists: Subscribe the types that are off, Unsubscribe the types that are on."""
    return [k for k in available if (k in muted) == (section == "sub")]


def title(muted: set[str], available: list[str]) -> str:
    """The /alerts message: what is on and what is off."""
    on = [LABELS[k] for k in available if k not in muted]
    off = [LABELS[k] for k in available if k in muted]
    return f"{TITLE}\n✅ On: {', '.join(on) or 'nothing'}\n🔕 Off: {', '.join(off) or 'nothing'}"
