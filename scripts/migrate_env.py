#!/usr/bin/env python3
"""Rename the settings in an environment file (/etc/ecobot.env) to their new names (docs/configuration.md, "Renamed settings"):
ecobot-mcp's are MCP_..., the bot's BOT_..., the shared ones unprefixed. Values, comments, blank lines and order are kept.

    sudo python3 scripts/migrate_env.py /etc/ecobot.env            # show what would change (secrets masked); changes nothing
    sudo python3 scripts/migrate_env.py /etc/ecobot.env --write    # change it, after a backup beside it (same owner and mode)

The old names still work (with a warning at start), so this is a tidy-up, not a must. It reads the renames from lib/config.py's text
(no import: any Python 3.9 or newer, no packages), so it works in a Docker install too. It can be run again: a renamed file is left
alone. It stops, changing nothing, where it can't tell what you meant: an old and a new name both set to different values.

Beyond the plain renames:
  - the bot's retired token, WEATHER_MCP_TOKEN, goes into AUTH_TOKENS (the shared list, was MCP_SERVICE_TOKENS) as its first token,
    the one the bot sends; dropped if it is already there;
  - the old container's name, ecobot-service, in a URL or an allowed host becomes ecobot-mcp's.
"""

import argparse
import difflib
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "lib" / "config.py"
RETIRED_TOKEN, TOKENS = "WEATHER_MCP_TOKEN", "AUTH_TOKENS"
OLD_HOST, NEW_HOST = "ecobot-service", "ecobot-mcp"
SECRET = re.compile(r"TOKEN|KEY|SECRET|PASSWORD")
LINE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*?)\s*$")


def renames(config: Path = CONFIG) -> dict[str, str]:
    """{old name: new name}, from lib/config.py's _setting("NEW", ..., old="OLD") calls."""
    text = config.read_text()
    found = dict((old, new) for new, old in re.findall(r'_setting\(\s*"([A-Z0-9_]+)"[^\n]*?old="([A-Z0-9_]+)"', text))
    if not found:
        raise SystemExit(f"No renamed settings found in {config}: is this the ecobot checkout?")
    return found


def unquoted(value: str) -> tuple[str, str]:
    """(the value without its quotes, the quote it had)."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1], value[0]
    return value, ""


def migrate(lines: list[str], names: dict[str, str]) -> tuple[list[str], list[str]]:
    """(the file's lines renamed, notes on what changed). SystemExit on a conflict."""
    parsed = [LINE.match(line.rstrip("\n")) for line in lines]
    values: dict[str, str] = {}
    for m in parsed:
        if m and not m.group(2).startswith("#"):
            values[m.group(2)] = unquoted(m.group(4))[0]
    conflicts = [f"{old} and {new}" for old, new in names.items()
                 if old in values and new in values and values[old] != values[new]]
    if conflicts:
        raise SystemExit("Both an old and a new name are set, to different values (keep one, then run this again): " + "; ".join(conflicts))
    notes, out = [], []
    token = values.get(RETIRED_TOKEN, "")
    tokens_at = next((i for i, m in enumerate(parsed) if m and names.get(m.group(2), m.group(2)) == TOKENS), None)
    for i, (line, m) in enumerate(zip(lines, parsed)):
        if not m:
            out.append(line)
            continue
        lead, key, eq, raw = m.groups()
        value, quote = unquoted(raw)
        end = "\n" if line.endswith("\n") else ""
        if key == RETIRED_TOKEN:
            if tokens_at is None:   # (no list yet: this becomes it)
                out.append(f"{lead}{TOKENS}{eq}{raw}{end}")
                notes.append(f"{RETIRED_TOKEN} -> {TOKENS}")
            else:
                notes.append(f"{RETIRED_TOKEN} removed: its token is the first of {TOKENS}, the one the bot sends")
            continue
        new = names.get(key)
        if new and new in values:   # (the new one is there already, with the same value: the old line goes)
            notes.append(f"{key} removed: {new} is already set to the same value")
            continue
        name = new or key
        if new:
            notes.append(f"{key} -> {new}")
        if name == TOKENS and token:
            listed = [t.strip() for t in value.split(",") if t.strip()]
            value = ",".join([token, *[t for t in listed if t != token]])
            if listed[:1] != [token]:
                notes.append(f"{TOKENS}: {RETIRED_TOKEN}'s token put first, the one the bot sends")
        if OLD_HOST in value and name in ("BOT_MCP_URL", "MCP_ALLOWED_HOSTS"):
            value = value.replace(OLD_HOST, NEW_HOST)
            notes.append(f"{name}: {OLD_HOST} -> {NEW_HOST} (the container's new name)")
        out.append(f"{lead}{name}{eq}{quote}{value}{quote}{end}")
    return out, notes


def masked(line: str) -> str:
    m = LINE.match(line.rstrip("\n"))
    if m and SECRET.search(m.group(2)) and m.group(4):
        return f"{m.group(1)}{m.group(2)}{m.group(3)}****"
    return line.rstrip("\n")


def write(path: Path, lines: list[str]) -> Path:
    """The file replaced in one step, with its owner and mode; the old one kept beside it first. Returns the backup's path."""
    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    st = path.stat()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(lines)
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass   # (not root: it is then the caller's own file)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return backup


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", type=Path, help="the environment file, e.g. /etc/ecobot.env")
    ap.add_argument("--write", action="store_true", help="change the file (a backup is kept beside it); without it, only show")
    args = ap.parse_args(argv)
    lines = args.path.read_text().splitlines(keepends=True)
    new, notes = migrate(lines, renames())
    if new == lines:
        print(f"{args.path}: nothing to rename")
        return 0
    for note in notes:
        print(f"  {note}")
    print()
    old_set, new_set = [masked(l) for l in lines], [masked(l) for l in new]
    sys.stdout.writelines(l + "\n" for l in difflib.unified_diff(old_set, new_set, str(args.path), f"{args.path} (renamed)", lineterm=""))
    if not args.write:
        print("\nNothing changed. Run again with --write to change the file.")
        return 0
    backup = write(args.path, new)
    print(f"\nChanged {args.path}; the old file is {backup}. Restart ecobot-mcp and the bot to use it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
