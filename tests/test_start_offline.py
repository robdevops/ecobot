"""ecobot-mcp opens its port without waiting on any server (step 3 of making it start quicker): each source starts from what the data
folder kept (the station's device and live reading, the air sensor's reading, the last forecast and pollen page), the tools answer from
it as of its time until the fresh readings come in, which the clients are then told of; a first start answers "not in yet"; the
station's device list, asked for in the background, is asked again until it answers."""

import asyncio
import json
import time
from datetime import datetime

import httpx
import pytest

from ecobot_mcp import core
from lib.airgradient import AirGradient
from lib.ecowitt import Ecowitt, source as eco_source
from lib.ecowitt.fallback import FallbackAPI
from lib.ecowitt.homeassistant import HomeAssistant
from lib.mcp_server import WEATHER_URI, EcobotServer
from lib.service import current as cur
from lib.service.alerts import AlertRules
from lib.service.context import Context
from lib.service.current import CurrentTool
from lib.warm import Tasks
from tests.fakes import air_transport, config, ecowitt_transport
from tests.test_airgradient_local import DEVICE, URL, sensor
from tests.test_forecast import transport as forecast_transport
from tests.test_pollen import page, transport as pollen_transport


async def until(check, seconds: float = 2):
    for _ in range(int(seconds / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never came")


def hanging(calls: list):
    """A server that never answers (each request recorded): a start that waited on it would never open the port."""
    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        await asyncio.Event().wait()
    return httpx.MockTransport(handler)


INITS = {cls: cls.__init__ for cls in (core.Ecowitt, core.AirGradient, core.Forecast, core.Pollen)}


def wire(monkeypatch, eco_t, air_t, forecast_t=None, pollen_t=None):
    """The sources start_sources makes, on these transports."""
    for cls, t in ((core.Ecowitt, eco_t), (core.AirGradient, air_t)):
        monkeypatch.setattr(cls, "__init__", lambda self, c, transport=None, orig=INITS[cls], t=t, **kw: orig(self, c, t, **kw))
    if forecast_t is not None:
        monkeypatch.setattr(core.Forecast, "__init__", lambda self, c, place=None: INITS[core.Forecast](self, c, place, forecast_t))
    if pollen_t is not None:
        monkeypatch.setattr(core.Pollen, "__init__", lambda self, c: INITS[core.Pollen](self, c, pollen_t))


async def current(eco, air=None, **meta) -> dict:
    return json.loads(await CurrentTool(Context(eco=eco, air=air)).tool().call({}, meta or None))


async def close(sources):
    for s in sources:
        await s.close()


async def test_a_first_start_asks_no_server_and_says_the_station_is_not_in_yet(tmp_path, monkeypatch):
    """Every server hangs: the sources still start at once, having asked nothing, the station not found yet (the configured zone)
    and "not in yet" said of it; the forecast waits for the station's place rather than being left out."""
    calls = []
    wire(monkeypatch, hanging(calls), hanging(calls), hanging(calls), hanging(calls))
    cfg = config(tmp_path, pollen=True, forecast=True)
    sources, cfg = await asyncio.wait_for(core.start_sources(cfg), 2)
    try:
        assert calls == [] and [s.name for s in sources] == ["Ecowitt", "AirGradient", "Pollen", "Forecast"]
        eco = sources[0]
        assert eco.mac == "" and not eco.listed.is_set() and eco.summary.startswith("Ecowitt: not found yet")
        assert sources[3].location is None and sources[3].summary == "Open-Meteo: none kept yet (every 900s)"
        answer = await asyncio.wait_for(current(eco), 2)
        assert answer["unavailable"][cur.STATION] == cur.NOT_IN_YET and calls == []
        assert eco.fetcher(["outdoor"]).mac == "" and await eco.fetcher(["outdoor"])._fetch("5min", datetime.now(), datetime.now(), ["outdoor"]) is None
        assert calls == []   # (a history request is refused before it is sent: "not in yet")
    finally:
        await close(sources)


async def test_a_failed_device_list_is_asked_again_in_the_background_until_the_station_is_found_and_kept(tmp_path, monkeypatch, caplog):
    """A first start whose device list fails (the network down) keeps asking, less and less often; once it answers the station is
    found (its address, name, place), kept in the file, and the next start finds it there without asking."""
    monkeypatch.setattr(eco_source, "DEVICE_RETRY_SECONDS", (0.01, 0.02))
    caplog.set_level("INFO")
    transport, _ = ecowitt_transport(history_days=3)
    eco = Ecowitt(config(tmp_path), transport)
    await eco.start(wait=False)
    real, failures = eco.api.devices, [2]

    async def devices():
        if failures[0]:
            failures[0] -= 1
            raise httpx.ConnectError("the network is down")
        return await real()
    eco.api.devices = devices
    await asyncio.wait_for(eco.refresh_device(), 2)
    assert eco.listed.is_set() and eco.mac and eco.location == (-37.8, 145.0) and failures == [0]
    assert caplog.text.count("the device list failed") == 1 and "the device list answers again" in caplog.text
    mac = eco.mac
    await eco.close()
    calls = []
    again = Ecowitt(config(tmp_path), hanging(calls))
    await again.start(wait=False)
    assert again.mac == mac and again.listed.is_set() and str(again.tz) == "Australia/Melbourne" and calls == []
    await again.close()


async def test_a_restart_answers_from_the_kept_readings_as_of_their_time_and_the_clients_are_told_of_the_fresh_ones(tmp_path, monkeypatch):
    """The first run keeps the station's device, its live reading and the air sensor's reading in the file. The restart, every server
    hanging, starts from them having asked nothing: `current` (and the dashboard's, with fetch off) answers at once from them, as of
    their time, the live reading being asked for meanwhile; when fresh readings come in, the pushed resources change."""
    eco_t, _ = ecowitt_transport(history_days=3)
    air_t, _ = air_transport()
    wire(monkeypatch, eco_t, air_t)
    cfg = config(tmp_path)
    sources, cfg = await core.start_sources(cfg)
    eco, air = sources
    await eco.refresh_device()
    await eco.first_live()
    await air.settle()
    kept_station, kept_air = eco._live, air._current
    assert kept_station and kept_air
    await close(sources)

    calls = []
    wire(monkeypatch, hanging(calls), hanging(calls))
    sources, cfg = await asyncio.wait_for(core.start_sources(cfg), 2)
    eco, air = sources
    try:
        assert calls == [] and eco.mac and str(cfg.tz) == "Australia/Melbourne"
        assert eco._live == (pytest.approx(kept_station[0]), kept_station[1]) and air._current[0] == kept_air[0]
        eco._live = (eco._live[0] - 600, eco._live[1])              # (kept ten minutes before the restart: no longer fresh)
        held = await current(eco, air, fetch=False)                  # the dashboard's
        asked = await asyncio.wait_for(current(eco, air), 2)          # a client's, with fetch on: not waiting on the hung servers
        assert asked["sensors"] == held["sensors"] and asked["as_of"] == held["as_of"] and "unavailable" not in asked
        await until(lambda: any(c.endswith("real_time") for c in calls))                   # ... the live reading asked for in the background
        server = EcobotServer(Context(eco=eco, air=air))
        assert await server.publish_changes() == []                  # (the kept readings: what a client reads now)
        newer = json.loads(json.dumps(eco._live[1]))
        newer["outdoor"]["temperature"] = {**newer["outdoor"]["temperature"], "value": "31.5", "time": str(int(time.time()))}
        eco._got_live(newer)                                          # the fresh reading comes in
        assert await server.publish_changes() == [WEATHER_URI]
        assert (await current(eco, air))["sensors"]["outdoor"]["temperature"]["value"] == 31.5
    finally:
        await close(sources)


async def test_the_air_sensor_starts_from_its_kept_reading_and_reads_the_sensor_in_the_warm_up(tmp_path):
    transport, calls = air_transport()
    air = AirGradient(config(tmp_path, airgradient_local_url=URL), transport=transport)
    air.local, _ = sensor(lambda request: httpx.Response(200, json=DEVICE))
    await air.start()                                                 # (wait: as before, the sensor read at once)
    assert air._current and air._fresh
    kept = air._current
    await air.close()
    again = AirGradient(config(tmp_path, airgradient_local_url=URL), transport=transport)
    asked = []
    again.local, _ = sensor(lambda request: asked.append(request) or httpx.Response(200, json=DEVICE))
    calls.clear()
    await again.start(wait=False)
    assert asked == [] and calls == [] and again._current == (kept[0], pytest.approx(kept[1])) and not again._fresh
    latest = await again.latest(kept=True)                            # a question just after the start: the kept reading, nothing asked
    assert latest.reading["pm2_5"]["value"] == 2.5 and asked == [] and calls == []
    await again.settle()
    assert len(asked) == 1 and again._fresh
    await again.close()


async def test_the_particulates_alert_starts_once_the_air_sensor_has_been_read(tmp_path):
    transport, _ = air_transport()
    air = AirGradient(config(tmp_path), transport=transport)
    rules = AlertRules({"AirGradient": air}, store=None, alerts=None)
    tasks = Tasks()
    running = rules.start(tasks)
    assert running == [] and not tasks.running
    rules.start_air(tasks, running)
    assert running == ["particulates"] and len(tasks.running) == 1
    await tasks.close()
    await air.close()


async def test_home_assistants_states_are_asked_once_for_everyone_wanting_them_at_about_the_same_moment():
    asked, now = [], [100.0]

    async def handler(request):
        asked.append(request.url.path)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=[{"entity_id": "sensor.x", "state": "1", "attributes": {"unit_of_measurement": "°C"}}])
    ha = HomeAssistant("http://ha", "tok", httpx.MockTransport(handler), clock=lambda: now[0])
    first, second = await asyncio.gather(ha.states(), ha.states())    # one request out: both await it
    assert first == second and asked == ["/api/states"] and ha.units == {"sensor.x": "°C"}
    now[0] += 1
    await ha.states()                                                 # a few seconds old: shared
    now[0] += 10
    await ha.states()                                                 # older: asked again
    assert asked == ["/api/states"] * 2
    await ha.close()


async def test_the_clouds_and_home_assistants_device_lists_are_asked_together():
    class Slow:
        def __init__(self, name):
            self.name = name

        async def devices(self):
            await asyncio.sleep(0.2)
            return [{"mac": self.name}]
    api = FallbackAPI(Slow("ha"), Slow("cloud"), None)
    started = time.monotonic()
    assert await api.devices() == [{"mac": "cloud"}] and time.monotonic() - started < 0.35

