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


async def test_the_keep_warm_refresh_reads_the_device_and_asks_the_cloud_only_for_the_history(tmp_path):
    transport, calls = air_transport()
    air = AirGradient(config(tmp_path, airgradient_local_url=URL), transport=transport)
    device = []
    air.local, _ = sensor(lambda request: device.append(request) or httpx.Response(200, json=DEVICE))
    await air.start()
    calls.clear()
    device.clear()
    await air.warm()
    assert len(device) == 1 and "current" not in calls and "past" in calls               # the live reading from the sensor, the history from the cloud
    await air.close()


# ---------- the bot keeps what the sensor says ----------
from datetime import date, datetime, timedelta  # noqa: E402
from types import SimpleNamespace as NS  # noqa: E402

from lib.airgradient import source  # noqa: E402
from lib.timeutil import day_bounds  # noqa: E402
from tests.fakes import TZ  # noqa: E402

DAY = date(2026, 10, 27)
T0 = int(datetime(2026, 10, 27, 10, 0, tzinfo=TZ).timestamp())


class Now:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


async def recording_air(tmp_path, clock, values):
    """An AirGradient whose sensor says values[n] for PM2.5 at its n-th reading, and whose clock (also the cloud's) is `clock`."""
    transport, calls = air_transport(rows_per_day=288)
    air = AirGradient(config(tmp_path, airgradient_local_url=URL), transport=transport)
    readings = iter(values)
    air.local, _ = sensor(lambda request: httpx.Response(200, json={**DEVICE, "pm02Compensated": next(readings)}), clock)
    air.now = lambda: datetime.fromtimestamp(clock.t, TZ).replace(tzinfo=None)
    return air, calls


async def test_readings_are_averaged_into_five_minute_buckets_kept_with_the_cloud_only_filling_gaps(tmp_path, monkeypatch):
    clock = Now(T0)
    monkeypatch.setattr(source, "time", NS(time=clock))
    air, calls = await recording_air(tmp_path, clock, [10 + k for k in range(12)])
    for k in range(12):                              # one reading a minute from 10:00 to 10:11
        clock.t = T0 + 60 * k
        assert await air._record()
    stored = {r["ts"]: r["pm2_5"] for r in air.store.load_day(DAY)}
    assert stored == {T0: 12.0, T0 + 300: 17.0}      # 10:00-10:04 and 10:05-10:09 averaged; 10:10 has two readings so far: not kept
    rows = {r["ts"]: r["pm2_5"] for r in await air._day_rows(DAY)}
    assert rows[T0] == 12.0 and rows[T0 + 300] == 17.0 and rows[T0 + 600] == 20.5     # what the sensor said wins; the open bucket is shown
    assert calls.count("past") == 1 and T0 - 3600 in rows                             # the hours before were asked of the cloud, once
    await air._day_rows(DAY)
    assert calls.count("past") == 1                                                    # and not again for a while
    clock.t = T0 + 300 * 20
    await air._day_rows(DAY)
    assert calls.count("past") == 2                                                    # (the readings stopped: those buckets are a gap)
    await air.close()


async def test_a_bucket_with_too_few_readings_is_not_kept(tmp_path, monkeypatch):
    clock = Now(T0)
    monkeypatch.setattr(source, "time", NS(time=clock))
    air, _ = await recording_air(tmp_path, clock, [10, 11, 12, 13])
    for t in (T0, T0 + 60, T0 + 300, T0 + 360):      # two readings in each of two buckets
        clock.t = t
        await air._record()
    await air._flush_bucket()                        # the open bucket ends
    assert air.store.load_day(DAY) == []
    await air.close()


