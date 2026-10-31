"""Render a chart the same way everywhere and compare it with its reference image (tests/charts/<name>.png).

The reference images are rendered with Pillow's own font, not whichever font the machine has (it lacks a few characters, such as
₂ and µ, drawn as boxes: only in these pictures), and shrunk to 40% and
reduced to 64 colours (10 to 35 KB each, so the repository stays small), so the picture does not depend on the machine. A chart passes when no more than TOLERANCE of its pixels differ by more
than PER_CHANNEL (0 to 255) in any colour: that absorbs the odd anti-aliasing difference but not a moved label or a changed colour.
Regenerate the references with scripts/update_charts.py when a change to a chart is intended."""

import io
from pathlib import Path

from PIL import Image, ImageChops

from lib import charts, draw, gauges
from lib.specs import Dashboard
from tests.fakes import TZ

GOLDEN = Path(__file__).parent / "charts"
DIFFS = Path(__file__).parent.parent / "chart-diffs"   # where a failing run leaves its picture and the difference (CI uploads it)
TOLERANCE = 0.001
PER_CHANNEL = 24


SCALE = 0.4
COLOURS = 64


def snapshot(chart) -> Image.Image:
    """The chart as the reference images are made: Pillow's own font, shrunk, 64 colours (a palette image)."""
    fonts = draw.FONTS
    draw.use_fonts(None)
    try:
        png = gauges.render(chart) if isinstance(chart, Dashboard) else charts.render(chart, TZ)
    finally:
        draw.FONTS = fonts
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
    golden = golden.convert("RGB")
    r, g, b = ImageChops.difference(actual.convert("RGB"), golden).split()
    changed = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v > PER_CHANNEL else 0)
    share = changed.histogram()[255] / (golden.width * golden.height)
    shown = Image.blend(golden, Image.new("RGB", golden.size, "#FFFFFF"), 0.75)   # the reference faded, with what changed in red
    shown.paste((230, 30, 30), mask=changed)
    return share, shown


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
