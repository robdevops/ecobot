"""The buttons under the message box in private chats (a persistent reply keyboard). A button sends its label as an ordinary
message, so the answer comes through the usual path; the label stands for a plain sentence (its words without the emoji)."""

import hashlib
from collections.abc import Collection

from telegram import ReplyKeyboardMarkup

CAPABILITIES = "\U0001f514 Help & Alerts"  # answered with what the bot can do, then the /alerts status
IRRIGATION = "\U0001f331 Irrigation"     # answered with the irrigation controller's status and buttons (only when it is configured)

# Every button but Help & Alerts and Status is a chart of the last day (the period buttons under it change that)
ROWS = [[(CAPABILITIES, "What can you do?"), ("\U0001f4cb Status", "Report"), ("\U0001f327️ Rain", "Rain chart 1d")],
        [("\U0001f326️ Weather", "Weather chart 1d"), ("\U0001f4c8 Temperature", "Temperature chart 1d"),
         ("\U0001f4a7 Humidity", "Humidity chart 1d")],
        [("\U0001f3ed Air Quality", "Air quality all metrics 1d"), ("\U0001f52c Particulates", "Particulates chart 1d"),
         ("\U0001f4a8 Wind", "Wind chart 1d")]]
SENTENCES = dict(button for row in ROWS for button in row)
# The command menu Telegram shows beside the message box (the "/" button): command, description
MENU = [("report", "Weather, air quality and more in one message"), ("dashboard", "Gauges with today's, this month's and this year's extremes"),
        ("rain", "Rain chart"), ("weather", "Every reading, a chart each"),
        ("temperature", "Temperature chart"), ("humidity", "Humidity chart"), ("air_quality", "Every air quality metric"),
        ("particulates", "PM1, PM2.5 and PM10 chart"), ("wind", "Wind chart"), ("alerts", "Alert settings"),
        ("usage", "What I can do"), ("keyboard", "Show or hide the buttons"), ("reset", "Clear this chat's memory")]
# The same questions as commands, named as their buttons are ("📋 Status" is /status, "🏭 Air Quality" is /air_quality), and /report for Status
COMMANDS = {"report": "Report", "dashboard": "Dashboard", **{label.split(" ", 1)[1].lower().replace(" ", "_"): question
                                   for label, question in SENTENCES.items() if label != CAPABILITIES}}
# Telegram keeps the keyboard in each app until a message brings a new one; this changes whenever the buttons do, so a chat
# whose stored value differs gets the current keyboard with its next reply. HIDDEN is what a chat that hid the buttons stores.
HIDDEN = "hidden"


def rows(irrigation: bool = False) -> list[list[tuple[str, str]]]:
    """The button rows. With the irrigation controller configured, its button takes the place of Wind (the last one)."""
    return [*ROWS[:-1], [*ROWS[-1][:-1], (IRRIGATION, "")]] if irrigation else ROWS


def version(irrigation: bool = False) -> str:
    return hashlib.md5("|".join(label for row in rows(irrigation) for label, _ in row).encode()).hexdigest()[:8]


def keyboard(irrigation: bool = False) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[label for label, _ in row] for row in rows(irrigation)], resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Ask about the weather or air…")


def sentence(text: str) -> str | None:
    """The question a button label stands for; None for anything else typed."""
    return SENTENCES.get(text.strip())


MEASURES = {   # a source (by name) -> what it measures, as the Capabilities button lists it
    "Ecowitt": ["Temperature, humidity (indoor, outdoor)", "Dew point, vapour pressure deficit, pressure, wind speed",
                "Rain, solar radiation, UV index"],
    "AirGradient": ["PM1, PM2.5, PM10, CO₂, VOC, NOx"],
    "Pollen": ["Pollen and thunderstorm asthma risk (October to December)"],
    "Forecast": ["Forecast"],
}


def capabilities_text(sources: Collection[str] = ("Ecowitt", "AirGradient")) -> str:
    """What the bot measures with these sources, as a short bulleted list (no dates, no detail): the answer to the Capabilities button."""
    lines = [line for name, measured in MEASURES.items() if name in sources for line in measured] + ["History charts"]
    return "\n".join(f"\u2022 {line}" for line in lines)
