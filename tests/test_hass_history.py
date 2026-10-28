from lib.ecowitt.hass_history import Series, converted, history, merged, present, rollup
from lib.ecowitt.hass_map import ENTITIES, convert, entity_ids, written

NEVER = 10 ** 12


def test_a_buckets_value_is_told_from_the_states_in_it_and_the_one_held_as_it_began():
    states = [(0, 10.0), (100, 20.0), (250, 30.0), (400, 5.0)]
    assert rollup(states, "mean", 0, 599, 300, NEVER) == {0: (550 / 30, 10.0, 30.0), 300: (40 / 3, 5.0, 30.0)}   # 10 for 100 s, 20 for 150 s, 30 for 50 s ...
    assert rollup(states, "last", 0, 599, 300, NEVER) == {0: (30.0, 10.0, 30.0), 300: (5.0, 5.0, 30.0)}           # a counter: where it ended
    assert rollup(states, "max", 0, 599, 300, NEVER)[300] == (30.0, 5.0, 30.0)                                    # a gust: the highest in it, the held one counted


def test_a_bucket_with_no_value_is_left_out_and_one_still_going_is_told_as_far_as_it_has_gone():
    assert rollup([(500, 7.0)], "mean", 0, 599, 300, NEVER) == {300: (7.0, 7.0, 7.0)}                            # nothing was known before 500
    assert rollup([(0, 10.0), (350, 20.0)], "mean", 0, 599, 300, 450)[300] == ((10 * 50 + 20 * 100) / 150, 10.0, 20.0)   # told to now (450) only
    assert 300 in rollup([(0, 10.0)], "mean", 0, 599, 300, NEVER) and rollup([(0, 10.0)], "mean", 0, 599, 300, 300) == {0: (10.0, 10.0, 10.0)}   # not begun at all
    assert rollup([], "mean", 0, 599, 300, NEVER) == {}


def series(name: str, states: list) -> Series:
    return Series(ENTITIES[name], states)


def test_a_cycles_history_has_the_clouds_shape_with_lows_and_highs_for_every_cycle_but_the_minute():
    states = [(0, 10.04), (100, 20.0), (250, 30.0), (400, 5.0)]
    got = history([series("outdoor.temperature", states)], "5min", 0, 599, NEVER)
    assert got == {"outdoor": {"temperature": {"unit": "℃", "list": {"0": "18.3", "300": "13.3"}},
                               "temperature_low": {"unit": "℃", "list": {"0": "10.0", "300": "5.0"}},
                               "temperature_high": {"unit": "℃", "list": {"0": "30.0", "300": "30.0"}}}}
    minutes = history([series("outdoor.temperature", states)], "1min", 0, 359, NEVER)["outdoor"]
    assert set(minutes) == {"temperature"} and list(minutes["temperature"]["list"].items())[:3] == [("0", "10.0"), ("60", "20.0"), ("120", "20.0")]   # the state at each minute's end
    direction = history([series("wind.wind_direction", [(0, 350.0), (100, 10.0)])], "30min", 0, 1799, NEVER)["wind"]
    assert set(direction) == {"wind_direction"} and direction["wind_direction"]["list"] == {"0": "10"}              # a compass point: where it ended, no range
    gust = history([series("wind.wind_gust", [(0, 20.0), (100, 45.0), (200, 30.0)])], "30min", 0, 1799, NEVER)["wind"]["wind_gust"]
    assert gust["list"] == {"0": "45.0"}


def test_the_buckets_are_cut_on_the_clock_the_way_the_cloud_cuts_them():
    states = [(0, 1.0)]
    for cycle, width in (("5min", 300), ("30min", 1800), ("4hour", 14400), ("1day", 86400)):
        stamps = [int(ts) for ts in history([series("pressure.relative", states)], cycle, 86400 * 3 + 5, 86400 * 4 + 5000, NEVER)["pressure"]["relative"]["list"]]
        assert stamps and all(ts % width == 0 for ts in stamps) and stamps == sorted(stamps) and stamps[0] == (86400 * 3 + 5) // width * width


def test_every_series_is_written_in_the_clouds_units_and_as_a_number_it_can_read_back():
    assert {e.unit for e in ENTITIES.values()} <= {"℃", "%", "kPa", "hPa", "km/h", "º", "mm/hr", "mm", "W/m²", ""}
    assert written(-0.04, 1) == "0.0" and written(12.345, 1) == "12.3" and written(94.0, 0) == "94" and written(0.0851, 3) == "0.085"
    assert list(entity_ids("sensor.x_", ["outdoor.temperature", "wind.wind_gust"])) == ["sensor.x_outdoor_temperature", "sensor.x_wind_gust"]


def test_home_assistants_own_units_become_the_bots():
    assert convert(68.0, "temperature", "°F") == 20.0 and convert(293.15, "temperature", "K") == 20.0 and convert(20.0, "temperature", "°C") == 20.0
    assert round(convert(29.92, "pressure", "inHg"), 1) == 1013.2 and convert(101.3, "pressure", "kPa") == 1013.0 and convert(1000.0, "pressure", "hPa") == 1000.0
    assert round(convert(10, "speed", "mph"), 2) == 16.09 and convert(10, "speed", "m/s") == 36.0
    assert round(convert(0.5, "length", "in"), 1) == 12.7 and convert(5.0, "rate", "mm/h") == 5.0
    assert convert(850.0, "pressure", "hPa", 0.1) == 85.0 or round(convert(8.5, "pressure", "hPa", 0.1), 2) == 0.85       # the vapour pressure deficit: hPa to kPa
    assert convert(5.0, "", "odd") == 5.0 and convert(5.0, "speed", "furlongs per fortnight") == 5.0                      # a unit it does not know: taken as the bot's
    assert [round(v, 1) for _, v in converted([(0, 68.0)], ENTITIES["outdoor.temperature"], "°F")] == [20.0]


def test_two_chunks_of_one_history_and_a_cloud_that_fills_what_a_bucket_lacks_join_up():
    first = {"outdoor": {"temperature": {"unit": "℃", "list": {"0": "1.0", "300": "2.0"}}}}
    more = {"outdoor": {"temperature": {"unit": "℃", "list": {"300": "9.0", "600": "3.0"}}, "humidity": {"unit": "%", "list": {"0": "50"}}}}
    assert merged({"outdoor": {"temperature": {"unit": "℃", "list": dict(first["outdoor"]["temperature"]["list"])}}}, more)["outdoor"]["temperature"]["list"] == {"0": "1.0", "300": "2.0", "600": "3.0"}
    got = merged({"outdoor": {"temperature": {"unit": "℃", "list": {"0": "1.0", "300": "2.0"}}}}, more, frozenset({300}))   # 300 is one it knows only part of
    assert got["outdoor"]["temperature"]["list"] == {"0": "1.0", "300": "9.0", "600": "3.0"} and got["outdoor"]["humidity"]["list"] == {"0": "50"}
    assert present(more, ["outdoor", "wind"]) == {"outdoor": {0, 300, 600}, "wind": set()}
