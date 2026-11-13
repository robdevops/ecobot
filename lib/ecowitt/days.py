"""The station's day fields and how a day's figures are read from storage (never asks Ecowitt), for the service's `days` and `current`
and `summary` (lib/service).

Questions like "the hottest day it also rained" or "how many days over 35" compare readings on the
same day across a long period, which the history tool's summaries can't answer. Each day's figures are
its day row (lib/storage/days.py), worked out from the best readings held for it:
  - 5-minute or 30-minute readings, where they hold the whole day: exact local days (midnight to midnight);
  - otherwise the daily buckets, which run 10am to 10am local time, so those days are approximate
    (a morning shower can land on the day before). They are flagged in the answer.
The exact range grows by itself as the archive keeps more sub-daily data.
"""

import logging
from datetime import date

from ..series import WEATHER
from ..storage.catalogue import column_of
from ..storage.read import Reader

log = logging.getLogger(__name__)

# friendly name -> (group, field, which extreme of the day, unit)
FIELDS = {
    "temp_max": ("outdoor", "temperature", "high", "°C"),
    "temp_min": ("outdoor", "temperature", "low", "°C"),
    "rain": ("rainfall", "daily", "high", "mm"),        # the day's total: the rain counter's highest reading
    "wind_gust": ("wind", "wind_gust", "high", "km/h"),
}
# Every other reading (series), then three that aren't series readings: a day's highest (_max), its mean (_avg: "average humidity
# per month") and, where its low says something (not 0 every night or calm hour), its lowest (_min): a day's peak UV, its driest
# air, its lowest pressure ...
_OTHERS = [("wind_speed" if name == "wind" else name, r.group, r.field, r.unit, r.low)
           for name, r in WEATHER.items() if name not in ("temperature", "rain")]
_OTHERS += [("rain_rate", "rainfall", "rain_rate", "mm/h", False),
            ("indoor_temp", "indoor", "temperature", "°C", True), ("indoor_humidity", "indoor", "humidity", "%", True)]
for _name, _group, _field, _unit, _low in _OTHERS:
    FIELDS[f"{_name}_max"] = (_group, _field, "high", _unit)
    FIELDS[f"{_name}_avg"] = (_group, _field, "mean", _unit)
    if _low:
        FIELDS[f"{_name}_min"] = (_group, _field, "low", _unit)
FIELDS["temp_avg"] = ("outdoor", "temperature", "mean", "°C")
AVERAGED = {n for n in FIELDS if n.endswith(("_max", "_min")) and n not in ("temp_max", "temp_min")}   # no range of their own at 30 minutes or daily
FIELD_UNITS = {n: f[3] for n, f in FIELDS.items()}
ALWAYS_SHOWN = ("temp_max", "temp_min", "rain")   # every listed day shows them; days rank by the first when nothing else is named

PARTS = {"high": "hi", "low": "lo", "mean": "mean"}   # a figure's kind -> where a day row (storage.read.Days) keeps it
COARSER = {"5min": 0, "30min": 1, "1day": 2}


def day_values_on(r: Reader, mac: str, shown: list[str], days) -> tuple[dict[str, dict[date, float]], dict[date, str]]:
    """({reading: {day: its figure}}, {day: where it came from}) for these days, from the day rows: a day is "5min" or "30min" where every
    sensor it is asked of has its figures from those (the coarser of them), else "daily" (Ecowitt's daily records: approximate). One
    query per kind of sensor, on the reader's thread."""
    days = set(days)
    values: dict[str, dict[date, float]] = {n: {} for n in shown}
    if not days:
        return values, {}
    places, by_kind = {}, {}
    for name in shown:
        group, field, kind, _ = FIELDS[name]
        sensor, place = r.sensor(mac, group), column_of(group, field)
        if sensor is not None and place is not None:
            places[name] = (sensor, place[0], PARTS[kind])
            by_kind.setdefault(sensor.kind, ({}, set()))[0][sensor.id] = sensor
            by_kind[sensor.kind][1].add(place[0])
    rows = {}
    for kind, (sensors, columns) in by_kind.items():
        rows.update(r.days(kind, list(sensors), min(days), max(days), sorted(columns)))
    source: dict[date, str] = {}
    for name, (sensor, column, part) in places.items():
        got = rows.get(sensor.id)
        if got is None or column not in got.mean:
            continue
        for d, v, src in zip(got.day, getattr(got, part)[column], got.source):
            if v is not None and d in days:
                values[name][d] = v
                source[d] = max(source.get(d, src), src, key=COARSER.get)
    return values, {d: "daily" if s == "1day" else s for d, s in source.items()}
