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

    async def recent(self, hours):
        return self.data

    def now(self):
        return datetime.now(TZ).replace(tzinfo=None)

    async def readings(self, *a, **k):
        return self.data


def monitor(tmp_path, data):
    state, sent = AlertState(tmp_path / "s.json"), []

    async def notify(text, link=None):
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
    m.station.data = rain(0, 0, 1, 1, 0, 0, 0, 0, 0, 0)  # 30 dry minutes since the last wet reading
    await m.check()
    assert len(sent) == 2 and "stopped" in sent[1] and "mm fell" in sent[1]


async def test_a_dry_gap_shorter_than_30_minutes_does_not_flap(tmp_path):
    m, state, sent = monitor(tmp_path, None)
    for data in (rain(1, 1), rain(1, 1, 0, 0), rain(1, 1, 0, 0, 0), rain(1, 1, 0, 0, 0, 1), rain(1, 1, 0, 0, 0, 1, 0)):
        m.station.data = data
        await m.check()
    assert len(sent) == 1 and "started" in sent[0]


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

    async def notify(text, link=None):
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

    async def notify(text, link=None):
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

    async def notify(text, link=None):
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
