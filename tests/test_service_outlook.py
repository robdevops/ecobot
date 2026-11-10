"""The `outlook` tool (lib/service/outlook.py): will it rain in the next few hours, by the prompt's rules worked out in code from the
station's last 3 hours and its reading now: each rule alone, their combination, the result's shape, the forecast's lines beside it,
what is held when the request says not to fetch, and the refusal when there is nothing to read."""

import json
import time
from datetime import datetime, timedelta

import pytest

from lib.ecowitt.source import LIVE_GROUPS
from lib.service.common import FRESH
from lib.service.context import Context
from lib.service.outlook import DESCRIPTION, GROUPS, HOW_TO_READ, PARAMETERS, dew_point, read, tools
from tests.clocks import pin
from tests.fakes import TZ, archived_station

NOW = datetime(2026, 11, 10, 14, 5, tzinfo=TZ)   # Tue 10 Nov 2026 2:05pm
NOW_TS = int(NOW.timestamp())
STEPS = 36   # 5-minute readings over the 3 hours: 37 of them, the last at NOW


def got(text: str) -> dict:
    return json.loads(text)


def at(spec, i: int):
    """A reading's value at step i of STEPS: a constant, (first, last) in a straight line, or a function of i."""
    if callable(spec):
        return spec(i)
    if isinstance(spec, tuple):
        return round(spec[0] + (spec[1] - spec[0]) * i / STEPS, 2)
    return spec


def station(pressure=(1012.0, 1012.0), humidity=70.0, temperature=15.0, dew=8.0, daily=0.0, rate=0.0, wind=10.0, now_ts=NOW_TS,
            gauge="rainfall") -> list[tuple[int, dict]]:
    """The station's last 3 hours of 5-minute readings to `now_ts`, as Ecowitt.recent gives them; a reading given as None is left out."""
    rows = []
    for i in range(STEPS + 1):
        row = {"pressure.relative": at(pressure, i), "outdoor.humidity": at(humidity, i), "outdoor.temperature": at(temperature, i),
               "outdoor.dew_point": at(dew, i), f"{gauge}.daily": at(daily, i), f"{gauge}.rain_rate": at(rate, i),
               "wind.wind_speed": at(wind, i)}
        rows.append((now_ts - (STEPS - i) * 300, {k: v for k, v in row.items() if v is not None}))
    return rows


def outlook(rows=None, live=None, live_ts=None, **kw) -> dict:
    return read(rows if rows is not None else station(**kw), live, NOW_TS, live_ts)


# ---------- each rule ----------
@pytest.mark.parametrize("pressure, verdict, tendency, change", [
    ((1012.0, 1008.5), "likely", "falling", -3.5),     # down more than 3 hPa
    ((1012.0, 1009.0), "possible", "falling", -3.0),   # 1 to 3 hPa: possible (3 is not more than 3)
    ((1012.0, 1009.6), "possible", "falling", -2.4),
    ((1012.0, 1011.0), "possible", "falling", -1.0),
    ((1012.0, 1011.1), "unlikely", "steady", -0.9),    # under 1 hPa: steady
    ((1009.0, 1011.0), "unlikely", "rising", 2.0),
    ((1012.0, 1012.0), "unlikely", "steady", 0.0),
])
def test_the_pressure_tendency_over_3_hours_sets_the_verdict(pressure, verdict, tendency, change):
    out = outlook(pressure=pressure)
    assert (out["verdict"], out["pressure"]["tendency"], out["pressure"]["change_hpa"]) == (verdict, tendency, change)
    assert out["pressure"]["level"] == "normal" and out["pressure"]["hpa"] == pressure[1]


def test_falling_or_rising_is_whether_the_high_or_the_low_came_first_not_where_it_ends():
    dip = lambda i: 1012.0 - 3.5 * min(i, 30) / 30 + (0.5 if i > 30 else 0)   # down 3.5 hPa, then back up a little at the end
    out = outlook(pressure=dip)
    assert out["pressure"]["tendency"] == "falling" and out["pressure"]["change_hpa"] == -3.5 and out["verdict"] == "likely"
    rise = lambda i: 1008.5 + 3.5 * min(i, 30) / 30 - (0.5 if i > 30 else 0)    # up 3.5, then down a little
    assert outlook(pressure=rise)["pressure"]["tendency"] == "rising" and outlook(pressure=rise)["verdict"] == "unlikely"


