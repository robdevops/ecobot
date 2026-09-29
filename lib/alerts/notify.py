"""Who gets alerts, and sending them.

Alerts go to every chat the bot knows about (Telegram can't list a bot's chats, so they are
remembered as messages arrive) unless it opted out with /alerts off. Chats and monitor state
live in a small JSON file, so restarts neither forget chats nor repeat alerts. Alerts are
sent silently, with a small italic footer: optional link label, then how to mute.
"""

import json
import logging
import os

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
        sent = 0
        for chat_id in self.state.alert_chats():
            try:
                await self.bot.send_message(chat_id, text, entities=entities, disable_notification=True,
                                            disable_web_page_preview=True)
                sent += 1
            except Forbidden as e:  # kicked from the group, or blocked in a private chat
                self.state.remove_chat(chat_id, f"can't post: {e}")
            except BadRequest as e:
                if "not found" in str(e).lower():
                    self.state.remove_chat(chat_id, f"chat gone: {e}")
                else:
                    log.warning("Alert to %s failed: %s", chat_id, e)
            except TelegramError as e:
                log.warning("Alert to %s failed: %s", chat_id, e)
        log.info("Alert sent to %d chat(s): %s", sent, text.replace("\n", " "))
