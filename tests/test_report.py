import json

from lib import report

# What `python scripts/report_inputs.py` printed on the real station (rain, night)
NOW = ('{"time":"Fri 02 Oct 2026 02:21","outdoor":{"temperature":"12.3 ℃","feels_like":"12.3 ℃","app_temp":"12.7 ℃","dew_point":"11.3 ℃",'
       '"vpd":"0.085 kPa","humidity":"94 %"},"indoor":{"temperature":"18.3 ℃","humidity":"63 %","dew_point":"11.1 ℃","feels_like":"18.3 ℃",'
       '"app_tempin":"18.7 ℃"},"solar_and_uvi":{"solar":"0.0 W/m²","uvi":"0"},"rainfall":{"rain_rate":"1.8 mm/hr","daily":"6.3 mm",'
       '"event":"49.8 mm","1_hour":"1.3 mm","weekly":"49.8 mm","monthly":"49.8 mm","yearly":"5105.7 mm"},"wind":{"wind_speed":"0.0 km/h",'
       '"wind_gust":"0.0 km/h","wind_direction":"131 º"},"pressure":{"relative":"1025.0 hPa","absolute":"1009.5 hPa"},'
       '"rain_outlook":"raining now (4.8 mm/h)","emoji":{"outdoor.temperature":"🧥","outdoor.feels_like":"🧥","outdoor.app_temp":"🧥",'
       '"outdoor.humidity":"💦","indoor.temperature":"🧥","indoor.feels_like":"🧥","indoor.app_tempin":"🧥","rainfall.rain_rate":"🌧️",'
       '"rainfall.daily":"☔","pressure.relative":"🗜️"}}')
AIR = ('{"sensor": "Fairleigh", "sensor_type": "outdoor", "time": "Fri 2 Oct 2026 2:22am", "pm2_5": {"value": 1.4, "unit": "µg/m³", '
       '"rating": "🟢 good", "aqi_us": 8, "band": "good"}, "pm10": {"value": 3.0, "unit": "µg/m³", "rating": "🟢 good"}, "pm1": {"value": 1.0, '
       '"unit": "µg/m³", "rating": "🟢 good"}, "co2": {"value": 433.0, "unit": "ppm", "rating": "🟢 good"}, "voc_index": {"value": 88.0, '
       '"unit": "relative index (100 = this sensor\'s recent average)", "rating": "🟢 good"}, "nox_index": {"value": 1.0, '
       '"unit": "relative index (1 = baseline)", "rating": "🟢 good"}}')
POLLEN = '{"lines": ["Grass pollen: 🟢 Low", "Thunderstorm asthma risk: 🟢 Low"], "place": "Melbourne", "source": "melbournepollen.com.au"}'
FORECAST = ('{"lines": ["Today: 🌦️ Showers. 11–16°C, 97% chance of rain", "Saturday: ⛈️ Thunderstorm. 12–20°C, 95% chance of rain"], '
            '"place": "Melbourne", "source": "Open-Meteo"}')

WEATHER = """\
• Outdoor: 🧥 12.3 °C, 💦 94 %, dew point 11.3 °C, VPD 0.085 kPa
• Indoor: 🧥 18.3 °C, 63 %
• Pressure: 🗜️ 1025.0 hPa
• Rain today: ☔ 6.3 mm (month total 49.8 mm); 🌧️ raining now (4.8 mm/h)"""   # night and calm: no Sun or Wind line


def test_the_report_is_laid_out_in_code_with_emoji_before_the_readings_they_belong_to():
    got = report.report({"weather_now": NOW, "air_quality": AIR, "pollen_asthma": POLLEN, "weather_forecast": FORECAST})
    assert got == f"""Weather station
{WEATHER}

Air quality
• PM2.5: 1.4 µg/m³ 🟢 good (AQI 8)
• PM10: 3.0 µg/m³ 🟢 good
• PM1: 1.0 µg/m³ 🟢 good
• CO₂: 433 ppm 🟢 good
• VOC index: 88 🟢 good
• NOx index: 1.0 🟢 good

Pollen & asthma (Melbourne)
• Grass pollen: 🟢 Low
• Thunderstorm asthma risk: 🟢 Low

Forecast (Melbourne)
• Today: 🌦️ Showers. 11–16°C, 97% chance of rain
• Saturday: ⛈️ Thunderstorm. 12–20°C, 95% chance of rain"""
    assert not got.startswith("Current report")


