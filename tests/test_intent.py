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


def test_numbered_periods_are_read_not_guessed():
    text = "use 30 minute day to plot 4 months of weather"   # was answered with the last 7 days
    name, args, label = call(text)
    assert name == "weather_history" and args["chart"] and label.endswith("last 4 months")
    assert args["start_date"] == "2026-05-31 00:00:00" and args["end_date"] == "2026-09-29 23:59:59"
    assert call("plot weather for four months")[1]["start_date"] == "2026-05-31 00:00:00"
    assert call("weather 2 weeks")[1]["start_date"] == "2026-09-16 00:00:00"
    assert call("weather 12h")[1]["start_date"] == "2026-09-29 02:05:00" and call("weather 12h")[1]["chart"]
    assert call("chart the temperature over 3 days")[1]["start_date"] == "2026-09-27 00:00:00"
    assert call("aq 3 days")[1]["start_date"] == "2026-09-27 00:00:00"


def test_a_period_we_cant_read_goes_to_the_model_not_to_a_default():
    for text in ("plot the weather over the last few months", "chart weather since march", "plot temperature this decade",
                 "graph the weather 3 days ago", "chart weather 3 months vs 6 months"):
        assert call(text) is None, text
    assert call("chart weather")[1]["start_date"] == "2026-09-23 00:00:00"   # nothing period-like: a week
    assert call("chart the air quality")[1]["start_date"] == "2026-09-28 14:05:00"
    assert call("chart the air quality over the last few days") is None


def test_1m_means_one_month():
    assert call("weather 1m")[1]["start_date"] == "2026-08-31 00:00:00"
    assert call("weather 1mo")[1]["start_date"] == "2026-08-31 00:00:00"
    assert call("aq 1m")[1]["start_date"] == "2026-08-31 00:00:00"
    assert call("weather 3m")[1]["start_date"] == "2026-07-01 00:00:00"
    # "30 minute" is not "30 months"
    assert call("use 30 minute data to plot 4 months of weather")[1]["start_date"] == "2026-05-31 00:00:00"


def test_a_bare_week_month_or_year_charts_for_air_quality_too():
    for text, start in (("aq week", "2026-09-23 00:00:00"), ("air quality month", "2026-09-01 00:00:00"),
                        ("aq year", "2026-01-01 00:00:00"), ("pm2.5 week", "2026-09-23 00:00:00")):
        name, args, label = call(text)
        assert name == "air_quality" and args["chart"] and args["start_date"] == start, text
    assert call("aq")[:2] == ("air_quality", {})   # no period at all: the current reading


def test_air_quality_chart_or_current_reading():
    """A period (however written) charts; the bare words give the current reading as text."""
    for text in ("aq week", "aq 1w", "air quality 1d", "aq 1d", "air quality week", "air qual week", "air qual 1d",
                 "aq 24h", "aq 1m", "pm2.5 1d"):
        name, args, label = call(text)
        assert name == "air_quality" and args["chart"] is True, text
    for text in ("aq", "air qual", "air quality", "air", "aqi", "aq now", "how's the air", "pm2.5"):
        assert call(text)[:2] == ("air_quality", {}), text
    assert call("aq 1w")[2] == "air quality chart, last 1 week"
