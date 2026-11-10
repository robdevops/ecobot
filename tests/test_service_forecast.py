"""The `forecast` tool (lib/service/forecast.py): the days ahead in words, read here; today's weather_forecast's lines, each day's
figures, the bot's forecast section word for word, what is held when the request says not to fetch, and refusals that teach."""

import json
from datetime import date, datetime, time as dtime, timedelta

import httpx
import pytest

from tests import report_words as report   # (the bot's words before step 2: the oracle)
from lib.forecast import Forecast
from lib.service.common import FRESH, Refusal
from lib.service.context import Context
from lib.service.forecast import DESCRIPTION, PARAMETERS, days_text, read_ahead, tools
from lib.timeutil import day_text
from tests.fakes import TZ, config

NOW = datetime(2026, 11, 10, 14, 5, tzinfo=TZ)   # Tue 10 Nov 2026 2:05pm, as the plan's periods table is asked


def got(text: str) -> dict:
    return json.loads(text)


def ahead(words, now=NOW) -> tuple[str, str]:
    period = read_ahead(words, now, TZ)
    return period.text, f"{period.first_day} {period.last_day}"


# ---------- the days ahead, in words ----------
@pytest.mark.parametrize("words, text, days", [
    (None, "the next 7 days: Tue 10 – Mon 16 Nov 2026", "2026-11-10 2026-11-16"),
    ("today", "today: Tue 10 Nov 2026", "2026-11-10 2026-11-10"),
    ("Tonight", "today: Tue 10 Nov 2026", "2026-11-10 2026-11-10"),
    ("tomorrow", "tomorrow: Wed 11 Nov 2026", "2026-11-11 2026-11-11"),
    ("for tomorrow", "tomorrow: Wed 11 Nov 2026", "2026-11-11 2026-11-11"),
    ("the day after tomorrow", "the day after tomorrow: Thu 12 Nov 2026", "2026-11-12 2026-11-12"),
    ("next 3 days", "the next 3 days: Tue 10 – Thu 12 Nov 2026", "2026-11-10 2026-11-12"),
    ("in the next three days", "the next 3 days: Tue 10 – Thu 12 Nov 2026", "2026-11-10 2026-11-12"),
    ("3d", "the next 3 days: Tue 10 – Thu 12 Nov 2026", "2026-11-10 2026-11-12"),
    ("1 day", "today: Tue 10 Nov 2026", "2026-11-10 2026-11-10"),
    ("the next 7 days", "the next 7 days: Tue 10 – Mon 16 Nov 2026", "2026-11-10 2026-11-16"),
    ("this week", "the next 7 days: Tue 10 – Mon 16 Nov 2026", "2026-11-10 2026-11-16"),
    ("the week ahead", "the next 7 days: Tue 10 – Mon 16 Nov 2026", "2026-11-10 2026-11-16"),
    ("this weekend", "this weekend: Sat 14 – Sun 15 Nov 2026", "2026-11-14 2026-11-15"),
    ("over the weekend", "this weekend: Sat 14 – Sun 15 Nov 2026", "2026-11-14 2026-11-15"),
    ("next week", "next week: Mon 16 – Sun 22 Nov 2026", "2026-11-16 2026-11-22"),
    ("Saturday", "Saturday: Sat 14 Nov 2026", "2026-11-14 2026-11-14"),
    ("on Sat", "Saturday: Sat 14 Nov 2026", "2026-11-14 2026-11-14"),
    ("Tuesday", "Tuesday: Tue 10 Nov 2026", "2026-11-10 2026-11-10"),          # a bare weekday is today when it is one
    ("next Tuesday", "Tuesday: Tue 17 Nov 2026", "2026-11-17 2026-11-17"),
    ("2026-11-12", "Thu 12 Nov 2026", "2026-11-12 2026-11-12"),
])
def test_the_days_ahead_are_read_from_the_words_and_say_how(words, text, days):
    assert ahead(words) == (text, days)


def test_the_weekend_from_a_saturday_or_a_sunday_starts_today():
    saturday, sunday = datetime(2026, 11, 14, 9, tzinfo=TZ), datetime(2026, 11, 15, 9, tzinfo=TZ)
    assert ahead("this weekend", saturday) == ("this weekend: Sat 14 – Sun 15 Nov 2026", "2026-11-14 2026-11-15")
    assert ahead("this weekend", sunday) == ("this weekend: Sun 15 Nov 2026", "2026-11-15 2026-11-15")
    assert ahead("the next 7 days", datetime(2026, 12, 28, 9, tzinfo=TZ))[0] == "the next 7 days: Mon 28 Dec 2026 – Sun 3 Jan 2027"


def test_a_period_is_whole_days_on_the_stations_clock():
    period = read_ahead("this weekend", NOW, TZ)
    assert period.asked == "this weekend" and period.cut is None
    assert period.start == datetime(2026, 11, 14, tzinfo=TZ) and period.end == datetime(2026, 11, 16, tzinfo=TZ)
    assert read_ahead(None, NOW, TZ).asked == "the next 7 days"
    assert days_text(date(2026, 11, 10), date(2026, 11, 10)) == "Tue 10 Nov 2026"


