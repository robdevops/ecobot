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
    assert temp(-50) == temp(-5) == "#4c1d95" and temp(99) == temp(42) == "#a21caf"
    assert temp(5) == "#2563eb" and temp(10) != temp(20)
    assert charts.ramp("wind") is None and charts.ramp("pressure") is None


@pytest.mark.parametrize("reading", list(charts.GRADIENTS))
def test_every_stop_is_deep_enough_for_the_white_text_of_a_value_pill(reading):
    for _, colour in charts.GRADIENTS[reading]:
        assert (1.05) / (luminance(colour) + 0.05) >= 2.0, colour   # white against the colour


def test_an_outdoor_line_of_these_readings_is_an_ink_that_knows_its_colour_at_each_value():
    line = Line("Outdoor", [1, 2, 3], [5.0, 20.0, 30.0], reading="temperature")
    ink = charts._colour(line, 0)
    assert isinstance(ink, str) and ink.startswith("#") and ink.at(5) != ink.at(30) and ink.at(30) == charts.ramp("temperature")(30)
    indoor = charts._colour(Line("Indoor", [1, 2], [20.0, 21.0], reading="temperature", indoor=True), 0)
    assert indoor == charts.INDOOR_COLOURS["temperature"] and not isinstance(indoor, charts.Ink)
    assert charts._at(indoor, 10) == indoor and charts._at(ink, 5) == ink.at(5)
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
