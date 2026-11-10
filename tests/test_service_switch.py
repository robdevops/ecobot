"""The bot's own answers (everything it answers without the model) pinned word for word, chart for chart, on the archived fake station
at a fixed time (Tue 10 Nov 2026 2:05pm, Melbourne), so moving them onto the service's tools (docs/mcp-service.md 6, step 2) changes
nothing the person sees but what is meant to change.

What is pinned, for each question the bot answers in code (its keyboard's charts, the shortcuts and plain lookups intent.read answers,
the report, the dashboard, an alert's chart, a period button's redraw): every message sent (its text or caption, and its buttons), the
text the answer leaves in the chat's history, and each chart as JSON (specs.to_json, less `drawn_by`, which only the service's charts
carry) and the gauges' sheet. They are recorded in tests/service_switch.json: `ECOBOT_RECORD_SWITCH=1 pytest tests/test_service_switch.py`
writes it afresh (only ever on purpose).

The clock: every `datetime.now()`, `date.today()` and `time.time()` in lib/ and tests/ reads the fixed time, and the station's archive is
built under it (once per module), so every figure, time and point is the same on every run."""

import asyncio
import hashlib
import json
import os
import shutil
import sys
import time as _time
from datetime import date as _date, datetime as _datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

GOLDEN = Path(__file__).with_name("service_switch.json")
RECORD = os.environ.get("ECOBOT_RECORD_SWITCH") == "1"
FROZEN = _datetime(2026, 11, 10, 14, 5, tzinfo=ZoneInfo("Australia/Melbourne")).timestamp()
_REAL = (_datetime, _date)   # the real classes, where freeze() can't rebind them (it rebinds module globals, never a tuple's items)


# ---------- the clock, fixed ----------
class _DatetimeType(type(_REAL[0])):   # (type, unless ECOBOT_CLOCK moved the clock: tests/sweepclock.py)
    def __instancecheck__(cls, obj):
        return isinstance(obj, _REAL[0])

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _REAL[0])


class FrozenDatetime(_datetime, metaclass=_DatetimeType):
    """datetime whose now() and today() are the fixed time (anything else, and its instances, are the real datetime's)."""

    @classmethod
    def now(cls, tz=None):
        return _REAL[0].fromtimestamp(FROZEN, tz)

    @classmethod
    def today(cls):
        return _REAL[0].fromtimestamp(FROZEN)


class _DateType(type(_REAL[1])):
    def __instancecheck__(cls, obj):
        return isinstance(obj, _REAL[1])

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _REAL[1])


class FrozenDate(_date, metaclass=_DateType):
    @classmethod
    def today(cls):
        return _REAL[0].fromtimestamp(FROZEN).date()


def freeze(patch):
    """Fix the clock in every module of lib/ and tests/ (and ecobot.py) loaded so far, with `patch` (a MonkeyPatch)."""
    import lib.alerts.pictures, lib.bot, lib.compose, lib.ecowitt.archive, lib.forecast, lib.history, lib.pollen, lib.service.about  # noqa: F401
    import lib.service.current, lib.service.days, lib.service.forecast, lib.service.pollen, lib.service.summary  # noqa: F401
    import tests.fakes, tests.test_service_forecast, tests.test_pollen  # noqa: F401
    patch.setattr(_time, "time", lambda: FROZEN)
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "lib" or name.startswith(("lib.", "tests.")) or name == "ecobot"):
            continue
        if vars(module) is globals():   # (this module keeps the real classes, whatever it is loaded as: the frozen ones are built on them)
            continue
        for attr, value in list(vars(module).items()):
            if value is _REAL[0]:
                patch.setattr(module, attr, FrozenDatetime)
            elif value is _REAL[1]:
                patch.setattr(module, attr, FrozenDate)
    patch.setattr(sys.modules["lib.ecowitt.archive"], "epoch_now", lambda: FROZEN)


