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
