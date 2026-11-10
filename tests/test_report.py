import json
from datetime import date

from lib import report
from tests import report_words   # (the bot's old report, for text to set bold)

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


# ---------- a days lookup (the model's day tools' charts that answer the question) ----------
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


def test_when_gives_the_form_the_tools_use_in_their_time_field():
    from datetime import datetime
    from lib.timeutil import when
    tz = ZoneInfo("Australia/Melbourne")
    assert when(int(datetime(2026, 10, 2, 18, 5, tzinfo=tz).timestamp()), tz) == "Fri 2 Oct 2026 6:05pm"
    assert when(int(datetime(2026, 10, 2, 0, 7, tzinfo=tz).timestamp()), tz) == "Fri 2 Oct 2026 12:07am"


from zoneinfo import ZoneInfo  # noqa: E402


def test_only_the_name_of_each_heading_is_bold_not_its_brackets_or_time():
    text = report_words.report({"weather_now": NOW, "air_quality": AIR, "pollen_asthma": POLLEN, "weather_forecast": FORECAST}, TODAY)
    units = lambda s: len(s.encode("utf-16-le")) // 2
    names = [text.encode("utf-16-le")[2 * e.offset:2 * (e.offset + e.length)].decode("utf-16-le") for e in report.heading_entities(text)]
    assert names == ["Weather station", "Air quality", "Pollen & asthma", "Forecast"]
    assert all(e.type == "bold" for e in report.heading_entities(text))
    after_emoji = report.heading_entities("• Rain today: ☔ 6.3 mm 🌧️\nForecast (Melbourne, 6:05pm)\n• Today")      # emoji count as two units
    assert [(e.offset, e.length) for e in after_emoji] == [(units("• Rain today: ☔ 6.3 mm 🌧️\n"), len("Forecast"))]
    assert report.heading_entities("Forecast\n• x") and report.heading_entities("• Forecast: sunny\nForecasts") == []
