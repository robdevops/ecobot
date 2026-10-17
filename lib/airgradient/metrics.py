"""What AirGradient measures, how it is rated, and how a raw reading is normalised."""

from datetime import datetime

# name -> (fields to try, in order: corrected first), unit
METRICS = {
    "pm2_5": (("pm02_corrected", "pm02"), "µg/m³"),
    "pm10": (("pm10_corrected", "pm10"), "µg/m³"),
    "pm1": (("pm01_corrected", "pm01"), "µg/m³"),
    "co2": (("rco2_corrected", "rco2"), "ppm"),
    "voc_index": (("tvocIndex", "tvoc_index"), "relative index (100 = this sensor's recent average)"),
    "nox_index": (("noxIndex", "nox_index"), "relative index (1 = baseline)"),
}

# Traffic-light ratings: value <= first -> good, <= second -> poor, above -> very poor.
# Particles follow the US AQI (very poor = AQI 151+, the same level as the mask alerts);
# PM1 has no standard, so it uses PM2.5's; CO2, VOC and NOx follow AirGradient's colour scales.
RATINGS = {
    "pm2_5": (9.0, 55.4),
    "pm10": (54.0, 254.0),
    "pm1": (9.0, 55.4),
    "co2": (799.0, 1499.0),
    "voc_index": (150.0, 250.0),
    "nox_index": (20.0, 150.0),
}

LABELS = {"pm2_5": "PM2.5", "pm10": "PM10", "pm1": "PM1", "co2": "CO₂",
          "voc_index": "VOC index", "nox_index": "NOx index"}
CHART_UNITS = {"pm2_5": "µg/m³", "pm10": "µg/m³", "pm1": "µg/m³", "co2": "ppm",
               "voc_index": "", "nox_index": ""}
ALL_METRICS = list(LABELS)
# Only these have a lowest reading worth marking on a chart: CO2 never falls to zero (it has an outdoor floor near 400 ppm),
# the others are often 0 or at their baseline, so their lowest says nothing.
MARK_LOW = ("co2",)
# Metrics charted together, one panel each on a shared time axis, top to bottom: the particles on one axis, then CO2, VOC and
# NOx each on its own (their scales differ too much to share an axis honestly). A panel with none of the requested metrics
# is left out.
AIR_PANELS = (("pm1", "pm2_5", "pm10"), ("co2",), ("voc_index",), ("nox_index",))

PARTICLES = AIR_PANELS[0]
SPIKE_RATIO = 2.5    # a particle reading that is this many times the readings either side of it ...
SPIKE_RISE = 5.0     # ... and this far (µg/m³) above both, with nothing like it next to it, is a glitch of the sensor, not the air
SPIKE_NEIGHBOUR = 900   # the readings either side must be within this many seconds


def without_spikes(rows: list[dict]) -> list[dict]:
    """The readings with each single-reading spike of the particles taken out, for drawing: a reading of PM1, PM2.5 or PM10 that is at
    least SPIKE_RATIO times, and SPIKE_RISE above, both the readings next to it. All the particle readings of that moment go (they spike
    together). A rise that lasts two readings or more is the air, and stays. The first and last readings are kept: they have one side."""
    drop: set[int] = set()
    for name in PARTICLES:
        have = [(i, r["ts"], r[name]) for i, r in enumerate(rows) if name in r]
        for (_, t0, before), (i, t, v), (_, t1, after) in zip(have, have[1:], have[2:]):
            near = max(before, after)
            if v >= SPIKE_RATIO * near and v - near >= SPIKE_RISE and t - t0 <= SPIKE_NEIGHBOUR and t1 - t <= SPIKE_NEIGHBOUR:
                drop.add(i)
    return [{k: v for k, v in r.items() if k not in PARTICLES} if i in drop else r for i, r in enumerate(rows)]


# US EPA 2024 PM2.5 breakpoints: (conc low, conc high, AQI low, AQI high, band)
PM25_AQI = [
    (0.0, 9.0, 0, 50, "good"),
    (9.1, 35.4, 51, 100, "moderate"),
    (35.5, 55.4, 101, 150, "unhealthy for sensitive groups"),
    (55.5, 125.4, 151, 200, "unhealthy"),
    (125.5, 225.4, 201, 300, "very unhealthy"),
    (225.5, 325.4, 301, 500, "hazardous"),
]


ZONES = ("good", "poor", "very poor")
_ZONE_EMOJI = ("\U0001f7e2", "\U0001f7e1", "\U0001f534")


def zone(value: float, limits: tuple[float, float]) -> int:
    """0 good, 1 poor, 2 very poor."""
    return 0 if value <= limits[0] else 1 if value <= limits[1] else 2


def rating(name: str, value: float) -> str | None:
    limits = RATINGS.get(name)
    if limits is None:
        return None
    z = zone(value, limits)
    return f"{_ZONE_EMOJI[z]} {ZONES[z]}"


def pm25_aqi(conc: float) -> tuple[int, str]:
    c = round(conc, 1)
    for lo, hi, a_lo, a_hi, band in PM25_AQI:
        if c <= hi:
            return round(a_lo + (a_hi - a_lo) * (max(c, lo) - lo) / (hi - lo)), band
    return 500, "hazardous"


def value_of(row: dict, name: str) -> float | None:
    """One metric from a raw reading (corrected value first), or None."""
    for field in METRICS[name][0]:
        v = row.get(field)
        if isinstance(v, int | float):
            return float(v)
    return None


def epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def normalise(raw: dict) -> dict:
    """{"ts": epoch, "pm2_5": ..., ...} with only the metrics the reading has."""
    row = {"ts": epoch(raw["timestamp"])}
    for name in METRICS:
        if (v := value_of(raw, name)) is not None:
            row[name] = v
    return row
