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
                   "atmp": 19.45, "atmp_corrected": 17.86, "timestamp": "2026-10-27T01:02:03+00:00".replace("+00:00", "Z")}
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
from datetime import date, datetime, timedelta, timezone  # noqa: E402
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


async def recorded(air, rows: list[dict]):
    """The bot's own readings (add_recorded), written in one transaction rather than one each: a day of them in a moment."""
    from lib.airgradient.readings import storage_rows
    from lib.storage.catalogue import BOT
    await air.store.storage.write(lambda w: w.put(w.air_sensor(air.store.loc), "5min", storage_rows(rows, True), BOT))


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
    stored = {r["ts"]: r["pm2_5"] for r in await air.store.load_day(DAY)}
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


async def test_a_recorded_bucket_keeps_its_lowest_and_highest_reading_and_the_chart_shades_between_them(tmp_path, monkeypatch):
    clock = Now(T0)
    monkeypatch.setattr(source, "time", NS(time=clock))
    air, _ = await recording_air(tmp_path, clock, [10 + k for k in range(12)])
    for k in range(12):
        clock.t = T0 + 60 * k
        await air._record()
    rows = await air.store.load_day(DAY)
    assert [r["_range"]["pm2_5"] for r in rows] == [(10.0, 14.0), (15.0, 19.0)]       # five readings each: 10..14, then 15..19
    line, plotted = air._line("pm2_5", rows)
    assert plotted.low == [10.0, 15.0] and plotted.high == [14.0, 19.0] and line.low == plotted.low
    await air.store.add_recorded({"ts": T0 + 3000, "pm2_5": 5.0})                              # a bucket with no spread has none
    assert "_range" not in (await air.store.load(T0 + 3000, T0 + 3300))[0]
    open_bucket = (await air._day_rows(DAY))[-1]                                           # the bucket still being averaged is shaded too
    assert open_bucket["_range"]["pm2_5"] == (20.0, 21.0)
    await air.close()


async def test_a_bucket_with_too_few_readings_is_not_kept(tmp_path, monkeypatch):
    clock = Now(T0)
    monkeypatch.setattr(source, "time", NS(time=clock))
    air, _ = await recording_air(tmp_path, clock, [10, 11, 12, 13])
    for t in (T0, T0 + 60, T0 + 300, T0 + 360):      # two readings in each of two buckets
        clock.t = t
        await air._record()
    await air._flush_bucket()                        # the open bucket ends
    assert await air.store.load_day(DAY) == []
    await air.close()


def clouds(ts: int) -> float:
    """What the fake cloud (fakes.air_transport) says PM2.5 was at this time."""
    return 5.0 + datetime.fromtimestamp(ts, timezone.utc).hour % 12


async def test_a_finished_day_the_bot_recorded_is_completed_with_only_what_it_lacks(tmp_path, monkeypatch, caplog):
    clock = Now(int(datetime(2026, 10, 29, 12, 0, tzinfo=TZ).timestamp()))
    monkeypatch.setattr(source, "time", NS(time=clock))
    air, calls = await recording_air(tmp_path, clock, [])
    lo, hi = day_bounds(DAY, TZ)
    gone = {lo + 300 * 50, lo + 300 * 51, lo + 300 * 200}
    await recorded(air, [{"ts": t, "pm2_5": clouds(t) + 0.5} for t in range(lo, hi, 300) if t not in gone])   # (near enough the cloud's to be the same reading)
    assert await air._day_rows(DAY) and calls.count("past") == 1                  # the whole day, in one request
    rows = await air.store.load_day(DAY)
    assert len(rows) == 288 and {r["ts"] for r in rows if r["pm2_5"] == clouds(r["ts"])} == gone   # the cloud's PM2.5 only where there was none
    assert all(r["co2"] == 450 for r in rows)                                                   # ... and what the bot's readings lacked
    assert (await air.store.counts([DAY])) == {DAY: 288} and "isn't the reading the bot keeps" not in caplog.text
    await air._day_rows(DAY)
    assert calls.count("past") == 1                                                     # a finished day is never asked about again
    await air.close()


async def test_a_metric_the_cloud_gives_differently_fills_no_gaps_and_is_said_once_with_the_fix(tmp_path, monkeypatch, caplog):
    clock = Now(int(datetime(2026, 10, 29, 12, 0, tzinfo=TZ).timestamp()))
    monkeypatch.setattr(source, "time", NS(time=clock))
    air, calls = await recording_air(tmp_path, clock, [])
    lo, hi = day_bounds(DAY, TZ)
    gone = {lo + 300 * 50, lo + 300 * 51}
    await recorded(air, [{"ts": t, "pm2_5": 1.0, "pm10": 8.0} for t in range(lo, hi, 300) if t not in gone])   # the monitor's raw PM2.5, and the cloud's PM10
    rows = {r["ts"]: r for r in await air._day_rows(DAY)}
    assert len(rows) == 288 and all("pm2_5" not in rows[t] for t in gone)             # the cloud's PM2.5 (5 to 16) is another reading: a gap stays
    assert all(rows[t]["pm10"] == 8.0 and rows[t]["co2"] == 450 for t in gone)         # the rest of its readings fill it
    assert caplog.text.count("the cloud's PM2.5 isn't the reading the bot keeps") == 1 and "set the monitor's PM2.5 correction" in caplog.text
    assert "PM2.5 differs between the cloud and what the bot keeps" in air._summary({})
    day2 = DAY - timedelta(days=1)
    await recorded(air, [{"ts": t, "pm2_5": 1.0} for t in range(*day_bounds(day2, TZ), 300)])
    await air._day_rows(day2)
    assert caplog.text.count("isn't the reading the bot keeps") == 1                   # (said once)
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
    assert (await air.store.counts([date(2026, 10, 20) + timedelta(days=k) for k in range(7)])) == {date(2026, 10, 20) + timedelta(days=k): None for k in range(7)}
    await air.close()


