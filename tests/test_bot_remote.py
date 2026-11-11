"""The bot as the service's client over MCP (BOT_MCP_URL: lib/weather.RemoteWeather, docs/ecobot-mcp.md 3.8 and step 5), against the
real server (lib/mcp_server.EcobotServer): over the SDK's in-memory transport for a question answered with the remote tools, a chart drawn
from a remote result with its buttons, an alert delivered through the listen and the irrigation check's inputs; under uvicorn on a localhost
port (a connection can fail there) for a dropped connection rebuilt with the alerts after the last id read once, the service down (the bot
says so), and ecobot.main(), always a client (step 6b). The rest of the suite runs the bot against ecobot-mcp in memory (tests/served.py)."""

import asyncio
import json
import time
from types import SimpleNamespace as NS

import pytest

import ecobot_mcp.__main__ as service_main
from lib import upstream
from lib import model_tools, weather as weather_module
from lib.alert_log import AlertLog
from lib.alerts.notify import AlertDelivery, AlertState, Notifier
from lib.bot import SERVICE_DOWN, Bot
from lib.llm import Agent
from lib.mcp_server import EcobotServer, http_app
from lib.service.context import Context
from lib.weather import RemoteWeather, ServiceForecast, ServiceRain, ServiceUnreachable
from tests import evalworld
from tests.fakes import TZ
from tests.test_mcp_listen import Serving, free_port, until

TOKEN = "s3cret"


def at_eval_now(patch: pytest.MonkeyPatch):
    evalworld.at_eval_now(patch, __name__)


@pytest.fixture(scope="module")
def eval_archive(shared_archive):
    """The fake station's whole archive (its ecobot.sqlite) built at EVAL_NOW, the air's month held in it too (real_context), once for every
    worker and run (tests/evalworld.py's)."""
    return evalworld.shared_eval_archive(shared_archive)


@pytest.fixture
async def world(tmp_path, eval_archive, monkeypatch):
    """The real server over the four sources (faked, the clock at the evals' NOW) and an alert log."""
    at_eval_now(monkeypatch)
    ctx = await evalworld.real_context(tmp_path, eval_archive)
    alerts = AlertLog(tmp_path / "alerts.sqlite", ctx.zone)
    server = EcobotServer(ctx, alerts)
    yield server
    alerts.close()
    await evalworld.close(ctx)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """Reconnections and waits in tenths of a second, not seconds."""
    monkeypatch.setattr(upstream, "BACKOFF", (0.1,))
    monkeypatch.setattr(weather_module, "CALL_WAIT", 0.3)
    monkeypatch.setattr(service_main, "SHUTDOWN_SECONDS", 0.25)   # (a listen still open when a server stops: tests/test_mcp_listen.py)


@pytest.fixture
async def connected(world):
    """A RemoteWeather over the server in memory, its supervisor running and listening."""
    remote = RemoteWeather(world.server, tz=None)
    task = asyncio.create_task(remote.run())
    await asyncio.wait_for(remote.listening.wait(), 10)
    yield remote
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await remote.close()


class Telegram:
    """What the bot sends, kept."""
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        return NS(photo=None)

    async def send_photo(self, chat_id, photo, **kw):
        self.sent.append((chat_id, kw.get("caption")))
        return NS(photo=(NS(file_id="small"), NS(file_id="big")))


def a_chat(kind="private"):
    """A message in a chat, its replies kept (text, or (caption, buttons) for a photo)."""
    replies = []

    async def reply_text(body, **kw):
        replies.append(body)
        return NS(message_id=8)

    async def reply_photo(photo, caption=None, reply_markup=None, **kw):
        replies.append((caption, reply_markup))
        return NS(message_id=9)

    async def nothing(*a, **k):
        pass
    msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text, reply_photo=reply_photo,
             chat=NS(type=kind, title="g" if kind != "private" else None), from_user=NS(full_name="Rob"), reply_to_message=None)
    update = NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))
    context = NS(bot=NS(send_chat_action=nothing, send_message_draft=nothing, id=99))
    return update, context, replies


class Model:
    """A model that makes `calls` (name, arguments) in its first step, then answers `answer`; what it was sent is kept."""

    def __init__(self, calls, answer="It's 14.2 °C."):
        self.calls, self.answer, self.seen = calls, answer, []
        self.chat = NS(completions=self)

    async def create(self, **kw):
        self.seen.append(kw)
        if len(self.seen) == 1 and self.calls:
            made = [NS(id=str(i), function=NS(name=name, arguments=json.dumps(args))) for i, (name, args) in enumerate(self.calls)]
            return NS(choices=[NS(message=NS(content="", tool_calls=made))], usage=None)
        return NS(choices=[NS(message=NS(content=self.answer, tool_calls=None))], usage=None)


