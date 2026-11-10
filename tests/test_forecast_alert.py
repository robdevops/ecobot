"""The forecast alert, station-wide (docs/mcp-service.md 3.6): today's and tomorrow's forecast against the day's baseline (the first one
fetched from 6 am), an alert when the high moves by more than 2°C or the chance of rain crosses 50%, the alerted forecast the new baseline."""

from datetime import date, datetime, time as dtime, timedelta

from lib.alert_log import AlertLog
from lib.alerts.forecast import ForecastMonitor, changes, revised
from lib.alerts.store import AlertStore
from tests.fakes import TZ
from tests.test_alerts import Texts

TODAY = date(2026, 10, 3)


def day(offset, max_c=20.0, pct=10, summary="☀️ Clear.", min_c=10.0):
    return {"date": TODAY + timedelta(days=offset), "min_c": min_c, "max_c": max_c, "rain_chance_pct": pct, "summary": summary}


class FakeForecast:
    """Just what the monitor reads: the time, the days, when they were fetched."""
    tz = TZ

    def __init__(self, days, hour=12, minute=0, today=TODAY):
        self.days, self.today, self.hour, self.minute = days, today, hour, minute
        self.fetched_at = self.at(hour, minute)

    def at(self, hour, minute=0) -> float:
        return datetime.combine(self.today, dtime(hour, minute), TZ).timestamp()

    def now(self):
        return datetime.combine(self.today, dtime(self.hour, self.minute))

    def fetch(self, days, hour=None, minute=0):
        """A refresh: these days, fetched now (or at `hour`)."""
        if hour is not None:
            self.hour, self.minute = hour, minute
        self.days, self.fetched_at = days, self.at(self.hour, self.minute)


def setup(tmp_path, days, **kw):
    store, log = AlertStore(tmp_path / "alert_state.json"), AlertLog(tmp_path / "alerts.sqlite", TZ)
    forecast = FakeForecast(days, **kw)
    return ForecastMonitor(forecast, store, log), forecast, Texts(log)


def test_substantial_means_rain_crossing_50_or_a_max_temperature_over_2_degrees_different():
    base = day(0, max_c=20.0, pct=95)
    assert revised(base, day(0, max_c=20.0, pct=10)) and revised(day(0, pct=10), day(0, pct=80))
    assert revised(base, day(0, pct=49)) and revised(day(0, pct=49), day(0, pct=50))                  # across 50, however little
    assert not revised(base, day(0, pct=50)) and not revised(day(0, pct=20), day(0, pct=49))          # on one side of it
    assert not revised(day(0, max_c=20.0), day(0, max_c=22.0)) and not revised(day(0, max_c=20.0), day(0, max_c=18.0))
    assert revised(day(0, max_c=20.0), day(0, max_c=22.1)) and revised(day(0, max_c=20.0), day(0, max_c=17.9))
    assert not revised(day(0, max_c=None, pct=None), day(0, max_c=25.0, pct=90))          # nothing to compare
    assert changes(day(0, max_c=24.0, pct=10), day(0, max_c=19.0, pct=80)) == ["the high is now 19°C (was 24°C)",
                                                                               "rain is now likely (80%, was 10%)"]


def test_a_revision_compares_the_one_millimetre_chance_when_both_days_have_it_and_the_older_figure_otherwise():
    day = lambda **k: {"max_c": 18, **k}
    assert revised(day(rain_1mm_pct=60), day(rain_1mm_pct=25))                       # rainy -> dry
    assert revised(day(rain_1mm_pct=20), day(rain_1mm_pct=55))                       # dry -> rainy
    assert not revised(day(rain_1mm_pct=45), day(rain_1mm_pct=35))                   # under 50 both times
    assert not revised(day(rain_chance_pct=94, rain_1mm_pct=95), day(rain_chance_pct=20, rain_1mm_pct=90))   # the new figure decides
    assert revised(day(rain_chance_pct=90), day(rain_chance_pct=10))                 # only the older figure on either side: as before
    assert not revised(day(rain_chance_pct=94), day(rain_1mm_pct=50))                # an old baseline vs a new figure: not compared


async def test_the_first_forecast_fetched_from_6am_is_the_baseline_and_alerts_nothing(tmp_path):
    mon, forecast, sent = setup(tmp_path, [day(0, pct=90), day(1)], hour=5, minute=50)
    await mon.check()                                                # before 6 am: no baseline yet
    assert mon.state.monitor.get("forecast") is None
    forecast.fetch([day(0, pct=90), day(1)], hour=6, minute=5)
    forecast.hour, forecast.fetched_at = 6, forecast.at(5, 55)       # (6 am, but the forecast held was fetched before it)
    await mon.check()
    assert mon.state.monitor.get("forecast") is None
    forecast.fetch([day(0, pct=90), day(1), day(2)], hour=6, minute=15)
    await mon.check()
    m = mon.state.monitor["forecast"]
    assert m["day"] == "2026-10-03" and sorted(m["baseline"]) == ["2026-10-03", "2026-10-04"] and sent == []   # today and tomorrow
    assert m["baseline"]["2026-10-03"]["rain_chance_pct"] == 90


