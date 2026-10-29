import json
from datetime import date

from lib import report

# What `python scripts/report_inputs.py` printed on the real station (rain, night), its time as the station now writes it
NOW = ('{"time":"Fri 2 Oct 2026 2:21am","outdoor":{"temperature":"12.3 ℃","feels_like":"12.3 ℃","app_temp":"12.7 ℃","dew_point":"11.3 ℃",'
       '"vpd":"0.085 kPa","humidity":"94 %"},"indoor":{"temperature":"18.3 ℃","humidity":"63 %","dew_point":"11.1 ℃","feels_like":"18.3 ℃",'
       '"app_tempin":"18.7 ℃"},"solar_and_uvi":{"solar":"0.0 W/m²","uvi":"0"},"rainfall":{"rain_rate":"1.8 mm/hr","daily":"6.3 mm",'
       '"event":"49.8 mm","1_hour":"1.3 mm","weekly":"49.8 mm","monthly":"49.8 mm","yearly":"5105.7 mm"},"wind":{"wind_speed":"0.0 km/h",'
       '"wind_gust":"0.0 km/h","wind_direction":"131 º"},"pressure":{"relative":"1025.0 hPa","absolute":"1009.5 hPa"},'
       '"emoji":{"outdoor.temperature":"🧥","outdoor.feels_like":"🧥","outdoor.app_temp":"🧥",'
       '"outdoor.humidity":"💦","indoor.temperature":"🧥","indoor.feels_like":"🧥","indoor.app_tempin":"🧥","rainfall.rain_rate":"🌧️",'
       '"rainfall.daily":"☔","pressure.relative":"🗜️"}}')
AIR = ('{"sensor": "Fairleigh", "sensor_type": "outdoor", "time": "Fri 2 Oct 2026 2:22am", "pm2_5": {"value": 1.4, "unit": "µg/m³", '
       '"rating": "🟢 good", "aqi_us": 8, "band": "good"}, "pm10": {"value": 3.0, "unit": "µg/m³", "rating": "🟢 good"}, "pm1": {"value": 1.0, '
       '"unit": "µg/m³", "rating": "🟢 good"}, "co2": {"value": 433.0, "unit": "ppm", "rating": "🟢 good"}, "voc_index": {"value": 88.0, '
       '"unit": "relative index (100 = this sensor\'s recent average)", "rating": "🟢 good"}, "nox_index": {"value": 1.0, '
       '"unit": "relative index (1 = baseline)", "rating": "🟢 good"}}')
POLLEN = '{"lines": ["Grass pollen forecast: 🟢 Low", "Thunderstorm asthma forecast: 🟢 Low"], "place": "Melbourne", "source": "melbournepollen.com.au", "time": "Fri 2 Oct 2026 6:00pm"}'
FORECAST = ('{"lines": ["Today: 🌦️ Showers. 11–16°C, 97% chance of rain", "Saturday: ⛈️ Thunderstorm. 12–20°C, 95% chance of rain"], '
            '"place": "Melbourne", "source": "Open-Meteo", "time": "Fri 2 Oct 2026 6:05pm"}')

TODAY = date(2026, 10, 2)   # the day the results above are from

WEATHER = """\
• Outdoor: 🧥 12.3 °C, 💦 94 %
• Indoor: 🧥 18.3 °C, 63 %
• Pressure: 🗜️ 1025.0 hPa
• Rain today: ☔ 6.3 mm (month total 49.8 mm); 🌧️ raining (1.8 mm/h)"""   # night and calm: no Sun or Wind line


