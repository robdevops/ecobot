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
from collections.abc import Coroutine
from dataclasses import dataclass, field, replace
from datetime import datetime, UTC
from typing import NamedTuple

from telegram import BotCommand, InputMediaPhoto, Message, ReplyKeyboardRemove, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import BadRequest, ChatMigrated, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import intent, periods, prompt, report, report_menu, templates
from .admins import Admins
from .alerts import menu, pictures
from .alerts.notify import AlertState
from .irrigation import IrrigationError, Watchdog
from .irrigation import menu as irrigation_menu
from . import gauges
from .charts import render_data
from .service import Service
from .service import legacy
from .service.common import AS_DRAWN, BACKGROUND
from .service.context import Context
from .specs import from_json, to_json
from . import gauge_sheet
from .ecowitt.query import chart_field, stack_names
from .tools import Turn
from .config import Config
from .entities import utf16_len, with_link
from .sources import Source
from .timeutil import clock_text, day_text, now_local
from .llm import Agent, shorten_old, strip_tool_turns, trim_history
from .topicmemory import TopicMemory
from .logs import BUGS, QUOTES, fit

log = logging.getLogger(__name__)

TG_LIMIT = 4000
CAPTION_LIMIT = 1024   # what Telegram lets a photo's caption hold
MAX_CHARTS = 3
DAY_TOOLS = ("weather_days", "air_days")
ANSWERING_CHARTS = ("weather_history", "air_quality", *DAY_TOOLS)   # the tools whose chart can be the whole answer (chart_answers_it)
RETRY_SECONDS = 60           # the second try, one reasoning step lower, after a question timed out
PENDING_MAX_SECONDS = 10 * 60   # a message that waited longer (the bot was down) is not answered: its moment has passed
TURN_SECONDS = 90            # a question that takes longer is given up on, so the ones queued behind it in the chat are not stuck
DRAFT_REFRESH_SECONDS = 20   # Telegram drops a draft 30 s after its last update, so it is re-sent before that
DRAFT_MIN_GAP = 1.0          # at most one draft update a second while the answer streams in
AHEAD_SECONDS = 300          # the keyboard's charts are drawn ahead this often (draw_buttons_ahead), so a press sends one already drawn
AHEAD_KEEP_SECONDS = 60      # ... unless the chart was drawn for a question this lately
USED_SECONDS = 900           # the bot counts as in use this long after a message or a press: the charts drawn ahead are then uploaded ahead too
IRRIGATION_SETTLE_SECONDS = 3   # after any irrigation action, the controller is given this long to report its new state before it is read and the message redrawn
NOT_ADMIN = "Sorry, only group admins can use this bot."
NOT_ADMIN_TOAST = "Only group admins can use this bot"
STALE_BUTTON = "That button is out of date, please ask again"
CHART_FAILED = "Sorry, I couldn't draw that chart, please ask again"   # a period button's chart that couldn't be drawn or sent
MAX_CALLBACK_BYTES = 64       # Telegram's own limit on a button's data
TOAST_LIMIT = 200             # ... and on the text a button's answer shows

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
    charts: list[dict] = field(default_factory=list)   # the charts a service tool drew, as data (each with its drawn_by), still to draw
    drawn_by: list = field(default_factory=list)       # each photo's drawn_by (the call that draws it again), None for one with none
    key: str | None = None                   # the call a chart made in code was read as (periods.call_key): the picture cache's key


@dataclass
class Made:
    """What a service tool gave for a question answered in code: its words (a caption, an answer, lines) and its charts as data."""
    text: str | None
    charts: list[dict] = field(default_factory=list)


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


async def toast_quietly(query, text: str):
    """answer_query with a toast for a press that goes no further (a refusal): Telegram not taking it, for whatever reason, changes nothing."""
    with contextlib.suppress(TelegramError):
        await answer_query(query, text)


async def _edit_quietly(editing):
    """Await an edit of a message (edit_message_text and the like) and return what it gives; Telegram refusing an edit that changes
    nothing ("message is not modified") is fine: None then."""
    try:
        return await editing
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise
        return None


def _short(text: str, limit: int) -> str:
    """The text on one line, cut to `limit` characters (logs.fit): a question or button's data as the log names it."""
    return fit(" ".join(text.split()), limit)


def chart_times(times: dict, made: float | None, pngs: list[bytes], sending: float) -> str:
    """A reply's chart timings for the log: "data 12, calc 3, draw 98, PNG 41 (23 KB), send 812" (ms). times is the question's Turn.times;
    made is how long the tool took when the chart was made in code (the calculation is that less its data), else None. A step that
    wasn't timed is left out."""
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


# The notes a caption written here passes on itself (report.chart_caption, report.lookup); any other note needs the model's words
CAPTIONED_NOTES = {"weather_history": {"note_missing", "warning", "missing"}, "air_quality": {"note_missing", "warning", "missing"},
                   **{tool: {"note_averaged"} for tool in DAY_TOOLS}}


def _drew_chart(result: str) -> bool:
    """Does this tool result say it drew a chart (the "chart" note a tool adds when it does)?"""
    try:
        data = json.loads(result)
    except ValueError:
        return False
    return isinstance(data, dict) and "chart" in data and "error" not in data


def _needs_words(tool: str, result: str) -> bool:
    """Does this result carry a note the person should hear that a caption written here would leave out (a month's figures without
    dates, the older days held only as daily records or hourly averages, a rarer day that nearly counted ...)? Then the model answers."""
    data = json.loads(result)
    notes = {k for k in data if k.startswith("note") or k.endswith("_note") or k in ("warning", "missing", "exact_from", "trace_rain_days")}
    return bool(notes - CAPTIONED_NOTES.get(tool, set()))


def _as_drawn(tool: str, args: dict, turn: Turn) -> tuple[dict, Turn]:
    """(args, turn) as the tool read them to draw its chart, so its caption speaks of what the chart shows: the readings the person's
    words name, else the model's (air quality: its metrics, else those named, else PM2.5), and an average if either asked for one."""
    if tool == "air_quality":
        return {**args, "metrics": args.get("metrics") or turn.air_named or ["pm2_5"]}, turn
    return args, replace(turn, chart_field=chart_field(args, turn), chart_fields=stack_names(args, turn),
                         average_asked=turn.average_asked or bool(args.get("average")))


def chat_tag(chat_id: int) -> str:
    """"[Telegram 5467329077]": what every log line about a chat starts with, so a request and its reply are found together."""
    return f"[Telegram {chat_id}]"


def describe_source(update: Update) -> str:
    """The chat and who wrote: "[Telegram 5467329077]: @rob" in a private chat, "[Telegram -1001234]: @rob, group 'Weather'" in a group."""
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    who = (f"@{user.username}" if user.username else user.full_name) if user else "unknown"
    tag = chat_tag(msg.chat_id if msg is not None else chat.id)   # (the reply's line takes it from the message too)
    return f"{tag}: {who}" + ("" if chat.type == ChatType.PRIVATE else f", group '{chat.title}'")


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


def _first_fitting(pieces: list[str], glue: str):
    """(caption, entities) of as many of the first `pieces` (joined by `glue`) as fit a caption; None when even the first doesn't."""
    for n in range(len(pieces), 0, -1):
        if fitted := caption_of(glue.join(pieces[:n]), bold_headings=True):
            return fitted
    return None


