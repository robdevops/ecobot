import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from lib.ecowitt import Ecowitt
from lib.ecowitt.api import EcowittAPI, EcowittError
from lib.ecowitt.fallback import FallbackAPI, newest
from lib.ecowitt.hass_api import HomeAssistantAPI, live_data
from tests.fakes import HA_PREFIX, MAC, TZ, config, ecowitt_transport, home_assistant_transport


def local_now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)


def api_for(fake=None, **kw) -> tuple[HomeAssistantAPI, object]:
    transport, fake = home_assistant_transport(fake, **kw)
    return HomeAssistantAPI("http://ha", "tok", TZ, "MAC", transport), fake


async def test_the_station_is_found_by_its_entities_names_and_says_when_it_began_recording():
    api, fake = api_for(history_days=10)
    (device,) = await api.devices()
    assert device["mac"] == "MAC" and device["name"] == "WS2900" and device["date_zone_id"] == "Australia/Melbourne"
    assert abs(device["createtime"] - fake.begins) < 70 and device["latitude"] == -37.8
    await api.close()


async def test_a_home_assistant_that_cannot_be_used_says_why_as_the_clouds_errors_do():
    transport, fake = home_assistant_transport()
    wrong = HomeAssistantAPI("http://ha", "wrong", TZ, "MAC", transport)
    with pytest.raises(EcowittError) as refused:
        await wrong.devices()
    assert not refused.value.transient and "401" in str(refused.value)                      # a refused token: not passing
    fake.off = True
    api = HomeAssistantAPI("http://ha", "tok", TZ, "MAC", transport)
    with pytest.raises(EcowittError) as down:
        await api.realtime("x", "outdoor")
    assert down.value.transient and "503" in str(down.value)                                 # it is down: passing

    def refuses(request):
        raise httpx.ConnectError("no route to host")
    gone = HomeAssistantAPI("http://ha", "tok", TZ, "MAC", httpx.MockTransport(refuses))
    with pytest.raises(EcowittError) as lost:
        await gone.realtime("x", "outdoor")
    assert lost.value.transient and "no route" in str(lost.value)
    bare = HomeAssistantAPI("http://ha", "tok", TZ, "MAC", httpx.MockTransport(lambda r: httpx.Response(200, json=[{"entity_id": "light.kitchen", "state": "on"}])))
    with pytest.raises(EcowittError, match="Ecowitt integration") as unset:
        await bare.realtime("x", "outdoor")
    assert not unset.value.transient                                                          # no Ecowitt sensors in it


async def test_the_live_reading_is_in_the_clouds_shape_and_units_whatever_units_its_owner_chose():
    api, _ = api_for(units={"outdoor_temperature": "°F", "wind_speed": "mph", "relative_pressure": "inHg"})
    live = await api.realtime("x", "outdoor,wind,pressure,rainfall")
    assert set(live) == {"outdoor", "wind", "pressure", "rainfall"}
    temperature = live["outdoor"]["temperature"]
    assert temperature["unit"] == "℃" and -5 < float(temperature["value"]) < 35 and abs(int(temperature["time"]) - datetime.now(timezone.utc).timestamp()) < 60
    assert live["pressure"]["relative"]["unit"] == "hPa" and 990 < float(live["pressure"]["relative"]["value"]) < 1030   # inHg back to hPa
    assert live["wind"]["wind_speed"]["unit"] == "km/h" and 0 <= float(live["wind"]["wind_speed"]["value"]) <= 25
    assert live["rainfall"]["rain_rate"]["unit"] == "mm/hr" and "light" not in str(live)
    assert "humidity" not in live["wind"] and set(await api.realtime("x", "indoor")) == {"indoor"}                    # only the groups asked for
    await api.close()


def test_a_state_that_is_not_a_number_is_left_out_and_the_time_is_when_the_gateway_last_said_it():
    states = [{"entity_id": HA_PREFIX + "outdoor_temperature", "state": "unavailable", "last_updated": "2026-10-27T01:00:00+00:00"},
              {"entity_id": HA_PREFIX + "humidity", "state": "71", "last_changed": "2026-10-27T01:00:00+00:00", "last_updated": "2026-10-27T01:00:05+00:00",
               "last_reported": "2026-10-27T01:02:00+00:00"},
              {"entity_id": HA_PREFIX + "dewpoint", "state": "9.5", "last_updated": "2026-10-27T01:00:05+00:00"},
              {"entity_id": "sensor.other_humidity", "state": "10", "last_updated": "2026-10-27T01:00:05+00:00"}]
    got = live_data(states, HA_PREFIX, {"outdoor"})
    assert set(got["outdoor"]) == {"humidity", "dew_point"}
    assert got["outdoor"]["humidity"] == {"time": "1793062920", "unit": "%", "value": "71"}                               # last_reported, when there is one
    assert got["outdoor"]["dew_point"]["time"] == "1793062805"                                                            # else last_updated


