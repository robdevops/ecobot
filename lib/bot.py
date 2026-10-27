"""The Telegram side: which messages get answered, how a question is run, how replies are sent.

Private chats: replies to every message. Groups: replies when @mentioned or when someone
replies to the bot. Replies are silent (no notification sound).
"""

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import NamedTuple

from telegram import BotCommand, InputMediaPhoto, Message, ReplyKeyboardRemove, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import intent, periods, prompt, report, report_menu, templates
from .admins import Admins
from .alerts import menu
from .alerts import AlertState
from .irrigation import IrrigationError, Watchdog
from .irrigation import menu as irrigation_menu
from . import gauges
from .charts import render as render_chart
from .ecowitt.dashboard import STATUS
from .tools import Turn
from .config import Config
from .entities import utf16_len, with_link
from .sources import Source
from .timeutil import clock_text, day_text, now_local
from .llm import Agent, shorten_old, strip_tool_turns, trim_history
from .topicmemory import TopicMemory
from .logs import QUOTES, fit

log = logging.getLogger(__name__)

TG_LIMIT = 4000
CAPTION_LIMIT = 1024   # what Telegram lets a photo's caption hold
MAX_CHARTS = 3
RETRY_SECONDS = 60           # the second try, one reasoning step lower, after a question timed out
PENDING_MAX_SECONDS = 10 * 60   # a message that waited longer (the bot was down) is not answered: its moment has passed
TURN_SECONDS = 90            # a question that takes longer is given up on, so the ones queued behind it in the chat are not stuck
DRAFT_REFRESH_SECONDS = 20   # Telegram drops a draft 30 s after its last update, so it is re-sent before that
DRAFT_MIN_GAP = 1.0          # at most one draft update a second while the answer streams in
IRRIGATION_SETTLE_SECONDS = 3   # after any irrigation action, the controller is given this long to report its new state before it is read and the message redrawn
NOT_ADMIN = "Sorry, only group admins can use this bot."
NOT_ADMIN_TOAST = "Only group admins can use this bot"
STALE_BUTTON = "That button is out of date, please ask again"
MAX_CALLBACK_BYTES = 64       # Telegram's own limit on a button's data
WATCHDOG_SECONDS = 45        # a question still running after this many seconds logs where everything is waiting
PREDRAW_SECONDS = 300        # the button charts are drawn ahead this often (a press finds one drawn within periods.ImageCache.FRESH)

HELP = ("Hi! Message me directly, or in groups @mention me or reply to me.\n"
        "/reset clears this chat's memory, /alerts opens the alert settings (buttons to subscribe or unsubscribe), /usage shows what I can do.\n"
        "In a private chat the buttons under the message box ask common questions; /keyboard off hides them.\n"
        "Your user ID: {user} | Chat ID: {chat}")
NO_MODEL = ("Sorry, that one needs the AI model, and this bot has none set up.\n"
            "Without it I can still do: /report, the chart commands (/rain, /weather, /temperature, /humidity, /air_quality, /particulates, "
            "/wind) and plain questions like \"how hot is it?\", \"is it raining?\", \"air quality now\", \"highest humidity yesterday\", "
            "\"rain chart 7d\" or \"forecast\", as well as all the alerts.")


@dataclass
class ChatState:
    history: list[dict] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    topics: TopicMemory = field(default_factory=TopicMemory)   # the guidance topics this chat is still on (the air sensor's own readings among them)


@dataclass
class Answer:
    """What a question is answered with, worked out before it is sent."""
    reply: str | None = None                 # its text; None until something gives one
    photos: list = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    silent: bool = False                     # a chart made in code: it goes with just its title, its text kept for the history
    ready: periods.Drawn | None = None       # a chart drawn moments ago, sent as it is
    made: float | None = None                # seconds the tool took to make the chart in code, for the timing line
    ok: bool = True                          # False: the reply says something went wrong


async def answer_query(query, *args, **kwargs) -> bool:
    """query.answer(...) (the toast, or just the end of the button's spinner). A press made while the bot was down is too old for Telegram to
    take an answer to: that is only logged, and what the press asks for is still done. Returns whether the answer was taken."""
    try:
        await query.answer(*args, **kwargs)
        return True
    except BadRequest as e:
        if "query is too old" not in str(e).lower() and "query id is invalid" not in str(e).lower():
            raise
        log.info("A button press too old to answer (made while the bot was down?): %s", query.data)
        return False


async def watchdog(label: str, seconds: float):
    """Log every task's stack if the question is still running after `seconds`: a hang says where it waits."""
    await asyncio.sleep(seconds)
    stacks = []
    for task in asyncio.all_tasks():
        frames = task.get_stack(limit=4)
        if frames:
            stacks.append(f"{task.get_name()}: " + " <- ".join(f"{f.f_code.co_name}:{f.f_lineno}" for f in reversed(frames)))
    log.warning("Still working after %ds on %s; tasks waiting:\n  %s", seconds, label, "\n  ".join(stacks) or "(none)")


def _short(text: str, limit: int) -> str:
    """The text on one line, cut to `limit` characters (logs.fit): a question or button's data as the log names it."""
    return fit(" ".join(text.split()), limit)


def chart_times(times: dict, made: float | None, pngs: list[bytes], sending: float) -> str:
    """A reply's chart timings for the log: "data 12, calc 3, draw 98, PNG 41 (23 KB), send 812" (ms). times is
    the question's Turn.times; made is how long the tool took when the chart was made in code (the calculation is that less its
    data), else None. A step that wasn't timed is left out."""
    ms = lambda seconds: f"{seconds * 1000:.0f}"
    parts = [f"data {ms(times['data'])}"] if "data" in times else []
    if made is not None:
        parts.append(f"calc {ms(max(0.0, made - times.get('data', 0.0)))}")
    if "draw" in times:
        parts += [f"draw {ms(times['draw'])}", f"PNG {ms(times['png'])} ({sum(map(len, pngs)) / 1024:.0f} KB)"]
    return ", ".join([*parts, f"send {ms(sending)}"])


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


# A chart's answer text sits beside the chart, so wording about the chart itself is redundant
_CHART_WORDS = r"(chart|graph|plot)s?"
_META_PREFIX = re.compile(
    rf"^\s*(here'?s|here is|i'?ve (sent|attached|made))?\s*(a|the|your)?\s*{_CHART_WORDS}\b[^.:\n]*?"
    r"\b((sent|attached|included|below|above|for|of|showing|with)\s+)+(the\s+)?", re.IGNORECASE)
_META_LINE = re.compile(
    rf"^\W*((see|check)\b.*)?{_CHART_WORDS}?\b.*\b(attached|above|below|sent|included)\W*$", re.IGNORECASE)


def strip_chart_talk(text: str) -> str:
    """Remove "Chart sent with the..." style wording from a chart caption."""
    lines = []
    for line in text.splitlines():
        if re.search(rf"\b{_CHART_WORDS}\b", line, re.IGNORECASE) and _META_LINE.match(line):
            continue  # a line that's only about the chart
        cleaned = _META_PREFIX.sub("", line, count=1)
        if cleaned != line and cleaned:
            cleaned = cleaned[0].upper() + cleaned[1:]
        lines.append(cleaned)
    return "\n".join(lines).strip() or text


def describe_source(update: Update) -> str:
    """e.g. "[@rob]" in a private chat, "[@rob, group 'Weather']" in a group."""
    chat, user = update.effective_chat, update.effective_user
    who = (f"@{user.username}" if user.username else user.full_name) if user else "unknown"
    return f"[{who}]" if chat.type == ChatType.PRIVATE else f"[{who}, group '{chat.title}']"


def connection_problem(err) -> bool:
    """A dropped or timed-out connection (BadRequest is a NetworkError subclass, but a real error)."""
    return isinstance(err, NetworkError) and not isinstance(err, BadRequest)