def report_caption(text: str):
    """(caption, entities) for the report under its gauges: the whole report, else as many of its first sections as fit a caption, else as
    many of its first section's lines (only a report longer than any yet, refreshed in its photo, needs cutting: a new one that long goes
    apart from its picture)."""
    sections = text.split("\n\n")
    return _first_fitting(sections, "\n\n") or _first_fitting(sections[0].splitlines(), "\n") or (text.splitlines()[0][:CAPTION_LIMIT // 2], [])


async def _photos(msg: Message, photos: list[bytes], titles: list[str], buttons, alone: bool) -> tuple[bool, Message | None]:
    """Send the charts, each with its title as its caption; a single one carries `buttons`. (whether a single one went, as sent).
    alone: no text goes with them, so a failure is said in the chat."""
    caption = lambda i: (titles[i] if i < len(titles) else "") or None
    try:
        if len(photos) == 1:
            return True, await msg.reply_photo(photos[0], caption=caption(0), **({"reply_markup": buttons} if buttons else {}))
        await msg.reply_media_group([InputMediaPhoto(p, caption=caption(i)) for i, p in enumerate(photos)])
    except TelegramError as e:
        log.error("Couldn't send the chart: %s: %s", type(e).__name__, e)
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
        except TelegramError as e:
            log.error("Couldn't send the chart with its answer (%s: %s); sending them apart", type(e).__name__, e)
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


class Tasks:
    """The tasks the bot runs in the background: one that stops with an error is logged as it does, and close() cancels them."""

    def __init__(self):
        self.running: set[asyncio.Task] = set()

    def start(self, work: Coroutine) -> asyncio.Task:
        task = asyncio.create_task(work, name=getattr(work, "__qualname__", None))
        self.running.add(task)
        task.add_done_callback(self._ended)
        return task

    def _ended(self, task: asyncio.Task):
        self.running.discard(task)
        if not task.cancelled() and (error := task.exception()):
            log.error("%s stopped: %s: %s", task.get_name(), type(error).__name__, error, exc_info=error if isinstance(error, BUGS) else None)

    async def close(self):
        for task in self.running:
            task.cancel()
        await asyncio.gather(*self.running, return_exceptions=True)


class Bot:
    def __init__(self, cfg: Config, agent: Agent, sources: list[Source], state: AlertState, irrigation=None, tasks: Tasks | None = None,
                 service: Service | None = None):
        """state: the known chats and their settings. irrigation: the Tuya client of the irrigation controller, when one is
        configured (its button replaces Wind). tasks: where the bot's background work runs (the uploads of a chart's other periods);
        its own when none is given. service: the service's tools (lib/service), which the bot's own code paths call (its shortcuts,
        lookups, report, alert charts and period buttons); built over `sources` when none is given. The model is offered the old
        tools (agent.tools), not these, until step 3."""
        self.cfg, self.agent, self.sources, self.state, self.irrigation = cfg, agent, sources, state, irrigation
        self._service = service
        self.tasks = tasks or Tasks()
        self.watchdog = Watchdog(irrigation, state) if irrigation else None   # closes the valve after a run, however it ends
        self.keyboard_version = templates.version(irrigation is not None)
        self.admin_only = bool(getattr(cfg, "admin_only", False))   # ADMIN_ONLY: only the admins of the groups the bot is in may use it
        self.admins = Admins(state, getattr(cfg, "admin_chat_ids", ()))
        self._refused: set[int] = set()   # the senders ADMIN_ONLY has turned away, each logged once with its id (for ADMIN_CHAT_IDS)
        self.chats: dict[tuple, ChatState] = defaultdict(ChatState)
        self.by_name = {s.name: s for s in sources}
        self.images = periods.ImageCache()   # charts drawn lately, so toggling the period buttons is instant
        self.alert_charts = pictures.AlertCharts(self.images, lambda: now_local(self.cfg.tz),
                                                 tz=getattr(cfg, "tz", None))   # the alerts' charts, kept with them
        self.preloading: set[str] = set()        # the calls whose other periods are being uploaded (TELEGRAM_PRELOAD_CHAT)
        self.last_used = 0.0                     # when a message or a press last came in (monotonic; 0: never)
        self.preload_off = False                 # Telegram refused an upload to that chat (not a flood limit or a timeout): no more are tried until restart
        self.charted = periods.Charted(state.charts, state.save)   # the call behind each chart sent (kept across restarts)

    @property
    def service(self) -> Service:
        """The service's tools over the bot's sources (built on first use when none was given)."""
        if self._service is None:
            get = self.by_name.get
            self._service = Service.of(Context(eco=get("Ecowitt"), air=get("AirGradient"), forecast=get("Forecast"), pollen=get("Pollen"),
                                               tz=getattr(self.cfg, "tz", None)))
        return self._service

    # ---------- the service's tools, as the bot calls them ----------
    async def _service_call(self, tool: str, args: dict, ahead: bool = False) -> dict:
        """A service tool's answer as data: its charts as drawn (AS_DRAWN, so its pictures are as they were),
        a chart drawn ahead (nobody waiting) as a background request. {"error": ...} when it refuses or fails."""
        found = self.service.get(tool)
        if found is None:
            return {"error": f"There is no {tool} here."}
        meta = {"chart_points": AS_DRAWN, **({"priority": BACKGROUND} if ahead else {})}
        log.debug("Service call %s %s", tool, json.dumps(args, ensure_ascii=False), extra=QUOTES)
        try:
            out = json.loads(await found.call(args, meta))
        except Exception as e:
            log.error("Service tool %s failed: %s: %s", tool, type(e).__name__, e, exc_info=isinstance(e, BUGS))
            return {"error": f"{type(e).__name__}: {e}"}
        return out if isinstance(out, dict) else {"error": "not an answer"}

    def _call_of(self, read, turn: Turn, now: datetime) -> tuple[str, dict] | None:
        """The service's call for the fast path's old tool call (lib/service/legacy.py), or None."""
        if not read.fast:
            return None
        try:
            return self._held_call(legacy.translate(read.fast[0], read.fast[1], turn, now=now, tz=self.cfg.tz))
        except Exception as e:
            log.error("Couldn't read %s as a service call (%s: %s)", read.fast[0], type(e).__name__, e, exc_info=isinstance(e, BUGS))
            return None

    def _held_call(self, call: tuple[str, dict] | None) -> tuple[str, dict] | None:
        """The call with its readings (full names) cut to those the service holds: the old tools read whichever groups the station has,
        so "temperature" was outdoor and indoor, and a side with nothing held was left out quietly."""
        if call is None or call[0] != "summary" or not isinstance(call[1].get("readings"), list):
            return call
        held = self.service.held(call[1]["readings"])
        return (call[0], {**call[1], "readings": held}) if held else call

    def _model_drawn_by(self, turn: Turn, i: int, now: datetime) -> dict | None:
        """A drawn_by for the i-th chart the model drew through the old tools (it has none of its own until step 3): its call read as the
        service's, with the periods that tool would list; None when the call has no service equivalent or lists no periods (its question
        is asked again)."""
        if i >= len(turn.chart_calls):
            return None
        name, args = turn.chart_calls[i]
        try:
            call = self._held_call(legacy.translate(name, args if isinstance(args, dict) else {}, turn, now=now, tz=self.cfg.tz))
        except Exception as e:
            log.debug("The model's %s chart has no service call (%s: %s)", name, type(e).__name__, e)
            return None
        if call is None or call[0] not in ("summary", "days"):
            return None
        tool, arguments = call[0], {k: v for k, v in call[1].items() if k != "chart_answers_it"}
        if tool == "summary":
            arguments["chart"] = True
        listed = legacy.periods_of(tool, arguments)
        if not listed:   # a period the service can't word (times...): its question is asked again, so the chart keeps its buttons
            return None
        return {"tool": tool, "arguments": arguments, "periods": listed}

    def _sentence_call(self, text: str, now: datetime) -> tuple[str, dict] | None:
        """The call a question is read as when it is a chart made in code ("Wind chart 1d"); None for any other."""
        read = intent.read(text, now, self.by_name)
        return self._call_of(read, Turn.of(read, text), now) if read.chart_in_code else None

    def _draw(self, chart: dict, times: dict | None = None) -> tuple[bytes, str, int]:
        """(PNG, title, the time of its newest reading) of a chart given as data."""
        spec = from_json(chart)
        return render_data(chart, self.cfg.tz, times), spec.caption(now_local(self.cfg.tz).date()), spec.newest

    async def _draw_call(self, tool: str, args: dict, ahead: bool = True) -> tuple[bytes, str, str | None, int, dict | None, dict] | None:
        """(PNG, title, its words, newest reading, drawn_by, the answer) of the chart a call draws; None when it draws none (the refusal
        logged)."""
        out = await self._service_call(tool, args, ahead=ahead)
        charts = out.get("charts") or []
        if "error" in out or len(charts) != 1:
            log.debug("No chart from %s %s: %s", tool, args, out.get("error"))
            return None
        png, title, newest = await asyncio.to_thread(self._draw, charts[0])
        return png, title, out.get("caption") or out.get("answer"), newest, charts[0].get("drawn_by"), out

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

    @staticmethod
    def _sender(update: Update) -> tuple[str, int, str] | None:
        """("user", id, full name) of who sent this, for the log; ("chat", id, title) for a message an admin sends "as the group" (or as a
        channel), which names no real user."""
        msg = update.effective_message
        if update.callback_query is None and msg is not None and getattr(msg, "sender_chat", None) is not None:
            return "chat", msg.sender_chat.id, msg.sender_chat.title
        return ("user", update.effective_user.id, update.effective_user.full_name) if update.effective_user else None

    async def _allowed(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """With ADMIN_ONLY on, only known group admins (and ADMIN_CHAT_IDS) get through; anyone else is told so, once, and False is
        returned. A group the bot hears from is remembered first: it has to be known for its admins to count."""
        chat = update.effective_chat
        if chat is not None and chat.type != ChatType.PRIVATE:
            self.remember_chat(update)
        if not self.admin_only or await self.admins.allowed(context.bot, self._sender_ids(update)):
            return True
        if (who := self._sender(update)) and who[1] not in self._refused:   # once per sender in this run: the id is for the owner to list
            self._refused.add(who[1])
            log.info("%s: refused: %s %d (%s) is not a known group admin; add the id to ADMIN_CHAT_IDS to let them in", describe_source(update), *who)
        if update.callback_query is not None:
            await toast_quietly(update.callback_query, NOT_ADMIN_TOAST)
        elif update.effective_message is not None:
            with contextlib.suppress(TelegramError):
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
            await toast_quietly(query, STALE_BUTTON)
        return believed

    def noticed(self):
        """A message or a press came in: the bot is in use (used_lately)."""
        self.last_used = time.monotonic()

    @property
    def used_lately(self) -> bool:
        return bool(self.last_used) and time.monotonic() - self.last_used <= USED_SECONDS

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
        self.noticed()
        handler = next((h for prefix, h in self.buttons().items() if query.data.startswith(prefix)), None)
        if handler is None:
            await toast_quietly(query, STALE_BUTTON)
            return
        if not await self._allowed(update, context):
            return
        try:
            await handler(update, context)
        except ChatMigrated as e:   # a press on an alert from before its group became a supergroup (the chat has a new id)
            self.state.migrate_chat(query.message.chat.id, e.new_chat_id)
            await toast_quietly(query, "This group was upgraded to a supergroup: please use the buttons on new alerts")

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
                log.info("%s: /alerts %s", describe_source(update), arg)
        muted, kinds = self.state.muted(chat.id), self._alert_kinds()
        loud = self.state.loud(chat.id) if chat.type == ChatType.PRIVATE else None   # sound settings: private chats only
        await msg.reply_text(menu.title(muted, kinds), reply_markup=menu.keyboard(muted, kinds, loud=loud))

    async def _help_and_alerts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """What the bot can do, then the alert settings: the Help & Alerts button and /usage."""
        about = await self._service_call("about", {})
        await update.effective_message.reply_text("\n".join(about.get("lines") or []) or templates.capabilities_text(self.by_name))
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
        its sound on or off. In a group only admins may change them. Refresh, under an alert's chart, changes nothing: anyone may press it."""
        query = update.callback_query
        action, *rest = (query.data or "").split(":")[1:] or [""]   # the grammar is in alerts/menu.py
        kind = parent = opened = None
        if action == "refresh" and len(rest) == 1:
            await self._redraw_alert(query, rest[0])
            return
        if action == "open" and len(rest) in (1, 2):              # al:open:<menu>[:<parent>]
            opened, parent = rest[0], rest[1] if len(rest) == 2 else None
        elif action == "close" and len(rest) <= 1:                # al:close[:<parent>]
            parent = rest[0] if rest else None
        elif action in ("on", "off") and len(rest) in (2, 3):     # al:on|off:<kind>:<menu>[:<parent>]: the type's alerts
            action, on, kind, opened, parent = "alert", action == "on", rest[0], rest[1], rest[2] if len(rest) == 3 else None
        elif action == "snd" and len(rest) in (2, 3) and rest[1] in ("on", "off"):   # al:snd:<kind>:on|off[:<parent>]: its sound
            action, on, kind, parent = "sound", rest[1] == "on", rest[0], rest[2] if len(rest) == 3 else None
            opened = menu.BELL if parent else menu.SOUND
        else:
            await answer_query(query)
            return
        message = query.message
        chat = message.chat
        if chat.type != ChatType.PRIVATE and not await self._is_group_admin(context.bot, chat.id, query.from_user.id):
            await answer_query(query, "Only group admins can change alerts")
            return
        kinds = self._alert_kinds()
        if parent and parent not in kinds:   # parent: the buttons under an alert of that type (None: the /alerts message) the bot no longer sends
            await answer_query(query, STALE_BUTTON)
            return
        private = chat.type == ChatType.PRIVATE
        toast = None
        if action == "sound" and private and kind in kinds and chat.id in self.state.chats:
            self.state.set_sound(chat.id, kind, on)
            toast = f"{self._alerts_named(kind)} {'now make' if on else 'no longer make'} a sound"
        elif action == "alert" and kind in kinds and chat.id in self.state.chats:
            self.state.set_kind(chat.id, kind, on, kinds)
            toast = f"{self._alerts_named(kind)} {'on' if on else 'off'} in this chat"
        elif action not in ("open", "close"):
            await answer_query(query)
            return
        muted = self.state.muted(chat.id)
        loud = self.state.loud(chat.id) if private else None
        await answer_query(query, toast)
        if parent:   # an alert: only its buttons change
            await self._edit(query, None, menu.alert_keyboard(parent, muted, loud, opened, refresh=self._redrawable(message, parent)))
        else:        # the /alerts message: its on/off summary is kept current too
            await self._edit(query, menu.title(muted, kinds), menu.keyboard(muted, kinds, opened, loud))

    @staticmethod
    def _redrawable(message, kind: str) -> bool:
        """Does this alert carry a chart its Refresh can draw again (it went out as a picture)?"""
        return kind in pictures.CHARTS and bool(getattr(message, "photo", None))

    async def _redraw_alert(self, query, kind: str):
        """A press on Refresh under an alert's chart: the chart with the latest readings, in the same message: the one drawn ahead since the
        message was last changed (AlertCharts.newer_than; Telegram's copy when it was uploaded ahead), else drawn now and kept. The alert's
        text (what happened, as it was sent) and its buttons stay as they are."""
        message = query.message
        source = self.by_name.get(menu.TYPES[kind][1]) if kind in menu.TYPES else None
        if source is None or not self._redrawable(message, kind):   # an alert of a type, or from a source, the bot no longer has
            await answer_query(query, STALE_BUTTON)
            return
        photo, drawn = self.alert_charts.newer_than(kind, message), False
        if photo is None:
            try:
                picture = await asyncio.wait_for(pictures.chart(source, pictures.CHARTS[kind], source.tz, self.service), TURN_SECONDS)
            except TimeoutError:
                picture = None
            if picture is None:   # (pictures.chart has said why) the message stays as it was
                await answer_query(query, "Couldn't draw the chart, please try again", show_alert=True)
                return
            photo, drawn = picture.png, True
            self.alert_charts.keep(kind, picture)
        await answer_query(query)   # no toast: the chart is the answer
        edited = await _edit_quietly(query.edit_message_media(InputMediaPhoto(photo, caption=message.caption, caption_entities=message.caption_entities or None),
                                                              reply_markup=message.reply_markup))
        if drawn:
            self.alert_charts.sent(kind, edited, alerted=False)
        log.debug("%s: the %s alert's chart %s", chat_tag(message.chat.id), kind, "drawn again" if drawn else "sent from the one drawn ahead")

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

    async def _controller(self, query, request, what: str):
        """What the controller answers `request` with (its status, or the toast of an action). When it can't be reached: logged as
        "Irrigation {what} failed", said in a toast, and None (the press goes no further)."""
        try:
            return await request
        except IrrigationError as e:
            log.warning("Irrigation %s failed: %s", what, e)
            await answer_query(query, "Couldn't reach the irrigation controller", show_alert=True)
            return None

    async def _irrigation_act(self, action: str, arg: str, chat_id: int) -> str:
        """Do what a button asks. Returns the toast to show, or "" for a button that means nothing. Raises IrrigationError."""
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
        return ""

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
            status = await self._controller(query, self.irrigation.status(), "refresh")
            if status is None:
                return
            await answer_query(query, "Refreshed")
            await self._edit(query, *self._irrigation_view(status, message.chat_id))
            return
        if action == "water" and arg.isdigit() and int(arg) in irrigation_menu.WATER_MINUTES:   # not while the timer is paused
            status = await self._controller(query, self.irrigation.status(), f"water {arg}")
            if status is None:
                return
            if irrigation_menu.is_paused(status):
                log.info("Irrigation: water %s refused, paused for %s (chat %s)", arg, status["weather_delay"], message.chat_id)
                await answer_query(query, irrigation_menu.PAUSED_ERROR)
                text, markup = self._irrigation_view(status, message.chat_id)
                text, entities = irrigation_menu.with_error(text)
                await self._edit(query, text, markup, entities)   # nothing was sent: no wait
                return
        toast = await self._controller(query, self._irrigation_act(action, arg, message.chat_id), f"{action} {arg}")
        if toast is None:
            return
        if not toast:
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
        """Put new text (None: the text stays) and buttons into the message a button was pressed under."""
        if text is None:
            await _edit_quietly(query.edit_message_reply_markup(reply_markup=markup))
        else:
            await _edit_quietly(query.edit_message_text(text, reply_markup=markup, **({"entities": entities} if entities else {})))

    async def on_report_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A press on 🔄 under the report (or the dashboard: _redraw_dashboard): read everything again and redraw the report in the
        same message. The chat's conversation is not touched (this is not a new question)."""
        query = update.callback_query
        if query.data == report_menu.DASHBOARD:
            await self._redraw_dashboard(query)
            return
        read = intent.read("Report", now_local(self.cfg.tz), self.by_name)
        pictured = bool(getattr(getattr(query, "message", None), "photo", None))   # the report is the caption of its gauges: they are drawn again too
        try:
            text, gauge = await self._report(read, Turn(text="Report"), pictured)
        except Exception as e:   # the message stays as it was
            log.warning("Report refresh failed: %s", e)
            await answer_query(query, "Couldn't refresh, please try again", show_alert=True)
            return
        await answer_query(query)   # no toast: the times in the headings show what changed
        if pictured:
            await self._recaption(query, text, gauge)
        else:
            await self._edit(query, text, report_menu.keyboard(), report.heading_entities(text))

    async def _redraw_dashboard(self, query):
        """A press on 🔄 under the dashboard: its gauges drawn again with the latest readings, and the time in its title, in the same
        message. When the station doesn't answer, the message stays as it was."""
        eco = self.by_name.get("Ecowitt")
        if eco is None or not getattr(getattr(query, "message", None), "photo", None):   # no station any more, or not the dashboard's picture
            await answer_query(query, STALE_BUTTON)
            return
        try:
            answer = await asyncio.wait_for(self._dashboard(), TURN_SECONDS)
        except Exception as e:
            log.warning("Dashboard refresh failed: %s: %s", type(e).__name__, e)
            answer = None
        if answer is None or not answer.ok:
            await answer_query(query, "Couldn't refresh, please try again", show_alert=True)
            return
        await answer_query(query)   # no toast: the time in the title shows it was drawn again
        await _edit_quietly(query.edit_message_media(InputMediaPhoto(answer.photos[0], caption=answer.titles[0]),
                                                     reply_markup=report_menu.dashboard_keyboard()))

    @staticmethod
    async def _recaption(query, text: str, photo: bytes | None):
        """Put the report's new text, and its gauges drawn again, into the photo message its Refresh was pressed under (no new picture:
        the caption alone)."""
        body, ents = report_caption(text)
        if photo:
            await _edit_quietly(query.edit_message_media(InputMediaPhoto(photo, caption=body, caption_entities=ents or None), reply_markup=report_menu.keyboard()))
        else:
            await _edit_quietly(query.edit_message_caption(caption=body, caption_entities=ents or None, reply_markup=report_menu.keyboard()))

    async def on_period_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A press on a period button under a chart: the chart's call (its drawn_by) replayed with that period, by the service, and the
        new chart put in the same message (from the picture cache when it was drawn lately). A replay the service refuses shows its
        refusal on the button. A chart the service can't replay (one the model drew with a call it has no equivalent of) asks its
        question again for that period instead."""
        query = update.callback_query
        message = query.message
        record = self.charted.get(message.chat_id, message.message_id) if message else None
        if isinstance(record, dict) and record.get("drawn_by"):
            answered: list[str] = []   # what the press was answered with (Telegram takes one answer per press)
            try:
                await self._replay(query, message, record, context.bot, answered)
            except Exception as e:   # (a chart that fails to draw, Telegram failing): said, never raised
                log.error("Couldn't redraw a chart for its period button (%s: %s)", type(e).__name__, e, exc_info=isinstance(e, BUGS))
                if not answered:   # on the button
                    await toast_quietly(query, CHART_FAILED)
                else:   # the button already said "Drawing ...": a second answer would be refused, so in the chat
                    with contextlib.suppress(TelegramError):
                        await message.reply_text(CHART_FAILED)
            return
        days = periods.days_in(query.data)
        question = record if isinstance(record, str) and days else None
        if not question:
            await answer_query(query, "That chart is out of date, please ask again")
            return
        now = now_local(self.cfg.tz)
        if intent.period_days(question, now) == days:   # the chart already shows this period
            await answer_query(query)
            return
        await answer_query(query, f"Drawing {periods.NAMES[days]}...")
        asked = intent.with_period(question, days, now)
        log.debug("Period button %s: %s", periods.NAMES[days], question, extra=QUOTES)
        await self.respond(update, context, asked, redraw=message)

    async def _replay(self, query, message: Message, record: dict, bot, answered: list[str] | None = None):
        """A period button under a chart the service drew: its drawn_by replayed with the period pressed, into the same message.
        answered: given the "Drawing ..." toast once it is sent (the press is answered once, even when the cached chart can't be put
        in and it is drawn again)."""
        answered = [] if answered is None else answered

        async def drawing(label: str):
            if not answered:
                await answer_query(query, f"Drawing {label}...")
                answered.append(label)

        drawn_by, words = record["drawn_by"], bool(record.get("words"))
        period = periods.period_in(query.data)
        listed = {p["period"]: p["label"] for p in drawn_by.get("periods") or ()}
        reload = period == periods.RELOAD and not listed   # the 🔄 under a chart with no periods that runs to now: drawn again as it is
        if period not in listed and not reload:
            await answer_query(query, "That chart is out of date, please ask again")
            return
        now = now_local(self.cfg.tz)
        refresh = False   # the chart's own shaded 1D pressed: drawn again with the latest readings
        if not reload and periods.own_period(drawn_by, now, self.cfg.tz) == period:   # the chart already shows this period
            if period != periods.REFRESHED or not periods.ends_now(drawn_by, now, self.cfg.tz):   # (longer: a few minutes more barely show)
                await answer_query(query)
                return
            refresh = True
        tool = drawn_by["tool"]
        args = dict(drawn_by["arguments"]) if reload or refresh else periods.with_period(drawn_by, period)
        label = "again" if reload else listed[period]
        key = periods.call_key(tool, args)
        log.debug("Period button %s: %s", label, key, extra=QUOTES)
        if not (reload or refresh) and (held := self.images.held(key)) is not None and held.drawn_by:   # drawn lately: not asked again
            await drawing(label)
            began = time.monotonic()
            row = periods.periods_keyboard(held.drawn_by, period, now, self.cfg.tz)
            if await self._redraw(message, (held.caption or "") if words else "", held.photo, held.title, row,
                                  {"drawn_by": held.drawn_by, "words": words}, key, TelegramError, "drawing it again"):
                self.preload_soon(bot, held.drawn_by)
                log.debug("Period button: cached chart sent in %.0f ms%s", (time.monotonic() - began) * 1000,
                          " (Telegram's copy)" if isinstance(held.photo, str) else "")
                return
        try:
            out = await asyncio.wait_for(self._service_call(tool, args), TURN_SECONDS)
        except TimeoutError:
            out = {"error": "That took too long, please try again"}
        charts = out.get("charts") or []
        if "error" in out or not charts:
            await toast_quietly(query, fit(str(out.get("error") or "No chart came of it for that period"), TOAST_LIMIT))
            return
        png, title, newest = await asyncio.to_thread(self._draw, charts[0])   # (drawn before the toast, so a failure can be toasted)
        await drawing(label)
        drawn = charts[0].get("drawn_by") or {"tool": tool, "arguments": args, "periods": drawn_by.get("periods")}
        text = out.get("caption") or out.get("answer") or ""
        self.images.put(key, png, title, text or None, newest=newest, drawn_by=drawn)
        row = periods.periods_keyboard(drawn, period, now, self.cfg.tz)
        if not await self._redraw(message, text if words else "", png, title, row, {"drawn_by": drawn, "words": words}, key):
            sent = await deliver(message, text if words else "", [png], titles=[title], period_row=row)
            if sent.photo:
                self.charted.remember(message.chat_id, sent.photo.message_id, {"drawn_by": drawn, "words": words})
                self.images.sent(key, sent.photo)
        self.preload_soon(bot, drawn)

    async def _redraw(self, message: Message, reply: str, photo: bytes | str, title: str, row, record: str | dict, key: str | None = None,
                      refused=BadRequest, then: str = "sending a new one") -> bool:
        """Put a new chart into the message a period button was pressed under (its answer text, if any, follows as a message of
        its own, unless it fits the caption: then the chart's caption is its title and the answer, as deliver sends them). False when
        Telegram refuses (an error of the `refused` kind, logged with what is done `then`): the chart is then sent as a new message,
        or drawn again. key: where the image cache keeps it. record: what drew it (periods.Charted), remembered for its buttons."""
        text = strip_chart_talk(reply.strip())
        fitted = caption_of(text, title) if text else None
        in_caption = fitted is not None
        body, ents = fitted or (title, [])
        try:
            self.images.sent(key, await _edit_quietly(message.edit_media(InputMediaPhoto(photo, caption=(body if in_caption else title) or None,
                                                                                        caption_entities=(ents or None) if in_caption else None),
                                                                         reply_markup=row)))
        except refused as e:
            log.warning("Couldn't edit the chart in place (%s); %s", e, then)
            return False
        self.charted.remember(message.chat_id, message.message_id, record)
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
        log.info("%s: /reset", describe_source(update))
        await msg.reply_text("Conversation memory cleared.")

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._allowed(update, context):
            return
        self.remember_chat(update)
        log.info("%s: /start", describe_source(update))
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
            log.error("Unhandled error while handling a message: %s: %s", type(err).__name__, err, exc_info=isinstance(err, BUGS))

    # ---------- messages ----------
    async def on_other(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Service messages and other non-text updates: only logged when Telegram says a draft's generation was stopped
        (a field this library version doesn't know, so it is found by name)."""
        msg = update.effective_message
        extra = getattr(msg, "api_kwargs", None) or {}
        if stopped := [k for k in extra if "generation" in k.lower()]:
            log.info("%s: message generation stopped (fields: %s)", describe_source(update), ", ".join(stopped))

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        if not msg or not msg.text:
            return
        waited = (datetime.now(UTC) - msg.date).total_seconds() if getattr(msg, "date", None) else 0
        if waited > PENDING_MAX_SECONDS:
            log.info("%s: ignored a message %d minutes old: %s", describe_source(update), waited // 60, msg.text, extra=QUOTES)
            return
        private = msg.chat.type == ChatType.PRIVATE
        if private and not await self._allowed(update, context):   # before remember_chat: a stranger never becomes an alert chat
            return
        self.noticed()
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
        """The report, or "weather now", in the service's words: the readings now (current: each device's heading and lines), then the
        pollen's and the forecast's own text, the calls made together."""
        calls = legacy.report(self.by_name) if read.report else [("current", {})]
        results = await asyncio.gather(*(self._service_call(name, args, ahead=turn.ahead) for name, args in calls))
        by_tool = {name: out for (name, _), out in zip(calls, results)}
        now = by_tool.get("current") or {"error": "none"}
        if not read.report:
            if "error" in now or "station" in (now.get("unavailable") or {}) or not (now.get("lines") or {}).get("station"):
                return "The weather station isn't answering right now."
            return "\n".join(now["lines"]["station"])
        sections = [] if "error" in now else [
            "\n".join([now["headings"][device], *now["lines"][device]]) for device in ("station", "air") if device in now.get("lines", {})]
        sections += [out["text"] for name in ("pollen", "forecast") if (out := by_tool.get(name)) and out.get("text")]
        self._shown_forecast(by_tool.get("forecast"), turn)
        return "\n\n".join(sections) or "No readings are available right now."

    def _shown_forecast(self, out: dict | None, turn: Turn):
        """The forecast days an answer shows, remembered for the chat once it is sent (a later revision of them is worth telling it)."""
        source = self.by_name.get("Forecast")
        if source is not None and out and "error" not in out and out.get("days"):
            shown = {day["date"] for day in out["days"]}
            turn.forecast_shown.extend(d for d in source.days or [] if d["date"].isoformat() in shown)

    async def _report(self, read, turn: Turn, gauges: bool) -> tuple[str, bytes | None]:
        """The report (or "weather now") written in code and, with `gauges`, its picture drawn alongside (None without one), in TURN_SECONDS."""
        written = self._written_in_code(read, turn)
        if gauges:
            return tuple(await asyncio.wait_for(asyncio.gather(written, self._status_gauges()), TURN_SECONDS))
        return await asyncio.wait_for(written, TURN_SECONDS), None

    async def _status_gauges(self) -> bytes | None:
        """The report's picture: the weather station's temperature, humidity and rain gauges (gauge_sheet.STATUS), from the service's
        current with context. None without the station, or when they can't be drawn (the report then goes as text alone)."""
        if "Ecowitt" not in self.by_name:
            return None
        try:
            sheet = await self._gauge_sheet(gauge_sheet.STATUS)
            return await asyncio.to_thread(gauges.render, sheet) if sheet else None
        except Exception as e:
            log.error("The report's gauges failed (%s: %s); sending it without them", type(e).__name__, e, exc_info=isinstance(e, BUGS))
            return None

    async def _warn(self, msg: Message, tokens: int):
        """A heads-up before a big job: the question has pulled in a lot of data. Sent now, silently; the answer follows by itself."""
        text = f"\u26a0\ufe0f That's pulling in about {tokens / 1000:.0f}k tokens of data, so it will take a little longer. Working on it..."
        log.info("Heads-up sent: %s", text, extra=QUOTES)
        with contextlib.suppress(TelegramError):
            await msg.reply_text(text)

    async def _chart_in_code(self, call: tuple[str, dict] | None, turn: Turn) -> Made | None:
        """A chart asked for plainly: the service tool the fast path's call means (summary, days ...) draws it and its caption (days:
        its answer) goes with it. None (the model then takes the question) when it drew nothing or has nothing to say."""
        if call is None:
            return None
        out = await self._service_call(*call, ahead=turn.ahead)
        text, charts = out.get("caption") or out.get("answer"), out.get("charts") or []
        return Made(text, charts) if "error" not in out and charts and text else None

    async def _gauge_sheet(self, titles=None):
        """The gauges of these titles (all when None) laid out from the service's current with context (lib/gauge_sheet.py); None when
        the station gives nothing."""
        return gauge_sheet.sheet(await self._service_call("current", gauge_sheet.call(titles)), titles)

    async def _dashboard(self) -> Answer:
        """The station's gauges as one picture, sent with just its title (no period buttons: it is the state now)."""
        sheet = await self._gauge_sheet()
        if sheet is None:
            return Answer("The weather station isn't answering right now.", ok=False)
        now = now_local(self.cfg.tz)
        title = f"Weather station dashboard, {day_text(now)} {clock_text(now)}"
        return Answer(title, [await asyncio.to_thread(gauges.render, sheet)], [title], silent=True)

    async def _lookup_in_code(self, read, turn: Turn, now: datetime) -> Made | None:
        """A plain lookup (a reading, the air, a day's figures or a count, a short period's highs and lows, the pollen, the forecast,
        what the bot can do) answered in its service tool's own words: current's lines, days' answer (with its chart, if it drew one),
        summary's text, pollen's and forecast's text, about's lines. None (the model then takes the question) when it can't be."""
        call = legacy.lookup(read) or self._call_of(read, turn, now)
        if call is None:
            return None
        out = await self._service_call(*call, ahead=turn.ahead)
        if "error" in out:
            return None
        tool, kind = call[0], read.lookup
        if tool == "current":
            device = "air" if kind == "air" else "station"
            lines = (out.get("lines") or {}).get(device)
            return Made("\n".join(lines)) if lines and device not in (out.get("unavailable") or {}) else None
        if tool == "about":
            return Made("\n".join(out.get("lines") or []) or None)
        if tool == "forecast":
            self._shown_forecast(out, turn)
        return Made(out.get("answer") or out.get("text") or None, out.get("charts") or [])

    def _chart_answer(self, turn: Turn):
        """The model loop's finish: when each call the model made in a step set chart_answers_it, drew its chart and can be captioned
        here, their captions in order are the reply and the model is asked nothing more; else None, and the model answers as usual."""
        def finish(calls: list[dict], results: list[str]) -> str | None:
            try:
                captions = [self._answer_caption(call, result, turn) for call, result in zip(calls, results)]
            except Exception as e:   # the model answers instead
                log.error("Couldn't caption a chart that answers the question (%s: %s)", type(e).__name__, e, exc_info=isinstance(e, BUGS))
                return None
            return None if None in captions else "\n\n".join(captions)
        return finish

    def _answer_caption(self, call: dict, result: str, turn: Turn) -> str | None:
        """One call's chart as the whole answer: its caption, written from the tool's result as for a chart made in code (the day tools:
        as for a days lookup); None when the call didn't set chart_answers_it, drew no chart, failed or left nothing to caption."""
        try:
            args = json.loads(call["arguments"] or "{}")
        except ValueError:
            return None
        if (call["name"] not in ANSWERING_CHARTS or not isinstance(args, dict) or args.get("chart_answers_it") is not True
                or not _drew_chart(result) or _needs_words(call["name"], result)):
            return None
        if call["name"] in DAY_TOOLS:
            return report.lookup("days", result, [], [], now_local(self.cfg.tz).date())
        args, drawn = _as_drawn(call["name"], args, turn)
        return report.chart_caption(call["name"], args, result, drawn, self.cfg.tz)

    async def _ask_model(self, working: list[dict], system: str, read, turn: Turn, draft, on_heavy=None) -> str:
        """The model's answer. If it has not answered in TURN_SECONDS, ask again once with one reasoning step less (medium > low >
        none), for RETRY_SECONDS; a question already at no reasoning just times out. A chart that answers the question by itself
        ends the turn with its caption written here (_chart_answer)."""
        first = [*([read.fast[:2], *read.more] if read.fast else []), *read.extra] or None
        before, effort, budget, retried = list(working), read.effort, TURN_SECONDS, False
        while True:
            try:
                return await asyncio.wait_for(
                    self.agent.run(working, system, effort, first_call=first, tool_choice=read.tool_choice, turn=turn,
                                   on_heavy=on_heavy, finish=self._chart_answer(turn),
                                   **({"on_text": lambda text: None if turn.chart_asked or turn.charts else draft.update(text)} if draft else {})),
                budget)
            except TimeoutError:
                lower = intent.lower_effort(effort)
                if not lower or retried:
                    raise
                log.warning("No answer in %ds at reasoning %s; asking again at %s", budget, effort, lower)
                working[:] = before          # the unfinished attempt's turns are dropped
                effort, budget, retried = lower, RETRY_SECONDS, True

    def preload_soon(self, bot, drawn_by: dict | None):
        """With TELEGRAM_PRELOAD_CHAT set, upload the chart of each other period its drawn_by lists, in the background."""
        chat = getattr(self.cfg, "telegram_preload_chat", None)
        key = periods.drawn_key(drawn_by)
        if chat is None or self.preload_off or not drawn_by or not drawn_by.get("periods") or key in self.preloading:
            return
        self.preloading.add(key)
        self.tasks.start(self.preload(bot, chat, drawn_by))

    async def preload(self, bot, chat: int, drawn_by: dict):
        """Upload (silently, to `chat`) the chart for each other period of a chart's drawn_by, each its call replayed with that period
        (in the background: nobody is waiting), so a press of a period button edits the message by Telegram's copy and uploads nothing.
        Nothing holds it back: each period is drawn and uploaded on its own, the moment it can be, whatever else is being answered."""
        started, done, skipped = time.monotonic(), set(), []
        tool = drawn_by["tool"]
        try:
            own = periods.own_period(drawn_by, now_local(self.cfg.tz), self.cfg.tz)
            asked = {p["period"]: periods.with_period(drawn_by, p["period"]) for p in drawn_by.get("periods") or () if p["period"] != own}
            asked = {p: args for p, args in asked.items() if not self.images.has_copy(periods.call_key(tool, args))}

            async def upload(period, args):
                key = periods.call_key(tool, args)
                try:
                    drawn = await asyncio.wait_for(self._draw_call(tool, args), TURN_SECONDS)
                except Exception as e:
                    log.debug("Couldn't draw %s to preload: %s: %s", key, type(e).__name__, e)
                    return
                if drawn is None:
                    return
                png, title, caption, newest, by, _ = drawn
                self.images.put(key, png, title, caption, ttl=self.images.PRELOAD_TTL, newest=newest, drawn_by=by)
                if self.preload_off:   # an upload was refused meanwhile
                    return
                if (why := await self._upload_ahead(bot, chat, key, png, title)) is not None:
                    skipped.append(why)
                elif not self.preload_off:
                    done.add(period)

            await asyncio.gather(*(upload(period, args) for period, args in asked.items()))
            if skipped:
                log.warning("TELEGRAM_PRELOAD_CHAT %s didn't take %d of the charts just now (%s: %s)", chat, len(skipped), type(skipped[0]).__name__, skipped[0])
            if done:
                log.info("[Telegram] Preloaded %s %s to %s in %.1fs", tool, json.dumps(periods.with_period(drawn_by, "…"), ensure_ascii=False),
                         chat, time.monotonic() - started, extra=QUOTES)
        finally:
            self.preloading.discard(periods.drawn_key(drawn_by))

    async def _upload_ahead(self, bot, chat: int, key: str, png: bytes, title: str) -> Exception | None:
        """Upload a chart drawn ahead to the preload chat, silently, and keep Telegram's copy under its key. Returns the flood limit or
        the timeout that stopped it (a press uploads the chart instead); None when it went, or when an upload was refused (said once,
        and no more are tried: preload_off)."""
        try:
            self.images.sent(key, await bot.send_photo(chat, png, caption=title or None, disable_notification=True))
        except (RetryAfter, TimedOut) as e:
            return e
        except TelegramError as e:
            if not self.preload_off:
                self.preload_off = True
                log.warning("Couldn't upload to TELEGRAM_PRELOAD_CHAT %s (%s: %s); not preloading charts", chat, type(e).__name__, e)
        return None

    async def draw_buttons_ahead(self, bot):
        """The chart of each keyboard button (each a fixed service call: the call its sentence is read as), and of each alert type sent in
        the last hours (alert_charts.warm, by their calls), drawn afresh (every AHEAD_SECONDS, from ecobot.warm_up), so a press, or the
        Refresh under the alert, is answered with a chart already drawn (images.ready, AlertCharts.newer_than) instead of drawing one
        then; with TELEGRAM_PRELOAD_CHAT set and the bot used in the last 15 minutes, uploaded too, so the press sends Telegram's copy and
        uploads nothing. A chart drawn for a question in the last minute is left. Nobody is waiting: the requests are background ones,
        and the logs are quiet."""
        started, now = time.monotonic(), now_local(self.cfg.tz)
        chat = getattr(self.cfg, "telegram_preload_chat", None) if self.used_lately else None
        drawn, uploaded, skipped = 0, 0, []
        calls = [call for text in templates.chart_sentences(self.irrigation is not None) if (call := self._sentence_call(text, now))]
        calls = list({periods.call_key(*call): call for call in [*calls, *self.alert_charts.warm()]}.items())
        for key, (tool, args) in calls:
            if (age := self.images.age_of(key)) is not None and age < AHEAD_KEEP_SECONDS:
                continue
            try:
                made = await asyncio.wait_for(self._draw_call(tool, args), TURN_SECONDS)
            except Exception as e:
                log.debug("Couldn't draw %s ahead: %s: %s", key, type(e).__name__, e)
                continue
            if made is None:   # it drew no chart (its source is not there)
                continue
            png, title, caption, newest, drawn_by, _ = made
            self.images.put(key, png, title, caption, newest=newest, drawn_by=drawn_by)
            drawn += 1
            if chat is not None and not self.preload_off:
                if (why := await self._upload_ahead(bot, chat, key, png, title)) is not None:
                    skipped.append(why)
                elif not self.preload_off:
                    uploaded += 1
        if drawn:
            log.debug("Drew %d button charts ahead%s in %.1fs", drawn, f", uploaded {uploaded} to {chat}" if uploaded else "", time.monotonic() - started)
        if skipped:
            log.warning("TELEGRAM_PRELOAD_CHAT %s didn't take %d of the button charts just now (%s: %s)", chat, len(skipped), type(skipped[0]).__name__, skipped[0])

    async def respond(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, redraw: Message | None = None):
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
        log.info("%s%s: %s", describe_source(update), f" (reasoning: {read.effort})" if read.effort != intent.EFFORT_DEFAULT else "",
                 text, extra=QUOTES)
        for source in self.sources:  # fetch recent readings while the model thinks
            if source.wants(text) and not read.chart_in_code:   # a chart made in code reads just what it needs, first in line
                source.poke()
        if read.fast:
            log.debug("Fast path: %s", read.fast[2])

        chat = self.chats[self._key(msg)]
        started = time.monotonic()
        async with chat.lock:
            draft = Draft(context.bot, msg.chat_id, self._thread(msg)) if redraw is None and msg.chat.type == ChatType.PRIVATE else None
            drafting = asyncio.create_task(draft.run(stop_typing)) if draft else None   # a private chat also gets the "Thinking..." draft
            working = [*chat.history, {"role": "user", "content": self._content(msg, text, context.bot.id)}]
            new_from = len(working)
            turn = Turn.of(read, text)
            earlier = working[:-1]
            recent = ([str(m["content"]) for m in earlier if m["role"] == "user"][-2:]
                      + [str(m["content"]) for m in earlier if m["role"] == "assistant"][-1:])
            # the topics of the last messages (the person's last two, the bot's last reply) and those this chat is still on
            turn.topics = chat.topics.active().union(*(intent.topics_in(said) for said in recent))
            said = [str(m["content"]) for m in earlier if m["role"] == "user"][-2:]   # the air readings named: now, else just before
            turn.air_named = next(filter(None, map(intent.air_named, [text, *reversed(said)])), [])
            try:
                answer = await self._answer(msg, read, turn, working, draft, now, redraw)
                tools_used = {tc["function"]["name"] for m in working[new_from:] for tc in m.get("tool_calls") or []}
                chat.topics.update(turn.topics, intent.topics_in(text) | intent.topics_in(answer.reply or "")
                                   | {intent.TOOL_TOPICS[t] for t in tools_used if t in intent.TOOL_TOPICS}
                                   | ({"air_extra"} if turn.air_extra_used else set()))
                chat.history = shorten_old(trim_history(strip_tool_turns(working)))
                for chart in answer.charts[:MAX_CHARTS]:   # a service tool's charts, drawn from their data while "typing..." is still showing
                    try:
                        png, title, _ = await asyncio.to_thread(self._draw, chart, turn.times)
                        answer.photos.append(png)
                        answer.titles.append(title)
                        answer.drawn_by.append(chart.get("drawn_by"))
                    except Exception as e:
                        log.error("Chart failed (%s: %s); sending the answer without it", type(e).__name__, e, exc_info=isinstance(e, BUGS))
                for i, spec in enumerate(turn.charts[:MAX_CHARTS - len(answer.photos)]):   # the model's (old tools): drawn from their JSON too
                    try:
                        answer.photos.append(await asyncio.to_thread(render_data, to_json(spec, self.cfg.tz), self.cfg.tz, turn.times))
                        answer.titles.append(spec.caption(now.date()))
                        answer.drawn_by.append(self._model_drawn_by(turn, i, now))
                    except Exception as e:
                        log.error("Chart failed (%s: %s); sending the answer without it", type(e).__name__, e, exc_info=isinstance(e, BUGS))
            except TimeoutError:
                log.error("%s: gave up after %ds on: %s", chat_tag(msg.chat_id), TURN_SECONDS, text, extra=QUOTES)
                answer = Answer("Sorry, that took too long. Please try again in a moment.", ok=False)
            except Exception as e:
                log.error("%s: agent error: %s: %s", chat_tag(msg.chat_id), type(e).__name__, e, exc_info=isinstance(e, BUGS))
                # The details (which can include provider error bodies) go to the log, not the chat
                answer = Answer(f"Sorry, something went wrong on my side ({type(e).__name__}). Please try again in a moment.", ok=False)
            finally:
                stop_typing.set()
                await asyncio.gather(*[t for t in (typing, drafting) if t])  # wait for any in-flight "typing" or draft so none is sent after the reply
        await self._send(msg, context.bot, read, turn, answer, working[new_from:], now, redraw, started)

    async def _answer(self, msg: Message, read, turn: Turn, working: list[dict], draft, now: datetime, redraw: Message | None) -> Answer:
        """The answer, from the first way that gives one: a chart drawn moments ago, a chart made in code, a lookup, the report
        written in code, else the model. `working` is the conversation, the question last; the answer joins it."""
        shown = bool(intent.HILO.search(turn.text))   # "when was CO2 highest": the answer is a chart's figures, so they go with it
        call = self._call_of(read, turn, now) if read.chart_in_code else None   # the service's call the chart is (its cache key)
        key = periods.call_key(*call) if call else None
        if read.chart_in_code and redraw is None and (ready := self.images.ready(key)):   # drawn ahead, or for someone's same call
            answer = Answer(ready.caption, [ready.photo], [ready.title], silent=not shown or not ready.caption, ready=ready,
                            drawn_by=[ready.drawn_by], key=key)
        elif read.chart_in_code:   # drawn by the service; the chart goes out with just its title (the text is kept for the history)
            began = time.monotonic()
            made = await asyncio.wait_for(self._chart_in_code(call, turn), TURN_SECONDS)
            answer = Answer(made.text, silent=not shown, made=time.monotonic() - began, charts=made.charts, key=key) if made else Answer()
        elif read.dashboard and "Ecowitt" in self.by_name:
            answer = await asyncio.wait_for(self._dashboard(), TURN_SECONDS)
        elif read.lookup:
            made = await asyncio.wait_for(self._lookup_in_code(read, turn, now), TURN_SECONDS)
            answer = Answer(made.text, charts=made.charts) if made and made.text else Answer()
            if answer.reply is None:
                turn.forecast_shown.clear()
        elif read.fast and (read.report or read.weather_now):   # written in code from the tools' results, no model (the report: with its gauges)
            text, gauge = await self._report(read, turn, read.report)
            answer = Answer(text, [gauge] if gauge else [], [""] if gauge else [])
        else:
            answer = Answer()
        if answer.reply is not None:
            working.append({"role": "assistant", "content": answer.reply})
            return answer
        if not getattr(self.agent, "ready", True):   # no OPENROUTER_API_KEY: what is left is for the model
            answer.reply = NO_MODEL
            working.append({"role": "assistant", "content": answer.reply})
            return answer
        before = [str(m["content"]) for m in working[:-1] if m["role"] == "user"][-2:]
        found = intent.topics(str(working[-1]["content"]), before) | turn.topics
        turn.topics = found
        turn.air_extra = turn.air_extra or "air_extra" in found   # the tools offer the air sensor's own readings whenever the prompt tells of them
        system = prompt.build(datetime.now(self.cfg.tz), {s.name: s.describe() for s in self.sources}, read.hints,
                              read.about_the_bot, read.rain_caption, found)
        answer.reply = await self._ask_model(working, system, read, turn, draft, lambda tokens: self._warn(msg, tokens))
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
        log.info("%s: %s in %.1fs, tools %d, charts %d, %d chars", chat_tag(msg.chat_id), "Replied" if answer.ok else "Error reply",
                 time.monotonic() - started, len(used), len(photos), len(reply))
        used_air = any(tc["function"]["name"] == "air_quality" for m in added for tc in m.get("tool_calls") or [])
        air = self.by_name.get("AirGradient")
        markup = templates.keyboard(self.irrigation is not None) if self._keyboard_stale(msg) else None
        refresh = answer.ok and read.report and 0 < len(reply) <= TG_LIMIT and not markup   # the report, to be refreshed in place
        single = len(photos) == 1 and not (read.dashboard or read.report)   # a single chart: its period buttons go under it
        drawn_by = answer.drawn_by[0] if single and answer.drawn_by else None
        if drawn_by:   # the periods its call lists, the chart's own shaded; none when it lists none
            row = periods.periods_keyboard(drawn_by, periods.own_period(drawn_by, now, self.cfg.tz), now, self.cfg.tz)
            record = {"drawn_by": drawn_by, "words": bool(reply.strip())}
        else:   # a chart the service can't replay (the model's, through a call it has no equivalent of): its question asked again
            row = periods.keyboard(intent.period_days(text, now)) if single else None
            record = text
        drawn = list(photos)
        kept = (answer.key or periods.drawn_key(drawn_by)) if single and answer.ok and (drawn_by or not reply.strip()) else None
        if kept and not answer.ready:   # kept for the period buttons, and (made in code) for the next to ask for the same call
            self.images.put(kept, photos[0], titles[0], (caption or reply or None) if answer.key else None, drawn_by=drawn_by)
        if kept and drawn_by and (own := periods.own_period(drawn_by, now, self.cfg.tz)):   # ... and as its own button replays it
            self.images.alias(kept, periods.call_key(drawn_by["tool"], periods.with_period(drawn_by, own)))
        try:
            began = time.monotonic()
            if redraw is not None and row and await self._redraw(redraw, reply, photos[0], titles[0], row, record, kept):
                reply, photos, row = "", [], None
            sent = await deliver(msg, reply, photos, link=air.link if air and used_air else None,
                                 markup=markup or (report_menu.keyboard() if refresh else report_menu.dashboard_keyboard()
                                                   if answer.ok and read.dashboard and photos else None), titles=titles, period_row=row,
                                 bold_headings=answer.ok and bool(read.report or read.lookup in ("pollen", "forecast")), photos_first=read.report)
            carried_keyboard = sent.carried and bool(markup)   # the refresh button is not the chat's keyboard
            if answer.ready:
                log.debug("Chart ms: drawn %.0fs ago, send %.0f%s", self.images.age(answer.ready), (time.monotonic() - began) * 1000,
                          " (Telegram's copy)" if isinstance(drawn[0], str) else "")
            elif drawn:
                log.debug("Chart ms: %s", chart_times(turn.times, answer.made, drawn, time.monotonic() - began))
            if sent.photo and row is not None:
                self.charted.remember(msg.chat_id, sent.photo.message_id, record)
                self.images.sent(kept, sent.photo)
            if kept and row is not None and drawn_by and read.chart_in_code:   # made in code: its other periods are uploaded ahead
                self.preload_soon(bot, drawn_by)
            if answer.ok and turn.forecast_shown:   # it was sent: a later revision of these days is worth telling this chat
                self.state.record_forecast(msg.chat_id, turn.forecast_shown, now_local(self.cfg.tz).date())
            if carried_keyboard:
                log.info("Buttons: sent keyboard %s to chat %s (it had %s)", self.keyboard_version, msg.chat_id, self.state.keyboard(msg.chat_id))
                self.state.set_keyboard(msg.chat_id, self.keyboard_version)
            elif markup and not row:
                log.info("Buttons: this reply couldn't carry the keyboard (several charts); will try the next one")
        except TelegramError as e:
            log.error("Failed to deliver reply: %s: %s", type(e).__name__, e)
