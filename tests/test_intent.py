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
    assert call("air quality yesterday") is None


def test_sources_are_optional():
    assert call("how's the air?", air=False) is None
    assert call("this week's highs", ecowitt=False) is None
