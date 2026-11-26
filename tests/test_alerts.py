import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from lib.airgradient import Latest
from lib.alert_log import AlertLog
from lib.alerts.pictures import CHARTS
from lib.alerts.particulates import ParticulatesMonitor
from lib.alerts.store import AlertStore
from lib.alerts.weather import WeatherMonitor
from tests.fakes import TZ
from tests import engines

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


class Texts:
    """The alerts a rule appended to its log, read as the list of their texts, oldest first (`events`: the events themselves)."""

    def __init__(self, log: AlertLog):
        self.log = log

    @property
    def events(self) -> list[dict]:
        return self.log.since(0, expired=True)

    def _texts(self) -> list[str]:
        return [e["text"] for e in self.events]

    def __eq__(self, other):
        return self._texts() == list(other)

    def __len__(self):
        return len(self.events)

    def __getitem__(self, i):
        return self._texts()[i]

    def __iter__(self):
        return iter(self._texts())

    def __repr__(self):
        return repr(self._texts())


def store_and_log(tmp_path):
    """The rules' store and an alert log in `tmp_path`, and the texts appended to the log."""
    log = AlertLog(tmp_path / "alerts.sqlite", TZ)
    return AlertStore(tmp_path / "alert_state.json"), log, Texts(log)


def monitor(tmp_path, data):
    state, log, sent = store_and_log(tmp_path)
    m = WeatherMonitor(FakeStation(data), state, log, cooldown_seconds=30 * 60)   # 30 minutes, whatever the default is
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
    state, log, sent = store_and_log(tmp_path)
    mon = ParticulatesMonitor(FakeAir(), state, log)
    asking(mon, photo=None)
    await mon.check(air_reading(80))
    assert not sent                                   # one puff of smoke isn't enough
    await mon.check(air_reading(20))
    await mon.check(air_reading(80))
    assert not sent                                   # the streak was broken
    await mon.check(air_reading(90))
    (event,) = sent.events
    assert "mask" in event["text"] and event["link"] == {"label": "live chart", "url": "https://example.com"}
    assert event["kind"] == "particulates" and event["data"]["pm2_5_ugm3"] == 90 and event["data"]["unsafe"]
    await mon.check(air_reading(90))
    assert len(sent) == 1                             # no repeats within an episode
    await mon.check(air_reading(30))
    assert len(sent) == 1
    await mon.check(air_reading(30))
    assert len(sent) == 2 and "safe again" in sent[1] and not sent.events[1]["data"]["unsafe"]


async def test_mask_threshold_is_official_aqi_151(tmp_path):
    state, log, sent = store_and_log(tmp_path)
    mon = ParticulatesMonitor(FakeAir(), state, log)
    asking(mon, photo=None)
    for _ in range(3):
        await mon.check(air_reading(55.4))            # AQI 150: unhealthy for sensitive groups only
    assert not sent
    for _ in range(2):
        await mon.check(air_reading(55.5))
    assert len(sent) == 1


async def test_stale_air_readings_are_ignored(tmp_path):
    state, log, sent = store_and_log(tmp_path)
    mon = ParticulatesMonitor(FakeAir(), state, log)
    for _ in range(3):
        await mon.check(air_reading(200, age=timedelta(hours=3)))
    assert not sent


# ---------- the state file ----------
def test_a_save_is_on_disk_before_it_replaces_the_old_file(tmp_path, monkeypatch):
    from lib.alerts import store
    calls, replace = [], store.os.replace
    monkeypatch.setattr(store.os, "fsync", lambda fd: calls.append("fsync"))
    monkeypatch.setattr(store.os, "replace", lambda a, b: (calls.append("replace"), replace(a, b)))
    AlertStore(tmp_path / "alert_state.json").save()
    assert calls == ["fsync", "replace"] * 2       # (a new store is written at once, then saved)


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
    from lib.alerts.pollen_and_asthma import PollenAndAsthmaMonitor
    state, log, sent = store_and_log(tmp_path)
    pollen = FakePollen()
    return PollenAndAsthmaMonitor(pollen, state, log), pollen, sent