def polling_error(error: TelegramError):
    """Errors while polling for new messages. The library retries by itself, so a dropped
    connection is a one-line warning, not a traceback."""
    if connection_problem(error):
        log.warning("Telegram connection problem while polling (%s: %s) - retrying", type(error).__name__, error)
    else:
        log.error("Telegram polling error (%s: %s)", type(error).__name__, error)


TYPING_MAX_SECONDS = 600   # a safety net: the indicator never outlives a stuck question


async def keep_typing(bot, chat_id: int, thread_id, stop: asyncio.Event):
    """Send "typing..." every 4.5s until stop is set (or TYPING_MAX_SECONDS)."""
    deadline = time.monotonic() + TYPING_MAX_SECONDS
    while not stop.is_set() and time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            await bot.send_chat_action(chat_id, ChatAction.TYPING, message_thread_id=thread_id)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), 4.5)


class Draft:
    """A private chat's "Thinking..." preview (sendMessageDraft): empty until the answer streams in, then the text so far.
    It only lasts 30 s after its last update, so run() re-sends it while the model works; the real reply is sent after."""

    def __init__(self, bot, chat_id: int, thread_id):
        self.bot, self.chat_id, self.thread_id = bot, chat_id, thread_id
        self.draft_id = random.randint(1, 2**31 - 1)  # one id, so the updates animate one draft
        self.text = ""
        self.changed = asyncio.Event()

    def update(self, text: str):
        self.text = text
        self.changed.set()

    async def run(self, stop: asyncio.Event):
        """Send the draft, then again on every change (at most once a second) and every DRAFT_REFRESH_SECONDS. If Telegram
        refuses drafts, give up on them: the "typing..." indicator (which runs alongside) carries on."""
        while not stop.is_set():
            self.changed.clear()
            try:
                await self.bot.send_message_draft(self.chat_id, self.draft_id, text=self.text[-TG_LIMIT:] or None,
                                                  message_thread_id=self.thread_id)
            except Exception as e:
                log.warning("Draft not sent (%s: %s); using the typing indicator alone", type(e).__name__, e)
                return
            waits = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(self.changed.wait())]
            try:
                done, _ = await asyncio.wait(waits, timeout=DRAFT_REFRESH_SECONDS, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waits:
                    w.cancel()
            if self.changed.is_set() and not stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), DRAFT_MIN_GAP)


class Delivered(NamedTuple):
    carried: bool                 # the keyboard (markup) went with the reply
    photo: Message | None = None  # the single chart, as sent


def caption_of(text: str, title: str = "", link: tuple[str, str] | None = None, bold_headings: bool = False):
    """(caption, entities) for a photo: its title and the text, the link under them as a footer, the report's heading names bold; None
    when that is longer than a caption may be (CAPTION_LIMIT, counted as Telegram counts it)."""
    body, ents = with_link(f"{title}\n\n{text}" if title else text, link)
    if utf16_len(body) > CAPTION_LIMIT:
        return None
    return body, [*(report.heading_entities(body) if bold_headings else []), *ents]