def test_the_report_is_laid_out_in_code_with_emoji_before_the_readings_they_belong_to():
    got = report.report({"weather_now": NOW, "air_quality": AIR, "pollen_asthma": POLLEN, "weather_forecast": FORECAST}, TODAY)
    assert got == f"""Weather station (2:21am)
{WEATHER}

Air quality (2:22am)
• PM2.5: 1.4 µg/m³ 🟢 good (AQI 8)
• PM10: 3.0 µg/m³ 🟢 good
• PM1: 1.0 µg/m³ 🟢 good
• CO₂: 433 ppm 🟢 good
• VOC index: 88 🟢 good
• NOx index: 1.0 🟢 good

Pollen & asthma (Melbourne, 6:00pm)
• Grass pollen forecast: 🟢 Low
• Thunderstorm asthma forecast: 🟢 Low

Forecast (Melbourne, 6:05pm)
• Today: 🌦️ Showers. 11–16°C, 97% chance of rain
• Saturday: ⛈️ Thunderstorm. 12–20°C, 95% chance of rain"""
    assert not got.startswith("Current report")


def test_weather_now_is_just_the_weather_stations_bullets():
    assert report.weather_now(NOW) == WEATHER
    assert report.weather_now("Error calling tool: timed out") is None and report.weather_now(None) is None


def test_a_dry_calm_day_has_no_rain_line_noise():
    dry = ('{"outdoor":{"temperature":"21.0 ℃","humidity":"50 %"},"rainfall":{"rain_rate":"0.0 mm/hr","daily":"0.0 mm","monthly":"12.0 mm"},'
           '"wind":{"wind_speed":"12.0 km/h","wind_gust":"20.0 km/h","wind_direction":"270 º"},"emoji":{}}')
    lines = report.weather_now(dry).splitlines()
    assert lines == ["• Outdoor: 21.0 °C, 50 %", "• Rain today: 0.0 mm (month total 12.0 mm)", "• Wind: 12.0 km/h from 270°, gust 20.0 km/h"]
    drizzle = dry.replace('"0.0 mm/hr"', '"0.6 mm/hr"').replace('"emoji":{}', '"emoji":{"rainfall.rain_rate":"🌧️"}')
    assert "; 🌧️ raining (0.6 mm/h)" in report.weather_now(drizzle)
    assert "raining" not in report.weather_now(dry.replace('"0.0 mm/hr"', '"0.00 mm/hr"'))   # however the zero is written


def test_a_failed_or_missing_source_is_left_out_or_said_so_and_the_forecast_without_a_tag_has_a_plain_heading():
    got = report.report({"weather_now": "Error calling tool: boom", "air_quality": AIR,
                         "pollen_asthma": '{"error": "No pollen levels are available right now."}',
                         "weather_forecast": '{"lines": ["Today: ☀️ Sunny. 20–25°C"]}'}, TODAY)
    assert got.startswith("Weather station\n• not available right now\n\nAir quality (2:22am)\n• PM2.5:")
    assert "Pollen" not in got and got.endswith("Forecast\n• Today: ☀️ Sunny. 20–25°C")
    assert report.report({}, TODAY) == "No readings are available right now."


def test_sun_uv_and_wind_are_left_out_when_zero_and_shown_when_not():
    day = NOW.replace('"solar":"0.0 W/m²","uvi":"0"', '"solar":"612.5 W/m²","uvi":"7"').replace(
        '"wind_speed":"0.0 km/h","wind_gust":"0.0 km/h"', '"wind_speed":"14.0 km/h","wind_gust":"31.0 km/h"')
    got = report.weather_now(day).splitlines()
    assert got[-2:] == ["• Sun: solar radiation 612.5 W/m² (high), UV index 7", "• Wind: 14.0 km/h from 131°, gust 31.0 km/h"]
    only_uv = report.weather_now(NOW.replace('"uvi":"0"', '"uvi":"3"'))
    assert "• Sun: UV index 3" in only_uv and "solar" not in only_uv
    gusty = report.weather_now(NOW.replace('"wind_gust":"0.0 km/h"', '"wind_gust":"9.0 km/h"'))
    assert "• Wind: 0.0 km/h from 131°, gust 9.0 km/h" in gusty
    assert "Sun" not in report.weather_now(NOW) and "Wind" not in report.weather_now(NOW)


