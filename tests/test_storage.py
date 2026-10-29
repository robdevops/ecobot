"""The new storage (lib/storage): its catalogue and tables, its two threads, the rules a write follows, the day rows, and the reads, each
checked against the code it replaces where there is one (the old cache's rules, lines.bucket_summary, rain.rain_slots)."""

import asyncio
import random
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

import pytest

from lib import counts
from lib.lines import bucket_summary
from lib.rain import MAX_GAP, rain_slots
from lib.storage import Storage, catalogue, schema
from lib.storage.catalogue import BOT, CLOUD, HOME_ASSISTANT, Column, column_of, field_of, text
from lib.storage.days import utc_days
from lib.storage.write import horizon
from lib.timeutil import day_bounds, local_date
from tests.fakes import MAC, TZ

DAY = 86400


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "ecobot.sqlite", TZ)
    yield s
    s.close()


def data(group: str = "outdoor", field: str = "temperature", values: dict | None = None, unit: str = "℃") -> dict:
    return {group: {field: {"unit": unit, "list": {str(t): v for t, v in (values or {}).items()}}}}


def old_day(days_ago: int = 10) -> int:
    """The start of a local day long settled."""
    return day_bounds(local_date(int(time.time()), TZ) - timedelta(days=days_ago), TZ)[0]


def read(s: Storage, fn):
    return s.reader.run(lambda r: fn(r), ())


# ---------- the catalogue and the tables ----------
def test_each_field_has_its_column_its_low_and_high_theirs_and_an_unknown_one_none():
    assert column_of("outdoor", "temperature") == ("temperature", "value")
    assert column_of("outdoor", "temperature_low") == ("temperature", "lo") and column_of("wind", "wind_gust_high") == ("wind_gust", "hi")
    assert column_of("indoor", "app_tempin") == ("app_temp", "value") and field_of("indoor", "app_temp") == "app_tempin"
    assert column_of("rainfall", "1_hour") == ("1_hour", "value") and column_of("rainfall_piezo", "daily") == ("daily", "value")
    assert column_of("outdoor", "lightning") is None and column_of("temp_and_humidity_ch1", "temperature") is None
    assert catalogue.kind_of("temp_and_humidity_ch1") == catalogue.OTHER
    assert text(-0.04, 1) == "0.0" and text(12.25, 1) == "12.2" and text(94.0, 0) == "94" and text(0.0846, 3) == "0.085"
    assert [text(v, 1, whole=True) for v in (3.0, 1.4, 2.96, -0.0)] == ["3", "1.4", "3", "0"]   # UV: the cloud's "3", Home Assistant's "1.4"


