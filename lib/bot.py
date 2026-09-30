"""The Telegram side: which messages get answered, how a question is run, how replies are sent.

Private chats: replies to every message. Groups: replies when @mentioned or when someone
replies to the bot. Replies are silent (no notification sound).
"""

import asyncio
import contextlib
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from telegram import InputMediaPhoto, Message, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import intent, prompt
from .alerts import AlertState, with_footer
from .charts import AVERAGE_ASKED, CHART_ASKED, CHART_FIELD, CHART_REQUESTS, render as render_chart
from .config import Config
from .timeutil import now_local
from .llm import Agent, strip_tool_turns, trim_history

log = logging.getLogger(__name__)

TG_LIMIT = 4000
CAPTION_LIMIT = 1024  # Telegram's limit for photo captions
MAX_CHARTS = 3

HELP = ("Hi! Message me directly, or in groups @mention me or reply to me.\n"
        "/reset clears this chat's memory, /alerts manages weather alerts (on/off for this chat).\n"
        "Your user ID: {user} | Chat ID: {chat}")
ALERTS_TEXT = ("Weather alerts are {on} here: rain starting and stopping, rain likely soon, indoor/outdoor "
               "temperatures crossing after 2+ days, and unhealthy outdoor air (and when it's safe again). "
               "Use /alerts {other} to turn them {other}.")


@dataclass
class ChatState:
    history: list[dict] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


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


