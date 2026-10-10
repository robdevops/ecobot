"""Who gets alerts, and sending them.

Alerts go to every chat the bot knows about (Telegram can't list a bot's chats, so they are
remembered as messages arrive) unless it unsubscribed (per alert type, with the buttons under each alert or /alerts). Chats and
monitor state live in a small JSON file, so restarts neither forget chats nor repeat alerts. Alerts are sent silently, with the
settings buttons and, when there is one, a small italic footer with a link label.
"""

import json
import logging
import os
from datetime import date

from telegram import MessageEntity
from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError

from .menu import ALL, LABELS, keyboard

log = logging.getLogger(__name__)


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
        for entry in self.chats.values():   # the old on/off flag, and a chat with every type muted, are "unsubscribed from all"
            if entry.pop("alerts", True) is False or set(entry.get("muted", [])) >= set(LABELS):
                entry.pop("muted", None)
                entry.update(optin=True, on=[])
        self.monitor: dict = data.get("monitor", {})
        self.charts: dict[str, str] = data.get("charts", {})   # the question behind each chart sent, for its period buttons

    def save(self):
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"chats": {str(k): v for k, v in self.chats.items()}, "monitor": self.monitor, "charts": self.charts}, f, indent=1)
        os.replace(tmp, self.path)

    def add_chat(self, chat_id: int, title: str):
        entry = self.chats.get(chat_id)
        if entry is None:
            self.chats[chat_id] = {"title": title}
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
        """/alerts on and /alerts off: every type."""
        self.chats.setdefault(chat_id, {"title": title})
        self.set_kind(chat_id, ALL, on)

    def migrate_chat(self, old: int, new: int):
        """A group became a supergroup: its settings move to the new chat id."""
        entry = self.chats.pop(old, None)
        if entry is not None:
            self.chats.setdefault(new, entry)
            log.info("Alerts: chat %s became %s", old, new)
            self.save()

    def muted(self, chat_id: int) -> set[str]:
        """The types this chat does not get. A chat that unsubscribed from all is in opt-in mode (it keeps the types it turned on),
        so a type added later is muted for it until it subscribes."""
        entry = self.chats.get(chat_id, {})
        if entry.get("optin"):
            return set(LABELS) - set(entry.get("on", []))
        return set(entry.get("muted", []))

    def set_kind(self, chat_id: int, kind: str, on: bool):
        """Subscribe (on) or unsubscribe one alert type, or all of them (kind = "all"). Unsubscribing from all also keeps
        future types off for the chat; subscribing to all turns them on again."""
        if chat_id not in self.chats:
            return
        entry = self.chats[chat_id]
        if kind == ALL:   # on: back to the normal mode (new types come on); off: opt-in mode, where new types stay off
            for key in ("muted", "optin", "on"):
                entry.pop(key, None)
            if not on:
                entry.update(optin=True, on=[])
            self.save()
            return
        kinds = {kind} & set(LABELS)
        if entry.get("optin"):
            turned_on = set(entry.get("on", [])) | kinds if on else set(entry.get("on", [])) - kinds
            entry["on"] = [k for k in LABELS if k in turned_on]
            self.save()
            return
        muted = self.muted(chat_id)
        muted = muted - kinds if on else muted | kinds
        if muted:
            self.chats[chat_id]["muted"] = [k for k in LABELS if k in muted]
        else:
            self.chats[chat_id].pop("muted", None)
        self.save()

    def loud(self, chat_id: int) -> set[str]:
        """The alert types this chat gets with notification sound (alerts are silent unless a chat turned the sound on)."""
        return set(self.chats.get(chat_id, {}).get("loud", []))

    def set_sound(self, chat_id: int, kind: str, on: bool):
        """Notification sound on (on) or off for one alert type in a known chat."""
        if chat_id not in self.chats or kind not in LABELS:
            return
        loud = self.loud(chat_id)
        loud = loud | {kind} if on else loud - {kind}
        if loud:
            self.chats[chat_id]["loud"] = [k for k in LABELS if k in loud]
        else:
            self.chats[chat_id].pop("loud", None)
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

    def subscribed(self, chat_id: int, kind: str | None = None) -> bool:
        """Does this chat get this alert type (or, with no kind, any at all)?"""
        if chat_id not in self.chats:
            return False
        muted = self.muted(chat_id)
        return kind not in muted if kind else bool(set(LABELS) - muted)

    def alerts_on(self, chat_id: int) -> bool:
        return self.subscribed(chat_id)

    def alert_chats(self, kind: str | None = None) -> list[int]:
        """The chats that get alerts of this type (any type, if none is given)."""
        return [c for c in self.chats if self.subscribed(c, kind)]


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
    def __init__(self, bot, state: AlertState, kinds: list[str] | None = None):
        """kinds: the alert types this bot sends (the settings buttons under each alert list them)."""
        self.bot, self.state, self.kinds = bot, state, kinds or list(LABELS)

    async def __call__(self, text: str, link: tuple[str, str] | None = None, kind: str | None = None):
        """Send an alert (silently) to every chat subscribed to its type; forget chats the bot can no
        longer post to. link = (label, url) adds a clickable label to the footer. Each carries the settings buttons."""
        text, entities = with_footer(text, link)
        sent = sum([await self._send(chat_id, text, entities, kind) for chat_id in self.state.alert_chats(kind)])
        log.info("Alert sent to %d chat(s): %s", sent, text.replace("\n", " "))

    async def to_chat(self, chat_id: int, text: str, link: tuple[str, str] | None = None, kind: str | None = None) -> bool:
        """An alert for one chat (the one that was sent something that has since changed), if it is subscribed to the type.
        True if it was sent."""
        if not self.state.subscribed(chat_id, kind):
            return False
        text, entities = with_footer(text, link)
        sent = await self._send(chat_id, text, entities, kind)
        log.info("Alert %s chat %s: %s", "sent to" if sent else "NOT sent to", chat_id, text.replace("\n", " "))
        return sent

    async def _send(self, chat_id: int, text: str, entities, kind: str | None = None) -> bool:
        """Send silently, with the settings buttons (which highlight this alert's own type); with sound only if the chat turned it on for this type; forget a chat the bot can no longer post to."""
        try:
            await self.bot.send_message(chat_id, text, entities=entities, disable_notification=kind not in self.state.loud(chat_id),
                                        disable_web_page_preview=True,
                                        reply_markup=keyboard(self.state.muted(chat_id), self.kinds, parent=kind, loud=self.state.loud(chat_id) if chat_id > 0 else None))
            return True
        except ChatMigrated as e:  # the group became a supergroup: follow it, and send again there
            self.state.migrate_chat(chat_id, e.new_chat_id)
            return await self._send(e.new_chat_id, text, entities, kind) if e.new_chat_id != chat_id else False
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
