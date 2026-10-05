"""The alert settings as buttons: Subscribe | Unsubscribe, each opening its options below: Subscribe lists the types that are off,
Unsubscribe the types that are on, so each list says by itself whether its alerts are on. Pure: it only builds the keyboard and
the title from what a chat has muted (the same menu goes under every alert and under /alerts).

Callback data (self-contained and short, so an old message's buttons still work after a restart):
  al:open:sub | al:open:unsub | al:close | al:noop (the heading rows of messages sent by an earlier version)
  al:on:<kind|all>:<section> | al:off:<kind|all>:<section>      (section: sub or unsub, the part left open)
An alert's buttons end with the alert's own type (parent), so the Unsubscribe list can mark it: al:open:unsub:rain, al:close:rain,
al:off:uv:unsub:rain. A chat's /alerts message has no parent and no such field.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# kind -> the name on the button, in the order listed
LABELS = {"rain": "rain", "rain_likely": "rain predicted", "gusts": "gusts", "uv": "UV", "temps": "temperature crossing",
          "air": "air quality", "pollen": "pollen & asthma", "forecast": "forecast changes"}
ALL = "all"
TITLE = "🔔 Alerts in this chat"
PER_ROW = 2


def available_kinds(sources: set[str]) -> list[str]:
    """The alert types the bot can send, from the names of its sources."""
    wanted = (["rain", "rain_likely", "gusts", "uv", "temps"] if "Ecowitt" in sources else []) + (
        ["air"] if "AirGradient" in sources else []) + (["pollen"] if "Pollen" in sources else []) + (
        ["forecast"] if "Forecast" in sources else [])
    return wanted


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def keyboard(muted: set[str], available: list[str], open: str | None = None, parent: str | None = None) -> InlineKeyboardMarkup:
    """The collapsed menu, or with one section open (open = "sub" or "unsub") and its options under it. `parent` is the type of the
    alert the buttons sit under: it is marked in the Unsubscribe list and carried in every button. A list of one has no "all" button."""
    tail = f":{parent}" if parent else ""
    rows = [[_button(("▾ " if open == "sub" else "") + "➕ Subscribe", f"al:close{tail}" if open == "sub" else f"al:open:sub{tail}"),
             _button(("▾ " if open == "unsub" else "") + "➖ Unsubscribe", f"al:close{tail}" if open == "unsub" else f"al:open:unsub{tail}")]]
    if open in ("sub", "unsub"):
        listed = options(muted, available, open)
        verb = "on" if open == "sub" else "off"
        mark = lambda k: f"● {LABELS[k]}" if open == "unsub" and k == parent else LABELS[k]   # the alert these buttons are under
        buttons = [_button(mark(k), f"al:{verb}:{k}:{open}{tail}") for k in listed]
        rows += [buttons[i:i + PER_ROW] for i in range(0, len(buttons), PER_ROW)]
        if len(listed) > 1:
            rows.append([_button("Subscribe to all" if open == "sub" else "Unsubscribe from all", f"al:{verb}:{ALL}:{open}{tail}")])
    return InlineKeyboardMarkup(rows)


def options(muted: set[str], available: list[str], section: str) -> list[str]:
    """What a section lists: Subscribe the types that are off, Unsubscribe the types that are on."""
    return [k for k in available if (k in muted) == (section == "sub")]


def title(muted: set[str], available: list[str]) -> str:
    """The /alerts message: what is on and what is off."""
    on = [LABELS[k] for k in available if k not in muted]
    off = [LABELS[k] for k in available if k in muted]
    return f"{TITLE}\n✅ On: {', '.join(on) or 'nothing'}\n🔕 Off: {', '.join(off) or 'nothing'}"
