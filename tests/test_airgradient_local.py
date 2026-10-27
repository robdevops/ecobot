"""The live reading from the sensor's own server (lib/airgradient/local.py)."""

import httpx
import pytest

from lib.airgradient import local
from lib.airgradient.local import LocalSensor, address_of, cloud_shaped
from lib.airgradient.source import AirGradient
from tests.fakes import air_transport, config

DEVICE = {"pm01": 1.5, "pm02": 4.0, "pm02Compensated": 2.5, "pm10": 6.0, "rco2": 419, "tvocIndex": 74.5, "noxIndex": 3,
          "atmp": 19.45, "atmpCompensated": 17.86, "serialno": "d83bda1c1e08", "firmware": "3.7.0", "model": "O-1PST"}
URL = "http://airgradient_d83bda1c1e08.local"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def sensor(handler, clock=None, found="192.168.1.116"):
    asked = []

    async def resolve(host):
        asked.append(host)
        return found
    return LocalSensor(URL, transport=httpx.MockTransport(handler), resolve=resolve, clock=clock or Clock()), asked


def test_a_reading_is_given_in_the_clouds_field_names_with_the_correction_first():
    row = cloud_shaped(DEVICE, local.datetime(2026, 10, 27, 1, 2, 3, tzinfo=local.UTC))
    assert row == {"pm01": 1.5, "pm02": 4.0, "pm02_corrected": 2.5, "pm10": 6.0, "rco2": 419, "tvocIndex": 74.5, "noxIndex": 3,
                   "timestamp": "2026-10-27T01:02:03+00:00".replace("+00:00", "Z")}
    assert cloud_shaped({"model": "x"}, local.datetime.now(local.UTC)) is None          # nothing the bot uses
    assert "pm02_corrected" not in cloud_shaped({"pm02": 4.0}, local.datetime.now(local.UTC))   # an older firmware: the plain value stands


async def test_the_device_is_asked_at_the_address_its_name_has_and_the_name_goes_in_the_host_header():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers["host"]))
        return httpx.Response(200, json=DEVICE)
    s, asked = sensor(handler)
    row = await s.read()
    assert seen == [("http://192.168.1.116/measures/current", "airgradient_d83bda1c1e08.local")] and row["pm02_corrected"] == 2.5
    await s.read()
    assert asked == ["airgradient_d83bda1c1e08.local"]                                  # the address is kept, not looked up each time


async def test_a_failure_is_not_retried_for_five_minutes_and_the_address_is_looked_up_again_after():
    clock, up = Clock(), [False]

    def handler(request):
        return httpx.Response(200, json=DEVICE) if up[0] else httpx.Response(500)
    s, asked = sensor(handler, clock)
    assert await s.read() is None
    up[0] = True
    clock.t += local.RETRY_SECONDS - 1
    assert await s.read() is None and len(asked) == 1                                   # still resting: nothing was asked
    clock.t += 2
    assert (await s.read())["pm02"] == 4.0 and len(asked) == 2                          # and the device is found again


@pytest.mark.parametrize("found, reply", [(None, httpx.Response(200, json=DEVICE)), ("192.168.1.116", httpx.Response(200, json={"model": "x"})),
                                          ("192.168.1.116", httpx.Response(200, text="not json"))])
async def test_a_name_that_is_not_found_or_a_reply_without_readings_gives_none_not_an_error(found, reply):
    s, _ = sensor(lambda request: reply, found=found)
    assert await s.read() is None


async def test_the_air_source_uses_the_device_and_the_clouds_name_and_falls_back_to_the_cloud(tmp_path):
    transport, calls = air_transport()
    air = AirGradient(config(tmp_path, airgradient_local_url=URL), transport=transport)
    up = [True]
    air.local, _ = sensor(lambda request: httpx.Response(200, json=DEVICE) if up[0] else httpx.Response(500))
    await air.start()
    reading = (await air.latest()).reading
    assert reading["sensor"] == "Outdoor" and reading["pm2_5"]["value"] == 2.5 and reading["pm10"]["value"] == 6.0   # the device's figures
    assert calls.count("current") == 1                                                  # the cloud was asked once, at start, for the name
    up[0] = False
    reading = (await air.latest()).reading                                              # (the reading is reused for a few minutes)
    assert reading["pm2_5"]["value"] == 2.5
    air._current = None
    assert (await air.latest()).reading["pm2_5"]["value"] == 12.0 and calls.count("current") == 2   # the device is down: the cloud's figure
    await air.close()


async def test_an_address_is_used_as_given_a_local_name_is_asked_of_the_network_and_other_names_of_the_system(monkeypatch):
    assert await address_of("192.168.1.116") == "192.168.1.116" and await address_of("[fe80::1]") == "[fe80::1]"

    async def mdns(host):
        return "10.0.0.7"
    monkeypatch.setattr(local, "mdns_address", mdns)
    assert await address_of("airgradient_d83bda1c1e08.local") == "10.0.0.7" and await address_of("Ag.LOCAL.") == "10.0.0.7"
    assert await address_of("localhost") in ("127.0.0.1", "::1")