def test_the_tables_come_from_the_catalogue_and_a_reading_added_to_it_is_added_to_them(tmp_path, monkeypatch):
    s = Storage(tmp_path / "e.sqlite", TZ)
    tables = {r[0] for r in s.write_db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {*catalogue.KINDS, *(f"{k}_day" for k in catalogue.KINDS), "sensors", "coverage", "fields", "extra", "meta"} <= tables
    assert s.write_db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    s.close()
    monkeypatch.setitem(catalogue.KINDS, "light", (*catalogue.KINDS["light"], Column("lux", 0, "mean")))
    again = Storage(tmp_path / "e.sqlite", TZ)
    have = {r[1] for r in again.write_db.execute("PRAGMA table_info(light)")}
    assert {"lux", "lux_lo", "lux_hi"} <= have and "lux_hi_ts" in {r[1] for r in again.write_db.execute("PRAGMA table_info(light_day)")}
    again.close()


def test_a_file_from_a_newer_bot_is_left_alone(tmp_path):
    db = sqlite3.connect(tmp_path / "e.sqlite")
    db.execute(f"PRAGMA user_version = {schema.SCHEMA_VERSION + 1}")
    db.close()
    with pytest.raises(schema.NewerFile):
        Storage(tmp_path / "e.sqlite", TZ)


# ---------- the threads ----------
async def test_a_request_reads_in_one_hop_on_the_readers_thread_and_its_queries_are_counted(storage, monkeypatch):
    monkeypatch.setattr(asyncio, "to_thread", asyncio.to_thread)
    t = old_day()
    await storage.write(lambda w: w.put_ecowitt(MAC, "5min", ["outdoor"], data(values={t: "10.0", t + 300: "11.0"}), t, t + 300))
    seen = {}

    def request(r):
        seen["thread"] = threading.current_thread().name
        sensor = r.sensor(MAC, "outdoor")
        return r.rows("climate", [sensor.id], "5min", t, t + 300), r.days("climate", [sensor.id], local_date(t, TZ), local_date(t, TZ))
    count = counts.start()
    rows, days = await storage.read(request)
    counts.detach()
    assert seen["thread"].startswith("storage-read") and (count.hops, count.sql) == (1, 2)   # BEGIN and COMMIT are not queries
    assert rows[1].values["temperature"] == [10.0, 11.0] and days[1].n == [2]


def test_a_write_that_fails_leaves_nothing_behind_not_even_in_memory(storage):
    t = old_day()
    with pytest.raises(RuntimeError), storage.writer.transaction() as w:
        w.put_ecowitt(MAC, "5min", ["outdoor"], data(values={t: "10.0"}), t, t + 300)
        raise RuntimeError("disk trouble")
    assert storage.sensors.all() == [] and storage.coverage.of(1, "5min") == []
    assert storage.write_db.execute("SELECT COUNT(*) FROM climate").fetchone()[0] == 0
    assert storage.write_db.execute("SELECT COUNT(*) FROM climate_day").fetchone()[0] == 0


def test_a_readers_view_of_coverage_and_sensors_changes_only_when_the_write_is_committed(storage):
    t = old_day()
    with storage.writer.transaction() as w:
        w.put_ecowitt(MAC, "30min", ["outdoor"], data(values={t: "10.0"}), t, t + 1799)
        assert storage.sensors.find(MAC, "outdoor") is None and not storage.coverage.held(1, "30min", t, t + 1799)
    assert storage.sensors.find(MAC, "outdoor").kind == "climate" and storage.coverage.held(1, "30min", t, t + 1799)


# ---------- the rules of a write ----------
def test_a_reading_replaces_one_as_final_and_from_a_source_ranked_as_high_and_otherwise_only_fills_in(storage):
    t = old_day()
    with storage.writer.transaction() as w:
        s = w.ecowitt_sensor(MAC, "outdoor")
        w.put(s, "5min", [{"ts": t, "temperature": 10.0, "final": 0}], CLOUD)
        w.put(s, "5min", [{"ts": t, "temperature": 11.0, "humidity": 50.0}], CLOUD)        # final replaces provisional
        w.put(s, "5min", [{"ts": t, "temperature": 12.0, "final": 0}], BOT)                # provisional never replaces final
        w.put(s, "5min", [{"ts": t, "temperature": 13.0}], HOME_ASSISTANT)                 # as final, ranked higher: replaces
        w.put(s, "5min", [{"ts": t, "temperature": 14.0, "humidity": 60.0, "dew_point": 5.0}], CLOUD)   # ranked lower: only fills in
    row = storage.write_db.execute("SELECT temperature, humidity, dew_point, src, final FROM climate").fetchone()
    assert row == (13.0, 50.0, 5.0, HOME_ASSISTANT, 1)
    with storage.writer.transaction() as w:   # a row Home Assistant gave while settling is settled by the cloud's final reading, kept
        w.put(s, "5min", [{"ts": t + 300, "temperature": 20.0, "final": 0}], HOME_ASSISTANT)
        w.put(s, "5min", [{"ts": t + 300, "temperature": 21.0}], CLOUD)
    assert storage.write_db.execute("SELECT temperature, src, final FROM climate WHERE ts = ?", (t + 300,)).fetchone() == (20.0, HOME_ASSISTANT, 1)


def test_a_band_is_only_ever_widened_keeps_the_value_inside_and_waits_for_a_reading_not_written_yet(storage):
    t = old_day()
    with storage.writer.transaction() as w:
        s = w.ecowitt_sensor(MAC, "outdoor")
        w.widen(s, "5min", {"temperature": {t: (9.0, 11.0)}})                       # before the reading: a row with only the band
        assert read(storage, lambda r: r.rows("climate", [s.id], "5min", t, t)) == {}   # (which is no reading)
        w.put(s, "5min", [{"ts": t, "temperature": 12.0}])                          # the reading: its value, the band taken out to it
        w.widen(s, "5min", {"temperature": {t: (10.0, 10.5)}})                      # narrower: nothing changes
        w.widen(s, "5min", {"temperature": {t: (9.0, 11.0)}})                       # again: nothing changes
    assert storage.write_db.execute("SELECT temperature, temperature_lo, temperature_hi, final FROM climate").fetchone() == (12.0, 9.0, 12.0, 1)


def test_the_half_hours_bands_come_from_the_five_minute_readings_inside_them(storage):
    """As HistoryCache.slot_ranges: the lowest and highest of two or more 5-minute values (and the half hour's own), and the 5-minute bands."""
    base = old_day()
    with storage.writer.transaction() as w:
        s = w.ecowitt_sensor(MAC, "outdoor")
        w.put(s, "30min", [{"ts": base, "dew_point": 12.0}, {"ts": base + 1800, "dew_point": 18.0}, {"ts": base + 3600, "dew_point": 30.0},
                           {"ts": base + 5400, "dew_point": 5.0}])
        w.put(s, "5min", [{"ts": base + 300 * i, "dew_point": 10.0 + i} for i in range(12)] + [{"ts": base + 3600, "dew_point": 31.0}])
        w.widen(s, "5min", {"dew_point": {base + 5400: (2.0, 6.0), base + 5700: (4.0, 7.5)}})
    rows = dict((ts, (lo, hi)) for ts, lo, hi in storage.write_db.execute(
        "SELECT ts, dew_point_lo, dew_point_hi FROM climate WHERE res = '30min' ORDER BY ts"))
    assert rows[base] == (10.0, 15.0) and rows[base + 1800] == (16.0, 21.0)
    assert rows[base + 3600] == (None, None)       # a single 5-minute reading is no range
    assert rows[base + 5400] == (2.0, 7.5)         # the bot's bands of its 5 minutes


def test_minutes_are_kept_ten_days_and_dropped_after(storage):
    now = time.time()
    minutes = {str(int(now) - 11 * DAY): "1.0", str(int(now) - 600): "2.0", str(int(now) - 540): "3.0"}
    with storage.writer.transaction() as w:
        w.put_minutes(MAC, {"outdoor": {"temperature": {"unit": "℃", "list": minutes}}}, now)
    assert [r[0] for r in storage.write_db.execute("SELECT temperature FROM climate WHERE res = '1min' ORDER BY ts")] == [2.0, 3.0]
    with storage.writer.transaction() as w:
        assert w.prune_minutes(int(now) - 570) == 1
    assert storage.write_db.execute("SELECT src FROM climate").fetchone()[0] == HOME_ASSISTANT


def test_the_live_ranges_of_two_readings_or_more_widen_their_five_minutes(storage):
    t = old_day()
    with storage.writer.transaction() as w:
        assert w.put_ranges(MAC, {"outdoor.temperature": {t: (3, 9.0, 11.0), t + 300: (1, 8.0, 8.0)}, "pressure.relative": {t: (2, 1010.0, 1011.0)},
                                  "rainfall.daily": {t: (2, 1.0, 2.0)}, "outdoor.nothing": {t: (2, 1.0, 2.0)}}) == 2
    rows = storage.write_db.execute("SELECT sensor, ts, temperature_lo, temperature_hi FROM climate").fetchall()
    assert rows == [(storage.sensors.find(MAC, "outdoor").id, t, 9.0, 11.0)]
    assert storage.write_db.execute("SELECT COUNT(*) FROM rain").fetchone()[0] == 0          # a rain counter takes no band


# ---------- an Ecowitt response, by the old cache's rules (tests/test_store.py) ----------
def test_the_unsettled_tail_is_kept_but_not_counted_as_fetched(storage):
    now = int(time.time())
    old, fresh = now - 5 * DAY, now - 600
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "5min", ["outdoor"], data(values={old: "1.5", old + 300: "1.5", fresh: "1.5"}), old - 300, now, now), ())
    rows = read(storage, lambda r: r.rows("climate", [1], "5min", 0, now))[1]
    assert rows.ts == [old, old + 300, fresh] and rows.final == [1, 1, 0]
    assert storage.coverage.held(1, "5min", old, old + 300) and storage.coverage.gaps(1, "5min", fresh, now)
    assert horizon("5min", now) == now - 3900