def test_every_emoji_threshold_the_station_reports_shows_in_the_report_at_its_reading():
    from lib.ecowitt.glance import glance
    now = ('{"outdoor":{"temperature":"36.0 ℃","humidity":"90 %","dew_point":"18.0 ℃","vpd":"1.300 kPa"},"indoor":{"temperature":"29.0 ℃","humidity":"20 %"},'
           '"pressure":{"relative":"1030.0 hPa"},"rainfall":{"rain_rate":"2.0 mm/hr","daily":"3.0 mm","monthly":"9.0 mm"},'
           '"solar_and_uvi":{"solar":"800.0 W/m²","uvi":"10"},"wind":{"wind_speed":"55.0 km/h","wind_gust":"70.0 km/h","wind_direction":"90 º"}}')
    import json
    data = json.loads(now)
    data["emoji"] = {f"{g}.{f}": e for g, fields in data.items() for f, v in fields.items()
                     if (e := glance(g, f, float(v.split()[0])))}
    got = report.weather_now(json.dumps(data))
    assert got.splitlines() == [
        "• Outdoor: 🔥 36.0 °C, 💦 90 %",
        "• Indoor: 🥵 29.0 °C, 🏜️ 20 %",
        "• Pressure: 🗜️ 1030.0 hPa",
        "• Rain today: ☔ 3.0 mm (month total 9.0 mm); 🌧️ raining (2.0 mm/h)",
        "• Sun: ☀️ solar radiation 800.0 W/m² (high), 🧴 UV index 10",
        "• Wind: 🌪️ 55.0 km/h from 90°, gust 🌪️ 70.0 km/h"]


def test_solar_radiation_is_rated_low_medium_or_high():
    from lib.ecowitt.glance import solar_band
    assert [solar_band(v) for v in (0.1, 40.2, 199.9, 200, 599.9, 600, 1000)] == ["low", "low", "low", "medium", "medium", "high", "high"]

    def sun(solar):
        return report.weather_now('{"solar_and_uvi":{"solar":"%s","uvi":"0"},"emoji":{}}' % solar)
    assert sun("40.2 W/m²") == "• Sun: solar radiation 40.2 W/m² (low)"
    assert sun("350.0 W/m²") == "• Sun: solar radiation 350.0 W/m² (medium)"
    assert sun("0.0 W/m²") == ""                                      # zero is left out, so there is no Sun line
    assert sun("n/a") == "• Sun: solar radiation n/a"                 # unreadable: shown as it came, with no rating


# ---------- chart captions ----------
def _turn(**kw):
    from types import SimpleNamespace as NS
    from lib.specs import Bars, Chart, Panel
    bars = Bars("Rain", "mm", [1759000000, 1759086400, 1759172800], [0.0, 7.2, 1.4], 86400, "day")
    return NS(charts=[Chart("Rain", "", [Panel("Rain", "mm", bars=bars)])], chart_fields=[], chart_field=None, average_asked=False, **kw)


def test_a_weather_chart_caption_gives_the_period_then_low_and_high_per_series():
    from zoneinfo import ZoneInfo
    result = json.dumps({"period": "Tue 29 Sep 2026 - Tue 06 Oct 2026", "series": {
        "outdoor.temperature": {"unit": "℃", "low": "8.1", "low_when": "at 5:30am", "low_date": "Wed 30 Sep 2026",
                                "high": "24.3", "high_when": "around 3pm", "high_date": "Sat 3 Oct 2026"},
        "indoor.temperature": {"unit": "℃", "low": "18.0", "low_when": "at 6am", "low_date": "Wed 30 Sep 2026",
                               "high": "22.5", "high_when": "at 4pm", "high_date": "Sat 3 Oct 2026"}}})
    caption = report.chart_caption("weather_history", {}, result, _turn(), ZoneInfo("Australia/Melbourne"))
    assert caption.splitlines() == ["Tue 29 Sep – Tue 06 Oct 2026",
                                    "Temperature (outdoor): low 8.1 °C, Wed 30 Sep at 5:30am · high 24.3 °C, Sat 3 Oct around 3pm",
                                    "Temperature (indoor): low 18.0 °C, Wed 30 Sep at 6am · high 22.5 °C, Sat 3 Oct at 4pm"]