async def test_high_pollen_and_asthma_alert_once_per_metric_and_day(tmp_path):
    mon, pollen, sent = pollen_monitor(tmp_path)
    for grass, asthma in (("Low", "Low"), ("Moderate", "Moderate")):   # 🟢 and 🟠 are not warnings
        pollen.grass, pollen.asthma = grass, asthma
        await mon.check()
    assert sent == []
    pollen.grass = "High"
    await mon.check()
    await mon.check()                                                  # unchanged: nothing more
    assert sent == ["🔴 Grass pollen forecast is High."]
    pollen.asthma = "High"
    await mon.check()
    assert sent[1:] == ["🔴 Thunderstorm asthma forecast is High. Check your asthma action plan."]
    pollen.grass, pollen.asthma = "Low", "Moderate"                    # dropped below High: ready to warn again
    await mon.check()
    pollen.grass = "High"
    await mon.check()
    assert len(sent) == 3 and sent[-1] == "🔴 Grass pollen forecast is High."
    pollen.grass = "Extreme"                                           # it rises to the top of the scale the same day: said again
    await mon.check()
    await mon.check()
    assert len(sent) == 4 and sent[-1] == "🟣 Grass pollen forecast is Extreme."
    pollen.grass = "High"                                              # (and back, for what follows)
    pollen.today += timedelta(days=1)                                  # a new day, still High
    pollen.fetched_at = time.time()
    await mon.check()
    assert len(sent) == 5 and "Central" not in " ".join(sent)
    event = sent.events[-1]
    assert event["kind"] == "pollen_and_asthma" and event["link"] == {"label": "melbournepollen.com.au", "url": "https://www.melbournepollen.com.au/"}
    assert event["data"] == {"metric": "grass", "level": "High", "for": "2026-10-02"} and event["title"] == "Grass pollen forecast: High"


async def test_a_restart_does_not_repeat_an_alert_and_a_missing_forecast_or_stale_page_is_ignored(tmp_path):
    from lib.alerts.pollen_and_asthma import PollenAndAsthmaMonitor
    mon, pollen, sent = pollen_monitor(tmp_path)
    pollen.grass = "High"
    await mon.check()
    assert len(sent) == 1
    again = PollenAndAsthmaMonitor(pollen, AlertStore(mon.state.path), mon.alerts)   # the state file keeps what was sent
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