# ---------- in memory ----------
async def test_a_question_is_answered_with_the_remote_tools_and_the_instructions_from_discover(connected, world, alert_state):
    model = Model([("current", {"readings": ["temperature"]})])
    bot = Bot(NS(tz=TZ), Agent(model, "m", model_tools.tools(connected)), alert_state, service=connected)
    update, context, replies = a_chat("group")   # (no draft streamed: the model's answer comes whole)
    await bot.respond(update, context, "how warm is it outside?")
    first = model.seen[0]
    assert [t["function"]["name"] for t in first["tools"]] == [t.name for t in world.service]   # the service's ten, as listed
    assert world.server.instructions and world.server.instructions in first["messages"][0]["content"]   # discover's instructions
    told = json.loads(model.seen[1]["messages"][-1]["content"])
    assert told == json.loads(await world.service["current"].call({"readings": ["temperature"]}))   # the remote tool's own answer
    assert replies == ["It's 14.2 °C."]
    assert bot.names == {"Ecowitt", "AirGradient", "Forecast", "Pollen"} and not hasattr(bot, "sources")   # (from ecobot://station; none here)


async def test_a_charts_picture_comes_from_the_service_with_its_period_buttons(connected, alert_state, monkeypatch):
    """The bot draws nothing: the chart's picture is the service's (asked for as an image), its JSON from the result's _meta."""
    got, photos = [], []
    asked = connected.call_images

    async def call_images(*a, **k):
        out, images = await asked(*a, **k)
        got.append(images)
        return out, images
    monkeypatch.setattr(connected, "call_images", call_images)
    bot = Bot(NS(tz=TZ), Agent(None, "m", model_tools.tools(connected)), alert_state, service=connected)
    update, context, replies = a_chat("group")
    reply_photo = update.effective_message.reply_photo

    async def kept(photo, **kw):
        photos.append(photo)
        return await reply_photo(photo, **kw)
    update.effective_message.reply_photo = kept
    await bot.respond(update, context, "Humidity chart 7d", call=("summary", {"readings": ["humidity"], "period": "7d", "chart": True}))   # (as a button: its call, no model)
    (caption, markup), = replies
    ((image,),) = got
    chart = image.chart
    assert photos == [image.png] and image.png.startswith(b"\x89PNG")
    assert chart["drawn_by"]["tool"] == "summary" and chart["drawn_by"]["arguments"]["period"] == "7d"
    assert len(max((s["points"] for p in chart["panels"] for s in p["series"]), key=len)) > 150   # (asked as drawn, not 150 points)
    assert [b.callback_data for b in markup.inline_keyboard[0]] == ["pd:1d", "pd:7d", "pd:1m", "pd:3m", "pd:1y"]
    assert bot.charted.get(1, 9)["drawn_by"] == chart["drawn_by"]


async def test_the_models_charts_come_from_the_service_as_pictures_and_the_model_gets_only_their_text(connected, world, alert_state,
                                                                                                      monkeypatch):
    """A chart in the model's answer: its call asked for the picture (chart_image "png"), the bot sends the service's PNG with its period
    buttons, and the model is sent the result's text alone (each chart's title and period, BOT_CHART_NOTE), never the image."""
    from lib import chart_images
    call = ("summary", {"readings": ["humidity"], "period": "7d", "chart": True})
    metas, photos = [], []
    calling = connected.upstream.client.call_tool

    async def call_tool(name, args, meta=None, **k):
        metas.append(meta)
        return await calling(name, args, meta=meta, **k)
    monkeypatch.setattr(connected.upstream.client, "call_tool", call_tool)
    model = Model([call], "Humid all week.")
    bot = Bot(NS(tz=TZ), Agent(model, "m", model_tools.tools(connected)), alert_state, service=connected)
    update, context, replies = a_chat("group")
    reply_photo = update.effective_message.reply_photo

    async def kept(photo, **kw):
        photos.append(photo)
        return await reply_photo(photo, **kw)
    update.effective_message.reply_photo = kept
    await bot.respond(update, context, "how humid was it this week? chart it")
    assert [m["chart_image"] for m in metas] == [chart_images.IMAGE]
    sent_to_model = model.seen[1]["messages"][-1]["content"]
    told = json.loads(sent_to_model)
    assert [set(c) for c in told["charts"]] == [{"title", "period"}] and told["chart_note"] == model_tools.BOT_CHART_NOTE
    assert "image" not in sent_to_model and "base64" not in sent_to_model
    (caption, markup), = replies
    out, (image,) = await connected.call_images(*call, {"chart_points": "drawn"})
    assert photos == [image.png] and image.png.startswith(b"\x89PNG") and "Humid all week." in caption
    assert [b.callback_data for b in markup.inline_keyboard[0]] == ["pd:1d", "pd:7d", "pd:1m", "pd:3m", "pd:1y"]
    assert bot.charted.get(1, 9)["drawn_by"] == out["charts"][0]["drawn_by"]


