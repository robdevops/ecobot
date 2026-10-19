"""Shared look of the bot's inline buttons: a plain button, a menu opener (▸ closed, ▾ open) and a toggle (blue while on, struck
through while off). Every menu builds its buttons with these."""

from collections.abc import Callable

from telegram import InlineKeyboardButton
from telegram.constants import KeyboardButtonStyle


def button(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data, style=style)


def opener(text: str, is_open: bool, open_data: str, close_data: str) -> InlineKeyboardButton:
    """A button that opens a menu: ▸ after the text while it is closed, ▾ in the same place once it is open (so pressing it looks like the
    arrow turning), and pressing it then closes the menu. Buttons that do something have neither."""
    return button(f"{text} {'▾' if is_open else '▸'}", close_data if is_open else open_data)


def struck(text: str) -> str:
    """The text with a line through it (a button cannot be styled, so each letter gets the combining long stroke)."""
    return "".join(f"{c}̶" for c in text)


def toggle_button(word: str, on: bool, data: Callable[[bool], str]) -> InlineKeyboardButton:
    """A button that flips a setting: its word on a blue button while the setting is on, the word struck through on a plain button while it
    is off. `data(turn_on)`: the callback data of pressing it, which sets the setting to `turn_on` (the opposite of how it is now)."""
    return button(word if on else struck(word), data(not on), KeyboardButtonStyle.PRIMARY if on else None)
