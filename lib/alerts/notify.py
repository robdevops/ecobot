"""Who gets alerts, and sending them.

Alerts go to every chat the bot knows about (Telegram can't list a bot's chats, so they are
remembered as messages arrive) unless it unsubscribed (per alert type, with the buttons under each alert or /alerts). A chat that
blocked or removed the bot is marked gone: it gets nothing, and has its settings back when it next writes. Chats and
monitor state live in a small JSON file, so restarts neither forget chats nor repeat alerts. Alerts are sent silently, with the
settings buttons and, when there is one, a small italic footer with a link label.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Collection
from datetime import date, timedelta

from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

from ..entities import with_link
from ..logs import QUOTES
from .menu import ALL, LABELS, alert_keyboard
from .pictures import CHARTS

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
        """A message from this chat (or the bot added to it): known from now on, and back if it was gone."""
        entry = self.chats.get(chat_id)
        if entry is None:
            self.chats[chat_id] = {"title": title}
            log.info("Alerts: now sending to %s (id %s)", title, chat_id)
            self.save()
        elif entry.get("title") != title or entry.get("gone"):
            if entry.pop("gone", None):
                log.info("Alerts: sending to %s (id %s) again", title, chat_id)
            entry["title"] = title
            self.save()

    def mark_gone(self, chat_id: int, reason: str):
        """The bot can't post to this chat any more (it was blocked or removed): nothing is sent to it, and its settings are kept
        for when it writes again."""
        entry = self.chats.get(chat_id)
        if entry is not None and not entry.get("gone"):
            entry["gone"] = True
            log.info("Alerts: no longer sending to chat %s (%s)", chat_id, reason)
            self.save()

    def present(self) -> list[int]:
        """The chats the bot is in: known, and not gone."""
        return [c for c, entry in self.chats.items() if not entry.get("gone")]

    def set_alerts(self, chat_id: int, on: bool):
        """/alerts on and /alerts off: every type."""
        self.set_kind(chat_id, ALL, on)

    def migrate_chat(self, old: int, new: int):
        """A group became a supergroup: its settings, and the forecast it was sent, move to the new chat id."""
        entry = self.chats.pop(old, None)
        if entry is not None:
            self.chats.setdefault(new, entry)
            sent = self.monitor.get("forecast_sent", {})
            if str(old) in sent:
                sent.setdefault(str(new), sent.pop(str(old)))
            log.info("Alerts: chat %s became %s", old, new)
            self.save()

    def muted(self, chat_id: int) -> set[str]:
        """The types this chat does not get. A chat that unsubscribed from all is in opt-in mode (it keeps the types it turned on),
        so a type added later is muted for it until it subscribes."""
        entry = self.chats.get(chat_id, {})
        if entry.get("optin"):
            return set(LABELS) - set(entry.get("on", []))
        return set(entry.get("muted", []))

    def set_kind(self, chat_id: int, kind: str, on: bool, offered: Collection[str] = LABELS):
        """Subscribe (on) or unsubscribe one alert type, or all of them (kind = "all"). Unsubscribing from all also keeps
        future types off for the chat, and so does unsubscribing one by one from every type `offered` (the types the bot
        sends); subscribing to all turns them on again."""
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
        if not on and muted >= set(offered):   # the last type muted: the same as unsubscribing from all
            entry.pop("muted", None)
            entry.update(optin=True, on=[])
        elif muted:
            entry["muted"] = [k for k in LABELS if k in muted]
        else:
            entry.pop("muted", None)
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

    def subscribed(self, chat_id: int, kind: str) -> bool:
        """Does this chat get this alert type?"""
        return chat_id in self.chats and not self.chats[chat_id].get("gone") and kind not in self.muted(chat_id)

    def alert_chats(self, kind: str) -> list[int]:
        """The chats that get alerts of this type."""
        return [c for c in self.chats if self.subscribed(c, kind)]


class Notifier:
    def __init__(self, bot, state: AlertState):
        self.bot, self.state = bot, state

    async def __call__(self, text: str, kind: str, link: tuple[str, str] | None = None,
                       chart: Callable[[], Awaitable[bytes | None]] | None = None, chats: list[int] | None = None) -> int:
        """Send an alert (silently) to the chats subscribed to its type (`kind`, one of menu.TYPES): of `chats` (say, the one sent
        something that has since changed), else of every chat. Chats the bot can no longer post to are marked gone. Returns how many it was sent to.
        link = (label, url) adds a clickable label to the footer; each carries the settings buttons. chart draws the alert's
        picture (None: the text alone), and is called only when some chat gets the alert. The text is the picture's caption
        (the text alone if Telegram refuses it); it is uploaded once and the chats after the first are sent Telegram's copy."""
        targets = self.state.alert_chats(kind) if chats is None else [c for c in chats if self.state.subscribed(c, kind)]
        text, entities = with_link(text, link)
        photo = await chart() if chart and targets else None
        started, sent = time.monotonic(), 0
        for chat_id in targets:
            ok, message = await self._send(chat_id, text, entities, kind, photo)
            sent += ok
            if photo and (sizes := getattr(message, "photo", None)):
                photo = sizes[-1].file_id   # the largest size: the picture as sent
        log.info("Alert sent to %d chat(s) in %.1fs: %s", sent, time.monotonic() - started, text, extra=QUOTES)
        return sent

    async def _post(self, chat_id: int, text: str, entities, kind: str, photo: bytes | str | None):
        """The message itself (returned): silent unless the chat turned sound on for this type, with the alert type's settings button (the
        bell). With a photo it is the picture with the text as its caption, and a Refresh that draws the chart again; a picture Telegram
        refuses falls back to the text alone."""
        quiet = kind not in self.state.loud(chat_id)
        markup = lambda refresh: alert_keyboard(kind, self.state.muted(chat_id), self.state.loud(chat_id) if chat_id > 0 else None, refresh=refresh)
        if photo:
            try:
                return await self.bot.send_photo(chat_id, photo, caption=text, caption_entities=entities, disable_notification=quiet,
                                                 reply_markup=markup(kind in CHARTS))
            except BadRequest as e:
                if "chat not found" in str(e).lower():
                    raise
                log.warning("Alert picture to %s failed (%s); sending the text alone", chat_id, e)
        return await self.bot.send_message(chat_id, text, entities=entities, disable_web_page_preview=True, disable_notification=quiet,
                                           reply_markup=markup(False))

    async def _send(self, chat_id: int, text: str, entities, kind: str, photo: bytes | str | None = None, again: bool = True) -> tuple[bool, object]:
        """Send an alert (see _post) to one chat; a chat the bot can no longer post to is marked gone. Telegram's flood limit (when it asks
        for a short wait) and a dropped connection get one more try; a timeout doesn't, as the message may have arrived. Whatever
        fails for one chat is logged, so the others still get the alert. (whether it was sent, the message sent)"""
        try:
            try:
                return True, await self._post(chat_id, text, entities, kind, photo)
            except ChatMigrated as e:  # the group became a supergroup: follow it, and send again there
                self.state.migrate_chat(chat_id, e.new_chat_id)
                return await self._send(e.new_chat_id, text, entities, kind, photo) if e.new_chat_id != chat_id else (False, None)
            except Forbidden as e:  # kicked from the group, or blocked in a private chat
                self.state.mark_gone(chat_id, f"can't post: {e}")
            except BadRequest as e:
                if "chat not found" not in str(e).lower():   # not "message thread not found", say: that chat is still there
                    raise
                self.state.mark_gone(chat_id, f"chat gone: {e}")
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
