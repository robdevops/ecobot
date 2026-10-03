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
from datetime import datetime, timezone

from telegram import InputMediaPhoto, Message, ReplyKeyboardRemove, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError
from telegram.ext import CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import intent, periods, prompt, report, templates
from .alerts import menu
from .alerts import AlertState, with_footer
from .charts import render as render_chart
from .tools import Turn
from .config import Config
from .timeutil import now_local
from .llm import Agent, shorten_old, strip_tool_turns, trim_history

log = logging.getLogger(__name__)

TG_LIMIT = 4000
MAX_CHARTS = 3
RETRY_SECONDS = 60           # the second try, one reasoning step lower, after a question timed out
PENDING_MAX_SECONDS = 10 * 60   # a message that waited longer (the bot was down) is not answered: its moment has passed
TURN_SECONDS = 90            # a question that takes longer is given up on, so the ones queued behind it in the chat are not stuck
DRAFT_REFRESH_SECONDS = 20   # Telegram drops a draft 30 s after its last update, so it is re-sent before that
DRAFT_MIN_GAP = 1.0          # at most one draft update a second while the answer streams in
WATCHDOG_SECONDS = 45        # a question still running after this many seconds logs where everything is waiting

HELP = ("Hi! Message me directly, or in groups @mention me or reply to me.\n"
        "/reset clears this chat's memory, /alerts opens the alert settings (buttons to subscribe or unsubscribe).\n"
        "In a private chat the buttons under the message box ask common questions; /keyboard off hides them.\n"
        "Your user ID: {user} | Chat ID: {chat}")
