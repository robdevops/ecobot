import json
from datetime import datetime, time as dtime, timedelta

import httpx
import pytest

from lib.forecast import Forecast, decorate, forecast_emoji
from lib.forecast.source import day_label, describe_day, temps
from tests.fakes import TZ, config


def transport(calls, ok=True):
    """Open-Meteo: three days from today."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if not ok:
            return httpx.Response(503)
        today = datetime.now(TZ).date()
        return httpx.Response(200, json={"daily": {
            "time": [str(today + timedelta(days=n)) for n in range(3)], "temperature_2m_min": [10.5, 12, 9],
            "temperature_2m_max": [16, 19.5, 14.2], "precipitation_probability_max": [40, 0, 100], "weather_code": [63, 0, 95]}})
    return httpx.MockTransport(handler)


def make(tmp_path, ok=True, hour=12, **over):
    calls = []
    forecast = Forecast(config(tmp_path, forecast=True, forecast_lat=-37.8, forecast_lon=144.9, **over), None, transport(calls, ok))
    forecast.now = lambda: datetime.combine(datetime.now(TZ).date(), dtime(hour, 0))
    return forecast, calls


def test_each_sentence_gets_its_own_emoji_and_a_full_stop():
    assert decorate("Showers. Possible storm.") == "🌦️ Showers. ⛈️ Possible storm."
    assert decorate("Rain") == "🌧️ Rain." and decorate("Partly cloudy.") == "⛅ Partly cloudy." and decorate("") == ""
    assert decorate("Windy. Frost.") == "🌬️ Windy. 🥶 Frost." and decorate("Cool change") == "Cool change."
    assert [forecast_emoji(s) for s in ("Mostly sunny", "Sunny", "Fog", "Overcast", "Hail", "Possible thunderstorm")] == \
        ["🌤️", "☀️", "🌫️", "☁️", "🌨️", "⛈️"]


def test_a_day_reads_like_the_report_line():
    assert describe_day({"summary": "Rain.", "min_c": 11, "max_c": 17, "rain_chance_pct": 90}) == "🌧️ Rain. 11–17°C, 90% chance of rain"
    assert describe_day({"summary": "Showers. Possible storm.", "min_c": 11, "max_c": 18, "rain_chance_pct": 95}) == \
        "🌦️ Showers. ⛈️ Possible storm. 11–18°C, 95% chance of rain"
    assert describe_day({"summary": "Sunny.", "min_c": 9.5, "max_c": None, "rain_chance_pct": None}) == "☀️ Sunny. min 10°C"
    assert describe_day({}) == "no data"


def test_temperatures_are_whole_degrees_with_halves_rounded_up():
    assert temps({"min_c": 10.5, "max_c": 19.5}) == "11–20°C" and temps({"min_c": -1.5, "max_c": -0.5}) == "-1–0°C"
    assert temps({"min_c": 12.4, "max_c": 16.6}) == "12–17°C" and temps({"max_c": 20.5}) == "max 21°C" and temps({}) is None


def test_days_after_today_are_named_by_their_weekday():
    today = datetime(2026, 10, 1).date()               # a Thursday
    assert [day_label(today + timedelta(days=n), today) for n in range(4)] == ["Today", "Friday", "Saturday", "Sunday"]


async def test_today_and_the_next_days_come_from_open_meteo_by_local_date(tmp_path):
    forecast, calls = make(tmp_path)
    await forecast.start()
    today = datetime.now(TZ).date()
    assert forecast.lines() == ["Today: 🌧️ Rain. 11–16°C, 40% chance of rain",
                                f"{today + timedelta(days=1):%A}: ☀️ Clear. 12–20°C, 0% chance of rain"]
    out = json.loads(await forecast.handle({"days": 3}))
    assert len(out["lines"]) == 3 and out["lines"][2] == f"{today + timedelta(days=2):%A}: ⛈️ Thunderstorm. 9–14°C, 100% chance of rain"
    assert out["place"] == "Melbourne" and out["source"] == "Open-Meteo" and "Tomorrow" not in " ".join(out["lines"])
    assert "tag" not in out and set(calls) == {"api.open-meteo.com"}
    await forecast.close()


async def test_the_place_is_the_configured_name(tmp_path):
    forecast, _ = make(tmp_path, place="Geelong")
    await forecast.start()
    assert json.loads(await forecast.handle({}))["place"] == "Geelong"
    await forecast.close()


async def test_the_forecast_is_fetched_only_in_the_day_and_not_again_while_young(tmp_path):
    forecast, calls = make(tmp_path, hour=22)
    await forecast.start()
    n = len(calls)
    assert n > 0
    await forecast.warm(True)
    await forecast.warm(False)
    assert len(calls) == n                                  # night: the cached forecast is used
    forecast.now = lambda: datetime.combine(datetime.now(TZ).date(), dtime(9, 0))
    await forecast.warm(False)
    assert len(calls) == n                                  # young enough for a question
    await forecast.warm(True)
    assert len(calls) > n                                   # the hourly timer
    await forecast.close()


async def test_the_station_location_is_used_when_none_is_configured_and_without_one_the_source_does_not_start(tmp_path):
    cfg = config(tmp_path, forecast=True)
    assert Forecast(cfg, (-37.8, 144.9)).location == (-37.8, 144.9)
    with pytest.raises(RuntimeError, match="FORECAST_LAT"):
        await Forecast(cfg).start()


async def test_a_direct_forecast_question_shows_the_whole_week_unless_fewer_days_are_asked_for(tmp_path):
    forecast, _ = make(tmp_path)
    await forecast.start()
    assert len(json.loads(await forecast.handle({}))["lines"]) == 3          # every day Open-Meteo gave (the test data has three)
    assert len(json.loads(await forecast.handle({"days": 2}))["lines"]) == 2
    assert len(json.loads(await forecast.handle({"days": "nonsense"}))["lines"]) == 3
    assert len(json.loads(await forecast.handle({"days": 30}))["lines"]) == 3
    await forecast.close()