async def test_a_move_of_the_high_over_2_degrees_alerts_with_what_changed_and_becomes_the_baseline(tmp_path):
    mon, forecast, sent = setup(tmp_path, [day(0), day(1, max_c=24.0)], hour=6, minute=15)
    await mon.check()                                                # the baseline
    forecast.fetch([day(0), day(1, max_c=22.5)], hour=9)
    await mon.check()
    assert sent == []                                                # 1.5 degrees: inside the threshold
    forecast.fetch([day(0), day(1, max_c=19.0, pct=80, summary="🌧️ Rain.")], hour=10)
    await mon.check()
    assert sent == ["🔄 The forecast for tomorrow has changed: the high is now 19°C (was 24°C) and rain is now likely (80%, was 10%).\n"
                    "Now: 🌧️ Rain. 10–19°C, 80% chance of rain"]
    (event,) = sent.events
    assert event["kind"] == "forecast" and event["title"] == "The forecast for tomorrow changed"
    assert event["data"] == {"date": "2026-10-04", "was": {"max_c": 24.0, "rain_chance_pct": 10, "summary": "☀️ Clear."},
                             "now": {"max_c": 19.0, "rain_chance_pct": 80, "summary": "🌧️ Rain."}}
    assert event["expires"] == "2026-10-05T00:00:00+11:00"          # news until the end of the day it is about
    assert mon.state.monitor["forecast"]["baseline"]["2026-10-04"]["max_c"] == 19.0   # the new baseline
    forecast.fetch([day(0), day(1, max_c=20.5, pct=80, summary="🌧️ Rain.")], hour=11)
    await mon.check()
    assert len(sent) == 1                                            # 1.5 from the new baseline: nothing
    forecast.fetch([day(0), day(1, max_c=21.5, pct=80, summary="🌧️ Rain.")], hour=12)
    await mon.check()
    assert len(sent) == 2 and "the high is now 22°C (was 19°C)" in sent[1]   # 2.5 from it: told


async def test_rain_crossing_50_alerts_each_way_and_today_and_tomorrow_are_separate_alerts(tmp_path):
    mon, forecast, sent = setup(tmp_path, [day(0, pct=40), day(1, pct=60), day(2)], hour=7)
    await mon.check()
    forecast.fetch([day(0, pct=55), day(1, pct=45), day(2, pct=90)], hour=8)   # both cross; the day after tomorrow is not watched
    await mon.check()
    assert [t.split(":")[0] for t in sent] == ["🔄 The forecast for today has changed", "🔄 The forecast for tomorrow has changed"]
    assert "rain is now likely (55%, was 40%)" in sent[0] and "rain is now unlikely (45%, was 60%)" in sent[1]
    forecast.fetch([day(0, pct=60), day(1, pct=40), day(2, pct=90)], hour=9)   # further, on the same side: nothing
    await mon.check()
    assert len(sent) == 2


async def test_a_new_day_starts_a_new_baseline_and_a_restart_keeps_it(tmp_path):
    mon, forecast, sent = setup(tmp_path, [day(0), day(1)], hour=7)
    await mon.check()
    again = ForecastMonitor(forecast, AlertStore(mon.state.path), mon.alerts)        # a restart: the baseline is in the store
    forecast.fetch([day(0, pct=80), day(1)], hour=8)
    await again.check()
    assert len(sent) == 1 and "today" in sent[0]
    forecast.today += timedelta(days=1)                                               # the next day: its first forecast is the baseline
    forecast.fetch([day(1, max_c=30.0), day(2)], hour=6, minute=10)
    await again.check()
    assert len(sent) == 1 and again.state.monitor["forecast"]["day"] == "2026-10-04"


async def test_the_old_per_chat_forecast_record_is_dropped_and_nothing_fires_after_the_upgrade(tmp_path):
    from lib.alerts.notify import AlertState
    bot_state = AlertState(tmp_path / "bot_state.json")
    bot_state.add_chat(1, "chat")
    bot_state.monitor["forecast_sent"] = {"1": {TODAY.isoformat(): {**day(0, pct=90), "date": TODAY.isoformat()}}}
    bot_state.save()
    store = AlertStore(tmp_path / "alert_state.json", moved_from=AlertState(bot_state.path))
    assert "forecast_sent" not in AlertState(bot_state.path).monitor and "forecast_sent" not in store.monitor
    mon = ForecastMonitor(FakeForecast([day(0, pct=5)], hour=14), store, AlertLog(tmp_path / "alerts.sqlite", TZ))
    await mon.check()                                                 # the first look after the upgrade: the baseline, no alert
    assert Texts(mon.alerts) == []
