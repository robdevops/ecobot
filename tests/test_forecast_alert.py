import json
from datetime import date, datetime, time as dtime, timedelta

from lib.alerts import AlertState, ForecastMonitor, Notifier
from lib.alerts.forecast import revised
from lib.forecast import Forecast
from lib.tools import Turn
from tests.fakes import TZ, config
from tests.test_forecast import make

TODAY = date(2026, 10, 3)


def day(offset, max_c=20.0, pct=10, summary="Sunny.", min_c=10.0):
    return {"date": TODAY + timedelta(days=offset), "min_c": min_c, "max_c": max_c, "rain_chance_pct": pct, "summary": summary}


class FakeForecast:
    """Just what the monitor reads: today and the current days."""

    def __init__(self, days):
        self.days = days

    def now(self):
        return datetime.combine(TODAY, dtime(12, 0))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def setup(tmp_path, days):
    state, bot = AlertState(tmp_path / "s.json"), FakeBot()
    for chat in (1, 2):
        state.add_chat(chat, f"chat {chat}")
    forecast = FakeForecast(days)
    return ForecastMonitor(forecast, state, Notifier(bot, state)), forecast, state, bot


def test_substantial_means_rain_flips_or_a_max_temperature_over_2_degrees_different():
    base = day(0, max_c=20.0, pct=95)
    assert revised(base, day(0, max_c=20.0, pct=10)) and revised(day(0, pct=10), day(0, pct=80))
    assert not revised(base, day(0, pct=60)) and not revised(base, day(0, pct=35)) and not revised(day(0, pct=20), day(0, pct=40))
    assert not revised(day(0, max_c=20.0), day(0, max_c=22.0)) and not revised(day(0, max_c=20.0), day(0, max_c=18.0))
    assert revised(day(0, max_c=20.0), day(0, max_c=22.1)) and revised(day(0, max_c=20.0), day(0, max_c=17.9))
    assert not revised(day(0, max_c=None, pct=None), day(0, max_c=25.0, pct=90))          # nothing to compare


async def test_a_day_sent_as_rainy_that_turns_dry_is_told_once_to_that_chat(tmp_path):
    mon, forecast, state, bot = setup(tmp_path, [day(0, pct=95, summary="Thunderstorm."), day(1)])
    state.record_forecast(1, [day(0, pct=95, summary="Thunderstorm."), day(1)])
    await mon.check()
    assert bot.sent == []                                            # unchanged
    forecast.days = [day(0, pct=10, summary="Sunny."), day(1)]
    await mon.check()
    await mon.check()                                                # the same revision does not repeat
    assert len(bot.sent) == 1 and bot.sent[0][0] == 1
    text = bot.sent[0][1]
    assert text.startswith("🔄 The forecast has changed since I sent it:\n• Today: was ⛈️ Thunderstorm. 10–20°C, 95% chance of rain\n  now ☀️ Sunny. 10–20°C, 10% chance of rain")
    assert state.forecast_sent(1)[TODAY]["rain_chance_pct"] == 10    # the new baseline


async def test_dry_to_rain_and_max_temperature_changes_alert_and_a_further_change_alerts_again(tmp_path):
    mon, forecast, state, bot = setup(tmp_path, [day(0), day(1, max_c=20.0)])
    state.record_forecast(1, [day(0), day(1, max_c=20.0)])
    forecast.days = [day(0, pct=80, summary="Showers."), day(1, max_c=22.1)]
    await mon.check()
    assert len(bot.sent) == 1                                        # two changed days: one message
    assert "• Today: was" in bot.sent[0][1] and "• Sunday: was" in bot.sent[0][1]
    forecast.days = [day(0, pct=80, summary="Showers."), day(1, max_c=24.3)]    # 2.2 from the new baseline
    await mon.check()
    assert len(bot.sent) == 2 and bot.sent[1][1].count("• ") == 1


