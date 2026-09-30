import time

from lib.ecowitt import store


def test_subtract_and_merge():
    assert store.subtract((0, 100), [(10, 20), (30, 40)]) == [(0, 9), (21, 29), (41, 100)]
    assert store.subtract((0, 10), [(0, 10)]) == []
    assert store.merge([(0, 10), (11, 20), (30, 40)]) == [(0, 20), (30, 40)]


def make(tmp_path, units=None):
    return store.HistoryCache(tmp_path / "c.sqlite", units or {"u": "C"})


def data(*stamps, field="temperature"):
    return {"outdoor": {field: {"unit": "C", "list": {str(t): "1.5" for t in stamps}}}}


def test_only_settled_readings_are_kept(tmp_path):
    cache, now = make(tmp_path), int(time.time())
    old, fresh = now - 5 * 86400, now - 600
    cache.store("M", "5min", ["outdoor"], data(old, old + 300, fresh), old - 300, now)
    loaded = cache.load("M", "5min", ["outdoor"], 0, now)
    assert sorted(loaded["outdoor"]["temperature"]["list"]) == [str(old), str(old + 300)]
    assert cache.missing("M", "5min", "outdoor", old, old + 300) == []
    assert cache.missing("M", "5min", "outdoor", fresh, now)  # the unsettled tail is still to fetch


def test_empty_old_ranges_are_remembered(tmp_path):
    cache, now = make(tmp_path), int(time.time())
    start, end = now - 30 * 86400, now - 29 * 86400
    cache.store("M", "30min", ["outdoor"], {}, start, end)
    assert cache.missing("M", "30min", "outdoor", start, end) == []


def test_changed_units_clear_the_cache(tmp_path):
    cache, now = make(tmp_path), int(time.time())
    old = now - 5 * 86400
    cache.store("M", "5min", ["outdoor"], data(old), old, old + 300)
    cache.close()
    reopened = make(tmp_path, {"u": "F"})
    assert reopened.load("M", "5min", ["outdoor"], 0, now) == {}


def test_hot_store_reuses_fresh_responses_up_to_now():
    hot, now = store.HotStore(), int(time.time())
    hot.put("M", "5min", "outdoor", now - 3600, now, {"f": 1})
    assert hot.get("M", "5min", "outdoor", now - 1800, now + 100) == {"f": 1}   # reaches the present
    assert hot.get("M", "5min", "outdoor", now - 7200, now) is None            # starts before what we hold


def test_a_metric_ecowitt_adds_later_makes_the_days_held_be_fetched_again(tmp_path):
    cache, now = make(tmp_path), int(time.time())
    old = now - 10 * 86400
    cache.store("M", "30min", ["outdoor"], data(old), old, old + 3600)
    assert cache.missing("M", "30min", "outdoor", old, old + 3600) == []                 # held
    cache.store("M", "30min", ["outdoor"], data(old + 7200, field="temperature") | {"outdoor": {
        **data(old + 7200)["outdoor"], "vpd": {"unit": "kPa", "list": {str(old + 7200): "0.8"}}}}, old + 7200, old + 7300)
    assert cache.missing("M", "30min", "outdoor", old, old + 3600)                       # the new metric: the earlier days are asked for again
    cache.store("M", "30min", ["outdoor"], data(old), old, old + 3600)
    assert cache.missing("M", "30min", "outdoor", old, old + 3600) == []                 # and once the vpd-less response repeats, no loop


def test_vpd_reported_in_inhg_is_stored_and_read_in_kpa_and_old_rows_are_converted(tmp_path):
    from lib.ecowitt.api import fix_units
    fixed = fix_units({"outdoor": {"vpd": {"unit": "inHg", "list": {"1": "0.261"}}, "temperature": {"unit": "C", "list": {"1": "20"}},
                                   "vpd_high": {"unit": "inHg", "value": "0.5"}}})
    assert fixed["outdoor"]["vpd"] == {"unit": "kPa", "list": {"1": "0.884"}} and fixed["outdoor"]["vpd_high"] == {"unit": "kPa", "value": "1.693"}
    assert fixed["outdoor"]["temperature"]["unit"] == "C"
    cache, now = make(tmp_path), int(time.time())
    old = now - 5 * 86400
    cache.db.execute("INSERT INTO fields VALUES ('M','30min','outdoor','vpd','inHg')")
    cache.db.execute("INSERT INTO points VALUES ('M','30min','outdoor','vpd',?, '0.261')", (old,))
    cache.db.commit()
    cache.close()
    again = make(tmp_path)                                                      # opened again: the old rows are converted once
    loaded = again.load("M", "30min", ["outdoor"], 0, now)["outdoor"]["vpd"]
    assert loaded["unit"] == "kPa" and loaded["list"] == {str(old): "0.884"}
    again.close()
    assert make(tmp_path).load("M", "30min", ["outdoor"], 0, now)["outdoor"]["vpd"]["list"] == {str(old): "0.884"}   # and not again
