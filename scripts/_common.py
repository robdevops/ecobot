"""Shared set-up for the scripts: make `lib` importable and build the argument parser."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def parser(doc: str | None) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