def test_an_average_chart_caption_leads_with_the_average():
    from zoneinfo import ZoneInfo
    turn = _turn()
    turn.average_asked = True
    result = json.dumps({"period": "Tue 29 Sep 2026 - Tue 06 Oct 2026", "series": {
        "outdoor.temperature": {"unit": "℃", "low": "8.1", "high": "24.3", "average": "15.2"}}})
    assert report.chart_caption("weather_history", {}, result, turn, ZoneInfo("UTC")).splitlines()[1] == \
        "Temperature: average 15.2 °C (low 8.1, high 24.3)"


def test_a_rain_chart_caption_is_the_least_and_most_rain():
    from zoneinfo import ZoneInfo
    turn = _turn()
    turn.chart_fields = ["rain"]
    result = json.dumps({"period": "Tue 29 Sep 2026 - Tue 06 Oct 2026", "series": {"rainfall.daily": {"unit": "mm", "low": "0", "high": "7.2"}}})
    lines = report.chart_caption("weather_history", {}, result, turn, ZoneInfo("UTC")).splitlines()
    assert lines[1].startswith("☔ Rain per day: least 0 mm (") and "most 7.2 mm (" in lines[1] and "8.6 mm in all" in lines[1]
    assert len(lines) == 2


def test_an_air_chart_caption_gives_each_metrics_peak_rating_and_average():
    from zoneinfo import ZoneInfo
    result = json.dumps({"period": "Tue 29 Sep 2026 - Tue 06 Oct 2026", "pm2_5": {
        "unit": "µg/m³", "high": 34.2, "high_time": "Wed 30 Sep 2026 3:00pm", "high_rating": "🟠 Poor", "average": 8.1},
        "pm10": {"unit": "µg/m³", "high": 50, "high_time": "x", "average": 9}})
    caption = report.chart_caption("air_quality", {"metrics": ["pm2_5"]}, result, _turn(), ZoneInfo("UTC"))
    assert caption.splitlines()[1] == "• PM2.5: peak 34.2 µg/m³ (Wed 30 Sep 3:00pm) · 🟠 Poor · average 8.1"
    assert len(caption.splitlines()) == 2


def test_no_caption_when_the_tool_failed_or_drew_nothing():
    from zoneinfo import ZoneInfo
    from types import SimpleNamespace as NS
    assert report.chart_caption("weather_history", {}, json.dumps({"error": "x"}), _turn(), ZoneInfo("UTC")) is None
    assert report.chart_caption("weather_history", {}, json.dumps({"series": {}}), NS(charts=[]), ZoneInfo("UTC")) is None


# ---------- plain lookups ----------
def test_a_reading_lookup_prints_only_what_was_asked():
    now = json.loads(NOW)
    assert report.reading_lines(now, ["temperature"], ["outdoor", "indoor"]) == [
        "• Outdoor: 🧥 12.3 °C, feels like 12.3 °C", "• Indoor: 🧥 18.3 °C, feels like 18.3 °C"]
    assert report.reading_lines(now, ["humidity"], ["outdoor"]) == ["• Outdoor: 💦 94 %"]
    assert report.reading_lines(now, ["wind"], []) == ["• Wind: calm"]
    assert report.reading_lines(now, ["pressure"], []) == ["• Pressure: 🗜️ 1025.0 hPa"]
    assert report.reading_lines(now, ["dew_point"], ["indoor"]) == ["• Dew point (indoor): 11.1 °C"]   # "indoor dew point"
    assert report.reading_lines(now, ["feels_like"], ["outdoor", "indoor"])[0].startswith("• Feels like: ")   # unasked: outdoor
    assert report.reading_lines(now, ["rain"], [])[0].startswith("• Rain today: ☔ 6.3 mm") and "raining (1.8 mm/h)" in report.reading_lines(now, ["rain"], [])[0]
    windy = json.loads(NOW.replace('"wind_speed":"0.0 km/h"', '"wind_speed":"14.0 km/h"'))
    assert [line for line in report.weather_lines(windy) if line.startswith(("• Wind", "• Rain"))] == \
        report.reading_lines(windy, ["rain", "wind"], [])   # the same words as the report's


