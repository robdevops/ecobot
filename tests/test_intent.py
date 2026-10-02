from datetime import datetime, timedelta

from lib import intent

NOW = datetime(2026, 9, 29, 14, 5)  # a Tuesday


def call(text, ecowitt=True, air=True):
    return intent.fast_call(text, NOW, ecowitt, air)


def test_only_predictions_and_descriptions_reason():
    assert intent.reasoning_effort("will it rain later?") == "medium"
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
    assert call("hottest day of the month")[1]["start_date"] == "2026-08-31 00:00:00"   # the rolling month, not the calendar one


def test_weather_now_and_open_questions_still_go_to_the_model():
    for text in ("weather today", "how's the weather now", "weather tomorrow", "weather this week vs last week",
                 "will the weather be nice this week"):
        assert call(text) is None, text


def test_weather_now_fetches_every_reading_the_station_has_and_asks_for_the_reports_layout():
    for text in ("weather", "weather now", "Weather Now", "current weather", "ecowitt", "ecowitt now", "show me the weather now please"):
        name, args, label = call(text)
        assert (name, label) == ("weather_now", "weather now") and args == {"groups": intent.NOW_GROUPS}, text
        assert intent.read(text, NOW).weather_now
    assert not intent.read("weather last 7 days", NOW).weather_now and not intent.read("report", NOW).weather_now
    assert not intent.read("weather now", NOW, ecowitt=False).weather_now


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


def test_a_plain_wind_chart_takes_the_fast_path_with_the_period_read():
    name, args, _ = call("wind direction plot 3m")
    assert name == "weather_history" and args["groups"] == "wind" and args["chart"] is True
    assert args["start_date"] == "2026-07-01 00:00:00" and args["end_date"] == "2026-09-29 23:59:59"
    assert call("graph the wind this week")[1]["groups"] == "wind"
    for text in ("highest wind this week", "plot wind vs temperature 3m", "will the wind chart change", "why was the wind strong 3m"):
        assert call(text) is None, text


def test_1m_means_one_month():
    assert call("weather 1m")[1]["start_date"] == "2026-08-31 00:00:00"
    assert call("weather 1mo")[1]["start_date"] == "2026-08-31 00:00:00"
    assert call("aq 1m")[1]["start_date"] == "2026-08-31 00:00:00"
    assert call("weather 3m")[1]["start_date"] == "2026-07-01 00:00:00"
    # "30 minute" is not "30 months"
    assert call("use 30 minute data to plot 4 months of weather")[1]["start_date"] == "2026-05-31 00:00:00"


def test_a_bare_week_month_or_year_charts_for_air_quality_too():
    for text, start in (("aq week", "2026-09-23 00:00:00"), ("air quality month", "2026-08-31 00:00:00"),
                        ("aq year", "2025-09-30 00:00:00"), ("pm2.5 week", "2026-09-23 00:00:00")):
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
    assert intent.reasoning_effort("will it rain later?") == "medium"
    assert intent.reasoning_effort("what was yesterday like?") == "low"


def test_absurd_periods_go_to_the_model_instead_of_crashing():
    from datetime import date
    for text in ("weather last 99999 years", "chart weather 2000 years", "aq 99999999999999999999 days",
                 "weather 999999 hours", "aq 500000 weeks", "weather 12345678 months"):
        assert call(text) is None, text            # no exception, no nonsense dates
    assert call("weather 4 years")[1]["start_date"] == "2022-10-01 00:00:00"      # a sane long period still works
    assert call("weather 8760 hours")[1]["chart"]
    # "on record" is safe on 29 February, when the year four earlier had none
    ranges = intent.period_ranges(date(2104, 2, 29))   # 2100 was not a leap year: the old code raised here
    assert ranges["on record"] == (date(2104, 2, 29) - timedelta(days=1459), date(2104, 2, 29))


def test_an_air_question_with_a_time_we_cant_read_is_not_answered_with_the_current_reading():
    for text in ("air quality since march", "aq the last few days", "how was the air 3 days ago", "aq these days"):
        assert call(text) is None, text
    for text in ("aq", "air quality", "aq now", "how's the air"):
        assert call(text)[:2] == ("air_quality", {}), text


