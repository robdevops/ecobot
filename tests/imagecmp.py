"""Render a chart the same way everywhere and compare it with its reference image (tests/charts/<name>.png).

The reference images are rendered with matplotlib's own font (DejaVu Sans), not whichever font the machine has, and shrunk to 40% and
reduced to 64 colours (10 to 35 KB each, so the repository stays small), so the picture does not depend on the machine. A chart passes when no more than TOLERANCE of its pixels differ by more
than PER_CHANNEL (0 to 255) in any colour: that absorbs the odd anti-aliasing difference but not a moved label or a changed colour.
Regenerate the references with scripts/update_charts.py when a change to a chart is intended."""

import io
from pathlib import Path
from unittest import mock

import matplotlib
import numpy as np
from PIL import Image

from lib import charts
from tests.fakes import TZ

GOLDEN = Path(__file__).parent / "charts"
DIFFS = Path(__file__).parent.parent / "chart-diffs"   # where a failing run leaves its picture and the difference (CI uploads it)
TOLERANCE = 0.001
PER_CHANNEL = 24


SCALE = 0.4
COLOURS = 64


def snapshot(chart) -> Image.Image:
    """The chart as the reference images are made: DejaVu Sans, shrunk, 64 colours (a palette image)."""
    with matplotlib.rc_context({"font.family": "DejaVu Sans"}), mock.patch.object(charts, "TITLE_WEIGHT", "bold"):
        png = charts.render(chart, TZ)
    image = Image.open(io.BytesIO(png)).convert("RGB")
    small = image.resize((round(image.width * SCALE), round(image.height * SCALE)), Image.Resampling.LANCZOS)
    return small.quantize(colors=COLOURS, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def save(image: Image.Image, path: Path):
    """Written the same way every time (no timestamps or version text), so an unchanged picture is an unchanged file."""
    image.save(path, format="PNG", optimize=True)


def difference(actual: Image.Image, golden: Image.Image) -> tuple[float, Image.Image | None]:
    """(the share of pixels that differ by more than PER_CHANNEL, a picture of where). A different size is a total difference."""
    if actual.size != golden.size:
        return 1.0, None
    a, g = np.asarray(actual.convert("RGB"), dtype=int), np.asarray(golden.convert("RGB"), dtype=int)
    changed = np.abs(a - g).max(axis=2) > PER_CHANNEL
    shown = (g * 0.25 + 255 * 0.75).astype(np.uint8)   # the reference faded, with what changed in red
    shown[changed] = (230, 30, 30)
    return float(changed.mean()), Image.fromarray(shown)


def check(name: str, chart) -> str | None:
    """None when the chart matches its reference image; else what is wrong (and the picture and the difference are saved in DIFFS)."""
    actual, path = snapshot(chart), GOLDEN / f"{name}.png"
    if not path.exists():
        return f"no reference image for {name}: run python scripts/update_charts.py"
    share, diff = difference(actual, Image.open(path))
    if share <= TOLERANCE:
        return None
    DIFFS.mkdir(exist_ok=True)
    save(actual, DIFFS / f"{name}-now.png")
    if diff:
        save(diff, DIFFS / f"{name}-difference.png")
    return (f"{name}: {share:.2%} of the pixels differ (the limit is {TOLERANCE:.2%}); see chart-diffs/{name}-now.png and "
            f"chart-diffs/{name}-difference.png. If the change is intended, run python scripts/update_charts.py")
