import time
from datetime import datetime, timedelta, timezone

from lib.alerts import AirMonitor, AlertState, Notifier, WeatherMonitor, with_footer
from lib.ecowitt import outlook
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
        return self.data

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


async def test_rain_starts_once_then_stops_after_60_dry_minutes(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    for data in (rain(0, 0, 0), rain(0, 0, 1), rain(0, 0, 1, 1), rain(0, 0, 1, 1, 0), rain(0, 0, 1, 1, 0, 0)):
        m.station.data = data
        await m.check()
    assert len(sent) == 1 and "started raining" in sent[0]
    m.station.data = rain(0, 0, 1, 1, *[0] * 12)  # 60 dry minutes since the last wet reading
    await m.check()
    assert len(sent) == 2 and "stopped" in sent[1] and "mm fell" in sent[1]


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


def test_the_status_outlook_says_raining_now_or_likely_soon_or_nothing():
    assert outlook.rain_outlook(rain(0, 0, 1), TZ, 145.0) == "raining now (1.2 mm/h)"
    assert outlook.rain_outlook(rain(0, 0, 0), TZ, 145.0) is None
    likely = outlook.rain_outlook(pressure_rows(4.0, 14), TZ, 145.0)
    assert likely.startswith("rain looks likely soon: pressure down") and "not an official forecast" in likely
    assert outlook.rain_outlook(pressure_rows(0.2, 14), TZ, 145.0) is None


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


def pressure_rows(drop, hour_local, hum=93.0, dew_rise=0.0, temp=15.0):
    """3 hours of readings ending at the given local hour, pressure falling `drop` hPa in total."""
    end = int(datetime(2026, 9, 29, hour_local, 0, tzinfo=TZ).timestamp())
    n = 37
    return rows(n, start=end - 36 * 300, **{
        "pressure.relative": lambda i: 1015 - drop * i / 36, "outdoor.humidity": hum,
        "outdoor.temperature": temp, "outdoor.dew_point": lambda i: temp - 1 + dew_rise * i / 36,
        "rainfall.rain_rate": 0.0, "wind.wind_gust": 10.0})


def test_daytime_fall_with_humid_air_predicts_rain():
    out = outlook.assess_rain(pressure_rows(3.2, 15), TZ, 145.0)
    assert out and out.score >= outlook.PREDICT_MIN_SCORE and not out.night


def test_the_overnight_pressure_dip_and_cooling_are_not_rain():
    # ~4am: pressure sags with the daily tide and the air sits near its dew point, as every night
    for drop in (0.5, 1.2, 1.6):
        out = outlook.assess_rain(pressure_rows(drop, 4), TZ, 145.0)
        assert out is None or out.score < outlook.PREDICT_MIN_SCORE, (drop, out)


def test_a_real_night_front_still_counts():
    out = outlook.assess_rain(pressure_rows(4.5, 3, dew_rise=2.5), TZ, 145.0)
    assert out and out.night and out.score >= outlook.PREDICT_MIN_SCORE


async def test_rain_likely_alerts_at_most_every_6_hours(tmp_path):
    m, state, sent = monitor(tmp_path, pressure_rows(3.5, 15))
    await m.check()
    await m.check()
    assert [s for s in sent if "likely" in s].__len__() == 1


async def test_no_rain_prediction_while_raining(tmp_path):
    data = pressure_rows(3.5, 15)
    data[-1][1]["rainfall.rain_rate"] = 2.0
    m, state, sent = monitor(tmp_path, data)
    await m.check()
    assert not [s for s in sent if "likely" in s]


def temps(outdoor, indoor, ts=T0):
    return [(ts, {"outdoor.temperature": outdoor, "indoor.temperature": indoor})]


async def test_temperature_crossing_needs_two_days_since_the_last_one(tmp_path):
    m, state, sent = monitor(tmp_path, temps(10, 20))
    await m.check()                                    # learns: outdoor cooler
    m.station.data = temps(25, 20, T0 + 3 * 86400)     # flips after 3 days -> alert
    await m.check()
    assert len(sent) == 1 and "warmer outside" in sent[0] and "3 days" in sent[0]
    m.station.data = temps(10, 20, T0 + 3 * 86400 + 3600)  # flips back within a day -> quiet
    await m.check()
    assert len(sent) == 1
    m.station.data = temps(25, 20, T0 + 3 * 86400 + 7200)
    await m.check()
    assert len(sent) == 1
    m.station.data = temps(10, 20, T0 + 6 * 86400)     # "warmer" held ~3 days, then flips -> alert
    await m.check()
    assert len(sent) == 2 and "cooler outside" in sent[1]
    m.station.data = temps(25, 20, T0 + 6 * 86400 + 60)  # and straight back -> quiet
    await m.check()
    assert len(sent) == 2


async def test_small_temperature_differences_are_noise(tmp_path):
    m, state, sent = monitor(tmp_path, temps(20.1, 20.0))
    await m.check()
    assert "cross" not in state.monitor and not sent


# ---------- air ----------
def air_reading(pm25, pm10=10.0, age=timedelta(minutes=5)):
    return {"pm2_5": {"value": pm25, "aqi_us": 160}, "pm10": {"value": pm10},
            "_time_utc": datetime.now(timezone.utc) - age}


class FakeAir:
    tz, link = TZ, ("live chart", "https://example.com")


async def test_mask_alert_needs_two_bad_checks_and_pairs_with_all_clear(tmp_path):
    state, sent = AlertState(tmp_path / "s.json"), []

    async def notify(text, link=None, **kw):
        sent.append((text, link))
    mon = AirMonitor(FakeAir(), state, notify)
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
    mon = AirMonitor(FakeAir(), state, notify)
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
    mon = AirMonitor(FakeAir(), state, notify)
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
    state.monitor["rain"] = {"raining": True, "since": 5}
    state.save()
    again = AlertState(path)
    assert again.alert_chats() == [1] and again.monitor["rain"]["raining"] and not again.alerts_on(2)


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


def uv(*values, start=T0):
    return [(start + i * 300, {"solar_and_uvi.uvi": v}) for i, v in enumerate(values)]


async def test_a_uv_index_of_9_alerts_once_and_again_only_after_an_hour_below_it(tmp_path):
    vals = [3, 5]
    m, state, sent = monitor(tmp_path, uv(*vals))

    async def step(*more):
        vals.extend(more)
        m.station.data = uv(*vals)
        await m.check()
    await m.check()                                    # the first look sets the mark
    await step(8.9)
    assert sent == []
    await step(9)
    assert len(sent) == 1 and "UV index 9" in sent[0] and "1:15pm" in sent[0]
    await step(10, 7, *[6] * 11)                       # still the same spell, then under an hour below 9
    await step(9.5)
    assert len(sent) == 1
    await step(*[5] * 12)                              # a calm hour starts counting
    await step(*[5] * 12)                              # an hour below 9 re-arms it
    await step(9)
    assert len(sent) == 2


async def test_old_uv_readings_are_not_announced_after_a_restart(tmp_path):
    m, state, sent = monitor(tmp_path, uv(10, 11, 4))
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
    from lib.alerts import PollenMonitor
    state, sent = AlertState(tmp_path / "s.json"), []

    async def notify(text, link=None, **kw):
        sent.append(text)
    pollen = FakePollen()
    return PollenMonitor(pollen, state, notify), pollen, sent


async def test_high_or_extreme_pollen_and_asthma_alert_once_per_level_and_day(tmp_path):
    mon, pollen, sent = pollen_monitor(tmp_path)
    for grass, asthma in (("Low", "Low"), ("Moderate", "Moderate")):
        pollen.grass, pollen.asthma = grass, asthma
        await mon.check()
    assert sent == []
    pollen.grass = "High"
    await mon.check()
    await mon.check()                                                  # unchanged: nothing more
    assert sent == ["🟠 Grass pollen is High."]
    pollen.grass, pollen.asthma = "Extreme", "Extreme"
    await mon.check()
    assert sent[1:] == ["🔴 Grass pollen is Extreme.", "🔴 Thunderstorm asthma risk is Extreme. Check your asthma action plan."]
    pollen.grass, pollen.asthma = "Low", "Moderate"                    # dropped below High: ready to warn again
    await mon.check()
    pollen.grass = "High"
    await mon.check()
    assert len(sent) == 4 and sent[-1] == "🟠 Grass pollen is High."
    pollen.today += timedelta(days=1)                                  # a new day, still High
    pollen.fetched_at = time.time()
    await mon.check()
    assert len(sent) == 5 and "Central" not in " ".join(sent)


async def test_a_restart_does_not_repeat_an_alert_and_a_missing_forecast_or_stale_page_is_ignored(tmp_path):
    from lib.alerts import PollenMonitor
    mon, pollen, sent = pollen_monitor(tmp_path)
    pollen.grass = "High"
    await mon.check()
    assert len(sent) == 1
    again = PollenMonitor(pollen, mon.state, mon.notify)               # the state file keeps what was sent
    await again.check()
    assert len(sent) == 1
    pollen.asthma = None                                               # off-season: nothing to say
    pollen.grass, pollen.fetched_at = "Extreme", time.time() - 3 * 3600  # and a page not fetched for 3 hours is not news
    await again.check()
    assert len(sent) == 1


async def test_no_pollen_alert_out_of_season_even_with_a_high_reading_cached(tmp_path):
    mon, pollen, sent = pollen_monitor(tmp_path)
    pollen.grass, pollen.asthma, pollen.season = "Extreme", "High", False
    await mon.check()
    assert sent == []
    pollen.season = True
    await mon.check()
    assert len(sent) == 2


async def test_the_time_dry_before_the_rain_stops_is_configurable(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    m.rain_stop_seconds = 30 * 60
    for data in (rain(0, 0, 0), rain(0, 0, 1), rain(0, 0, 1, 1)):
        m.station.data = data
        await m.check()
    m.station.data = rain(0, 0, 1, 1, 0, 0, 0, 0, 0, 0)   # 30 dry minutes
    await m.check()
    assert len(sent) == 2 and "stopped" in sent[1]


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
    for data in (night_rain(one_am, 0, 0, 0), night_rain(one_am, 0, 0, 1), night_rain(one_am, 0, 0, 1, 1, 1),
                 night_rain(one_am, 0, 0, 1, 1, 1, *[0] * 13)):   # it starts, rains, stops after 60 dry minutes
        m.station.data = data
        await m.check()
    assert sent == []                                              # all in quiet hours
    m.station.data = night_rain(int(datetime(2026, 9, 30, 5, 55, tzinfo=TZ).timestamp()), 0, 0, 0)   # 5:55 to 6:05
    await m.check()
    assert len(sent) == 1 and sent[0].startswith("\U0001f327️ Overnight rain: 1.2 mm, from about 1:10am to 1:25am.")
    await m.check()
    assert len(sent) == 1                                          # once
    m.station.data = night_rain(int(datetime(2026, 9, 30, 13, 0, tzinfo=TZ).timestamp()), 0, 1)
    await m.check()
    assert len(sent) == 2 and "started raining" in sent[1]         # by day, rain alerts are back


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
