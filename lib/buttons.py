"""Shared look of the bot's inline buttons."""

from telegram import InlineKeyboardButton
from telegram.constants import KeyboardButtonStyle


def menu_text(text: str, is_open: bool) -> str:
    """The label of a button that opens a menu: ▸ after the text while it is closed, ▾ in the same place once it is open (so pressing it
    looks like the arrow turning). Buttons that do something have neither."""
    return f"{text} {'▾' if is_open else '▸'}"


def struck(text: str) -> str:
    """The text with a line through it (a button cannot be styled, so each letter gets the combining long stroke)."""
    return "".join(f"{c}\u0336" for c in text)


def toggle_button(word: str, on: bool, data: str) -> InlineKeyboardButton:
    """A button that flips a setting: its word on a green button while the setting is on, the word struck through on a red one while it
    is off."""
    return InlineKeyboardButton(word if on else struck(word), callback_data=data,
                                style=KeyboardButtonStyle.SUCCESS if on else KeyboardButtonStyle.DANGER)