async def test_status_sends_the_services_picture_of_three_gauges_then_the_report_prompts_answer(connected, world, alert_state, monkeypatch):
    """Status (/status): the Status picture is ecobot://status's PNG, byte for byte, then the model's answer to ecobot-mcp's `report`
    prompt (got over MCP), asked as a typed question (step 6c: the report written in code, its caption and 🔄 are gone)."""
    import base64
    from lib.mcp_server import prompt
    photos = []
    model = Model([], "Weather station: 14.2 °C.")
    bot = Bot(NS(tz=TZ), Agent(model, "m", model_tools.tools(connected)), alert_state, service=connected)
    update, context, replies = a_chat("group")
    reply_photo = update.effective_message.reply_photo

    async def kept(photo, **kw):
        photos.append(photo)
        return await reply_photo(photo, **kw)
    update.effective_message.reply_photo = kept
    await bot.status(update, context)
    contents = (await connected.upstream.client.read_resource("ecobot://status")).contents
    (png,) = [base64.b64decode(c.blob) for c in contents if getattr(c, "blob", None)]
    assert photos == [png] and png.startswith(b"\x89PNG") and replies == [(None, None), "Weather station: 14.2 °C."]
    assert model.seen[0]["messages"][-1]["content"] == f"Rob: {prompt('report').messages[0].content.text}"   # (a group: its sender named)


async def test_ecobot_mcps_prompts_are_telegram_commands_whose_words_fill_their_arguments(connected, world, alert_state, caplog):
    """Step 6c: each of ecobot-mcp's prompts is a command in the menu (after the charts', before the settings), with its description;
    "/<prompt> <words>" fills its arguments in order, the last taking the rest, and the prompt's question is asked of the model as a typed
    question; none: the prompt's defaults. A prompt the service refuses is said in the chat; another bot's command is left alone."""
    from lib import templates
    from lib.mcp_server import PROMPTS, prompt
    model = Model([], "Done.")
    bot = Bot(NS(tz=TZ), Agent(model, "m", model_tools.tools(connected)), alert_state, service=connected)
    menus = []

    class Telegram:
        async def set_my_commands(self, commands):
            menus.append([(c.command, c.description) for c in commands])
    await bot.publish_commands(Telegram())
    own = list(templates.MENU)
    at = [name for name, _ in own].index("alerts")
    assert menus == [[*own[:at], *((name, what) for name, (what, _, _) in PROMPTS.items()), *own[at:]]]
    asked = lambda: model.seen[-1]["messages"][-1]["content"]
    question = lambda name, **args: prompt(name, args).messages[0].content.text
    for typed, want in (("/chart rain 7d", question("chart", reading="rain", period="7d")),
                        ("/chart pm2.5 last two weeks", question("chart", reading="pm2.5", period="last two weeks")),
                        ("/rainfall last week", question("rainfall", period="last week")),
                        ("/rainfall", question("rainfall")), ("/now", question("now")), ("/report@ecobot", question("report")),
                        ("/air", "How is the air quality now?"),
                        ("/will_it_rain tomorrow", f"{question('will_it_rain')} (tomorrow)")):   # (no arguments: the words follow it)
        update, context, replies = a_chat("group")
        update.effective_message.text, update.effective_message.chat.id, context.bot.username = typed, 1, "ecobot"
        await bot.on_command(update, context)
        assert asked() == f"Rob: {want}" and replies == ["Done."], typed      # (a group: its sender named)
    seen = len(model.seen)
    for typed in ("/chart", "/report@otherbot", "/nosuch rain"):
        update, context, replies = a_chat()
        update.effective_message.text, update.effective_message.chat.id, context.bot.username = typed, 1, "ecobot"
        await bot.on_command(update, context)
        assert replies == (["Sorry, /chart didn't work: The prompt 'chart' needs reading."] if typed == "/chart" else []), typed
    assert len(model.seen) == seen                                                        # none of these asked the model


async def test_while_ecobot_mcp_is_unreached_a_command_not_the_bots_own_says_so(alert_state):
    """Before ecobot-mcp is first reached its prompts (so their commands) aren't known: "/report" is told the service can't be reached,
    not left unanswered; another bot's command is still left alone."""
    from lib.bot import SERVICE_DOWN
    from tests.served import unreached
    weather = unreached()
    bot = Bot(NS(tz=TZ), Agent(None, "m", model_tools.tools(weather)), alert_state, service=weather)
    for typed, want in (("/report", [SERVICE_DOWN]), ("/report@otherbot", [])):
        update, context, replies = a_chat()
        update.effective_message.text, update.effective_message.chat.id, context.bot.username = typed, 1, "ecobot"
        await bot.on_command(update, context)
        assert replies == want, typed