def test_an_old_stretch_with_nothing_in_it_counts_as_fetched_and_a_group_with_none_as_far_as_the_others(storage):
    now = int(time.time())
    start, end = now - 30 * DAY, now - 29 * DAY
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "30min", ["outdoor"], {}, start, end, now), ())
    assert storage.coverage.held(1, "30min", start, end)
    recent = now - 86400
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "30min", ["outdoor", "wind"], data(values={recent: "2.0"}), recent - 3600, now, now), ())
    wind = storage.sensors.find(MAC, "wind")
    assert storage.coverage.of(wind.id, "30min") == storage.coverage.of(1, "30min")[-1:] == [(recent - 3600, recent + 1799)]


def test_a_metric_ecowitt_adds_later_has_the_days_held_fetched_again_once(storage):
    now = int(time.time())
    old = now - 10 * DAY
    put = lambda d, s, e: storage.writer.run(lambda w: w.put_ecowitt(MAC, "30min", ["outdoor"], d, s, e, now), ())
    put(data(values={old: "1.0"}), old, old + 3600)
    assert storage.coverage.held(1, "30min", old, old + 3600)
    with_vpd = {"outdoor": {**data(values={old + 7200: "1.0"})["outdoor"], "vpd": {"unit": "kPa", "list": {str(old + 7200): "0.8"}}}}
    put(with_vpd, old + 7200, old + 7300)
    assert not storage.coverage.held(1, "30min", old, old + 3600)       # asked for again, to fill the vpd in
    put(data(values={old: "1.0"}), old, old + 3600)
    assert storage.coverage.held(1, "30min", old, old + 3600)           # and the vpd-less response does not loop