@dataclass
class ChatState:
    history: list[dict] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def watchdog(label: str, seconds: float):
    """Log every task's stack if the question is still running after `seconds`: a hang says where it waits."""
    await asyncio.sleep(seconds)
    stacks = []
    for task in asyncio.all_tasks():
        frames = task.get_stack(limit=4)
        if frames:
            stacks.append(f"{task.get_name()}: " + " <- ".join(f"{f.f_code.co_name}:{f.f_lineno}" for f in reversed(frames)))
    log.warning("Still working after %ds on %s; tasks waiting:\n  %s", seconds, label, "\n  ".join(stacks) or "(none)")


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
    connection is a one-line warning, not a traceback. Must not raise."""
    try:
        if connection_problem(error):
            log.warning("Telegram connection problem while polling (%s: %s) - retrying", type(error).__name__, error)
        else:
            log.error("Telegram polling error (%s: %s)", type(error).__name__, error)
    except Exception:
        pass


TYPING_MAX_SECONDS = 600   # a safety net: the indicator never outlives a stuck question


async def keep_typing(bot, chat_id: int, thread_id, stop: asyncio.Event):
    """Send "typing..." every 4.5s until stop is set (or TYPING_MAX_SECONDS)."""
    deadline = time.monotonic() + TYPING_MAX_SECONDS
    while not stop.is_set() and time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            await bot.send_chat_action(chat_id, ChatAction.TYPING, message_thread_id=thread_id)
        with contextlib.suppress(asyncio.TimeoutError):
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
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), DRAFT_MIN_GAP)


async def deliver(msg: Message, text: str, photos: list[bytes], link: tuple[str, str] | None = None, markup=None,
                  titles: list[str] | None = None, period_row=None, sent_photo: list | None = None) -> bool:
    """Send the answer with any charts. A chart carries only its title as its caption; any answer text is its own message,
    sent first. link = (label, url) adds a small italic link line at the end of the text. markup (the button keyboard) rides on
    the last text, or on the single photo when there is no text; returns whether it was sent (a group of photos can't carry one).
    period_row (the period buttons) rides on a single photo instead, and then the keyboard waits; the photo's Message goes into sent_photo."""
    text = text.strip()
    if photos:
        text = strip_chart_talk(text)
    titles = titles or []
    carried = False
    if text:
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            body, ents = with_footer(chunk, link) if last else (chunk, [])
            await msg.reply_text(body, entities=ents or None, disable_web_page_preview=True,
                                 **({"reply_markup": markup} if markup and last and not photos else {}))
        carried = bool(markup) and not photos
    if photos:
        caption = lambda i: (titles[i] if i < len(titles) else "") or None
        try:
            if len(photos) == 1:
                buttons = period_row or markup
                sent = await msg.reply_photo(photos[0], caption=caption(0), **({"reply_markup": buttons} if buttons else {}))
                if sent_photo is not None and sent is not None:
                    sent_photo.append(sent)
                return bool(markup) and not period_row
            await msg.reply_media_group([InputMediaPhoto(p, caption=caption(i)) for i, p in enumerate(photos)])
        except TelegramError:
            log.exception("Couldn't send the chart")
            if not text:
                await msg.reply_text("Sorry, I couldn't send that chart. Please try again in a moment.")
    return carried


class Bot:
    def __init__(self, cfg: Config, agent: Agent, sources: list, state: AlertState | None):
        self.cfg, self.agent, self.sources, self.state = cfg, agent, sources, state
        self.chats: dict[tuple, ChatState] = defaultdict(ChatState)
        self.by_name = {s.name: s for s in sources}
        self.images = periods.ImageCache()   # charts drawn lately, so toggling the period buttons is instant
        self.charted = periods.Charted(state.charts if state else None, state.save if state else None)   # the question behind each chart sent (kept across restarts)

    def register(self, app):
        app.add_handler(CommandHandler(["start", "help"], self.on_start))
        app.add_handler(CommandHandler("reset", self.on_reset))
        app.add_handler(CommandHandler("keyboard", self.on_keyboard))
        app.add_handler(CommandHandler("alerts", self.on_alerts))
        app.add_handler(CallbackQueryHandler(self.on_alert_button, pattern=r"^al:"))
        app.add_handler(CallbackQueryHandler(self.on_period_button, pattern=rf"^{periods.PREFIX}"))
        app.add_handler(MessageHandler(filters.ALL & ~filters.TEXT & ~filters.COMMAND, self.on_other), group=1)
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

    def _alert_kinds(self) -> list[str]:
        return menu.available_kinds(set(self.by_name))

    async def on_alerts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/alerts: the alert settings, as buttons (Subscribe | Unsubscribe). /alerts on and /alerts off still subscribe or
        unsubscribe every type."""
        msg, chat = update.effective_message, update.effective_chat
        if not self.state:
            await msg.reply_text("Alerts aren't available: no sensor is connected.")
            return
        arg = context.args[0].lower() if context.args else ""
        self.remember_chat(update)
        if arg in ("on", "off"):
            self.state.set_alerts(chat.id, self._title(update), arg == "on")
            log.info("/alerts %s in %s", arg, describe_source(update))
        muted, kinds = self.state.muted(chat.id), self._alert_kinds()
        await msg.reply_text(menu.title(muted, kinds), reply_markup=menu.keyboard(muted, kinds))

    async def on_alert_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A press on the alert settings buttons (under an alert or the /alerts message): open or close a section, or subscribe
        or unsubscribe a type or all. In a group only admins may change them."""
        query = update.callback_query
        parts = (query.data or "").split(":")
        action, arg, section = (parts + ["", "", ""])[1:4]
        if action == "noop" or not self.state:
            await query.answer()
            return
        message = query.message
        chat = message.chat
        if chat.type != ChatType.PRIVATE:
            member = await context.bot.get_chat_member(chat.id, query.from_user.id)
            if member.status not in ("administrator", "creator"):
                await query.answer("Only group admins can change alerts")
                return
        kinds = self._alert_kinds()
        toast, opened = None, section or None
        if action in ("open", "close"):
            opened = arg if action == "open" else None
            if opened in ("sub", "unsub") and not menu.options(self.state.muted(chat.id), kinds, opened):
                await query.answer("You're subscribed to everything" if opened == "sub" else "No alerts are on")   # nothing to list
                return
        elif action in ("on", "off") and (arg == menu.ALL or arg in kinds) and chat.id in self.state.chats:
            self.state.set_kind(chat.id, arg, action == "on")
            what = "All alerts" if arg == menu.ALL else f"{menu.LABELS[arg].capitalize()} alerts"
            toast = f"{what} {action} in this chat"
        else:
            await query.answer()
            return
        muted = self.state.muted(chat.id)
        if opened in ("sub", "unsub") and not menu.options(muted, kinds, opened):
            opened = None                                    # the last one was just turned on or off: close the section
        markup = menu.keyboard(muted, kinds, opened if opened in ("sub", "unsub") else None)
        await query.answer(toast)
        try:
            if (message.text or "").startswith(menu.TITLE):   # the /alerts message: keep its on/off summary current
                await query.edit_message_text(menu.title(muted, kinds), reply_markup=markup)
            else:                                             # an alert: only its buttons change
                await query.edit_message_reply_markup(reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    async def on_period_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A press on 7d / 30d / 90d / 365d under a chart: ask the chart's question again for that period and put the new chart
        in the same message."""
        query = update.callback_query
        days = periods.days_in(query.data)
        message = query.message
        question = self.charted.get(message.chat_id, message.message_id) if days and message else None
        if not question:
            await query.answer("That chart is out of date, please ask again")
            return
        now = now_local(self.cfg.tz)
        if intent.period_days(question, now) == days:   # the chart already shows this period
            await query.answer()
            return
        await query.answer(f"Drawing {days} days...")
        asked = intent.with_period(question, days, now)
        log.info("Period button: %s -> %s", _short(question, 40), _short(asked, 40))
        if cached := self.images.get(self.images.key(asked, days, now)):   # drawn lately: no new question
            png, title = cached
            try:
                await message.edit_media(InputMediaPhoto(png, caption=title or None), reply_markup=periods.keyboard())
                self.charted.remember(message.chat_id, message.message_id, asked)
                log.info("Period button: chart from the cache")
                return
            except TelegramError as e:
                log.warning("Couldn't edit the chart in place (%s); drawing it again", e)
        await self.respond(update, context, asked, redraw=message)

    async def _redraw(self, message: Message, reply: str, photo: bytes, title: str, row, question: str) -> bool:
        """Put a new chart into the message a period button was pressed under (its answer text, if any, follows as a message of
        its own). False when Telegram refuses, and the chart is then sent as a new message."""
        try:
            await message.edit_media(InputMediaPhoto(photo, caption=title or None), reply_markup=row)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.warning("Couldn't edit the chart in place (%s); sending a new one", e)
                return False
        self.charted.remember(message.chat_id, message.message_id, question)
        if reply.strip():
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
        msg = update.effective_message
        self.chats.pop(self._key(msg), None)
        log.info("/reset in %s", describe_source(update))
        await msg.reply_text("Conversation memory cleared.")

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.remember_chat(update)
        log.info("/start in %s", describe_source(update))
        private = update.effective_chat.type == ChatType.PRIVATE
        await update.effective_message.reply_text(
            HELP.format(user=update.effective_user.id, chat=update.effective_chat.id),
            reply_markup=templates.keyboard() if private else None)
        if private and self.state:
            self.state.set_keyboard(update.effective_chat.id, templates.VERSION)

    async def refresh_keyboards(self, tg_bot) -> int:
        """At startup: tell each private chat whose buttons are out of date that they changed, with the new keyboard (silently).
        A chat that hid them is left alone. Returns how many were sent."""
        if not self.state:
            return 0
        sent = 0
        for chat_id in [c for c in self.state.chats if c > 0 and self.state.keyboard(c) not in (templates.VERSION, templates.HIDDEN)]:
            try:
                await tg_bot.send_message(chat_id, "Buttons updated.", reply_markup=templates.keyboard(), disable_notification=True)
                self.state.set_keyboard(chat_id, templates.VERSION)
                sent += 1
            except Forbidden as e:  # blocked the bot
                self.state.remove_chat(chat_id, f"can't post: {e}")
            except TelegramError as e:
                log.warning("Buttons update to %s failed: %s", chat_id, e)
        if sent:
            log.info("Buttons: told %d private chat(s) the buttons changed (keyboard %s)", sent, templates.VERSION)
        return sent

    def _keyboard_stale(self, msg: Message) -> bool:
        """Does this private chat need the current buttons (it has never had them, or they have changed since)? Not when it
        hid them."""
        return bool(self.state and msg.chat.type == ChatType.PRIVATE
                    and self.state.keyboard(msg.chat_id) not in (templates.VERSION, templates.HIDDEN))

    async def on_keyboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/keyboard shows the buttons again, /keyboard off hides them (private chats only)."""
        msg = update.effective_message
        if update.effective_chat.type != ChatType.PRIVATE:
            await msg.reply_text("The buttons are only in private chats.")
        elif context.args and context.args[0].lower() == "off":
            await msg.reply_text("Buttons hidden. /keyboard shows them again.", reply_markup=ReplyKeyboardRemove())
            if self.state:
                self.state.set_keyboard(msg.chat_id, templates.HIDDEN)
        else:
            await msg.reply_text("Here are the buttons.", reply_markup=templates.keyboard())
            if self.state:
                self.state.set_keyboard(msg.chat_id, templates.VERSION)

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
        waited = (datetime.now(timezone.utc) - msg.date).total_seconds() if getattr(msg, "date", None) else 0
        if waited > PENDING_MAX_SECONDS:
            log.info("Ignored a message %d minutes old from %s: %s", waited // 60, describe_source(update), _short(msg.text, 40))
            return
        self.remember_chat(update)
        if msg.chat.type == ChatType.PRIVATE:
            if msg.text.strip() == templates.CAPABILITIES:  # what it measures, then the alert settings (no model needed)
                await msg.reply_text(templates.capabilities_text("Ecowitt" in self.by_name, "AirGradient" in self.by_name,
                                                                      "Pollen" in self.by_name, "Forecast" in self.by_name))
                await self.on_alerts(update, context)
            else:
                await self.respond(update, context, templates.sentence(msg.text) or msg.text)
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

    async def _written_in_code(self, read, turn: Turn) -> str:
        """The report ("weather now") from the fast path's tool calls, made together; lib/report.py lays it out."""
        calls = [read.fast[:2], *read.more]
        results = await asyncio.gather(*(self.agent.tools.call(name, json.dumps(args), turn) for name, args in calls))
        by_tool = {name: result for (name, _), result in zip(calls, results)}
        if read.report:
            return report.report(by_tool)
        return report.weather_now(by_tool["weather_now"]) or "The weather station isn't answering right now."

    async def _warn(self, msg: Message, tokens: int):
        """A heads-up before a big job: the question has pulled in a lot of data. Sent now, silently; the answer follows by itself."""
        text = f"\u26a0\ufe0f That's pulling in about {tokens / 1000:.0f}k tokens of data, so it will take a little longer. Working on it..."
        log.info("Heads-up sent: %s", text)
        with contextlib.suppress(TelegramError):
            await msg.reply_text(text)

    async def _chart_in_code(self, read, turn: Turn) -> str | None:
        """A chart asked for plainly: the tool draws it and the caption is written here. None (the model then takes the
        question) when there is nothing to caption."""
        calls = [read.fast[:2], *read.more]
        results = await asyncio.gather(*(self.agent.tools.call(name, json.dumps(args), turn) for name, args in calls))
        caption = report.chart_caption(read.fast[0], read.fast[1], results[0], turn, self.cfg.tz,
                                       results[1] if len(results) > 1 else None)
        if caption is None:
            turn.charts.clear()
            turn.forecast_shown.clear()
        return caption

    async def _lookup_in_code(self, read, turn: Turn) -> str | None:
        """A plain lookup (a reading, the air, the pollen, the forecast, what the bot can do) answered from its one tool result; None
        (the model then takes the question) when it can't be."""
        if read.lookup == "about":
            return templates.capabilities_text("Ecowitt" in self.by_name, "AirGradient" in self.by_name,
                                               "Pollen" in self.by_name, "Forecast" in self.by_name)
        name, args = read.fast[:2]
        result = await self.agent.tools.call(name, json.dumps(args), turn)
        return report.lookup(read.lookup, result, read.lookup_arg, read.sides)

    async def _ask_model(self, working: list[dict], system: str, read, turn: Turn, draft, on_heavy=None, conv_id=None) -> str:
        """The model's answer. If it has not answered in TURN_SECONDS, ask again once with one reasoning step less (medium > low >
        none), for RETRY_SECONDS; a question already at no reasoning just times out."""
        first = [*([read.fast[:2], *read.more] if read.fast else []), *read.extra] or None
        before, effort, budget, retried = list(working), read.effort, TURN_SECONDS, False
        while True:
            try:
                return await asyncio.wait_for(
                    self.agent.run(working, system, effort, first_call=first, require_tool=read.needs_data,
                                   no_tools=read.about_the_bot, turn=turn, on_heavy=on_heavy, conv_id=conv_id,
                                   **({"on_text": draft.update} if draft else {})), budget)
            except asyncio.TimeoutError:
                lower = intent.lower_effort(effort)
                if not lower or retried:
                    raise
                log.warning("No answer in %ds at reasoning %s; asking again at %s", budget, effort, lower)
                working[:] = before          # the unfinished attempt's turns are dropped
                effort, budget, retried = lower, RETRY_SECONDS, True

    async def respond(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, redraw: Message | None = None):
        msg = update.effective_message
        text = text.strip()
        if not text:
            return
        stop_typing = asyncio.Event()   # "typing..." from the first moment, even while this question waits its turn
        typing = None if redraw is not None else asyncio.create_task(   # a chart redrawn in place from a button shows no "typing..."
            keep_typing(context.bot, msg.chat_id, self._thread(msg), stop_typing))
        await asyncio.sleep(0)
        now = now_local(self.cfg.tz)
        read = intent.read(text, now, "Ecowitt" in self.by_name, "AirGradient" in self.by_name, "Pollen" in self.by_name,
                           "Forecast" in self.by_name)
        log.info("%s %s%s", describe_source(update), f"(reasoning: {read.effort}) " if read.effort != intent.EFFORT_DEFAULT else "",
                 _short(text))
        for source in self.sources:  # fetch recent readings while the model thinks
            if source.wants(text):
                source.poke()
        if read.fast:
            log.info("Fast path: %s", read.fast[2])

        chat = self.chats[self._key(msg)]
        thread_id = self._thread(msg)
        started = time.monotonic()
        async with chat.lock:
            draft = (Draft(context.bot, msg.chat_id, thread_id)
                     if redraw is None and msg.chat.type == ChatType.PRIVATE and hasattr(context.bot, "send_message_draft") else None)
            drafting = asyncio.create_task(draft.run(stop_typing)) if draft else None   # a private chat also gets the "Thinking..." draft
            stuck = asyncio.create_task(watchdog(_short(text, 40), WATCHDOG_SECONDS))
            working = [*chat.history, {"role": "user", "content": self._content(msg, text, context.bot.id)}]
            new_from = len(working)
            ok, photos, titles, silent = True, [], [], False
            turn = Turn(chart_asked=read.chart_asked, chart_field=read.chart_field, chart_fields=read.chart_fields,
                        average_asked=read.average_asked, readings=read.readings, per_day=read.per_day, text=text)
            try:
                reply = None
                if read.fast and read.chart_in_code:   # drawn by the tool; the chart goes out with just its title (the text is kept for the history)
                    reply = await asyncio.wait_for(self._chart_in_code(read, turn), TURN_SECONDS)
                    silent = reply is not None
                elif read.lookup:
                    reply = await asyncio.wait_for(self._lookup_in_code(read, turn), TURN_SECONDS)
                    if reply is None:
                        turn.forecast_shown.clear()
                elif read.fast and (read.report or read.weather_now):   # written in code from the tools' results, no model
                    reply = await asyncio.wait_for(self._written_in_code(read, turn), TURN_SECONDS)
                if reply is not None:
                    working.append({"role": "assistant", "content": reply})
                else:
                    before = [str(m["content"]) for m in chat.history if m["role"] == "user"][-2:]
                    found = intent.topics(str(working[new_from - 1]["content"]), before)
                    system = prompt.build(datetime.now(self.cfg.tz), [s.describe() for s in self.sources], read.hints,
                                          read.about_the_bot, read.rain_caption, found)
                    reply = await self._ask_model(working, system, read, turn, draft, lambda tokens: self._warn(msg, tokens),
                                              f"ecobot-{msg.chat_id}")
                chat.history = shorten_old(trim_history(strip_tool_turns(working)))
                for spec in turn.charts[:MAX_CHARTS]:  # drawn while "typing..." is still showing
                    try:
                        photos.append(await asyncio.to_thread(render_chart, spec, self.cfg.tz))
                        titles.append(spec.caption)
                    except Exception:
                        log.exception("Chart failed; sending the answer without it")
            except asyncio.TimeoutError:
                log.error("Gave up after %ds on: %s", TURN_SECONDS, _short(text, 60))
                ok, reply = False, "Sorry, that took too long. Please try again in a moment."
            except Exception as e:
                log.exception("Agent error")
                # The details (which can include provider error bodies) go to the log, not the chat
                ok, reply = False, f"Sorry, something went wrong on my side ({type(e).__name__}). Please try again in a moment."
            finally:
                stuck.cancel()
                stop_typing.set()
                await asyncio.gather(*[t for t in (typing, drafting) if t])  # wait for any in-flight "typing" or draft so none is sent after the reply

        if silent and photos:
            reply = ""
        if ok and not reply.strip() and not photos:   # a chart with no text that could not be drawn
            reply = "Sorry, I couldn't draw that chart. Please try again in a moment."
        used = [m for m in working[new_from:] if m["role"] == "tool"]
        log.info("%s %s in %.1fs, tools %d, charts %d, %d chars: %s", "Replied" if ok else "Error reply", msg.chat_id,
                 time.monotonic() - started, len(used), len(photos), len(reply), _short(reply, 30))
        used_air = any(tc["function"]["name"] == "air_quality"
                       for m in working[new_from:] for tc in m.get("tool_calls") or [])
        air = self.by_name.get("AirGradient")
        markup = templates.keyboard() if self._keyboard_stale(msg) else None
        row = periods.keyboard() if len(photos) == 1 else None   # period buttons under a single chart
        sent_photo = []
        if row and ok and not reply.strip():   # a chart and nothing else: keep it for the period buttons
            self.images.put(self.images.key(text, intent.period_days(text, now), now), photos[0], titles[0])
        try:
            if redraw is not None and row and await self._redraw(redraw, reply, photos[0], titles[0], row, text):
                reply, photos, row = "", [], None
            carried_keyboard = await deliver(msg, reply, photos, link=air.link if air and used_air else None, markup=markup,
                                             titles=titles, period_row=row, sent_photo=sent_photo)
            if sent_photo:
                self.charted.remember(msg.chat_id, sent_photo[0].message_id, text)
            if self.state and ok and turn.forecast_shown:   # it was sent: a later revision of these days is worth telling this chat
                self.state.record_forecast(msg.chat_id, turn.forecast_shown)
            if carried_keyboard:
                log.info("Buttons: sent keyboard %s to chat %s (it had %s)", templates.VERSION, msg.chat_id, self.state.keyboard(msg.chat_id))
                self.state.set_keyboard(msg.chat_id, templates.VERSION)
            elif markup and not row:
                log.info("Buttons: this reply couldn't carry the keyboard (several charts); will try the next one")
        except TelegramError:
            log.exception("Failed to deliver reply")
