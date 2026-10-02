"""The alert settings as buttons: Subscribe | Unsubscribe, each opening its options below: Subscribe lists the types that are off,
Unsubscribe the types that are on, so each list says by itself whether its alerts are on. Pure: it only builds the keyboard and
the title from what a chat has muted (the same menu goes under every alert and under /alerts).

Callback data (self-contained and short, so an old message's buttons still work after a restart):
  al:open:sub | al:open:unsub | al:close | al:noop (the heading rows of messages sent by an earlier version)
  al:on:<kind|all>:<section> | al:off:<kind|all>:<section>      (section: sub or unsub, the part left open)
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# kind -> the name on the button, in the order listed
LABELS = {"rain": "rain", "rain_likely": "rain likely", "gusts": "gusts", "uv": "UV", "temps": "temperature crossing",
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


def keyboard(muted: set[str], available: list[str], open: str | None = None) -> InlineKeyboardMarkup:
    """The collapsed menu, or with one section open (open = "sub" or "unsub") and its options under it."""
    rows = [[_button(("▾ " if open == "sub" else "") + "➕ Subscribe", "al:close" if open == "sub" else "al:open:sub"),
             _button(("▾ " if open == "unsub" else "") + "➖ Unsubscribe", "al:close" if open == "unsub" else "al:open:unsub")]]
    if open in ("sub", "unsub"):
        listed = options(muted, available, open)
        verb = "on" if open == "sub" else "off"
        buttons = [_button(LABELS[k], f"al:{verb}:{k}:{open}") for k in listed]
        rows += [buttons[i:i + PER_ROW] for i in range(0, len(buttons), PER_ROW)]
        if listed:
            rows.append([_button("All alerts", f"al:{verb}:{ALL}:{open}")])
    return InlineKeyboardMarkup(rows)


def options(muted: set[str], available: list[str], section: str) -> list[str]:
    """What a section lists: Subscribe the types that are off, Unsubscribe the types that are on."""
    return [k for k in available if (k in muted) == (section == "sub")]


def title(muted: set[str], available: list[str]) -> str:
    """The /alerts message: what is on and what is off."""
    on = [LABELS[k] for k in available if k not in muted]
    off = [LABELS[k] for k in available if k in muted]
    return f"{TITLE}\n✅ On: {', '.join(on) or 'nothing'}\n🔕 Off: {', '.join(off) or 'nothing'}"
