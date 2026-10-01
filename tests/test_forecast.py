import json
from datetime import datetime, time as dtime, timedelta, timezone

import httpx
import pytest

from lib.forecast import Forecast, decorate, forecast_emoji, geohash
from lib.forecast.source import describe_day
from tests.fakes import TZ, config


def local_iso(day):
    """BOM dates are the start of the local day, in UTC."""
    return datetime.combine(day, dtime(0, 0), TZ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bom_days():
    today = datetime.now(TZ).date()
    return [{"date": local_iso(today), "temp_min": 11, "temp_max": 17, "short_text": "Rain.", "rain": {"chance": 90}},
            {"date": local_iso(today + timedelta(days=1)), "temp_min": 11, "temp_max": 18,
             "short_text": "Showers. Possible storm.", "rain": {"chance": 95}},
            {"date": local_iso(today + timedelta(days=2)), "temp_min": 9, "temp_max": None, "short_text": "Mostly sunny.", "rain": {}}]


def transport(calls, bom_ok=True):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "api.weather.bom.gov.au":
            if not bom_ok:
                return httpx.Response(503)
            if request.url.path.endswith("/forecasts/daily"):
                return httpx.Response(200, json={"data": bom_days()})
            return httpx.Response(200, json={"data": {"name": "Testville"}})
        today = datetime.now(TZ).date()
        return httpx.Response(200, json={"daily": {
            "time": [str(today), str(today + timedelta(days=1))], "temperature_2m_min": [10.5, 12], "temperature_2m_max": [16, 19.5],
            "precipitation_probability_max": [40, 0], "weather_code": [63, 0]}})
    return httpx.MockTransport(handler)


def make(tmp_path, bom_ok=True, hour=12, **over):
    calls = []
    forecast = Forecast(config(tmp_path, forecast=True, forecast_lat=-37.8, forecast_lon=144.9, **over), None, transport(calls, bom_ok))
    forecast.now = lambda: datetime.combine(datetime.now(TZ).date(), dtime(hour, 0))
    return forecast, calls


def test_geohash_matches_a_known_value():
    assert geohash(57.64911, 10.40744, 11) == "u4pruydqqvj" and geohash(57.64911, 10.40744, 7) == "u4pruyd"


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
    assert describe_day({"summary": "Sunny.", "min_c": 9.5, "max_c": None, "rain_chance_pct": None}) == "☀️ Sunny. min 9.5°C"
    assert describe_day({}) == "no data"


async def test_today_and_tomorrow_come_from_the_bom_by_local_date(tmp_path):
    forecast, calls = make(tmp_path)
    await forecast.start()
    assert forecast.lines() == ["Today: 🌧️ Rain. 11–17°C, 90% chance of rain",
                                "Tomorrow: 🌦️ Showers. ⛈️ Possible storm. 11–18°C, 95% chance of rain"]
    out = json.loads(await forecast.handle({"days": 3}))
    assert len(out["lines"]) == 3 and out["lines"][2].endswith(": 🌤️ Mostly sunny. min 9°C") and "Testville" in out["source"]
    assert set(calls) == {"api.weather.bom.gov.au"}
    await forecast.close()


async def test_when_the_bom_fails_open_meteo_answers(tmp_path):
    forecast, calls = make(tmp_path, bom_ok=False)
    await forecast.start()
    assert forecast.lines()[0] == "Today: 🌧️ Rain. 10.5–16°C, 40% chance of rain"
    assert forecast.lines()[1].startswith("Tomorrow: ☀️ Clear. 12–19.5°C, 0% chance of rain")
    assert "Open-Meteo" in json.loads(await forecast.handle({}))["source"] and "api.open-meteo.com" in set(calls)
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
