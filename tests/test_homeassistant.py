import json
from datetime import datetime, timedelta, UTC

import httpx
import pytest

from lib.ecowitt import homeassistant as ha
from lib.ecowitt.live import BUCKET
from lib.ecowitt.homeassistant import HomeAssistant, ranges_of, states_of
from tests.fakes import archived_station, config

M = 1_800_000_000 // BUCKET * BUCKET


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


def test_a_state_that_is_not_a_number_is_left_out_of_an_entitys_history():
    rows = [{"entity_id": "sensor.a", "state": "12.5", "last_changed": iso(M)}, {"state": "unavailable", "last_changed": iso(M + 60)},
            {"state": "13.0", "last_changed": iso(M + 120)}, {"state": "oops"}, {"state": "9", "last_updated": iso(M + 30)}]
    assert states_of(rows) == [(M, 12.5), (M + 30, 9.0), (M + 120, 13.0)]


def test_a_five_minutes_range_is_the_states_changed_to_in_it_and_the_one_held_as_it_began():
    states = [(M - 3000, 20.0), (M + 10, 22.0), (M + 100, 19.5), (M + 400, 25.0)]
    found = ranges_of(states, M - 1000, M + 1000)
    assert found[M] == (4, 19.5, 22.0) or found[M][1:] == (19.5, 22.0)      # 20 (held as it began), 22 and 19.5 inside
    assert found[M - 300][1:] == (20.0, 20.0) and found[M - 300][0] == 2     # nothing changed: a range of nothing, counted as two readings
    assert found[M + 300][1:] == (19.5, 25.0)                                # 19.5 held at the start, 25.0 arrived
    assert min(found) == M - 900 and max(found) == M + 600                   # (the stretch around M + 900 is not over yet at M + 1000)
    assert ranges_of([], M - 1000, M + 1000) == {} and ranges_of([(M + 5000, 1.0)], M - 1000, M + 1000) == {}   # nothing known before


def station(entities: dict[str, list[dict]], extra_states=()):
    """A Home Assistant that answers /api/states and /api/history/period with these entities' histories."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params), request.headers.get("authorization")))
        if request.url.path == "/api/states":
            return httpx.Response(200, json=[{"entity_id": e} for e in [*entities, *extra_states]])
        wanted = request.url.params["filter_entity_id"].split(",")
        return httpx.Response(200, json=[[{"entity_id": e, **rows[0]}, *rows[1:]] for e, rows in entities.items() if e in wanted])
    return httpx.MockTransport(handler), seen


async def test_the_readings_home_assistant_kept_become_ranges_by_series_with_the_stations_names_found():
    temperature = [{"state": "20.0", "last_changed": iso(M - 1000)}, {"state": "23.0", "last_changed": iso(M + 20)}]
    vpd = [{"state": "8.0", "last_changed": iso(M - 1000)}, {"state": "12.0", "last_changed": iso(M + 20)}]
    transport, seen = station({"sensor.ws2900_outdoor_temperature": temperature, "sensor.ws2900_vapour_pressure_deficit": vpd,
                               "sensor.ws2900_wind_speed": [{"state": "5", "last_changed": iso(M)}]})
    home = HomeAssistant("http://ha.local:8123/", "secret", transport)
    found = await home.ranges(M - 400, M + 700)
    assert set(found) == {"outdoor.temperature", "outdoor.vpd"}                          # the others the station has no history for
    assert found["outdoor.temperature"][M][1:] == (20.0, 23.0)
    assert found["outdoor.vpd"][M][1:] == (0.8, 1.2)                                     # hPa to kPa
    paths = [p for p, _, _ in seen]
    assert paths[0] == "/api/states" and paths[1].startswith("/api/history/period/") and {a for _, _, a in seen} == {"Bearer secret"}
    assert "sensor.ws2900_outdoor_temperature" in seen[1][1]["filter_entity_id"] and "sensor.ws2900_wind_speed" not in seen[1][1]["filter_entity_id"]
    await home.close()


async def test_a_home_assistant_without_the_ecowitt_integration_says_so():
    transport, _ = station({"sensor.other_thing": [{"state": "1", "last_changed": iso(M)}]})
    home = HomeAssistant("http://ha.local", "t", transport)
    with pytest.raises(LookupError, match="outdoor_temperature"):
        await home.ranges(M - 400, M + 700)
    await home.close()


async def test_the_station_folds_home_assistants_readings_into_its_ranges_and_logs_a_failure_once(tmp_path, archived_cache, caplog):
    eco, _ = await archived_station(tmp_path, archived_cache)
    assert eco.ha is None and await eco.import_ranges() is None                         # no Home Assistant set: nothing
    now = datetime.now(UTC).timestamp()
    t0 = now - 7200
    changes = [{"state": str(10 + (i % 4)), "last_changed": iso(t0 + 60 * i)} for i in range(100)]
    transport, _ = station({"sensor.ws2900_outdoor_temperature": changes})
    eco.ha = HomeAssistant("http://ha", "t", transport)
    caplog.set_level("INFO")
    await eco.import_ranges()
    marks = eco.live.between("outdoor.temperature", int(t0), int(now))
    assert len(marks) > 15 and any(lo < hi for lo, hi in marks.values())      # (a stretch where it did not change has no width)
    assert "Home Assistant: " in caplog.text

    def down(request):
        raise httpx.ConnectError("no route")
    eco.ha.client = httpx.AsyncClient(base_url="http://ha", transport=httpx.MockTransport(down))
    caplog.clear()
    await eco.import_ranges()
    await eco.import_ranges()
    assert caplog.text.count("couldn't be read") == 1                                   # once, not every 15 minutes
    await eco.close()


def test_home_assistant_is_on_only_with_its_address_and_its_token(tmp_path):
    assert not config(tmp_path).homeassistant
    assert not config(tmp_path, homeassistant_url="http://ha").homeassistant
    assert config(tmp_path, homeassistant_url="http://ha", homeassistant_token="t").homeassistant
