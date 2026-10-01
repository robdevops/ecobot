"""The buttons under the message box in private chats (a persistent reply keyboard). A button sends its label as an ordinary
message, so the answer comes through the usual path; the leading emoji is dropped before the sentence is read."""

from telegram import ReplyKeyboardMarkup

ALERTS = "\U0001f514 Alerts"  # answered by the /alerts command, not the model

ROWS = [["\U0001f321️ Weather now", "\U0001f32c️ Air quality now", "\U0001f4cb Report"],
        ["\U0001f4c8 Temperature chart 7d", "\U0001f327️ Rain chart 7d", "\U0001f4a7 Humidity chart 7d"],
        ["\U0001f324️ Weather all week", "\U0001f3ed AQ all week", ALERTS]]
LABELS = {label for row in ROWS for label in row}


def keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(ROWS, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Ask about the weather or air…")


def sentence(text: str) -> str | None:
    """The question a button label stands for (its words without the emoji); None for anything else typed."""
    text = text.strip()
    return text.split(" ", 1)[1] if text in LABELS else None
