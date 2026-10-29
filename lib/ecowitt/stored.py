"""TEMPORARY, while step 2 of docs/storage-plan.md moves the station's readers to the new storage one at a time: what they asked of the old
history cache (HistoryCache's reads, in its shapes: Ecowitt's {group: {field: {"unit", "list": {epoch: text}}}} and {epoch: float}), and of
the minutes, answered from the new storage. A reading is written back as the station writes it (the
catalogue's decimals); a value that is not a number (kept in `extra`) is not read, as no answer could use one. A band is read where its
reading is. What is held (the coverage) is in memory; a read of readings waits for the reader's thread (Storage.read_now), as the readers
called the cache from threads of their own.
"""

from bisect import bisect_left

from ..storage import Storage
from ..storage.catalogue import AIR, BY_NAME, KINDS, SLOT, column_of, field_of, text
from ..storage.read import Reader
from ..storage.schema import some_value

SLACK_SECONDS = 300   # the first minute held may be this much after a chart's start (minutes.SLACK_SECONDS)
PARTS = (("", "values"), ("_low", "lo"), ("_high", "hi"))   # a field's own name and its low and high, and where Rows keeps them


def fields_on(r: Reader, mac: str, cycle: str, group: str, fields: list[str] | None, start: int, end: int) -> dict:
    """A group's fields (all of them when None) between two epochs, as the cache gave them: {field: {"unit", "list": {str(epoch): text}}}."""
    sensor = r.sensor(mac, group)
    if sensor is None or sensor.kind not in KINDS:
        return {}
    got = r.rows(sensor.kind, [sensor.id], cycle, start, end).get(sensor.id)
    if got is None:
        return {}
    units = r.fields(sensor.id, cycle)
    out = {}
    for column, values in got.values.items():
        reading, name = BY_NAME[sensor.kind][column], field_of(group, column)
        for suffix, part in PARTS:
            field = name + suffix
            if fields is not None and field not in fields:
                continue
            series = getattr(got, part)[column]
            found = {str(t): text(v, reading.decimals, reading.whole) for t, v, own in zip(got.ts, series, values)
                     if v is not None and own is not None}
            if found:
                out[field] = {"unit": units.get(field, units.get(name, "")), "list": found}
    return out


def load_on(r: Reader, mac: str, cycle: str, groups: list[str], start: int, end: int) -> dict:
    """HistoryCache.load: {group: {field: {"unit", "list"}}} of the groups that have readings."""
    got = {group: fields_on(r, mac, cycle, group, None, start, end) for group in groups}
    return {group: fields for group, fields in got.items() if fields}


def slots_on(r: Reader, mac: str, cycle: str, group: str, fields: list[str], start: int, end: int) -> list[dict[int, float]]:
    """HistoryCache.slots: each field as {epoch: float}, in the order asked (empty for a field there is none of). Final readings only, as
    the cache held them (what is newer than its cycle's settle horizon was the fetch's own, in memory)."""
    sensor = r.sensor(mac, group)
    if sensor is None or sensor.kind not in KINDS:
        return [{} for _ in fields]
    places = [column_of(group, f) for f in fields]
    columns = list(dict.fromkeys(p[0] for p in places if p))
    got = r.rows(sensor.kind, [sensor.id], cycle, start, end, columns).get(sensor.id) if columns else None
    out = []
    for place in places:
        if got is None or place is None or place[0] not in got.values:
            out.append({})
            continue
        column, part = place
        series, own = getattr(got, "values" if part == "value" else part)[column], got.values[column]
        out.append({t: v for t, v, o, final in zip(got.ts, series, own, got.final) if v is not None and o is not None and final})
    return out


