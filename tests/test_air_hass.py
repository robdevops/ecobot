"""The air sensor's ranges read back from Home Assistant: its entities found by name, its states told as 5-minute lows and highs, and only
those kept whose average agrees with the bot's reading."""
import logging
import time
from datetime import datetime, timezone

import httpx

from lib.airgradient import AirGradient
from lib.airgradient.homeassistant import checked, entity_ids, find_prefix, ranges
from lib.ecowitt.homeassistant import HomeAssistant
from lib.timeutil import naive_local
from tests.fakes import air_transport, config

PREFIX = "sensor.open_air_"


def state(entity, value, unit=""):
    return {"entity_id": entity, "state": str(value), "attributes": {"unit_of_measurement": unit} if unit else {}}


def test_the_sensor_is_found_by_its_index_entities_and_with_several_the_one_whose_reading_is_nearest():
    states = [state("sensor.ws2900_outdoor_temperature", 18), state(PREFIX + "voc_index", 140), state(PREFIX + "pm2_5", 6, "µg/m³"),
              state("sensor.kitchen_voc_index", 300), state("light.kitchen", "on")]
    assert find_prefix(states, {"voc_index": 150}) == (PREFIX, 2)
    assert find_prefix(states, {"voc_index": 290}) == ("sensor.kitchen_", 2)
    assert find_prefix([state("sensor.ws2900_humidity", 60)]) == (None, 0)
    assert entity_ids(PREFIX, states) == {PREFIX + "voc_index": "voc_index", PREFIX + "pm2_5": "pm2_5"}   # only what it has


def test_states_become_each_five_minutes_mean_lowest_and_highest_and_a_bucket_still_going_waits():
    t0 = 1_793_000_100 // 300 * 300
    history = {PREFIX + "voc_index": [(t0 - 30, 100.0), (t0 + 60, 120.0), (t0 + 240, 90.0), (t0 + 360, 110.0)],
               PREFIX + "temperature": [(t0 - 30, 68.0)]}
    ids = {PREFIX + "voc_index": "voc_index", PREFIX + "temperature": "air_temperature"}
    got = ranges(history, ids, {PREFIX + "temperature": "°F"}, t0, t0 + 599, now=t0 + 450)
    mean, lo, hi = got["voc_index"][t0]
    assert (lo, hi) == (90.0, 120.0) and abs(mean - (100 * 60 + 120 * 180 + 90 * 60) / 300) < 1e-9
    assert t0 + 300 not in got["voc_index"]                                         # still going at now: left for the next time
    assert got["air_temperature"][t0] == (20.0, 20.0, 20.0)                          # 68°F in the bot's °C; it never changed


def test_only_agreeing_buckets_of_agreeing_metrics_are_kept_and_the_bots_own_range_stays():
    rows = [{"ts": t * 300, "voc_index": 100.0, "pm2_5": 5.0, **({"_range": {"voc_index": (95, 105)}} if t == 0 else {})} for t in range(20)]
    found = {"voc_index": {t * 300: (100.0 + (30 if t == 5 else 2), 90.0, 115.0) for t in range(20)},   # one five minutes is off
             "pm2_5": {t * 300: (11.0, 9.0, 13.0) for t in range(20)},                                 # raw PM2.5: twice the reading
             "co2": {t * 300: (450.0, 440.0, 460.0) for t in range(20)}}                              # no reading to compare: no line
    keep, verdict = checked(found, rows)
    assert set(keep["voc_index"]) == {t * 300 for t in range(20)} - {0, 1500}         # the bot's own and the one that is off are not
    assert "pm2_5" not in keep and verdict["pm2_5"] == (20, 0) and verdict["voc_index"] == (20, 19)
    assert keep["co2"] == {} and verdict["co2"] == (0, 0)


