"""How the bot's log lines look: the message alone, marked WARN, ERROR or DEBUG when it is not an ordinary (INFO) line. Every line of a message is cut to the log width (LOG_WIDTH), an ellipsis (…) marking the cut,
whatever its level; a traceback after it is logged whole. A line that quotes a request or a chat message (the quote last on the
line) also has the quote put on one line first."""

import logging

QUOTES = {"quotes": True}   # extra= for a line whose last argument is a request or a chat message


def fit(text: str, room: int) -> str:
    """The text in at most `room` characters, an ellipsis marking a cut (just the ellipsis when there is no room)."""
    return text if len(text) <= room else text[:max(room - 1, 0)] + "…"


class LogFormat(logging.Formatter):
    """"INFO message": the level (WARNING is shown as WARN) and the message, with no module name. Cuts each line of the message to
    the width; a traceback (or stack) follows whole."""

    def __init__(self, fmt: str, width: int = 0):
        """width: the most characters of a line of the log; 0 cuts none."""
        super().__init__(fmt)
        self.width = width

    def format(self, record):
        shown, text = record.levelname, record.msg
        record.levelname = "WARN" if shown == "WARNING" else shown
        if isinstance(text, str):   # the HTTP client's own lines: "POST https://... "HTTP/1.1 200 OK"", not "HTTP Request: POST ..."
            record.msg = text.removeprefix("HTTP Request: ")
        try:
            record.message = record.getMessage()
            if self.usesTime():
                record.asctime = self.formatTime(record, self.datefmt)
            line = self.formatMessage(record)
            if shown == "INFO":   # the ordinary line has no label: only the exceptions (DEBUG, WARN, ERROR) are marked
                line = line.removeprefix("INFO ")
        finally:
            record.levelname, record.msg = shown, text
        quote = str(record.args[-1]) if getattr(record, "quotes", False) and isinstance(record.args, tuple) and record.args else None
        if quote and line.endswith(quote):
            line = line[:len(line) - len(quote)] + " ".join(quote.split())   # the quote on one line
        if self.width:
            line = "\n".join(fit(part, self.width) for part in line.split("\n"))
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        return "\n".join(filter(None, [line, record.exc_text, record.stack_info and self.formatStack(record.stack_info)]))
