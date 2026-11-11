"""The running code: the checked-out commit, so the log shows which code is running (the bot's and ecobot-mcp's)."""

from .config import ROOT


def version() -> str:
    """The checked-out commit, so the log shows which code is running."""
    return commit_in(ROOT / ".git")


def commit_in(git_dir) -> str:
    """The short hash HEAD points at, read from the repository's files rather than by running git (which refuses a folder owned by
    another user, as when the program runs as its own user). "unknown" when it cannot be read."""
    try:
        head = (git_dir / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(None, 1)[1]
            try:
                head = (git_dir / ref).read_text().strip()
            except FileNotFoundError:   # after `git pack-refs` or gc the branch is a line in packed-refs
                head = next(line.split()[0] for line in (git_dir / "packed-refs").read_text().splitlines()
                            if line.endswith(f" {ref}"))
        return head[:7] if len(head) >= 7 and all(c in "0123456789abcdef" for c in head.lower()) else "unknown"
    except Exception:
        return "unknown"
