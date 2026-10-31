from datetime import timezone

import pytest

from lib import charts
from lib.specs import Chart, Line, Panel


def labels_of(chart):
    """The labels drawn on the chart (values, pills, numbers), in the order they were laid out; the headline first."""
    return [t.text for t in charts.draw_texts(chart, timezone.utc)]


def one_line(reading, ys):
    xs = list(range(1_790_000_000, 1_790_000_000 + len(ys) * 1800, 1800))
    return Chart("T", "s", [Panel("P", "", [Line("L", xs, ys, reading=reading)], reading=reading)])


READINGS = ["pressure", "co2", "voc_index", "nox_index", "solar", "wind", "vpd", "humidity", "temperature", "uv", "pm2_5"]


@pytest.mark.parametrize("reading", READINGS)
def test_every_line_ends_with_its_latest_value_beside_it(reading):
    ys = [10.0 + (i % 7) for i in range(40)] + [13.4]
    assert labels_of(one_line(reading, ys))[-1] == "13.4"                  # after any peak or low that is tagged


@pytest.mark.parametrize("reading", READINGS)
def test_a_latest_value_of_zero_is_labelled_too(reading):
    flat = [5.0 + (i % 6) for i in range(40)] + [0.0]
    assert labels_of(one_line(reading, flat))[-1] == "0"
    assert labels_of(one_line(reading, flat[:-1] + [0.4]))[-1] == "0.4"


def test_the_wind_rose_reaches_5_percent_past_its_longest_wedge():
    assert charts._rose_radius([[10.0, 30.0, 40.0], [5.0, 5.0, 20.0]]) == pytest.approx(42.0)
    assert charts._rose_radius([[0.0, 0.0, 0.0]] * 16) == 1.0                    # nothing to draw


def test_an_end_value_is_never_written_in_exponent_form():
    assert charts._figure(1014.8) == "1014.8" and charts._figure(1015.0) == "1015" and charts._figure(67.53) == "67.5"
    assert charts._figure(0.858) == "0.858" and charts._figure(2.0) == "2" and charts._figure(0.0) == "0"


def annotations_of(chart):
    """[(text, which side of its point the pill sits)] for every label drawn."""
    return [(t.text, t.va) for t in charts.draw_texts(chart, timezone.utc)]


def test_two_lows_that_end_together_at_the_right_edge_get_pills_on_opposite_sides():
    """Humidity outdoors and indoors both ending at their lows: the pills used to sit on top of each other (both below their points)."""
    xs = list(range(1_790_000_000, 1_790_000_000 + 49 * 1800, 1800))
    steps = range(49)
    outdoor = [90.0 - 44.7 * i / 48 for i in steps]                  # ends at its low, 45.3
    indoor = [58.0 - 10.0 * i / 48 for i in steps]                   # ends at its low, 48
    floor = [30.0 + (i % 3) for i in steps]                          # something lower, so neither low is on the floor
    chart = Chart("T", "s", [Panel("Humidity", "%", [Line("Outdoor", xs, outdoor, reading="humidity"), Line("Indoor", xs, indoor, reading="humidity"),
                                                    Line("Other", xs, floor, reading="humidity")], reading="humidity")])
    sides = dict(annotations_of(chart))
    assert {sides["45.3 %"], sides["48.0 %"]} == {"top", "bottom"}, sides                       # never the same side


def test_a_temperature_low_just_above_zero_is_labelled():
    """Only a reading that can't go below zero (rain, wind, sun, air) has its axis floored at 0 and its low on the floor left
    unlabelled; a cold night of 0.4° is a record worth its label."""
    ys = [12.0, 8.0, 0.4, 6.0, 15.0, 20.0, 18.0]
    chart = Chart("T", "s", [Panel("Temperature", "°C", [one_line("temperature", ys).panels[0].lines[0]], reading="temperature")])
    assert "0.4°" in labels_of(chart)
    assert "0" not in labels_of(one_line("wind", [3.0, 0.0, 5.0, 9.0]))   # a calm low on the floor still says nothing


