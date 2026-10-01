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
POLLEN = '{"lines": ["Grass pollen: 🟢 Low", "Thunderstorm asthma risk: 🟢 Low"], "source": "melbournepollen.com.au"}'
FORECAST = ('{"lines": ["Today: 🌦️ Showers. 11–16°C, 97% chance of rain", "Saturday: ⛈️ Thunderstorm. 12–20°C, 95% chance of rain"], '
            '"tag": "Open-Meteo", "source": "Open-Meteo (the BOM was unavailable)"}')

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

Pollen & asthma
• Grass pollen: 🟢 Low
• Thunderstorm asthma risk: 🟢 Low

Forecast [Open-Meteo]
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
    assert got[-2:] == ["• Sun: solar radiation 612.5 W/m², UV index 7", "• Wind: 14.0 km/h from 131°, gust 31.0 km/h"]
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
        "• Sun: ☀️ solar radiation 800.0 W/m², 🧴 UV index 10",
        "• Wind: 🌪️ 55.0 km/h from 90°, gust 🌪️ 70.0 km/h"]