async def test_a_finished_day_the_bot_recorded_is_completed_with_only_the_missing_buckets(tmp_path, monkeypatch):
    clock = Now(int(datetime(2026, 10, 29, 12, 0, tzinfo=TZ).timestamp()))
    monkeypatch.setattr(source, "time", NS(time=clock))
    air, calls = await recording_air(tmp_path, clock, [])
    lo, hi = day_bounds(DAY, TZ)
    gone = {lo + 300 * 50, lo + 300 * 51, lo + 300 * 200}
    for t in range(lo, hi, 300):
        if t not in gone:
            air.store.add_recorded({"ts": t, "pm2_5": 1.0})
    assert await air._day_rows(DAY) and calls.count("past") == 1
    rows = air.store.load_day(DAY)
    assert len(rows) == 288 and {r["ts"] for r in rows if r["pm2_5"] != 1.0} == gone    # the cloud's rows only where there were none
    assert air.store.counts([DAY]) == {DAY: 288}
    await air._day_rows(DAY)
    assert calls.count("past") == 1                                                     # a finished day is never asked about again
    await air.close()


# ---------- no cloud account: the sensor and the disk only ----------
def only_the_sensor(tmp_path, clock):
    transport, calls = air_transport(rows_per_day=288)
    cfg = config(tmp_path, airgradient_local_url=URL, airgradient_token="", airgradient_location="")
    air = AirGradient(cfg, transport=transport)
    air.local, _ = sensor(lambda request: httpx.Response(200, json=DEVICE), clock)
    air.now = lambda: datetime.fromtimestamp(clock.t, TZ).replace(tzinfo=None)
    return cfg, air, calls


def test_the_sensors_address_alone_switches_air_quality_on_with_no_cloud_account(tmp_path):
    assert config(tmp_path, airgradient_local_url=URL, airgradient_token="", airgradient_location="").airgradient
    assert not config(tmp_path, airgradient_token="", airgradient_location="").airgradient
    assert config(tmp_path).airgradient_cloud and not config(tmp_path, airgradient_token="").airgradient_cloud


async def test_without_a_cloud_account_the_cloud_is_never_asked(tmp_path, monkeypatch):
    clock = Now(T0)
    monkeypatch.setattr(source, "time", NS(time=clock))
    cfg, air, calls = only_the_sensor(tmp_path, clock)
    await air.start()
    reading = (await air.latest()).reading
    assert reading["pm2_5"]["value"] == 2.5 and reading["sensor"] == "AirGradient d83bda1c1e08"   # named by its serial number
    for k in range(12):
        clock.t = T0 + 60 * k
        await air.warm()
    assert air.requests == 0 and calls == [] and await air.backfill() == (0, 0, 0)
    rows, hourly, skipped = await air.rows(datetime(2026, 10, 20), datetime(2026, 10, 27, 23, 0))   # a week of which only today was recorded
    assert {r["ts"] for r in rows} <= {T0, T0 + 300, T0 + 600} and skipped == [] and calls == []
    assert air.store.counts([date(2026, 10, 20) + timedelta(days=k) for k in range(7)]) == {date(2026, 10, 20) + timedelta(days=k): None for k in range(7)}
    await air.close()


async def test_without_a_cloud_account_a_finished_day_with_gaps_stays_open_and_a_whole_one_is_done(tmp_path, monkeypatch):
    clock = Now(int(datetime(2026, 10, 29, 12, 0, tzinfo=TZ).timestamp()))
    monkeypatch.setattr(source, "time", NS(time=clock))
    cfg, air, calls = only_the_sensor(tmp_path, clock)
    lo, hi = day_bounds(DAY, TZ)
    for t in range(lo, hi, 300):
        if t != lo + 300 * 10:
            air.store.add_recorded({"ts": t, "pm2_5": 1.0})
    assert len(await air._day_rows(DAY)) == 287 and air.store.counts([DAY]) == {DAY: None} and calls == []   # a gap nothing can fill: not marked
    air.store.add_recorded({"ts": lo + 300 * 10, "pm2_5": 1.0})
    assert len(await air._day_rows(DAY)) == 288 and air.store.counts([DAY]) == {DAY: 288} and calls == []
    await air.close()


def test_zeroconf_is_not_loaded_unless_a_local_name_is_looked_up():
    import subprocess
    import sys
    code = ("import sys, lib.bot, lib.airgradient.local as l; assert 'zeroconf' not in sys.modules; "
            "import asyncio; asyncio.run(l.address_of('192.168.1.5')); assert 'zeroconf' not in sys.modules")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**__import__("os").environ, "MPLCONFIGDIR": "/tmp"})
    assert done.returncode == 0, done.stderr
