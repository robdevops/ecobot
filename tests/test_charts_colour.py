from datetime import timezone

import pytest
from matplotlib.colors import to_rgb

from lib import charts
from lib.specs import Chart, Line, Panel


def luminance(hex_colour):
    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in to_rgb(hex_colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def test_a_ramp_blends_between_its_stops_and_holds_at_the_ends():
    temp = charts.ramp("temperature")
    assert temp(-50) == temp(-5) == "#312e81" and temp(99) == temp(38) == "#f59e0b"
    assert temp(5) == "#4338ca" and temp(10) != temp(20)
    assert charts.ramp("wind") is None and charts.ramp("pressure") is None


@pytest.mark.parametrize("scale", [*charts.GRADIENTS.values(), *charts.INDOOR_GRADIENTS.values()])
def test_every_stop_is_deep_enough_for_the_white_text_of_a_value_pill(scale):
    for _, colour in scale:
        assert (1.05) / (luminance(colour) + 0.05) >= 2.0, colour   # white against the colour


def test_an_outdoor_line_of_these_readings_is_an_ink_that_knows_its_colour_at_each_value():
    line = Line("Outdoor", [1, 2, 3], [5.0, 20.0, 30.0], reading="temperature")
    ink = charts._colour(line, 0)
    assert isinstance(ink, str) and ink.startswith("#") and ink.at(5) != ink.at(30) and ink.at(30) == charts.ramp("temperature")(30)
    indoor = charts._colour(Line("Indoor", [1, 2], [20.0, 21.0], reading="temperature", indoor=True), 0)
    assert isinstance(indoor, charts.Ink) and indoor.at(20) == charts.ramp("temperature", True)(20)
    flat = charts._colour(Line("Indoor", [1, 2], [50.0, 60.0], reading="humidity", indoor=True), 0)    # no indoor scale: one solid colour
    assert flat == charts.INDOOR_COLOURS["humidity"] and not isinstance(flat, charts.Ink) and charts._at(flat, 10) == flat
    assert charts._at(ink, 5) == ink.at(5)
    assert not isinstance(charts._colour(Line("W", [1, 2], [1.0, 2.0], reading="wind"), 0), charts.Ink)


@pytest.mark.parametrize("reading,unit", [("temperature", "°C"), ("humidity", "%"), ("dew_point", "°C")])
def test_charts_of_lines_drawn_by_value_render_alone_banded_and_stacked(reading, unit):
    xs = list(range(1_790_000_000, 1_790_000_000 + 48 * 1800, 1800))
    ys = [10 + (i % 24) * 0.8 for i in range(48)]
    plain = Line("Outdoor", xs, ys, reading=reading)
    banded = Line("Outdoor", xs, ys, [y - 2 for y in ys], [y + 2 for y in ys], reading=reading)
    indoor = Line("Indoor", xs, [20.0] * 48, reading=reading, indoor=True)
    for lines in ([plain], [banded], [plain, indoor]):
        assert charts.render(Chart("T", "s", [Panel("P", unit, lines, reading=reading)]), timezone.utc)[:4] == b"\x89PNG"
    two = Chart("T", "s", [Panel("P", unit, [plain], reading=reading), Panel("Q", unit, [banded], reading=reading)])
    assert charts.render(two, timezone.utc)[:4] == b"\x89PNG"


def hue(hex_colour):
    import colorsys
    return colorsys.rgb_to_hsv(*to_rgb(hex_colour))[0] * 360


def test_the_indoor_temperature_scale_shares_no_hue_with_the_outdoor_one():
    indoor = [hue(c) for _, c in charts.INDOOR_GRADIENTS["temperature"]]
    outdoor = [hue(c) for _, c in charts.GRADIENTS["temperature"]]
    apart = lambda a, b: min(abs(a - b), 360 - abs(a - b))
    assert all(apart(i, o) > 20 for i in indoor for o in outdoor)   # at least 20 degrees of hue apart


def test_a_short_period_line_loses_its_range_unless_it_is_wind_or_the_period_is_long():
    from lib.lines import SHORT_DAYS, Plotted

    def banded():
        return Plotted([1, 2, 3], [1.0, 2.0, 3.0], 1800, True, [0.0, 1.0, 2.0], [2.0, 3.0, 4.0])
    assert banded().unbanded(7).low is None and banded().unbanded(SHORT_DAYS).high is None
    assert banded().unbanded(7, keep=True).low is not None                # wind: the mean shaded up to the gusts
    assert banded().unbanded(SHORT_DAYS + 1).low is not None              # a quarter keeps its daily range


def labels_of(chart):
    import matplotlib.pyplot as plt
    seen = []
    original = plt.Axes.annotate

    def record(self, text, *a, **k):
        seen.append(text)
        return original(self, text, *a, **k)
    plt.Axes.annotate = record
    try:
        charts.render(chart, timezone.utc)
    finally:
        plt.Axes.annotate = original
    return seen


def one_line(reading, ys):
    xs = list(range(1_790_000_000, 1_790_000_000 + len(ys) * 1800, 1800))
    return Chart("T", "s", [Panel("P", "", [Line("L", xs, ys, reading=reading)], reading=reading)])


@pytest.mark.parametrize("reading", ["pressure", "co2", "voc_index", "nox_index", "solar", "wind", "vpd"])
def test_these_readings_show_their_latest_value_beside_the_line(reading):
    ys = [10.0 + (i % 7) for i in range(40)] + [13.4]
    assert "13.4" in labels_of(one_line(reading, ys))


def test_a_zero_wind_or_solar_reading_is_labelled_but_a_zero_vpd_is_not_and_other_readings_are_not_labelled():
    flat = [5.0 + (i % 6) for i in range(40)] + [0.0]
    assert "0" in labels_of(one_line("wind", flat)) and "0" in labels_of(one_line("solar", flat))
    assert "0" not in labels_of(one_line("vpd", flat))
    assert "52.3" not in labels_of(one_line("humidity", [50.0 + (i % 7) for i in range(40)] + [52.3]))   # a peak or low is tagged, the latest is not


def test_an_end_value_is_never_written_in_exponent_form():
    assert charts._figure(1014.8) == "1014.8" and charts._figure(1015.0) == "1015" and charts._figure(67.53) == "67.5"
    assert charts._figure(0.858) == "0.858" and charts._figure(2.0) == "2" and charts._figure(0.0) == "0"