def test_a_field_the_catalogue_lacks_and_a_value_that_is_not_a_number_are_kept_as_they_came(storage):
    t = old_day()
    response = {"outdoor": {"temperature": {"unit": "℃", "list": {str(t): "12.3", str(t + 300): "--"}},
                            "brightness": {"unit": "lux", "list": {str(t): "4000"}}}}
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "5min", ["outdoor"], response, t, t + 300), ())
    assert read(storage, lambda r: r.extra(1, "5min", 0, 2 ** 40)) == {"brightness": {t: "4000"}, "temperature": {t + 300: "--"}}
    assert read(storage, lambda r: r.fields(1)) == {"temperature": "℃", "brightness": "lux"}


# ---------- the day rows ----------
def test_a_day_is_worked_out_from_the_finest_readings_that_hold_it_whole(storage):
    d = local_date(old_day(20), TZ)
    start, end = day_bounds(d, TZ, True)
    hour = lambda t: f"{10 + (t - start) // 3600 / 10:.1f}"   # 10.0 at midnight, up a tenth each hour
    five = {t: hour(t) for t in range(start, end + 1, 300)}
    thirty = data(values={t: hour(t) for t in range(start, end + 1, 1800)})
    thirty["outdoor"] |= {"temperature_high": {"unit": "℃", "list": {str(start + 14 * 3600 + 1800): "12.9"}},   # between 5-minute readings
                          "temperature_low": {"unit": "℃", "list": {str(start + 3 * 3600): "9.5"}}}
    with storage.writer.transaction() as w:
        w.put_ecowitt(MAC, "30min", ["outdoor"], thirty, start, end)
        assert storage.write_db.execute("SELECT source FROM climate_day").fetchone() is None   # (worked out when it is committed)
    assert read(storage, lambda r: r.days("climate", [1], d, d))[1].source == ["30min"]
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "5min", ["outdoor"], data(values=five), start, end), ())
    got = read(storage, lambda r: r.days("climate", [1], d, d))[1]
    assert (got.source, got.complete, got.n) == (["5min"], [True], [288])
    # the half hours' own lows and highs count, at the first of its 5-minute readings that is lowest or highest in the half hour
    assert got.hi["temperature"] == [12.9] and got.hi_ts["temperature"] == [start + 14 * 3600 + 1800]
    assert got.lo["temperature"] == [9.5] and got.lo_ts["temperature"] == [start + 3 * 3600]
    assert got.mean["temperature"][0] == pytest.approx(sum(10 + h / 10 for h in range(24)) / 24)     # the 5-minute readings' only
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "30min", ["outdoor"], data(values={t: hour(t) for t in range(start, end + 1, 1800)}),
                                               start, end), ())
    got = read(storage, lambda r: r.days("climate", [1], d, d))[1]
    assert got.hi["temperature"] == [12.9]                  # (a band only widens)
    five[start + 23 * 3600 + 600] = "13.0"
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "5min", ["outdoor"], data(values=five), start, end), ())
    got = read(storage, lambda r: r.days("climate", [1], d, d))[1]
    assert got.hi["temperature"] == [13.0] and got.hi_ts["temperature"] == [start + 23 * 3600 + 600]   # its 5-minute readings' own, when higher


