"""Who gets alerts, and sending them.

Alerts go to every chat the bot knows about (Telegram can't list a bot's chats, so they are
remembered as messages arrive) unless it unsubscribed (per alert type, with the buttons under each alert or /alerts). A chat that
blocked or removed the bot is marked gone: it gets nothing, and has its settings back when it next writes. Chats, their
settings and the last alert delivered live in a small JSON file (bot_state.json), so restarts neither forget chats nor repeat alerts.
Alerts are sent silently, with the settings buttons and, when there is one, a small italic footer with a link label.

The alerts themselves are the service's: its rules append them to the alert log (lib/alert_log.py), and AlertDelivery sends each to the
chats subscribed to its kind. Only the irrigation controller's are still sent here directly (lib/alerts/irrigation.py), until it is a
server of its own.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Collection
from datetime import timedelta

from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

from ..entities import with_link
from ..logs import QUOTES
from .menu import ALL, LABELS, alert_keyboard
from .pictures import CHARTS, AlertCharts, Picture, picture_of
from .store import load, write

log = logging.getLogger(__name__)

RETRY_SECONDS = 3       # before sending an alert again after a dropped connection
FLOOD_WAIT_MAX = 60     # Telegram's flood limit asks to wait: a longer wait is not waited out (that chat misses the alert)


def _flood_wait(e: RetryAfter) -> float:
    """The seconds Telegram asks to wait (python-telegram-bot gives a number, or a timedelta once PTB_TIMEDELTA is set)."""
    wait = e.retry_after
    return wait.total_seconds() if isinstance(wait, timedelta) else wait


class AlertState:
    """Known chats (with alerts on/off), the irrigation controller's state and the last alert delivered, saved to a JSON file
    (bot_state.json). The alert rules keep theirs apart (lib/alerts/store.py: alert_state.json)."""

    def __init__(self, path):
        self.path = path
        data = load(path) or {}
        self.chats: dict[int, dict] = {int(k): v for k, v in data.get("chats", {}).items()}
        self.monitor: dict = data.get("monitor", {})   # the irrigation controller's (until it is a server of its own)
        self.charts: dict[str, str] = data.get("charts", {})   # the question behind each chart sent, for its period buttons
        self.read: int | None = data.get("alerts_read")       # the id of the last alert in the log the bot handled (AlertDelivery)

    def save(self):
        """Written in full to a new file that then replaces the old one, on disk first, so a crash leaves one or the other whole."""
        write(self.path, {"chats": {str(k): v for k, v in self.chats.items()}, "monitor": self.monitor, "charts": self.charts,
                          **({"alerts_read": self.read} if self.read is not None else {})})

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

    def subscribed(self, chat_id: int, kind: str) -> bool:
        """Does this chat get this alert type?"""
        return chat_id in self.chats and not self.chats[chat_id].get("gone") and kind not in self.muted(chat_id)

    def alert_chats(self, kind: str) -> list[int]:
        """The chats that get alerts of this type."""
        return [c for c in self.chats if self.subscribed(c, kind)]


class Notifier:
    def __init__(self, bot, state: AlertState, charts: AlertCharts | None = None):
        """charts: the bot's alert charts (AlertCharts), when the alerts' pictures are to be shared with its other charts."""
        self.bot, self.state, self.charts = bot, state, charts

    async def __call__(self, text: str, kind: str, link: tuple[str, str] | None = None,
                       chart: Callable[[], Awaitable[Picture | None]] | None = None, chats: list[int] | None = None,
                       since: int | None = None, event: int | None = None) -> int:
        """Send an alert (silently) to the chats subscribed to its type (`kind`, one of menu.TYPES): of `chats` (say, the one sent
        something that has since changed), else of every chat. Chats the bot can no longer post to are marked gone. Returns how many it was sent to.
        link = (label, url) adds a clickable label to the footer; each carries the settings buttons. chart draws the alert's
        picture (None: the text alone), and is called only when some chat gets the alert and the bot doesn't hold the chart with the
        alert's data already (`since`: the time of the newest reading the alert is about; AlertCharts.held). The text is the picture's
        caption (the text alone if Telegram refuses it); it is uploaded once and the chats after the first are sent Telegram's copy. event: the
        alert's id in the log, which its 🔄 names (so it replays the call that drew the alert's chart)."""
        targets = self.state.alert_chats(kind) if chats is None else [c for c in chats if self.state.subscribed(c, kind)]
        text, entities = with_link(text, link)
        photo = None
        if chart and targets:
            photo = self.charts.held(kind, since) if self.charts else None
            if photo is None and (picture := await chart()):
                photo = picture.png
                if self.charts:
                    self.charts.keep(kind, picture, since)
        started, sent = time.monotonic(), 0
        for chat_id in targets:
            ok, message = await self._send(chat_id, text, entities, kind, photo, event=event)
            sent += ok
            if photo and (sizes := getattr(message, "photo", None)):
                photo = sizes[-1].file_id   # the largest size: the picture as sent
                if self.charts:
                    self.charts.sent(kind, message)
        log.info("Alert sent to %d chat(s) in %.1fs: %s", sent, time.monotonic() - started, text, extra=QUOTES)
        return sent

    async def _post(self, chat_id: int, text: str, entities, kind: str, photo: bytes | str | None, event: int | None = None):
        """The message itself (returned): silent unless the chat turned sound on for this type, with the alert type's settings button (the
        bell). With a photo it is the picture with the text as its caption, and a Refresh that draws the chart again; a picture Telegram
        refuses falls back to the text alone."""
        quiet = kind not in self.state.loud(chat_id)
        markup = lambda refresh: alert_keyboard(kind, self.state.muted(chat_id), self.state.loud(chat_id) if chat_id > 0 else None, refresh=refresh,
                                                event=event)
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

    async def _send(self, chat_id: int, text: str, entities, kind: str, photo: bytes | str | None = None, again: bool = True,
                    event: int | None = None) -> tuple[bool, object]:
        """Send an alert (see _post) to one chat; a chat the bot can no longer post to is marked gone. Telegram's flood limit (when it asks
        for a short wait) and a dropped connection get one more try; a timeout doesn't, as the message may have arrived. Whatever
        fails for one chat is logged, so the others still get the alert. (whether it was sent, the message sent)"""
        try:
            try:
                return True, await self._post(chat_id, text, entities, kind, photo, event)
            except ChatMigrated as e:  # the group became a supergroup: follow it, and send again there
                self.state.migrate_chat(chat_id, e.new_chat_id)
                return await self._send(e.new_chat_id, text, entities, kind, photo, event=event) if e.new_chat_id != chat_id else (False, None)
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
                return await self._send(chat_id, text, entities, kind, photo, again=False, event=event)
        except Exception as e:   # Telegram refused it, or the state file couldn't be saved
            log.warning("Alert to %s failed: %s", chat_id, e, exc_info=not isinstance(e, TelegramError))
        return False, None


class AlertDelivery:
    """The bot's side of the alert log (lib/alert_log.py, docs/mcp-service.md section 4): it reads each new event and sends it, as the
    Notifier does, to the chats subscribed to its kind, with its link, its chart drawn from the event's chart data (and the 🔄 that
    replays the chart's drawn_by) or the text alone. The id of the last event handled is kept in bot_state.json (AlertState.read), so a
    restart resumes after it; the events that expired meanwhile are skipped (the morning's rain is not news by the afternoon). With no id
    kept (the first start) it begins after the newest event: what came before is not news either."""

    def __init__(self, log, notify: "Notifier", state: AlertState, tz):
        self.log, self.notify, self.state, self.tz = log, notify, state, tz
        if self.state.read is None:
            self._handled(self.log.last_id())

    @property
    def last(self) -> int:
        return self.state.read or 0

    def _handled(self, event_id: int):
        self.state.read = event_id
        try:
            self.state.save()
        except Exception as e:   # (the next one saved covers it; at worst a restart sends it again)
            log.warning("Couldn't save the last alert delivered (%s: %s)", type(e).__name__, e)

    async def run(self):
        """Forever: deliver what is new, then wait for more."""
        while True:
            await self.deliver_new()
            await self.log.wait(self.last)

    async def deliver_new(self) -> int:
        """Send each unexpired event after the last handled, oldest first; returns how many were sent."""
        newest = self.log.last_id()
        sent = 0
        for event in self.log.since(self.last):
            try:
                await self.deliver(event)
                sent += 1
            except Exception as e:   # one alert that fails never holds up the next
                log.error("Alert %d couldn't be delivered (%s: %s)", event["id"], type(e).__name__, e, exc_info=True)
            self._handled(event["id"])
        if newest > self.last:   # (only expired events after the last handled: skipped)
            self._handled(newest)
        return sent

    async def deliver(self, event: dict) -> int:
        """One event to the chats subscribed to its kind; returns how many it reached."""
        link = (event["link"]["label"], event["link"]["url"]) if event.get("link") else None
        chart, data, since = None, event.get("chart"), None
        if data:
            from ..specs import from_json
            try:
                since = from_json(data).newest
            except Exception:
                since = None

            async def chart() -> Picture | None:
                return await picture_of(data, self.tz)
        return await self.notify(event["text"], kind=event["kind"], link=link, chart=chart, since=since, event=event["id"])
