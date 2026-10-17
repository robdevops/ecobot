"""How the bot's log lines look. A line that quotes a request or a chat message (the quote last on the line) has the quote cut to
fit the log width (LOG_WIDTH), an ellipsis (…) marking the cut; the rest of a line, and every other line, is never cut."""

import logging

QUOTES = {"quotes": True}   # extra= for a line whose last argument is a request or a chat message


def fit(text: str, room: int) -> str:
    """The text in at most `room` characters, an ellipsis marking a cut (just the ellipsis when there is no room)."""
    return text if len(text) <= room else text[:max(room - 1, 0)] + "…"


class LogFormat(logging.Formatter):
    """"INFO message" for the main script's own lines, "INFO lib.bot: message" for the rest: the module name says who spoke."""

    def __init__(self, fmt: str, width: int = 0):
        """width: the most characters of a line that quotes (QUOTES); 0 cuts none."""
        super().__init__(fmt)
        self.width = width

    def format(self, record):
        record.where = "" if record.name == "ecobot" else f"{record.name}: "
        line = super().format(record)
        if not getattr(record, "quotes", False) or not isinstance(record.args, tuple) or not record.args:
            return line
        quote = str(record.args[-1])
        if not line.endswith(quote):   # a traceback or stack after it: left as it is
            return line
        head, quote = line[:len(line) - len(quote)], " ".join(quote.split())   # the quote on one line
        return head + (fit(quote, self.width - len(head)) if self.width else quote)
