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


# ---------- a days lookup (the model's day tools' charts that answer the question) ----------
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
