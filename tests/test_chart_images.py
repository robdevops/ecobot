"""Golden-image tests: a few fixed charts are drawn and compared with the reference images in tests/charts (see tests/imagecmp.py).
They catch what the spec tests cannot: a label that moved, a colour that changed, a title that no longer fits. Mark: `golden`."""

import pytest

from tests import chart_samples, imagecmp

pytestmark = pytest.mark.golden


@pytest.mark.parametrize("name", list(chart_samples.SAMPLES))
def test_the_chart_looks_as_approved(name):
    assert imagecmp.check(name, chart_samples.SAMPLES[name]()) is None, imagecmp.check(name, chart_samples.SAMPLES[name]())


def test_every_reference_image_has_a_sample_chart_and_the_other_way_round():
    assert {p.stem for p in imagecmp.GOLDEN.glob("*.png")} == set(chart_samples.SAMPLES)


def test_the_comparison_tolerates_a_stray_pixel_but_not_a_moved_label(tmp_path):
    from PIL import Image, ImageDraw
    base = imagecmp.snapshot(chart_samples.temperature())
    one = base.copy()
    one.putpixel((5, 5), (0, 0, 0))
    assert imagecmp.difference(one, base)[0] <= imagecmp.TOLERANCE
    moved = base.copy()
    ImageDraw.Draw(moved).rectangle((40, 20, 300, 60), fill=(255, 0, 0))
    share, diff = imagecmp.difference(moved, base)
    assert share > imagecmp.TOLERANCE and isinstance(diff, Image.Image)
    assert imagecmp.difference(base.resize((10, 10)), base) == (1.0, None)
