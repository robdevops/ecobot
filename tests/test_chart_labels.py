from datetime import timezone

import pytest

from lib import charts
from lib.specs import Chart, Line, Panel


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


@pytest.mark.parametrize("reading", ["pressure", "co2", "voc_index", "nox_index", "solar", "wind", "vpd"])
def test_a_latest_value_of_zero_is_not_labelled_for_any_of_them_and_other_readings_are_not_labelled(reading):
    flat = [5.0 + (i % 6) for i in range(40)] + [0.0]
    assert "0" not in labels_of(one_line(reading, flat))
    assert "0.4" in labels_of(one_line(reading, flat[:-1] + [0.4]))       # a tiny but non-zero value still is
    assert "52.3" not in labels_of(one_line("humidity", [50.0 + (i % 7) for i in range(40)] + [52.3]))   # a peak or low is tagged, the latest is not


def test_an_end_value_is_never_written_in_exponent_form():
    assert charts._figure(1014.8) == "1014.8" and charts._figure(1015.0) == "1015" and charts._figure(67.53) == "67.5"
    assert charts._figure(0.858) == "0.858" and charts._figure(2.0) == "2" and charts._figure(0.0) == "0"


def annotations_of(chart):
    """[(text, which side of its point the pill sits)] for every label drawn."""
    import matplotlib.pyplot as plt
    seen = []
    original = plt.Axes.annotate

    def record(self, text, *a, **k):
        seen.append((text, k.get("va")))
        return original(self, text, *a, **k)
    plt.Axes.annotate = record
    try:
        charts.render(chart, timezone.utc)
    finally:
        plt.Axes.annotate = original
    return seen


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
