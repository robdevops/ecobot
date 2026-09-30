import pytest

from lib.airgradient import AirGradient
from lib.airgradient.metrics import AIR_PANELS
from lib.charts import render
from lib.specs import Bars, Chart, Compass, Line, Panel, Shares, rain_behind, stack
from datetime import date

from tests.fakes import TZ

TS = [1_780_000_000 + i * 1800 for i in range(48)]


def line(label="A", n=48, **kw):
    return Line(label, TS[:n], [float(i % 5) for i in range(n)], **kw)


def bars(n=4):
    return Bars("Rain", "mm", TS[:n], [1.0] * n, 3600, "hour")


def test_a_malformed_spec_fails_when_it_is_built_not_when_it_is_drawn():
    with pytest.raises(ValueError):
        Line("A", [1, 2, 3], [1.0, 2.0])                                    # x and y differ
    with pytest.raises(ValueError):
        Line("A", [1], [1.0])                                               # one point is no line
    with pytest.raises(ValueError):
        Line("A", [1, 2], [1.0, 2.0], low=[0.0, 1.0])                       # low without high
    with pytest.raises(ValueError):
        Line("A", [1, 2], [1.0, 2.0], records={"middle": (1, 1.0)})
    with pytest.raises(ValueError):
        Panel("empty")
    with pytest.raises(ValueError):
        Panel("both", "%", [line()], shares=Shares("s", TS[:2], 3600, [1.0, 1.0], [0.0, 0.0], [0.0, 0.0]))
    with pytest.raises(ValueError):
        Panel("right needs left", "", right=[line()], bars=bars())
    with pytest.raises(ValueError):
        Chart("no panels", "x", [])
    with pytest.raises(ValueError):
        Chart("compass on a stack", "x", [Panel("a", "", [line()]), Panel("b", "", [line("B")])], Compass([[0] * 3] * 16, False))


def test_rain_goes_behind_the_first_line_and_stays_a_panel_when_there_is_no_line():
    temp, hum = Panel("Temperature", "°C", [line("T")]), Panel("Humidity", "%", [line("H")])
    chart = stack([temp, hum, Panel("Rain", "mm", bars=bars())], date(2026, 9, 1), date(2026, 9, 7))
    assert chart.title == "Temperature and Humidity and Rain" and [p.label for p in chart.panels] == ["Temperature", "Humidity"]
    assert chart.panels[0].bars and not chart.panels[1].bars and chart.subtitle.endswith("per hour")
    rating = Panel("Rating", "%", shares=Shares("Rating", TS[:2], 3600, [1.0, 1.0], [0.0, 0.0], [0.0, 0.0], "hour"))
    assert [p.label for p in rain_behind([rating, Panel("Rain", "mm", bars=bars())])] == ["Rating", "Rain"]
    assert [p.label for p in rain_behind([Panel("Rain", "mm", bars=bars()), Panel("Wind", "km/h", [line("W")])])] == ["Wind"]


def test_rain_goes_behind_humidity_or_pressure_before_the_first_line_and_the_order_is_kept():
    panels = lambda *readings: [Panel(r.capitalize(), "", [line(r)], reading=r) for r in readings] + [Panel("Rain", "mm", bars=bars())]
    got = rain_behind(panels("temperature", "humidity", "wind"))
    assert [p.label for p in got] == ["Temperature", "Humidity", "Wind"] and got[1].bars and not got[0].bars   # order as asked
    got = rain_behind(panels("temperature", "pressure", "humidity"))                                            # humidity outranks pressure
    assert got[2].bars and not got[1].bars
    got = rain_behind(panels("temperature", "pressure"))
    assert got[1].bars
    got = rain_behind(panels("temperature", "wind"))                                                            # neither: the first line
    assert got[0].bars
    assert rain_behind([Panel("Only", "", [line("o")], reading="pm2_5"), Panel("Rain", "mm", bars=bars())])[0].bars