def test_a_day_of_25_hours_is_drawn_25_hours_long_with_its_ticks_on_local_hours():
    """5 Apr 2026 in Melbourne: 2am to 3am happens twice. The axis is real time, so a line through it never runs backwards (on
    local clock times it drew that hour twice), and the hour ticks still fall on local 0, 3, 6 ... o'clock."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from lib.draw import Canvas
    tz = ZoneInfo("Australia/Melbourne")
    start = int(datetime(2026, 4, 5, tzinfo=tz).timestamp())
    xs = list(range(start, start + 25 * 3600, 1800))
    ax = charts.Axes(Canvas(100, 100), [0, 0, 1, 1], xs[0], xs[-1])
    assert all(ax.X(b) > ax.X(a) for a, b in zip(xs, xs[1:])) and (xs[-1] - xs[0]) / 3600 == 24.5   # real time: never backwards
    ticks = [datetime.fromtimestamp(t, tz) for t, _ in charts._time_ticks(xs[0], xs[-1], (xs[-1] - xs[0]) / 86400, tz)]
    assert ticks and {t.hour for t in ticks} <= {0, 3, 6, 9, 12, 15, 18, 21} and ticks[0].day == 5
    assert [t.hour for t in ticks].count(3) == 1 and len(ticks) == 8          # 3am once, and every 3 hours to 9pm ticked


def test_every_weather_reading_and_air_metric_has_a_colour_of_its_own():
    from lib import charts
    from lib.series import AIR, WEATHER
    lines = [name for name in WEATHER if name != "rain"]          # rain is drawn as bars, in its own blue
    assert not [name for name in (*lines, *AIR) if name not in charts.READING_COLOURS]


def test_two_panels_of_lines_become_one_panel_with_a_scale_each_and_nothing_else_does():
    from lib.panels import panel_for, two_axes
    from lib.specs import Bars

    def made(reading, label):
        return panel_for(reading, [Line(label, [1, 2, 3], [1.0, 2.0, 3.0], reading=reading)])
    uv, solar = made("uv", "UV index"), made("solar", "Solar")
    both = two_axes([uv, solar])
    assert both.label == "UV index and Solar radiation" and both.unit == "" and both.peaks == "none"
    assert [(s.label, s.right, s.reading) for s in both.lines] == [("UV index", False, "uv"), ("Solar radiation (W/m²)", True, "solar")]
    rain = panel_for("rain", bars=Bars("Rain", "mm", [1], [1.0], 3600))
    assert two_axes([uv]) is None and two_axes([uv, rain]) is None
    assert not both.flip_right and two_axes([uv, solar], flip_right=True).flip_right
    with pytest.raises(ValueError):
        Panel("x", "", [Line("a", [1, 2], [1.0, 2.0], right=True)])      # a right-hand scale needs a line on the left
    wind = made("pm2_5", "PM2.5")
    pairs = two_axes([uv, solar, uv, solar])                              # more than two: a side for each unit
    assert [s.right for s in pairs.lines] == [False, True, False, True]
    assert two_axes([uv, solar, wind]) is None                            # three units: no two sides to put them on


def test_a_panel_with_two_scales_is_drawn_with_both_latest_values_and_a_right_axis():
    from lib.panels import panel_for, two_axes
    xs = list(range(1_790_000_000, 1_790_000_000 + 60 * 1800, 1800))
    uv = panel_for("uv", [Line("UV index", xs, [float(i % 11) for i in range(60)], reading="uv")])
    solar = panel_for("solar", [Line("Solar", xs, [float(i % 11) * 90 + 40.5 for i in range(60)], reading="solar")])
    panel = two_axes([uv, solar])
    chart = Chart(panel.label, "s", [panel])
    assert labels_of(chart)[-2:] == ["4", "400.5"]                          # each line's latest value, by its own scale
    assert charts.render(chart, timezone.utc)[:4] == b"\x89PNG"
