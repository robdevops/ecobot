import asyncio
import logging
import time

from lib.ecowitt import Ecowitt, source
from tests.fakes import config, ecowitt_transport, home_assistant_transport


async def home_station(tmp_path, monkeypatch, both=False):
    monkeypatch.setattr(source, "LIVE_POLL_SECONDS", 0.02)
    transport, home = home_assistant_transport()
    kw = {"homeassistant_url": "http://ha", "homeassistant_token": "tok"}
    if not both:
        kw.update(ecowitt_api_key="", ecowitt_app_key="")
    eco = Ecowitt(config(tmp_path, **kw), ecowitt_transport()[0] if both else None, transport)
    await eco.start()
    return eco, home


async def test_a_new_reading_is_found_in_seconds_and_calls_the_rain_alert_each_time(tmp_path, monkeypatch):
    eco, home = await home_station(tmp_path, monkeypatch)
    seen = []

    async def alert():
        seen.append(time.time())
    eco.on_live.append(alert)
    task = asyncio.create_task(eco.watch_live())
    await asyncio.sleep(1.1)                                                                  # the fake's readings are a second apart: two of them
    task.cancel()
    assert len(seen) >= 2 and eco._live and time.time() - eco._live[0] < 1               # a reading kept for "weather now" and the report
    before = len([c for c in home.calls if c["path"] == "/api/states"])
    ts, row = await eco.live_rain()                                                        # the alert's reading: the poll's, no request of its own
    assert len([c for c in home.calls if c["path"] == "/api/states"]) == before and "rainfall.rain_rate" in row and ts
    await eco.close()


async def test_a_reading_no_newer_than_the_last_is_not_announced_again(tmp_path, monkeypatch):
    eco, home = await home_station(tmp_path, monkeypatch)
    seen = []
    eco.on_live.append(lambda: seen.append(1) or asyncio.sleep(0))
    first = await eco.fast.realtime(eco.mac, "outdoor")
    eco._seen = max(int(o["time"]) for f in first.values() for o in f.values()) + 3600    # all that follow are older than it
    task = asyncio.create_task(eco.watch_live())
    await asyncio.sleep(0.2)
    task.cancel()
    assert not seen
    await eco.close()


async def test_a_home_assistant_that_stops_answering_is_said_once_and_the_cloud_answers_the_rain(tmp_path, monkeypatch, caplog):
    eco, home = await home_station(tmp_path, monkeypatch, both=True)
    caplog.set_level(logging.INFO)
    home.off = True
    task = asyncio.create_task(eco.watch_live())
    await asyncio.sleep(0.3)
    assert caplog.text.count("Home Assistant: the ") == 1                                  # every place that asks it, one line between them
    before = eco.api.second.requests
    await eco.live_rain()                                                                  # no poll to use: the fallback's request, the cloud's
    assert eco.api.second.requests == before + 1
    home.off = False
    await asyncio.sleep(0.2)
    task.cancel()
    assert "Home Assistant is answering again" in caplog.text
    await eco.close()


async def test_a_cloud_only_station_does_not_poll(tmp_path, monkeypatch):
    eco = Ecowitt(config(tmp_path), ecowitt_transport()[0])
    assert eco.fast is None
    await asyncio.wait_for(eco.watch_live(), 1)                                            # returns at once
    await eco.close()


async def test_a_name_that_cannot_be_found_is_said_once_for_every_place_that_asks_with_a_way_out(tmp_path, monkeypatch, caplog):
    import httpx
    monkeypatch.setattr(source, "LIVE_POLL_SECONDS", 0.02)

    def nowhere(request):
        raise httpx.ConnectError("[Errno -2] Name or service not known")
    from lib import netenv
    monkeypatch.setattr(netenv, "docker_private_network", lambda: True)   # in a container on Docker's own network
    kw = {"ecowitt_api_key": "", "ecowitt_app_key": "", "homeassistant_url": "http://homeassistant.local:8123", "homeassistant_token": "tok"}
    eco = Ecowitt(config(tmp_path, **kw), None, httpx.MockTransport(nowhere))
    caplog.set_level(logging.INFO)
    task = asyncio.create_task(eco.watch_live())
    await eco.import_ranges()
    await asyncio.sleep(0.2)
    task.cancel()
    assert caplog.text.count("Home Assistant: the ") == 1                                  # ranges, minutes and live reading: one line
    assert "can't find .local names: put the IP address in HOMEASSISTANT_URL" in caplog.text and "host network" in caplog.text
    await eco.close()


def test_outside_docker_a_name_that_cannot_be_found_is_to_be_checked_or_given_as_an_address(monkeypatch, caplog):
    from lib import netenv
    from lib.ecowitt.homeassistant import Trouble
    monkeypatch.setattr(netenv, "docker_private_network", lambda: False)
    caplog.set_level(logging.INFO)
    Trouble("http://homeassistant.local:8123").failed("live reading", "[Errno -2] Name or service not known")
    assert "The name in HOMEASSISTANT_URL (http://homeassistant.local:8123) can't be found from here: check it, or use the address" in caplog.text
    assert "Docker" not in caplog.text