def test_the_reason_cites_one_or_two_readings_ready_to_quote():
    out = outlook(pressure=(1012.0, 1009.6), humidity=91.0)
    assert out["verdict"] == "likely" and out["reason"] == "pressure down 2.4 hPa in 3 hours, humidity 91%"   # the prompt's example
    assert out["pressure"]["text"] == "1009.6 hPa, down 2.4 hPa in 3 hours"
    assert outlook(pressure=(1012.0, 1008.5))["reason"] == "pressure down 3.5 hPa in 3 hours, humidity 70%"
    assert outlook(pressure=(1009.0, 1011.0))["reason"] == "pressure up 2.0 hPa in 3 hours, humidity 70%"
    steady = outlook(pressure=(1012.0, 1011.4))
    assert steady["reason"] == "pressure steady, humidity 70%"
    assert steady["pressure"]["text"] == "1011.4 hPa, steady (down 0.6 hPa in 3 hours)"


def test_the_pressure_level_unsettled_or_settled_moves_the_verdict_a_step():
    low = outlook(pressure=(1003.4, 1003.0))
    assert low["pressure"]["level"] == "unsettled" and low["verdict"] == "possible"
    assert low["reason"] == "low pressure (1003.0 hPa), pressure steady" and low["pressure"]["text"].endswith("; under 1005 hPa: unsettled")
    high = outlook(pressure=(1026.0, 1024.0))
    assert high["pressure"]["level"] == "settled" and high["verdict"] == "unlikely"   # falling 2 hPa, but settled
    assert high["reason"] == "high pressure (1024.0 hPa), pressure down 2.0 hPa in 3 hours"
    assert high["pressure"]["text"].endswith("; over 1020 hPa: settled")
    assert outlook(pressure=(1005.0, 1005.0))["pressure"]["level"] == outlook(pressure=(1020.0, 1020.0))["pressure"]["level"] == "normal"


def test_moist_air_by_humidity_or_by_the_dew_point_moves_the_verdict_a_step():
    humid = outlook(humidity=90.0)
    assert humid["moisture"] == {"humidity": 90, "dew_point_spread": 7.0, "moist": True,
                                 "text": "humidity 90%, 7.0 °C above the dew point"}
    assert humid["verdict"] == "possible" and humid["reason"] == "humidity 90%, pressure steady"
    near = outlook(humidity=85.0, temperature=15.0, dew=13.5)
    assert near["moisture"]["moist"] and near["moisture"]["dew_point_spread"] == 1.5 and near["verdict"] == "possible"
    assert near["reason"] == "temperature within 1.5 °C of the dew point, pressure steady"
    dry = outlook(humidity=89.0, dew=12.9)
    assert not dry["moisture"]["moist"] and dry["moisture"]["dew_point_spread"] == 2.1 and dry["verdict"] == "unlikely"
    at_dew = outlook(humidity=None, temperature=12.0, dew=12.0)
    assert at_dew["moisture"] == {"humidity": None, "dew_point_spread": 0.0, "moist": True, "text": "at the dew point"}


def test_a_station_without_a_dew_point_has_it_worked_out():
    assert dew_point(20.0, 50.0) == pytest.approx(9.26, abs=0.05) and dew_point(15.0, 100.0) == pytest.approx(15.0, abs=0.01)
    out = outlook(dew=None, temperature=15.0, humidity=88.0)
    assert out["moisture"]["dew_point_spread"] == 2.0 and out["moisture"]["moist"] and "missing" not in out