def test_the_past_is_refused_pointing_to_summary_and_every_form_says_what_it_means_now():
    with pytest.raises(Refusal) as e:
        read_ahead("yesterday", NOW, TZ)
    assert "'yesterday' reads as a period that has been measured (yesterday: Mon 9 Nov 2026)" in e.value.message
    assert "use summary" in e.value.message and "It is now Tue 10 Nov 2026 2:05pm." in e.value.message
    forms = e.value.teach["forms"]
    assert forms["tomorrow"] == "tomorrow: Wed 11 Nov 2026"
    assert forms["this weekend, the weekend (Saturday and Sunday; from today when it is one)"] == "this weekend: Sat 14 – Sun 15 Nov 2026"
    assert forms["a date, YYYY-MM-DD"] == "Thu 12 Nov 2026"
    with pytest.raises(Refusal) as e:
        read_ahead("2026-11-01", NOW, TZ)
    assert e.value.message.startswith("'2026-11-01' is in the past (Sun 1 Nov 2026)") and "use summary" in e.value.message
    with pytest.raises(Refusal) as e:
        read_ahead("a fortnight from now", NOW, TZ)
    assert e.value.message.startswith("'a fortnight from now' isn't a period the forecast can read") and len(e.value.teach["forms"]) == 9
    with pytest.raises(Refusal):
        read_ahead("in 3 days", NOW, TZ)   # (one day, three days from now: not "the next 3 days")
    with pytest.raises(Refusal):
        read_ahead("next 0 days", NOW, TZ)


