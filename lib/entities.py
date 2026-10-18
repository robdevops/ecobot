"""A Telegram message's formatting as plain text plus entities (bold, italic, a link) rather than markup, so nothing in the text
needs escaping. Telegram places an entity in UTF-16 code units, where an emoji is two."""

from telegram import MessageEntity


def utf16_len(text: str) -> int:
    """The length of `text` as Telegram counts it."""
    return len(text.encode("utf-16-le")) // 2


def footer(text: str, line: str, style: str = MessageEntity.ITALIC, url: str | None = None) -> tuple[str, list[MessageEntity]]:
    """`text` with `line` under it, set in `style` (italic: a small footer; bold: an error) and linked to `url` if there is one."""
    start, length = utf16_len(text) + 1, utf16_len(line)
    links = [MessageEntity(MessageEntity.TEXT_LINK, start, length, url=url)] if url else []
    return f"{text}\n{line}", [MessageEntity(style, start, length), *links]


def with_link(text: str, link: tuple[str, str] | None) -> tuple[str, list[MessageEntity]]:
    """`text`, trimmed, with the link (label, url) under it as a small italic footer, if there is one."""
    return footer(text.rstrip(), link[0], url=link[1]) if link else (text.rstrip(), [])
