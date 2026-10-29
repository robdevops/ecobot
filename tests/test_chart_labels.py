from datetime import timezone

import pytest

from lib import charts
from lib.specs import Chart, Line, Panel


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


def test_the_wind_rose_is_scaled_as_matplotlib_scales_its_bars():
    """The rose is one collection of wedges, not polar bars, so its radius is set by hand: it must be the one the bars got (5% room
    past the longest wedge, none when that wedge ends in an empty step), so the rose keeps its size whatever the readings."""
    import random

    import matplotlib.pyplot as plt
    import numpy as np
    rng = random.Random(3)
    roses = [[[4, 2, 1]] * 16, [[3 + k % 5, 0, 0] for k in range(16)], [[0, 0, 0]] * 3 + [[5, 0, 2]] + [[0, 0, 0]] * 12,
             [[4, 2, 1]] * 5 + [[9, 4, 0]] + [[4, 2, 1]] * 10]
    for _ in range(8):
        rose = [[rng.choice([0, 0, rng.randint(1, 40)]) for _ in range(3)] for _ in range(16)]
        rose[rng.randrange(16)][0] += 1                                       # a rose is only drawn when there are readings
        roses.append(rose)
    for rose in roses:
        percent = np.asarray(rose, dtype=float) / np.sum(rose) * 100
        ax = plt.figure().add_axes([0, 0, 1, 1], projection="polar")
        bottom = np.zeros(16)
        for i in range(3):                                                    # the rose as it was drawn before
            ax.bar(np.deg2rad(np.arange(16) * 22.5), percent[:, i], width=np.deg2rad(20), bottom=bottom)
            bottom += percent[:, i]
        assert charts._rose_radius(np.cumsum(percent, axis=1)) == pytest.approx(ax.get_ylim()[1], rel=1e-12), rose
        plt.close(ax.figure)


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
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import numpy as np
    tz = ZoneInfo("Australia/Melbourne")
    start = int(datetime(2026, 4, 5, tzinfo=tz).timestamp())
    xs = charts._nums(range(start, start + 25 * 3600, 1800))
    assert np.all(np.diff(xs) > 0) and round((xs[-1] - xs[0]) * 24, 3) == 24.5
    fig, ax = plt.subplots()
    try:
        ax.set_xlim(xs[0], xs[-1])
        charts._time_axis(ax, xs[-1] - xs[0], tz)
        ticks = [mdates.num2date(t, tz) for t in ax.xaxis.get_major_locator()()]
    finally:
        plt.close(fig)
    assert ticks and {t.hour for t in ticks} <= {0, 3, 6, 9, 12, 15, 18, 21} and ticks[0].day == 5


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
