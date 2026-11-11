"""intent: the reasoning effort from the words, and the few shortcuts the bot answers in code (docs/ecobot-mcp.md 4): the report,
"weather now" and a named reading's chart and a period, each a fixed call to a service tool. Everything else is the
model's."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from lib import intent, templates
from lib.alerts import pictures
from lib.service.names import Catalogue
from tests.fakes import EVERY_SOURCE

TZ = ZoneInfo("Australia/Melbourne")
NOW = datetime(2026, 9, 29, 14, 5, tzinfo=TZ)  # a Tuesday


def chart(text, catalogue=None):
    return intent.chart_call(text, NOW, TZ, catalogue)


def test_only_looking_ahead_and_descriptions_reason_a_little():
    assert intent.reasoning_effort("will it rain later?") == "low"
    assert intent.reasoning_effort("what was yesterday like?") == "low"
    assert intent.reasoning_effort("what was this week's high and low") == "none"
    assert intent.reasoning_effort("how much rain fell today") == "none"


def test_asking_to_think_try_or_reason_gives_medium_reasoning_to_any_question():
    for text in ("think about the wind this week", "try to work out why it was humid", "reason it through: hottest day", "thinking harder about yesterday",
                 "how hot was it, think", "what was yesterday like? think", "Try 3m", "estimate the hottest day this week",
                 "predict the average humidity for 30d", "what's your estimate of the week's rain", "an estimation of the wind 7d",
                 "give me a prediction for the week's high", "grind on the hottest day this week", "whirl it around: 7d wind"):
        assert intent.reasoning_effort(text) == "medium", text                      # medium wins over describe's low
    for text in ("what was this week's high and low", "thanks", "a thinner chart please", "I tried that yesterday"):
        assert intent.reasoning_effort(text) == "none", text


def test_how_one_reading_relates_to_another_gets_medium_thinking():
    for text in ("Is there a correlation between pressure and rainfall", "is there a link between humidity and rain",
                 "cross-reference pressure with rain", "cross reference wind and rain", "what's the connection between wind and temperature",
                 "is there a relationship between pressure and rain", "is rain related to pressure", "what is the relation between heat and pm2.5",
                 "does pressure affect the rain", "correlate wind and rain"):
        assert intent.reasoning_effort(text) == "medium", text
    for text in ("what was the pressure last month", "how much rain fell yesterday", "hottest day", "connect to the station"):
        assert intent.reasoning_effort(text) == "none", text


def test_analysis_across_days_or_readings_gets_low_thinking():
    for text in ("what was the hottest day that where it also rained", "how many days over 30 had rain",
                 "hottest day when it also rained?", "days when it was both windy and cold",
                 "how often does it rain on hot days", "was the coldest day also the wettest"):
        assert intent.reasoning_effort(text) == "low", text
    for text in ("what was this week's high and low", "how much rain fell yesterday", "weather 1m",
                 "what's the hottest day this year", "thanks!"):
        assert intent.reasoning_effort(text) == "none", text
    assert intent.reasoning_effort("will it rain later?") == "low"
    assert intent.reasoning_effort("what was yesterday like?") == "low"


def test_one_reasoning_step_down_for_asking_again_after_a_timeout():
    assert [intent.lower_effort(e) for e in ("high", "medium", "low", "none", "odd")] == ["medium", "low", "none", None, None]


def test_a_bare_status_or_report_asks_for_everything_and_nothing_else_does():
    for text in ("status", "report", "Report please", "give me the full report", "current report", "show what you've got",
                 "overview", "everything?", "what's the status", "sitrep", "SITREP please", "give me the sitrep"):
        assert intent.wants_report(text), text
        assert intent.read(text, NOW, EVERY_SOURCE).report
    for text in ("weather report for Tuesday", "report the humidity", "status of the rain alert", "report last week", "sitrep last week", "hello",
                 "how's the air?"):
        assert not intent.wants_report(text), text


def test_the_reports_calls_are_the_readings_now_the_pollen_and_the_next_three_days():
    assert intent.report_calls(EVERY_SOURCE) == [("current", {}), ("pollen", {}), ("forecast", {"period": "next 3 days"})]
    assert intent.report_calls(["AirGradient"]) == [("current", {})]
    assert not intent.read("report", NOW, []).report                  # nothing to report from: the model's
    assert not intent.read("report for last week", NOW, EVERY_SOURCE).report


def test_weather_now_is_the_stations_and_a_period_makes_it_the_models():
    for text in ("weather", "weather now", "Weather Now", "current weather", "ecowitt", "ecowitt now", "show me the weather now please"):
        r = intent.read(text, NOW, EVERY_SOURCE)
        assert r.weather_now and r.chart is None and r.shortcut, text
    for text in ("weather today", "how's the weather now", "weather tomorrow", "weather this week vs last week", "dashboard last week"):
        r = intent.read(text, NOW, EVERY_SOURCE)
        assert not (r.weather_now or r.report), text
    for text in ("dashboard", "gauges", "show me the dashboard"):                  # the Telegram dashboard is no more: the model's
        assert not intent.read(text, NOW, EVERY_SOURCE).shortcut, text
    assert not intent.read("weather now", NOW, ["AirGradient"]).weather_now   # (no station)


def test_a_named_readings_chart_and_a_period_is_summary_with_the_words_as_they_are():
    for text, readings, period in (("humidity chart 7d", ["humidity"], "7d"), ("chart rain this month", ["rain"], "this month"),
                                   ("humidity 7d", ["humidity"], "7d"), ("Temperature chart last month", ["temperature"], "last month"),
                                   ("indoor temperature chart 7d", ["indoor temperature"], "7d"), ("pm2.5 plot 3d", ["pm2.5"], "3d"),
                                   ("temperature and rain chart 7d", ["temperature", "rain"], "7d"), ("weather 30d", ["weather"], "30d"),
                                   ("co2 graph yesterday", ["co2"], "yesterday"),
                                   ("humidity, pressure chart 2 weeks", ["humidity", "pressure"], "2 weeks"),
                                   ("humidity 7d?", ["humidity"], "7d")):
        assert chart(text) == ("summary", {"readings": readings, "period": period, "chart": True}), text


def test_anything_beside_the_chart_is_the_models():
    for text in ("average temperature 7d", "plot uv and solar on two axes 7d", "lowest temperature on record", "highest humidity this week",
                 "temps this week", "chart weather", "humidity chart", "plot pressure this week: is a storm coming?", "weather all week",
                 "how hot is it", "rain today", "rain yesterday", "humidity past day", "temperature 12h", "humidity chart next week", "chart chart 7d", "rain chart 7d vs last week",
                 "temperature chart 99999 years ago", "hello", "what can you do", "pollen", "forecast"):
        assert chart(text) is None, text
    assert intent.read("humidity chart 7d", NOW, ["Pollen", "Forecast"]).chart is None   # (nothing to draw from)


def test_a_bare_reading_and_a_day_given_as_a_number_is_its_chart_as_before_the_swap():
    """"temperature 1d", "co2 24h": the chart of the last day (step 3 gate's intent drew these); a day in words ("yesterday", "past day")
    or less than a day ("12h", "today") is a figure to read, the model's."""
    for text, readings, period in (("temperature 1d", ["temperature"], "1d"), ("co2 1d", ["co2"], "1d"), ("pm2.5 1d", ["pm2.5"], "1d"),
                                   ("humidity 1d", ["humidity"], "1d"), ("temperature 24h", ["temperature"], "24h"),
                                   ("co2 1 day", ["co2"], "1 day")):
        assert chart(text) == ("summary", {"readings": readings, "period": period, "chart": True}), text
    for text in ("temperature yesterday", "temperature today", "co2 12h", "humidity past day", "pm2.5 last 24 hours"):
        assert chart(text) is None, text


def test_the_words_are_read_against_the_readings_the_service_holds():
    station_only = Catalogue.of(air=False)
    assert chart("co2 chart 7d") and chart("co2 chart 7d", station_only) is None


def test_every_button_and_alert_chart_is_a_fixed_call():
    calls = {text: chart(text) for text in [*templates.chart_sentences(), *pictures.CHARTS.values()]}
    assert calls == {
        "Rain chart 1d": ("summary", {"readings": ["rain"], "period": "1d", "chart": True}),
        "Weather chart 1d": ("summary", {"readings": ["weather"], "period": "1d", "chart": True}),
        "Temperature chart 1d": ("summary", {"readings": ["temperature"], "period": "1d", "chart": True}),
        "Humidity chart 1d": ("summary", {"readings": ["humidity"], "period": "1d", "chart": True}),
        "Air quality all metrics 1d": ("summary", {"readings": ["air"], "period": "1d", "chart": True}),
        "Particulates chart 1d": ("summary", {"readings": ["pm2.5", "pm10", "pm1"], "period": "1d", "chart": True}),
        "Wind chart 1d": ("summary", {"readings": ["wind"], "period": "1d", "chart": True}),
        "UV chart 1d": ("summary", {"readings": ["uv"], "period": "1d", "chart": True})}


@pytest.mark.parametrize("text", ["status", "humidity 7d", "how hot is it", "thanks", "will it rain later?"])
def test_the_effort_is_read_for_every_message(text):
    assert intent.read(text, NOW, EVERY_SOURCE).effort == intent.reasoning_effort(text)