async def test_a_prompt_that_is_no_command_name_or_is_one_of_the_bots_is_left_out_with_a_warning(connected, alert_state, caplog):
    from lib import templates
    from lib.weather import Prompt
    bot = Bot(NS(tz=TZ), Agent(None, "m", []), alert_state, service=connected)
    connected.prompts = [Prompt("Weather Now", "Bad name", ()), Prompt("rain", "Takes the Rain button's", ()),
                         Prompt("x" * 33, "Too long", ()), Prompt("frost", "Frost tonight?", ("place",))]
    menu = bot.commands()
    assert [name for name, _ in menu] == [*[n for n, _ in templates.MENU][:8], "frost", *[n for n, _ in templates.MENU][8:]]
    assert list(bot.prompt_commands) == ["frost"]
    assert "'Weather Now' isn't a name Telegram takes" in caplog.text and "'rain' has the name of one of the bot's own commands" in caplog.text
    assert f"'{'x' * 33}' isn't a name" in caplog.text


async def test_the_menu_is_set_again_when_ecobot_mcp_says_its_prompts_changed(connected, world, alert_state, monkeypatch):
    """PromptsListChanged: the prompts are read again and on_prompts called (ecobot.py sets the command menu again with them)."""
    from mcp.server.subscriptions import PromptsListChanged
    from lib import mcp_server
    called = []
    connected.on_prompts = lambda: called.append([p.name for p in connected.prompts])
    assert "frost" not in [p.name for p in connected.prompts]
    monkeypatch.setitem(mcp_server.PROMPTS, "frost", ("Frost tonight?", [], "Will there be frost tonight?"))
    await world.bus.publish(PromptsListChanged())
    await until(lambda: called, what="the prompts read again")
    assert called[-1][-1] == "frost"


async def drawn_by_the_service(world, call) -> tuple[dict, bytes]:
    """(a call's answer as the bot asks it, every point as drawn; its chart as ecobot-mcp's renderer draws it, light): what the bot is to
    be sent. Asked until two answers draw the same (what a call fetches inline, fetched first: the charts are then the same each time)."""
    from lib import chart_images
    for _ in range(3):
        out = json.loads(await world.service[call[0]].call(call[1], {"chart_points": "drawn"}))
        png = (await asyncio.to_thread(chart_images.render, out["charts"][0], TZ, False))[0]
        again = json.loads(await world.service[call[0]].call(call[1], {"chart_points": "drawn"}))
        if (await asyncio.to_thread(chart_images.render, again["charts"][0], TZ, False))[0] == png:
            break
    return out, png


async def test_the_bot_sends_the_services_picture_of_a_chart_byte_for_byte_as_its_renderer_draws_it(connected, world, alert_state,
                                                                                                      monkeypatch):
    """The bot draws nothing: a chart's picture is the PNG ecobot-mcp's renderer draws (lib/chart_images.render), its title, words,
    newest reading and drawn_by from the chart's JSON (the result's _meta); an alert's chart is its drawn_by replayed."""
    from lib import chart_images
    from lib.alerts import pictures
    from lib.specs import from_json
    from lib.timeutil import now_local
    call = ("summary", {"readings": ["humidity"], "period": "7d", "chart": True})
    out, png = await drawn_by_the_service(world, call)
    chart = out["charts"][0]
    event_chart = await pictures.chart_data(None, pictures.CHARTS["gusts"], TZ, world.service)   # (an alert's chart, as its event carries it)
    event_png = (await asyncio.to_thread(chart_images.render, event_chart, TZ, False))[0]
    bot = Bot(NS(tz=TZ), Agent(None, "m", model_tools.tools(connected)), alert_state, service=connected)
    made = await bot._draw_call(*call)
    spec = from_json(chart)
    assert made.png == png and made.png.startswith(b"\x89PNG") and made.dark is False   # the service's PNG, as its renderer draws it
    assert (made.title, made.caption, made.newest, made.drawn_by) == (spec.caption(now_local(TZ).date()), out.get("caption"), spec.newest,
                                                                      chart["drawn_by"])
    assert made.out["charts"] == out["charts"]                                               # the chart's JSON, from the result's _meta
    alert = await pictures.picture_of(event_chart, TZ, connected)                            # an alert's chart: its drawn_by replayed
    assert (alert.png, alert.drawn_by, alert.dark) == (event_png, event_chart["drawn_by"], False)
    assert alert.title == from_json(event_chart).caption(now_local(TZ).date())
    typed = await pictures.chart(pictures.CHARTS["gusts"], TZ, connected)                    # the 🔄 under an alert from before the log
    assert typed.png == event_png


