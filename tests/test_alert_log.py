"""The alert log (lib/alert_log.py), the rules' own store (lib/alerts/store.py) and the bot's delivery from the log
(lib/alerts/notify.AlertDelivery): docs/ecobot-mcp.md 3.6 and section 4."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from lib.alert_log import KEEP_SECONDS, AlertLog
from lib.alerts.menu import alert_keyboard
from lib.alerts.notify import AlertDelivery, AlertState, Notifier
from lib.alerts.pictures import Picture
from lib.alerts.store import AlertStore
from tests.fakes import TZ

T0 = 1_790_000_000.0   # 2026-09-21, a time the tests' clock starts at


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def new_log(tmp_path, clock=None):
    return AlertLog(tmp_path / "alerts.sqlite", TZ, now=clock or Clock())


# ---------- the log ----------
def test_events_are_kept_in_order_with_rising_ids_and_their_parts(tmp_path):
    log = new_log(tmp_path)
    chart = {"title": "Wind", "drawn_by": {"tool": "summary", "arguments": {"readings": ["wind"], "period": "1d", "chart": True}}}
    first = log.append("rain", "🌧️ It's started raining.", "Rain started", {"rate_mm_h": 1.2}, expires=T0 + 3600)
    second = log.append("gusts", "💨 Strong gusts", "Strong gusts", expires=T0 + 3600, link=("live chart", "https://x"), chart=chart)
    assert (first["id"], second["id"]) == (1, 2) and log.last_id() == 2
    assert first == {"id": 1, "kind": "rain", "time": "2026-09-22T00:13:20+10:00", "expires": "2026-09-22T01:13:20+10:00",
                     "title": "Rain started", "text": "🌧️ It's started raining.", "data": {"rate_mm_h": 1.2}}
    assert second["link"] == {"label": "live chart", "url": "https://x"} and second["chart"] == chart and second["data"] == {}
    assert [e["id"] for e in log.since(0)] == [1, 2] and [e["id"] for e in log.since(1)] == [2] and log.since(2) == []
    assert log.get(2) == second and log.get(3) is None


def test_the_log_survives_a_restart_and_its_ids_never_go_back(tmp_path):
    clock = Clock()
    log = new_log(tmp_path, clock)
    for n in range(3):
        log.append("uv", f"UV {n}", "Very high UV", expires=T0 + 3600)
    log.close()
    clock.t += KEEP_SECONDS + 1                       # a month later: those are dropped when it opens again ...
    again = new_log(tmp_path, clock)
    assert again.since(0, expired=True) == [] and again.last_id() == 3
    assert again.append("uv", "UV 3", "Very high UV")["id"] == 4   # ... and the ids carry on
    third = new_log(tmp_path, clock)
    assert [e["text"] for e in third.since(0)] == ["UV 3"]


def test_since_leaves_out_what_has_expired_unless_asked(tmp_path):
    clock = Clock()
    log = new_log(tmp_path, clock)
    log.append("rain", "short", "t", expires=T0 + 60)
    log.append("pollen_and_asthma", "long", "t", expires=T0 + 86400)
    log.append("rain", "never", "t")
    clock.t += 120
    assert [e["text"] for e in log.since(0)] == ["long", "never"]
    assert [e["text"] for e in log.since(0, expired=True)] == ["short", "long", "never"]


async def test_a_reader_waits_for_the_next_event(tmp_path):
    log = new_log(tmp_path)
    assert await log.wait(0, timeout=0.05) is False
    waiter = asyncio.create_task(log.wait(0))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    log.append("rain", "x", "t")
    assert await asyncio.wait_for(waiter, 1) is True
    assert await log.wait(0, timeout=0.01) is True    # one is there already: no wait
    assert await log.wait(1, timeout=0.05) is False


# ---------- the rules' store ----------
def test_the_rules_state_moves_out_of_the_bots_file_once_so_nothing_fires_again(tmp_path):
    bot = AlertState(tmp_path / "bot_state.json")
    bot.add_chat(1, "chat")
    bot.monitor.update(rain={"alerted_state": "raining", "alerted_time": 5}, uv={"active": True, "calm_since": None},
                       particulates={"unsafe": True}, irrigation_monitor={"checked": "2026-10-01"}, forecast_sent={"1": {}})
    bot.save()
    loaded = AlertState(bot.path)
    store = AlertStore(tmp_path / "alert_state.json", moved_from=loaded)
    assert store.monitor == {"rain": {"alerted_state": "raining", "alerted_time": 5}, "uv": {"active": True, "calm_since": None},
                             "particulates": {"unsafe": True}}
    assert AlertState(bot.path).monitor == {"irrigation_monitor": {"checked": "2026-10-01"}}   # the controller's stays; the forecast record goes
    assert AlertStore(store.path).monitor == store.monitor
    again = AlertState(bot.path)
    again.monitor["rain"] = {"alerted_state": "dry"}          # (as an old bot_state.json copied back would have)
    assert AlertStore(store.path, moved_from=again).monitor["rain"]["alerted_state"] == "raining"   # moved once only


# ---------- the bot's delivery ----------
class Telegram:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, None, kw))
        return NS(photo=None)

    async def send_photo(self, chat_id, photo, **kw):
        self.sent.append((chat_id, kw["caption"], photo, kw))
        return NS(photo=(NS(file_id="small"), NS(file_id=f"copy of {chat_id}")))


def rows(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


CHART = {"title": "Wind", "drawn_by": {"tool": "summary", "arguments": {"readings": ["wind"], "period": "1d", "chart": True}}}


def delivery(tmp_path, clock=None, chats=(1, 2, 3), state=None, monkeypatch=None):
    log = new_log(tmp_path, clock)
    state = state or AlertState(tmp_path / "bot_state.json")
    for chat in chats:
        state.add_chat(chat, str(chat))
    telegram = Telegram()
    return AlertDelivery(log, Notifier(telegram, state), state, TZ), log, state, telegram


async def test_each_event_goes_to_the_chats_subscribed_to_its_kind_silently_unless_sound_is_on(tmp_path):
    d, log, state, telegram = delivery(tmp_path)
    state.set_kind(2, "rain", False)
    state.set_sound(3, "rain", True)
    log.append("rain", "🌧️ It's started raining.", "Rain started", expires=T0 + 3600)
    assert await d.deliver_new() == 1
    assert [(chat, text, kw["disable_notification"]) for chat, text, _, kw in telegram.sent] == [(1, "🌧️ It's started raining.", True),
                                                                                             (3, "🌧️ It's started raining.", False)]
    assert rows(telegram.sent[0][3]["reply_markup"]) == [[("🔔 ▸", "al:open:bell:rain")]]   # the bell; no chart, no 🔄
    assert state.read == 1 and AlertState(state.path).read == 1


async def test_an_events_link_is_its_footer(tmp_path):
    d, log, state, telegram = delivery(tmp_path, chats=(1,))
    log.append("pollen_and_asthma", "🔴 Grass pollen forecast is High.", "Grass", expires=T0 + 3600,
               link=("melbournepollen.com.au", "https://www.melbournepollen.com.au/"))
    await d.deliver_new()
    (chat, text, _, kw), = telegram.sent
    assert text == "🔴 Grass pollen forecast is High.\nmelbournepollen.com.au"
    assert [e.url for e in kw["entities"] if e.url] == ["https://www.melbournepollen.com.au/"]


async def test_an_events_chart_is_drawn_from_its_data_once_and_its_refresh_names_the_event(tmp_path, monkeypatch):
    from lib.alerts import notify
    drawn = []

    async def picture_of(data, tz):
        drawn.append(data)
        return Picture(b"png", "Wind, today", None, data["drawn_by"])
    monkeypatch.setattr(notify, "picture_of", picture_of)
    d, log, state, telegram = delivery(tmp_path)
    state.set_kind(3, "gusts", False)
    log.append("rain", "earlier", "Rain started", expires=T0 + 3600)
    event = log.append("gusts", "💨 Strong gusts: 55 km/h at 3:05pm (alerts above 40 km/h).", "Strong gusts", expires=T0 + 3600, chart=CHART)
    await d.deliver_new()
    photos = [(chat, photo) for chat, _, photo, _ in telegram.sent if photo]
    assert drawn == [CHART] and photos == [(1, b"png"), (2, "copy of 1")]     # drawn once, uploaded once
    assert rows(telegram.sent[-1][3]["reply_markup"]) == [[("🔔 ▸", "al:open:bell:gusts"), ("🔄", f"al:refresh:gusts:{event['id']}")]]


async def test_a_real_chart_in_an_event_is_drawn_as_a_picture(tmp_path, archived_cache):
    from lib.alerts import pictures
    from tests.fakes import archived_station
    eco, _ = await archived_station(tmp_path, archived_cache)
    data = await pictures.chart_data(eco, "Wind chart 1d", TZ)
    assert data["drawn_by"]["tool"] == "summary" and data["drawn_by"]["arguments"]["period"]
    (tmp_path / "d").mkdir()
    d, log, state, telegram = delivery(tmp_path / "d", chats=(1,))
    log.append("gusts", "💨 Strong gusts", "Strong gusts", chart=data)
    await d.deliver_new()
    (_, _, photo, _), = telegram.sent
    assert photo[:4] == b"\x89PNG"
    await eco.close()


async def test_a_restart_resumes_after_the_last_event_handled_and_skips_what_expired_meanwhile(tmp_path):
    clock = Clock()
    d, log, state, telegram = delivery(tmp_path, clock, chats=(1,))
    log.append("rain", "first", "t", expires=T0 + 3600)
    await d.deliver_new()
    log.append("rain", "the morning's rain", "t", expires=T0 + 3600)     # sent while the bot was down ...
    log.append("pollen_and_asthma", "today's pollen", "t", expires=T0 + 86400)
    clock.t += 2 * 3600                                                    # ... and it is back two hours later
    restarted = AlertDelivery(new_log(tmp_path, clock), Notifier(telegram, AlertState(state.path)), AlertState(state.path), TZ)
    assert restarted.last == 1
    await restarted.deliver_new()
    assert [text for _, text, _, _ in telegram.sent] == ["first", "today's pollen"]   # the rain is not news; the pollen still is
    assert restarted.last == 3
    log.append("uv", "expired at once", "t", expires=clock.t - 1)
    await restarted.deliver_new()
    assert len(telegram.sent) == 2 and restarted.last == 4                # skipped, and not looked at again


async def test_the_first_start_begins_after_the_newest_event(tmp_path):
    log = new_log(tmp_path)
    log.append("rain", "old news", "t")
    state = AlertState(tmp_path / "bot_state.json")
    state.add_chat(1, "1")
    telegram = Telegram()
    d = AlertDelivery(log, Notifier(telegram, state), state, TZ)
    assert d.last == 1
    await d.deliver_new()
    assert telegram.sent == []


async def test_a_removed_log_is_noticed_and_delivery_starts_again_from_the_new_one(tmp_path, caplog):
    d, log, state, telegram = delivery(tmp_path, chats=(1,))
    for i in range(5):
        log.append("rain", f"old {i}", "t")
    await d.deliver_new()
    assert state.read == 5
    log.close()
    (tmp_path / "alerts.sqlite").unlink()                                 # removed while the bot was down; bot_state.json kept
    log = new_log(tmp_path)
    restarted = AlertDelivery(log, Notifier(telegram, AlertState(state.path)), AlertState(state.path), TZ)
    assert restarted.last == 0 and "not the one" in caplog.text
    for i in range(3):
        log.append("rain", f"new {i}", "t")
    assert await restarted.deliver_new() == 3
    assert await log.wait(restarted.last - 1, timeout=0.1)


async def test_a_replaced_log_already_past_the_last_id_is_noticed_by_its_identity(tmp_path):
    d, log, state, telegram = delivery(tmp_path, chats=(1,))
    log.append("rain", "old", "t")
    await d.deliver_new()
    log.close()
    (tmp_path / "alerts.sqlite").unlink()
    log = new_log(tmp_path)
    for i in range(3):
        log.append("rain", f"before the restart {i}", "t")               # another file, its ids already past the old one
    restarted = AlertDelivery(log, Notifier(telegram, AlertState(state.path)), AlertState(state.path), TZ)
    assert restarted.last == 3                                             # its own newest: what came before is not news
    log.append("rain", "after", "t")
    await restarted.deliver_new()
    assert [text for _, text, _, _ in telegram.sent] == ["old", "after"]
    assert AlertState(state.path).read_log == log.identity == new_log(tmp_path).identity   # kept, and the file's own across opens


async def test_a_kept_id_from_before_the_log_had_an_identity_is_resumed_from(tmp_path):
    d, log, state, telegram = delivery(tmp_path, chats=(1,))
    log.append("rain", "first", "t")
    await d.deliver_new()
    state.read_log = None
    state.save()                                                           # as step 4 first kept it: the id alone
    log.append("rain", "second", "t")
    restarted = AlertDelivery(log, Notifier(telegram, AlertState(state.path)), AlertState(state.path), TZ)
    assert restarted.last == 1
    await restarted.deliver_new()
    assert [text for _, text, _, _ in telegram.sent] == ["first", "second"]


async def test_the_delivery_runs_on_the_logs_notification(tmp_path):
    d, log, state, telegram = delivery(tmp_path, chats=(1,))
    task = asyncio.create_task(d.run())
    try:
        await asyncio.sleep(0.01)
        log.append("uv", "UV 11", "t")
        for _ in range(100):
            if telegram.sent:
                break
            await asyncio.sleep(0.01)
        assert [text for _, text, _, _ in telegram.sent] == ["UV 11"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_log_that_cant_be_read_for_a_moment_is_tried_again_not_the_end_of_alerts(tmp_path):
    """A locked file or a disk error reading the log is logged and retried: the delivery keeps running, and the alert written meanwhile
    is sent once the log reads again (before, one error ended the task and every alert stopped until a restart)."""
    import sqlite3
    d, log, state, telegram = delivery(tmp_path, chats=(1,))
    d.RETRY_SECONDS = 0.01
    real, failures = log.since, [sqlite3.OperationalError("database is locked")]

    def flaky(*a, **k):
        if failures:
            raise failures.pop()
        return real(*a, **k)
    log.since = flaky
    log.append("uv", "UV 11", "t")
    task = asyncio.create_task(d.run())
    try:
        for _ in range(200):
            if telegram.sent:
                break
            await asyncio.sleep(0.01)
        assert [text for _, text, _, _ in telegram.sent] == ["UV 11"] and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ---------- the 🔄 under a delivered alert ----------
class ChartQuery:
    def __init__(self, data, kind="gusts", event=None):
        self.data = data
        self.message = NS(text=None, chat=NS(type="private", id=1), photo=(NS(file_id="large"),), caption="💨 Strong gusts", caption_entities=(),
                          reply_markup=alert_keyboard(kind, set(), set(), refresh=True, event=event), date=None, edit_date=None)
        self.from_user, self.toast, self.media, self.edits = NS(id=7), "unset", [], []

    async def answer(self, text=None, **kw):
        self.toast = text

    async def edit_message_media(self, media, reply_markup=None):
        self.media.append((media, reply_markup))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edits.append(reply_markup)


def a_bot(tmp_path, log, monkeypatch):
    from lib.bot import Bot
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "private")
    bot = Bot(NS(tz=TZ), None, [NS(name="Ecowitt", tz=TZ)], state, alert_log=log)
    calls = []

    async def draw_call(tool, args, ahead=True):
        calls.append((tool, args, ahead))
        return b"replayed png", "Wind, today", None, 1_800_000_000, {"tool": tool, "arguments": args}, {}
    monkeypatch.setattr(bot, "_draw_call", draw_call)
    return bot, calls


async def test_refresh_under_a_delivered_alert_replays_its_charts_drawn_by(tmp_path, monkeypatch):
    log = new_log(tmp_path)
    drawn_by = {"tool": "summary", "arguments": {"readings": ["wind"], "period": "1d", "chart": True, "points": "auto"}}
    event = log.append("gusts", "💨 Strong gusts", "Strong gusts", chart={"title": "Wind", "drawn_by": drawn_by})
    bot, calls = a_bot(tmp_path, log, monkeypatch)
    q = ChartQuery(f"al:refresh:gusts:{event['id']}", event=event["id"])
    await bot.on_alert_button(NS(callback_query=q), NS())
    assert calls == [("summary", drawn_by["arguments"], False)]
    (media, markup), = q.media
    assert media.media.input_file_content == b"replayed png" and markup is q.message.reply_markup and q.toast is None


async def test_refresh_of_an_alert_not_in_the_log_draws_its_types_chart(tmp_path, monkeypatch):
    from lib.alerts import pictures
    asked = []

    async def chart(source, text, tz, service=None):
        asked.append(text)
        return Picture(b"type png", "Wind, today", None)
    monkeypatch.setattr(pictures, "chart", chart)
    bot, calls = a_bot(tmp_path, new_log(tmp_path), monkeypatch)
    q = ChartQuery("al:refresh:gusts:99", event=99)                     # (dropped from the log)
    await bot.on_alert_button(NS(callback_query=q), NS())
    assert calls == [] and asked == ["Wind chart 1d"] and q.media[0][0].media.input_file_content == b"type png"


async def test_the_bell_under_a_delivered_alert_keeps_the_event_in_its_refresh(tmp_path, monkeypatch):
    bot, _ = a_bot(tmp_path, new_log(tmp_path), monkeypatch)
    q = ChartQuery("al:open:bell:gusts", event=7)
    await bot.on_alert_button(NS(callback_query=q), NS())
    assert rows(q.edits[0])[0] == [("🔔 ▾", "al:close:gusts"), ("🔄", "al:refresh:gusts:7")]


@pytest.mark.parametrize("data", ["al:refresh:gusts:x", "al:refresh:gusts:1:2"])
async def test_a_refresh_with_a_malformed_id_does_nothing(tmp_path, monkeypatch, data):
    bot, calls = a_bot(tmp_path, new_log(tmp_path), monkeypatch)
    q = ChartQuery(data)
    await bot.on_alert_button(NS(callback_query=q), NS())
    assert calls == [] and q.media == [] and q.toast is None