# ---------- the tool ----------
def open_meteo(calls, days=7):
    """Open-Meteo: `days` days from today (the station's), each a little different; its ensemble's chance for every other day."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        today = datetime.now(TZ).date()
        if request.url.host.startswith("ensemble"):
            hourly = {"time": [f"{today + timedelta(days=n)}T{h:02d}:00" for n in range(days) for h in range(24)]}
            members = [[(3.0 if n % 2 else 0.0) if h == 0 else 0.0 for n in range(days) for h in range(24)] for _ in range(4)]
            members[0] = [0.0] * len(hourly["time"])   # one member dry every day: 75% on the wet ones
            return httpx.Response(200, json={"hourly": {**hourly, "precipitation": members[0],
                                                       **{f"precipitation_member{m:02d}": s for m, s in enumerate(members[1:], 1)}}})
        return httpx.Response(200, json={"daily": {
            "time": [str(today + timedelta(days=n)) for n in range(days)], "temperature_2m_min": [10.5 + n for n in range(days)],
            "temperature_2m_max": [16 + n for n in range(days)], "precipitation_probability_max": [10 * n for n in range(days)],
            "weather_code": [[63, 0, 95, 2, 80, 3, 61][n % 7] for n in range(days)]}})
    return httpx.MockTransport(handler)


def make(tmp_path, days=7, place="Melbourne"):
    """(the forecast source, its requests, the context the tool is built over), all on the clock at noon today."""
    calls = []
    forecast = Forecast(config(tmp_path, forecast=True, forecast_lat=-37.8, forecast_lon=144.9, place=place), None, open_meteo(calls, days))
    noon = datetime.combine(datetime.now(TZ).date(), dtime(12, 0))
    forecast.now = lambda: noon
    ctx = Context(forecast=forecast, tz=TZ, clock=lambda: noon.replace(tzinfo=TZ).timestamp())
    return forecast, calls, ctx


def tool(ctx):
    (it,) = tools(ctx)
    return it


async def test_the_lines_are_weather_forecasts_and_each_day_has_its_figures(tmp_path):
    forecast, calls, ctx = make(tmp_path)
    forecast_tool = tool(ctx)
    assert forecast_tool.name == "forecast" and forecast_tool.annotations == {"readOnlyHint": True, "openWorldHint": True}
    out = got(await forecast_tool.call({}))
    assert calls == ["api.open-meteo.com", "ensemble-api.open-meteo.com"]   # nothing held: fetched, as weather_forecast does
    old = got(await forecast.handle({}))
    assert out["lines"] == old["lines"] and len(out["lines"]) == 7 and out["lines"][0].startswith("Today: 🌧️ Rain. 11–16°C")
    today = forecast.now().date()
    assert out["period"]["asked"] == "the next 7 days" and out["period"]["text"].startswith("the next 7 days: ")
    assert out["period"]["start"] == datetime.combine(today, dtime(), tzinfo=TZ).isoformat()
    assert out["days"][0] == {"date": today.isoformat(), "min": 10.5, "max": 16, "summary": "🌧️ Rain.", "rain_chance": 0,
                              "rain_1mm_pct": 0}
    assert out["days"][1] == {"date": (today + timedelta(days=1)).isoformat(), "min": 11.5, "max": 17, "summary": "☀️ Clear.",
                              "rain_chance": 10, "rain_1mm_pct": 75}
    assert out["place"] == "Melbourne" and out["source"] == "Open-Meteo" and out["fresh"] == FRESH
    fetched = datetime.fromtimestamp(int(forecast.fetched_at), TZ)
    assert out["fetched_at"] == fetched.isoformat() and out["as_of"]["time"] == fetched.isoformat()
    assert list(out) == ["period", "as_of", "fresh", "text", "lines", "days", "place", "source", "fetched_at"]
    for n in range(1, 8):   # "next N days" is weather_forecast's `days: N`, line for line
        assert got(await forecast_tool.call({"period": f"next {n} days"}))["lines"] == got(await forecast.handle({"days": n}))["lines"]
    await forecast.close()


async def test_a_day_ahead_has_its_own_line_as_weather_forecast_writes_it(tmp_path):
    forecast, _, ctx = make(tmp_path)
    week = got(await forecast.handle({"days": 7}))["lines"]
    tomorrow = got(await tool(ctx).call({"period": "tomorrow"}))
    assert tomorrow["lines"] == [week[1]] and tomorrow["period"]["text"].startswith("tomorrow: ")
    day = forecast.now().date() + timedelta(days=4)
    named = got(await tool(ctx).call({"period": f"{day:%A}"}))
    assert named["lines"] == [week[4]] and named["days"][0]["date"] == day.isoformat()
    await forecast.close()


async def test_its_text_is_the_bots_forecast_section_word_for_word(tmp_path):
    forecast, _, ctx = make(tmp_path)
    today = forecast.now().date()
    for words, days in (("next 3 days", 3), ("the next 7 days", 7), ("today", 1)):
        out = got(await tool(ctx).call({"period": words}))
        old = await forecast.handle({"days": days, "cached": True})
        assert out["text"] == report.lookup("forecast", old, [], [], today) == report.report({"weather_forecast": old}, today)
    assert out["text"].startswith(f"Forecast (Melbourne, {report._stamp(json.loads(old), today)})\n• Today: ")
    later = today + timedelta(days=1)   # asked the next day, the time it was fetched has its day
    assert got(await tool(Context(forecast=forecast, tz=TZ, clock=lambda: datetime.combine(later, dtime(9), tzinfo=TZ).timestamp()))
               .call({"period": "today"}))["text"].startswith(f"Forecast (Melbourne, {report._stamp(json.loads(old), later)})\n• Today: ")
    await forecast.close()


async def test_with_no_place_the_heading_has_only_the_time(tmp_path):
    forecast, _, ctx = make(tmp_path, place="")
    out = got(await tool(ctx).call({"period": "today"}))
    old = await forecast.handle({"days": 1})
    assert "place" not in out and out["text"] == report.lookup("forecast", old, [], [], forecast.now().date())
    await forecast.close()


async def test_with_fetch_false_it_answers_from_what_is_held_and_fetches_nothing(tmp_path):
    forecast, calls, ctx = make(tmp_path)
    out = got(await tool(ctx).call({}, {"fetch": False}))
    assert out == {"error": "No forecast is available right now."} and calls == []   # (weather_forecast's words, with cached)
    assert got(await forecast.handle({"cached": True})) == out
    await forecast.warm(True)
    calls.clear()
    held = got(await tool(ctx).call({"period": "tomorrow"}, {"fetch": False}))
    assert len(held["lines"]) == 1 and calls == []
    await forecast.close()


async def test_days_beyond_the_forecast_are_said_so_or_refused_with_how_far_it_reaches(tmp_path):
    forecast, _, ctx = make(tmp_path, days=3)
    today = forecast.now().date()
    week = got(await tool(ctx).call({"period": "this week"}))
    assert len(week["lines"]) == 3 and week["note"] == \
        f"The forecast reaches only {day_text(today + timedelta(days=2), year=False)}: the days after it aren't forecast yet."
    far, reaches = today + timedelta(days=5), today + timedelta(days=2)
    out = got(await tool(ctx).call({"period": far.isoformat()}))
    assert out["error"] == f"The days asked for ({days_text(far, far)}) are beyond the forecast, which reaches {day_text(reaches)}: ask " \
        "for days up to then (the next 7 days is all of it)."
    assert out["reaches"] == reaches.isoformat()
    await forecast.close()


async def test_refusals_teach_the_call_that_works(tmp_path):
    forecast, calls, ctx = make(tmp_path)
    out = got(await tool(ctx).call({"period": "last week"}))
    assert "reads as a period that has been measured" in out["error"] and "use summary" in out["error"] and "forms" in out
    out = got(await tool(ctx).call({"start": "2026-11-12"}))
    assert out["error"].startswith("The forecast takes `period` in words, not `start` and `end`") and "tomorrow" in out["forms"]
    assert calls == []   # (nothing is fetched for a call that is refused)
    await forecast.close()


def test_it_is_offered_only_with_a_forecast_and_its_description_carries_the_rules():
    assert tools(Context(tz=TZ)) == []
    assert len(DESCRIPTION) < 1500 and "outlook" in DESCRIPTION and "summary" in DESCRIPTION and '{"period": "tomorrow"}' in DESCRIPTION
    assert '"weather week", "ecowitt week": the past week, summary' in DESCRIPTION and "Only what is ahead is forecast." in DESCRIPTION
    assert list(PARAMETERS["properties"]) == ["period"] and "Default: the next 7 days." in PARAMETERS["properties"]["period"]["description"]
