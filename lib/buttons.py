"""Shared look of the bot's inline buttons."""


def menu_text(text: str, is_open: bool) -> str:
    """The label of a button that opens a menu: ▸ after the text while it is closed, ▾ in the same place once it is open (so pressing it
    looks like the arrow turning). Buttons that do something have neither."""
    return f"{text} {'▾' if is_open else '▸'}"
