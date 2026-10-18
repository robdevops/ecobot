"""Shared look of the bot's inline buttons."""

from telegram import InlineKeyboardButton
from telegram.constants import KeyboardButtonStyle


def menu_text(text: str, is_open: bool) -> str:
    """The label of a button that opens a menu: ▸ after the text while it is closed, ▾ in the same place once it is open (so pressing it
    looks like the arrow turning). Buttons that do something have neither."""
    return f"{text} {'▾' if is_open else '▸'}"


def toggle_button(word: str, on: bool, data: str) -> InlineKeyboardButton:
    """A button that flips a setting: its word after a ✅ on a green button while the setting is on, after a ❌ on a red one while it is
    off (the tick and cross stay for a client that does not draw button colours)."""
    return InlineKeyboardButton(f"{'✅' if on else '❌'} {word}", callback_data=data,
                                style=KeyboardButtonStyle.SUCCESS if on else KeyboardButtonStyle.DANGER)