def test_a_day_of_5_minute_readings_takes_in_the_30_minute_bands_that_arrive_after_it(storage):
    """The archive fetches the 5-minute days before the coarser cycles: a day held whole at 5 minutes is worked out again when its
    30-minute readings come, for Ecowitt's lows and highs between the 5-minute readings."""
    d = local_date(old_day(20), TZ)
    start, end = day_bounds(d, TZ, True)
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "5min", ["outdoor"], data(values={t: "10.0" for t in range(start, end + 1, 300)}), start, end), ())
    assert read(storage, lambda r: r.days("climate", [1], d, d))[1].hi["temperature"] == [10.0]
    thirty = data(values={t: "10.0" for t in range(start, end + 1, 1800)})
    thirty["outdoor"] |= {"temperature_high": {"unit": "℃", "list": {str(start + 3 * 3600): "12.9"}},
                          "temperature_low": {"unit": "℃", "list": {str(start + 5 * 3600): "7.5"}}}
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "30min", ["outdoor"], thirty, start, end), ())
    got = read(storage, lambda r: r.days("climate", [1], d, d))[1]
    assert (got.source, got.hi["temperature"], got.lo["temperature"]) == (["5min"], [12.9], [7.5])
    assert (got.hi_ts["temperature"], got.lo_ts["temperature"]) == ([start + 3 * 3600], [start + 5 * 3600])
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "1day", ["outdoor"], data(values={start - start % 86400: "10.0"}), start - 86400, end), ())
    assert read(storage, lambda r: r.days("climate", [1], d, d))[1].hi["temperature"] == [12.9]   # (a daily record changes nothing in it)


