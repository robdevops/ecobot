"""The server's instructions (lib/service/instructions.py; docs/mcp-service.md 3.2): the devices and their nicknames, Indoor and Outdoor,
the units, built from the Context's sources and the catalogue."""

from zoneinfo import ZoneInfo

from lib.service.context import Context
from lib.service.instructions import INSTRUCTIONS, instructions, names, units
from lib.service.names import CATALOGUE, Catalogue

MELBOURNE = ZoneInfo("Australia/Melbourne")


def test_the_default_names_every_device_its_nicknames_indoor_and_outdoor_and_the_units():
    text = INSTRUCTIONS
    for words in ('"ecowitt"', '"the weather station"', "AirGradient", '"ag"', '"airgradient"', '"air gradient"', "Open-Meteo",
                  "pollen", "Indoor and Outdoor", "outdoor.temperature, indoor.temperature", "air.temperature and air.humidity",
                  "no indoor air quality", "°C: temperature", "%: humidity", "hPa: pressure", "km/h: wind speed, wind gust",
                  "mm: rain", "µg/m³: PM2.5, PM10, PM1", "ppm: CO₂", "no unit (an index): UV index, VOC index, NOx index",
                  "person's own words"):
        assert words in text, words
    assert len(text) < 1500 and text == instructions()
    # every reading's name (a live run made up rain.rain_rate for rainfall.rain_rate)
    assert "Names (sensor.reading): outdoor and indoor: temperature, humidity, dew_point" in text
    assert "rainfall and rainfall_piezo: rain_rate, rain" in text and "air: pm2_5, pm10" in text


def test_names_list_each_sensor_with_its_readings_sensors_alike_together():
    listed = names(CATALOGUE)
    assert listed.startswith("outdoor and indoor: temperature, humidity") and "; pressure: relative, absolute; " in listed
    assert all(n.partition(".")[2] in listed for n in CATALOGUE.names)


def test_units_list_each_unit_once_with_its_readings_sides_and_devices_aside():
    listed = units(CATALOGUE)
    assert listed.count("°C:") == 1 and "Outdoor" not in listed and "Indoor" not in listed and "AirGradient" not in listed
    assert listed.split("; ")[0] == "°C: temperature, dew point, feels like, apparent temperature"


def test_a_service_without_the_air_sensor_forecast_or_pollen_names_only_what_it_has_and_its_zone():
    station = Catalogue(r for r in CATALOGUE if not r.name.startswith("air."))
    text = instructions(Context(eco=object(), catalogue=station, tz=MELBOURNE))
    assert "Ecowitt" in text and "AirGradient" not in text and "µg/m³" not in text and "air.temperature" not in text
    assert "Open-Meteo" not in text and "pollen" not in text
    assert "The station's clock is Australia/Melbourne time" in text
    # the catalogue isn't read yet (every reading known): only the devices there are give units
    assert "ppm" not in instructions(Context(eco=object(), tz=MELBOURNE))
    air = instructions(Context(air=object(), tz=MELBOURNE))
    assert "AirGradient" in air and "Ecowitt" not in air and "Indoor and Outdoor" not in air and "°C: temperature" in air