async def test_a_service_without_pillow_sends_the_charts_json_and_the_bot_sends_no_picture(connected, world, alert_state, monkeypatch):
    """The bot draws nothing (docs/ecobot-mcp.md step 6a): a service that can't draw (no Pillow there) gives the answer with its charts'
    JSON and no picture, and the bot sends none; an alert's chart goes without its picture."""
    from types import SimpleNamespace
    from lib import chart_images, mcp_server
    from lib.alerts import pictures
    call = ("summary", {"readings": ["humidity"], "period": "7d", "chart": True})
    here = json.loads(await world.service["summary"].call(call[1], {"chart_points": "drawn"}))["charts"][0]
    event_chart = await pictures.chart_data(None, pictures.CHARTS["gusts"], TZ, world.service)
    monkeypatch.setattr(mcp_server, "chart_images", SimpleNamespace(**{**vars(chart_images), "renderer": lambda: None}))   # (no Pillow there)
    bot = Bot(NS(tz=TZ), Agent(None, "m", model_tools.tools(connected)), alert_state, service=connected)
    assert await bot._draw_call(*call) is None
    out, images = await connected.call_images(*call)
    assert out["chart_image_note"] == chart_images.UNAVAILABLE_NOTE and images == [] and out["charts"][0]["drawn_by"] == here["drawn_by"]
    assert await pictures.picture_of(event_chart, TZ, connected) is None


async def test_an_alert_reaches_the_subscribed_chats_through_the_listen(connected, world, tmp_path):
    publishing = asyncio.create_task(world.watch_alerts())
    state = AlertState(tmp_path / "bot_state.json")
    state.add_chat(1, "private")
    telegram = Telegram()
    delivery = asyncio.create_task(AlertDelivery(connected.alerts, Notifier(telegram, state), state, TZ).run())
    try:
        await until(lambda: state.read is not None, what="the delivery to begin")
        world.alert_log.append("rain", "🌧️ It's started raining: 0.4 mm so far", "Rain started")   # (no expiry: the clock here is the evals')
        await until(lambda: telegram.sent, what="the alert")
    finally:
        for task in (publishing, delivery):
            task.cancel()
        await asyncio.gather(publishing, delivery, return_exceptions=True)
    assert telegram.sent == [(1, "🌧️ It's started raining: 0.4 mm so far")] and state.read == 1


async def test_the_irrigation_check_reads_the_measured_rain_and_the_forecast_from_the_service(connected, world):
    rain = await ServiceRain(connected).rain_mm(24)
    summary = json.loads(await world.service["summary"].call({"readings": ["rain"], "period": "last 24 hours"}))
    assert rain == summary["rain_total_mm"]
    await world.ctx.forecast.warm()   # (the service fetches its forecast)
    forecast = connected.forecast
    await forecast.refresh()          # (as the check does; the listen's ResourceUpdated does it too)
    assert forecast.days
    assert [d["date"] for d in forecast.days] == [d["date"] for d in world.ctx.forecast.days]
    assert forecast.fetched_at == pytest.approx(world.ctx.forecast.fetched_at, abs=1)


async def test_the_irrigation_check_runs_on_the_forecast_last_read_when_the_service_cant_be_reached(caplog):
    from datetime import date, datetime
    from lib.alerts.irrigation import IrrigationMonitor

    class Down:
        tz = TZ

        async def call_text(self, *a, **k):
            raise ServiceUnreachable()

        async def resource(self, uri, wait=None):
            raise ServiceUnreachable()
    forecast = ServiceForecast(Down())
    now = datetime(2026, 11, 10, 18, 0, tzinfo=TZ)
    forecast.days, forecast.fetched_at = [{"date": date(2026, 11, 11), "rain_1mm_pct": 70}], now.timestamp() - 3600   # (read an hour ago)
    forecast.now = lambda: now
    monitor = IrrigationMonitor(None, NS(monitor={}), None, TZ, station=ServiceRain(Down()), forecast=forecast)
    assert await monitor.rain_reason(now) == "70% chance of at least 1 mm tomorrow"
    assert "the measured rain is unavailable" in caplog.text and "using the one last read" in caplog.text


# ---------- under uvicorn ----------
@pytest.fixture
def bare(tmp_path):
    """A server with no sources (the alerts and their listen are what matter) and its alert log."""
    alerts = AlertLog(tmp_path / "alerts.sqlite", TZ)
    yield EcobotServer(Context(tz=TZ), alerts)
    alerts.close()


