from datetime import datetime

from lib import intent

NOW = datetime(2026, 9, 29, 14, 5)  # a Tuesday


def call(text, ecowitt=True, air=True):
    return intent.fast_call(text, NOW, ecowitt, air)


def test_only_predictions_and_descriptions_reason():
    assert intent.reasoning_effort("will it rain later?") == "medium"
    assert intent.reasoning_effort("what was yesterday like?") == "low"
    assert intent.reasoning_effort("what was this week's high and low") == "none"
    assert intent.reasoning_effort("how much rain fell today") == "none"


def test_chat_needs_no_data():
    assert not intent.needs_data("thanks, that's great")
    assert intent.needs_data("how hot is it")


def test_highs_and_lows_go_fast():
    name, args, _ = call("what was this week's high and low?")
    assert name == "weather_history"
    assert args == {"groups": "outdoor,indoor", "chart": True, "start_date": "2026-09-23 00:00:00",
                    "end_date": "2026-09-29 23:59:59"}


def test_single_day_has_no_chart_and_respects_indoor():
    _, args, _ = call("indoor high yesterday")
    assert args["groups"] == "indoor" and args["chart"] is False
    assert args["start_date"] == "2026-09-28 00:00:00"


def test_last_24_hours_is_rolling():
    _, args, _ = call("graph the temperature last 24 hours")
    assert args["start_date"] == "2026-09-28 14:05:00" and args["end_date"] == "2026-09-29 14:05:00"


def test_anything_else_takes_the_normal_path():
    assert call("will it rain tomorrow?") is None
    assert call("compare this week and last week highs") is None
    assert call("highest wind this week") is None
    assert call("hello") is None


def test_air_now_and_air_chart():
    assert call("how's the air?")[:2] == ("air_quality", {})
    name, args, _ = call("graph PM2.5 and CO2 this week")
    assert name == "air_quality" and args["chart"] and args["metrics"] == ["pm2_5", "co2"]
    assert args["start_date"] == "2026-09-23 00:00:00"
    assert call("chart the air quality")[1]["metrics"] == ["pm2_5"]
    assert call("air quality yesterday")[1]["start_date"] == "2026-09-28 00:00:00"


def test_sources_are_optional():
    assert call("how's the air?", air=False) is None
    assert call("this week's highs", ecowitt=False) is None


def test_weather_plus_a_period_is_a_summary_request():
    for text in ("weather week", "weather this week", "weather yesterday", "what was the weather last month"):
        assert call(text) and call(text)[0] == "weather_history", text
    assert call("weather week")[1]["start_date"] == "2026-09-23 00:00:00" and call("weather week")[1]["chart"]
    assert call("hottest day of the month")[1]["start_date"] == "2026-09-01 00:00:00"


def test_weather_now_and_open_questions_still_go_to_the_model():
    for text in ("weather", "weather today", "how's the weather now", "weather tomorrow", "weather this week vs last week",
                 "will the weather be nice this week"):
        assert call(text) is None, text


def test_24_hour_weather_charts_over_a_rolling_window():
    for text in ("weather 24h", "weather 1d", "weather last 24 hours", "weather 1 day"):
        name, args, label = call(text)
        assert name == "weather_history" and args["chart"] is True, text
        assert args["start_date"] == "2026-09-28 14:05:00" and args["end_date"] == "2026-09-29 14:05:00"
    assert call("weather yesterday")[1]["chart"] is False


def test_air_chart_over_the_last_24_hours_works():
    for text in ("chart pm2.5 last 24 hours", "graph the air quality 24h", "chart co2 1d"):
        name, args, _ = call(text)
        assert name == "air_quality" and args["start_date"] == "2026-09-28 14:05:00", text


def test_aq_with_a_period_charts():
    cases = {"aq 24h": "2026-09-28 14:05:00", "aq 1d": "2026-09-28 14:05:00", "aq 1 month": "2026-08-31 00:00:00",
             "aq 1w": "2026-09-23 00:00:00", "air quality 1 year": "2025-09-30 00:00:00"}
    for text, start in cases.items():
        name, args, _ = call(text)
        assert name == "air_quality" and args["chart"] and args["start_date"] == start, text
    assert call("aq")[:2] == ("air_quality", {})          # no period: the current reading
    assert call("aq now")[:2] == ("air_quality", {})
    assert call("why was the aq bad yesterday") is None   # a question, not a chart request
    assert call("aq this week vs last week") is None


def test_more_ways_to_name_a_period_for_weather():
    assert call("weather 1 month")[1]["start_date"] == "2026-08-31 00:00:00"
    assert call("weather 1y")[1]["start_date"] == "2025-09-30 00:00:00"
    assert call("weather 7d")[1]["start_date"] == "2026-09-23 00:00:00"
