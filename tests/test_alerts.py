import time
from datetime import datetime, timedelta, timezone

import pytest

from lib.airgradient import Latest
from lib.alerts import ParticulatesMonitor, AlertState, Notifier, WeatherMonitor, with_footer
from tests.fakes import TZ

T0 = int(datetime(2026, 9, 29, 13, 0, tzinfo=TZ).timestamp())  # 1pm Melbourne, a settled day


def rows(n, step=300, start=T0, **fields):
    """n readings; each field is a value or f(i)."""
    out = []
    for i in range(n):
        out.append((start + i * step, {k: (v(i) if callable(v) else v) for k, v in fields.items()}))
    return out


class FakeStation:
    tz, longitude = TZ, 145.0

    def __init__(self, data=None):
        self.data = data or []
        self.live = None

    async def live_rain(self):
        if isinstance(self.live, Exception):
            return None
        return self.live

    async def recent(self, hours):
        """The readings of the last `hours` (up to the newest one, which stands for now)."""
        return [(ts, r) for ts, r in self.data if ts >= self.data[-1][0] - hours * 3600] if self.data else []

    def now(self):
        return datetime.now(TZ).replace(tzinfo=None)

    async def readings(self, *a, **k):
        return self.data


def monitor(tmp_path, data):
    state, sent = AlertState(tmp_path / "s.json"), []

    async def notify(text, link=None, **kw):
        sent.append(text)
    m = WeatherMonitor(FakeStation(data), state, notify)
    return m, state, sent


def rain(*wet):
    """One reading per flag: rain rate 1.2 mm/h when wet."""
    return [(T0 + i * 300, {"rainfall.rain_rate": 1.2 if w else 0.0, "rainfall.daily": 0.5 + 0.2 * sum(wet[:i + 1])})
            for i, w in enumerate(wet)]