async def test_a_history_is_cut_in_the_clouds_buckets_and_asks_home_assistant_a_day_at_a_time():
    api, fake = api_for(history_days=10)
    await api.devices()
    fake.calls.clear()
    now = local_now()
    data = await api.history("x", "30min", now - timedelta(days=3), now, "outdoor,wind")
    stamps = [int(t) for t in data["outdoor"]["temperature"]["list"]]
    assert stamps == sorted(stamps) and all(t % 1800 == 0 for t in stamps) and 140 <= len(stamps) <= 146        # three days of half hours
    assert {"temperature", "temperature_low", "temperature_high", "humidity", "dew_point"} <= set(data["outdoor"]) and set(data) == {"outdoor", "wind"}
    asked = [c for c in fake.calls if c["path"].startswith("/api/history")]
    assert 3 <= len(asked) <= 5 and all("sensor.ws2900_rain_rate" not in c["filter_entity_id"] for c in asked)         # only the groups asked for
    daily = await api.history("x", "1day", now - timedelta(days=4), now, "outdoor")
    assert all(int(t) % 86400 == 0 for t in daily["outdoor"]["temperature"]["list"])                              # UTC days, as the cloud's
    await api.close()


async def test_nothing_is_asked_of_home_assistant_for_a_time_before_it_began_recording():
    api, fake = api_for(history_days=2)
    await api.devices()
    fake.calls.clear()
    now = local_now()
    assert await api.history("x", "5min", now - timedelta(days=60), now - timedelta(days=30), "outdoor") == {}
    assert not fake.calls                                                                                           # no request at all
    got = await api.history("x", "1day", now - timedelta(days=400), now, "outdoor")                                # a year of days, two of them told
    assert len(got["outdoor"]["temperature"]["list"]) <= 4 and len([c for c in fake.calls if c["path"].startswith("/api/history")]) <= 3
    await api.close()


async def cloud_and_home(tmp_path, home_days=10, cloud_days=60, **fakes):
    cloud_transport, cloud = ecowitt_transport(history_days=cloud_days)
    home_transport, home = home_assistant_transport(history_days=home_days, **fakes)
    return cloud_transport, cloud, home_transport, home


