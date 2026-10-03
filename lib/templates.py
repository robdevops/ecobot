"""The buttons under the message box in private chats (a persistent reply keyboard). A button sends its label as an ordinary
message, so the answer comes through the usual path; the label stands for a plain sentence (its words without the emoji)."""

import hashlib

from telegram import ReplyKeyboardMarkup

CAPABILITIES = "\U0001f514 Alerts & Capabilities"  # answered with what the bot can do, then the /alerts status

ROWS = [[("\U0001f4c8 Temperature 7d", "Temperature chart 7d"), (CAPABILITIES, "What can you do?"), ("\U0001f4cb Report", "Report")],
        [(f"\U0001f326️ Weather {days}d", f"Weather chart {days}d") for days in (7, 30, 90)],
        [(f"\U0001f3ed Air Qual. {days}d", f"Air quality all metrics {days}d") for days in (7, 30, 90)]]
SENTENCES = dict(button for row in ROWS for button in row)
LABELS = set(SENTENCES)
# Telegram keeps the keyboard in each app until a message brings a new one; this changes whenever the buttons do, so a chat
# whose stored value differs gets the current keyboard with its next reply. HIDDEN is what a chat that hid the buttons stores.
VERSION = hashlib.md5("|".join(label for row in ROWS for label, _ in row).encode()).hexdigest()[:8]
HIDDEN = "hidden"


def keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[label for label, _ in row] for row in ROWS], resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Ask about the weather or air…")


def sentence(text: str) -> str | None:
    """The question a button label stands for; None for anything else typed."""
    return SENTENCES.get(text.strip())


WEATHER_METRICS = ["Temperature, humidity (indoor, outdoor)", "Dew point, vapour pressure deficit, pressure, wind speed",
                   "Rain, solar radiation, UV index"]
AIR_METRICS = ["PM1, PM2.5, PM10, CO₂, VOC, NOx"]
POLLEN = ["Pollen and thunderstorm asthma risk (October to December)"]
FORECAST = ["Forecast"]
HISTORY = ["History charts"]


def capabilities_text(weather: bool = True, air: bool = True, pollen: bool = False, forecast: bool = False) -> str:
    """What the bot measures, as a short bulleted list (no dates, no detail): the answer to the Capabilities button."""
    lines = (WEATHER_METRICS if weather else []) + (AIR_METRICS if air else []) + (POLLEN if pollen else []) + (
        FORECAST if forecast else []) + HISTORY
    return "\n".join(f"\u2022 {line}" for line in lines)