def test_rain_now_or_in_the_last_hour_makes_it_likely_whatever_the_pressure():
    raining = outlook(pressure=(1009.0, 1011.0), rate=lambda i: 1.8 if i == STEPS else 0.0, daily=lambda i: 0.2 if i == STEPS else 0.0)
    assert raining["verdict"] == "likely" and raining["recent_rain"] == {"rained": True, "raining_now": True, "rate_mm_h": 1.8,
                                                                        "last_hour_mm": 0.2,
                                                                        "text": "raining now (1.8 mm/h), 0.2 mm in the last hour"}
    assert raining["reason"] == "raining now (1.8 mm/h), pressure up 2.0 hPa in 3 hours"
    shower = outlook(daily=lambda i: 0.0 if i < 30 else 0.6)   # 0.6 mm 30 minutes ago, dry since
    assert shower["verdict"] == "likely" and shower["recent_rain"]["raining_now"] is False
    assert shower["recent_rain"]["text"] == "0.6 mm in the last hour" and shower["reason"] == "0.6 mm in the last hour, pressure steady"
    earlier = outlook(daily=lambda i: 0.0 if i < 10 else 0.6)   # 2 hours ago: not recent
    assert earlier["verdict"] == "unlikely" and earlier["recent_rain"]["rained"] is False
    assert earlier["recent_rain"]["text"] == "no rain in the last hour" and earlier["recent_rain"]["last_hour_mm"] == 0.0
    trace = outlook(daily=lambda i: 0.0 if i < 30 else 0.02)
    assert trace["recent_rain"]["text"] == "a trace of rain in the last hour" and trace["verdict"] == "likely"


def test_rain_across_midnight_counts_the_reset():
    late = NOW.replace(hour=0, minute=20)
    rows = station(now_ts=int(late.timestamp()), daily=lambda i: 3.0 if i < 32 else 0.0 if i == 32 else 0.4)   # reset at midnight
    out = read(rows, None, int(late.timestamp()))
    assert out["recent_rain"]["last_hour_mm"] == 0.4 and out["verdict"] == "likely"


def test_the_piezo_gauge_is_read_when_the_station_has_no_other():
    out = outlook(gauge="rainfall_piezo", rate=lambda i: 2.0 if i == STEPS else 0.0)
    assert out["recent_rain"]["raining_now"] and out["verdict"] == "likely"


def test_wind_picking_up_strengthens_a_falling_pressure_only():
    picking_up = lambda i: 5.0 if i < 12 else 14.0
    out = outlook(pressure=(1012.0, 1010.0), wind=picking_up)
    assert out["wind_trend"] == {"trend": "picking up", "first_hour_kmh": 5.0, "last_hour_kmh": 14.0,
                                 "text": "picking up: 14.0 km/h on average in the last hour, 5.0 km/h 2 to 3 hours ago"}
    assert out["verdict"] == "likely" and out["reason"] == "pressure down 2.0 hPa in 3 hours, wind picking up"
    assert outlook(pressure=(1010.0, 1012.0), wind=picking_up)["verdict"] == "unlikely"   # rising pressure: it doesn't count
    assert outlook(pressure=(1012.0, 1011.6), wind=picking_up)["verdict"] == "unlikely"   # nor with steady pressure
    assert outlook(wind=lambda i: 15.0 if i < 12 else 9.0)["wind_trend"]["trend"] == "easing"
    assert outlook(wind=lambda i: 10.0 if i < 12 else 14.0)["wind_trend"]["trend"] == "steady"


# ---------- the rules together ----------
@pytest.mark.parametrize("kw, verdict", [
    (dict(pressure=(1004.0, 1001.0), humidity=95.0, wind=lambda i: 5.0 if i < 12 else 14.0), "likely"),   # everything: no more than likely
    (dict(pressure=(1012.0, 1010.0), humidity=93.0), "likely"),          # possible, and moist
    (dict(pressure=(1004.0, 1003.5), humidity=93.0), "likely"),          # steady, but unsettled and moist
    (dict(pressure=(1024.0, 1024.2), humidity=93.0), "unlikely"),        # moist, but settled
    (dict(pressure=(1025.0, 1021.0)), "possible"),                       # likely, but settled
    (dict(pressure=(1025.0, 1021.0), rate=0.4), "likely"),               # ... and it's raining
    (dict(pressure=(1010.0, 1014.0), humidity=60.0), "unlikely"),
])
def test_the_rules_together_stay_between_unlikely_and_likely(kw, verdict):
    assert outlook(**kw)["verdict"] == verdict


