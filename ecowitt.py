"""Telegram bot powered by xAI Grok with local MCP tools.

Private chats: replies to every message.
Groups: replies when @mentioned, when someone replies to the bot, or on /ask.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from telegram import InputMediaPhoto, Message, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (Application, ChatMemberHandler, CommandHandler, ContextTypes, Defaults, MessageHandler,
                          filters)

from lib import ecowitt_history, fast_path
from lib.history_archive import Archive, macs_from
from lib.history_cache import HistoryCache
from lib.recent_data import REFRESH_SECONDS, RecentData
from lib.agent import Agent, strip_tool_turns, trim_history
from lib.airgradient import DESCRIPTION as AIR_DESCRIPTION, PARAMETERS as AIR_PARAMETERS, AirGradient
from lib.alerts import AirMonitor, BotState, Monitor, send_to_all, with_footer
from lib.charts import CHART_REQUESTS, render as render_chart
from lib.mcp_manager import MCPManager

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Application").setLevel(logging.WARNING)  # "Application started" etc.
log = logging.getLogger("bot")


def _ids(var: str) -> set[int]:
    return {int(x) for x in os.getenv(var, "").replace(" ", "").split(",") if x}


TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
XAI_API_KEY = os.environ["XAI_API_KEY"]
XAI_BASE_URL = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4.3")
XAI_REASONING_EFFORT = (os.getenv("XAI_REASONING_EFFORT") or "none").strip().lower()
# Questions that need a judgement call (will it rain?) get more reasoning than lookups
XAI_FORECAST_REASONING_EFFORT = (os.getenv("XAI_FORECAST_REASONING_EFFORT") or "medium").strip().lower()
# Describing a day or playing along with a hypothetical benefits from a little thinking
XAI_DESCRIBE_REASONING_EFFORT = (os.getenv("XAI_DESCRIBE_REASONING_EFFORT") or "low").strip().lower()
for _name, _value in (("XAI_REASONING_EFFORT", XAI_REASONING_EFFORT),
                      ("XAI_FORECAST_REASONING_EFFORT", XAI_FORECAST_REASONING_EFFORT),
                      ("XAI_DESCRIBE_REASONING_EFFORT", XAI_DESCRIBE_REASONING_EFFORT)):
    if _value not in ("none", "low", "medium", "high"):
        raise SystemExit(f"{_name} must be none, low, medium or high (got {_value!r})")

# Messages about the weather must fetch fresh data; anything else (thanks, chat) needn't
WEATHER_RE = re.compile(
    r"\b(weather|temp\w*|hot\w*|cold\w*|warm\w*|cool\w*|heat\w*|freez\w*|frost\w*|degrees?|celsius|"
    r"rain\w*|showers?|drizzle|storms?|thunder\w*|hail|snow|fog\w*|cloud\w*|sun\w*|uv|solar|"
    r"wind\w*|gusts?|breez\w*|humid\w*|dew|pressure|barometer|forecast\w*|umbrella|"
    r"highs?|lows?|max\w*|min\w*|records?|extremes?|average|chart\w*|graph\w*|plot\w*|trend\w*|"
    r"indoors?|outdoors?|inside|outside|conditions|feels?|today|tonight|tomorrow|yesterday|"
    r"week|month|year|now|currently|current|air|aqi|pm ?2\.?5|pm ?10|co2|voc\w*|nox|smok\w*|pollut\w*|stuffy)\b|\u00b0|"
    r"\b\d{1,2}(st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*|"
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}\b|\b\d{1,2}/\d{1,2}\b", re.IGNORECASE)

DESCRIBE_RE = re.compile(
    r"\b(what\b.*\blike|describe\w*|imagine\w*|picture|pretend|feel like|felt like|"
    r"if (we|i|you|someone|anyone) (were|was|had|lived))\b", re.IGNORECASE)

FORECAST_RE = re.compile(  # looking ahead, not weather words alone ("how much rain fell" is a lookup)
    r"\b(will it|will there|is it going to|going to (rain|be|get|stay)|gonna|forecast\w*|umbrella|"
    r"tonight|tomorrow|later|soon|coming|next (few )?(hour|hours)|this (afternoon|evening)|"
    r"should i|chance of|likely|expect\w*|predict\w*|outlook)\b", re.IGNORECASE)
MCP_CONFIG = os.getenv("MCP_CONFIG", "mcp_ecowitt.json")
# Answer simple "period + highs/lows" questions without the model's first round trip
FAST_PATH = os.getenv("FAST_PATH", "true").strip().lower() not in ("0", "false", "no", "off")
# Keep today's recent readings fetched ahead of questions while chats are active
ECOWITT_KEEP_WARM = os.getenv("ECOWITT_KEEP_WARM", "true").strip().lower() not in ("0", "false", "no", "off")
# Nightly copy of 5-minute data into the cache (Ecowitt only keeps it 90 days)
ECOWITT_ARCHIVE = os.getenv("ECOWITT_ARCHIVE", "true").strip().lower() not in ("0", "false", "no", "off")
ECOWITT_ARCHIVE_TIME = dtime.fromisoformat(os.getenv("ECOWITT_ARCHIVE_TIME") or "01:30")
ECOWITT_ARCHIVE_GROUPS = [g.strip() for g in (os.getenv("ECOWITT_ARCHIVE_GROUPS") or
                          "outdoor,indoor,pressure,wind,rainfall,rainfall_piezo").split(",") if g.strip()]
# Your AirGradient sensor (token from the AirGradient dashboard; location ID of the sensor)
AIRGRADIENT_API_TOKEN = os.getenv("AIRGRADIENT_API_TOKEN", "").strip()
AIRGRADIENT_LOCATION_ID = os.getenv("AIRGRADIENT_LOCATION_ID", "").strip()
# Small italic "live chart" link under air-quality replies and alerts (e.g. your AirGradient display URL)
AIRGRADIENT_DASHBOARD_URL = os.getenv("AIRGRADIENT_DASHBOARD_URL", "").strip()
AIR_LINK = ("live chart", AIRGRADIENT_DASHBOARD_URL) if AIRGRADIENT_DASHBOARD_URL else None
AIR_CHECK_SECONDS = 30 * 60  # how often the AirGradient sensor is read for air-quality alerts
# Rain and temperature-crossing alerts to every chat the bot is in (needs keep-warm)
ALERTS = os.getenv("ALERTS", "true").strip().lower() not in ("0", "false", "no", "off")
BOT_STATE = os.getenv("BOT_STATE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")
ECOWITT_CACHE = os.getenv("ECOWITT_CACHE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecowitt_cache.sqlite")
MAX_HISTORY = int(os.getenv("MAX_HISTORY_MESSAGES", "40"))
MAX_TOOL_STEPS = int(os.getenv("MAX_TOOL_STEPS", "10"))
ALLOWED_USERS = _ids("ALLOWED_USER_IDS")
ALLOWED_CHATS = _ids("ALLOWED_CHAT_IDS")

# Timezone from the standard TZ variable (e.g. TZ=Australia/Melbourne), else the system's
_tz_name = os.getenv("TZ", "").lstrip(":")
TIMEZONE = ZoneInfo(_tz_name) if _tz_name else datetime.now().astimezone().tzinfo

DEFAULT_PROMPT = """\
You are a friendly weather bot on Telegram. It is now {now}.
People ask about the owner's personal weather station: current conditions and historical data from indoor and outdoor sensors. Always fetch data with your tools; never guess or invent readings. If data is missing, say so briefly.

