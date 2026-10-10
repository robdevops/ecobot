import json
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace as NS

import httpx

from lib.pollen import LEVEL_EMOJI, LEVELS, Pollen, parse_melbourne_pollen
from lib.warm import in_sync_hours
from tests.fakes import TZ, config

SEASON_DAY = date(2026, 11, 10)   # a day in the pollen season (October to December), whatever day the tests run


def page(grass="Low", day=None, asthma="Low", season=True, district_extra=""):
    day = day or SEASON_DAY
    asthma_block = f"""<h2>Thunderstorm Asthma Forecast</h2><ul><li>Central</li><li>{asthma}</li><li>Wimmera</li><li>High</li></ul>
        <p>Note: forecasts are issued daily.</p><p>Last updated: 1 Oct 2026 4:00pm</p>""" if season else ""
    return f"""<html><head><script>var Central = 'High';</script></head><body>
        <div class="pollen-level level-low"><span id="plevel">{grass}</span></div><span id="pdate">{day:%A, %B %d, %Y}</span>
        <h2>Victorian District Grass Pollen Forecast</h2><ul><li>Central</li><li>{grass}</li><li>Mallee</li><li>Moderate</li>{district_extra}</ul>
        {asthma_block}<h3>Recent News</h3><p>Central</p></body></html>"""