def test_the_reading_now_is_used_when_it_is_newer_than_the_5_minute_readings():
    rows = station(pressure=(1012.0, 1010.0), now_ts=NOW_TS - 240)
    live = {"pressure.relative": 1008.0, "rainfall.rain_rate": 0.0, "outdoor.humidity": 92.0, "outdoor.temperature": 14.0,
            "outdoor.dew_point": 12.7}
    out = read(rows, live, NOW_TS, NOW_TS - 30)
    assert out["pressure"]["change_hpa"] == -4.0 and out["moisture"]["humidity"] == 92 and out["newest"] == NOW_TS - 30
    stale = read(rows, live, NOW_TS, NOW_TS - 600)   # older than the newest 5 minutes': the rows say it all
    assert stale["pressure"]["change_hpa"] == -2.0 and stale["newest"] == NOW_TS - 240


def test_what_the_station_lacks_is_listed_and_the_verdict_rests_on_the_rest():
    rows = [(ts, {k: v for k, v in r.items() if not k.startswith("pressure")}) for ts, r in station(humidity=95.0)]
    out = read(rows, None, NOW_TS)
    assert out["pressure"] is None and out["missing"] == ["pressure"] and out["verdict"] == "possible" and out["reason"] == "humidity 95%"
    assert read([], None, NOW_TS) == {"verdict": "unlikely", "reason": "too few readings in the last 3 hours to tell", "pressure": None,
                                      "moisture": None, "recent_rain": None, "wind_trend": None,
                                      "missing": ["pressure", "humidity and dew point", "rain", "wind"], "newest": None}


# ---------- the tool ----------
class Station:
    """A stand-in for lib.ecowitt.Ecowitt: what the outlook reads of it, and what it was asked."""

    def __init__(self, rows, live=None):
        self.rows, self.tz, self.mac, self.groups = rows, TZ, "M", ["outdoor", "indoor", "pressure", "wind", "rainfall", "solar_and_uvi"]
        self._live = (time.time(), live) if live else None
        self.asked = []
        self.storage = self   # (its read() is the file's)

    def fetcher(self, groups, urgent=False):
        self.asked.append(("fetcher", tuple(groups), urgent))
        return self

    async def get(self, cycle, start, end, refresh=False, load=True):
        self.asked.append(("get", cycle, start, end))
        return self.rows

    async def read(self, fn, mac, groups, cycle, start, end):
        self.asked.append(("held", fn.__name__, tuple(groups), cycle, start, end))
        return self.rows

    async def _live_data(self, groups, max_age=90, urgent=True):
        self.asked.append(("live", tuple(groups), urgent))
        return self._live[1] if self._live else {}


def live_reading(ts=NOW_TS, **values) -> dict:
    """A real_time response: {group: {field: {value, unit, time}}}."""
    out = {}
    for name, value in values.items():
        group, field = name.split("__")
        out.setdefault(group, {})[field] = {"value": str(value), "unit": "", "time": str(ts)}
    return out


class HeldForecast:
    """A stand-in for lib.forecast.Forecast with its days held."""

    def __init__(self, days):
        self.days, self.fetched_at, self.place, self.tz, self.warmed = days, NOW_TS - 600, "Melbourne", TZ, 0

    async def warm(self, fresh=True):
        self.warmed += 1


def context(eco, forecast=None, now_ts=NOW_TS) -> Context:
    return Context(eco=eco, forecast=forecast, tz=TZ, clock=lambda: now_ts)


