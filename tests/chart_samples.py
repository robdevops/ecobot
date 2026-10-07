"""Fixed sample charts for the image tests (tests/test_chart_images.py) and the script that regenerates their reference images
(scripts/update_charts.py). They are built through the same helpers production uses (panel_for, stack) from made-up data that never
reads the clock, so a given chart always has the same pixels for the same library versions."""

import math
import random
from datetime import date, datetime

import lib.airgradient  # noqa: F401  (imported first: lib.panels and the air package import each other)
from lib.panels import panel_for
from lib.specs import Bars, Chart, Compass, Line, Panel, stack
from tests.fakes import TZ

WEEK = (date(2026, 9, 28), date(2026, 10, 4))
T0 = int(datetime(2026, 9, 28, tzinfo=TZ).timestamp())
HALF_HOURS = [T0 + i * 1800 for i in range(7 * 48)]
DAYS = [int(datetime(2026, 7, 7, 12, tzinfo=TZ).timestamp()) + i * 86400 for i in range(90)]


def wobble(seed: int, n: int, size: float = 0.4) -> list[float]:
    rng = random.Random(seed)
    return [rng.uniform(-size, size) for _ in range(n)]


def daily(i: int, peak: float = 0.0) -> float:
    """A smooth day-night cycle in half-hour steps, with a slower swing across the week."""
    return math.sin((i - 14) / 7.64) + 0.25 * math.sin(i / 40) + peak


def line(label: str, ys: list[float], reading: str, xs: list[int] = HALF_HOURS, indoor: bool = False, **band) -> Line:
    return Line(label, xs, ys, reading=reading, indoor=indoor, **band)


def temperature() -> Chart:
    n, noise = len(HALF_HOURS), wobble(1, len(HALF_HOURS))
    outdoor = line("Outdoor", [15 + 8 * daily(i) + noise[i] for i in range(n)], "temperature")
    indoor = line("Indoor", [18 + 1.5 * math.sin(i / 25) for i in range(n)], "temperature", indoor=True)
    return Chart("Temperature", "Mon 28 Sep – Sun 4 Oct 2026  ·  30-minute readings", [panel_for("temperature", [outdoor, indoor])])


def rain_bars(n: int = 28, width: int = 6 * 3600) -> Bars:
    return Bars("Rain", "mm", [T0 + i * width for i in range(n)], [round(max(0, 6 * math.sin(i / 3.5) - 2), 1) for i in range(n)], width, "6 hours")


def humidity_rain() -> Chart:
    n, noise = len(HALF_HOURS), wobble(2, len(HALF_HOURS), 1.5)
    outdoor = line("Outdoor", [min(100, 78 - 14 * daily(i) + noise[i]) for i in range(n)], "humidity")
    indoor = line("Indoor", [52 + 4 * math.sin(i / 30) for i in range(n)], "humidity", indoor=True)
    return stack([panel_for("humidity", [outdoor, indoor]), panel_for("rain", bars=rain_bars())], *WEEK)


def all_week() -> Chart:
    n = len(HALF_HOURS)
    series = {
        "temperature": [line("Outdoor", [15 + 8 * daily(i) + w for i, w in enumerate(wobble(1, n))], "temperature"),
                        line("Indoor", [18 + 1.5 * math.sin(i / 25) for i in range(n)], "temperature", indoor=True)],
        "humidity": [line("Outdoor", [min(100, 78 - 14 * daily(i) + w) for i, w in enumerate(wobble(2, n, 1.5))], "humidity")],
        "solar": [line("Solar", [max(0.0, 700 * daily(i, -0.1) + w * 20) for i, w in enumerate(wobble(3, n))], "solar")],
        "pressure": [line("Pressure", [1018 + 8 * math.sin(i / 90) + w for i, w in enumerate(wobble(4, n, 0.2))], "pressure")],
        "dew_point": [line("Outdoor", [9 + 3 * math.sin(i / 55) + w for i, w in enumerate(wobble(5, n))], "dew_point")],
        "wind": [line("Wind", [max(0.0, 7 + 4 * math.sin(i / 11) + 6 * w) for i, w in enumerate(wobble(6, n))], "wind")],
    }
    panels = [panel_for(name, lines) for name, lines in series.items()]
    panels.insert(4, panel_for("rain", bars=rain_bars()))
    return stack(panels, *WEEK)


def quarter() -> Chart:
    rng = random.Random(7)
    mean = [14 + 7 * math.sin(i / 14) + rng.uniform(-1.5, 1.5) for i in range(90)]
    band = {"low": [m - rng.uniform(3, 6) for m in mean], "high": [m + rng.uniform(3, 6) for m in mean]}
    return Chart("Temperature", "Tue 7 Jul – Sun 4 Oct 2026  ·  daily averages, range shaded", [panel_for("temperature", [line("Outdoor", mean, "temperature", DAYS, **band)])])


def air() -> Chart:
    n, rng = 7 * 24, random.Random(9)
    xs = [T0 + i * 3600 for i in range(n)]
    pm25 = [max(0.5, 6 + 5 * math.sin(i / 9) + rng.uniform(-1, 1) + (30 if 90 < i < 100 else 0)) for i in range(n)]
    mk = lambda scale, off, name: line(name, [max(0.0, s * scale + off) for s in pm25], name, xs)
    panels = [panel_for(["pm1", "pm2_5", "pm10"], [mk(0.6, 0, "pm1"), mk(1, 0, "pm2_5"), mk(1.4, 2, "pm10")]),
              panel_for("co2", [line("CO₂", [520 + 80 * math.sin(i / 7) + 30 * rng.random() for i in range(n)], "co2", xs)]),
              panel_for("voc_index", [line("VOC", [110 + 25 * math.sin(i / 11) + 10 * rng.random() for i in range(n)], "voc_index", xs)]),
              panel_for("nox_index", [line("NOx", [1 + 6 * max(0, math.sin(i / 20)) for i in range(n)], "nox_index", xs)])]
    return stack(panels, *WEEK)


def wind() -> Chart:
    n, noise = len(HALF_HOURS), wobble(11, len(HALF_HOURS))
    mean = [max(0.0, 8 + 5 * math.sin(i / 13) + 3 * noise[i]) for i in range(n)]
    gusts = [m + 6 + 6 * abs(noise[i]) for i, m in enumerate(mean)]
    rose = [[max(0.0, 4 + 3 * math.cos(math.radians(22.5 * k - 200)) + s) for s in (0, 1.5, 0.6)] for k in range(16)]
    chart = Chart("Wind", "Mon 28 Sep – Sun 4 Oct 2026  ·  30-minute readings, shaded up to the gusts",
                  [panel_for("wind", [line("Wind", mean, "wind", low=mean, high=gusts)])], compass=Compass(rose, True))
    return chart


def counts() -> Chart:
    """Days over a limit, by month: the bar chart with a number above each bar."""
    starts = [int(datetime(2026, m, 1, tzinfo=TZ).timestamp()) for m in range(1, 10)]
    bars = Bars("Days", "days", starts, [0, 0, 1, 3, 8, 14, 21, 17, 6], 28 * 86400, "month", values=True)
    return Chart("Days with UV index ≥ 10", "Thu 1 Jan – Wed 30 Sep 2026  ·  70 of 273 days  ·  per month", [Panel("Days", "", bars=bars)])


SAMPLES = {"temperature": temperature, "humidity_rain": humidity_rain, "all_week": all_week, "quarter": quarter, "air": air,
           "wind": wind, "counts": counts}
