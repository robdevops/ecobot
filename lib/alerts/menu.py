"""The alert settings as buttons: Subscribe | Unsubscribe, each opening its options below: Subscribe lists the types that are off,
Unsubscribe the types that are on, so each list says by itself whether its alerts are on. Pure: it only builds the keyboard and
the title from what a chat has muted (the same menu goes under every alert and under /alerts).

Callback data (self-contained and short, so an old message's buttons still work after a restart):
  al:open:sub | al:open:unsub | al:open:other | al:open:set | al:open:snd_on | al:open:snd_off | al:close | al:noop (the heading rows of messages sent by an earlier version)
  al:snd:<kind|all>:<on|off>[:parent]      (notification sound for one type; Settings is in private chats only)
  al:on:<kind|all>:<section> | al:off:<kind|all>:<section>      (section: sub or unsub, the part left open)
An alert's buttons end with the alert's own type (parent): under an alert each list is its own type (marked ●) and "Other", which holds the rest and "all": al:open:unsub:rain, al:close:rain,
al:off:uv:unsub:rain (the sound menus' Other is al:open:snd_on_o:rain, al:open:snd_off_o:rain). A chat's /alerts message has no parent and no such field.
"""

from itertools import batched

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..buttons import menu_text

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


def _type_rows(items: list[str], parent: str | None, more: bool, button, other_data: tuple[str, str], every: InlineKeyboardButton) -> list[list]:
    """A list of alert types as button rows, the same under Unsubscribe, Subscribe and both sound menus. Under an alert that is in the
    list (with others), the alert's own type comes first, then an "Other" menu (`other_data` = the data that opens it, the data that
    closes it; `more`: it is open) that holds the rest and `every` ("all"). Otherwise a flat list, with `every` when it has more than
    one type. `button(kind)` draws one type."""
    rows: list[list] = []
    if parent in items and len(items) > 1:
        rows += [[button(parent)], [_button(menu_text("Other", more), other_data[1] if more else other_data[0])]]
        items = [k for k in items if k != parent] if more else []
    elif more:
        more = False   # nothing to put under Other
    rows += map(list, batched(map(button, items), PER_ROW))
    if len(items) > 1 or (more and items):
        rows.append([every])
    return rows


def keyboard(muted: set[str], available: list[str], open: str | None = None, parent: str | None = None,
             loud: set[str] | None = None) -> InlineKeyboardMarkup:
    """The menu: Subscribe, Unsubscribe and (private chats) Settings, one section open at a time (`open`: sub, unsub, other, set,
    snd_on, snd_off, or snd_on_o / snd_off_o, the Other list of a sound menu) with its options under it. A ▸ after the text marks a button that opens
    a menu, ▾ (in the same place) the open one; the rest do something. `parent` is the type of the alert the buttons sit under (carried in every button):
    its lists put that type first (●) and the rest under "Other". `loud`: the types whose alerts make a notification sound; None (a
    group) leaves out Settings, which holds two menus, Enable and Disable notification sounds, over the subscribed types."""
    tail = f":{parent}" if parent else ""
    subscribed = options(muted, available, "unsub")
    if open == "other" and not (parent in subscribed and len(subscribed) > 1):
        open = "unsub"
    unsub_open = open in ("unsub", "other")
    def head(is_open: bool, text: str, section: str) -> InlineKeyboardButton:
        return _button(menu_text(text, is_open), f"al:close{tail}" if is_open else f"al:open:{section}{tail}")
    rows = [[head(open == "sub", "➕ Sub", "sub"), head(unsub_open, "➖ Unsub", "unsub")]]
    if loud is not None:
        rows[0].append(head(open in SETTINGS, "⚙️ Settings", "set"))
    mark = lambda k: f"● {label(k)}" if k == parent else label(k)   # the alert these buttons are under
    if open in SETTINGS and loud is not None:
        for section, heading, items in (("snd_on", "🔔 Enable notification sounds", [k for k in subscribed if k not in loud]),
                                        ("snd_off", "🔕 Disable notification sounds", [k for k in subscribed if k in loud])):
            here = open in (section, f"{section}_o")
            if not items:
                continue
            rows.append([_button(menu_text(heading, here), f"al:open:set{tail}" if here else f"al:open:{section}{tail}")])
            if here:
                verb = "on" if section == "snd_on" else "off"
                rows += _type_rows(items, parent, open == f"{section}_o", lambda k: _button(mark(k), f"al:snd:{k}:{verb}{tail}"),
                                   (f"al:open:{section}_o{tail}", f"al:open:{section}{tail}"),
                                   _button("🔔 Enable all" if verb == "on" else "🔕 Disable all", f"al:snd:{ALL}:{verb}{tail}"))
    if open in ("sub", "unsub", "other"):
        verb = "on" if open == "sub" else "off"
        rows += _type_rows(options(muted, available, "sub" if open == "sub" else "unsub"), parent if open != "sub" else None, open == "other",
                           lambda k: _button(mark(k), f"al:{verb}:{k}:{open}{tail}"), (f"al:open:other{tail}", f"al:open:unsub{tail}"),
                           _button("🔔 Subscribe to all" if verb == "on" else "🔕 Unsubscribe from all", f"al:{verb}:{ALL}:{open}{tail}"))
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
