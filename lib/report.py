"""The bold names of the report's headings (the service's current, pollen and forecast write the report's text: lib/service)."""

import re

from telegram import MessageEntity

from .entities import utf16_len


# ---------- the report's headings (the service's current, pollen and forecast write them) ----------
HEADINGS = ("Weather station", "Air quality", "Pollen & asthma", "Forecast")
HEADING_LINE = re.compile(rf"^({'|'.join(map(re.escape, HEADINGS))})(?= \(|$)", re.M)


def heading_entities(text: str) -> list[MessageEntity]:
    """Bold entities over the name of each heading line in `text` (not over its brackets)."""
    return [MessageEntity(MessageEntity.BOLD, utf16_len(text[:m.start()]), utf16_len(m.group(1))) for m in HEADING_LINE.finditer(text)]