def test_a_period_shows_the_year_on_both_dates_when_it_spans_years():
    from lib.specs import period_text
    assert period_text(date(2026, 9, 30), date(2026, 9, 30)) == "Wed 30 Sep 2026"
    assert period_text(date(2026, 9, 24), date(2026, 9, 30)) == "Thu 24 – Wed 30 Sep 2026"
    assert period_text(date(2026, 8, 27), date(2026, 9, 30)) == "Thu 27 Aug – Wed 30 Sep 2026"
    assert period_text(date(2025, 8, 27), date(2026, 9, 30)) == "Wed 27 Aug 2025 – Wed 30 Sep 2026"


def test_a_reading_that_cannot_be_negative_is_not_drawn_below_zero_and_temperature_may_be():
    import matplotlib
    from lib import charts
    ax = matplotlib.pyplot.figure().add_axes([0, 0, 1, 1])
    charts._pad_limits(ax, 0.0, 8.0, floor=0)
    assert ax.get_ylim()[0] == 0
    charts._pad_limits(ax, 0.0, 8.0)
    assert ax.get_ylim()[0] < 0
    charts._pad_limits(ax, -4.0, 8.0, floor=0)                                    # real negatives keep their room
    assert ax.get_ylim()[0] < -4


def test_every_layout_renders_including_a_second_axis_and_rain_behind_the_line():
    for chart in (Chart("One", "x", [Panel("One", "°C", [line("Outdoor"), line("Indoor")])]),
                  Chart("Two axes", "x", [Panel("Two", "", [line("CO₂")], right=[line("VOC index")])]),   # one panel, another unit
                  Chart("Rain behind", "x", [Panel("Pressure", "hPa", [line("P")], bars=bars())]),
                  Chart("Rain alone", "x", [Panel("Rain", "mm", bars=bars())]),
                  Chart("Records", "x", [Panel("R", "", [line("A", records={"low": (TS[3], -1.0), "high": (TS[9], 9.0)})]),
                                         Panel("S", "", [line("B")])])):
        assert render(chart, TZ)[:4] == b"\x89PNG"


def test_a_taller_chart_for_more_panels():
    from PIL import Image
    import io
    sizes = []
    for n in (1, 2, 3, 4):
        chart = Chart("t", "s", [Panel(f"P{i}", "", [line(f"L{i}")]) for i in range(n)])
        sizes.append(Image.open(io.BytesIO(render(chart, TZ))).size)
    assert sizes[0] == sizes[1] == (1280, 720) and sizes[2][1] > 720 and sizes[3][1] > sizes[2][1]


def test_air_metrics_are_grouped_into_two_panels_with_nox_on_a_right_hand_axis():
    class Air:
        tz = TZ
        _line = AirGradient._line
        _chart = AirGradient._chart

    rows = [{"ts": TS[0] + i * 300, "co2": 500.0 + i % 9, "voc_index": 100.0 + i % 7, "pm1": 2.0, "pm2_5": 5.0 + i % 3, "pm10": 9.0,
             "nox_index": 1.0 + i % 4} for i in range(60)]
    everything = [m for on_left, on_right in AIR_PANELS for m in (*on_left, *on_right)]
    chart = Air()._chart(everything, rows, "period")
    assert [p.label for p in chart.panels] == ["PM1, PM2.5 and PM10", "CO₂ (ppm), VOC index and NOx index"]
    particles, gases = chart.panels
    assert [s.label for s in gases.lines] == ["CO₂", "VOC index"] and [s.label for s in gases.right] == ["NOx index"] and gases.zones is None
    assert [s.label for s in particles.lines] == ["PM1", "PM2.5", "PM10"] and particles.unit == "µg/m³" and particles.zones is None
    assert render(chart, TZ)[:4] == b"\x89PNG"
    only = Air()._chart(["pm10", "co2"], rows, "period")                             # asked-for metrics only, in panel order
    assert [p.label for p in only.panels] == ["PM10", "CO₂"]
    nox = Air()._chart(["nox_index", "pm10"], rows, "period")                        # NOx alone in its panel: the left axis, with its zones
    assert [p.label for p in nox.panels] == ["PM10", "NOx index"] and nox.panels[1].zones == (20.0, 150.0) and not nox.panels[1].right
    single = Air()._chart(["pm2_5"], rows, "period")                                 # one metric: the large chart with its zones
    assert len(single.panels) == 1 and single.title == "PM2.5" and single.panels[0].zones == (9.0, 55.4)