def transport(html, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.headers.get("if-none-match") == "v1":
            return httpx.Response(304)
        return httpx.Response(200, text=html(), headers={"etag": "v1"})
    return httpx.MockTransport(handler)


def make(tmp_path, html=None, hour=12, **over):
    """A Pollen source whose clock says `hour` (the sync hours are 6 am to 6 pm), and the requests it made."""
    calls = []
    pollen = Pollen(config(tmp_path, pollen=True, **over), transport(html or (lambda: page()), calls))
    pollen.now = lambda: datetime.combine(SEASON_DAY, dtime(hour, 0))
    return pollen, calls


def test_the_page_is_parsed_into_levels_districts_and_the_update_time():
    got = parse_melbourne_pollen(page(grass="High", asthma="High"))
    assert got["melbourne_grass"] == "High" and got["melbourne_date"] == SEASON_DAY
    assert got["district_grass"] == {"Central": "High", "Mallee": "Moderate"}
    assert got["thunderstorm_asthma"] == {"Central": "High", "Wimmera": "High"}
    assert got["asthma_updated"] == "1 Oct 2026 4:00pm"


def test_outside_the_asthma_season_there_is_no_asthma_forecast_and_an_unknown_page_gives_nothing():
    assert parse_melbourne_pollen(page(season=False))["thunderstorm_asthma"] == {}
    nothing = parse_melbourne_pollen("<html><body><p>Under maintenance</p></body></html>")
    assert nothing["melbourne_grass"] is None and nothing["district_grass"] == {} and nothing["thunderstorm_asthma"] == {}


async def test_the_lines_have_a_level_emoji_and_never_name_the_district(tmp_path):
    pollen, _ = make(tmp_path, lambda: page(grass="High", asthma="High"))
    await pollen.start()
    assert pollen.lines() == ["Grass pollen: 🔴 High", "Thunderstorm asthma risk: 🔴 High"]
    out = json.loads(await pollen.handle({}))
    assert out["lines"] == pollen.lines() and "Central" not in json.dumps(out) and out["asthma_updated"] == "1 Oct 2026 4:00pm"
    await pollen.close()


async def test_a_forecast_for_another_day_and_a_missing_asthma_forecast_are_said_so(tmp_path):
    pollen, _ = make(tmp_path, lambda: page(grass="Moderate", day=SEASON_DAY + timedelta(days=1), season=False))
    await pollen.start()
    assert pollen.lines() == ["Grass pollen: 🟠 Moderate (forecast for Wed 11 Nov)"]
    assert "asthma_note" in json.loads(await pollen.handle({}))
    await pollen.close()


async def test_the_page_is_fetched_conditionally_and_not_again_while_fresh(tmp_path):
    pollen, calls = make(tmp_path)
    await pollen.start()
    assert len(calls) == 1 and "if-none-match" not in calls[0].headers and "ecobot" in calls[0].headers["user-agent"]
    await pollen.warm(False)                               # a question: the data is young, no request
    assert len(calls) == 1
    await pollen.warm(True)                                # the timer: asks again with the validator, gets a 304
    assert len(calls) == 2 and calls[1].headers["if-none-match"] == "v1"
    assert pollen.lines() == ["Grass pollen: 🟢 Low", "Thunderstorm asthma risk: 🟢 Low"]
    await pollen.close()


async def test_nothing_is_fetched_outside_6am_to_6pm_except_once_when_nothing_is_cached(tmp_path):
    pollen, calls = make(tmp_path, hour=22)
    await pollen.start()
    assert len(calls) == 1                                 # empty cache: one fetch, even at night
    await pollen.warm(True)
    await pollen.warm(False)
    assert len(calls) == 1                                 # then none until the morning
    pollen.now = lambda: datetime.combine(SEASON_DAY, dtime(6, 0))
    await pollen.warm(True)
    assert len(calls) == 2
    await pollen.close()


def test_the_sync_hours_are_6am_to_6pm():
    day = datetime(2026, 10, 1)
    assert [in_sync_hours(datetime.combine(day, dtime(h, m))) for h, m in ((5, 59), (6, 0), (17, 59), (18, 0))] == [False, True, True, False]


async def test_a_page_that_cannot_be_fetched_does_not_stop_the_source_starting(tmp_path):
    def handler(request):
        return httpx.Response(500)
    pollen = Pollen(config(tmp_path, pollen=True), httpx.MockTransport(handler))
    pollen.now = lambda: datetime.combine(SEASON_DAY, dtime(12, 0))
    await pollen.start()
    assert pollen.lines() == [] and "No pollen levels" in await pollen.handle({})
    await pollen.close()


def test_pollen_words_need_data_and_a_bare_pollen_question_is_fetched_without_the_model():
    from lib import intent
    now = datetime(2026, 10, 1, 12, 0)
    for text in ("pollen", "Hay fever", "thunderstorm asthma risk"):
        assert intent.read(text, now, True, True, True).fast[0] == "pollen_asthma"
    assert intent.read("pollen", now, True, True, False).fast is None
    for text in ("is the pollen bad today?", "any asthma risk"):
        assert intent.read(text, now, True, True, True).needs_data


def make_on(tmp_path, day, hour=12, html=None):
    """A Pollen source whose clock says this day (the season is October to December)."""
    pollen, calls = make(tmp_path, html)
    pollen.now = lambda: datetime.combine(day, dtime(hour, 0))
    return pollen, calls


def test_the_season_is_october_to_december(tmp_path):
    pollen = Pollen(config(tmp_path, pollen=True))
    for day, expected in ((date(2026, 9, 30), False), (date(2026, 10, 1), True), (date(2026, 12, 31), True), (date(2027, 1, 1), False),
                          (date(2026, 7, 15), False)):
        pollen.now = lambda day=day: datetime.combine(day, dtime(12, 0))
        assert pollen.in_season() is expected, day


async def test_out_of_season_nothing_is_fetched_not_even_at_start_with_an_empty_cache(tmp_path):
    pollen, calls = make_on(tmp_path, date(2027, 2, 10))
    await pollen.start()
    assert await pollen.warm(True) == "Pollen out of season" and await pollen.warm(False) == "Pollen out of season"
    assert calls == [] and pollen.lines() == []
    out = json.loads(await pollen.handle({}))
    assert out == {"error": "Pollen and thunderstorm asthma forecasts only run from October to December."} and calls == []
    await pollen.close()


async def test_last_seasons_page_is_not_shown_and_the_report_leaves_the_block_out(tmp_path):
    from lib import report
    pollen, _ = make_on(tmp_path, date(2026, 12, 20), html=lambda: page(grass="High", day=date(2026, 12, 20)))
    await pollen.start()
    assert pollen.lines()[0].startswith("Grass pollen: 🔴 High")
    await pollen.close()
    january, calls = make_on(tmp_path, date(2027, 1, 5))                # the saved page is still in the database
    await january.start()
    assert january.data is not None and january.lines() == [] and january.current()["grass"] is None and calls == []
    result = await january.handle({"cached": True})
    assert "error" in json.loads(result)
    assert "Pollen" not in report.report({"pollen_asthma": result})                       # no block in the report
    await january.close()


async def test_the_first_warm_on_the_first_of_october_fetches_without_a_restart(tmp_path):
    clock = {"day": date(2026, 9, 30)}
    pollen, calls = make(tmp_path)
    pollen.now = lambda: datetime.combine(clock["day"], dtime(12, 0))
    await pollen.start()
    await pollen.warm(True)
    assert calls == []
    clock["day"] = date(2026, 10, 1)
    await pollen.warm(True)
    assert len(calls) == 1 and pollen.lines()
    await pollen.close()


def test_no_data_is_not_a_level_so_it_reads_as_no_forecast():
    got = parse_melbourne_pollen(page(grass="No data", asthma="No data"))
    assert got["melbourne_grass"] is None and got["thunderstorm_asthma"] == {"Wimmera": "High"}   # Central has none: Wimmera is another district
    assert LEVELS == ("Low", "Moderate", "High") and [LEVEL_EMOJI[k] for k in LEVELS] == ["🟢", "🟠", "🔴"]