async def test_rain_that_starts_again_after_the_stopped_alert_is_announced_at_once(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    flags = [0, 0, 1, 1] + [0] * 6 + [1] * 8                # starts, stops at reading 10 (30 dry minutes), starts again at reading 11, rains on
    for n in (3, 4, 10):
        m.station.data = rain_at(flags, n)
        await m.check()
    assert len(sent) == 2 and "started" in sent[0] and "stopped" in sent[1]
    m.station.data = rain_at(flags, 11)                     # raining again 5 minutes after the "stopped": told at once
    await m.check()
    assert len(sent) == 3 and "started raining" in sent[2]
    m.station.data = rain_at(flags, 16)                     # and rain that goes on is not told again
    await m.check()
    assert len(sent) == 3


async def test_a_shower_after_the_stopped_alert_is_its_own_spell_with_its_own_stopped_alert(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    flags = [0, 0, 1, 1] + [0] * 6 + [1] + [0] * 8          # stopped at reading 10, one wet reading, then dry again
    for n in (3, 4, 10, 11, 14, 19):
        m.station.data = rain_at(flags, n)
        await m.check()
    assert [("started" in t, "stopped" in t) for t in sent] == [(True, False), (False, True), (True, False), (False, True)]


async def test_patchy_showers_inside_the_dry_time_are_one_spell_with_one_started_and_one_stopped(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    flags = [0, 0, 1, 0, 0, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0]   # gaps of 10 to 15 minutes, all under the 30
    for n in range(3, len(flags) + 1):
        m.station.data = rain_at(flags, n)
        await m.check()
    assert [("started" in t, "stopped" in t) for t in sent] == [(True, False), (False, True)]


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


CHART = {"title": "a chart", "drawn_by": {"tool": "summary", "arguments": {"readings": ["wind"], "period": "1d", "chart": True}}}


def asking(mon, photo=CHART):
    """Replace a monitor's chart maker with one that records what was asked and answers `photo` (a chart's data); returns the list of
    requests."""
    asked = []

    async def chart(*text):
        asked.append(text[-1] if text else "particulates")
        return photo
    mon._chart = chart
    return asked


async def test_the_crossing_alert_carries_the_temperature_chart_when_there_is_one(tmp_path):
    m, state, sent = monitor(tmp_path, temps(10, 20))
    asked = asking(m)
    await m.check()                                    # learns: outdoor cooler
    m.station.data = temps(25, 20, T0 + 3 * 3600)
    await m.check()
    assert len(sent) == 1 and sent.events[0]["chart"] == CHART and asked == [CHARTS["temps"]]
    assert sent.events[0]["title"] == "Warmer outside than inside" and sent.events[0]["data"]["outdoor_c"] == 25
    m.station.data = temps(10, 20, T0 + 6 * 3600)      # and back, with no chart this time: the alert still goes out, as text
    asking(m, photo=None)
    await m.check()
    assert len(sent) == 2 and "chart" not in sent.events[1]


async def test_the_gusts_alert_carries_the_wind_chart(tmp_path):
    vals = [10, 12]
    m, state, sent = monitor(tmp_path, [(T0 + i * 300, {"wind.wind_gust": v}) for i, v in enumerate(vals)])
    asked = asking(m)
    await m.check()                                    # the first look sets the mark
    m.station.data = [(T0 + i * 300, {"wind.wind_gust": v}) for i, v in enumerate([10, 12, 55])]
    await m.check()
    (event,) = sent.events
    assert "Strong gusts" in event["text"] and event["chart"]["drawn_by"] == CHART["drawn_by"] and asked == [CHARTS["gusts"]]
    assert event["kind"] == "gusts" and event["data"]["gust_kmh"] == 55 and "link" not in event
    (tmp_path / "b").mkdir()
    m2, _, plain = monitor(tmp_path / "b", [(T0, {"wind.wind_gust": 10}), (T0 + 300, {"wind.wind_gust": 12})])
    asking(m2, photo=None)
    await m2.check()
    m2.station.data = [(T0, {"wind.wind_gust": 10}), (T0 + 300, {"wind.wind_gust": 12}), (T0 + 600, {"wind.wind_gust": 70})]
    await m2.check()
    assert [(e["kind"], e.get("chart")) for e in plain.events] == [("gusts", None)]                  # no chart: text only


async def test_both_particulates_alerts_carry_the_particulates_chart(tmp_path):
    state, log, seen = store_and_log(tmp_path)
    mon = ParticulatesMonitor(FakeAir(), state, log)
    asked = asking(mon)
    for pm in (90, 90, 30, 30):
        await mon.check(air_reading(pm))
    assert [("mask" in e["text"], "safe again" in e["text"], e.get("chart")) for e in seen.events] == [(True, False, CHART), (False, True, CHART)]
    assert asked == ["particulates"] * 2
    (tmp_path / "t").mkdir()
    state, log, quiet_seen = store_and_log(tmp_path / "t")
    quiet = ParticulatesMonitor(FakeAir(), state, log)
    asking(quiet, photo=None)
    for pm in (90, 90):
        await quiet.check(air_reading(pm))
    assert len(quiet_seen) == 1 and "chart" not in quiet_seen.events[0]


def test_every_alert_chart_is_the_one_day_summary_chart_of_its_readings():
    day = {"period": "1d", "chart": True}
    assert CHARTS == {"gusts": ("summary", {"readings": ["wind"], **day}), "uv": ("summary", {"readings": ["uv"], **day}),
                      "temps": ("summary", {"readings": ["temperature"], **day}),
                      "particulates": ("summary", {"readings": ["pm2.5", "pm10", "pm1"], **day})}


async def test_other_alerts_send_no_picture(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    asked = asking(m)
    for data in (rain(0, 0, 0), rain(0, 0, 1)):
        m.station.data = data
        await m.check()
    assert sent.events and all("chart" not in e for e in sent.events) and asked == []


async def test_the_alert_charts_are_one_day_charts_drawn_from_each_sources_history(tmp_path, archived_cache):
    """The chart an alert carries is the one-day chart of its type's fixed call (Wind, Temperature, UV, Particulates), as data, with the
    drawn_by that has it drawn again, made over each source's history."""
    from lib import charts
    from lib.airgradient import AirGradient
    from lib.alerts import pictures
    from lib.specs import from_json
    from tests.fakes import air_transport, archived_station, config
    eco, _ = await archived_station(tmp_path, archived_cache)

    def picture(chart, text):
        spec = from_json(chart)
        assert charts.render_data(chart, TZ)[:4] == b"\x89PNG", text
        assert chart["drawn_by"]["tool"] == "summary" and spec.caption(datetime.now(TZ).date()).startswith(text), text
        return spec
    for kind, text in (("temps", "Temperature"), ("gusts", "Wind"), ("uv", "UV")):
        chart = await pictures.chart_data(eco, pictures.CHARTS[kind], TZ)
        assert picture(chart, text).newest and abs(picture(chart, text).newest - eco.now().timestamp()) < 3600, text
    transport, _ = air_transport(oldest=datetime.now(timezone.utc) - timedelta(days=5))
    (tmp_path / "air").mkdir()
    air = AirGradient(config(tmp_path / "air"), transport=transport)
    await air.start()
    chart = await pictures.chart_data(air, pictures.CHARTS["particulates"], TZ)
    assert picture(chart, "Air quality").newest
    assert await pictures.chart_data(eco, ("nothing", {}), TZ) is None                              # a call that can't be made: no chart, no error
    await eco.close()
    await air.close()


async def test_the_particulates_chart_draws_pm1_pm25_and_pm10_together(tmp_path):
    from lib.airgradient import AirGradient
    from lib.tools import Turn
    from tests.fakes import air_transport, config
    transport, _ = air_transport(oldest=datetime.now(timezone.utc) - timedelta(days=5))
    air = AirGradient(config(tmp_path), transport=transport)
    await air.start()
    turn = Turn(chart_asked=True)
    await engines.air_history(air, {"chart": True, "metrics": ["pm2_5", "pm10", "pm1"]}, turn)
    (panel,) = turn.charts[0].panels
    assert sorted(line.label for line in panel.lines) == ["PM1", "PM10", "PM2.5"]
    await air.close()


async def test_the_uv_alert_carries_the_uv_chart_once_per_spell(tmp_path):
    vals = [3, 5]
    m, state, seen = monitor(tmp_path, uv(*vals))
    asked = asking(m)
    await m.check()                                    # the first look sets the mark
    m.station.data = uv(3, 5, 11, 12)
    await m.check()
    assert len(seen) == 1 and "UV index 12" in seen[0] and seen.events[0]["chart"] == CHART and asked == [CHARTS["uv"]]
    m.station.data = uv(3, 5, 11, 12, 12)              # the same spell: nothing more, and no second chart is made
    await m.check()
    assert len(seen) == 1 and asked == [CHARTS["uv"]]
    (tmp_path / "b").mkdir()
    plain, _, got = monitor(tmp_path / "b", uv(3, 5))
    asking(plain, photo=None)
    await plain.check()
    plain.station.data = uv(3, 5, 11, 12)
    await plain.check()
    assert [(e["kind"], e.get("chart")) for e in got.events] == [("uv", None)]                       # no chart: text only


def test_an_alert_rearms_only_once_its_condition_has_stopped_for_the_whole_wait():
    from lib.alerts import rules
    m = {"active": True, "calm_since": None}             # it has fired
    for now in (T0, T0 + 60):
        rules.rearm(m, False, now, 100)
    assert m["active"] and m["calm_since"] == T0
    rules.rearm(m, True, T0 + 90, 100)                 # the condition again: the calm starts over
    for now in (T0 + 120, T0 + 200):
        rules.rearm(m, False, now, 100)
    assert m["active"]                                   # only 80 s of calm since T0 + 120
    rules.rearm(m, False, T0 + 220, 100)
    assert not m["active"] and m["calm_since"] is None   # it can fire again


def test_the_confirmation_rule_counts_distinct_readings_in_a_row():
    from lib.alerts import rules
    saved = {}
    assert rules.new_reading(saved, 100) and not rules.new_reading(saved, 100) and not rules.new_reading(saved, 50) and rules.new_reading(saved, 101)
    assert rules.run(saved, "n", True) == 1 and rules.run(saved, "n", True) == 2
    assert rules.run(saved, "n", None) == 2                      # a reading with no value changes nothing
    assert rules.run(saved, "n", False) == 0 and rules.run(saved, "n", True) == 1


async def test_uv_needs_two_distinct_readings_even_across_checks_and_a_restart(tmp_path):
    m, state, sent = monitor(tmp_path, uv(3, 5))
    await m.check()                                    # the first look sets the mark
    m.station.data = uv(3, 5, 10)
    await m.check()
    await m.check()                                    # the same reading looked at again does not count twice
    assert sent == []
    restarted = WeatherMonitor(m.station, AlertStore(state.path), AlertLog(tmp_path / "alerts.sqlite", TZ),   # (the same log)
                               cooldown_seconds=30 * 60)    # a restart: the run is in the state file
    restarted.station.data = uv(3, 5, 10, 10.5)
    await restarted.check()
    assert len(sent) == 1 and "UV index 10.5" in sent[0]
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
    state, log, sent = store_and_log(tmp_path)
    mon = ParticulatesMonitor(FakeAir(), state, log)
    asking(mon, photo=None)
    first = air_reading(90)
    await mon.check(first)
    await mon.check(first)                             # the sensor said nothing new: still one reading
    assert sent == []
    await mon.check(air_reading(30))                   # a reading that is not unhealthy ends the run
    await mon.check(first)                             # and an old reading is not counted again
    assert sent == []
    again = ParticulatesMonitor(FakeAir(), AlertStore(state.path), log)        # a restart keeps what was counted
    asking(again, photo=None)
    await again.check(air_reading(90))
    await again.check(air_reading(90))
    assert len(sent) == 1 and "mask" in sent[0]


async def test_a_single_spike_never_makes_the_air_unhealthy_but_two_readings_in_a_row_do(tmp_path):
    state, log, sent = store_and_log(tmp_path)
    mon = ParticulatesMonitor(FakeAir(), state, log)
    asking(mon, photo=None)
    stamp = datetime.now(timezone.utc)

    def reading(minutes, latest, before):                           # the lowest of the last readings, as air.latest() gives it
        return Latest(air_reading(latest).reading, stamp + timedelta(minutes=minutes), {"pm2_5": min(latest, before)})
    await mon.check(reading(0, 90, 5))                              # a spike: the reading before it was clean
    await mon.check(reading(30, 90, 5))                             # and again at the next check, still alone
    assert not sent and state.monitor["particulates"]["above"] == 0
    await mon.check(reading(60, 90, 80))                            # two in a row at this check ...
    await mon.check(reading(90, 85, 90))                            # ... and at the next
    assert len(sent) == 1 and "Unhealthy air" in sent[0]


def test_the_default_wait_before_the_rain_has_stopped_is_an_hour(tmp_path):
    from lib.alerts.weather import ALERT_COOLDOWN_SECONDS
    assert ALERT_COOLDOWN_SECONDS == 60 * 60 == WeatherMonitor(FakeStation([]), AlertStore(tmp_path / "s.json"), None).cooldown


async def test_a_live_reading_alone_starts_the_rain_alert_without_the_rest_of_the_check(tmp_path):
    m, state, sent = monitor(tmp_path, rain(0, 0, 0))
    await m.check()
    m.station.live = (T0 + 3 * 300, {"rainfall.rain_rate": 1.5, "rainfall.daily": 0.9})
    await asyncio.gather(m.check_rain(), m.check_rain())                  # two at once: one alert
    assert len(sent) == 1 and "started raining" in sent[0]
    m.station.live = None
    await m.check_rain()                                                  # no live reading: nothing to add
    assert len(sent) == 1


async def test_the_pollen_alert_waits_for_7_30_am(tmp_path):
    mon, pollen, sent = pollen_monitor(tmp_path)
    pollen.grass = "Extreme"
    pollen.today = datetime(2026, 10, 30, 7, 20)
    await mon.check()
    assert sent == []                                                  # the page is read from 6 am: not yet
    pollen.today = datetime(2026, 10, 30, 7, 30)
    await mon.check()
    assert sent == ["🟣 Grass pollen forecast is Extreme."]
