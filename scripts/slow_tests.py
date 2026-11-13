#!/usr/bin/env python3
"""Measure every test (run alone, one process) and write tests/slow_tests.txt: the tests that take SLOW seconds or more, which plain
`pytest` leaves out (pytest.ini: -m "not slow"; tests/conftest.py marks them). Run it when a test gets slower or faster, then commit the file.

    python scripts/slow_tests.py            # about two and a half minutes
"""

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIST = ROOT / "tests" / "slow_tests.txt"
SLOW = 0.5   # seconds (setup, call and teardown, over every parameter of the test)
HEADER = ("# The slow tests (0.5 s or more each, run alone): plain `pytest` leaves them out (pytest.ini: -m \"not slow\"); `pytest -m slow`\n"
          "# runs them alone, `pytest -m \"\"` everything (as CI does). Written by scripts/slow_tests.py from the tests' measured times.\n")


def main() -> int:
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-n0", "-m", "", "--durations=0", "-p", "no:cacheprovider"], cwd=ROOT,
                         capture_output=True, text=True)
    if run.returncode != 0:
        print(run.stdout[-3000:], run.stderr[-3000:], sep="\n")
        print("The tests didn't all pass: tests/slow_tests.txt is left as it was.")
        return 1
    took: dict[str, float] = defaultdict(float)
    for m in re.finditer(r"^([0-9.]+)s (?:setup|call|teardown)\s+(\S+)", run.stdout, re.M):
        took[m.group(2).split("[")[0]] += float(m.group(1))
    slow = sorted(name for name, seconds in took.items() if seconds >= SLOW)
    LIST.write_text(HEADER + "".join(f"{name}\n" for name in slow))
    print(f"{len(slow)} slow tests ({sum(took[n] for n in slow):.0f} of {sum(took.values()):.0f} s) written to {LIST.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