async def test_the_result_has_the_verdict_its_reason_each_rules_figures_and_the_forecasts_lines():
    eco = Station(station(pressure=(1012.0, 1009.6), humidity=91.0, now_ts=NOW_TS - 120),
                  live_reading(rainfall__rain_rate=0.0, pressure__relative=1009.5))
    today = NOW.date()
    days = [{"date": today - timedelta(days=1), "min_c": 9, "max_c": 15, "summary": "☁️ Overcast.", "rain_chance_pct": 20},
            {"date": today, "min_c": 11, "max_c": 17, "summary": "🌧️ Rain.", "rain_chance_pct": 80, "rain_1mm_pct": 65},
            {"date": today + timedelta(days=1), "min_c": 10, "max_c": 18, "summary": "🌦️ Showers.", "rain_chance_pct": 60},
            {"date": today + timedelta(days=2), "min_c": 12, "max_c": 22, "summary": "☀️ Clear.", "rain_chance_pct": 0}]
    (outlook_tool,) = tools(context(eco, HeldForecast(days)))
    assert outlook_tool.name == "outlook" and outlook_tool.annotations == {"readOnlyHint": True, "openWorldHint": True}
    out = got(await outlook_tool.call({}))
    assert list(out) == ["as_of", "fresh", "verdict", "reason", "pressure", "moisture", "recent_rain", "wind_trend", "forecast",
                         "how_to_read"]
    assert out["as_of"] == {"text": "Tue 10 Nov 2026 2:05pm", "time": NOW.isoformat()} and out["fresh"] == FRESH
    assert out["verdict"] == "likely" and out["reason"] == "pressure down 2.5 hPa in 3 hours, humidity 91%"
    assert out["pressure"] == {"hpa": 1009.5, "change_hpa": -2.5, "tendency": "falling", "level": "normal",
                               "text": "1009.5 hPa, down 2.5 hPa in 3 hours"}
    assert out["moisture"]["humidity"] == 91 and out["recent_rain"]["text"] == "no rain in the last hour"
    assert out["wind_trend"]["trend"] == "steady"
    assert out["forecast"] == ["Today: 🌧️ Rain. 11–17°C, 65% chance of at least 1 mm",
                               "Wednesday: 🌦️ Showers. 10–18°C, 60% chance of rain"]   # (yesterday's and the day after's left out)
    assert out["how_to_read"] == HOW_TO_READ
    groups = ("outdoor", "pressure", "rainfall", "wind")   # (the station has no piezo gauge)
    assert eco.asked[0][:3] == ("fetcher", groups, True) and eco.asked[1][:2] == ("get", "5min")
    assert eco.asked[2] == ("live", tuple(LIVE_GROUPS), True)   # (every group, as the report asks: the reading is kept for the next)
    assert eco.asked[1][2:] == (datetime(2026, 11, 10, 11, 5), datetime(2026, 11, 10, 14, 5))   # the last 3 hours, on the station's clock


async def test_with_fetch_false_it_reads_what_is_held_and_drawn_ahead_its_fetches_wait_their_turn():
    eco = Station(station(), live_reading(pressure__relative=1012.0))
    forecast = HeldForecast([])
    out = got(await tools(context(eco, forecast))[0].call({}, {"fetch": False}))
    assert [a[0] for a in eco.asked] == ["held"] and eco.asked[0][1:4] == ("rows_on", ("outdoor", "pressure", "rainfall", "wind"), "5min")
    assert eco.asked[0][4:] == (NOW_TS - 3 * 3600, NOW_TS) and forecast.warmed == 0
    assert out["forecast"] == [] and out["forecast_note"] == "No forecast is available right now: the station's read is the whole answer."
    eco.asked.clear()
    await tools(context(eco))[0].call({}, {"priority": "background"})
    assert eco.asked[0][2] is False and eco.asked[2][2] is False   # (not urgent: nobody is waiting)


async def test_without_the_forecast_the_stations_read_is_the_whole_answer_and_an_old_reading_is_warned_about():
    eco = Station(station(now_ts=NOW_TS - 3600))
    out = got(await tools(context(eco))[0].call({}))
    assert "forecast" not in out and out["as_of"]["text"] == "Tue 10 Nov 2026 1:05pm"
    assert out["warning"] == "The station's newest reading is from Tue 10 Nov 1:05pm: this read may be out of date."


async def test_with_no_readings_it_is_refused_pointing_to_the_forecast():
    out = got(await tools(context(Station([]), HeldForecast([])))[0].call({}))
    assert out == {"error": "The weather station has no readings from the last 3 hours, so there is nothing to read the next few hours "
                            "from. Ask forecast for today's forecast."}