async def test_small_changes_past_days_muted_chats_and_chats_never_sent_a_forecast_are_left_alone(tmp_path):
    mon, forecast, state, bot = setup(tmp_path, [day(0), day(1)])
    state.record_forecast(1, [day(0), day(1)])
    forecast.days = [day(0, max_c=21.9, pct=40), day(1, max_c=18.5)]
    await mon.check()
    assert bot.sent == []                                            # inside the thresholds
    state.record_forecast(1, [day(-1, pct=90), day(0), day(1)])
    forecast.days = [day(0, pct=70), day(1)]                         # yesterday is not in the forecast any more; today flips
    await mon.check()
    assert len(bot.sent) == 1 and "Yesterday" not in bot.sent[0][1] and bot.sent[0][1].count("• ") == 1
    state.record_forecast(2, [day(0, pct=90)])
    state.set_alerts(2, "chat 2", False)
    forecast.days = [day(0, pct=5), day(1)]
    await mon.check()
    assert [chat for chat, _ in bot.sent] == [1, 1] and state.forecast_sent(2)[TODAY]["rain_chance_pct"] == 90   # muted: not told, not reset


async def test_what_was_sent_survives_a_restart_and_is_not_repeated(tmp_path):
    mon, forecast, state, bot = setup(tmp_path, [day(0, pct=90)])
    state.record_forecast(1, [day(0, pct=90)])
    again = AlertState(tmp_path / "s.json")
    assert again.forecast_sent(1)[TODAY]["rain_chance_pct"] == 90
    forecast.days = [day(0, pct=5)]
    mon2 = ForecastMonitor(forecast, again, Notifier(bot, again))
    await mon2.check()
    assert len(bot.sent) == 1
    await ForecastMonitor(forecast, AlertState(tmp_path / "s.json"), Notifier(bot, again)).check()
    assert len(bot.sent) == 1                                        # the new baseline was saved too


async def test_the_forecast_tool_notes_the_days_it_gave_for_the_chat_and_a_failure_notes_none(tmp_path):
    forecast, _ = make(tmp_path)
    await forecast.start()
    turn = Turn()
    await forecast.handle({"days": 2}, turn)
    assert [d["date"] for d in turn.forecast_shown] == [forecast.now().date(), forecast.now().date() + timedelta(days=1)]
    assert turn.forecast_shown[0]["max_c"] == 16 and turn.forecast_shown[0]["rain_chance_pct"] == 40
    empty = Forecast(config(tmp_path, forecast=True, forecast_lat=-37.8, forecast_lon=144.9), None, None)
    empty.days, turn = [], Turn()
    assert "error" in json.loads(await empty.handle({"cached": True}, turn)) and turn.forecast_shown == []
    await forecast.close()
    await empty.close()


async def test_a_delivered_answer_records_the_forecast_for_its_chat_and_an_undelivered_one_does_not(tmp_path):
    from types import SimpleNamespace as NS
    from telegram.error import TelegramError
    from lib.bot import Bot
    forecast, _ = make(tmp_path)
    await forecast.start()
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "chat")

    class Agent:
        tools = None

        async def run(self, messages, system, effort, first_call=None, turn=None, **k):
            await forecast.handle({"days": 2}, turn)
            return "Today: showers"
    bot = Bot(NS(tz=TZ), Agent(), [], state)

    def update(fail):
        async def reply_text(body, **kw):
            if fail:
                raise TelegramError("down")
        msg = NS(chat_id=1, message_thread_id=None, is_topic_message=False, reply_text=reply_text,
                 chat=NS(type="private", title=None), from_user=NS(full_name="Rob"), reply_to_message=None)
        return NS(effective_message=msg, effective_chat=msg.chat, effective_user=NS(username="rob", full_name="Rob"))

    async def send_chat_action(*a, **k):
        pass
    ctx = NS(bot=NS(send_chat_action=send_chat_action, id=99))
    await bot.respond(update(True), ctx, "what's the forecast")
    assert state.forecast_sent(1) == {}                              # not delivered: not "sent"
    await bot.respond(update(False), ctx, "what's the forecast")
    assert sorted(state.forecast_sent(1)) == [forecast.now().date(), forecast.now().date() + timedelta(days=1)]
    await forecast.close()


async def test_to_chat_goes_to_one_chat_and_forgets_a_blocked_one(tmp_path):
    from telegram.error import Forbidden
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "a")
    state.add_chat(2, "b")
    sent = []

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            if chat_id == 2:
                raise Forbidden("bot was blocked by the user")
            sent.append((chat_id, text))
    notifier = Notifier(Bot(), state)
    assert await notifier.to_chat(1, "hello") is True and [c for c, _ in sent] == [1] and "/alerts to change" in sent[0][1]
    assert await notifier.to_chat(2, "hello") is False and 2 not in state.chats
