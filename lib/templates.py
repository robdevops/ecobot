"""The buttons under the message box in private chats (a persistent reply keyboard), and the bot's own commands. A button sends its
label as an ordinary message; the bot answers it at once, without the model: each chart button is a fixed call to ecobot-mcp
(CALLS: summary of a named reading over the last day, which ecobot-mcp draws), Status is the gauges and the `report` prompt, and Help &
Alerts is ecobot-mcp's `about` and the alert settings. The label stands for a plain sentence (its words without the emoji): what the
chat's history and the log record."""

import copy
import hashlib
from collections.abc import Collection

CAPABILITIES = "\U0001f514 Help & Alerts"  # answered with what the bot can do, then the /alerts status
IRRIGATION = "\U0001f331 Irrigation"     # answered with the irrigation controller's status and buttons (only with irrigation-mcp: BOT_IRRIGATION_MCP_URL)
STATUS = "Status"                         # the Status button's sentence: the gauges, then the `report` prompt's answer (Bot.status)

# Every button but Help & Alerts and Status is a chart of the last day (the period buttons under it change that)
ROWS = [[(CAPABILITIES, "What can you do?"), ("\U0001f4cb Status", STATUS), ("\U0001f327️ Rain", "Rain chart 1d")],
        [("\U0001f326️ Weather", "Weather chart 1d"), ("\U0001f4c8 Temperature", "Temperature chart 1d"),
         ("\U0001f4a7 Humidity", "Humidity chart 1d")],
        [("\U0001f3ed Air Quality", "Air quality all metrics 1d"), ("\U0001f52c Particulates", "Particulates chart 1d"),
         ("\U0001f4a8 Wind", "Wind chart 1d")]]
SENTENCES = dict(button for row in ROWS for button in row)


def _day_chart(*readings: str) -> tuple[str, dict]:
    """summary of these readings (the service's words) over the last day, as a chart."""
    return "summary", {"readings": list(readings), "period": "1d", "chart": True}


# Each chart button's sentence -> the fixed call to ecobot-mcp it is (the alerts' charts are some of the same: lib/alerts/pictures.py)
CALLS = {"Rain chart 1d": _day_chart("rain"), "Weather chart 1d": _day_chart("weather"),
         "Temperature chart 1d": _day_chart("temperature"), "Humidity chart 1d": _day_chart("humidity"),
         "Air quality all metrics 1d": _day_chart("air"), "Particulates chart 1d": _day_chart("pm2.5", "pm10", "pm1"),
         "Wind chart 1d": _day_chart("wind")}
# The same buttons as commands, named as their buttons are ("📋 Status" is /status, "🏭 Air Quality" is /air_quality)
COMMANDS = {label.split(" ", 1)[1].lower().replace(" ", "_"): question for label, question in SENTENCES.items() if label != CAPABILITIES}
# The bot's own command menu (the "/" button beside the message box): command, description. ecobot-mcp's prompts join it, a command
# each (Bot.publish_commands); none may take one of these names
MENU = [("status", "The gauges and a report"), ("rain", "Rain chart"), ("weather", "Every reading, a chart each"),
        ("temperature", "Temperature chart"), ("humidity", "Humidity chart"), ("air_quality", "Every air quality metric"),
        ("particulates", "PM1, PM2.5 and PM10 chart"), ("wind", "Wind chart"), ("alerts", "Alert settings"),
        ("usage", "What I can do"), ("keyboard", "Show or hide the buttons"), ("reset", "Clear this chat's memory")]
OWN_COMMANDS = {"start", "help", *(name for name, _ in MENU)}   # every command the bot handles itself
# Telegram keeps the keyboard in each app until a message brings a new one; this changes whenever the buttons do, so a chat
# whose stored value differs gets the current keyboard with its next reply. HIDDEN is what a chat that hid the buttons stores.
HIDDEN = "hidden"


def rows(irrigation: bool = False) -> list[list[tuple[str, str]]]:
    """The button rows. With irrigation-mcp (BOT_IRRIGATION_MCP_URL), its button takes the place of Wind (the last one)."""
    return [*ROWS[:-1], [*ROWS[-1][:-1], (IRRIGATION, "")]] if irrigation else ROWS


def version(irrigation: bool = False) -> str:
    return hashlib.md5("|".join(label for row in rows(irrigation) for label, _ in row).encode()).hexdigest()[:8]


def keyboard(irrigation: bool = False):
    from telegram import ReplyKeyboardMarkup   # (here, not at the top: ecobot-mcp's alert rules read CALLS, and know no Telegram)
    return ReplyKeyboardMarkup([[label for label, _ in row] for row in rows(irrigation)], resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Ask about the weather or air…")


def sentence(text: str) -> str | None:
    """The sentence a button label stands for; None for anything else typed."""
    return SENTENCES.get(text.strip())


def call(sentence: str | None) -> tuple[str, dict] | None:
    """The fixed call a chart button's sentence is (a copy, so the caller may keep it); None for any other."""
    found = CALLS.get(sentence) if sentence else None
    return (found[0], copy.deepcopy(found[1])) if found else None


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
