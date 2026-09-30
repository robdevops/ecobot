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


def test_air_metrics_are_grouped_into_three_panels_with_a_second_axis_for_the_other_unit():
    class Air:
        tz = TZ
        _line = AirGradient._line
        _chart = AirGradient._chart

    rows = [{"ts": TS[0] + i * 300, "co2": 500.0 + i % 9, "voc_index": 100.0 + i % 7, "pm1": 2.0, "pm2_5": 5.0 + i % 3, "pm10": 9.0,
             "nox_index": 1.0 + i % 4} for i in range(60)]
    everything = [m for group in AIR_PANELS for m in group]
    chart = Air()._chart(everything, rows, "period")
    assert [p.label for p in chart.panels] == ["CO₂ (ppm) and VOC index", "PM1, PM2.5 and PM10", "NOx index"]
    co2, particles, nox = chart.panels
    assert [s.label for s in co2.lines] == ["CO₂"] and [s.label for s in co2.right] == ["VOC index"] and co2.zones is None
    assert [s.label for s in particles.lines] == ["PM1", "PM2.5", "PM10"] and particles.unit == "µg/m³" and particles.zones is None
    assert nox.zones == (20.0, 150.0)                                                # a single reading keeps its rating zones
    assert render(chart, TZ)[:4] == b"\x89PNG"
    only = Air()._chart(["pm10", "co2"], rows, "period")                             # asked-for metrics only, in panel order
    assert [p.label for p in only.panels] == ["CO₂", "PM10"]
    single = Air()._chart(["pm2_5"], rows, "period")                                 # one metric: the large chart with its zones
    assert len(single.panels) == 1 and single.title == "PM2.5" and single.panels[0].zones == (9.0, 55.4)