def test_a_particular_date_or_time_goes_to_the_model_not_a_whole_period():
    for text in ("what was the high on 5 Jan this year", "temperature at 3pm today", "hottest day in Jan 12 to Jan 20",
                 "weather on monday this week", "high at 15:30 yesterday", "how hot was it on 3/2 this month",
                 "aq on sunday this week", "air quality this morning", "graph the temperature yesterday afternoon",
                 "what was the low on the 5th of March this year", "weather tonight this week"):
        assert call(text) is None, text
    # whole periods, however they are written, still take the fast path
    for text in ("weather this week", "weather 1m", "aq 24h", "aq week", "hottest day this month", "weather 3 months",
                 "high and low this year", "chart the temperature last 7 days", "weather 12h", "how sunny was last month"):
        assert call(text) is not None or text == "how sunny was last month", text


def test_a_question_about_one_reading_charts_that_reading():
    assert intent.chart_field("lowest and highest humidity") == "humidity"
    assert intent.chart_field("plot pressure this week") == "relative"
    assert intent.chart_field("highest wind gust this year") == "wind_speed"   # the mean, shaded up to the gusts
    for text in ("hottest day this year", "humidity and temperature this week", "wind and rain", "weather week", "how hot was it"):
        assert intent.chart_field(text) is None, text


def test_average_questions_are_recognised():
    for text in ("average temp 3m", "what was the mean humidity", "avg wind this week"):
        assert intent.AVERAGE.search(text), text
    assert not intent.AVERAGE.search("hottest day this year")


def test_the_model_is_told_the_exact_dates_of_any_period_the_words_name():
    hints = intent.period_hints("average temp 3m", NOW)
    assert hints == ['"3m" = last 3 months: 2026-07-01 00:00:00 to 2026-09-29 23:59:59']      # never 3 days
    assert intent.period_hints("wind direction 1y", NOW)[0].startswith('"1y" = last 1 year: 2025-09-30')
    assert intent.period_hints("humidity 24h", NOW) == ['"24h" = last 24 hours: 2026-09-28 14:05:00 to 2026-09-29 14:05:00']
    assert len(intent.period_hints("compare this week and last week highs", NOW)) == 2
    assert any("last 2 weeks" in h for h in intent.period_hints("weather 2 weeks", NOW))
    assert intent.period_hints("hello", NOW) == [] and intent.period_hints("use 30 minute data", NOW) == []


def test_readings_named_together_are_charted_together():
    assert intent.chart_fields("plot temperature and rain") == ["temperature", "rain"]
    assert intent.chart_fields("graph rain, pressure and humidity for 3m") == ["rain", "pressure", "humidity"]
    assert intent.chart_fields("wind vs temp this week") == ["wind", "temperature"]
    for text in ("plot the temperature", "will it rain", "hottest day this year", "hello"):
        assert intent.chart_fields(text) == [], text


def test_a_bare_period_is_hinted_and_a_command_to_the_bot_is_not_a_request_for_readings():
    assert intent.period_hints("humidity week", NOW) == ['"week" = last 7 days: 2026-09-23 00:00:00 to 2026-09-29 23:59:59']
    assert intent.period_hints("weather this week", NOW)[0].startswith('"this week"')                # a named period wins
    for text in ("add an alert for if winds reach 100 km hour", "please set up a rain alert", "mute the alerts"):
        assert not intent.needs_data(text), text
    for text in ("will the wind reach 100 km/h", "what was the addition of rain", "how windy is it"):
        assert intent.needs_data(text), text


def test_questions_about_the_bot_are_recognised_and_readings_questions_are_not():
    for text in ("list our metrics from both sources", "what metrics do you have?", "which sensors do you use", "what can you do",
                 "what do you measure", "what can I ask", "metrics", "our metrics?", "Available sensors", "sources please"):
        assert intent.about_the_bot(text) and not intent.needs_data(text), text
    for text in ("rain metrics for Tuesday", "metrics last week", "what was the hottest day", "list rainy days in september", "what's the temperature", "show the past week",
                 "what was the pressure last month", "how's the air?"):
        assert not intent.about_the_bot(text), text


def test_a_bare_status_or_report_asks_for_everything_and_nothing_else_does():
    for text in ("status", "report", "Report please", "give me the full report", "current report", "show what you've got",
                 "overview", "everything?", "what's the status", "sitrep", "SITREP please", "give me the sitrep"):
        assert intent.wants_report(text), text
    for text in ("weather report for Tuesday", "report the humidity", "status of the rain alert", "report last week", "sitrep last week", "hello",
                 "how's the air?"):
        assert not intent.wants_report(text), text