async def test_without_a_cloud_account_a_finished_day_with_gaps_stays_open_and_a_whole_one_is_done(tmp_path, monkeypatch):
    clock = Now(int(datetime(2026, 10, 29, 12, 0, tzinfo=TZ).timestamp()))
    monkeypatch.setattr(source, "time", NS(time=clock))
    cfg, air, calls = only_the_sensor(tmp_path, clock)
    lo, hi = day_bounds(DAY, TZ)
    await recorded(air, [{"ts": t, "pm2_5": 1.0} for t in range(lo, hi, 300) if t != lo + 300 * 10])
    assert len(await air._day_rows(DAY)) == 287 and (await air.store.counts([DAY])) == {DAY: None} and calls == []   # a gap nothing can fill: not marked
    await air.store.add_recorded({"ts": lo + 300 * 10, "pm2_5": 1.0})
    assert len(await air._day_rows(DAY)) == 288 and (await air.store.counts([DAY])) == {DAY: 288} and calls == []
    await air.close()


def test_zeroconf_is_not_loaded_unless_a_local_name_is_looked_up():
    import subprocess
    import sys
    code = ("import sys, lib.airgradient, lib.airgradient.local as l; assert 'zeroconf' not in sys.modules; "
            "import asyncio; asyncio.run(l.address_of('192.168.1.5')); assert 'zeroconf' not in sys.modules")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


async def test_airgradient_source_picks_the_monitors_sources_and_a_typo_keeps_auto(tmp_path, caplog):
    ha = {"homeassistant_url": "http://ha", "homeassistant_token": "tok"}
    for kwargs, monitor, home, cloud in (
            ({}, False, False, True),
            ({"airgradient_local_url": URL, **ha}, True, True, True),
            ({"airgradient_local_url": URL, **ha, "airgradient_source": "local"}, True, False, False),
            ({"airgradient_local_url": URL, **ha, "airgradient_source": "homeassistant"}, False, True, False),
            ({"airgradient_local_url": URL, **ha, "airgradient_source": "cloud"}, False, False, True),
            ({"airgradient_source": "homeassistant"}, False, False, False)):             # no Home Assistant set: nothing
        cfg = config(tmp_path, **kwargs)
        assert (cfg.airgradient_local, cfg.airgradient_homeassistant, cfg.airgradient_cloud, cfg.airgradient) == (monitor, home, cloud, monitor or home or cloud), kwargs
        if cfg.airgradient:
            air = AirGradient(cfg, air_transport()[0])
            assert (air.local is not None, air.ha is not None, air.cloud) == (monitor, home, cloud), kwargs
            await air.close()
    from lib.config import _air_source
    assert _air_source("Cloud", "auto", "AIRGRADIENT_SOURCE") == "cloud"
    assert _air_source("sensor", "auto", "AIRGRADIENT_SOURCE") == "auto" and "AIRGRADIENT_SOURCE: 'sensor' is not" in caplog.text


async def test_each_reading_of_the_monitor_is_kept_as_a_minute_and_a_short_chart_is_drawn_from_them(tmp_path, monkeypatch):
    clock = Now(T0)
    monkeypatch.setattr(source, "time", NS(time=clock))
    air, _ = await recording_air(tmp_path, clock, [10 + k for k in range(130)])
    for k in range(130):                              # a reading a minute for a little over two hours
        clock.t = T0 + 60 * k
        await air._record()
    minutes = await air.store.load_minutes(T0, T0 + 130 * 60)
    assert [m["ts"] for m in minutes] == [T0 + 60 * k for k in range(130)] and minutes[5]["pm2_5"] == 15.0
    assert await air.store.load_minutes(T0 - 3600, T0 + 130 * 60) == []              # they do not reach back to the start: not used
    start = datetime.fromtimestamp(T0, TZ).replace(tzinfo=None)
    rows, _, _ = await air.rows(start, start + timedelta(hours=2))
    assert len(rows) == 121 and rows[1]["ts"] - rows[0]["ts"] == 60                  # the chart's readings are the minutes ...
    rows, _, _ = await air.rows(start - timedelta(hours=1), start + timedelta(hours=2))
    assert all(b["ts"] - a["ts"] == 300 for a, b in zip(rows, rows[1:]))             # ... else the 5-minute readings
    await air.close()