async def test_rain_starts_once_then_stops_after_30_dry_minutes(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    for data in (rain(0, 0, 0), rain(0, 0, 1), rain(0, 0, 1, 1), rain(0, 0, 1, 1, 0), rain(0, 0, 1, 1, 0, 0)):
        m.station.data = data
        await m.check()
    assert len(sent) == 1 and "started raining" in sent[0]
    m.station.data = rain(0, 0, 1, 1, *[0] * 6)  # 30 dry minutes since the last wet reading
    await m.check()
    assert len(sent) == 2 and "stopped" in sent[1] and "mm fell" in sent[1]


@pytest.mark.parametrize("wet, told", [(60, "12.0 mm fell over 5h"),
                                       (31, "6.2 mm fell over 2h 35m")])   # its first tip is the oldest of the 3 hours looked at
async def test_the_rain_stopped_alert_counts_a_spell_longer_than_the_readings_it_looks_at(tmp_path, wet, told):
    m, state, sent = monitor(tmp_path, None)
    spell = rain(0, 0, *[1] * wet, *[0] * 6)                # 0.2 mm a wet reading, then 30 dry minutes
    for n in (3, len(spell)):
        m.station.data = spell[:n]
        await m.check()
    assert len(sent) == 2 and sent[1] == f"\U0001f324️ The rain has stopped. {told}."


async def test_a_dry_gap_shorter_than_the_stop_time_does_not_flap(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    for data in (rain(1, 1), rain(1, 1, 0, 0), rain(1, 1, 0, 0, 0), rain(1, 1, 0, 0, 0, 1), rain(1, 1, 0, 0, 0, 1, 0)):
        m.station.data = data
        await m.check()
    assert len(sent) == 1 and "started" in sent[0]


async def test_a_live_reading_starts_the_rain_alert_before_the_history_catches_up(tmp_path):
    m, state, sent = monitor(tmp_path, rain(0, 0, 0))
    await m.check()
    m.station.live = (T0 + 3 * 300, {"rainfall.rain_rate": 0.0, "rainfall.daily": 0.5})   # dry live reading
    await m.check()
    assert sent == []
    m.station.live = (T0 + 3 * 300, {"rainfall.rain_rate": 0.0, "rainfall.daily": 0.7})   # one tip: the total rose
    await m.check()
    assert len(sent) == 1 and "started raining" in sent[0]
    m.station.live = (T0 + 3 * 300, {"rainfall.rain_rate": 2.4, "rainfall.daily": 0.9})
    await m.check()
    assert len(sent) == 1


async def test_a_live_rate_alone_starts_it_and_a_failed_live_call_is_ignored(tmp_path):
    m, state, sent = monitor(tmp_path, rain(0, 0, 0))
    m.station.live = RuntimeError("down")
    await m.check()
    assert sent == []
    m.station.live = (T0 + 4 * 300, {"rainfall.rain_rate": 3.0, "rainfall.daily": 0.5})
    await m.check()
    assert len(sent) == 1 and "3 mm/h" in sent[0]


def gusts(*values):
    return [(T0 + i * 300, {"wind.wind_gust": v}) for i, v in enumerate(values)]


async def test_a_gust_over_40_alerts_once_and_again_only_after_an_hour_of_calm(tmp_path):
    vals = [10, 12]
    m, state, sent = monitor(tmp_path, gusts(*vals))

    async def step(*more):
        vals.extend(more)
        m.station.data = gusts(*vals)
        await m.check()
    await m.check()                                             # the first look sets the mark: nothing to announce
    await step(40)                                              # 40 is not over 40
    assert sent == []
    await step(41.6)
    assert len(sent) == 1 and "42 km/h" in sent[0] and "1:15pm" in sent[0]
    await step(55, 38)                                          # gusty spell continues: still the same alert
    await step(*[20] * 11)                                      # under 55 minutes calm...
    await step(45)                                              # ...then a gust: no second alert
    assert len(sent) == 1
    await step(*[20] * 12)                                      # a calm hour starts counting
    await step(*[20] * 12)                                      # ...an hour of it re-arms the alert
    assert len(sent) == 1
    await step(48)
    assert len(sent) == 2 and "48 km/h" in sent[1]


async def test_old_gusts_are_not_announced_after_a_restart(tmp_path):
    m, state, sent = monitor(tmp_path, gusts(60, 20, 20))
    await m.check()
    assert sent == []


def temps(outdoor, indoor, ts=T0):
    return [(ts, {"outdoor.temperature": outdoor, "indoor.temperature": indoor})]


async def test_a_crossing_after_a_long_quiet_spell_is_announced_at_once(tmp_path):
    m, state, sent = monitor(tmp_path, temps(10, 20))
    await m.check()                                    # learns: outdoor cooler
    m.station.data = temps(25, 20, T0 + 3 * 3600)
    await m.check()
    assert sent == ["\U0001f321️ It's now 5.0°C warmer outside than inside (25.0°C vs 20.0°C). It had been cooler for 3h."]


async def test_crossing_back_inside_the_cooldown_is_announced_when_it_ends(tmp_path):
    m, state, sent = monitor(tmp_path, temps(10, 20))
    await m.check()
    start = T0 + 3 * 3600
    m.station.data = temps(25, 20, start)                       # warmer: alerted
    await m.check()
    for minutes in (10, 29):                                    # back to cooler inside the cooldown: held
        m.station.data = temps(10, 20, start + minutes * 60)
        await m.check()
    assert len(sent) == 1
    m.station.data = temps(10, 20, start + 30 * 60)             # the cooldown ends and it is still cooler
    await m.check()
    assert len(sent) == 2 and sent[1] == "\u2744\ufe0f It's now 10.0°C cooler outside than inside (10.0°C vs 20.0°C). It had been warmer for 30 min."
    m.station.data = temps(10, 20, start + 40 * 60)             # and once only
    await m.check()
    assert len(sent) == 2


async def test_crossings_that_end_on_the_alerted_side_inside_the_cooldown_send_nothing(tmp_path):
    m, state, sent = monitor(tmp_path, temps(10, 20))
    await m.check()
    start = T0 + 3 * 3600
    m.station.data = temps(25, 20, start)
    await m.check()
    for minutes, outdoor in ((10, 10), (20, 25), (30, 25), (60, 25)):   # cooler, warmer again, and on
        m.station.data = temps(outdoor, 20, start + minutes * 60)
        await m.check()
    assert len(sent) == 1


async def test_a_crossing_after_days_says_days(tmp_path):
    m, state, sent = monitor(tmp_path, temps(10, 20))
    await m.check()
    m.station.data = temps(25, 20, T0 + 3 * 86400)
    await m.check()
    assert len(sent) == 1 and "cooler for 3 days" in sent[0]


async def test_a_crossing_soon_after_the_state_was_learned_waits_out_the_cooldown(tmp_path):
    m, state, sent = monitor(tmp_path, temps(10, 20))
    await m.check()
    m.station.data = temps(25, 20, T0 + 29 * 60)
    await m.check()
    assert not sent
    m.station.data = temps(25, 20, T0 + 30 * 60)
    await m.check()
    assert len(sent) == 1 and "cooler for 30 min" in sent[0]


async def test_crossing_state_saved_by_an_earlier_version_still_works(tmp_path):
    m, state, sent = monitor(tmp_path, temps(25, 20, T0 + 2 * 3600))
    state.monitor["cross"] = {"side": "cooler", "since": T0}
    await m.check()
    assert len(sent) == 1 and "cooler for 2h" in sent[0] and state.monitor["cross"]["alerted_state"] == "warmer"


async def test_small_temperature_differences_are_noise(tmp_path):
    m, state, sent = monitor(tmp_path, temps(20.1, 20.0))
    await m.check()
    assert "cross" not in state.monitor and not sent


# ---------- air ----------
def air_reading(pm25, pm10=10.0, age=timedelta(minutes=5)):
    """What air.latest() gives: the reading, when it was taken, and (none here: the reading's own) the lowest of the last readings."""
    return Latest({"pm2_5": {"value": pm25, "aqi_us": 160}, "pm10": {"value": pm10}}, datetime.now(timezone.utc) - age, {})


class FakeAir:
    tz, link = TZ, ("live chart", "https://example.com")


async def test_mask_alert_needs_two_bad_checks_and_pairs_with_all_clear(tmp_path):
    state, sent = AlertState(tmp_path / "s.json"), []

    async def notify(text, link=None, **kw):
        sent.append((text, link))
    mon = ParticulatesMonitor(FakeAir(), state, notify)
    await mon.check(air_reading(80))
    assert not sent                                   # one puff of smoke isn't enough
    await mon.check(air_reading(20))
    await mon.check(air_reading(80))
    assert not sent                                   # the streak was broken
    await mon.check(air_reading(90))
    assert len(sent) == 1 and "mask" in sent[0][0] and sent[0][1] == FakeAir.link
    await mon.check(air_reading(90))
    assert len(sent) == 1                             # no repeats within an episode
    await mon.check(air_reading(30))
    assert len(sent) == 1
    await mon.check(air_reading(30))
    assert len(sent) == 2 and "safe again" in sent[1][0]


async def test_mask_threshold_is_official_aqi_151(tmp_path):
    state, sent = AlertState(tmp_path / "s.json"), []

    async def notify(text, link=None, **kw):
        sent.append(text)
    mon = ParticulatesMonitor(FakeAir(), state, notify)
    for _ in range(3):
        await mon.check(air_reading(55.4))            # AQI 150: unhealthy for sensitive groups only
    assert not sent
    for _ in range(2):
        await mon.check(air_reading(55.5))
    assert len(sent) == 1


async def test_stale_air_readings_are_ignored(tmp_path):
    state, sent = AlertState(tmp_path / "s.json"), []

    async def notify(text, link=None, **kw):
        sent.append(text)
    mon = ParticulatesMonitor(FakeAir(), state, notify)
    for _ in range(3):
        await mon.check(air_reading(200, age=timedelta(hours=3)))
    assert not sent


# ---------- chats and delivery ----------
def test_alert_state_survives_restarts_and_opt_out(tmp_path):
    path = tmp_path / "s.json"
    state = AlertState(path)
    state.add_chat(1, "Home")
    state.add_chat(2, "Work")
    state.set_alerts(2, "Work", False)
    state.monitor["rain"] = {"alerted_state": "raining", "alerted_time": 5, "since": 5}
    state.save()
    again = AlertState(path)
    assert again.alert_chats() == [1] and again.monitor["rain"]["alerted_state"] == "raining" and not again.alerts_on(2)


def test_footer_marks_up_link_and_mute_hint():
    text, entities = with_footer("\U0001f327️ Rain", ("live chart", "https://x"), "/alerts off to mute")
    assert text.endswith("live chart · /alerts off to mute")
    italic, link = entities
    assert italic.offset == link.offset == len("\U0001f327️ Rain".encode("utf-16-le")) // 2 + 1
    assert link.length == len("live chart") and link.url == "https://x"


async def test_alerts_are_silent_and_dead_chats_are_forgotten(tmp_path):
    from telegram.error import Forbidden
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "ok")
    state.add_chat(2, "blocked")
    calls = []

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            calls.append((chat_id, kw))
            if chat_id == 2:
                raise Forbidden("bot was blocked")
    await Notifier(Bot(), state)("hi")
    assert all(kw["disable_notification"] for _, kw in calls) and 2 not in state.chats and 1 in state.chats


def test_a_damaged_state_file_is_kept_aside_not_overwritten(tmp_path, caplog):
    path = tmp_path / "s.json"
    path.write_text('{"chats": {"1": {"title": "Home"}}, "monitor": {"rain": {"alerted_st')   # cut short
    state = AlertState(path)
    assert state.chats == {}
    state.add_chat(2, "Work")                                      # the next save
    aside = list(tmp_path.glob("s.json.damaged-*"))
    assert len(aside) == 1 and aside[0].read_text().startswith('{"chats": {"1"') and "moved it to" in caplog.text
    assert AlertState(path).chats == {2: {"title": "Work"}}


def test_a_save_is_on_disk_before_it_replaces_the_old_file(tmp_path, monkeypatch):
    from lib.alerts import notify
    calls, replace = [], notify.os.replace
    monkeypatch.setattr(notify.os, "fsync", lambda fd: calls.append("fsync"))
    monkeypatch.setattr(notify.os, "replace", lambda a, b: (calls.append("replace"), replace(a, b)))
    AlertState(tmp_path / "s.json").add_chat(1, "Home")
    assert calls == ["fsync", "replace"]


async def test_one_chat_failing_never_stops_the_alert_reaching_the_others(tmp_path, monkeypatch):
    from telegram.error import Forbidden
    state = AlertState(tmp_path / "s.json")
    for chat in (1, 2, 3):
        state.add_chat(chat, f"chat {chat}")
    got = []

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            if chat_id == 1:
                raise Forbidden("bot was blocked")
            got.append(chat_id)

    def full():
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(state, "save", full)                       # forgetting chat 1 can't be saved
    await Notifier(Bot(), state)("hi")
    assert got == [2, 3]


@pytest.mark.filterwarnings("ignore:Deprecated since version v22.2")   # python-telegram-bot 22: retry_after will be a timedelta
async def test_a_short_flood_wait_or_a_dropped_connection_is_tried_once_more_and_a_timeout_is_not(tmp_path, monkeypatch):
    from telegram.error import NetworkError, RetryAfter, TimedOut
    from lib.alerts import notify
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
    monkeypatch.setattr(notify.asyncio, "sleep", sleep)
    flood, long_flood = RetryAfter(5), RetryAfter(300)
    state = AlertState(tmp_path / "s.json")
    state.add_chat(1, "chat")

    class Bot:
        def __init__(self, *errors):
            self.errors, self.tries = list(errors), 0

        async def send_message(self, chat_id, text, **kw):
            self.tries += 1
            if self.errors:
                raise self.errors.pop(0)
    cases = [((flood,), True, 2, [5]), ((NetworkError("Connection reset by peer"),), True, 2, [3]),
             ((NetworkError("down"), NetworkError("down")), False, 2, [3]),    # once more, not twice
             ((TimedOut(),), False, 1, []),                                   # it may have arrived
             ((long_flood,), False, 1, [])]                                   # not waited out
    for errors, sent, tries, waited in cases:
        waits.clear()
        bot = Bot(*errors)
        assert (await Notifier(bot, state).to_chat(1, "hi"), bot.tries, waits) == (sent, tries, waited), errors


def uv(*values, start=T0):
    return [(start + i * 300, {"solar_and_uvi.uvi": v}) for i, v in enumerate(values)]


async def test_a_uv_index_of_10_alerts_once_and_again_only_after_an_hour_below_it(tmp_path):
    vals = [3, 5]
    m, state, sent = monitor(tmp_path, uv(*vals))

    async def step(*more):
        vals.extend(more)
        m.station.data = uv(*vals)
        await m.check()
    await m.check()                                    # the first look sets the mark
    await step(9.9)
    assert sent == []
    await step(10)
    assert sent == []                                  # one reading at 10 is not enough
    await step(11)
    assert len(sent) == 1 and "UV index 11" in sent[0] and "1:20pm" in sent[0] and "alerts at 10 and above" in sent[0]   # the run's highest
    await step(7, *[6] * 11)                           # still the same spell, then under an hour below 10
    await step(10.5, 10.5)
    assert len(sent) == 1
    await step(*[5] * 12)                              # a calm hour starts counting
    await step(*[5] * 12)                              # an hour below 10 re-arms it
    await step(10)
    assert len(sent) == 1                              # one reading again
    await step(10)
    assert len(sent) == 2


async def test_old_uv_readings_are_not_announced_after_a_restart(tmp_path):
    m, state, sent = monitor(tmp_path, uv(11, 12, 4))
    await m.check()
    assert sent == []


# ---------- pollen and thunderstorm asthma ----------
class FakePollen:
    def __init__(self):
        self.grass = self.asthma = None
        self.season = True
        self.fetched_at = time.time()
        self.today = datetime(2026, 10, 1, 12, 0)

    def now(self):
        return self.today

    def in_season(self):
        return self.season

    def current(self):
        from lib.pollen import LEVEL_EMOJI
        make = lambda level, **kw: {"level": level, "emoji": LEVEL_EMOJI[level], **kw} if level else None
        return {"grass": make(self.grass, date=self.today.date()), "asthma": make(self.asthma, updated=None),
                "fetched_at": self.fetched_at}


def pollen_monitor(tmp_path):
    from lib.alerts import PollenAndAsthmaMonitor
    state, sent = AlertState(tmp_path / "s.json"), []

    async def notify(text, link=None, **kw):
        sent.append(text)
    pollen = FakePollen()
    return PollenAndAsthmaMonitor(pollen, state, notify), pollen, sent


async def test_high_pollen_and_asthma_alert_once_per_metric_and_day(tmp_path):
    mon, pollen, sent = pollen_monitor(tmp_path)
    for grass, asthma in (("Low", "Low"), ("Moderate", "Moderate")):   # 🟢 and 🟠 are not warnings
        pollen.grass, pollen.asthma = grass, asthma
        await mon.check()
    assert sent == []
    pollen.grass = "High"
    await mon.check()
    await mon.check()                                                  # unchanged: nothing more
    assert sent == ["🔴 Grass pollen is High."]
    pollen.asthma = "High"
    await mon.check()
    assert sent[1:] == ["🔴 Thunderstorm asthma risk is High. Check your asthma action plan."]
    pollen.grass, pollen.asthma = "Low", "Moderate"                    # dropped below High: ready to warn again
    await mon.check()
    pollen.grass = "High"
    await mon.check()
    assert len(sent) == 3 and sent[-1] == "🔴 Grass pollen is High."
    pollen.today += timedelta(days=1)                                  # a new day, still High
    pollen.fetched_at = time.time()
    await mon.check()
    assert len(sent) == 4 and "Central" not in " ".join(sent)


async def test_a_restart_does_not_repeat_an_alert_and_a_missing_forecast_or_stale_page_is_ignored(tmp_path):
    from lib.alerts import PollenAndAsthmaMonitor
    mon, pollen, sent = pollen_monitor(tmp_path)
    pollen.grass = "High"
    await mon.check()
    assert len(sent) == 1
    again = PollenAndAsthmaMonitor(pollen, mon.state, mon.notify)               # the state file keeps what was sent
    await again.check()
    assert len(sent) == 1
    pollen.asthma = None                                               # off-season: nothing to say
    pollen.grass, pollen.fetched_at = "High", time.time() - 3 * 3600  # and a page not fetched for 3 hours is not news
    await again.check()
    assert len(sent) == 1


async def test_no_pollen_alert_out_of_season_even_with_a_high_reading_cached(tmp_path):
    mon, pollen, sent = pollen_monitor(tmp_path)
    pollen.grass, pollen.asthma, pollen.season = "High", "High", False
    await mon.check()
    assert sent == []
    pollen.season = True
    await mon.check()
    assert len(sent) == 2


async def test_the_cooldown_sets_the_dry_time_before_the_rain_stops(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    m.cooldown = 10 * 60
    for data in (rain(0, 0, 0), rain(0, 0, 1), rain(0, 0, 1, 1)):
        m.station.data = data
        await m.check()
    m.station.data = rain(0, 0, 1, 1, 0, 0)               # 10 dry minutes: a shorter cooldown
    await m.check()
    assert len(sent) == 2 and "stopped" in sent[1]


def rain_at(flags, n):
    return rain(*flags[:n])


async def test_rain_that_starts_again_inside_the_cooldown_is_announced_when_it_ends_if_it_is_still_raining(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    flags = [0, 0, 1, 1] + [0] * 6 + [1] * 8                # starts, stops at reading 10 (30 dry minutes), starts again at 10, rains on
    for n in (3, 4, 10):                                    # (reading numbers are counts, so 10 readings end at index 9)
        m.station.data = rain_at(flags, n)
        await m.check()
    assert len(sent) == 2 and "started" in sent[0] and "stopped" in sent[1]
    m.station.data = rain_at(flags, 11)                     # raining again 5 minutes after the "stopped": held
    await m.check()
    assert len(sent) == 2
    m.station.data = rain_at(flags, 15)                     # 25 minutes after
    await m.check()
    assert len(sent) == 2
    m.station.data = rain_at(flags, 16)                     # 30 minutes after the "stopped", still raining: now announced
    await m.check()
    assert len(sent) == 3 and "started raining" in sent[2]


async def test_rain_that_comes_and_goes_inside_the_cooldown_and_ends_as_alerted_sends_nothing(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    flags = [0, 0, 1, 1] + [0] * 6 + [1] + [0] * 8          # stopped at reading 10, one wet reading, then dry again
    for n in (3, 4, 10, 11, 14, 19):
        m.station.data = rain_at(flags, n)
        await m.check()
    assert len(sent) == 2 and "stopped" in sent[1]          # no "started" for the shower, no second "stopped"


def night_rain(start, *wet):
    """One reading per flag from `start` (epoch): rain rate 1.2 mm/h when wet; the day's total rises 0.4 mm a wet reading."""
    total, out = 0.0, []
    for i, w in enumerate(wet):
        total += 0.4 if w else 0.0
        out.append((start + i * 300, {"rainfall.rain_rate": 1.2 if w else 0.0, "rainfall.daily": total}))
    return out


async def test_no_rain_alerts_in_quiet_hours_and_one_summary_after_them(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    m.quiet = (0, 6)
    one_am = int(datetime(2026, 9, 30, 1, 0, tzinfo=TZ).timestamp())
    night = night_rain(one_am, 0, 0, 1, 1, 1, *[0] * 57)           # 1am to 6:05am
    for n in (3, 4, 6, 18, 60):                                    # it starts, rains, stops after 60 dry minutes; 5:55
        m.station.data = night[:n]
        await m.check()
    assert sent == []                                              # all in quiet hours
    m.station.data = night                                         # 6:05
    await m.check()
    assert len(sent) == 1 and sent[0].startswith("\U0001f327️ Overnight rain: 1.2 mm, from about 1:10am to 1:25am.")
    await m.check()
    assert len(sent) == 1                                          # once
    m.station.data = night_rain(int(datetime(2026, 9, 30, 13, 0, tzinfo=TZ).timestamp()), 0, 1)
    await m.check()
    assert len(sent) == 2 and "started raining" in sent[1]         # by day, rain alerts are back


async def test_quiet_hours_across_midnight_keep_the_evening_rain_for_the_summary(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    m.quiet = (22, 6)
    evening = night_rain(int(datetime(2026, 9, 29, 22, 0, tzinfo=TZ).timestamp()), *[0] * 12, *[1] * 6, *[0] * 6)   # 11pm, 2.4 mm
    morning = night_rain(int(datetime(2026, 9, 30, 0, 0, tzinfo=TZ).timestamp()), *[0] * 12, *[1] * 3, *[0] * 59)  # the gauge
    night = evening + morning                                      # reset at midnight; 1am, 1.2 mm; dry to 6:05am
    for n in range(1, len(night) + 1):                             # a check every 5 minutes
        m.station.data = night[:n]
        await m.check()
    assert sent == ["\U0001f327️ Overnight rain: 3.6 mm, from about 11:00pm to 1:15am."]


async def test_a_note_left_from_a_night_the_bot_missed_gives_way_to_the_next_nights(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    m.quiet = (22, 6)
    m.station.data = night_rain(int(datetime(2026, 9, 29, 23, 0, tzinfo=TZ).timestamp()), 0, 1)
    await m.check()                                                # rain noted at 11:05pm, then the bot is down until the next night
    night = night_rain(int(datetime(2026, 9, 30, 22, 30, tzinfo=TZ).timestamp()), 0, 1, *[0] * 89)   # to 6am
    m.station.data = night[:2]
    await m.check()
    m.station.data = night
    await m.check()
    assert sent == ["\U0001f327️ Overnight rain: 0.4 mm, from about 10:35pm to 10:40pm."]


async def test_a_dry_night_has_no_summary_and_quiet_hours_can_be_off(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    m.quiet = (0, 6)
    m.station.data = night_rain(int(datetime(2026, 9, 30, 2, 0, tzinfo=TZ).timestamp()), 0, 0, 0)
    await m.check()
    m.station.data = night_rain(int(datetime(2026, 9, 30, 6, 5, tzinfo=TZ).timestamp()), 0, 0, 0)
    await m.check()
    assert sent == []
    m.quiet = None
    m.station.data = night_rain(int(datetime(2026, 9, 30, 2, 0, tzinfo=TZ).timestamp()), 0, 0, 1)
    await m.check()
    assert len(sent) == 1 and "started raining" in sent[0]


def test_unsubscribing_from_all_keeps_future_alert_types_off_until_subscribed(tmp_path, monkeypatch):
    from lib.alerts import menu, notify
    state = notify.AlertState(tmp_path / "s.json")
    for chat in (1, 2, 3):
        state.add_chat(chat, str(chat))
    state.set_kind(1, "all", False)                       # unsubscribe from all
    state.set_kind(2, "rain", False)                      # just one type: the normal mode
    monkeypatch.setitem(menu.LABELS, "newthing", "new thing")
    assert not state.subscribed(1, "newthing") and state.subscribed(2, "newthing") and state.subscribed(3, "newthing")
    state.set_kind(1, "uv", True)                         # then one type back on: only that one
    monkeypatch.setitem(menu.LABELS, "newer", "newer")
    assert state.subscribed(1, "uv") and not state.subscribed(1, "rain") and not state.subscribed(1, "newer") and state.alert_chats("uv") == [1, 2, 3]
    state.set_kind(1, "uv", False)
    assert not state.subscribed(1)                        # nothing on at all
    state.set_kind(1, "all", True)                        # subscribe to all: new types come on again
    assert state.subscribed(1, "newer") and state.subscribed(1, "rain")
    for kind in list(menu.LABELS):                        # turning every type off one by one is not "unsubscribe from all"
        state.set_kind(3, kind, False)
    monkeypatch.setitem(menu.LABELS, "newest", "newest")
    assert state.subscribed(3, "newest")


def test_chats_that_already_unsubscribed_from_all_load_as_opt_in(tmp_path, monkeypatch):
    import json
    from lib.alerts import menu, notify
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"chats": {"1": {"title": "a", "muted": list(menu.LABELS)}, "2": {"title": "b", "alerts": False},
                                          "3": {"title": "c", "muted": ["rain"]}}}))
    state = notify.AlertState(path)
    monkeypatch.setitem(menu.LABELS, "newthing", "new thing")
    assert not state.subscribed(1, "newthing") and not state.subscribed(2, "newthing") and state.subscribed(3, "newthing")
    state.set_kind(1, "uv", True)
    state.save()
    again = notify.AlertState(path)                       # and it survives a restart
    assert again.subscribed(1, "uv") and not again.subscribed(1, "rain") and not again.subscribed(1, "newthing")


def asking(mon, photo=b"png"):
    """Replace a monitor's chart maker with one that records what was asked and answers `photo`; returns the list of requests."""
    asked = []

    async def picture(*text):
        asked.append(text[-1] if text else "particulates")
        return photo
    mon._picture = picture
    return asked


async def test_the_crossing_alert_carries_the_temperature_chart_when_there_is_one(tmp_path):
    m, state, sent = monitor(tmp_path, temps(10, 20))
    photos = []

    async def notify(text, link=None, **kw):
        sent.append(text)
        photos.append(kw.get("photo"))
    m.notify = notify
    asked = asking(m)
    await m.check()                                    # learns: outdoor cooler
    m.station.data = temps(25, 20, T0 + 3 * 3600)
    await m.check()
    assert len(sent) == 1 and photos == [b"png"] and asked == ["Temperature chart 1d"]
    m.station.data = temps(10, 20, T0 + 6 * 3600)      # and back, with no chart this time: the alert still goes out, as text
    asking(m, photo=None)
    await m.check()
    assert len(sent) == 2 and photos[1] is None


async def test_the_gusts_alert_carries_the_wind_chart(tmp_path):
    vals = [10, 12]
    m, state, sent = monitor(tmp_path, [(T0 + i * 300, {"wind.wind_gust": v}) for i, v in enumerate(vals)])
    seen = []

    async def notify(text, link=None, **kw):
        seen.append((text, kw.get("photo")))
    m.notify = notify
    asked = asking(m)
    await m.check()                                    # the first look sets the mark
    m.station.data = [(T0 + i * 300, {"wind.wind_gust": v}) for i, v in enumerate([10, 12, 55])]
    await m.check()
    assert len(seen) == 1 and "Strong gusts" in seen[0][0] and seen[0][1] == b"png" and asked == ["Wind chart 1d"]
    (tmp_path / "b").mkdir()
    m2, _, _ = monitor(tmp_path / "b", [(T0, {"wind.wind_gust": 10}), (T0 + 300, {"wind.wind_gust": 12})])
    plain = []

    async def notify2(text, link=None, **kw):
        plain.append(kw)
    m2.notify = notify2
    asking(m2, photo=None)
    await m2.check()
    m2.station.data = [(T0, {"wind.wind_gust": 10}), (T0 + 300, {"wind.wind_gust": 12}), (T0 + 600, {"wind.wind_gust": 70})]
    await m2.check()
    assert plain == [{"kind": "gusts"}]                # no chart: text only


async def test_both_particulates_alerts_carry_the_particulates_chart(tmp_path):
    state, seen = AlertState(tmp_path / "s.json"), []

    async def notify(text, link=None, **kw):
        seen.append((text, kw.get("photo")))
    mon = ParticulatesMonitor(FakeAir(), state, notify)
    asked = asking(mon)
    for pm in (90, 90, 30, 30):
        await mon.check(air_reading(pm))
    assert [("mask" in t, "safe again" in t, p) for t, p in seen] == [(True, False, b"png"), (False, True, b"png")] and asked == ["particulates"] * 2
    quiet = ParticulatesMonitor(FakeAir(), AlertState(tmp_path / "t.json"), notify)
    asking(quiet, photo=None)
    seen.clear()
    for pm in (90, 90):
        await quiet.check(air_reading(pm))
    assert seen == [(seen[0][0], None)]


async def test_other_alerts_send_no_picture(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    seen = []

    async def notify(text, link=None, **kw):
        seen.append(kw)
    m.notify = notify
    for data in (rain(0, 0, 0), rain(0, 0, 1)):
        m.station.data = data
        await m.check()
    assert seen and all("photo" not in kw for kw in seen)


async def test_the_alert_charts_are_the_typed_one_day_charts_drawn_from_each_sources_history(tmp_path, archived_cache):
    from lib.airgradient import AirGradient
    from lib.alerts import pictures
    from tests.fakes import air_transport, archived_station, config
    eco, _ = await archived_station(tmp_path, archived_cache)
    for text in ("Temperature chart 1d", "Wind chart 1d", "UV chart 1d"):
        png = await pictures.chart(eco, text, TZ)
        assert png and png[:4] == b"\x89PNG", text
    transport, _ = air_transport(oldest=datetime.now(timezone.utc) - timedelta(days=5))
    (tmp_path / "air").mkdir()
    air = AirGradient(config(tmp_path / "air"), transport=transport)
    await air.start()
    png = await pictures.chart(air, "Particulates chart 1d", TZ)
    assert png and png[:4] == b"\x89PNG"
    assert await pictures.chart(FakeStation(), "Wind chart 1d", TZ) is None        # a source without the tool: no chart, no error
    await eco.close()
    await air.close()


async def test_the_particulates_chart_draws_pm1_pm25_and_pm10_together(tmp_path):
    from lib import intent
    from lib.airgradient import AirGradient
    from lib.timeutil import now_local
    from lib.tools import Turn
    from tests.fakes import air_transport, config
    transport, _ = air_transport(oldest=datetime.now(timezone.utc) - timedelta(days=5))
    air = AirGradient(config(tmp_path), transport=transport)
    await air.start()
    read = intent.read("Particulates chart 1d", now_local(TZ), True, True)
    turn = Turn(chart_asked=read.chart_asked, text="Particulates chart 1d")
    await air.handle(read.fast[1], turn)
    (panel,) = turn.charts[0].panels
    assert sorted(line.label for line in panel.lines) == ["PM1", "PM10", "PM2.5"]
    await air.close()


async def test_the_uv_alert_carries_the_uv_chart_once_per_spell(tmp_path):
    vals = [3, 5]
    m, state, sent = monitor(tmp_path, uv(*vals))
    seen = []

    async def notify(text, link=None, **kw):
        seen.append((text, kw.get("photo")))
    m.notify = notify
    asked = asking(m)
    await m.check()                                    # the first look sets the mark
    m.station.data = uv(3, 5, 11, 12)
    await m.check()
    assert len(seen) == 1 and "UV index 12" in seen[0][0] and seen[0][1] == b"png" and asked == ["UV chart 1d"]
    m.station.data = uv(3, 5, 11, 12, 12)              # the same spell: nothing more, and no second chart is made
    await m.check()
    assert len(seen) == 1 and asked == ["UV chart 1d"]
    (tmp_path / "b").mkdir()
    plain, _, _ = monitor(tmp_path / "b", uv(3, 5))
    got = []

    async def notify2(text, link=None, **kw):
        got.append(kw)
    plain.notify = notify2
    asking(plain, photo=None)
    await plain.check()
    plain.station.data = uv(3, 5, 11, 12)
    await plain.check()
    assert got == [{"kind": "uv"}]                     # no chart: text only


def test_the_confirmation_rule_counts_distinct_readings_in_a_row():
    from lib.alerts import confirm
    saved = {}
    assert confirm.new_reading(saved, 100) and not confirm.new_reading(saved, 100) and not confirm.new_reading(saved, 50) and confirm.new_reading(saved, 101)
    assert confirm.run(saved, "n", True) == 1 and confirm.run(saved, "n", True) == 2
    assert confirm.run(saved, "n", None) == 2                      # a reading with no value changes nothing
    assert confirm.run(saved, "n", False) == 0 and confirm.run(saved, "n", True) == 1


async def test_uv_needs_two_distinct_readings_even_across_checks_and_a_restart(tmp_path):
    m, state, sent = monitor(tmp_path, uv(3, 5))
    await m.check()                                    # the first look sets the mark
    m.station.data = uv(3, 5, 10)
    await m.check()
    await m.check()                                    # the same reading looked at again does not count twice
    assert sent == []
    restarted = WeatherMonitor(m.station, AlertState(state.path), m.notify)    # a restart: the run is in the state file
    sent2 = []

    async def notify(text, link=None, **kw):
        sent2.append(text)
    restarted.notify = notify
    restarted.station.data = uv(3, 5, 10, 10.5)
    await restarted.check()
    assert len(sent2) == 1 and "UV index 10.5" in sent2[0]
    (tmp_path / "x").mkdir()
    m3, _, sent3 = monitor(tmp_path / "x", uv(3, 5))
    await m3.check()
    m3.station.data = [(T0 + 600, {"solar_and_uvi.uvi": 11}), (T0 + 900, {"solar_and_uvi.uvi": 9.9}), (T0 + 1200, {"solar_and_uvi.uvi": 11})]
    await m3.check()
    assert sent3 == []                                 # a reading below 10 ends the run
    m3.station.data += [(T0 + 1500, {}), (T0 + 1800, {"solar_and_uvi.uvi": 12})]
    await m3.check()
    assert len(sent3) == 1                             # a reading with no value is skipped: 11 then 12 is a run


async def test_a_difference_of_a_degree_or_less_is_not_a_crossing(tmp_path):
    from lib.alerts.weather import side
    assert side({"outdoor.temperature": 21.0, "indoor.temperature": 20.0}) is None and side({"outdoor.temperature": 19.0, "indoor.temperature": 20.0}) is None
    assert side({"outdoor.temperature": 21.1, "indoor.temperature": 20.0}) == "warmer" and side({"outdoor.temperature": 18.9, "indoor.temperature": 20.0}) == "cooler"
    m, state, sent = monitor(tmp_path, temps(10, 20))
    await m.check()
    m.station.data = temps(20.9, 20, T0 + 3 * 3600)    # only 0.9 degrees: not warmer
    await m.check()
    assert sent == []
    m.station.data = temps(21.1, 20, T0 + 3 * 3600 + 300)
    await m.check()
    assert len(sent) == 1 and "warmer outside" in sent[0]


async def test_particulates_count_readings_not_checks(tmp_path):
    state, sent = AlertState(tmp_path / "s.json"), []

    async def notify(text, link=None, **kw):
        sent.append(text)
    mon = ParticulatesMonitor(FakeAir(), state, notify)
    first = air_reading(90)
    await mon.check(first)
    await mon.check(first)                             # the sensor said nothing new: still one reading
    assert sent == []
    await mon.check(air_reading(30))                   # a reading that is not unhealthy ends the run
    await mon.check(first)                             # and an old reading is not counted again
    assert sent == []
    again = ParticulatesMonitor(FakeAir(), AlertState(tmp_path / "s.json"), notify)        # a restart keeps what was counted
    await again.check(air_reading(90))
    await again.check(air_reading(90))
    assert len(sent) == 1 and "mask" in sent[0]


async def test_a_single_spike_never_makes_the_air_unhealthy_but_two_readings_in_a_row_do(tmp_path):
    state, sent = AlertState(tmp_path / "s.json"), []

    async def notify(text, link=None, **kw):
        sent.append(text)
    mon = ParticulatesMonitor(FakeAir(), state, notify)
    stamp = datetime.now(timezone.utc)

    def reading(minutes, latest, before):                           # the lowest of the last readings, as air.latest() gives it
        return Latest(air_reading(latest).reading, stamp + timedelta(minutes=minutes), {"pm2_5": min(latest, before)})
    await mon.check(reading(0, 90, 5))                              # a spike: the reading before it was clean
    await mon.check(reading(30, 90, 5))                             # and again at the next check, still alone
    assert not sent and state.monitor["particulates"]["above"] == 0
    await mon.check(reading(60, 90, 80))                            # two in a row at this check ...
    await mon.check(reading(90, 85, 90))                            # ... and at the next
    assert len(sent) == 1 and "Unhealthy air" in sent[0]
