"""How the bot's log lines look. Every line of the log is cut to the log width (LOG_WIDTH), an ellipsis (…) marking the cut. A line that
quotes a request or a chat message (the quote last on the line) also has the quote put on one line first."""

import logging

QUOTES = {"quotes": True}   # extra= for a line whose last argument is a request or a chat message


def fit(text: str, room: int) -> str:
    """The text in at most `room` characters, an ellipsis marking a cut (just the ellipsis when there is no room)."""
    return text if len(text) <= room else text[:max(room - 1, 0)] + "…"


class LogFormat(logging.Formatter):
    """"INFO message": the level and the message, with no module name. Cuts each line to the width."""

    def __init__(self, fmt: str, width: int = 0):
        """width: the most characters of a line of the log; 0 cuts none."""
        super().__init__(fmt)
        self.width = width

    def format(self, record):
        line = super().format(record)
        quote = str(record.args[-1]) if getattr(record, "quotes", False) and isinstance(record.args, tuple) and record.args else None
        if quote and line.endswith(quote):   # (not when a traceback follows it)
            line = line[:len(line) - len(quote)] + " ".join(quote.split())   # the quote on one line
        return "\n".join(fit(part, self.width) for part in line.split("\n")) if self.width else line
