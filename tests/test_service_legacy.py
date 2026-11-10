"""The service's tools registered (lib/service/__init__.py), and the bot's old calls read as the service's (lib/service/legacy.py)."""

from datetime import datetime
from types import SimpleNamespace as NS

from lib import intent
from lib.service import ORDER, Service, build, legacy
from lib.service.common import AS_DRAWN, MAX_CHART_POINTS, request_meta
from lib.service.context import Context
from lib.tools import Turn
from tests.fakes import TZ

NOW = datetime(2026, 11, 10, 14, 5)   # Tue 10 Nov 2026 2:05pm


def test_the_ten_are_built_in_order_each_where_its_source_is_and_found_by_name():
    station = NS(name="Ecowitt", tz=TZ, storage=None, latitude=-37.8)
    everything = build(Context(eco=station, air=NS(name="AirGradient", tz=TZ), forecast=NS(), pollen=NS(), tz=TZ))
    assert [t.name for t in everything] == list(ORDER)
    assert [t.name for t in build(Context(tz=TZ))] == ["summary", "readings", "relate", "scan", "about"]   # no source: what needs none
    service = Service(everything)
    assert service["summary"] is service.get("summary") and "days" in service and service.get("nothing") is None
    assert [t.name for t in service] == list(ORDER) and service.held(["a"]) == ["a"]   # (no context: every name is held)


def test_the_bot_asks_for_its_charts_as_drawn():
    assert request_meta({"chart_points": AS_DRAWN})["chart_points"] == AS_DRAWN
    assert request_meta({"chart_points": 10_000})["chart_points"] == MAX_CHART_POINTS


def read(text):
    r = intent.read(text, NOW, ("Ecowitt", "AirGradient", "Pollen", "Forecast"))
    return r, Turn.of(r, text)


def test_a_chart_asked_for_plainly_is_summarys_call_with_its_period_in_the_services_words():
    for text, want in (("Rain chart 1d", {"readings": ["rainfall.rain"], "period": "last 24 hours", "chart": True}),
                       ("Temperature chart 7d", {"readings": ["outdoor.temperature", "indoor.temperature"], "period": "last 7 days", "chart": True}),
                       ("indoor temperature chart 7d", {"readings": ["indoor.temperature"], "period": "last 7 days", "chart": True}),
                       ("pressure chart this month", {"readings": ["pressure.relative"], "period": "this month", "chart": True}),
                       ("average temperature 7d", {"readings": ["outdoor.temperature", "indoor.temperature"], "period": "last 7 days",
                                                   "chart": True, "average": True}),
                       ("plot uv and solar on two axes 7d", {"readings": ["solar_and_uvi.uvi", "solar_and_uvi.solar"], "period": "last 7 days",
                                                             "chart": True, "layout": "two_axes"}),
                       ("lowest temperature on record", {"readings": ["outdoor.temperature", "indoor.temperature"],   # (the bot's "on record")
                                                         "period": "2022-11-12 to 2026-11-10", "chart": True}),
                       ("Particulates chart 1d", {"readings": ["air.pm2_5", "air.pm10", "air.pm1"], "period": "last 24 hours", "chart": True})):
        r, turn = read(text)
        assert legacy.translate(*r.fast[:2], turn, now=NOW, tz=TZ) == ("summary", want), text


def test_the_lookups_are_the_tools_that_word_them():
    for text, want in (("how hot is it", ("current", {"readings": ["temperature"]})),
                       ("indoor temperature", ("current", {"readings": ["indoor temperature"]})),
                       ("co2", ("current", {"readings": ["air.co2"]})), ("air quality now", ("current", {"readings": ["air"]})),
                       ("what can you do", ("about", {}))):
        assert legacy.lookup(read(text)[0]) == want, text
    for text, want in (("forecast", ("forecast", {"period": "the next 7 days"})), ("pollen", ("pollen", {})),
                       ("highest temperature yesterday", ("summary", {"readings": ["outdoor.temperature", "indoor.temperature"],
                                                                       "period": "yesterday", "chart": False})),
                       ("rain by month this year", ("days", {"count_only": True, "group_by": "month", "stat": "sum", "of": "rain",
                                                             "period": "2026-01-01 to 2026-11-10"}))):
        r, turn = read(text)
        assert legacy.lookup(r) is None and legacy.translate(*r.fast[:2], turn, now=NOW, tz=TZ) == want, text
    assert legacy.report(("Ecowitt", "AirGradient", "Pollen", "Forecast")) == [("current", {}), ("pollen", {}),
                                                                             ("forecast", {"period": "next 3 days"})]


def test_a_period_is_words_the_service_reads_as_the_same_span_else_whole_days_else_times():
    assert legacy.period("2026-11-09 14:05:00", "2026-11-10 14:05:00", ["last 24 hours"], NOW, TZ) == {"period": "last 24 hours"}
    assert legacy.period("2026-11-04 00:00:00", "2026-11-10 23:59:59", ["this week"], NOW, TZ) == {"period": "this week"}
    assert legacy.period("2026-11-04 00:00:00", "2026-11-10 23:59:59", ["this month"], NOW, TZ) == {"period": "2026-11-04 to 2026-11-10"}
    assert legacy.period("2026-11-01 00:00:00", None, [], NOW, TZ) == {"period": "2026-11-01 to now"}
    assert legacy.period("2026-11-09 06:00:00", "2026-11-09 11:59:59", [], NOW, TZ) == {"start": "2026-11-09 06:00", "end": "2026-11-09 12:00"}
    assert legacy.period("2026-01-01", "2026-11-10", [], NOW, TZ, whole_days=True) == {"period": "2026-01-01 to 2026-11-10"}


def test_the_periods_a_chart_the_model_drew_is_worth_drawing_over_are_its_tools():
    assert [p["period"] for p in legacy.periods_of("summary", {"period": "last 7 days"})] == ["1d", "7d", "1m", "3m", "1y"]
    assert [p["period"] for p in legacy.periods_of("summary", {"period": "last 7 days", "average": True})] == ["7d", "1m", "3m", "1y"]
    assert legacy.periods_of("summary", {"start": "2026-11-09 06:00"}) == []                  # a time can't be swapped for a period
    assert [p["label"] for p in legacy.periods_of("days", {"group_by": "month"})] == ["3M", "1Y", "All"]
    assert legacy.periods_of("days", {"group_by": "year"}) == [] and legacy.translate("plot_chart", {}, now=NOW, tz=TZ) is None