DEVICES (already fetched - use this MAC, don't look it up)
{devices}

TIME PERIODS
- A day runs from midnight to midnight local time. Weeks start on Monday.
- "This week" (especially in past tense) means the last 7 days: today plus the previous 6 days.
- "This month"/"this year" are month-to-date/year-to-date.
- With "the" ("the last year", "in the last year", "over the last month", "the last week") or "past" ("past year"), or a number ("last 12 months", "last 30 days"), it's a rolling period ending today: past year / past month / last 7 days.
- Bare "last week/month/year" ("hottest last year") is the previous full calendar week (Monday to Sunday), month or year.
- Exact ranges right now (use these, don't recalculate):
{dates}
- A period that includes today runs up to now; include today's data.
- "On record", "ever" or "all time" means all available data: the "on record" range below, or from the device's creation time in DEVICES if that is later. Never shorten it to this year.
- State the date range you used in a few words, e.g. "Sun 20 - Sat 26 Sep".

AIR QUALITY
- For air quality (AQI, PM2.5, PM10, CO2, VOC, smoke, "is the air OK"), use the air_quality tool (the owner's AirGradient outdoor sensor): no dates for now, start_date/end_date for how it was over a period (up to 14 days).
- Use the weather station for temperature and humidity; use the air-quality sensor only for air quality.
- Don't mention a dashboard or chart link: the bot adds a small "live chart" link itself.
- For air-quality graphs ("graph PM2.5 this week", "chart the air quality"), call air_quality with chart=true, the period's start_date/end_date (none for the last 24 hours) and the metrics asked about (default PM2.5; "all" means all six). Keep the caption to one or two short lines.
- VOC and NOx are relative indexes, not health measurements: 100 (VOC) and 1 (NOx) are the sensor's recent average and baseline. Well above that means something changed nearby (e.g. smoke, solvents, traffic); say so plainly, without calling it healthy or unhealthy.
- Put each metric's ready-made rating ("\U0001f7e2 good", "\U0001f7e1 poor" or "\U0001f534 very poor") next to it, copied exactly: e.g. "\u2022 PM2.5: 0.0 \u00b5g/m\u00b3 \U0001f7e2 good (AQI 0)". For history, use high_rating and average_rating. Add a short practical tip when anything isn't good.

HOW TO FETCH DATA (be fast: ONE round of tool calls, in parallel if more than one, then answer)
- Always fetch fresh data for every question, even if the same or a similar question was answered earlier in this conversation. Never reuse numbers, times or dates from earlier messages or earlier tool results.
- For any past period (highs/lows, records, daily summaries, "this week" etc.): make ONE get_device_historical_info call covering the whole period, start_date = first day 00:00:00, end_date = last day 23:59:59 (today is included up to now). Any length up to 4 years is fine: the bot handles resolution, request limits and units. Don't split it yourself and don't add realtime calls.
- Set chart=true on the history call when a graph would help: trends over several days or longer, or when a graph or chart is asked for. Your reply then becomes the chart's caption: keep it to one or two short lines (the period and the most notable point), no lists. Never write about the chart itself (e.g. "Chart sent...").
- Feels-like, apparent temperature, dew point and VPD are left out of history results unless you ask for them with include_derived (only when the question is about them).
- callback takes plain group names, comma-separated, e.g. "outdoor,indoor" (add "rainfall" or "wind" only if needed). Never use dotted names like "outdoor.temp".
- The result has, per series (e.g. "outdoor.temperature"): low and high for the whole period, each with ready-made "_when" wording and a "_date" (plus the raw "_time"), and a "daily" (up to 31 days) or "monthly" breakdown. For periods of up to about 3 months, each month in "monthly" also has its own low/high "_when" and "_date"; for longer periods monthly figures are values only (see "monthly_note"). Read the answer straight from those fields.
- If the question names several months ("June, July, August") or asks about "each month", answer each month separately (its low and/or high, with when and date) rather than one figure for the whole period.
- If the result has a "warning" or "missing", say briefly that some data couldn't be fetched.
- Use get_device_realtime_info only for questions about current conditions.
- Never repeat an identical call. Timestamps in results are already local time.

HIGHS AND LOWS
- Say when each high or low happened by copying its "_when" text exactly as given ("at 7:05am", "around 3:30pm", or a window), then "on", the day's emoji and its "_date": "28.3°C around 3:30pm on ☀️ Fri 9 Jan 2026". Never change "at" to "around" or the reverse.
- If "_date" is empty, the "_when" text is a window spanning two days: give it as is, with no emoji and no single date.
- If a value has a note saying it came from averaged data, add a short caveat that the real value may have been more extreme.

RAIN AND SHORT-TERM OUTLOOK ("will it rain?", "do I need an umbrella?", "what's it doing later?")
- Fetch in ONE step, both in parallel:
  1. get_device_realtime_info, callback "outdoor,pressure,rainfall,rainfall_piezo,wind"
  2. get_device_historical_info for the last 3 hours up to now, same callback
- Pressure tendency (relative pressure, last 3 hours): falling if the high came before the low, rising if the low came first. Change = high minus low.
  Falling more than 3 hPa: unsettled, rain likely soon. Falling 1-3 hPa: rain possible. Steady (under 1 hPa) or rising: rain unlikely.
- Pressure level: under 1005 hPa is unsettled; over 1020 hPa is settled.
- Moisture: humidity 90% or more, or temperature within 2°C of dew point, makes rain more likely.
- Recent rain: a rain rate above 0 now, or rain in the last hour, means showers are likely to continue for a while.
- Wind picking up alongside falling pressure strengthens a rain call.
- Answer with one of: unlikely / possible / likely, then a short reason citing one or two readings (e.g. "pressure down 2.4 hPa in 3 hours, humidity 91%"). Only look a few hours ahead.
- Say briefly it's a read of the station data, not an official forecast. Don't list every reading.

LABELS
- Always give both Indoor and Outdoor readings (and fetch both, callback "outdoor,indoor"), unless the question asks specifically about only one of them.
- Call readings simply "Indoor" and "Outdoor". Don't mention sensor models, console names or the station name unless there is more than one device of that kind.

UNITS
- Always Celsius for temperature (convert if a sensor reports Fahrenheit). Never show Fahrenheit, not even in brackets. Metric for everything else: mm rain, km/h wind, hPa pressure.

WEATHER EMOJIS (required)
- Whenever you name a specific day, including today, put an emoji for that day's outdoor weather immediately before it: in day-by-day lists AND single mentions such as the day a high or low occurred.
- Infer it from that day's data in the "daily" breakdown (temperature range, rain, wind, humidity, solar/UV if present): ☀️ sunny, 🌤️ mostly sunny, ⛅ partly cloudy, ☁️ overcast, 🌦️ light showers, 🌧️ rain, ⛈️ storms, 💨 windy, 🥵 very hot, 🥶 very cold, 🌫️ foggy. Use only these emojis.

TONE
- Friendly and a little playful. Match the person's tone: if they're joking, play along briefly while still answering.
- For "what was it like", "what would it have been like", "describe" or "imagine" questions, paint a short, vivid picture of the day in 2-4 sentences of plain prose, built only from the real readings (e.g. when the rain came, how cold it got, how windy), then give the key numbers in one short line. No bullet list for these.
- Imagination is for the description, never the data: every number, time and date must come from tool results.

STYLE
- Only talk about the weather when the message asks about it. Compliments, thanks, jokes and chat get a short, natural reply, with no weather data and no tool calls.
- In group chats, messages are prefixed with the sender's name. Never start your reply with a name or "Name:" prefix.
- Be concise. Plain text only: no markdown headers, tables or bold. Use "•" bullet lists for data answers (highs and lows, lists of days); descriptive and chatty answers are prose.

EXAMPLE
Q: what was this week's high and low for indoor and outdoor?
A: Mon 3 - Sun 9 Aug (including today so far):
Outdoor
• High: 19.4°C at 2:35pm on 🌤️ Thu 6 Aug
• Low: -1.2°C at 6:10am on 🥶 Mon 3 Aug
Indoor
• High: 22.1°C at 3:55pm on 🌤️ Thu 6 Aug
• Low: 11.5°C at 7:20am on 🥶 Mon 3 Aug
(Example values are made up. Always use real tool data.)
"""

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT") or DEFAULT_PROMPT

TG_LIMIT = 4000


@dataclass
class ChatState:
    history: list[dict] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


states: dict[tuple, ChatState] = defaultdict(ChatState)


def chat_key(msg: Message) -> tuple:
    # Forum topics get their own conversation
    return (msg.chat_id, msg.message_thread_id if msg.is_topic_message else None)


def is_allowed(update: Update) -> bool:
    # No allowlist configured -> everyone may use the bot
    if not (ALLOWED_USERS or ALLOWED_CHATS):
        return True
    user = update.effective_user
    return (user and user.id in ALLOWED_USERS) or update.effective_chat.id in ALLOWED_CHATS


def describe_source(update: Update) -> str:
    """e.g. "[@rob_llama]" in a private chat, "[@rob_llama, group 'Weather']" in a group."""
    chat, user = update.effective_chat, update.effective_user
    who = (f"@{user.username}" if user.username else user.full_name) if user else "unknown"
    if chat.type == ChatType.PRIVATE:
        return f"[{who}]"
    return f"[{who}, group '{chat.title}']"


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


DATE_LABELS = [  # period name -> label in the prompt
    ("today", "Today ({} 00:00 to now)"), ("yesterday", "Yesterday"),
    ("last 7 days", 'Last 7 days / the last week / "this week"'), ("current calendar week", "Current calendar week"),
    ("last week", "Last week"), ("this month", "This month"), ("last month", "Last month"),
    ("this year", "This year"), ("last year", "Last year"), ("past month", "Past month / the last month / last 30 days"),
    ("past year", "Past year / the last year / last 12 months"), ("on record", "On record (Ecowitt keeps 4 years)"),
]


def date_ranges(now: datetime) -> str:
    """Pre-computed date ranges so the model never does calendar maths (same
    definitions as the fast path)."""
    d = lambda x: x.strftime("%a %d %b %Y")
    periods = fast_path.period_ranges(now.date())
    lines = []
    for name, label in DATE_LABELS:
        first, last = periods[name]
        if name == "today":
            lines.append(f"  Today: {d(first)} (00:00 to now)")
        elif first == last:
            lines.append(f"  {label}: {d(first)}")
        else:
            lines.append(f"  {label}: {d(first)} to {d(last)}")
    return "\n".join(lines)


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


async def keep_typing(bot, chat_id: int, thread_id, stop: asyncio.Event):
    """Send "typing..." every 4.5s until stop is set."""
    while not stop.is_set():
        with contextlib.suppress(Exception):
            await bot.send_chat_action(chat_id, ChatAction.TYPING, message_thread_id=thread_id)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), 4.5)


async def respond(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, trigger: str):
    msg = update.effective_message
    text = text.strip()
    if not text:
        return
    effort = (XAI_FORECAST_REASONING_EFFORT if FORECAST_RE.search(text)
              else XAI_DESCRIBE_REASONING_EFFORT if DESCRIBE_RE.search(text) else XAI_REASONING_EFFORT)
    log.info("%s %s%s", describe_source(update),
             f"(reasoning: {effort}) " if effort != XAI_REASONING_EFFORT else "", _short(text))
    recent: RecentData | None = context.bot_data.get("recent")
    if recent:
        recent.question_arrived()  # fetch today's recent readings while the model thinks
    air: AirGradient | None = context.bot_data.get("air")
    if air and (fast_path.AIR.search(text) or re.search(r"\bair quality\b", text, re.IGNORECASE)):
        air.question_arrived()  # same for the air-quality sensor
    first_call = None
    macs = context.bot_data.get("macs") or []
    agent_tools = getattr(context.bot_data["agent"].mcp, "tools", {})
    air_chart = (fast_path.air_chart(text, datetime.now(TIMEZONE).replace(tzinfo=None))
                 if FAST_PATH and "air_quality" in agent_tools else None)
    if air_chart:
        first_call = ("air_quality", air_chart[1])
        log.info("Fast path: air quality chart, %s, %s", air_chart[0], ", ".join(air_chart[1]["metrics"]))
    elif FAST_PATH and "air_quality" in agent_tools and fast_path.air_now(text):
        first_call = ("air_quality", {})
        log.info("Fast path: air quality now")
    elif FAST_PATH and len(macs) == 1 and ecowitt_history.INSTALLED:
        now_local = datetime.now(TIMEZONE).replace(tzinfo=None)
        matched = fast_path.match(text, now_local.date(), now_local)
        if matched:
            name, first, last = matched
            first_call = (next(iter(ecowitt_history.INSTALLED)),
                          fast_path.tool_args(macs[0], first, last, fast_path.wants_chart(text, first, last),
                                              fast_path.groups(text)))
            when = lambda d: f"{d:%Y-%m-%d %H:%M}" if isinstance(d, datetime) else f"{d}"
            log.info("Fast path: %s (%s to %s), %s", name, when(first), when(last), first_call[1]["callback"])
    started = time.monotonic()

    is_group = msg.chat.type != ChatType.PRIVATE
    if is_group:
        sender = msg.from_user.full_name if msg.from_user else "Someone"
        content = f"{sender}: {text}"
        quoted = msg.reply_to_message
        if quoted and quoted.from_user and quoted.from_user.id != context.bot.id:
            qtext = (quoted.text or quoted.caption or "")[:1000]
            if qtext:
                content = f"{sender} (replying to {quoted.from_user.full_name}: \"{qtext}\"): {text}"
    else:
        content = text

    agent: Agent = context.bot_data["agent"]
    state = states[chat_key(msg)]
    thread_id = msg.message_thread_id if msg.is_topic_message else None

    async with state.lock:
        stop_typing = asyncio.Event()
        typing = asyncio.create_task(keep_typing(context.bot, msg.chat_id, thread_id, stop_typing))
        working = [*state.history, {"role": "user", "content": content}]
        new_from = len(working)
        ok = True
        charts: list = []
        chart_token = CHART_REQUESTS.set(charts)  # the history tool adds chart specs here
        photos: list[bytes] = []
        try:
            now = datetime.now(TIMEZONE)
            prompt = SYSTEM_PROMPT.format(
                now=now.strftime("%A %d %B %Y, %H:%M %Z"), dates=date_ranges(now),
                devices=context.bot_data.get("devices", "(unknown)"))
            reply = await agent.run(working, prompt, reasoning_effort=effort, first_tool_call=first_call,
                                    require_tool=bool(WEATHER_RE.search(text)))
            state.history = trim_history(strip_tool_turns(working), MAX_HISTORY)
            for spec in charts[:3]:  # drawn while "typing..." is still showing
                try:
                    photos.append(await asyncio.to_thread(render_chart, spec, TIMEZONE))
                except Exception:
                    log.exception("Chart failed; sending the answer without it")
        except Exception as e:
            log.exception("Agent error")
            ok = False
            reply = f"Sorry, something went wrong: {type(e).__name__}: {e}"
        finally:
            CHART_REQUESTS.reset(chart_token)
            stop_typing.set()
            await typing  # wait for any in-flight "typing" so none is sent after the reply

    tool_calls = sum(1 for m in working[new_from:] if m["role"] == "tool")
    log.info("%s chat %s in %.1fs, %d tool call(s), %d chars, %d chart(s): %s",
             "Replied to" if ok else "Error reply to", msg.chat_id,
             time.monotonic() - started, tool_calls, len(reply), len(photos), _short(reply, 120))

    try:
        used_air = any(tc["function"]["name"] == "air_quality"
                       for m in working[new_from:] for tc in m.get("tool_calls") or [])
        await deliver(msg, reply, photos, link=AIR_LINK if used_air else None)
    except TelegramError:
        log.exception("Failed to deliver reply")


CAPTION_LIMIT = 1024  # Telegram's limit for photo captions

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
    result = "\n".join(lines).strip()
    return result or text


async def deliver(msg: Message, text: str, photos: list[bytes], link: tuple[str, str] | None = None):
    """Send the answer, with any charts. A short answer goes in the photo's caption (one
    message); a long one goes first as text, then the charts. If sending the chart
    fails, the answer is still sent as text. link = (label, url) adds a small italic
    link line at the end (e.g. the air-quality live chart)."""
    text = text.strip()
    if photos:
        text = strip_chart_talk(text)
    caption, caption_entities = with_footer(text, link)
    if photos and len(caption) <= CAPTION_LIMIT:
        try:
            if len(photos) == 1:
                await msg.reply_photo(photos[0], caption=caption, caption_entities=caption_entities or None)
            else:
                await msg.reply_media_group([InputMediaPhoto(p, caption=caption if i == 0 else None,
                                                             caption_entities=(caption_entities or None) if i == 0 else None)
                                             for i, p in enumerate(photos)])
            return
        except TelegramError:
            log.exception("Couldn't send the chart; sending the answer as text")
            photos = []
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        body, entities = with_footer(chunk, link) if i == len(chunks) - 1 else (chunk, [])
        await msg.reply_text(body, entities=entities or None, disable_web_page_preview=True)
    if photos:
        try:
            if len(photos) == 1:
                await msg.reply_photo(photos[0])
            else:
                await msg.reply_media_group([InputMediaPhoto(p) for p in photos])
        except TelegramError:
            log.exception("Couldn't send the chart")


def remember_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record the chat so alerts can be sent to it (Telegram can't list a bot's chats)."""
    state: BotState | None = context.bot_data.get("state")
    chat = update.effective_chat
    if state and chat and is_allowed(update):
        state.add_chat(chat.id, chat.title or (update.effective_user.full_name if update.effective_user else str(chat.id)))


async def on_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The bot was added to or removed from a chat."""
    state: BotState | None = context.bot_data.get("state")
    change = update.my_chat_member
    if not state or not change:
        return
    chat, status = change.chat, change.new_chat_member.status
    if status in ("member", "administrator"):
        remember_chat(update, context)
    elif status in ("left", "kicked"):
        state.remove_chat(chat.id, f"bot {status}")


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/alerts, /alerts on, /alerts off"""
    if not is_allowed(update):
        return
    state: BotState | None = context.bot_data.get("state")
    msg, chat = update.effective_message, update.effective_chat
    if not state:
        await msg.reply_text("Alerts are turned off for this bot.")
        return
    title = chat.title or (update.effective_user.full_name if update.effective_user else str(chat.id))
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "off"):
        state.set_alerts(chat.id, title, arg == "on")
        log.info("/alerts %s in %s", arg, describe_source(update))
    on = state.chats.get(chat.id, {}).get("alerts", True)
    if chat.id not in state.chats:
        state.add_chat(chat.id, title)
    await msg.reply_text(
        f"Weather alerts are {'on' if on else 'off'} here: rain starting and stopping, rain likely soon, "
        f"indoor/outdoor temperatures crossing after 2+ days, and unhealthy outdoor air (and when it's safe again). Use /alerts {'off' if on else 'on'} to turn them "
        f"{'off' if on else 'on'}.")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    remember_chat(update, context)

    if msg.chat.type == ChatType.PRIVATE:
        if not is_allowed(update):
            log.warning("Unauthorised message in %s: %s", describe_source(update), _short(msg.text))
            await msg.reply_text(f"Not authorised. Your user ID is {msg.from_user.id}.")
            return
        await respond(update, context, msg.text, "private message")
        return

    # Group / supergroup: only respond when addressed
    username = context.bot.username
    mention = re.compile(rf"@{re.escape(username)}\b", re.IGNORECASE)
    replied_to_bot = (
        msg.reply_to_message is not None
        and msg.reply_to_message.from_user is not None
        and msg.reply_to_message.from_user.id == context.bot.id
    )
    mentioned = bool(mention.search(msg.text))
    if not (mentioned or replied_to_bot):
        return
    if not is_allowed(update):
        log.warning("Ignored (not allowlisted) %s: %s", describe_source(update), _short(msg.text))
        return  # stay quiet in unauthorised groups
    await respond(update, context, mention.sub("", msg.text), "mention" if mentioned else "reply to bot")


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        log.warning("Ignored /ask (not allowlisted) %s", describe_source(update))
        return
    text = " ".join(context.args)
    if not text:
        await update.effective_message.reply_text("Usage: /ask <question>")
        return
    await respond(update, context, text, "/ask")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_chat(update, context)
    log.info("/start in %s", describe_source(update))
    await update.effective_message.reply_text(
        "Hi! Message me directly, or in groups @mention me, reply to me, or use /ask.\n"
        "/reset clears this chat's memory, /tools lists connected tools, /alerts manages weather alerts.\n"
        f"Your user ID: {update.effective_user.id} | Chat ID: {update.effective_chat.id}"
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    states.pop(chat_key(update.effective_message), None)
    log.info("/reset in %s", describe_source(update))
    await update.effective_message.reply_text("Conversation memory cleared.")


async def cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    log.info("/tools in %s", describe_source(update))
    mcp: MCPManager = context.bot_data["mcp"]
    for chunk in split_message(mcp.describe()):
        await update.effective_message.reply_text(chunk)


def _connection_problem(err) -> bool:
    """A dropped or timed-out connection (BadRequest is a NetworkError subclass, but a real error)."""
    return isinstance(err, NetworkError) and not isinstance(err, BadRequest)


def polling_error(error: TelegramError):
    """Errors while polling Telegram for new messages. The library retries by itself (waiting
    up to 30s between tries), so a dropped connection is just a one-line warning, not a
    traceback. Must not raise."""
    try:
        if _connection_problem(error):  # includes TimedOut
            log.warning("Telegram connection problem while polling (%s: %s) - retrying",
                        type(error).__name__, error)
        else:
            log.error("Telegram polling error (%s: %s)", type(error).__name__, error)
    except Exception:
        pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Errors raised inside handlers: network blips get one line, anything else a full traceback."""
    err = context.error
    if _connection_problem(err):
        log.warning("Telegram connection problem while handling a message (%s: %s)", type(err).__name__, err)
    else:
        log.error("Unhandled error while handling a message", exc_info=err)


def station_names(devices_text: str) -> list[str]:
    """"'Fairleigh' (CC:50:E3:D1:15:DA)" for each station in the cached device list."""
    try:
        data = json.loads(devices_text)
    except (TypeError, ValueError):
        return []
    found = []
    def walk(node):
        if isinstance(node, dict):
            if node.get("mac"):
                found.append(f"'{node.get('name', '?')}' ({node['mac']})")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(data)
    return found


async def prefetch_devices(mcp: MCPManager) -> str:
    """The device list never changes, so fetch it once at startup and put it in the
    prompt instead of letting the model spend a round trip on it every question."""
    for oname in list(mcp.tools):
        if oname.endswith("__get_devices"):
            info = await mcp.call(oname, "{}", quiet=True)
            if not info.startswith(("Error", "Tool reported an error")):
                mcp.remove_tool(oname)
                log.debug("Device list cached in prompt: %s", info[:300])
                return info
            log.warning("get_devices failed at startup, leaving it available to the model: %s", info[:300])
    return "(not available - use a tool to find devices)"


async def main():
    if ALLOWED_USERS or ALLOWED_CHATS:
        log.info("Allowlist active: %d users, %d chats", len(ALLOWED_USERS), len(ALLOWED_CHATS))
    else:
        log.debug("No allowlist set - bot will answer anyone")

    client = AsyncOpenAI(api_key=XAI_API_KEY, base_url=XAI_BASE_URL)

    async with MCPManager(MCP_CONFIG, TIMEZONE) as mcp:
        agent = Agent(client, XAI_MODEL, mcp, MAX_TOOL_STEPS, XAI_REASONING_EFFORT)

        devices = await prefetch_devices(mcp)
        stations = station_names(devices)
        if stations:
            log.info("Weather station: Ecowitt %s", ", ".join(stations))
        else:
            log.warning("Weather station: no Ecowitt device found")
        for oname in list(mcp.tools):
            if oname.endswith("__get_current_datetime"):
                mcp.remove_tool(oname)  # the prompt already has the local time
        air = None
        if AIRGRADIENT_API_TOKEN and AIRGRADIENT_LOCATION_ID:
            air = AirGradient(AIRGRADIENT_API_TOKEN, AIRGRADIENT_LOCATION_ID, TIMEZONE)
            mcp.add_local_tool("air_quality", AIR_DESCRIPTION, AIR_PARAMETERS, air.handle)
            log.info("Air quality: AirGradient location %s", AIRGRADIENT_LOCATION_ID)
        else:
            log.info("Air quality off: set AIRGRADIENT_API_TOKEN and AIRGRADIENT_LOCATION_ID to enable")
        cache = HistoryCache(ECOWITT_CACHE, ecowitt_history.UNIT_PARAMS)
        if not ecowitt_history.install(mcp, TIMEZONE, cache):
            log.warning("get_device_historical_info not found - history questions won't work")
        macs = macs_from(devices)
        recent = None
        if ECOWITT_KEEP_WARM and ecowitt_history.INSTALLED and macs:
            recent = RecentData(ecowitt_history.fetcher_factory(mcp, TIMEZONE, cache), TIMEZONE, macs)
        weather_alerts = bool(ALERTS and recent and len(macs) == 1)
        state = BotState(BOT_STATE) if ALERTS and (weather_alerts or air) else None
        if ALERTS and not weather_alerts:
            log.warning("Weather alerts not started: they need keep-warm on and exactly one station")

        app = (Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(True)
               .defaults(Defaults(disable_notification=True))  # silent messages
               .connect_timeout(10).read_timeout(15).write_timeout(15)  # default 5s is tight
               .build())
        app.bot_data.update(agent=agent, mcp=mcp, devices=devices, macs=macs, recent=recent, state=state, air=air)
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_start))
        app.add_handler(CommandHandler("ask", cmd_ask))
        app.add_handler(CommandHandler("reset", cmd_reset))
        app.add_handler(CommandHandler("tools", cmd_tools))
        app.add_handler(CommandHandler("alerts", cmd_alerts))
        app.add_handler(ChatMemberHandler(on_membership, ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
        app.add_error_handler(on_error)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                pass

        # Run everything in one task so MCP connections open and close cleanly
        async with app:
            await app.start()
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True,
                                            error_callback=polling_error)
            log.info("Bot @%s running with model %s (reasoning: %s, forecast questions: %s)",
                     app.bot.username, XAI_MODEL, XAI_REASONING_EFFORT, XAI_FORECAST_REASONING_EFFORT)

            archive_task = air_task = archive = air_warm_task = warm_task = warmup_task = None
            try:  # any startup failure below still runs the shutdown steps (and shows the real error)
                air_warm_task = asyncio.create_task(air.keep_warm(skip_first=True)) if air else None
                async def notify(text: str, link: tuple[str, str] | None = None):
                    await send_to_all(app.bot, state, text, link)
                if state and air:
                    air_monitor = AirMonitor(air, TIMEZONE, state, notify, link=AIR_LINK)

                    async def air_loop():
                        while True:
                            try:
                                await air_monitor.check()
                            except Exception as e:  # sensor or network trouble: try again next time
                                log.warning("Air quality check failed: %s", e)
                            await asyncio.sleep(AIR_CHECK_SECONDS)
                    air_task = asyncio.create_task(air_loop())
                if state and weather_alerts:
                    lon = re.search(r'"longitude"\s*:\s*(-?[0-9.]+)', devices or "")
                    monitor = Monitor(ecowitt_history.fetcher_factory(mcp, TIMEZONE, cache), TIMEZONE, macs[0], state, notify,
                                      longitude=float(lon.group(1)) if lon else 145.0)
                    try:
                        await monitor.init()
                        recent.after_refresh.append(monitor.check)
                    except Exception:
                        log.exception("Weather alerts couldn't start")
                if state:
                    kinds = (["rain", "rain likely", "temperature crossing"] if recent and recent.after_refresh else []) + \
                            ([f"air quality (every {AIR_CHECK_SECONDS // 60} min)"] if air else [])
                    log.info("Alerts: %s, to %d chat(s)", ", ".join(kinds) or "none", len(state.alert_chats()))
                warm_task = asyncio.create_task(recent.keep_warm(skip_first=True)) if recent else None
                warm_parts = (["Ecowitt"] if recent else []) + (["AirGradient"] if air else [])
                if warm_parts:
                    log.info("Keeping warm every %ds: %s recent data", REFRESH_SECONDS, " + ".join(warm_parts))
                if not ECOWITT_ARCHIVE:
                    log.info("Archive disabled (ECOWITT_ARCHIVE=false)")
                elif not ecowitt_history.INSTALLED or not macs:
                    log.warning("Archive not started: no history tool or no device MAC found")
                else:
                    archive = Archive(mcp, TIMEZONE, cache, macs, ECOWITT_ARCHIVE_GROUPS, ECOWITT_ARCHIVE_TIME)
                    archive_task = asyncio.create_task(archive.loop(skip_first=True))

                async def startup_warmup():
                    """Fetch everything once, together, and log one summary line."""
                    started, parts = time.monotonic(), []

                    async def ecowitt_part():
                        if recent:
                            parts.append(f"Ecowitt recent data {await recent.fetch(refresh=True)} request(s)")
                            for check in recent.after_refresh:  # alert checks on fresh readings straight away
                                await check()
                        if archive:
                            added, failed = await archive.run_once()
                            parts.append(f"5-min archive +{added} day(s)" + (f", {failed} failed" if failed else ""))

                    async def air_part():
                        if air:
                            before = air.requests
                            await air.warm(refresh=True)
                            parts.append(f"AirGradient {air.requests - before} request(s)")
                    try:
                        await asyncio.gather(ecowitt_part(), air_part())
                    except Exception:
                        log.exception("Startup warm-up failed (the regular refreshes will catch up)")
                    log.info("Startup warm-up: %s, %.1fs", ", ".join(parts) or "nothing to fetch", time.monotonic() - started)
                warmup_task = asyncio.create_task(startup_warmup())
                await stop.wait()
            finally:
                for task in (warmup_task, archive_task, warm_task, air_task, air_warm_task):
                    if task:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                await app.updater.stop()
                await app.stop()
                cache.close()
                if air:
                    await air.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
