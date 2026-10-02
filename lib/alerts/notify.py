"""Who gets alerts, and sending them.

Alerts go to every chat the bot knows about (Telegram can't list a bot's chats, so they are
remembered as messages arrive) unless it opted out with /alerts off. Chats and monitor state
live in a small JSON file, so restarts neither forget chats nor repeat alerts. Alerts are
sent silently, with a small italic footer: optional link label, then how to mute.
"""

import json
import logging
import os
from datetime import date

from telegram import MessageEntity
from telegram.error import BadRequest, Forbidden, TelegramError

log = logging.getLogger(__name__)

OPT_OUT = "/alerts off to mute"


class AlertState:
    """Known chats (with alerts on/off) and monitor state, saved to a JSON file."""

    def __init__(self, path):
        self.path = path
        data: dict = {}
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Couldn't read %s - starting with no known chats", path)
        self.chats: dict[int, dict] = {int(k): v for k, v in data.get("chats", {}).items()}
        self.monitor: dict = data.get("monitor", {})

    def save(self):
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"chats": {str(k): v for k, v in self.chats.items()}, "monitor": self.monitor}, f, indent=1)
        os.replace(tmp, self.path)

    def add_chat(self, chat_id: int, title: str):
        entry = self.chats.get(chat_id)
        if entry is None:
            self.chats[chat_id] = {"title": title, "alerts": True}
            log.info("Alerts: now sending to %s (id %s)", title, chat_id)
            self.save()
        elif entry.get("title") != title:
            entry["title"] = title
            self.save()

    def remove_chat(self, chat_id: int, reason: str):
        if self.chats.pop(chat_id, None) is not None:
            log.info("Alerts: no longer sending to chat %s (%s)", chat_id, reason)
            self.save()

    def set_alerts(self, chat_id: int, title: str, on: bool):
        self.chats.setdefault(chat_id, {"title": title})["alerts"] = on
        self.save()

    def keyboard(self, chat_id: int) -> str | None:
        """The version of the button keyboard this chat was last sent (or "hidden" if it hid it)."""
        return self.chats.get(chat_id, {}).get("keyboard")

    def set_keyboard(self, chat_id: int, version: str):
        if chat_id in self.chats and self.chats[chat_id].get("keyboard") != version:
            self.chats[chat_id]["keyboard"] = version
            self.save()

    def record_forecast(self, chat_id: int, days: list[dict]):
        """Remember the forecast days this chat was just sent (a newer send replaces the baseline for the same day); days
        already past are dropped. days is not empty."""
        sent = self.monitor.setdefault("forecast_sent", {}).setdefault(str(chat_id), {})
        for d in days:
            sent[d["date"].isoformat()] = {**d, "date": d["date"].isoformat()}
        first = min(d["date"] for d in days).isoformat()   # what was sent starts today: older days are past
        for day in [k for k in sent if k < first]:
            del sent[day]
        self.save()

    def forecast_sent(self, chat_id: int) -> dict:
        """{date: the day as it was sent} for this chat."""
        return {date.fromisoformat(k): {**v, "date": date.fromisoformat(k)}
                for k, v in self.monitor.get("forecast_sent", {}).get(str(chat_id), {}).items()}

    def forecast_chats(self) -> list[int]:
        return [int(c) for c in self.monitor.get("forecast_sent", {})]

    def alerts_on(self, chat_id: int) -> bool:
        return self.chats.get(chat_id, {}).get("alerts", True)

    def alert_chats(self) -> list[int]:
        return [c for c in self.chats if self.alerts_on(c)]


def _utf16_len(text: str) -> int:
    """Telegram measures entity positions in UTF-16 code units (emoji count as 2)."""
    return len(text.encode("utf-16-le")) // 2


def with_footer(text: str, link: tuple[str, str] | None = None, extra: str | None = None) -> tuple[str, list]:
    """Add a small italic footer line directly under the text: the link's label (clickable),
    then any extra text, joined by " · ". Plain text stays plain (no HTML to escape);
    only the footer is formatted. link = (label, url)."""
    text = text.rstrip()
    parts = ([link[0]] if link else []) + ([extra] if extra else [])
    if not parts:
        return text, []
    start = _utf16_len(text) + 1
    footer = " · ".join(parts)
    entities = [MessageEntity(MessageEntity.ITALIC, start, _utf16_len(footer))]
    if link:
        entities.append(MessageEntity(MessageEntity.TEXT_LINK, start, _utf16_len(link[0]), url=link[1]))
    return f"{text}\n{footer}", entities


class Notifier:
    def __init__(self, bot, state: AlertState):
        self.bot, self.state = bot, state

    async def __call__(self, text: str, link: tuple[str, str] | None = None):
        """Send an alert (silently) to every chat with alerts on; forget chats the bot can no
        longer post to. link = (label, url) adds a clickable label to the footer."""
        text, entities = with_footer(text, link, OPT_OUT)
        sent = sum([await self._send(chat_id, text, entities) for chat_id in self.state.alert_chats()])
        log.info("Alert sent to %d chat(s): %s", sent, text.replace("\n", " "))

    async def to_chat(self, chat_id: int, text: str, link: tuple[str, str] | None = None) -> bool:
        """An alert for one chat (the one that was sent something that has since changed). True if it was sent."""
        text, entities = with_footer(text, link, OPT_OUT)
        sent = await self._send(chat_id, text, entities)
        log.info("Alert %s chat %s: %s", "sent to" if sent else "NOT sent to", chat_id, text.replace("\n", " "))
        return sent

    async def _send(self, chat_id: int, text: str, entities) -> bool:
        """Send silently; forget a chat the bot can no longer post to."""
        try:
            await self.bot.send_message(chat_id, text, entities=entities, disable_notification=True, disable_web_page_preview=True)
            return True
        except Forbidden as e:  # kicked from the group, or blocked in a private chat
            self.state.remove_chat(chat_id, f"can't post: {e}")
        except BadRequest as e:
            if "not found" in str(e).lower():
                self.state.remove_chat(chat_id, f"chat gone: {e}")
            else:
                log.warning("Alert to %s failed: %s", chat_id, e)
        except TelegramError as e:
            log.warning("Alert to %s failed: %s", chat_id, e)
        return False