def test_air_pollen_and_forecast_lookups_print_the_tools_own_lines():
    assert report.lookup("air", AIR, ["pm10"], [], TODAY) == "• PM10: 3.0 µg/m³ 🟢 good"
    assert report.lookup("air", AIR, [], [], TODAY).splitlines()[0] == "• PM2.5: 1.4 µg/m³ 🟢 good (AQI 8)"
    assert report.lookup("pollen", POLLEN, [], [], TODAY) == "Pollen & asthma (Melbourne, 6:00pm)\n• Grass pollen forecast: 🟢 Low\n• Thunderstorm asthma forecast: 🟢 Low"
    assert report.lookup("forecast", FORECAST, [], [], TODAY).startswith("Forecast (Melbourne, 6:05pm)\n• Today: ")
    assert report.lookup("pollen", json.dumps({"error": "off season"}), [], [], TODAY) is None


def test_a_short_period_highs_and_lows_lookup_gives_each_sides_extremes_with_their_times():
    result = json.dumps({"period": "Fri 02 Oct 2026 - Fri 02 Oct 2026", "series": {
        "outdoor.temperature": {"unit": "℃", "low": "9.0", "low_when": "at 5am", "low_date": "Fri 2 Oct 2026",
                                "high": "16.2", "high_when": "at 2pm", "high_date": "Fri 2 Oct 2026"},
        "outdoor.humidity": {"unit": "%", "low": "40", "high": "95"}}})
    assert report.lookup("extremes", result, [], [], TODAY) == ("Fri 02 Oct – Fri 02 Oct 2026\n"
                                                         "• Temperature: low 9.0 °C, Fri 2 Oct at 5am · high 16.2 °C, Fri 2 Oct at 2pm")


def test_an_average_or_highs_lookup_covers_the_readings_named():
    result = json.dumps({"period": "Thu 01 Oct 2026 - Thu 01 Oct 2026", "series": {
        "outdoor.temperature": {"unit": "℃", "low": "9.0", "high": "16.2", "average": "12.4"},
        "outdoor.humidity": {"unit": "%", "low": "40", "high": "95", "low_when": "at 3pm", "low_date": "Thu 1 Oct 2026", "high_when": "at 5am", "high_date": "Thu 1 Oct 2026"},
        "wind.wind_speed": {"unit": "km/h", "low": "0", "high": "14"}, "wind.wind_gust": {"unit": "km/h", "low": "0", "high": "31", "high_when": "at 2pm", "high_date": "Thu 1 Oct 2026"}}})
    assert report.lookup("extremes", result, ["temperature"], [], TODAY).splitlines()[1] == "• Temperature: average 12.4 °C (low 9.0, high 16.2)"
    assert report.lookup("extremes", result, ["humidity"], [], TODAY).splitlines()[1] == "• Humidity: low 40 %, Thu 1 Oct at 3pm · high 95 %, Thu 1 Oct at 5am"
    assert [line.split(":")[0] for line in report.lookup("extremes", result, ["wind"], [], TODAY).splitlines()[1:]] == ["• Wind", "• Wind gust"]


def test_a_days_count_lookup_gives_the_count_the_condition_and_the_period():
    result = json.dumps({"period": "Wed 5 Oct 2022 - Sat 3 Oct 2026", "days_checked": 1454, "matching_days": 137, "condition": "UV index ≥ 10"})
    assert report.lookup("days", result, [], [], TODAY) == "137 of 1,454 days with UV index ≥ 10 (Wed 5 Oct 2022 – Sat 3 Oct 2026)."
    averaged = json.dumps({"period": "a - b", "days_checked": 10, "matching_days": 2, "note_averaged": "x"})
    assert "may fall slightly short" in report.lookup("days", averaged, [], [], TODAY)
    assert report.lookup("days", json.dumps({"error": "bad"}), [], [], TODAY) is None


