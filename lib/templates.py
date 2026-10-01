"""The buttons under the message box in private chats (a persistent reply keyboard). A button sends its label as an ordinary
message, so the answer comes through the usual path; the label stands for a plain sentence (its words without the emoji)."""

from telegram import ReplyKeyboardMarkup

CAPABILITIES = "\U0001f514 Capabilities & alerts"  # answered with what the bot can do, then the /alerts status

ROWS = [[("\U0001f321️ Weather now", "Weather now"), ("\U0001f32c️ Air quality now", "Air quality now"), ("\U0001f4cb Report", "Report")],
        [(f"\U0001f4c8 Temperature chart {days}d", f"Temperature chart {days}d") for days in (7, 30, 90)],
        [("\U0001f3ed Air quality 7d", "Air quality PM1, PM2.5 and PM10 7d"),
         ("\U0001f3ed Air quality 30d", "Air quality PM1, PM2.5 and PM10 30d"),
         (CAPABILITIES, "What can you do?")]]
SENTENCES = dict(button for row in ROWS for button in row)
LABELS = set(SENTENCES)


def keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[label for label, _ in row] for row in ROWS], resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Ask about the weather or air…")


def sentence(text: str) -> str | None:
    """The question a button label stands for; None for anything else typed."""
    return SENTENCES.get(text.strip())
