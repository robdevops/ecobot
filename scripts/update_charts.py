#!/usr/bin/env python
"""Regenerate the reference images the chart tests compare with (tests/charts/<name>.png).

Run it after an intended change to how charts look, then look at the changed images (GitHub shows them side by side in the pull
request) before committing:

    python scripts/update_charts.py            # every chart
    python scripts/update_charts.py wind air   # just these

The images are drawn from the made-up data in tests/chart_samples.py with matplotlib's own font, so the result is the same on any
machine with the same matplotlib, numpy and Pillow versions (pinned in requirements.txt)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from tests import chart_samples, imagecmp  # noqa: E402


def main(names: list[str]):
    unknown = [n for n in names if n not in chart_samples.SAMPLES]
    if unknown:
        sys.exit(f"unknown chart(s): {', '.join(unknown)}; the charts are {', '.join(chart_samples.SAMPLES)}")
    imagecmp.GOLDEN.mkdir(exist_ok=True)
    for name in names or chart_samples.SAMPLES:
        path = imagecmp.GOLDEN / f"{name}.png"
        image = imagecmp.snapshot(chart_samples.SAMPLES[name]())
        share = imagecmp.difference(image, Image.open(path))[0] if path.exists() else None
        imagecmp.save(image, path)
        what = "new" if share is None else "unchanged" if share == 0 else f"{share:.2%} of the pixels changed"
        print(f"{path.relative_to(imagecmp.GOLDEN.parent.parent)}: {path.stat().st_size / 1024:.0f} KB, {what}")


if __name__ == "__main__":
    main(sys.argv[1:])