def minutes_on(r: Reader, mac: str, groups: list[str], start: int, end: int) -> dict:
    """MinuteStore.get: the minutes of these groups in [start, end]; {} when there are none or they do not reach back to the start."""
    got = {group: fields_on(r, mac, "1min", group, None, start, end) for group in groups}
    got = {group: fields for group, fields in got.items() if fields}
    first = min((int(t) for fields in got.values() for obj in fields.values() for t in obj["list"]), default=None)
    return {} if first is None or first > start + SLACK_SECONDS else got


def newest_minute_on(r: Reader, mac: str) -> int | None:
    """MinuteStore.newest: the time of the newest minute held of the station."""
    found = [r.db.execute(f"SELECT MAX(ts) FROM {s.kind} WHERE sensor = ? AND res = '1min'", (s.id,)).fetchone()[0]
             for s in r.sensors(device=mac) if s.kind in KINDS]
    return max((t for t in found if t is not None), default=None)


class StoredHistory:
    """HistoryCache's reads, from the new storage."""

    def __init__(self, storage: Storage):
        self.storage = storage
        self.path = storage.path

    def _sensor(self, mac: str, group: str):
        return self.storage.sensors.find(mac, group)

    def macs(self) -> list[str]:
        """The stations the file holds readings of."""
        return sorted({s.device for s in self.storage.sensors.all() if s.kind != AIR[0]})

    def coverage(self, mac: str, cycle: str, grp: str) -> list[tuple[int, int]]:
        sensor = self._sensor(mac, grp)
        return list(self.storage.coverage.of(sensor.id, cycle)) if sensor else []

    def uncovered(self, mac: str, groups: list[str], windows: list[tuple[str, int, int]]) -> list[dict[str, list[tuple[int, int]]]]:
        out = []
        for cycle, start, end in windows:
            sensors = {g: self._sensor(mac, g) for g in groups}
            out.append({g: self.storage.coverage.gaps(s.id, cycle, start, end) if s else [(start, end)] for g, s in sensors.items()})
        return out

    def days_held(self, mac: str, cycle: str, groups: list[str]) -> int:
        return min((self.storage.coverage.days(s.id, cycle) if (s := self._sensor(mac, g)) else 0 for g in groups), default=0)

    def count(self, mac: str, cycle: str, grp: str, start: int, end: int) -> int:
        sensor = self._sensor(mac, grp)
        if sensor is None or sensor.kind not in KINDS:
            return 0
        names = [c.name for c in KINDS[sensor.kind]]
        return self.storage.read_now(lambda r: r.db.execute(f"SELECT COUNT(*) FROM {sensor.kind} WHERE sensor = ? AND res = ? AND ts BETWEEN ? AND ? "
                                                            f"AND {some_value(names)}", (sensor.id, cycle, start, end)).fetchone()[0])

    def load(self, mac: str, cycle: str, groups: list[str], start: int, end: int) -> dict:
        return self.storage.read_now(load_on, mac, cycle, groups, start, end)

    def load_fields(self, mac: str, cycle: str, grp: str, fields: list[str] | None, start: int, end: int) -> dict:
        return self.storage.read_now(fields_on, mac, cycle, grp, fields, start, end)

    def slots(self, mac: str, cycle: str, grp: str, fields: list[str], start: int, end: int) -> list[dict[int, float]]:
        return self.storage.read_now(slots_on, mac, cycle, grp, fields, start, end)

    def slot_ranges(self, mac: str, grp: str, field: str, values: dict[int, float], start: int, end: int) -> tuple[dict, dict]:
        """(lows, highs) of the half hours in `values` from the 5-minute readings inside each (two or more), and its own value."""
        (fine,) = self.slots(mac, "5min", grp, [field], start, end)
        times = sorted(fine)
        readings = [fine[t] for t in times]
        lows, highs = {}, {}
        for t, v in values.items():
            inside = readings[bisect_left(times, t):bisect_left(times, t + SLOT)]
            if len(inside) >= 2:
                lows[t], highs[t] = min(inside + [v]), max(inside + [v])
        return lows, highs


__all__ = ["StoredHistory", "load_on", "minutes_on", "newest_minute_on"]