# The reply is the chart's caption, so wording about the chart itself is redundant
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
    connection is a one-line warning, not a traceback. Must not raise."""
    try:
        if connection_problem(error):
            log.warning("Telegram connection problem while polling (%s: %s) - retrying", type(error).__name__, error)
        else:
            log.error("Telegram polling error (%s: %s)", type(error).__name__, error)
    except Exception:
        pass


async def keep_typing(bot, chat_id: int, thread_id, stop: asyncio.Event):
    """Send "typing..." every 4.5s until stop is set."""
    while not stop.is_set():
        with contextlib.suppress(Exception):
            await bot.send_chat_action(chat_id, ChatAction.TYPING, message_thread_id=thread_id)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), 4.5)


async def deliver(msg: Message, text: str, photos: list[bytes], link: tuple[str, str] | None = None):
    """Send the answer with any charts. A short answer goes in the photo's caption (one message);
    a long one goes first as text, then the charts. If sending the chart fails the answer is still
    sent as text. link = (label, url) adds a small italic link line at the end."""
    text = text.strip()
    if photos:
        text = strip_chart_talk(text)
    caption, entities = with_footer(text, link)
    if photos and len(caption) <= CAPTION_LIMIT:
        try:
            if len(photos) == 1:
                await msg.reply_photo(photos[0], caption=caption, caption_entities=entities or None)
            else:
                await msg.reply_media_group([InputMediaPhoto(p, caption=caption if i == 0 else None,
                                                             caption_entities=(entities or None) if i == 0 else None)
                                             for i, p in enumerate(photos)])
            return
        except TelegramError:
            log.exception("Couldn't send the chart; sending the answer as text")
            photos = []
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        body, ents = with_footer(chunk, link) if i == len(chunks) - 1 else (chunk, [])
        await msg.reply_text(body, entities=ents or None, disable_web_page_preview=True)
    if photos:
        try:
            if len(photos) == 1:
                await msg.reply_photo(photos[0])
            else:
                await msg.reply_media_group([InputMediaPhoto(p) for p in photos])
        except TelegramError:
            log.exception("Couldn't send the chart")


class Bot:
    def __init__(self, cfg: Config, agent: Agent, sources: list, state: AlertState | None):
        self.cfg, self.agent, self.sources, self.state = cfg, agent, sources, state
        self.chats: dict[tuple, ChatState] = defaultdict(ChatState)
        self.by_name = {s.name: s for s in sources}

    def register(self, app):
        app.add_handler(CommandHandler(["start", "help"], self.on_start))
        app.add_handler(CommandHandler("reset", self.on_reset))
        app.add_handler(CommandHandler("alerts", self.on_alerts))
        app.add_handler(ChatMemberHandler(self.on_membership, ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message))
        app.add_error_handler(self.on_error)

    # ---------- alert chats ----------
    @staticmethod
    def _title(update: Update) -> str:
        chat = update.effective_chat
        return chat.title or (update.effective_user.full_name if update.effective_user else str(chat.id))

    def remember_chat(self, update: Update):
        """Record the chat so alerts can be sent to it (Telegram can't list a bot's chats)."""
        if self.state and update.effective_chat:
            self.state.add_chat(update.effective_chat.id, self._title(update))

    async def on_membership(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The bot was added to or removed from a chat."""
        change = update.my_chat_member
        if not self.state or not change:
            return
        status = change.new_chat_member.status
        if status in ("member", "administrator"):
            self.remember_chat(update)
        elif status in ("left", "kicked"):
            self.state.remove_chat(change.chat.id, f"bot {status}")

    async def on_alerts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/alerts, /alerts on, /alerts off"""
        msg, chat = update.effective_message, update.effective_chat
        if not self.state:
            await msg.reply_text("Alerts aren't available: no sensor is connected.")
            return
        arg = context.args[0].lower() if context.args else ""
        if arg in ("on", "off"):
            self.state.set_alerts(chat.id, self._title(update), arg == "on")
            log.info("/alerts %s in %s", arg, describe_source(update))
        self.remember_chat(update)
        on = self.state.alerts_on(chat.id)
        await msg.reply_text(ALERTS_TEXT.format(on="on" if on else "off", other="off" if on else "on"))

    @staticmethod
    def _thread(msg: Message):
        """The forum topic a message is in (None outside topics); each topic is its own conversation."""
        return msg.message_thread_id if msg.is_topic_message else None

    @classmethod
    def _key(cls, msg: Message) -> tuple:
        return (msg.chat_id, cls._thread(msg))

    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/reset: forget this chat's conversation."""
        msg = update.effective_message
        self.chats.pop(self._key(msg), None)
        log.info("/reset in %s", describe_source(update))
        await msg.reply_text("Conversation memory cleared.")

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.remember_chat(update)
        log.info("/start in %s", describe_source(update))
        await update.effective_message.reply_text(
            HELP.format(user=update.effective_user.id, chat=update.effective_chat.id))

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Errors raised inside handlers: network blips get one line, anything else a traceback."""
        err = context.error
        if connection_problem(err):
            log.warning("Telegram connection problem while handling a message (%s: %s)", type(err).__name__, err)
        else:
            log.error("Unhandled error while handling a message", exc_info=err)

    # ---------- messages ----------
    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if not msg or not msg.text:
            return
        self.remember_chat(update)
        if msg.chat.type == ChatType.PRIVATE:
            await self.respond(update, context, msg.text)
            return
        # Group / supergroup: only respond when addressed
        mention = re.compile(rf"@{re.escape(context.bot.username)}\b", re.IGNORECASE)
        replied_to_bot = (msg.reply_to_message is not None and msg.reply_to_message.from_user is not None
                          and msg.reply_to_message.from_user.id == context.bot.id)
        if mention.search(msg.text) or replied_to_bot:
            await self.respond(update, context, mention.sub("", msg.text))

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

    async def respond(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        msg = update.effective_message
        text = text.strip()
        if not text:
            return
        effort = intent.reasoning_effort(text)
        log.info("%s %s%s", describe_source(update), f"(reasoning: {effort}) " if effort != intent.EFFORT_DEFAULT else "",
                 _short(text))
        for source in self.sources:  # fetch recent readings while the model thinks
            if source.wants(text):
                source.poke()
        now = now_local(self.cfg.tz)
        try:
            fast = intent.fast_call(text, now, "Ecowitt" in self.by_name, "AirGradient" in self.by_name)
        except Exception:  # never lose a reply to a shortcut: let the model handle it
            log.exception("Fast path failed; using the normal path")
            fast = None
        if fast:
            log.info("Fast path: %s", fast[2])

        chat = self.chats[self._key(msg)]
        thread_id = self._thread(msg)
        started = time.monotonic()
        async with chat.lock:
            stop_typing = asyncio.Event()
            typing = asyncio.create_task(keep_typing(context.bot, msg.chat_id, thread_id, stop_typing))
            working = [*chat.history, {"role": "user", "content": self._content(msg, text, context.bot.id)}]
            new_from = len(working)
            ok, charts, photos = True, [], []
            chart_token = CHART_REQUESTS.set(charts)  # the history tools add chart specs here
            average_token = AVERAGE_ASKED.set(bool(intent.AVERAGE.search(text)))
            field_token = CHART_FIELD.set(intent.chart_field(text))  # humidity questions get a humidity chart
            asked_token = CHART_ASKED.set(bool(intent.GRAPH.search(text)))  # "plot" means a chart, whatever the model calls
            try:
                system = prompt.build(datetime.now(self.cfg.tz), [s.describe() for s in self.sources],
                                      intent.period_hints(text, now))
                reply = await self.agent.run(working, system, effort, first_call=fast[:2] if fast else None,
                                             require_tool=intent.needs_data(text))
                chat.history = trim_history(strip_tool_turns(working))
                for spec in charts[:MAX_CHARTS]:  # drawn while "typing..." is still showing
                    try:
                        photos.append(await asyncio.to_thread(render_chart, spec, self.cfg.tz))
                    except Exception:
                        log.exception("Chart failed; sending the answer without it")
            except Exception as e:
                log.exception("Agent error")
                # The details (which can include provider error bodies) go to the log, not the chat
                ok, reply = False, f"Sorry, something went wrong on my side ({type(e).__name__}). Please try again in a moment."
            finally:
                CHART_REQUESTS.reset(chart_token)
                CHART_ASKED.reset(asked_token)
                CHART_FIELD.reset(field_token)
                AVERAGE_ASKED.reset(average_token)
                stop_typing.set()
                await typing  # wait for any in-flight "typing" so none is sent after the reply

        used = [m for m in working[new_from:] if m["role"] == "tool"]
        log.info("%s %s in %.1fs, tools %d, charts %d, %d chars: %s", "Replied" if ok else "Error reply", msg.chat_id,
                 time.monotonic() - started, len(used), len(photos), len(reply), _short(reply, 30))
        used_air = any(tc["function"]["name"] == "air_quality"
                       for m in working[new_from:] for tc in m.get("tool_calls") or [])
        air = self.by_name.get("AirGradient")
        try:
            await deliver(msg, reply, photos, link=air.link if air and used_air else None)
        except TelegramError:
            log.exception("Failed to deliver reply")