def test_a_part_of_a_day_is_worked_out_from_its_own_readings_as_a_day_is_with_only_the_half_hours_it_holds_whole(storage):
    d = local_date(old_day(20), TZ)
    start, end = day_bounds(d, TZ, True)
    hour = lambda t: f"{10 + (t - start) // 3600 / 10:.1f}"   # 10.0 at midnight, up a tenth each hour
    thirty = data(values={t: hour(t) for t in range(start, end + 1, 1800)})
    thirty["outdoor"] |= {"temperature_high": {"unit": "℃", "list": {str(start + 14 * 3600 + 1800): "12.9", str(start + 16 * 3600): "13.4"}}}
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "30min", ["outdoor"], thirty, start, end), ())
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "5min", ["outdoor"], data(values={t: hour(t) for t in range(start, end + 1, 300)}),
                                               start, end), ())
    a, b = start + 14 * 3600, start + 16 * 3600 + 600   # 2pm to 4:10pm: the half hour at 2:30 whole, the one at 4 not
    got = read(storage, lambda r: r.part_days("climate", [(1, d, a, b, "5min")]))[1]
    assert (got.day, got.source, got.complete, got.n) == ([d], ["5min"], [False], [27])
    assert got.hi["temperature"] == [12.9] and got.hi_ts["temperature"] == [start + 14 * 3600 + 1800]   # not 4 o'clock's 13.4
    assert got.lo["temperature"] == [11.4] and got.lo_ts["temperature"] == [a]
    assert got.mean["temperature"] == [pytest.approx((11.4 * 2 + 11.5 * 2 + 11.6) / 5)]               # the mean of its half hours
    whole = read(storage, lambda r: r.part_days("climate", [(1, d, start, end, "5min")]))[1]
    day = read(storage, lambda r: r.days("climate", [1], d, d))[1]
    assert (whole.mean, whole.lo, whole.lo_ts, whole.hi, whole.hi_ts) == (day.mean, day.lo, day.lo_ts, day.hi, day.hi_ts)   # as its row
    assert read(storage, lambda r: r.part_days("climate", [(1, d, start, end, "1min")])) == {}                # none at that resolution


def test_a_part_of_a_day_read_by_the_minute_has_its_records_at_their_minute(storage):
    now = int(time.time())
    a = now - now % 1800 - 3 * 3600
    minutes = {a + 60 * i: "11.0" for i in range(180)} | {a + 47 * 60: "12.7", a + 101 * 60: "9.1", a + 130 * 60: "12.7"}
    storage.writer.run(lambda w: w.put_minutes(MAC, data(values=minutes), now), ())
    got = read(storage, lambda r: r.part_days("climate", [(1, local_date(a, TZ), a, a + 3 * 3600 - 1, "1min")]))[1]
    assert (got.hi["temperature"], got.hi_ts["temperature"]) == ([12.7], [a + 47 * 60])   # the first of the two
    assert (got.lo["temperature"], got.lo_ts["temperature"]) == ([9.1], [a + 101 * 60])
    assert got.n == [180]


def test_a_day_not_held_whole_comes_from_its_daily_record_with_the_times_from_the_four_hour_readings(storage):
    d = local_date(old_day(500), TZ)
    first, last = utc_days(d, TZ)
    with storage.writer.transaction() as w:
        s = w.ecowitt_sensor(MAC, "outdoor")
        w.put(s, "1day", [{"ts": first, "temperature": 15.0, "temperature_lo": 8.0, "temperature_hi": 22.0}])
        w.put(s, "4hour", [{"ts": first + 4 * 3600 * i, "temperature": 15.0, "temperature_lo": 8.5 if i == 4 else 9.0,
                            "temperature_hi": 22.4 if i == 1 else 20.0} for i in range(6)])
        w.cover(s, "4hour", first, last + DAY - 1)
    got = read(storage, lambda r: r.days("climate", [s.id], d, d))[s.id]
    assert (got.source, got.timing, got.complete) == (["1day"], ["4hour"], [False])
    assert (got.hi["temperature"], got.hi_ts["temperature"]) == ([22.0], [first + 4 * 3600])     # the record's figure, at the 4 hours' time
    assert (got.lo["temperature"], got.lo_ts["temperature"]) == ([8.0], [first + 16 * 3600])
    assert got.mean["temperature"] == [15.0]


def test_today_is_worked_out_from_what_there_is_and_marked_incomplete(storage):
    now = int(time.time())
    start = day_bounds(local_date(now, TZ), TZ)[0]
    readings = {t: "10.0" for t in range(start, now - 300, 300)}
    if not readings:
        readings = {start: "10.0"}
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "5min", ["outdoor"], data(values=readings), start, now, now), ())
    got = read(storage, lambda r: r.days("climate", [1], local_date(now, TZ), local_date(now, TZ)))[1]
    assert (got.source, got.complete, got.n) == (["5min"], [False], [len(readings)])


