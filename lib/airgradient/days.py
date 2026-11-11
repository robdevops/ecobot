"""The air-quality sensor's day fields, for the service's `days` (lib/service/days.py, which reads the day rows itself: storage.days works
each day out as its readings are written, the sensor's glitches taken out): a day's highest (_max), lowest (_min) or mean (_avg) of each
metric, e.g. pm2_5_max > 25 for the days particles passed 25 µg/m³."""

from .metrics import ALL_METRICS, CHART_UNITS

SUFFIX_KIND = ("max", "min", "avg")
FIELDS = {f"{m}_{kind}": CHART_UNITS[m] for m in ALL_METRICS for kind in SUFFIX_KIND}
ALWAYS_SHOWN = ("pm2_5_max",)   # every listed day shows it; days rank by it when nothing else is named