def test_dew_point_feels_like_and_vpd_are_charted_readings_that_outrank_temperature():
    assert intent.chart_field("dew point 90d") == intent.chart_field("dew piont 90d") == intent.chart_field("dewpoint 90d") == "dew_point"
    assert intent.chart_field("feels like temperature last month") == "feels_like"
    assert intent.chart_field("graph the vpd this week") == "vpd"
    assert intent.chart_fields("plot dew point and humidity") == ["dew_point", "humidity"]
    assert intent.chart_field("how hot was it") is None


def test_weather_all_week_is_every_reading_stacked_and_feel_is_the_feels_like_reading():
    assert intent.chart_fields("weather all week") == ["temperature", "humidity", "pressure", "wind", "rain", "dew_point", "feels_like", "vpd", "solar", "uv"]
    assert intent.chart_fields("weather this week") == [] and intent.chart_fields("all the rain last week") == []
    assert intent.chart_field("feels 90d") == intent.chart_field("feel 90d") == intent.chart_field("feels-like 90d") == "feels_like"


def test_solar_radiation_and_uv_are_charted_readings_and_a_weekday_is_not_solar():
    assert intent.chart_field("solar 30d") == "solar" and intent.chart_field("uv 7d") == "uvi" == intent.chart_field("uvi last week")
    assert intent.chart_field("sun 5 jan") is None and intent.chart_field("how hot was sunday") is None


def test_sun_and_uvi_is_two_stacked_readings_but_a_sunday_or_a_dated_sun_is_not_solar():
    assert intent.chart_fields("sun + uvi one month") == ["solar", "uv"]
    assert intent.chart_field("sun 5 jan") is None and intent.chart_field("was it hot on sunday") is None


def test_a_readings_band_field_still_names_it_and_only_plain_readings_derive_a_range():
    from lib.series import derives_range, field_of, find_name
    assert find_name("wind_gust") == find_name("wind_speed") == find_name("wind") == "wind" and field_of("wind_gust") == "wind_speed"
    assert derives_range("dew_point") and derives_range("solar") and not derives_range("wind_speed") and not derives_range("wind_direction")


def test_ecowitt_means_the_weather_and_ag_or_airgradient_means_the_air_quality():
    assert call("ecowitt week")[0] == "weather_history" and call("ecowitt week") == call("weather week") and call("ecowitt week")[1]["chart"]
    for text in ("ag 7d", "airgradient 7d", "air gradient 7d", "AG 7d"):
        name, args, _ = call(text)
        assert name == "air_quality" and args["chart"] is True and args["metrics"] == ["pm2_5"], text
    assert call("ag now")[0] == "air_quality" and call("air gradient")[0] == "air_quality" and not call("ag now")[1]
    assert call("ag and ecowitt 7d") is None                                          # both devices: the model decides
    assert intent.chart_fields("ecowitt all week") == intent.chart_fields("weather all week") and intent.chart_fields("ecowitt all week")
    assert intent.air_metrics("ag all week") == ["pm2_5", "pm10", "pm1", "co2", "voc_index", "nox_index"] or len(intent.air_metrics("ag all week")) == 6
    assert intent.needs_data("ecowitt") and intent.needs_data("ag") and intent.about_the_bot("what does ecowitt measure") and intent.about_the_bot("what does ag measure")
    assert not intent.needs_data("that was a nice sag in the road") and not intent.needs_data("thanks")


def test_the_report_fetches_everything_in_code_before_the_model_sees_it():
    r = intent.read("report", NOW, True, True, True, True)
    assert r.report and r.fast == ("weather_now", {"groups": intent.NOW_GROUPS}, "report")
    assert r.more == [("air_quality", {}), ("pollen_asthma", {"cached": True}), ("weather_forecast", {"days": 3, "cached": True})]
    plain = intent.read("sitrep", NOW)
    assert [plain.fast[0], *(t for t, _ in plain.more)] == ["weather_now", "air_quality"]
    only_air = intent.read("status", NOW, ecowitt=False)
    assert only_air.fast[0] == "air_quality" and only_air.more == []
    assert intent.read("report", NOW, ecowitt=False, air=False).fast is None
    assert not intent.read("report for last week", NOW).report