def test_a_day_held_whole_with_no_readings_has_no_day_row(storage):
    start, end = day_bounds(local_date(old_day(20), TZ), TZ, True)
    storage.writer.run(lambda w: w.put_ecowitt(MAC, "5min", ["outdoor"], {}, start, end), ())
    assert storage.coverage.held(1, "5min", start, end)
    assert storage.write_db.execute("SELECT COUNT(*) FROM climate_day").fetchone()[0] == 0


def test_the_days_of_the_clock_changes_are_23_and_25_hours(storage):
    for d, hours in ((date(2026, 4, 5), 25), (date(2026, 10, 4), 23)):   # Melbourne: daylight saving ends, then starts
        start, end = day_bounds(d, TZ, True)
        assert (end + 1 - start) // 3600 == hours
        if d >= local_date(int(time.time()), TZ) - timedelta(days=2):
            continue
        storage.writer.run(lambda w: w.put_ecowitt(MAC, "5min", ["outdoor"], data(values={t: "1.0" for t in range(start, end + 1, 300)}),
                                                   start, end), ())
        assert read(storage, lambda r: r.days("climate", [1], d, d))[1].n == [hours * 12]


def test_a_new_time_zone_works_every_day_out_again(tmp_path):
    from zoneinfo import ZoneInfo
    s = Storage(tmp_path / "e.sqlite", TZ)
    d = local_date(old_day(20), TZ)
    start, end = day_bounds(d, TZ, True)
    s.writer.run(lambda w: w.put_ecowitt(MAC, "5min", ["outdoor"], data(values={t: "1.0" for t in range(start - DAY, end + DAY, 300)}),
                                         start - DAY, end + DAY), ())
    assert read(s, lambda r: r.days("climate", [1], d, d))[1].n == [288]
    s.close()
    perth = ZoneInfo("Australia/Perth")   # two or three hours behind: the same local date holds other readings
    again = Storage(tmp_path / "e.sqlite", perth)
    assert str(again.tz) == "Australia/Melbourne"                        # a file keeps its own zone ...
    asyncio.run(again.set_timezone(perth))                               # ... until the station names another
    start_p, end_p = day_bounds(d, perth, True)
    got = read(again, lambda r: r.days("climate", [1], d, d))[1]
    assert got.n == [288] and got.lo_ts["temperature"] == [start_p] and again.reader.meta("tz") == "Australia/Perth"
    again.close()


# ---------- reading ----------
def test_rows_are_columns_of_one_query_and_leave_out_a_reading_the_sensor_never_had(storage):
    t = old_day()
    with storage.writer.transaction() as w:
        out, ind = w.ecowitt_sensor(MAC, "outdoor"), w.ecowitt_sensor(MAC, "indoor")
        w.put(out, "30min", [{"ts": t, "temperature": 10.0, "temperature_lo": 9.0, "temperature_hi": 11.0}, {"ts": t + 1800, "temperature": 12.0}])
        w.put(ind, "30min", [{"ts": t, "temperature": 20.0, "app_temp": 21.0}])
    count = counts.start()
    got = read(storage, lambda r: r.rows("climate", [out.id, ind.id], "30min", t, t + 1800))
    counts.detach()
    assert count.sql == 1 and set(got[out.id].values) == {"temperature"} and set(got[ind.id].values) == {"temperature", "app_temp"}
    assert got[out.id].lo["temperature"] == [9.0, None] and got[out.id].hi["temperature"] == [11.0, None]