async def pinned_station(when, tmp_path, pinned_archive, monkeypatch):
    """(Ecowitt, fake) over the fake station's archive as it was at `when`, with the clock pinned there."""
    cache = pinned_archive(when)
    pin(monkeypatch, when)
    return await archived_station(tmp_path, cache)


async def test_on_the_fake_station_it_reads_the_last_3_hours_from_the_file_and_the_reading_now(tmp_path, pinned_archive, monkeypatch):
    eco, fake = await pinned_station(NOW, tmp_path, pinned_archive, monkeypatch)   # (2:05pm: the last 3 hours are all today's)
    try:
        ctx = Context(eco=eco)
        nothing = got(await tools(ctx)[0].call({}, {"fetch": False}))   # the archive holds no 5-minute readings of today yet
        assert fake.calls == [] and nothing["error"].startswith("The weather station has no readings from the last 3 hours")
        out = got(await tools(ctx)[0].call({}))
        assert {(c["path"], c.get("cycle_type")) for c in fake.calls} == {("history", "5min"), ("real_time", None)}
        fake.calls.clear()
        held = got(await tools(ctx)[0].call({}, {"fetch": False}))   # now held: nothing asked of Ecowitt
        assert fake.calls == [] and held["pressure"] == out["pressure"] and held["verdict"] == out["verdict"]
        rows = await eco.recent(3)
        last = rows[-1][1]
        assert out["pressure"]["hpa"] == round(last["pressure.relative"], 1)   # (the reading now has no pressure: the newest 5 minutes')
        assert out["moisture"]["dew_point_spread"] == 0.0 and out["moisture"]["moist"]   # (the fake's dew point is its temperature)
        assert out["verdict"] in ("possible", "likely")
        assert datetime.fromisoformat(out["as_of"]["time"]) > NOW - timedelta(minutes=10)
    finally:
        await eco.close()


async def test_just_after_midnight_the_last_3_hours_are_yesterdays_evening_read_from_the_file(tmp_path, pinned_archive, monkeypatch):
    """At 12:05am the last 3 hours are yesterday's from 9:05pm, a finished day the archive holds (up to its settled edge, 10:50pm):
    without fetching, the read is made from them (nothing asked of Ecowitt), as of its newest reading and warned about when that is
    over half an hour old; fetching adds the rest and the reading now. (By day the archive holds nothing of the last 3 hours: the
    test above.)"""
    midnight = datetime(2026, 11, 11, 0, 5, tzinfo=TZ)
    eco, fake = await pinned_station(midnight, tmp_path, pinned_archive, monkeypatch)
    try:
        ctx = Context(eco=eco)
        held = got(await tools(ctx)[0].call({}, {"fetch": False}))
        assert fake.calls == [] and "error" not in held and held["verdict"] in ("unlikely", "possible", "likely")
        newest = datetime.fromisoformat(held["as_of"]["time"])
        assert midnight - timedelta(hours=3) <= newest < midnight and newest.date() == midnight.date() - timedelta(days=1)
        assert ("warning" in held) is (midnight - newest > timedelta(minutes=30))
        out = got(await tools(ctx)[0].call({}))
        assert {(c["path"], c.get("cycle_type")) for c in fake.calls} == {("history", "5min"), ("real_time", None)}
        assert midnight - timedelta(minutes=10) < datetime.fromisoformat(out["as_of"]["time"]) <= midnight and "warning" not in out
    finally:
        await eco.close()


def test_it_is_offered_only_with_a_station_and_its_description_carries_the_rules():
    assert tools(Context(tz=TZ)) == []
    assert len(DESCRIPTION) < 1500 and PARAMETERS == {"type": "object", "properties": {}}
    for rule in ("high came before its low", "more than 3 hPa", "1005 hPa", "1020 hPa", "humidity 90%", "within 2 °C of the dew point",
                 "wind picking up", "rain rate above 0 now", "forecast", "summary", "relate"):
        assert rule in DESCRIPTION, rule
    assert set(GROUPS) == {"outdoor", "pressure", "rainfall", "rainfall_piezo", "wind"}   # (the prefetch's groups)
