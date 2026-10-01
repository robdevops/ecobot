import json
from datetime import datetime, time as dtime, timedelta

import httpx

from lib import intent
from lib.forecast import Forecast
from lib.forecast.source import FORECAST_REFRESH_SECONDS
from lib.pollen import Pollen
from lib.snapshots import KEEP_SECONDS, Snapshots
from lib.warm import safely
from tests.fakes import TZ, config
from tests.test_forecast import transport as forecast_transport
from tests.test_pollen import make as make_pollen, page, transport as pollen_transport


def test_a_repeat_only_moves_last_seen_and_a_change_adds_a_row(tmp_path):
    store = Snapshots.open(tmp_path / "s.sqlite")
    assert store.save("pollen", {"level": "Low"}, now=100) is True
    assert store.save("pollen", {"level": "Low"}, now=200) is False
    assert store.latest("pollen") == (200, {"level": "Low"}) and store.count("pollen") == 1
    assert store.save("pollen", {"level": "High"}, now=300) is True
    assert store.count("pollen") == 2 and store.latest("pollen") == (300, {"level": "High"}) and store.latest("forecast") is None
    store.put("k", "v")
    assert store.get("k") == "v" and store.get("missing") is None
    store.save("pollen", {"level": "Low"}, now=300 + KEEP_SECONDS + 1)     # rows not seen for over a year are dropped
    assert store.count("pollen") == 1
    store.close()


def test_the_connection_is_shared_and_closed_by_the_last_user(tmp_path):
    a, b = Snapshots.open(tmp_path / "s.sqlite"), Snapshots.open(tmp_path / "s.sqlite")
    assert a is b
    a.close()
    b.put("k", "v")                                          # still open for the other user
    b.close()
    assert Snapshots.open(tmp_path / "s.sqlite") is not a


async def test_a_restart_in_the_evening_starts_from_the_saved_pollen_page_without_fetching(tmp_path):
    first, calls = make_pollen(tmp_path, lambda: page(grass="High", asthma="Extreme"))
    await first.start()
    assert len(calls) == 1
    await first.close()
    again, more = make_pollen(tmp_path, lambda: page(grass="Low"), hour=22)
    await again.start()
    assert more == [] and again.lines() == ["Grass pollen: 🟠 High", "Thunderstorm asthma risk: 🔴 Extreme"]
    assert again.current()["fetched_at"] == first.fetched_at
    again.now = lambda: datetime.combine(datetime.now(TZ).date(), dtime(9, 0))
    await again.warm(True)                                   # next morning: asks with the saved validator
    assert len(more) == 1 and more[0].headers["if-none-match"] == "v1"
    assert again.lines()[0].startswith("Grass pollen: 🟠 High")      # a 304: the saved page still stands
    await again.close()


async def test_the_pollen_history_keeps_one_row_per_change(tmp_path):
    level = {"now": "Low"}
    pollen, _ = make_pollen(tmp_path, lambda: page(grass=level["now"]).replace('<span id="pdate">', f'<span data-v="{level["now"]}" id="pdate">'))
    await pollen.start()
    await pollen.warm(True)
    assert pollen.store.count("pollen") == 1
    level["now"] = "High"
    pollen._validators = {}                                  # the site has a new page
    await pollen.warm(True)
    assert pollen.store.count("pollen") == 2
    await pollen.close()


async def test_the_forecast_survives_a_restart_and_a_night_start_needs_no_request(tmp_path):
    calls = []
    first = Forecast(config(tmp_path, forecast=True, forecast_lat=-37.8, forecast_lon=144.9), None, forecast_transport(calls))
    first.now = lambda: datetime.combine(datetime.now(TZ).date(), dtime(12, 0))
    await first.start()
    lines = first.lines(3)
    await first.close()
    calls.clear()
    again = Forecast(config(tmp_path, forecast=True, forecast_lat=-37.8, forecast_lon=144.9), None, forecast_transport(calls))
    again.now = lambda: datetime.combine(datetime.now(TZ).date(), dtime(22, 0))
    await again.start()
    assert calls == [] and again.lines(3) == lines and again.tag == "BOM" and again.store.count("forecast") == 1
    await again.close()


async def test_the_forecast_is_kept_warm_every_15_minutes_and_only_both_failing_is_logged(tmp_path, caplog):
    assert FORECAST_REFRESH_SECONDS == 15 * 60
    caplog.set_level("DEBUG")
    forecast = Forecast(config(tmp_path, forecast=True, forecast_lat=-37.8, forecast_lon=144.9), None, forecast_transport([], bom_ok=False))
    forecast.now = lambda: datetime.combine(datetime.now(TZ).date(), dtime(12, 0))
    await safely(forecast.warm, True)                       # the BOM is down, Open-Meteo answers: nothing above debug
    assert forecast.tag == "Open-Meteo" and not [r for r in caplog.records if r.name.startswith("lib") and r.levelname != "DEBUG"]

    def both_down(request):
        return httpx.Response(503)
    broken = Forecast(config(tmp_path, forecast=True, forecast_lat=-37.8, forecast_lon=144.9), None, httpx.MockTransport(both_down))
    broken.now = forecast.now
    caplog.clear()
    await safely(broken.warm, True)                         # both down: one warning, from the warmer
    assert [r.levelname for r in caplog.records if r.name.startswith("lib") and r.levelname != "DEBUG"] == ["WARNING"] and "Forecast.warm failed" in caplog.text
    await forecast.close()
    await broken.close()


async def test_the_report_always_uses_the_cache_for_pollen_and_forecast(tmp_path):
    calls = []
    pollen = Pollen(config(tmp_path, pollen=True), pollen_transport(lambda: page(), calls))
    forecast = Forecast(config(tmp_path, forecast=True, forecast_lat=-37.8, forecast_lon=144.9), None, forecast_transport(calls))
    assert not pollen.wants("report") and not forecast.wants("sitrep") and pollen.wants("pollen today") and forecast.wants("forecast")
    for tool, args in intent.report_calls(False, False, True, True):
        out = json.loads(await {"pollen_asthma": pollen.handle, "weather_forecast": forecast.handle}[tool](args))
        assert "error" in out                                # nothing cached yet: the report goes without it, no request made
    assert calls == []
    await pollen.close()
    await forecast.close()