def test_weather_now_is_just_the_weather_stations_bullets():
    assert report.weather_now(NOW) == WEATHER
    assert report.weather_now("Error calling tool: timed out") is None and report.weather_now(None) is None


def test_a_dry_calm_day_has_no_rain_line_noise_and_a_likely_rain_outlook_is_shortened():
    dry = ('{"outdoor":{"temperature":"21.0 ℃","humidity":"50 %"},"rainfall":{"rain_rate":"0.0 mm/hr","daily":"0.0 mm","monthly":"12.0 mm"},'
           '"wind":{"wind_speed":"12.0 km/h","wind_gust":"20.0 km/h","wind_direction":"270 º"},"emoji":{}}')
    lines = report.weather_now(dry).splitlines()
    assert lines == ["• Outdoor: 21.0 °C, 50 %", "• Rain today: 0.0 mm (month total 12.0 mm)", "• Wind: 12.0 km/h from 270°, gust 20.0 km/h"]
    likely = dry.replace('"emoji"', '"rain_outlook":"rain looks likely soon: pressure down 3.0 hPa in 3 hours (an estimate from the station\'s '
                                     'readings, not an official forecast)","emoji"')
    assert "• Rain today: 0.0 mm (month total 12.0 mm); rain looks likely soon: pressure down 3.0 hPa in 3 hours (an estimate)" in report.weather_now(likely)
    drizzle = dry.replace('"0.0 mm/hr"', '"0.6 mm/hr"').replace('"emoji":{}', '"emoji":{"rainfall.rain_rate":"🌧️"}')
    assert "; 🌧️ raining (0.6 mm/h)" in report.weather_now(drizzle)


def test_a_failed_or_missing_source_is_left_out_or_said_so_and_the_forecast_without_a_tag_has_a_plain_heading():
    got = report.report({"weather_now": "Error calling tool: boom", "air_quality": AIR,
                         "pollen_asthma": '{"error": "No pollen levels are available right now."}',
                         "weather_forecast": '{"lines": ["Today: ☀️ Sunny. 20–25°C"]}'})
    assert got.startswith("Weather station\n• not available right now\n\nAir quality\n• PM2.5:")
    assert "Pollen" not in got and got.endswith("Forecast\n• Today: ☀️ Sunny. 20–25°C")
    assert report.report({}) == "No readings are available right now."


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
        "• Outdoor: 🔥 36.0 °C, 💦 90 %, dew point 18.0 °C, 🧽 VPD 1.300 kPa",
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


def test_a_rain_chart_caption_is_the_least_and_most_rain_then_whether_more_is_expected():
    from zoneinfo import ZoneInfo
    turn = _turn()
    turn.chart_fields = ["rain"]
    result = json.dumps({"period": "Tue 29 Sep 2026 - Tue 06 Oct 2026", "series": {"rainfall.daily": {"unit": "mm", "low": "0", "high": "7.2"}}})
    now = json.dumps({"rain_outlook": "Rain looks likely soon: pressure falling. (An estimate from the station's readings, not an official forecast)"})
    lines = report.chart_caption("weather_history", {}, result, turn, ZoneInfo("UTC"), now).splitlines()
    assert lines[1].startswith("☔ Rain per day: least 0 mm (") and "most 7.2 mm (" in lines[1] and "8.6 mm in all" in lines[1]
    assert lines[2] == "☔ Rain looks likely soon: pressure falling. (an estimate)"
    dry = report.chart_caption("weather_history", {}, result, turn, ZoneInfo("UTC"), "{}")
    assert dry.splitlines()[2] == "☔ No rain is expected soon."


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