def test_the_clock_is_fixed_in_every_module_whatever_order_they_were_loaded_in(monkeypatch):
    """freeze() reaches a module loaded after this one too (it keeps its own names for the real classes, and leaves this module be),
    and the frozen classes still tell real dates and times apart."""
    import datetime as real
    import types
    late = types.ModuleType("tests._loaded_late")
    late.datetime, late.date = real.datetime, real.date
    monkeypatch.setitem(sys.modules, "tests.test_service_switch", sys.modules[__name__])   # (as --import-mode=importlib names it)
    monkeypatch.setitem(sys.modules, late.__name__, late)   # (after this module in sys.modules)
    freeze(monkeypatch)
    assert late.datetime is FrozenDatetime and late.date is FrozenDate
    assert sys.modules[__name__]._datetime is real.datetime and sys.modules[__name__]._date is real.date
    now = FrozenDatetime.now(ZoneInfo("Australia/Melbourne"))
    assert now.timestamp() == FROZEN and isinstance(now, FrozenDatetime) and isinstance(FrozenDate.today(), FrozenDate)
    assert issubclass(real.datetime, FrozenDatetime) and not isinstance(now.date(), FrozenDatetime)


async def _inline(func, /, *args, **kwargs):
    return func(*args, **kwargs)


@pytest.fixture(scope="module")
def frozen_archive(tmp_path_factory):
    """The fake station's whole archive, built at the fixed time (about 8 s, once for the module)."""
    from lib.ecowitt import Archive, Ecowitt, api, archive
    from tests.fakes import config, ecowitt_transport

    async def build(path):
        transport, _ = ecowitt_transport()
        eco = Ecowitt(config(path), transport=transport)
        await eco.start()
        await Archive(eco).run_once()
        await eco.close()
        return config(path).storage_path
    path = tmp_path_factory.mktemp("frozen")
    with pytest.MonkeyPatch.context() as patch:
        freeze(patch)
        with mock.patch.object(api, "MIN_GAP_SECONDS", 0), mock.patch.object(archive, "PACE_SECONDS", 0), \
                mock.patch.object(asyncio, "to_thread", _inline):
            return asyncio.run(build(path))