async def test_the_cloud_makes_up_what_home_assistant_lacks_and_home_assistants_buckets_are_kept(tmp_path):
    cloud_transport, cloud, home_transport, home = await cloud_and_home(tmp_path)
    eco = Ecowitt(config(tmp_path, homeassistant_url="http://ha", homeassistant_token="tok"), cloud_transport, home_transport)
    await eco.start()
    assert isinstance(eco.api, FallbackAPI) and "cloud fallback" in eco.via and eco.mac == MAC                  # the cloud names the station
    now = local_now()
    cloud.calls.clear()
    data = await eco.api.history(eco.mac, "30min", now - timedelta(days=20), now - timedelta(days=1), "outdoor")
    stamps = sorted(int(t) for t in data["outdoor"]["temperature"]["list"])
    assert stamps[0] <= int((now - timedelta(days=19, hours=23)).replace(tzinfo=TZ).timestamp()) and stamps[-1] > int((now - timedelta(days=1, hours=2)).replace(tzinfo=TZ).timestamp())
    assert len(stamps) > 900 and len(set(stamps)) == len(stamps)                                                         # the whole stretch, once each
    assert len([c for c in cloud.calls if c["path"] == "history"]) == 1                                                  # one request, for the part before Home Assistant
    asked = [(c["start_date"], c["end_date"]) for c in cloud.calls if c["path"] == "history"][0]
    assert asked[1] < (now - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")                                             # ending where Home Assistant's begin
    cloud.calls.clear()
    recent = await eco.api.history(eco.mac, "5min", now - timedelta(hours=5), now, "outdoor")
    assert len(recent["outdoor"]["temperature"]["list"]) > 50 and not [c for c in cloud.calls if c["path"] == "history"]      # all Home Assistant's: the cloud is not asked
    cloud.calls.clear()
    minutes = await eco.api.history(eco.mac, "1min", now - timedelta(hours=2), now, "outdoor")
    assert len(minutes["outdoor"]["temperature"]["list"]) > 100 and not cloud.calls                                       # the cloud has no minute cycle
    await eco.close()


async def test_a_home_assistant_that_is_down_leaves_the_cloud_to_answer_and_is_said_once(tmp_path, caplog):
    cloud_transport, cloud, home_transport, home = await cloud_and_home(tmp_path)
    eco = Ecowitt(config(tmp_path, homeassistant_url="http://ha", homeassistant_token="tok"), cloud_transport, home_transport)
    await eco.start()
    home.off = True
    now = local_now()
    caplog.set_level(logging.INFO)
    for _ in range(3):
        data = await eco.api.history(eco.mac, "5min", now - timedelta(hours=3), now, "outdoor")
        assert len(data["outdoor"]["temperature"]["list"]) > 20                                                           # the cloud's
        assert (await eco.api.realtime(eco.mac, "outdoor"))["outdoor"]["temperature"]["value"] == "12.3"                  # the cloud's live reading
    assert caplog.text.count("Home Assistant: ") == 1 and caplog.text.count("failed (") == 1                              # one outage, said once
    home.off = False
    await eco.api.realtime(eco.mac, "outdoor")
    assert "Home Assistant is answering again" in caplog.text
    await eco.close()


async def test_a_stale_home_assistant_reading_gives_way_to_a_fresher_cloud_one(tmp_path):
    cloud_transport, cloud, home_transport, home = await cloud_and_home(tmp_path)
    eco = Ecowitt(config(tmp_path, homeassistant_url="http://ha", homeassistant_token="tok"), cloud_transport, home_transport)
    await eco.start()
    fresh = await eco.api.realtime(eco.mac, "outdoor")
    assert fresh["outdoor"]["temperature"]["value"] != "12.3"                                                            # Home Assistant's, it is young
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    original = home.__call__

    def stale(request):
        reply = original(request)
        if request.url.path == "/api/states":
            states = reply.json()
            for s in states:
                s["last_updated"] = old
            return httpx.Response(200, json=states)
        return reply
    eco.api.first.ha.client = httpx.AsyncClient(base_url="http://ha", headers={"Authorization": "Bearer tok"}, transport=httpx.MockTransport(stale))
    assert (await eco.api.realtime(eco.mac, "outdoor"))["outdoor"]["temperature"]["value"] == "12.3"                    # two hours old: the cloud's newer one
    assert newest({"a": {"b": {"time": "5"}}, "c": {"d": {"time": "9"}, "e": {}}}) == 9 and newest({}) == 0
    await eco.close()


async def test_the_cloud_is_asked_only_when_both_are_set_and_only_home_assistant_when_the_cloud_is_not(tmp_path):
    for kwargs, kind, cloud_set, home_set in (
            ({}, "cloud", True, False),
            ({"homeassistant_url": "http://ha", "homeassistant_token": "tok"}, "both", True, True),
            ({"ecowitt_api_key": "", "ecowitt_app_key": "", "homeassistant_url": "http://ha", "homeassistant_token": "tok"}, "home", False, True),
            ({"ecowitt_source": "cloud", "homeassistant_url": "http://ha", "homeassistant_token": "tok"}, "cloud", True, False),
            ({"ecowitt_source": "homeassistant", "homeassistant_url": "http://ha", "homeassistant_token": "tok"}, "home", False, True)):
        cfg = config(tmp_path, **kwargs)
        assert (cfg.ecowitt_cloud, cfg.ecowitt_home_assistant, cfg.ecowitt) == (cloud_set, home_set, True), kwargs
        eco = Ecowitt(cfg, ecowitt_transport()[0], home_assistant_transport()[0])
        assert {FallbackAPI: "both", HomeAssistantAPI: "home", EcowittAPI: "cloud"}[type(eco.api)] == kind, kwargs
        await eco.close()
    assert not config(tmp_path, ecowitt_api_key="", ecowitt_app_key="").ecowitt                                          # neither: no station
    assert not config(tmp_path, ecowitt_source="homeassistant").ecowitt
