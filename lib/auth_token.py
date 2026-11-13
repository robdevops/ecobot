"""The token ecobot-mcp generates when AUTH_TOKENS is unset: made at its first start, kept in the data folder (auth_token, readable by
its own user only) and used again at every start after. The bot, with AUTH_TOKENS unset too, reads it from the same folder and sends it.
Its value is never logged: only where it is."""

import contextlib
import os
import secrets
from pathlib import Path

FILE = "auth_token"


def path(folder: Path) -> Path:
    return Path(folder) / FILE


def read(folder: Path) -> str | None:
    """The token kept in `folder`; None when there is none yet, or it can't be read."""
    try:
        return path(folder).read_text().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def ensure(folder: Path) -> str:
    """The token kept in `folder`, made first if there is none: written whole to a file of its own (mode 0600) and then linked into
    place, so a reader never sees half of one, and two starting at once keep the one that got there first."""
    if token := read(folder):
        return token
    target = path(folder)
    token = secrets.token_urlsafe(32)
    temporary = target.with_name(f".{FILE}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temporary, target)   # (fails if one is there: an empty or unreadable one is replaced, a good one is kept)
        except FileExistsError:
            if kept := read(folder):
                return kept
            os.replace(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    return token