# ---------- the bot over every source ----------
def summary_of(chart: dict) -> dict:
    """What a chart's JSON is pinned as: its hash (less drawn_by), and enough of it to see where two differ."""
    chart = {k: v for k, v in chart.items() if k != "drawn_by"}
    panels = [{"label": p["label"], "unit": p["unit"],
               "series": [[s["name"], s["kind"], len(s["points"]), s["points"][:1], s["points"][-1:]] for s in p["series"]]}
              for p in chart["panels"]]
    digest = hashlib.sha256(json.dumps(chart, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return {"title": chart["title"], "subtitle": chart["subtitle"], "period": chart["period"], "source": chart["source"],
            "panels": panels, "sha": digest}


def buttons(markup) -> list | None:
    rows = getattr(markup, "inline_keyboard", None)
    return [[[b.text, b.callback_data] for b in row] for row in rows] if rows else None


class World:
    """The archived station (its live reading the wet night of tests/test_service_current), the fake AirGradient on the same file, the
    forecast and the pollen, all at the fixed time; and what the bot drew and sent."""

    def __init__(self):
        self.charts: list[dict] = []
        self.drawn_by: list[dict | None] = []   # each chart's drawn_by (the service's charts only)
        self.sheets: list[str] = []
        self.sent: list[list] = []
        self.edits: list[list] = []
        self.toasts: list = []

    async def open(self, tmp_path, archive):
        from lib.airgradient import AirGradient
        from lib.compose import Composer
        from lib.ecowitt import Ecowitt
        from lib.forecast import Forecast
        from tests.fakes import TZ, config, ecowitt_transport
        from tests.test_pollen import make as make_pollen, page
        from tests.test_service_current import Station, live
        from tests.test_service_forecast import open_meteo
        cfg = config(tmp_path)
        shutil.copy(archive, cfg.storage_path)
        transport, _ = ecowitt_transport()
        eco = Ecowitt(cfg, transport=transport)
        await eco.start()
        Station(eco, live(FROZEN, **{"solar_and_uvi.solar": "412.5", "solar_and_uvi.uvi": "6", "wind.wind_speed": "14.0",
                                     "wind.wind_gust": "31.0", "indoor.humidity": None}))   # (the fake station has no indoor humidity)
        air_transport_, _ = __import__("tests.fakes", fromlist=["air_transport"]).air_transport()
        air = AirGradient(cfg, transport=air_transport_, storage=eco.storage, owned=False)
        await air.start()
        forecast = Forecast(config(tmp_path, forecast=True, forecast_lat=-37.8, forecast_lon=144.9, place="Melbourne"), None,
                            open_meteo([], 7))
        await forecast.warm(True)
        pollen, _ = make_pollen(tmp_path, lambda: page(grass="Moderate", asthma="Low"))
        pollen.now = lambda: _datetime.fromtimestamp(FROZEN, TZ).replace(tzinfo=None)
        await pollen.warm(True)
        self.sources = [eco, air, pollen, forecast]
        for source in self.sources:   # a question's fetch of the newest readings while it is answered races the answer: not here
            source.poke = lambda: None
        self.eco, self.air, self.pollen, self.forecast = eco, air, pollen, forecast
        self.composer = Composer(eco, air)
        from lib import model_tools
        from lib.service import Service
        from lib.service.context import Context
        self.service = Service.of(await Context.open(eco=eco, air=air, composer=self.composer, forecast=forecast, pollen=pollen, tz=TZ,
                                                     clock=lambda: FROZEN))
        self.tools = model_tools.tools(self.service)
        self.tz, self.cfg = TZ, cfg
        return self

    async def close(self):
        for source in (self.air, self.eco, self.forecast, self.pollen):
            close = getattr(source, "close", None)
            if close:
                await close()

    def bot(self, alert_state, monkeypatch):
        from lib import bot as botmod
        from lib.llm import Agent

        def render(data, tz, times=None, dark=None):
            self.charts.append(summary_of(data))
            self.drawn_by.append(data.get("drawn_by"))
            return b"png"

        def gauges(sheet):
            self.sheets.append(hashlib.sha256(repr(sheet).encode()).hexdigest()[:16])
            return b"gauges"
        monkeypatch.setattr(botmod, "render_data", render)
        monkeypatch.setattr(botmod.gauges, "render", gauges)
        return botmod.Bot(NS(tz=self.tz), Agent(None, "m", self.tools), self.sources, alert_state, service=self.service)

    def message(self, message_id=50):
        world = self

        async def reply_text(body, **kw):
            world.sent.append(["text", body, buttons(kw.get("reply_markup"))])
            return NS(message_id=100 + len(world.sent))

        async def reply_photo(photo, caption=None, **kw):
            world.sent.append(["photo", caption, buttons(kw.get("reply_markup"))])
            return NS(message_id=100 + len(world.sent), photo=[NS(file_id=f"file{len(world.sent)}")])

        async def reply_media_group(media, **kw):
            world.sent.append(["photos", [m.caption for m in media], None])

        async def edit_media(media, reply_markup=None):
            world.edits.append(["photo", media.caption, buttons(reply_markup)])
            return NS(photo=[NS(file_id="edited")])
        return NS(chat_id=-5, message_id=message_id, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
                  reply_photo=reply_photo, reply_media_group=reply_media_group, edit_media=edit_media,
                  chat=NS(type="supergroup", title="Weather", id=-5), from_user=NS(full_name="Rob"), reply_to_message=None, photo=None)


async def nothing(*a, **k):
    pass


CONTEXT = NS(bot=NS(send_chat_action=nothing, send_message_draft=nothing, id=99, username="ecobot"))


@pytest.fixture
async def world(tmp_path, frozen_archive, monkeypatch):
    freeze(monkeypatch)
    w = await World().open(tmp_path, frozen_archive)
    yield w
    await w.close()


# ---------- what is pinned ----------
def golden() -> dict:
    return json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}


def period_rows(sent: list) -> list:
    """The period buttons under each message sent (None for a message with none, or with other buttons)."""
    return [s[2] if s[2] and all(d.startswith("pd:") for row in s[2] for _, d in row) else None for s in sent]


def without_period_rows(record: dict) -> dict:
    """What was done, less the period buttons (deliberately changed: they come from the chart's drawn_by now)."""
    record = json.loads(json.dumps(record, ensure_ascii=False))
    for s, row in zip(record.get("sent", []), period_rows(record.get("sent", []))):
        if row is not None:
            s[2] = None
    return record


def pinned(name: str, got: dict):
    """Compare what the bot did with the recording (or record it), its period buttons aside (period_buttons_are_drawn_bys)."""
    if RECORD:
        data = golden()
        data[name] = got
        GOLDEN.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
        return
    if dump := os.environ.get("ECOBOT_SWITCH_DUMP"):   # (a folder: what was done, to compare with the recording by hand)
        Path(dump).mkdir(parents=True, exist_ok=True)
        (Path(dump) / f"{hashlib.sha1(name.encode()).hexdigest()[:10]}.json").write_text(
            json.dumps({"name": name, "got": got}, indent=1, ensure_ascii=False, sort_keys=True))
    want = golden().get(name)
    assert want is not None, f"{name} isn't recorded: run with ECOBOT_RECORD_SWITCH=1"
    assert without_period_rows(got) == without_period_rows(want)


def period_buttons_are_drawn_bys(world, got: dict):
    """The deliberate change: a single chart's buttons are its drawn_by's periods, each labelled as the service labels it (none when it
    lists none: a count per month no longer offers 1D), each button's data the period alone, within Telegram's 64 bytes."""
    rows = [row for row in period_rows(got["sent"]) if row is not None]
    photos = [s for s in got["sent"] if s[0] == "photo"]
    drawn = [d for d in world.drawn_by if d]
    if len(photos) == 1 and len(world.charts) == 1 and drawn:
        listed = drawn[0]["periods"]
        assert rows == ([[[[p["label"], f"pd:{p['period']}"] for p in listed]]] if listed else [])
        assert all(len(d.encode()) <= 64 for row in rows for r in row for _, d in r)
    else:
        assert rows == []


async def asked(world, alert_state, monkeypatch, text) -> dict:
    """Ask the bot `text`, as a group's message to it, and return what it did."""
    bot = world.bot(alert_state, monkeypatch)
    msg = world.message()
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob", id=7))
    await bot.respond(update, CONTEXT, text)
    history = bot.chats[(msg.chat_id, None)].history
    said = history[-1]["content"] if history and history[-1]["role"] == "assistant" else None
    return {"sent": world.sent, "charts": world.charts, "gauges": world.sheets, "history": said}


# Every question the bot answers in code: the keyboard's charts (templates.chart_sentences, the same with irrigation: only Wind goes),
# then each of intent's shortcuts (bot._answer: a named reading's chart and a period, the dashboard, the report and "weather now")
from lib import templates  # noqa: E402

KEYBOARD = templates.chart_sentences(False)
SHORTCUTS = [
    # the report and "weather now" (written in code from current, pollen and forecast)
    "Report", "weather now", "status",
    # the gauges
    "dashboard",
    # a named reading's chart and a period (summary with the words as they are; the caption goes with it)
    "temperature chart 7d", "humidity 7d", "rain chart 7d", "pressure chart this month", "temperature and rain chart 7d",
    "weather 30d", "indoor temperature chart 7d", "wind direction plot 3m", "temperature chart last month", "air quality chart 7d",
    "pm2.5 chart 3d",
]
# What the bot answered in code until step 3 and the model answers now (with no model: NO_MODEL)
TO_THE_MODEL = [
    "how hot is it", "is it raining", "humidity", "wind", "indoor temperature", "uv", "pressure",          # a reading now
    "air quality now", "co2", "how's the air",                                                             # the air now
    "pollen", "forecast", "what can you do",
    "how many days was the temperature over 18 this year", "how many days was humidity under 60 last month",   # days
    "how many days was pm2.5 over 15 this week", "days with uv over 9 per month this year",
    "rain by month this year", "average temperature per month this year",
    "highest temperature yesterday", "high and low today", "lowest humidity yesterday", "average pressure yesterday",   # extremes
    "strongest gust today", "temps today",
    "weather all week", "average temperature 7d", "plot uv and solar on two axes 7d", "lowest temperature on record",    # charts beside
    "highest humidity this week", "temps this week", "chart weather",                                                   # a question
]
CHANGED = {"air quality chart 7d"}   # a shortcut whose words the service reads as every air reading now (until step 3: PM2.5 alone)


@pytest.mark.parametrize("text", TO_THE_MODEL)
async def test_what_was_answered_in_code_goes_to_the_model(world, alert_state, monkeypatch, text):
    from lib.bot import NO_MODEL
    got = await asked(world, alert_state, monkeypatch, text)
    assert got["history"] == NO_MODEL and got["charts"] == [] and got["gauges"] == []


@pytest.mark.parametrize("text", KEYBOARD + SHORTCUTS)
async def test_the_bots_own_answers_are_unchanged(world, alert_state, monkeypatch, text):
    got = await asked(world, alert_state, monkeypatch, text)
    pinned(text, got)
    if not RECORD:
        period_buttons_are_drawn_bys(world, got)


@pytest.mark.parametrize("question, press", [("Temperature chart 1d", "7D"), ("Rain chart 1d", "1M"), ("humidity 7d", "1Y")])
async def test_a_period_button_redraws_the_same_chart_as_before(world, alert_state, monkeypatch, question, press):
    """A press on a period button under a chart: the chart over that period, put into the same message."""
    bot = world.bot(alert_state, monkeypatch)
    msg = world.message()
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob", id=7))
    await bot.respond(update, CONTEXT, question)
    sent_id = 100 + len(world.sent)
    row = next((s[2] for s in world.sent if s[0] == "photo" and s[2]), None)
    assert row, "a single chart with its period buttons"
    data = next(d for r in row for t, d in r if t == press)
    under = world.message(sent_id)
    under.reply_markup = NS(inline_keyboard=[[NS(text=t, callback_data=d) for t, d in r] for r in row])
    world.charts.clear()

    async def answer(text=None, **k):
        world.toasts.append(text)
    query = NS(data=data, message=under, answer=answer)
    await bot.on_period_button(NS(callback_query=query, effective_message=under, effective_chat=under.chat,
                                  effective_user=NS(username="rob", full_name="Rob", id=7)), CONTEXT)
    got = {"charts": world.charts, "edits": [e[:2] for e in world.edits]}
    pinned(f"press {press} under {question}", got)


@pytest.mark.parametrize("kind", ["gusts", "uv", "temps", "particulates"])
async def test_an_alerts_chart_is_the_same_as_before(world, monkeypatch, kind):
    from lib.alerts import pictures
    captured = []
    monkeypatch.setattr(pictures, "render_data", lambda data, tz, *a, **k: captured.append(summary_of(data)) or b"png")
    source = world.air if kind == "particulates" else world.eco
    picture = await pictures.chart(source, pictures.CHARTS[kind], world.tz)
    got = {"title": picture.title, "newest": picture.newest, "charts": captured}
    if RECORD:
        pinned(f"alert {kind}", got)
        return
    # The deliberate change: an alert's chart is the service's chart of its request, the very chart the keyboard's button draws ("Wind
    # chart 1d"), so it names its source under it as that one does (the old drew it through the tool's handler, which named none).
    want = golden()[f"alert {kind}"]
    (chart,), (old,) = got["charts"], want["charts"]
    assert old["source"] == "" and chart["source"] == ("AirGradient sensor" if kind == "particulates" else "Ecowitt weather station")
    assert {k: v for k, v in chart.items() if k not in ("source", "sha")} == {k: v for k, v in old.items() if k not in ("source", "sha")}
    assert {k: v for k, v in got.items() if k != "charts"} == {k: v for k, v in want.items() if k != "charts"}
    keyboard = golden().get(pictures.CHARTS[kind])
    if keyboard:   # (the UV chart is no button)
        assert chart == keyboard["charts"][0]


# ---------- what follows from the model's calls ----------
class Scripted:
    """A model that makes these steps' calls ([(tool, arguments)] each), then answers `reply` (an exception: raised instead)."""

    def __init__(self, steps: list[list[tuple[str, dict]]], reply):
        self.steps, self.reply = list(steps), reply
        self.chat = NS(completions=self)

    async def create(self, **kw):
        if self.steps:
            calls = [NS(id=f"c{i}", function=NS(name=name, arguments=json.dumps(args))) for i, (name, args) in enumerate(self.steps.pop(0))]
            return NS(choices=[NS(message=NS(content="", tool_calls=calls))], usage=None)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return NS(choices=[NS(message=NS(content=self.reply, tool_calls=None))], usage=None)


async def asked_the_model(world, alert_state, monkeypatch, text, steps, reply="ok") -> list:
    """Ask the bot `text` with a scripted model (Scripted), the air sensor's live-chart link configured: the text or caption of each
    message it sent."""
    from lib.llm import Agent
    bot = world.bot(alert_state, monkeypatch)
    bot.agent = Agent(Scripted(steps, reply), "m", world.tools)
    world.air.link = ("live chart", "https://air.example")
    msg = world.message()
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob", id=7))
    await bot.respond(update, CONTEXT, text)
    return [s[1] for s in world.sent]


LINK_CASES = [   # (question, the model's steps, does the live-chart link go under the reply)
    ("what's the temperature outside", [[("current", {})]], False),                        # every sensor read: not an air answer
    ("what's it like out", [[("current", {"readings": ["all"]})]], False),
    ("how much rain this week", [[("summary", {"readings": ["rainfall total"]})],         # a refusal lists every reading's words
                                 [("summary", {"readings": ["rain"], "period": "7d"})]], False),
    ("temperature readings", [[("readings", {"readings": ["temperature"], "period": "today"})]], False),   # refused (bad arguments)
    ("hottest days", [[("days", {"sort_by": "temp_max", "period": "this month"})]], False),
    ("how's the air", [[("current", {"readings": ["air"]})]], True),
    ("pm2.5 and temperature now", [[("current", {"readings": ["pm2.5", "temperature"]})]], True),
    ("how was the air this week?", [[("summary", {"readings": ["pm2.5"], "period": "7d"})]], True),
    ("smoky days", [[("days", {"where": [{"field": "pm2_5_max", "op": ">", "value": 15}], "period": "this month"})]], True),
]


@pytest.mark.parametrize("text, steps, link", LINK_CASES)
async def test_the_air_link_goes_under_an_answer_that_read_the_air_and_no_other(world, alert_state, monkeypatch, text, steps, link):
    """The live-chart link follows what was read (as before the swap, when air_quality's call brought it), never a key or a word
    anywhere in a result: not `current` of every sensor, nor a refusal's catalogue of readings."""
    said = await asked_the_model(world, alert_state, monkeypatch, text, steps)
    assert said and all(("live chart" in s) is link for s in said), said


async def test_an_error_reply_gets_no_air_link(world, alert_state, monkeypatch):
    """The air was read, then the model timed out: the apology goes alone."""
    said = await asked_the_model(world, alert_state, monkeypatch, "how's the air", [[("current", {"readings": ["air"]})]], TimeoutError())
    assert said == ["Sorry, that took too long. Please try again in a moment."]


@pytest.mark.parametrize("text", ["temperature 1d", "co2 1d", "pm2.5 1d"])
async def test_a_bare_reading_and_1d_is_still_its_chart_without_the_model(world, alert_state, monkeypatch, text):
    """As before the swap: "temperature 1d" is the chart of the last day, drawn in code (no model needed)."""
    from lib.bot import NO_MODEL
    got = await asked(world, alert_state, monkeypatch, text)
    assert got["history"] != NO_MODEL and len(got["charts"]) == 1
