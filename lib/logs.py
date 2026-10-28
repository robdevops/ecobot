"""How the bot's log lines look: the message alone, marked WARN, ERROR or DEBUG when it is not an ordinary (INFO) line. Only a chat
message (a request quoted last on a line) is shortened: put on one line and cut to the log width (LOG_WIDTH) with an ellipsis (…)
marking the cut. Every other line, and a traceback, is logged whole."""

import logging

# Errors that point at the code are logged with their traceback; any other failure (an outage, a refusal, a full disk) is one line
BUGS = (AssertionError, AttributeError, IndexError, KeyError, NameError, TypeError, ValueError, ZeroDivisionError)

QUOTES = {"quotes": True}   # extra= for a line whose last argument is a request or a chat message


def fit(text: str, room: int) -> str:
    """The text in at most `room` characters, an ellipsis marking a cut (just the ellipsis when there is no room)."""
    return text if len(text) <= room else text[:max(room - 1, 0)] + "…"


class LogFormat(logging.Formatter):
    """"INFO message": the level (WARNING is shown as WARN) and the message, with no module name. Cuts a quoted chat message to
    the width; a traceback (or stack) follows whole."""

    def __init__(self, fmt: str, width: int = 48):
        """width: the most characters of a chat message kept in the log, then an ellipsis; 0 keeps all."""
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
            one = " ".join(quote.split())   # the quote on one line, and short
            line = line[:len(line) - len(quote)] + (one if not self.width or len(one) <= self.width else one[:self.width] + "…")
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        return "\n".join(filter(None, [line, record.exc_text, record.stack_info and self.formatStack(record.stack_info)]))