def report_caption(text: str):
    """(caption, entities) for the report under its gauges: the whole report, or as many of its first sections as fit a caption (only a
    report longer than any yet, refreshed in its photo, needs cutting: a new one that long goes apart from its picture)."""
    sections = text.split("\n\n")
    while len(sections) > 1 and caption_of("\n\n".join(sections), bold_headings=True) is None:
        sections.pop()
    lines = "\n\n".join(sections).splitlines()
    while len(lines) > 1 and caption_of("\n".join(lines), bold_headings=True) is None:
        lines.pop()
    return caption_of("\n".join(lines), bold_headings=True) or (lines[0][:CAPTION_LIMIT // 2], [])


async def _nothing():
    return None


async def _photos(msg: Message, photos: list[bytes], titles: list[str], buttons, alone: bool) -> tuple[bool, Message | None]:
    """Send the charts, each with its title as its caption; a single one carries `buttons`. (whether a single one went, as sent).
    alone: no text goes with them, so a failure is said in the chat."""
    caption = lambda i: (titles[i] if i < len(titles) else "") or None
    try:
        if len(photos) == 1:
            return True, await msg.reply_photo(photos[0], caption=caption(0), **({"reply_markup": buttons} if buttons else {}))
        await msg.reply_media_group([InputMediaPhoto(p, caption=caption(i)) for i, p in enumerate(photos)])
    except TelegramError:
        log.exception("Couldn't send the chart")
        if alone:
            await msg.reply_text("Sorry, I couldn't send that chart. Please try again in a moment.")
    return False, None


async def deliver(msg: Message, text: str, photos: list[bytes], link: tuple[str, str] | None = None, markup=None,
                  titles: list[str] | None = None, period_row=None, bold_headings: bool = False, photos_first: bool = False) -> Delivered:
    """Send the answer with any charts. One chart and an answer that fits a caption (CAPTION_LIMIT) go as one message, the picture with its
    title and the answer; otherwise a chart carries only its title as its caption and any answer text is its own message, sent first
    (photos_first: sent after the pictures, carrying the buttons: the report and its gauges). link = (label, url) adds a small italic link
    line at the end of the text. markup (the button keyboard) rides on the last text, or on the single photo when there is no text (a
    group of photos can't carry one). period_row (the period buttons) rides on a single photo instead, and then the keyboard waits.
    bold_headings: the text is the report (or a pollen / forecast lookup), whose heading names are set in bold."""
    text = text.strip()
    if photos:
        text = strip_chart_talk(text)
    titles = titles or []
    if text and len(photos) == 1 and (fitted := caption_of(text, titles[0] if titles else "", link, bold_headings)):
        body, ents = fitted   # one chart and an answer that fits its caption: one message, the picture with its title and the answer
        buttons = period_row or markup
        try:
            sent = await msg.reply_photo(photos[0], caption=body, caption_entities=ents or None,
                                         **({"reply_markup": buttons} if buttons else {}))
            return Delivered(bool(markup) and not period_row, sent)
        except TelegramError:
            log.exception("Couldn't send the chart with its answer; sending them apart")
    sent = None
    if photos and photos_first:
        _, sent = await _photos(msg, photos, titles, None, not text)
    on_text = bool(markup) and (not photos or photos_first)   # the keyboard rides on the text
    if text:
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            body, ents = with_link(chunk, link) if last else (chunk, [])
            ents = [*(report.heading_entities(chunk) if bold_headings else []), *ents]
            await msg.reply_text(body, entities=ents or None, disable_web_page_preview=True,
                                 **({"reply_markup": markup} if on_text and last else {}))
    if photos and not photos_first:
        went, sent = await _photos(msg, photos, titles, period_row or markup, not text)
        return Delivered(bool(markup) and not period_row and went, sent)
    return Delivered(on_text and bool(text), sent)


class Bot:
    def __init__(self, cfg: Config, agent: Agent, sources: list[Source], state: AlertState, irrigation=None):
        """state: the known chats and their settings. irrigation: the Tuya client of the irrigation controller, when one is
        configured (its button replaces Wind)."""
        self.cfg, self.agent, self.sources, self.state, self.irrigation = cfg, agent, sources, state, irrigation
        self.watchdog = Watchdog(irrigation, state) if irrigation else None   # closes the valve after a run, however it ends
        self.keyboard_version = templates.version(irrigation is not None)
        self.admin_only = bool(getattr(cfg, "admin_only", False))   # ADMIN_ONLY: only the admins of the groups the bot is in may use it
        self.admins = Admins(state, getattr(cfg, "admin_chat_ids", ()))
        self.chats: dict[tuple, ChatState] = defaultdict(ChatState)
        self.by_name = {s.name: s for s in sources}
        self.images = periods.ImageCache()   # charts drawn lately, so toggling the period buttons is instant (and the buttons' drawn ahead)
        self.answering = 0                   # questions being answered right now: the button charts are drawn ahead (predraw) only when there are none
        self.idle = asyncio.Event()
        self.idle.set()
        self.predrawn = False                # the first round of charts drawn ahead has been logged
        self.predraw_failing: set[str] = set()   # button charts that couldn't be drawn ahead (said once, until they can again)
        self.preloading: set[str] = set()        # questions whose other periods are being uploaded (TELEGRAM_PRELOAD_CHAT)
        self.preload_off = False                 # Telegram refused an upload to that chat (not a flood limit or a timeout): no more are tried until restart
        self.background: set[asyncio.Task] = set()   # tasks running on their own, kept so they aren't dropped
        self.charted = periods.Charted(state.charts if state else None, state.save if state else None)   # the question behind each chart sent (kept across restarts)

    async def close(self):
        """Cancel the tasks still running on their own (the uploads of a chart's other periods)."""
        for task in self.background:
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)

    def register(self, app):
        app.add_handler(CommandHandler(["start", "help"], self.on_start))
        app.add_handler(CommandHandler("reset", self.on_reset))
        app.add_handler(CommandHandler("keyboard", self.on_keyboard))
        app.add_handler(CommandHandler("alerts", self.on_alerts))
        app.add_handler(CommandHandler("usage", self.on_usage))
        app.add_handler(CommandHandler(list(templates.COMMANDS), self.on_ask))
        app.add_handler(CallbackQueryHandler(self.on_button))   # every button press, whatever its data: see on_button
        app.add_handler(MessageHandler(filters.ALL & ~filters.TEXT & ~filters.COMMAND, self.on_other), group=1)
        app.add_handler(ChatMemberHandler(self.on_membership, ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message))
        app.add_error_handler(self.on_error)

    # ---------- who may use the bot, and which button presses are believed ----------
    @staticmethod
    def _sender_ids(update: Update) -> list[int]:
        """The ids that stand for the sender: their user id and, for a message an admin sends "as the group" (or as a channel), that
        chat's id (a button press has no such thing: Telegram names the real user)."""
        ids = [update.effective_user.id] if update.effective_user else []
        msg = update.effective_message
        if update.callback_query is None and msg is not None and getattr(msg, "sender_chat", None) is not None:
            ids.append(msg.sender_chat.id)
        return ids

    async def _allowed(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """With ADMIN_ONLY on, only known group admins (and ADMIN_CHAT_IDS) get through; anyone else is told so, once, and False is
        returned. A group the bot hears from is remembered first: it has to be known for its admins to count."""
        chat = update.effective_chat
        if chat is not None and chat.type != ChatType.PRIVATE:
            self.remember_chat(update)
        if not self.admin_only or await self.admins.allowed(context.bot, self._sender_ids(update)):
            return True
        log.info("Refused %s: not a known group admin", describe_source(update))
        with contextlib.suppress(TelegramError):
            if update.callback_query is not None:
                await answer_query(update.callback_query, NOT_ADMIN_TOAST)
            elif update.effective_message is not None:
                await update.effective_message.reply_text(NOT_ADMIN)
        return False

    @staticmethod
    async def _is_group_admin(bot, chat_id: int, user_id: int) -> bool:
        """Is this user an administrator or the creator of this group, by Telegram's own account of it?"""
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")

    @staticmethod
    def _on_keyboard(message, data: str) -> bool:
        """Is `data` the data of a button on this message's own inline keyboard? (An inaccessible message has no keyboard to look at.)"""
        rows = getattr(getattr(message, "reply_markup", None), "inline_keyboard", None) or ()
        return any(getattr(button, "callback_data", None) == data for row in rows for button in row)

    async def _press(self, query) -> bool:
        """Is this button press believable? A client can send any `data` for any message (Telegram does not check it against the
        keyboard), so a press counts only when its data is short, plain text and is one of the buttons the message actually carries.
        Otherwise it is answered with a toast, logged, and goes no further."""
        data, message = getattr(query, "data", None), getattr(query, "message", None)
        believed = (isinstance(data, str) and 0 < len(data.encode()) <= MAX_CALLBACK_BYTES and data.isprintable()
                    and message is not None and self._on_keyboard(message, data))
        if not believed:
            log.info("Ignored a button press that is not on its message: %s", _short(repr(data), 60))
            with contextlib.suppress(TelegramError):
                await answer_query(query, STALE_BUTTON)
        return believed

    def buttons(self) -> dict:
        """Every family of buttons the bot draws: the start of its data -> what handles it. The only way into any of them is on_button."""
        return {"al:": self.on_alert_button, **({"ir:": self.on_irrigation_button} if self.irrigation else {}),
                periods.PREFIX: self.on_period_button, report_menu.PREFIX: self.on_report_button}

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The one place every button press comes in: believed (_press), a known family (a prefix nobody handles is refused, so a
        new kind of button is dead until it is added to buttons()), allowed (ADMIN_ONLY), then handled."""
        query = update.callback_query
        if query is None or not await self._press(query):
            return
        handler = next((h for prefix, h in self.buttons().items() if query.data.startswith(prefix)), None)
        if handler is None:
            with contextlib.suppress(TelegramError):
                await answer_query(query, STALE_BUTTON)
            return
        if not await self._allowed(update, context):
            return
        try:
            await handler(update, context)
        except ChatMigrated as e:   # a press on an alert from before its group became a supergroup (the chat has a new id)
            self.state.migrate_chat(query.message.chat.id, e.new_chat_id)
            with contextlib.suppress(TelegramError):
                await answer_query(query, "This group was upgraded to a supergroup: please use the buttons on new alerts")

    # ---------- alert chats ----------
    @staticmethod
    def _title(update: Update) -> str:
        chat = update.effective_chat
        return chat.title or (update.effective_user.full_name if update.effective_user else str(chat.id))

    def remember_chat(self, update: Update):
        """Record the chat so alerts can be sent to it (Telegram can't list a bot's chats)."""
        if update.effective_chat:
            self.state.add_chat(update.effective_chat.id, self._title(update))

    async def on_membership(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The bot was added to or removed from a chat."""
        change = update.my_chat_member
        if not change:
            return
        status = change.new_chat_member.status
        if status in ("member", "administrator"):
            self.remember_chat(update)
        elif status in ("left", "kicked"):
            self.state.mark_gone(change.chat.id, f"bot {status}")

    def _alert_kinds(self) -> list[str]:
        return menu.available_kinds(self.by_name, bool(self.irrigation))

    async def on_alerts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/alerts: the alert settings, as buttons (🔔 Alerts and 🔊 Sound). /alerts on and /alerts off still subscribe or
        unsubscribe every type."""
        msg, chat = update.effective_message, update.effective_chat
        in_group = chat.type != ChatType.PRIVATE
        if not in_group and not await self._allowed(update, context):   # anyone in a group may look; a private chat is gated like the rest
            return
        arg = context.args[0].lower() if context.args else ""
        self.remember_chat(update)
        if arg in ("on", "off"):
            if in_group and not (update.effective_user and await self._is_group_admin(context.bot, chat.id, update.effective_user.id)):
                await msg.reply_text("Only group admins can change alerts")   # the menu below is still shown
            else:
                self.state.set_alerts(chat.id, arg == "on")
                log.info("/alerts %s in %s", arg, describe_source(update))
        muted, kinds = self.state.muted(chat.id), self._alert_kinds()
        loud = self.state.loud(chat.id) if chat.type == ChatType.PRIVATE else None   # sound settings: private chats only
        await msg.reply_text(menu.title(muted, kinds), reply_markup=menu.keyboard(muted, kinds, loud=loud))

    async def _help_and_alerts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """What the bot can do, then the alert settings: the Help & Alerts button and /usage."""
        await update.effective_message.reply_text(templates.capabilities_text(self.by_name))
        await self.on_alerts(update, context)

    async def on_usage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/usage: the same as the Help & Alerts button."""
        if not await self._allowed(update, context):
            return
        self.remember_chat(update)
        await self._help_and_alerts(update, context)

    async def on_ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/report, /status, /rain, /weather, /temperature, /humidity, /air_quality, /particulates, /wind: the same as the button of that name,
        in a private chat or a group."""
        if not await self._allowed(update, context):
            return
        self.remember_chat(update)
        command = update.effective_message.text.split()[0].lstrip("/").split("@")[0].lower()
        await self.respond(update, context, templates.COMMANDS[command])

    @staticmethod
    def _alerts_named(kind: str) -> str:
        """"UV alerts" (not "Uv"), for a toast."""
        return f"{menu.LABELS[kind][0].upper()}{menu.LABELS[kind][1:]} alerts"

    async def on_alert_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A press on the alert settings buttons (under an alert or the /alerts message): open or close a menu, or turn a type's alerts or
        its sound on or off. In a group only admins may change them."""
        query = update.callback_query
        press = menu.parse(query.data)
        if press is None:
            await answer_query(query)
            return
        message = query.message
        chat = message.chat
        if chat.type != ChatType.PRIVATE and not await self._is_group_admin(context.bot, chat.id, query.from_user.id):
            await answer_query(query, "Only group admins can change alerts")
            return
        kinds = self._alert_kinds()
        parent = press.parent   # set: the buttons under an alert of that type; None: the /alerts message
        if parent and parent not in kinds:   # an alert of a type the bot no longer sends
            await answer_query(query, STALE_BUTTON)
            return
        private = chat.type == ChatType.PRIVATE
        toast, opened = None, press.section if press.action != "close" else None
        if press.action == "sound" and private and press.kind in kinds and chat.id in self.state.chats:
            self.state.set_sound(chat.id, press.kind, press.on)
            toast = f"{self._alerts_named(press.kind)} {'now make' if press.on else 'no longer make'} a sound"
        elif press.action == "alert" and press.kind in kinds and chat.id in self.state.chats:
            self.state.set_kind(chat.id, press.kind, press.on, kinds)
            toast = f"{self._alerts_named(press.kind)} {'on' if press.on else 'off'} in this chat"
        elif press.action not in ("open", "close"):
            await answer_query(query)
            return
        muted = self.state.muted(chat.id)
        loud = self.state.loud(chat.id) if private else None
        await answer_query(query, toast)
        if parent:   # an alert: only its buttons change
            await self._edit(query, None, menu.alert_keyboard(parent, muted, loud, opened))
        else:        # the /alerts message: its on/off summary is kept current too
            await self._edit(query, menu.title(muted, kinds), menu.keyboard(muted, kinds, opened, loud))

    # ---------- irrigation ----------
    def _irrigation_alerts(self, chat_id: int) -> tuple[bool, bool]:
        """Does this chat get the irrigation battery alerts, and the pause alerts?"""
        return tuple(self.state.subscribed(chat_id, kind) for kind, _ in irrigation_menu.ALERTS.values())

    def _irrigation_keyboard(self, chat_id: int, opened: str | None = None, paused: bool = False, running: bool = False):
        return irrigation_menu.keyboard(*self._irrigation_alerts(chat_id), opened, paused, running)

    def _irrigation_view(self, status: dict, chat_id: int, opened: str | None = None):
        """(the status message, its buttons) for the controller's `status`."""
        return (irrigation_menu.status_text(status, *self._irrigation_alerts(chat_id)),
                self._irrigation_keyboard(chat_id, opened, irrigation_menu.is_paused(status), bool(status.get("switch"))))

    async def send_irrigation(self, msg: Message):
        """The Irrigation button: the controller's status, with its buttons."""
        try:
            status = await self.irrigation.status()
        except IrrigationError as e:
            log.warning("Irrigation status failed: %s", e)
            await msg.reply_text("Sorry, I couldn't reach the irrigation controller.")
            return
        text, markup = self._irrigation_view(status, msg.chat_id)
        await msg.reply_text(text, reply_markup=markup)

    async def _irrigation_act(self, action: str, arg: str, chat_id: int) -> str | None:
        """Do what a button asks. Returns the toast to show, or None for a button that means nothing. Raises IrrigationError."""
        if action == "water" and arg.isdigit() and int(arg) in irrigation_menu.WATER_MINUTES:
            if self.watchdog:   # first: a command whose reply is lost may still have opened the valve
                self.watchdog.arm(int(arg))
            await self.irrigation.water(int(arg))
            return f"Watering for {arg} minutes"
        if action == "delay" and arg in irrigation_menu.DELAYS:
            await self.irrigation.delay(arg)
            return "Unpaused" if arg == "cancel" else f"Paused {arg}"
        if action == "sw" and arg == "off":   # there is no On: a run is always timed (Water). An old message's On button does nothing
            await self.irrigation.set_switch(False)
            if self.watchdog:
                self.watchdog.disarm()
            return "Switched off"
        if action in irrigation_menu.ALERTS and arg in ("on", "off") and chat_id in self.state.chats:
            kind, name = irrigation_menu.ALERTS[action]
            self.state.set_kind(chat_id, kind, arg == "on", self._alert_kinds())
            return f"Irrigation {name} alerts {arg} in this chat"
        return None

    async def on_irrigation_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A press on the irrigation buttons: open or close a list of options, or water, delay, switch, or (un)subscribe; the
        message then shows the controller's fresh status."""
        query = update.callback_query
        message = query.message
        action, arg = ((query.data or "").split(":") + ["", ""])[1:3]
        if not self.irrigation or not message:
            await answer_query(query)
            return
        if action in ("open", "close"):
            opened = arg if action == "open" and arg in irrigation_menu.SECTIONS else None
            await answer_query(query)
            # the message says it: no need to ask the controller to open a list
            await self._edit(query, None, self._irrigation_keyboard(message.chat_id, opened, irrigation_menu.paused_in(message.text),
                                                                    irrigation_menu.running_in(message.text)))
            return
        if action == "refresh":   # read the controller again and redraw: nothing is sent to it, so no wait
            try:
                status = await self.irrigation.status()
            except IrrigationError as e:
                log.warning("Irrigation refresh failed: %s", e)
                await answer_query(query, "Couldn't reach the irrigation controller", show_alert=True)
                return
            await answer_query(query, "Refreshed")
            await self._edit(query, *self._irrigation_view(status, message.chat_id))
            return
        if action == "water" and arg.isdigit() and int(arg) in irrigation_menu.WATER_MINUTES:   # not while the timer is paused
            try:
                status = await self.irrigation.status()
            except IrrigationError as e:
                log.warning("Irrigation water %s failed: %s", arg, e)
                await answer_query(query, "Couldn't reach the irrigation controller", show_alert=True)
                return
            if irrigation_menu.is_paused(status):
                log.info("Irrigation: water %s refused, paused for %s (chat %s)", arg, status["weather_delay"], message.chat_id)
                await answer_query(query, irrigation_menu.PAUSED_ERROR)
                text, markup = self._irrigation_view(status, message.chat_id)
                text, entities = irrigation_menu.with_error(text)
                await self._edit(query, text, markup, entities)   # nothing was sent: no wait
                return
        try:
            toast = await self._irrigation_act(action, arg, message.chat_id)
        except IrrigationError as e:
            log.warning("Irrigation %s %s failed: %s", action, arg, e)
            await answer_query(query, "Couldn't reach the irrigation controller", show_alert=True)
            return
        if toast is None:
            await answer_query(query)
            return
        log.info("Irrigation: %s %s in chat %s", action, arg, message.chat_id)
        await answer_query(query, toast)
        await asyncio.sleep(IRRIGATION_SETTLE_SECONDS)   # every action, before the state is read
        try:
            status = await self.irrigation.status()
        except IrrigationError as e:
            log.warning("Irrigation status after %s %s failed: %s", action, arg, e)   # the toast said what was done; the message stays as it was
            return
        stay = "alerts" if action in irrigation_menu.ALERTS else None   # the Alerts menu stays open after a toggle, to show the new label
        await self._edit(query, *self._irrigation_view(status, message.chat_id, stay))

    @staticmethod
    async def _edit(query, text: str | None, markup, entities=None):
        """Put new text (None: the text stays) and buttons into the message a button was pressed under. Telegram refusing an edit
        that changes nothing is fine."""
        try:
            if text is None:
                await query.edit_message_reply_markup(reply_markup=markup)
            else:
                await query.edit_message_text(text, reply_markup=markup, **({"entities": entities} if entities else {}))
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def on_report_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A press on Refresh under the report: read everything again and redraw the report in the same message. The chat's
        conversation is not touched (this is not a new question)."""
        query = update.callback_query
        read = intent.read("Report", now_local(self.cfg.tz), self.by_name)
        pictured = bool(getattr(getattr(query, "message", None), "photo", None))   # the report is the caption of its gauges: they are drawn again too
        try:
            text, gauge = await asyncio.wait_for(asyncio.gather(self._written_in_code(read, Turn(text="Report")),
                                                                self._status_gauges() if pictured else _nothing()), TURN_SECONDS)
        except Exception as e:   # the message stays as it was
            log.warning("Report refresh failed: %s", e)
            await answer_query(query, "Couldn't refresh, please try again", show_alert=True)
            return
        await answer_query(query)   # no toast: the times in the headings show what changed
        if pictured:
            await self._recaption(query, text, gauge)
        else:
            await self._edit(query, text, report_menu.keyboard(), report.heading_entities(text))

    @staticmethod
    async def _recaption(query, text: str, photo: bytes | None):
        """Put the report's new text, and its gauges drawn again, into the photo message its Refresh was pressed under (no new picture:
        the caption alone). Telegram refusing an edit that changes nothing is fine."""
        body, ents = report_caption(text)
        try:
            if photo:
                await query.edit_message_media(InputMediaPhoto(photo, caption=body, caption_entities=ents or None), reply_markup=report_menu.keyboard())
            else:
                await query.edit_message_caption(caption=body, caption_entities=ents or None, reply_markup=report_menu.keyboard())
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def on_period_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A press on 1D / 7D / 1M / 3M / 1Y under a chart: ask the chart's question again for that period and put the new chart
        in the same message."""
        query = update.callback_query
        days = periods.days_in(query.data)
        message = query.message
        question = self.charted.get(message.chat_id, message.message_id) if days and message else None
        if not question:
            await answer_query(query, "That chart is out of date, please ask again")
            return
        now = now_local(self.cfg.tz)
        if intent.period_days(question, now) == days:   # the chart already shows this period
            await answer_query(query)
            return
        await answer_query(query, f"Drawing {periods.NAMES[days]}...")
        asked = intent.with_period(question, days, now)
        log.info("Period button %s: %s", periods.NAMES[days], question, extra=QUOTES)
        key = self.images.key(asked, days, now)
        if cached := self.images.get(key):   # drawn lately: no new question
            photo, title = cached
            try:
                began = time.monotonic()
                self.images.sent(key, await message.edit_media(InputMediaPhoto(photo, caption=title or None), reply_markup=periods.keyboard(days)))
                self.charted.remember(message.chat_id, message.message_id, asked)
                self.preload_soon(context.bot, asked)
                log.debug("Period button: cached chart sent in %.0f ms%s", (time.monotonic() - began) * 1000,
                         " (Telegram's copy)" if isinstance(photo, str) else "")
                return
            except TelegramError as e:
                log.warning("Couldn't edit the chart in place (%s); drawing it again", e)
        await self.respond(update, context, asked, redraw=message)

    async def _redraw(self, message: Message, reply: str, photo: bytes, title: str, row, question: str, key: str | None = None) -> bool:
        """Put a new chart into the message a period button was pressed under (its answer text, if any, follows as a message of
        its own, unless it fits the caption: then the chart's caption is its title and the answer, as deliver sends them). False when
        Telegram refuses, and the chart is then sent as a new message. key: where the image cache keeps it."""
        text = strip_chart_talk(reply.strip())
        fitted = caption_of(text, title) if text else None
        in_caption = fitted is not None
        body, ents = fitted or (title, [])
        try:
            self.images.sent(key, await message.edit_media(InputMediaPhoto(photo, caption=(body if in_caption else title) or None,
                                                                          caption_entities=(ents or None) if in_caption else None), reply_markup=row))
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.warning("Couldn't edit the chart in place (%s); sending a new one", e)
                return False
        self.charted.remember(message.chat_id, message.message_id, question)
        if text and not in_caption:
            await deliver(message, reply, [])
        return True

    @staticmethod
    def _thread(msg: Message):
        """The forum topic a message is in (None outside topics); each topic is its own conversation."""
        return msg.message_thread_id if msg.is_topic_message else None

    @classmethod
    def _key(cls, msg: Message) -> tuple:
        return (msg.chat_id, cls._thread(msg))

    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/reset: forget this chat's conversation."""
        if not await self._allowed(update, context):
            return
        msg = update.effective_message
        self.chats.pop(self._key(msg), None)
        log.info("/reset in %s", describe_source(update))
        await msg.reply_text("Conversation memory cleared.")

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._allowed(update, context):
            return
        self.remember_chat(update)
        log.info("/start in %s", describe_source(update))
        private = update.effective_chat.type == ChatType.PRIVATE
        await update.effective_message.reply_text(
            HELP.format(user=update.effective_user.id, chat=update.effective_chat.id),
            reply_markup=templates.keyboard(self.irrigation is not None) if private else None)
        if private:
            self.state.set_keyboard(update.effective_chat.id, self.keyboard_version)

    @staticmethod
    async def publish_commands(tg_bot) -> None:
        """At startup: give Telegram the command menu (templates.MENU). A refusal is logged and the bot carries on."""
        try:
            await tg_bot.set_my_commands([BotCommand(name, description) for name, description in templates.MENU])
        except Exception as e:
            log.warning("Couldn't set the command menu: %s: %s", type(e).__name__, e)

    async def refresh_keyboards(self, tg_bot) -> int:
        """At startup: tell each private chat whose buttons are out of date that they changed, with the new keyboard (silently).
        A chat that hid them is left alone. Returns how many were sent."""
        sent = 0
        for chat_id in [c for c in self.state.present() if c > 0 and self.state.keyboard(c) not in (self.keyboard_version, templates.HIDDEN)]:
            try:
                await tg_bot.send_message(chat_id, "Buttons updated.", reply_markup=templates.keyboard(self.irrigation is not None), disable_notification=True)
                self.state.set_keyboard(chat_id, self.keyboard_version)
                sent += 1
            except Forbidden as e:  # blocked the bot
                self.state.mark_gone(chat_id, f"can't post: {e}")
            except TelegramError as e:
                log.warning("Buttons update to %s failed: %s", chat_id, e)
        if sent:
            log.info("Buttons: told %d private chat(s) the buttons changed (keyboard %s)", sent, self.keyboard_version)
        return sent

    def _keyboard_stale(self, msg: Message) -> bool:
        """Does this private chat need the current buttons (it has never had them, or they have changed since)? Not when it
        hid them."""
        return msg.chat.type == ChatType.PRIVATE and self.state.keyboard(msg.chat_id) not in (self.keyboard_version, templates.HIDDEN)

    async def on_keyboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/keyboard shows the buttons again, /keyboard off hides them (private chats only)."""
        if not await self._allowed(update, context):
            return
        msg = update.effective_message
        if update.effective_chat.type != ChatType.PRIVATE:
            await msg.reply_text("The buttons are only in private chats.")
        elif context.args and context.args[0].lower() == "off":
            await msg.reply_text("Buttons hidden. /keyboard shows them again.", reply_markup=ReplyKeyboardRemove())
            self.state.set_keyboard(msg.chat_id, templates.HIDDEN)
        else:
            await msg.reply_text("Here are the buttons.", reply_markup=templates.keyboard(self.irrigation is not None))
            self.state.set_keyboard(msg.chat_id, self.keyboard_version)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Errors raised inside handlers: network blips get one line, anything else a traceback."""
        err = context.error
        if connection_problem(err):
            log.warning("Telegram connection problem while handling a message (%s: %s)", type(err).__name__, err)
        else:
            log.error("Unhandled error while handling a message", exc_info=err)

    # ---------- messages ----------
    async def on_other(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Service messages and other non-text updates: only logged when Telegram says a draft's generation was stopped
        (a field this library version doesn't know, so it is found by name)."""
        msg = update.effective_message
        extra = getattr(msg, "api_kwargs", None) or {}
        if stopped := [k for k in extra if "generation" in k.lower()]:
            log.info("Message generation stopped in %s (fields: %s)", describe_source(update), ", ".join(stopped))

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if not msg or not msg.text:
            return
        waited = (datetime.now(UTC) - msg.date).total_seconds() if getattr(msg, "date", None) else 0
        if waited > PENDING_MAX_SECONDS:
            log.info("Ignored a message %d minutes old from %s: %s", waited // 60, describe_source(update), msg.text, extra=QUOTES)
            return
        private = msg.chat.type == ChatType.PRIVATE
        if private and not await self._allowed(update, context):   # before remember_chat: a stranger never becomes an alert chat
            return
        self.remember_chat(update)
        if private:
            if self.irrigation and (msg.text.strip() == templates.IRRIGATION or irrigation_menu.asked(msg.text)):   # the controller's status and buttons (no model needed)
                await self.send_irrigation(msg)
            elif msg.text.strip() == templates.CAPABILITIES:  # what it measures, then the alert settings (no model needed)
                await self._help_and_alerts(update, context)
            else:
                await self.respond(update, context, templates.sentence(msg.text) or msg.text)
            return
        # Group / supergroup: only respond when addressed
        mention = re.compile(rf"@{re.escape(context.bot.username)}\b", re.IGNORECASE)
        replied_to_bot = (msg.reply_to_message is not None and msg.reply_to_message.from_user is not None
                          and msg.reply_to_message.from_user.id == context.bot.id)
        if mention.search(msg.text) or replied_to_bot:
            if not await self._allowed(update, context):   # only a message meant for the bot is refused, never the group's own talk
                return
            asked = mention.sub("", msg.text)
            if self.irrigation and irrigation_menu.asked(asked):   # "@bot irrigation": the controller, as in a private chat
                await self.send_irrigation(msg)
            else:
                await self.respond(update, context, asked)

    def _content(self, msg: Message, text: str, bot_id: int) -> str:
        """The user turn: the message replied to (in any chat: "the lowest day" means the one in that answer), and
        in groups the sender's name."""
        quoted, reply = msg.reply_to_message, ""
        if quoted and (qtext := (quoted.text or quoted.caption or "")[:1000]):
            who = "your earlier message" if quoted.from_user and quoted.from_user.id == bot_id else (
                quoted.from_user.full_name if quoted.from_user else "someone")
            reply = f'replying to {who}: "{qtext}"'
        if msg.chat.type == ChatType.PRIVATE:
            return f"({reply}) {text}" if reply else text
        sender = msg.from_user.full_name if msg.from_user else "Someone"
        return f"{sender} ({reply}): {text}" if reply else f"{sender}: {text}"

    async def _written_in_code(self, read, turn: Turn) -> str:
        """The report ("weather now") from the fast path's tool calls, made together; lib/report.py lays it out."""
        calls = [read.fast[:2], *read.more]
        results = await self.agent.tools.call_all(calls, turn)
        by_tool = {name: result for (name, _), result in zip(calls, results)}
        if read.report:
            return report.report(by_tool, now_local(self.cfg.tz).date())
        return report.weather_now(by_tool["weather_now"]) or "The weather station isn't answering right now."

    async def _status_gauges(self) -> bytes | None:
        """The report's picture: the weather station's temperature, humidity and rain gauges (ecowitt.dashboard.STATUS). None without the
        station, or when they can't be drawn (the report then goes as text alone)."""
        station = self.by_name.get("Ecowitt")
        if getattr(station, "dashboard", None) is None:
            return None
        try:
            sheet = await station.dashboard(STATUS)
            return await asyncio.to_thread(gauges.render, sheet) if sheet else None
        except Exception:
            log.exception("The report's gauges failed; sending it without them")
            return None

    async def _warn(self, msg: Message, tokens: int):
        """A heads-up before a big job: the question has pulled in a lot of data. Sent now, silently; the answer follows by itself."""
        text = f"\u26a0\ufe0f That's pulling in about {tokens / 1000:.0f}k tokens of data, so it will take a little longer. Working on it..."
        log.info("Heads-up sent: %s", text, extra=QUOTES)
        with contextlib.suppress(TelegramError):
            await msg.reply_text(text)

    async def _chart_in_code(self, read, turn: Turn) -> str | None:
        """A chart asked for plainly: the tool draws it and the caption is written here. None (the model then takes the
        question) when there is nothing to caption."""
        calls = [read.fast[:2], *read.more]
        results = await self.agent.tools.call_all(calls, turn)
        caption = report.chart_caption(read.fast[0], read.fast[1], results[0], turn, self.cfg.tz)
        if caption is None:
            turn.charts.clear()
            turn.forecast_shown.clear()
        return caption

    async def _dashboard(self, ecowitt) -> Answer:
        """The station's gauges as one picture, sent with just its title (no period buttons: it is the state now)."""
        sheet = await ecowitt.dashboard()
        if sheet is None:
            return Answer("The weather station isn't answering right now.", ok=False)
        now = now_local(self.cfg.tz)
        title = f"Weather station dashboard, {day_text(now)} {clock_text(now)}"
        return Answer(title, [await asyncio.to_thread(gauges.render, sheet)], [title], silent=True)

    async def _lookup_in_code(self, read, turn: Turn) -> str | None:
        """A plain lookup (a reading, the air, the pollen, the forecast, what the bot can do) answered from its one tool result; None
        (the model then takes the question) when it can't be."""
        if read.lookup == "about":
            return templates.capabilities_text(self.by_name)
        name, args = read.fast[:2]
        result = await self.agent.tools.call(name, json.dumps(args), turn)
        return report.lookup(read.lookup, result, read.lookup_arg, read.sides, now_local(self.cfg.tz).date())

    async def _ask_model(self, working: list[dict], system: str, read, turn: Turn, draft, on_heavy=None, conv_id=None) -> str:
        """The model's answer. If it has not answered in TURN_SECONDS, ask again once with one reasoning step less (medium > low >
        none), for RETRY_SECONDS; a question already at no reasoning just times out."""
        first = [*([read.fast[:2], *read.more] if read.fast else []), *read.extra] or None
        before, effort, budget, retried = list(working), read.effort, TURN_SECONDS, False
        while True:
            try:
                return await asyncio.wait_for(
                    self.agent.run(working, system, effort, first_call=first, tool_choice=read.tool_choice, turn=turn,
                                   on_heavy=on_heavy, conv_id=conv_id,
                                   **({"on_text": lambda text: None if turn.chart_asked or turn.charts else draft.update(text)} if draft else {})),
                budget)
            except TimeoutError:
                lower = intent.lower_effort(effort)
                if not lower or retried:
                    raise
                log.warning("No answer in %ds at reasoning %s; asking again at %s", budget, effort, lower)
                working[:] = before          # the unfinished attempt's turns are dropped
                effort, budget, retried = lower, RETRY_SECONDS, True

    async def _draw_ahead(self, text: str, now) -> tuple[str, bytes, str, str] | None:
        """(image key, PNG, title, write-up) of the chart `text` asks for when it is one made in code with its own words as its key;
        None otherwise. Raises when it can't be drawn. Nobody is waiting: its requests are not urgent and its logs are quiet."""
        read = intent.read(text, now, self.by_name)
        key = self.images.canonical(text, now)
        if not (read.chart_in_code and key):   # not a chart made in code ("Report"), or a source that isn't there
            return None
        turn = Turn.of(read, text, ahead=True)
        caption = await asyncio.wait_for(self._chart_in_code(read, turn), TURN_SECONDS)
        if caption is None or len(turn.charts) != 1:
            raise ValueError("no chart came of it")
        png = await asyncio.to_thread(render_chart, turn.charts[0], self.cfg.tz)
        return key, png, turn.charts[0].caption(now.date()), caption

    def preload_soon(self, bot, question: str):
        """With TELEGRAM_PRELOAD_CHAT set, upload the other periods of this chart's question in the background."""
        chat = getattr(self.cfg, "telegram_preload_chat", None)
        if chat is None or self.preload_off or question in self.preloading:
            return
        self.preloading.add(question)
        task = asyncio.create_task(self.preload(bot, chat, question))
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    async def preload(self, bot, chat: int, question: str):
        """Upload (silently, to `chat`) the chart for each other period of `question`, so a press of a period button edits the message by
        Telegram's copy and uploads nothing. Nothing holds it back: each period is drawn and uploaded on its own, the moment it can be,
        whatever else is being answered."""
        started, done, skipped = time.monotonic(), set(), []
        try:
            now = now_local(self.cfg.tz)
            own = intent.period_days(question, now)
            asked = {days: intent.with_period(question, days, now) for days in periods.PERIODS if days != own}
            asked = {days: text for days, text in asked.items() if not self.images.has_copy(self.images.key(text, days, now))}

            async def upload(days, text):
                try:
                    drawn = await self._draw_ahead(text, now)
                except Exception as e:
                    log.debug("Couldn't draw %r to preload: %s: %s", text, type(e).__name__, e)
                    return
                if drawn is None:
                    return
                key, png, title, caption = drawn
                self.images.put(key, png, title, caption, ttl=self.images.PRELOAD_TTL)
                if self.preload_off:   # an upload was refused meanwhile
                    return
                try:
                    self.images.sent(key, await bot.send_photo(chat, png, caption=title or None, disable_notification=True))
                except (RetryAfter, TimedOut) as e:   # Telegram's flood limit or a slow upload: a press uploads this one instead, and the next chart tries again
                    skipped.append(e)
                except TelegramError as e:
                    if not self.preload_off:   # said once, though every period's upload is refused
                        self.preload_off = True
                        log.warning("Couldn't upload to TELEGRAM_PRELOAD_CHAT %s (%s: %s); not preloading charts", chat, type(e).__name__, e)
                else:
                    done.add(days)

            await asyncio.gather(*(upload(days, text) for days, text in asked.items()))
            if skipped:
                log.warning("TELEGRAM_PRELOAD_CHAT %s didn't take %d of the charts just now (%s: %s)", chat, len(skipped), type(skipped[0]).__name__, skipped[0])
            if done:
                log.info("Preloaded %s of %r to chat %s in %.1fs", ", ".join(periods.NAMES[days] for days in periods.PERIODS if days in done), question,
                         chat, time.monotonic() - started, extra=QUOTES)
        finally:
            self.preloading.discard(question)

    async def predraw(self):
        """Draw the charts the buttons ask for ahead of the press: one pressed in the next ImageCache.FRESH seconds is sent at once.
        One chart at a time, and never while a question is being answered (its own drawing would wait behind these). Run every
        PREDRAW_SECONDS; the first round is logged, and a chart that can't be drawn once, until it can again."""
        started, done = time.monotonic(), []
        for text in dict.fromkeys(question for row in templates.rows(self.irrigation is not None) for _, question in row if question):
            await self.idle.wait()
            try:
                drawn = await self._draw_ahead(text, now_local(self.cfg.tz))
                if drawn is None:
                    continue
            except Exception as e:
                if text not in self.predraw_failing:
                    self.predraw_failing.add(text)
                    log.warning("Can't draw %r ahead: %s: %s", text, type(e).__name__, e)
                continue
            key, png, title, caption = drawn
            self.images.put(key, png, title, caption)
            done.append(text)
            if text in self.predraw_failing:
                self.predraw_failing.discard(text)
                log.info("Drawn ahead again: %s", text)
        if not self.predrawn:
            self.predrawn = True
            log.info("Button charts drawn ahead every %ds: %d in %.1fs", PREDRAW_SECONDS, len(done), time.monotonic() - started)

    async def respond(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, redraw: Message | None = None):
        """Answer a question (_respond), counted while it runs: predraw waits for it."""
        self.answering += 1
        self.idle.clear()
        try:
            await self._respond(update, context, text, redraw)
        finally:
            self.answering -= 1
            if not self.answering:
                self.idle.set()

    async def _respond(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, redraw: Message | None = None):
        """Answer `text` (redraw: a chart's message, when a period button asks for that chart again in place)."""
        msg = update.effective_message
        text = text.strip()
        if not text:
            return
        stop_typing = asyncio.Event()   # "typing..." from the first moment, even while this question waits its turn
        typing = None if redraw is not None else asyncio.create_task(   # a chart redrawn in place from a button shows no "typing..."
            keep_typing(context.bot, msg.chat_id, self._thread(msg), stop_typing))
        await asyncio.sleep(0)
        now = now_local(self.cfg.tz)
        read = intent.read(text, now, self.by_name)
        log.info("%s %s%s", describe_source(update), f"(reasoning: {read.effort}) " if read.effort != intent.EFFORT_DEFAULT else "",
                 text, extra=QUOTES)
        for source in self.sources:  # fetch recent readings while the model thinks
            if source.wants(text) and not read.chart_in_code:   # a chart made in code reads just what it needs, first in line
                source.poke()
        if read.fast:
            log.info("Fast path: %s", read.fast[2])

        chat = self.chats[self._key(msg)]
        started = time.monotonic()
        async with chat.lock:
            draft = (Draft(context.bot, msg.chat_id, self._thread(msg))
                     if redraw is None and msg.chat.type == ChatType.PRIVATE and hasattr(context.bot, "send_message_draft") else None)
            drafting = asyncio.create_task(draft.run(stop_typing)) if draft else None   # a private chat also gets the "Thinking..." draft
            stuck = asyncio.create_task(watchdog(_short(text, 40), WATCHDOG_SECONDS))
            working = [*chat.history, {"role": "user", "content": self._content(msg, text, context.bot.id)}]
            new_from = len(working)
            turn = Turn.of(read, text)
            earlier = working[:-1]
            recent = ([str(m["content"]) for m in earlier if m["role"] == "user"][-2:]
                      + [str(m["content"]) for m in earlier if m["role"] == "assistant"][-1:])
            # the topics of the last messages (the person's last two, the bot's last reply) and those this chat is still on
            turn.topics = chat.topics.active().union(*(intent.topics_in(said) for said in recent))
            try:
                answer = await self._answer(msg, read, turn, working, draft, now, redraw)
                tools_used = {tc["function"]["name"] for m in working[new_from:] for tc in m.get("tool_calls") or []}
                chat.topics.update(turn.topics, intent.topics_in(text) | intent.topics_in(answer.reply or "")
                                   | {intent.TOOL_TOPICS[t] for t in tools_used if t in intent.TOOL_TOPICS}
                                   | ({"air_extra"} if turn.air_extra_used else set()))
                chat.history = shorten_old(trim_history(strip_tool_turns(working)))
                for spec in turn.charts[:MAX_CHARTS]:  # drawn while "typing..." is still showing
                    try:
                        answer.photos.append(await asyncio.to_thread(render_chart, spec, self.cfg.tz, turn.times))
                        answer.titles.append(spec.caption(now.date()))
                    except Exception:
                        log.exception("Chart failed; sending the answer without it")
            except TimeoutError:
                log.error("Gave up after %ds on: %s", TURN_SECONDS, text, extra=QUOTES)
                answer = Answer("Sorry, that took too long. Please try again in a moment.", ok=False)
            except Exception as e:
                log.exception("Agent error")
                # The details (which can include provider error bodies) go to the log, not the chat
                answer = Answer(f"Sorry, something went wrong on my side ({type(e).__name__}). Please try again in a moment.", ok=False)
            finally:
                stuck.cancel()
                stop_typing.set()
                await asyncio.gather(*[t for t in (typing, drafting) if t])  # wait for any in-flight "typing" or draft so none is sent after the reply
        await self._send(msg, context.bot, read, turn, answer, working[new_from:], now, redraw, started)

    async def _answer(self, msg: Message, read, turn: Turn, working: list[dict], draft, now: datetime, redraw: Message | None) -> Answer:
        """The answer, from the first way that gives one: a chart drawn moments ago, a chart made in code, a lookup, the report
        written in code, else the model. `working` is the conversation, the question last; the answer joins it."""
        if read.chart_in_code and redraw is None and (ready := self.images.ready(turn.text, now)):
            answer = Answer(ready.caption, [ready.photo], [ready.title], silent=True, ready=ready)   # drawn ahead, or for someone's same question
        elif read.chart_in_code:   # drawn by the tool; the chart goes out with just its title (the text is kept for the history)
            began = time.monotonic()
            reply = await asyncio.wait_for(self._chart_in_code(read, turn), TURN_SECONDS)
            answer = Answer(reply, silent=reply is not None, made=time.monotonic() - began if reply is not None else None)
        elif read.dashboard and "Ecowitt" in self.by_name:
            answer = await asyncio.wait_for(self._dashboard(self.by_name["Ecowitt"]), TURN_SECONDS)
        elif read.lookup:
            answer = Answer(await asyncio.wait_for(self._lookup_in_code(read, turn), TURN_SECONDS))
            if answer.reply is None:
                turn.forecast_shown.clear()
        elif read.fast and (read.report or read.weather_now):   # written in code from the tools' results, no model (the report: with its gauges)
            text, gauge = await asyncio.wait_for(asyncio.gather(self._written_in_code(read, turn),
                                                                self._status_gauges() if read.report else _nothing()), TURN_SECONDS)
            answer = Answer(text, [gauge] if gauge else [], [""] if gauge else [])
        else:
            answer = Answer()
        if answer.reply is not None:
            working.append({"role": "assistant", "content": answer.reply})
            return answer
        if not getattr(self.agent, "ready", True):   # no XAI_API_KEY: what is left is for the model
            answer.reply = NO_MODEL
            working.append({"role": "assistant", "content": answer.reply})
            return answer
        before = [str(m["content"]) for m in working[:-1] if m["role"] == "user"][-2:]
        found = intent.topics(str(working[-1]["content"]), before) | turn.topics
        turn.topics = found
        turn.air_extra = turn.air_extra or "air_extra" in found   # the tools offer the air sensor's own readings whenever the prompt tells of them
        system = prompt.build(datetime.now(self.cfg.tz), {s.name: s.describe() for s in self.sources}, read.hints,
                              read.about_the_bot, read.rain_caption, found)
        answer.reply = await self._ask_model(working, system, read, turn, draft, lambda tokens: self._warn(msg, tokens),
                                             f"ecobot-{msg.chat_id}")
        return answer

    async def _send(self, msg: Message, bot, read, turn: Turn, answer: Answer, added: list[dict], now: datetime,
                    redraw: Message | None, started: float):
        """Send the answer, a single chart with the period buttons under it (in place of `redraw` when a button asked for it), the
        keyboard if the chat's is out of date; then keep what follows from it: the chart for its period buttons and the next to
        ask, the question behind it, the forecast days shown. `added`: the messages the answer added to the conversation."""
        text, reply, photos, titles = turn.text, answer.reply, answer.photos, answer.titles
        caption = reply if answer.silent else None   # a chart made in code: its text, kept with the chart for the next to ask the same
        if answer.silent and photos:
            reply = ""
        if answer.ok and not reply.strip() and not photos:   # a chart with no text that could not be drawn
            reply = "Sorry, I couldn't draw that chart. Please try again in a moment."
        used = [m for m in added if m["role"] == "tool"]
        log.info("%s %s in %.1fs, tools %d, charts %d, %d chars: %s", "Replied" if answer.ok else "Error reply", msg.chat_id,
                 time.monotonic() - started, len(used), len(photos), len(reply), reply, extra=QUOTES)
        used_air = any(tc["function"]["name"] == "air_quality" for m in added for tc in m.get("tool_calls") or [])
        air = self.by_name.get("AirGradient")
        markup = templates.keyboard(self.irrigation is not None) if self._keyboard_stale(msg) else None
        refresh = answer.ok and read.report and 0 < len(reply) <= TG_LIMIT and not markup   # the report, to be refreshed in place
        row = periods.keyboard(intent.period_days(text, now)) if len(photos) == 1 and not (read.dashboard or read.report) else None   # period buttons under a single chart
        drawn = list(photos)
        kept = self.images.key(text, intent.period_days(text, now), now) if row and answer.ok and not reply.strip() else None
        if kept and not answer.ready:   # a chart and nothing else: keep it for the period buttons (and, its words being its key, for the next to ask them)
            self.images.put(kept, photos[0], titles[0], caption if kept == self.images.canonical(text, now) else None)
        try:
            began = time.monotonic()
            if redraw is not None and row and await self._redraw(redraw, reply, photos[0], titles[0], row, text, kept):
                reply, photos, row = "", [], None
            sent = await deliver(msg, reply, photos, link=air.link if air and used_air else None,
                                 markup=markup or (report_menu.keyboard() if refresh else None), titles=titles, period_row=row,
                                 bold_headings=answer.ok and bool(read.report or read.lookup in ("pollen", "forecast")), photos_first=read.report)
            carried_keyboard = sent.carried and bool(markup)   # the refresh button is not the chat's keyboard
            if answer.ready:
                log.debug("Chart ms: drawn %.0fs ago, send %.0f%s", self.images.age(answer.ready), (time.monotonic() - began) * 1000,
                          " (Telegram's copy)" if isinstance(drawn[0], str) else "")
            elif drawn:
                log.debug("Chart ms: %s", chart_times(turn.times, answer.made, drawn, time.monotonic() - began))
            if sent.photo and row is not None:
                self.charted.remember(msg.chat_id, sent.photo.message_id, text)
                self.images.sent(kept, sent.photo)
            if kept and read.chart_in_code:   # a chart with period buttons, made in code: its other periods are uploaded ahead
                self.preload_soon(bot, text)
            if answer.ok and turn.forecast_shown:   # it was sent: a later revision of these days is worth telling this chat
                self.state.record_forecast(msg.chat_id, turn.forecast_shown, now_local(self.cfg.tz).date())
            if carried_keyboard:
                log.info("Buttons: sent keyboard %s to chat %s (it had %s)", self.keyboard_version, msg.chat_id, self.state.keyboard(msg.chat_id))
                self.state.set_keyboard(msg.chat_id, self.keyboard_version)
            elif markup and not row:
                log.info("Buttons: this reply couldn't carry the keyboard (several charts); will try the next one")
        except TelegramError:
            log.exception("Failed to deliver reply")
