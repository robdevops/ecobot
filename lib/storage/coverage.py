"""Which times are held, per sensor and resolution: the stretches fetched (with readings or none, so they are not asked for again), merged.

It is read from the file once, at start, and kept in memory, so "is this held?" is a lookup and never a query. A write changes a copy of
what it touches (Coverage.pending) and the table in the same transaction; the copy replaces what the readers see once the transaction is
committed, so a reader never sees coverage for readings that were rolled back.
"""

import threading

type Interval = tuple[int, int]


def merge(intervals: list[Interval]) -> list[Interval]:
    """Inclusive intervals merged: those that overlap or touch become one, oldest first."""
    out: list[list[int]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def subtract(span: Interval, covered: list[Interval]) -> list[Interval]:
    """The parts of inclusive [start, end] inside none of the covered intervals."""
    start, end = span
    gaps, cursor = [], start
    for c_start, c_end in sorted(covered):
        if c_end < cursor:
            continue
        if c_start > end:
            break
        if c_start > cursor:
            gaps.append((cursor, c_start - 1))
        cursor = max(cursor, c_end + 1)
        if cursor > end:
            break
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


class Coverage:
    """{(sensor, res): merged intervals}, shared by the reader and the writer. A list is replaced, never changed in place, so a reader
    holding one is safe."""

    def __init__(self, rows: list[tuple[int, str, int, int]]):
        found: dict[tuple[int, str], list[Interval]] = {}
        for sensor, res, start, end in rows:
            found.setdefault((sensor, res), []).append((start, end))
        self._held = {key: merge(spans) for key, spans in found.items()}
        self._lock = threading.Lock()

    def of(self, sensor: int, res: str) -> list[Interval]:
        with self._lock:
            return self._held.get((sensor, res), [])

    def gaps(self, sensor: int, res: str, start: int, end: int) -> list[Interval]:
        return subtract((start, end), self.of(sensor, res))

    def held(self, sensor: int, res: str, start: int, end: int) -> bool:
        return not self.gaps(sensor, res, start, end)

    def days(self, sensor: int, res: str) -> int:
        """Whole days held at this resolution."""
        return sum(e - s + 1 for s, e in self.of(sensor, res)) // 86400

    def publish(self, changed: dict[tuple[int, str], list[Interval]]):
        """A committed write's coverage, for the readers."""
        with self._lock:
            self._held.update(changed)


__all__ = ["Coverage", "Interval", "merge", "subtract"]
