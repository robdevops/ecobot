"""How the bot's log lines look: the message alone, marked WARN, ERROR or DEBUG when it is not an ordinary (INFO) line. Only a chat
message (a request quoted last on a line) is shortened: put on one line and cut to the log width (LOG_WIDTH) with an ellipsis (…)
marking the cut. Every other line, and a traceback, is logged whole.

DEBUG and INFO lines go to stdout, WARN and ERROR to stderr, so Docker (and its journald driver) tells them apart. Under systemd both go
to the journal, which would take every line as information: there each line starts with its syslog priority (<6>, <4>, <3> ...), which
the journal reads as the line's level and takes off, so journalctl shows warnings and errors in colour."""

import logging
import os
import sys

# Errors that point at the code are logged with their traceback; any other failure (an outage, a refusal, a full disk) is one line
BUGS = (AssertionError, AttributeError, IndexError, KeyError, NameError, TypeError, ValueError, ZeroDivisionError)

QUOTES = {"quotes": True}   # extra= for a line whose last argument is a request or a chat message
PRIORITY = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3, logging.CRITICAL: 2}   # syslog's, by level


def fit(text: str, room: int) -> str:
    """The text in at most `room` characters, an ellipsis marking a cut (just the ellipsis when there is no room)."""
    return text if len(text) <= room else text[:max(room - 1, 0)] + "…"


class LogFormat(logging.Formatter):
    """"INFO message": the level (WARNING is shown as WARN) and the message, with no module name. Cuts a quoted chat message to
    the width; a traceback (or stack) follows whole."""

    def __init__(self, fmt: str, width: int = 24, priority: bool = False):
        """width: the most characters of a chat message kept in the log, then an ellipsis; 0 keeps all. priority: each line starts
        with its syslog priority, for systemd's journal (on_journal)."""
        super().__init__(fmt)
        self.width, self.priority = width, priority

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
        text = "\n".join(filter(None, [line, record.exc_text, record.stack_info and self.formatStack(record.stack_info)]))
        if self.priority:   # every line: the journal takes each line of a traceback as an entry of its own
            mark = f"<{PRIORITY.get(record.levelno, 6)}>"
            text = "\n".join(mark + part for part in text.split("\n"))
        return text


def on_journal(*streams) -> bool:
    """Is one of these streams (stderr and stdout by default) systemd's journal? systemd names the stream it connected in JOURNAL_STREAM
    (its device and inode); a stream the bot's output was sent elsewhere by (a pipe, a terminal) does not match."""
    name = os.environ.get("JOURNAL_STREAM", "")
    for stream in streams or (sys.stderr, sys.stdout):
        try:
            found = os.fstat(stream.fileno())
        except (AttributeError, OSError, ValueError):
            continue
        if name and name == f"{found.st_dev}:{found.st_ino}":
            return True
    return False


def stream_handlers(formatter: logging.Formatter, out=None, err=None) -> list[logging.Handler]:
    """Two handlers with one format: DEBUG and INFO lines to `out` (stdout), WARN and ERROR to `err` (stderr)."""
    below = logging.StreamHandler(out or sys.stdout)
    below.addFilter(lambda record: record.levelno < logging.WARNING)
    above = logging.StreamHandler(err or sys.stderr)
    above.setLevel(logging.WARNING)
    for handler in (below, above):
        handler.setFormatter(formatter)
    return [below, above]
