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


# ---------- four lines, two scales, one panel (temperature of both sensors left, humidity of both right, upside down) ----------
def four_lines(flip: bool = False, days: int = 1) -> Chart:
    from lib.panels import panel_for, two_axes
    n = 48 * days
    xs = list(range(1_790_000_000, 1_790_000_000 + n * 1800, 1800))
    up = [i / (n - 1) for i in range(n)]   # a day that warms and dries
    spec = [("temperature", "Ecowitt temperature", [10 + 10 * u for u in up]), ("air_temperature", "AirGradient temperature", [11 + 10 * u for u in up]),
            ("humidity", "Ecowitt humidity", [80 - 40 * u for u in up]), ("air_humidity", "AirGradient humidity", [78 - 40 * u for u in up])]
    panels = [panel_for(name if name in ("temperature", "humidity") else name.removeprefix("air_"), [
        Line(label, xs, ys, low=[y - 0.8 for y in ys], high=[y + 0.8 for y in ys], reading=name)]) for name, label, ys in spec]
    for panel, (_, label, _) in zip(panels, spec):
        panel.label = label
    panel = two_axes(panels, flip_right=flip)
    return Chart(panel.label, "s", [panel])


def pixels(chart: Chart):
    from io import BytesIO

    from PIL import Image
    return Image.open(BytesIO(charts.render(chart, timezone.utc))).convert("RGB")


def near(rgb, hex_colour: str, tolerance: int = 40) -> bool:
    want = tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return all(abs(a - b) <= tolerance for a, b in zip(rgb, want))


def colour_columns(image, hex_colour: str, x0: int, x1: int) -> list[int]:
    """The columns of x0..x1 that hold a pixel of that colour."""
    return [x for x in range(x0, x1) if any(near(image.getpixel((x, y)), hex_colour) for y in range(image.height))]


def test_four_lines_on_two_scales_are_one_panel_one_colour_family_for_each_scale():
    chart = four_lines()
    (panel,) = chart.panels
    assert [(s.label, s.right) for s in panel.lines] == [("Ecowitt temperature (°C)", False), ("AirGradient temperature (°C)", False),
                                                          ("Ecowitt humidity (%)", True), ("AirGradient humidity (%)", True)]
    assert [charts._colour(s, i, panel.reading) for i, s in enumerate(panel.lines)] == [
        charts.READING_COLOURS[r] for r in ("temperature", "air_temperature", "humidity", "air_humidity")]
    warm, cool = [charts.READING_COLOURS[r] for r in ("temperature", "air_temperature")], [charts.READING_COLOURS[r] for r in ("humidity", "air_humidity")]
    assert all(int(c[1:3], 16) > int(c[5:7], 16) for c in warm) and all(int(c[5:7], 16) > int(c[1:3], 16) for c in cool)   # red beats blue, blue beats red


def test_each_of_the_four_lines_is_labelled_with_its_latest_value_and_the_right_scale_can_run_downwards():
    upright, inverted = four_lines(), four_lines(flip=True)
    for chart in (upright, inverted):
        ends = [t for t in charts.draw_texts(chart, timezone.utc) if t.text in ("20", "21", "40", "38")]
        assert sorted(t.text for t in ends) == ["20", "21", "38", "40"]            # every line ends with its latest value
    y = lambda chart, text: next(t.y for t in charts.draw_texts(chart, timezone.utc) if t.text == text)
    assert y(upright, "40") > y(upright, "20") + 50     # a humidity that fell ends below a temperature that rose
    assert y(inverted, "40") < y(inverted, "20") + 50 and y(inverted, "40") < y(upright, "40") - 100   # upside down it ends high: 80 is the bottom
    assert y(inverted, "38") < y(upright, "38") - 100 and y(inverted, "20") == pytest.approx(y(upright, "20"))   # the left scale is untouched


@pytest.mark.parametrize("days", [1, 90])
@pytest.mark.parametrize("flip", [False, True])
def test_the_two_scales_ticks_stand_outside_the_plot_in_their_lines_colours_with_their_units(flip, days):
    chart = four_lines(flip, days)
    image = pixels(chart)
    plot_left, plot_right = round(charts.AX_RECT[0] * image.width), round((charts.AX_RECT[0] + charts._axes_width(chart)) * image.width)
    assert image.size == (1280, 720)
    left = colour_columns(image, charts.READING_COLOURS["temperature"], 0, plot_left - 2)          # the left ticks: the first line's colour
    right = colour_columns(image, charts.READING_COLOURS["humidity"], plot_right + 2, image.width)   # the right ticks: the second scale's first
    assert left and right and max(left) < plot_left and min(right) > plot_right
    assert min(left) >= 0 and max(right) < image.width                           # the text is not cut off by the picture's edge


def test_a_scales_ticks_carry_a_short_unit_and_a_long_one_stays_in_the_key():
    a = lambda *names: [Line(n, [1, 2], [1.0, 2.0]) for n in names]
    assert charts._scale_unit(a("Ecowitt temperature (°C)", "AirGradient temperature (°C)")) == "°C"
    assert charts._scale_unit(a("Humidity (%)")) == "%"
    assert charts._scale_unit(a("Solar radiation (W/m²)")) == ""                    # too wide for a tick
    assert charts._scale_unit(a("Temperature (°C)", "Humidity (%)")) == "" and charts._scale_unit(a("UV index")) == ""


def test_the_key_names_all_four_lines_with_their_units_and_the_range_of_each_is_shaded(monkeypatch):
    chart, keys, real = four_lines(), [], charts._legend
    monkeypatch.setattr(charts, "_legend", lambda canvas, entries, **kw: (keys.append(entries), real(canvas, entries, **kw))[1])
    pixels(chart)
    assert keys == [[(charts.READING_COLOURS["temperature"], "Ecowitt temperature (°C)"), (charts.READING_COLOURS["air_temperature"], "AirGradient temperature (°C)"),
                     (charts.READING_COLOURS["humidity"], "Ecowitt humidity (%)"), (charts.READING_COLOURS["air_humidity"], "AirGradient humidity (%)")]]
    image = pixels(chart)
    for reading in ("temperature", "air_temperature", "humidity", "air_humidity"):   # a band is the line's colour, faint: lighter than the line
        faint = charts._blend(charts.READING_COLOURS[reading], 0.8)
        assert any(near(image.getpixel((x, y)), faint, 14) for x in range(150, 1100, 7) for y in range(240, 600, 3)), reading


def test_readings_are_grouped_to_the_two_scales_by_unit_whatever_their_order():
    from lib.panels import panel_for, two_axes
    made = lambda reading, label: panel_for(reading, [Line(label, [1, 2, 3], [1.0, 2.0, 3.0], reading=reading)])
    t, at, h, ah = (made("temperature", "Ecowitt temperature"), made("temperature", "AirGradient temperature"),
                    made("humidity", "Ecowitt humidity"), made("humidity", "AirGradient humidity"))
    for order, sides in (([t, at, h, ah], [False, False, True, True]), ([t, h, at, ah], [False, True, False, True]),
                         ([h, ah, t, at], [False, False, True, True]), ([h, t, ah, at], [False, True, False, True])):
        merged = two_axes(order)
        assert [s.right for s in merged.lines] == sides
        assert [s.label for s in merged.lines if not s.right] == [p.label + f" ({p.unit})" for p, side in zip(order, sides) if not side]
    assert two_axes([h, t, ah, at]).label == "Humidity and Temperature"           # the first reading's unit is the left scale
    assert two_axes([t, at, h]) is not None and two_axes([t, h, made("pressure", "Pressure")]) is None   # a third unit has no side
