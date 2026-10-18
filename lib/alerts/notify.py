"""Who gets alerts, and sending them.

Alerts go to every chat the bot knows about (Telegram can't list a bot's chats, so they are
remembered as messages arrive) unless it unsubscribed (per alert type, with the buttons under each alert or /alerts). Chats and
monitor state live in a small JSON file, so restarts neither forget chats nor repeat alerts. Alerts are sent silently, with the
settings buttons and, when there is one, a small italic footer with a link label.
"""

import asyncio
import json
import logging
import os
import time
from datetime import date, timedelta

from telegram import MessageEntity
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

from ..logs import QUOTES
from .menu import ALL, LABELS, alert_keyboard

log = logging.getLogger(__name__)

RETRY_SECONDS = 3       # before sending an alert again after a dropped connection
FLOOD_WAIT_MAX = 60     # Telegram's flood limit asks to wait: a longer wait is not waited out (that chat misses the alert)


def _load(path) -> dict:
    """The saved state, {} when there is none. A file that can't be read is moved aside, as the next save would replace it,
    so its chats and settings can still be recovered by hand."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        aside = f"{path}.damaged-{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(path, aside)
        log.error("Couldn't read %s (%s): moved it to %s and starting with no known chats", path, e, aside)
        return {}


def _flood_wait(e: RetryAfter) -> float:
    """The seconds Telegram asks to wait (python-telegram-bot gives a number, or a timedelta once PTB_TIMEDELTA is set)."""
    wait = e.retry_after
    return wait.total_seconds() if isinstance(wait, timedelta) else wait


class AlertState:
    """Known chats (with alerts on/off) and monitor state, saved to a JSON file."""

    def __init__(self, path):
        self.path = path
        data = _load(path)
        self.chats: dict[int, dict] = {int(k): v for k, v in data.get("chats", {}).items()}
        for entry in self.chats.values():   # the old on/off flag, and a chat with every type muted, are "unsubscribed from all"
            if entry.pop("alerts", True) is False or set(entry.get("muted", [])) >= set(LABELS):
                entry.pop("muted", None)
                entry.update(optin=True, on=[])
        self.monitor: dict = data.get("monitor", {})
        self.charts: dict[str, str] = data.get("charts", {})   # the question behind each chart sent, for its period buttons

    def save(self):
        """Written in full to a new file that then replaces the old one, on disk first, so a crash leaves one or the other whole."""
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"chats": {str(k): v for k, v in self.chats.items()}, "monitor": self.monitor, "charts": self.charts}, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
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

    def record_forecast(self, chat_id: int, days: list[dict], today: date):
        """Remember the forecast days this chat was just sent (a newer send replaces the baseline for the same day, the other
        days keep theirs); days before today are dropped."""
        sent = self.monitor.setdefault("forecast_sent", {}).setdefault(str(chat_id), {})
        for d in days:
            sent[d["date"].isoformat()] = {**d, "date": d["date"].isoformat()}
        for day in [k for k in sent if k < today.isoformat()]:
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
    def __init__(self, bot, state: AlertState):
        self.bot, self.state = bot, state

    async def __call__(self, text: str, link: tuple[str, str] | None = None, kind: str | None = None, photo: bytes | str | None = None):
        """Send an alert (silently) to every chat subscribed to its type; forget chats the bot can no
        longer post to. link = (label, url) adds a clickable label to the footer. Each carries the settings buttons.
        photo: a picture the text is the caption of (the text alone if Telegram refuses the picture): a PNG, or the file id of one
        Telegram holds. It is uploaded once: the chats after the first are sent Telegram's copy."""
        text, entities = with_footer(text, link)
        started, sent = time.monotonic(), 0
        for chat_id in self.state.alert_chats(kind):
            ok, message = await self._send(chat_id, text, entities, kind, photo)
            sent += ok
            if photo and (sizes := getattr(message, "photo", None)):
                photo = sizes[-1].file_id   # the largest size: the picture as sent
        log.info("Alert sent to %d chat(s) in %.1fs: %s", sent, time.monotonic() - started, text, extra=QUOTES)

    async def to_chat(self, chat_id: int, text: str, link: tuple[str, str] | None = None, kind: str | None = None) -> bool:
        """An alert for one chat (the one that was sent something that has since changed), if it is subscribed to the type.
        True if it was sent."""
        if not self.state.subscribed(chat_id, kind):
            return False
        text, entities = with_footer(text, link)
        sent, _ = await self._send(chat_id, text, entities, kind)
        log.info("Alert %s chat %s: %s", "sent to" if sent else "NOT sent to", chat_id, text, extra=QUOTES)
        return sent

    async def _post(self, chat_id: int, text: str, entities, kind: str | None, photo: bytes | str | None):
        """The message itself (returned): silent unless the chat turned sound on for this type, with the alert type's settings button (the bell). With a photo it is the picture with the text as its caption; a picture Telegram refuses falls back to the text alone."""
        common = dict(disable_notification=kind not in self.state.loud(chat_id),
                      reply_markup=alert_keyboard(kind, self.state.muted(chat_id), self.state.loud(chat_id) if chat_id > 0 else None) if kind else None)
        if photo:
            try:
                return await self.bot.send_photo(chat_id, photo, caption=text, caption_entities=entities, **common)
            except BadRequest as e:
                if "not found" in str(e).lower():
                    raise
                log.warning("Alert picture to %s failed (%s); sending the text alone", chat_id, e)
        return await self.bot.send_message(chat_id, text, entities=entities, disable_web_page_preview=True, **common)

    async def _send(self, chat_id: int, text: str, entities, kind: str | None = None, photo: bytes | str | None = None,
                    again: bool = True) -> tuple[bool, object]:
        """Send an alert (see _post) to one chat; forget a chat the bot can no longer post to. Telegram's flood limit (when it asks
        for a short wait) and a dropped connection get one more try; a timeout doesn't, as the message may have arrived. Whatever
        fails for one chat is logged, so the others still get the alert. (whether it was sent, the message sent)"""
        try:
            try:
                return True, await self._post(chat_id, text, entities, kind, photo)
            except ChatMigrated as e:  # the group became a supergroup: follow it, and send again there
                self.state.migrate_chat(chat_id, e.new_chat_id)
                return await self._send(e.new_chat_id, text, entities, kind, photo) if e.new_chat_id != chat_id else (False, None)
            except Forbidden as e:  # kicked from the group, or blocked in a private chat
                self.state.remove_chat(chat_id, f"can't post: {e}")
            except BadRequest as e:
                if "not found" not in str(e).lower():
                    raise
                self.state.remove_chat(chat_id, f"chat gone: {e}")
            except (RetryAfter, NetworkError) as e:   # BadRequest, a NetworkError too, is caught above
                wait = _flood_wait(e) if isinstance(e, RetryAfter) else RETRY_SECONDS
                if not again or isinstance(e, TimedOut) or wait > FLOOD_WAIT_MAX:
                    raise
                log.info("Alert to %s: %s; trying again in %gs", chat_id, e, wait)
                await asyncio.sleep(wait)
                return await self._send(chat_id, text, entities, kind, photo, again=False)
        except Exception as e:   # Telegram refused it, or the state file couldn't be saved
            log.warning("Alert to %s failed: %s", chat_id, e, exc_info=not isinstance(e, TelegramError))
        return False, None