@pytest.mark.parametrize("width", [1800, 3600, 7200, 14400, 86400])
def test_buckets_are_worked_out_as_lines_bucket_summary_does(storage, width):
    rng = random.Random(width)
    t0 = old_day(40)
    t0 -= t0 % DAY
    fine, coarse = [], []
    for slot in range(0, 10 * DAY, 1800):
        v = round(rng.uniform(0, 30), 1)
        if rng.random() < 0.9:
            coarse.append({"ts": t0 + slot, "temperature": v, **({"temperature_lo": v - 2, "temperature_hi": v + 3} if rng.random() < 0.5 else {})})
        if slot < 6 * DAY:   # 5-minute readings for the first six days, some missing, some with a band
            for k in range(0, 1800, 300):
                if rng.random() < 0.8:
                    w = round(rng.uniform(0, 30), 1)
                    fine.append({"ts": t0 + slot + k, "temperature": w, **({"temperature_hi": w + 1} if rng.random() < 0.2 else {})})
    with storage.writer.transaction() as w:
        s = w.ecowitt_sensor(MAC, "outdoor")
        w.put(s, "30min", coarse, exact=True)
        w.put(s, "5min", fine, exact=True)
    got = read(storage, lambda r: r.buckets("climate", [s.id], ("5min", "30min"), t0, t0 + 10 * DAY, width, ["temperature"]))[s.id]
    points = [(r["ts"], r["temperature"], r.get("temperature_lo", r["temperature"]), r.get("temperature_hi", r["temperature"]), is_fine)
              for rows, is_fine in ((fine, True), (coarse, False)) for r in rows]
    want = bucket_summary(points, width)
    assert got.ts == sorted(want)
    for i, b in enumerate(got.ts):
        assert (got.mean["temperature"][i], got.lo["temperature"][i], got.hi["temperature"][i]) == pytest.approx(want[b])


def test_records_are_the_first_time_of_the_highest_and_lowest_and_each_months_own(storage):
    first = local_date(old_day(80), TZ).replace(day=1)
    days = [first + timedelta(days=n) for n in range(62)]
    with storage.writer.transaction() as w:
        s = w.ecowitt_sensor(MAC, "outdoor")
        for i, d in enumerate(days):
            start, end = day_bounds(d, TZ, True)
            w.put(s, "30min", [{"ts": start + 3600, "temperature": 30.0 if i in (5, 40) else 20.0 + i % 3}, {"ts": start + 7200, "temperature": 1.0}])
            w.cover(s, "30min", start, end)
    hottest = read(storage, lambda r: r.records("climate", s.id, "temperature", days[0], days[-1], "hi"))
    assert [(r.day, r.value, r.timing) for r in hottest] == [(days[5], 30.0, "30min")]   # the first of the two
    monthly = read(storage, lambda r: r.records("climate", s.id, "temperature", days[0], days[-1], "hi", "month"))
    assert [r.value for r in monthly][:2] == [30.0, 30.0] and all(r.day.day >= 1 for r in monthly)
    coldest = read(storage, lambda r: r.records("climate", s.id, "temperature", days[0], days[-1], "lo"))
    assert coldest[0].day == days[0] and coldest[0].ts == day_bounds(days[0], TZ)[0] + 7200


def test_rain_per_reading_is_worked_out_as_rain_rain_slots_does(storage):
    t0 = old_day(30)
    rng = random.Random(3)
    daily, total, t = {}, 0.0, t0
    while t < t0 + 3 * DAY:
        if t % DAY == 14 * 3600:   # a reset
            total = 0.0
        total = round(total + (rng.choice([0, 0, 0, 0.2, 0.5])), 1)
        daily[t] = total
        t += 1800 if rng.random() < 0.9 else MAX_GAP + 1800   # now and then a hole
    with storage.writer.transaction() as w:
        s = w.ecowitt_sensor(MAC, "rainfall")
        w.put(s, "30min", [{"ts": t, "daily": v} for t, v in daily.items()])
    assert read(storage, lambda r: r.rain_slots(s.id, "30min", t0, t0 + 3 * DAY)) == rain_slots(daily)


async def test_the_writers_own_thread_writes(storage):
    seen = {}

    def write(w):
        seen["thread"] = threading.current_thread().name
        w.set_meta("k", "v")
    await storage.write(write)
    assert seen["thread"].startswith("storage-write") and storage.reader.meta("k") == "v"


def test_the_fake_stations_first_day_ever_and_the_daily_records_labels(storage):
    d = date(2026, 1, 10)   # summer: Melbourne is 11 hours ahead, the UTC midnight is 11am
    first, last = utc_days(d, TZ)
    assert first == last and datetime.fromtimestamp(first, TZ).hour == 11