async def test_a_dropped_connection_is_rebuilt_and_the_alerts_after_the_last_id_are_read_once(bare, tmp_path):
    port = free_port()
    remote = RemoteWeather(f"http://127.0.0.1:{port}/mcp", TOKEN, TZ)
    state = AlertState(tmp_path / "bot_state.json")
    state.add_chat(1, "private")
    telegram = Telegram()
    publishing = asyncio.create_task(bare.watch_alerts())
    supervisor = asyncio.create_task(remote.run())
    delivery = asyncio.create_task(AlertDelivery(remote.alerts, Notifier(telegram, state), state, TZ).run())
    texts = lambda: [text for _, text in telegram.sent]
    alerts = bare.alert_log
    try:
        async with Serving(http_app(bare, tokens=(TOKEN,)), port) as first:
            await until(lambda: state.read is not None and bare.listen._streams, what="the listen")
            alerts.append("rain", "first", "Rain started", expires=time.time() + 3600)
            await until(lambda: texts() == ["first"], what="alert 1")
            await first.stop()                                              # the service goes away: the listen is lost
        await until(lambda: not remote.upstream.connected.is_set(), what="the bot to notice")
        assert not await remote.ready(0.1)
        alerts.append("gusts", "missed", "Gusts", expires=time.time() + 3600)   # sent while it was down
        alerts.append("uv", "stale", "UV", expires=time.time() - 1)             # ... and expired already: not news
        async with Serving(http_app(bare, tokens=(TOKEN,)), port):
            await until(lambda: texts() == ["first", "missed"], what="the alert missed, read after the last id")
            await until(lambda: bare.listen._streams, what="the listen again")
            alerts.append("temps", "after", "Temperatures", expires=time.time() + 3600)
            await until(lambda: texts() == ["first", "missed", "after"], what="alert 4 on the new listen")
            await asyncio.sleep(0.3)
    finally:
        for task in (delivery, supervisor, publishing):
            task.cancel()
        await asyncio.gather(delivery, supervisor, publishing, return_exceptions=True)
    assert texts() == ["first", "missed", "after"] and state.read == 4    # each once, the expired one skipped


async def test_the_service_back_on_a_fresh_alert_log_while_the_bot_runs_has_its_new_alerts_delivered(tmp_path, caplog):
    """The service stops and comes back on a new alerts.sqlite (deleted, moved, redeployed: its ids start again at 1) while the bot runs:
    delivery goes on in the new log with what came after the old one was last read (one logged while it was down included), not after
    the old last id (3, which the new ids would take weeks to pass). An event of the new log from before then is not news."""
    port = free_port()
    path = tmp_path / "service" / "alerts.sqlite"
    path.parent.mkdir()
    old = AlertLog(path, TZ)
    remote = RemoteWeather(f"http://127.0.0.1:{port}/mcp", TOKEN, TZ)
    state = AlertState(tmp_path / "bot_state.json")
    state.add_chat(1, "private")
    telegram = Telegram()
    supervisor = asyncio.create_task(remote.run())
    delivery = asyncio.create_task(AlertDelivery(remote.alerts, Notifier(telegram, state), state, TZ).run())
    texts = lambda: [text for _, text in telegram.sent]
    tasks = [delivery, supervisor]
    try:
        first = EcobotServer(Context(tz=TZ), old)
        tasks.append(asyncio.create_task(first.watch_alerts()))
        async with Serving(http_app(first, tokens=(TOKEN,)), port):
            await until(lambda: state.read is not None and first.listen._streams, what="the listen")
            for text in ("one", "two", "three"):
                old.append("rain", text, "Rain", expires=time.time() + 3600)
            await until(lambda: texts() == ["one", "two", "three"] and state.read == 3, what="the old log's alerts")
            old_identity = old.identity
        old.close()
        for leftover in path.parent.iterdir():   # (alerts.sqlite and its -wal, -shm)
            leftover.unlink()
        new = AlertLog(path, TZ)
        assert new.identity != old_identity
        new.append("uv", "old news", "UV", expires=time.time() + 3600, time=time.time() - 3600)   # (from before the bot last read)
        new.append("gusts", "while down", "Gusts", expires=time.time() + 3600)
        second = EcobotServer(Context(tz=TZ), new)
        tasks.append(asyncio.create_task(second.watch_alerts()))
        async with Serving(http_app(second, tokens=(TOKEN,)), port):
            await until(lambda: texts()[3:] == ["while down"], what="the new log's alert logged while it was down")
            await until(lambda: second.listen._streams, what="the listen again")
            new.append("temps", "after", "Temperatures", expires=time.time() + 3600)
            await until(lambda: texts()[3:] == ["while down", "after"], what="the new log's next alert")
            await asyncio.sleep(0.3)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await remote.close()
    assert texts() == ["one", "two", "three", "while down", "after"]
    assert (state.read, state.read_log) == (3, new.identity) and AlertState(state.path).read_log == new.identity
    assert "is another than the one the last alert delivered (3) is from" in caplog.text
    new.close()