def test_a_figure_per_period_lookup_gives_the_figure_over_the_whole_period():
    result = json.dumps({"period": "Fri 4 Oct 2024 - Sat 3 Oct 2026", "days_checked": 731, "matching_days": 731,
                         "stat": {"what": "Total rain", "of": "rain", "unit": "mm"}, "value": 1234.5, "by_month": {"2024-10": 0.0}})
    assert report.lookup("days", result, [], [], TODAY) == "Total rain: 1234.5 mm over 731 days (Fri 4 Oct 2024 – Sat 3 Oct 2026)."
    hot = json.dumps({"period": "a 2025 - b 2026", "days_checked": 3, "matching_days": 3, "stat": {"what": "Highest temperature", "of": "temp_max", "unit": "℃"}, "value": 41.2})
    assert report.lookup("days", hot, [], [], TODAY).startswith("Highest temperature: 41.2 °C over 3 days")


def test_a_heading_carries_the_place_and_the_time_of_day_of_its_data():
    assert report._heading("Forecast", {"place": "Melbourne", "time": "Fri 2 Oct 2026 6:05pm"}, TODAY) == "Forecast (Melbourne, 6:05pm)"
    assert report._heading("Weather station", {"time": "Fri 2 Oct 2026 12:07am"}, TODAY) == "Weather station (12:07am)"
    assert report._heading("Weather station", {"time": "Fri 2 Oct 2026 1:30pm"}, TODAY) == "Weather station (1:30pm)"
    assert report._heading("Air quality", {"time": "Fri 2 Oct 2026 12:00pm"}, TODAY) == "Air quality (12:00pm)"
    assert report._heading("Forecast", {"place": "Melbourne"}, TODAY) == "Forecast (Melbourne)"                           # no time: as before
    assert report._heading("Air quality", {"time": "yesterday"}, TODAY) == "Air quality" and report._heading("Air quality", {"time": None}, TODAY) == "Air quality"


def test_a_heading_gives_the_day_of_data_that_is_not_todays():
    stale = report.report({"weather_now": NOW, "pollen_asthma": POLLEN}, date(2026, 10, 4))   # the station has been silent since Friday
    assert stale.startswith("Weather station (Fri 2 Oct 2:21am)\n") and "\nPollen & asthma (Melbourne, Fri 2 Oct 6:00pm)\n" in stale
    assert report.lookup("forecast", FORECAST, [], [], date(2026, 10, 3)).startswith("Forecast (Melbourne, Fri 2 Oct 6:05pm)\n")
    assert [e.length for e in report.heading_entities(stale)] == [len("Weather station"), len("Pollen & asthma")]   # still bold


def test_when_gives_the_form_the_tools_use_in_their_time_field():
    from datetime import datetime
    from lib.timeutil import when
    tz = ZoneInfo("Australia/Melbourne")
    assert when(int(datetime(2026, 10, 2, 18, 5, tzinfo=tz).timestamp()), tz) == "Fri 2 Oct 2026 6:05pm"
    assert when(int(datetime(2026, 10, 2, 0, 7, tzinfo=tz).timestamp()), tz) == "Fri 2 Oct 2026 12:07am"


from zoneinfo import ZoneInfo  # noqa: E402


def test_only_the_name_of_each_heading_is_bold_not_its_brackets_or_time():
    text = report.report({"weather_now": NOW, "air_quality": AIR, "pollen_asthma": POLLEN, "weather_forecast": FORECAST}, TODAY)
    units = lambda s: len(s.encode("utf-16-le")) // 2
    names = [text.encode("utf-16-le")[2 * e.offset:2 * (e.offset + e.length)].decode("utf-16-le") for e in report.heading_entities(text)]
    assert names == ["Weather station", "Air quality", "Pollen & asthma", "Forecast"]
    assert all(e.type == "bold" for e in report.heading_entities(text))
    after_emoji = report.heading_entities("• Rain today: ☔ 6.3 mm 🌧️\nForecast (Melbourne, 6:05pm)\n• Today")      # emoji count as two units
    assert [(e.offset, e.length) for e in after_emoji] == [(units("• Rain today: ☔ 6.3 mm 🌧️\n"), len("Forecast"))]
    assert report.heading_entities("Forecast\n• x") and report.heading_entities("• Forecast: sunny\nForecasts") == []