class FakeHA:
    """Home Assistant with an AirGradient sensor: a VOC index that wanders 20 either side of the cloud's 100 within each five minutes, and a
    PM2.5 that is the raw reading, twice the cloud's corrected one."""

    def __init__(self, voc=True):
        self.voc, self.calls, self.off = voc, [], False
        self.live = lambda: [state(PREFIX + "voc_index", 100), state(PREFIX + "pm2_5", 24, "µg/m³")]   # its states now

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if self.off:
            return httpx.Response(503)
        if request.url.path == "/api/states":
            return httpx.Response(200, json=self.live() if self.voc else [state("sensor.ws2900_outdoor_temperature", 18)])
        start = datetime.fromisoformat(request.url.path.rsplit("/", 1)[-1]).timestamp()
        end = min(datetime.fromisoformat(request.url.params["end_time"]).timestamp(), time.time())
        out = []
        for entity in request.url.params["filter_entity_id"].split(","):
            rows = []
            for t in range(int(start) // 60 * 60, int(end), 60):
                value = 100 + (20 if t // 60 % 2 else -20) if entity.endswith("voc_index") else 2 * (5 + datetime.fromtimestamp(t, timezone.utc).hour % 12)
                rows.append({"entity_id": entity, "state": str(value), "last_changed": datetime.fromtimestamp(t, timezone.utc).isoformat()})
            out.append(rows)
        return httpx.Response(200, json=out)


async def air_with(tmp_path, fake):
    transport, _ = air_transport(rows_per_day=288)
    air = AirGradient(config(tmp_path), transport=transport, ha=HomeAssistant("http://ha", "tok", httpx.MockTransport(fake)))
    await air.start()
    return air


async def test_the_voc_index_gets_home_assistants_ranges_and_the_raw_pm25_none_said_once(tmp_path, caplog):
    fake = FakeHA()
    air = await air_with(tmp_path, fake)
    caplog.set_level(logging.INFO)
    await air.import_history()
    now = time.time()
    rows, _, _ = await air.rows(*(naive_local(t, air.tz) for t in (now - 36 * 3600, now - 600)))   # (a finished day among them: checked against the cloud)
    with_range = [r for r in rows if "voc_index" in r.get("_range", {})]
    assert len(with_range) > 50 and all(r["_range"]["voc_index"] == (80.0, 120.0) for r in with_range)
    line = air._chart(["voc_index"], rows, "today").panels[0].lines[0]
    assert line.low is not None and min(line.low) == 80.0 and max(line.high) == 120.0                 # the chart is shaded from them
    assert "Home Assistant (AirGradient): " in caplog.text
    # Home Assistant is the first source here, so its raw PM2.5 is the one kept for its days, and the cloud's (twice as different) fills no
    # gaps of it: said once, with the fix. Its other metrics (CO2 ...) fill the cells Home Assistant has nothing for
    raw = lambda ts: 2 * (5 + datetime.fromtimestamp(ts, timezone.utc).hour % 12)
    assert all(r["pm2_5"] == raw(r["ts"]) and r["co2"] == 450 for r in rows)
    assert caplog.text.count("the cloud's PM2.5 isn't the reading the bot keeps") == 1 and "set the monitor's PM2.5 correction" in caplog.text
    short, _, _ = await air.rows(*(naive_local(t, air.tz) for t in (now - 6 * 3600, now - 600)))   # a few hours: Home Assistant's minutes
    assert len(short) > 300 and {r["voc_index"] for r in short} == {80.0, 120.0} and not any("_range" in r for r in short)
    assert all(r["pm2_5"] == raw(r["ts"]) for r in short)
    await air.import_history()                                                         # the next time: the last hours, nothing said again
    await air.rows(*(naive_local(t, air.tz) for t in (now - 60 * 3600, now - 600)))   # (another day checked against the cloud)
    assert caplog.text.count("isn't the reading the bot keeps") == 1 and caplog.text.count("Home Assistant (AirGradient)") == 1
    await air.close()


async def test_no_airgradient_in_home_assistant_is_said_once_and_a_failing_one_is_said_once_too(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    fake = FakeHA(voc=False)
    air = await air_with(tmp_path, fake)                                              # (the monitor is looked for at start)
    await air.import_history()
    await air.import_history()
    assert caplog.text.count("Home Assistant has no AirGradient monitor") == 1 and not [c for c in fake.calls if c.startswith("/api/history")]
    fake.off = True
    await air.import_history()
    await air.import_history()
    assert caplog.text.count("Home Assistant: the history of the air monitor failed") == 1
    fake.off = False
    await air.import_history()
    assert "Home Assistant is answering again" in caplog.text
    await air.close()


async def test_without_home_assistant_there_is_nothing_to_import(tmp_path):
    transport, _ = air_transport()
    air = AirGradient(config(tmp_path), transport=transport)
    assert air.ha is None and await air.import_history() is None
    await air.close()


async def test_the_air_sensor_shares_the_weather_stations_home_assistant_and_its_ranges_are_imported_on_a_schedule(tmp_path, monkeypatch):
    import ecobot
    from lib.ecowitt import Ecowitt
    from tests.fakes import ecowitt_transport, home_assistant_transport
    cfg = config(tmp_path, homeassistant_url="http://ha", homeassistant_token="tok")
    eco_t, _ = ecowitt_transport()
    ha_t, _ = home_assistant_transport()
    air_t, _ = air_transport()
    eco_init, air_init = Ecowitt.__init__, AirGradient.__init__
    monkeypatch.setattr(ecobot.Ecowitt, "__init__", lambda self, c, **kw: eco_init(self, c, eco_t, ha_t, **kw))
    monkeypatch.setattr(ecobot.AirGradient, "__init__", lambda self, c, **kw: air_init(self, c, transport=air_t, **kw))
    sources, _ = await ecobot.start_sources(cfg)
    eco, air = sources
    assert air.ha is eco.ha and air.ha is not None and not air._own_ha                   # one client, so Home Assistant failing is said once
    for source in sources:
        await source.close()


async def test_without_the_weather_station_the_air_sensor_has_a_home_assistant_client_of_its_own(tmp_path):
    transport, _ = air_transport()
    air = AirGradient(config(tmp_path, homeassistant_url="http://ha", homeassistant_token="tok"), transport=transport)
    assert air.ha is not None and air._own_ha
    await air.close()


async def test_home_assistants_states_are_the_live_reading_and_are_sampled_like_the_monitors_while_it_does_not_answer(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from lib.airgradient import source
    from lib.storage.catalogue import HOME_ASSISTANT
    t0 = 1_793_000_100 // 300 * 300
    clock = NS(t=float(t0))
    monkeypatch.setattr(source, "time", NS(time=lambda: clock.t))
    fake = FakeHA()
    fake.live = lambda: [state(PREFIX + "voc_index", 100 + (clock.t - t0) // 60), state(PREFIX + "pm2_5", 24, "µg/m³")]
    air = await air_with(tmp_path, fake)
    reading = (await air.latest()).reading
    assert reading["pm2_5"]["value"] == 24.0 and reading["voc_index"]["value"] == 100.0 and reading["sensor"] == "Outdoor"   # Home Assistant's states, with the cloud's name
    assert air.warmer.interval == source.SAMPLE_SECONDS                                 # read every minute, as the monitor would be
    for k in range(6):
        clock.t = t0 + 60 * k + 1
        await air.warm()
    rows = await air.store.load(t0, t0 + 600, srcs=(HOME_ASSISTANT,))
    assert rows[0]["ts"] == t0 and rows[0]["pm2_5"] == 24.0 and rows[0]["voc_index"] == 102.0 and rows[0]["_range"]["voc_index"] == (100.0, 104.0)   # the five samples' mean and band
    assert len(await air.store.load_minutes(t0, t0 + 360)) == 6
    assert not [c for c in fake.calls if c.endswith("/current")] or air.requests == 1   # the cloud was asked for the name at most
    await air.close()