async def test_with_the_service_down_the_bot_says_so_and_carries_on(alert_state, caplog):
    remote = RemoteWeather(f"http://127.0.0.1:{free_port()}/mcp", TOKEN, TZ)   # (nothing listens there)
    supervisor = asyncio.create_task(remote.run())
    try:
        model = Model([("current", {})])
        bot = Bot(NS(tz=TZ), Agent(model, "m", model_tools.tools(remote)), alert_state, service=remote)
        for text in ("how warm is it?", "weather now", "Humidity chart 7d", "Humidity chart 1d"):
            update, context, replies = a_chat()
            await (bot.press if text == "Humidity chart 1d" else bot.respond)(update, context, text)   # (the last: the Humidity button)
            assert replies == [SERVICE_DOWN], text
        assert model.seen == []                                            # (no model call for an answer it couldn't get)
        assert model_tools.tools(remote).schemas == []                     # (no tools listed while it has not been reached)
        with pytest.raises(ServiceUnreachable):
            await remote.call_text("current", {}, {"priority": "background"})   # (a call nobody waits for is refused at once)
        assert "isn't reachable" in caplog.text
    finally:
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)


async def test_main_as_a_client_runs_no_sources_and_answers_from_the_service(bare, tmp_path, monkeypatch, caplog):
    """ecobot.main(), ecobot-mcp's client at BOT_MCP_URL: no source is started (the bot has none: no keys are set), the service's
    station names the time zone, the command menu is set (with ecobot-mcp's prompts), and an alert the service logs reaches a chat."""
    import os
    import signal
    import ecobot
    from tests.fakes import config
    from tests.test_startup import FakeApp, FakeBuilder
    caplog.set_level("INFO")
    port = free_port()
    cfg = config(tmp_path, mcp_url=f"http://127.0.0.1:{port}/mcp", auth_tokens=(TOKEN,), ecowitt_api_key="",
                 ecowitt_app_key="", airgradient_token="", airgradient_location="", state_path=tmp_path / "bot" / "bot_state.json")
    (tmp_path / "bot").mkdir()
    AlertState(cfg.state_path).add_chat(5, "private")
    app = FakeApp()
    monkeypatch.setattr(ecobot._format, "width", ecobot._format.width)   # (main sets BOT_LOG_WIDTH's: put back after; the levels: conftest)
    monkeypatch.setattr(ecobot.Config, "from_env", classmethod(lambda cls: cfg))
    monkeypatch.setattr(ecobot.Application, "builder", staticmethod(lambda: FakeBuilder(app)))
    publishing = asyncio.create_task(bare.watch_alerts())

    async def alert_then_stop():
        try:
            await alert()
        finally:   # (main() runs until it is stopped, whatever happened)
            os.kill(os.getpid(), signal.SIGTERM)

    async def alert():
        await until(lambda: bare.listen._streams and cfg.state_path.exists() and "alerts_read" in cfg.state_path.read_text(),
                    what="the bot to listen and its delivery to begin")
        bare.alert_log.append("rain", "🌧️ Raining", "Rain started", expires=time.time() + 3600)
        await until(lambda: app.sent, what="the alert sent")
    try:
        async with Serving(http_app(bare, tokens=(TOKEN,)), port):
            stopper = asyncio.create_task(alert_then_stop())
            await ecobot.main()
            await stopper
    finally:
        publishing.cancel()
        await asyncio.gather(publishing, return_exceptions=True)
    assert "Ecowitt: " not in caplog.text and "Telegram: @testbot" in caplog.text
    assert f"ecobot-mcp: {cfg.mcp_url}" in caplog.text
    assert [(chat, text) for chat, text, _ in app.sent] == [(5, "🌧️ Raining")]
    assert app.menus and "chart" in app.menus[-1] and "report" in app.menus[-1]


async def test_a_start_while_the_service_cant_be_reached_follows_the_station_s_zone_once_read_and_keeps_it(tmp_path, monkeypatch, caplog):
    """ecobot.main() with BOT_MCP_URL and the service not up within STARTUP_WAIT starts in TZ's zone (UTC here, as a container's); once
    the service is reached, its station's zone (Melbourne) is the bot's, the charts', the alert delivery's and the irrigation check's,
    and it is kept in bot_state.json: the next start with the service down runs in it, not in UTC."""
    import os
    import signal
    from zoneinfo import ZoneInfo
    import ecobot
    from lib import theme
    from tests.fakes import config
    from tests.test_startup import FakeApp, FakeBuilder
    caplog.set_level("INFO")
    utc = ZoneInfo("Etc/UTC")
    port = free_port()
    cfg = config(tmp_path, tz=utc, mcp_url=f"http://127.0.0.1:{port}/mcp", auth_tokens=(TOKEN,), ecowitt_api_key="",
                 ecowitt_app_key="", airgradient_token="", airgradient_location="", state_path=tmp_path / "bot" / "bot_state.json")
    (tmp_path / "bot").mkdir()
    monkeypatch.setattr(ecobot, "STARTUP_WAIT", 0.3)
    monkeypatch.setattr(ecobot._format, "width", ecobot._format.width)
    monkeypatch.setattr(ecobot.Config, "from_env", classmethod(lambda cls: cfg))
    monkeypatch.setattr(theme, "ZONE", theme.ZONE)   # (put back after)
    made = {}

    class Kept(ecobot.Bot):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made["bot"] = self

    class Delivery(ecobot.AlertDelivery):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made["delivery"] = self
    monkeypatch.setattr(ecobot, "Bot", Kept)
    monkeypatch.setattr(ecobot, "AlertDelivery", Delivery)

    async def until_then_stop(check, what):
        try:
            await until(check, what=what)
        finally:
            os.kill(os.getpid(), signal.SIGTERM)

    alerts = AlertLog(tmp_path / "alerts.sqlite", TZ)
    server = EcobotServer(Context(tz=TZ), alerts)   # (its station: Australia/Melbourne)
    try:
        monkeypatch.setattr(ecobot.Application, "builder", staticmethod(lambda: FakeBuilder(FakeApp())))
        bot_up = lambda: "delivery" in made and made["bot"].cfg.tz is not None

        async def serve_late():
            await until(bot_up, what="the bot started without the service")
            assert str(made["bot"].cfg.tz) == "Etc/UTC" and str(made["delivery"].tz) == "Etc/UTC"   # (TZ's, while unread)
            async with Serving(http_app(server, tokens=(TOKEN,)), port):
                await until_then_stop(lambda: str(made["bot"].cfg.tz) == "Australia/Melbourne", "the station's zone")
        late = asyncio.create_task(serve_late())
        await ecobot.main()
        await late
        bot, delivery = made["bot"], made["delivery"]
        assert str(bot.cfg.tz) == str(delivery.tz) == str(bot.alert_charts.tz) == str(theme.ZONE) == "Australia/Melbourne"
        assert json.loads(cfg.state_path.read_text())["station_zone"] == "Australia/Melbourne"
        assert "isn't reachable yet" in caplog.text and "TZ's, Etc/UTC" in caplog.text

        made.clear()   # the next start, the service down: the zone last read, not UTC
        caplog.clear()
        monkeypatch.setattr(ecobot.Application, "builder", staticmethod(lambda: FakeBuilder(FakeApp())))
        stopper = asyncio.create_task(until_then_stop(bot_up, "the bot to start again"))
        await ecobot.main()
        await stopper
        assert str(made["bot"].cfg.tz) == str(made["delivery"].tz) == str(theme.ZONE) == "Australia/Melbourne"
        assert "the station's last read, Australia/Melbourne" in caplog.text
    finally:
        alerts.close()


async def test_the_irrigation_check_is_timed_again_in_a_zone_read_later(tmp_path, monkeypatch):
    """The irrigation check's wait for 18:00 is cut short when the station's zone arrives, and timed again in it."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from lib.alerts import irrigation as monitor_module
    from lib.alerts.irrigation import IrrigationMonitor
    utc = ZoneInfo("Etc/UTC")
    monitor = IrrigationMonitor(None, NS(monitor={"irrigation_monitor": {}}), None, utc, 18)
    waits = []

    def now_local(tz):
        return datetime(2026, 11, 10, 6, 0) if str(tz) == "Etc/UTC" else datetime(2026, 11, 10, 17, 0)   # (06:00 UTC: 17:00 there)
    monkeypatch.setattr(monitor_module, "now_local", now_local)
    timed = monitor.seconds_until_next
    monitor.seconds_until_next = lambda now: waits.append(timed(now)) or waits[-1]   # (the waits themselves are real)
    task = asyncio.create_task(monitor.run())
    try:
        await until(lambda: waits, what="the first wait")
        monitor.tz = TZ
        await until(lambda: len(waits) == 2, what="the wait timed again")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert waits == [12 * 3600, 3600]   # 12 hours to 18:00 UTC; then the hour to 18:00 in Melbourne


def test_the_bot_needs_ecobot_mcps_address_and_no_source_settings(monkeypatch, tmp_path):
    from lib.config import Config
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOT_TELEGRAM_TOKEN", "1:x")
    for name in ("MCP_ECOWITT_API_KEY", "MCP_ECOWITT_APP_KEY", "MCP_HOMEASSISTANT_URL", "MCP_HOMEASSISTANT_TOKEN", "MCP_AIRGRADIENT_API_TOKEN",
                 "MCP_AIRGRADIENT_LOCATION_ID", "MCP_AIRGRADIENT_LOCAL_URL", "BOT_MCP_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="BOT_MCP_URL is not set"):   # (the bot is only ever ecobot-mcp's client: step 6b)
        Config.from_env()
    monkeypatch.setenv("BOT_MCP_URL", "http://127.0.0.1:8765/mcp/")
    monkeypatch.setenv("AUTH_TOKENS", "t0ken, other")
    cfg = Config.from_env()
    assert (cfg.mcp_url, cfg.auth_tokens) == ("http://127.0.0.1:8765/mcp", ("t0ken", "other")) and not cfg.ecowitt
