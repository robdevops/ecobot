"""What AirGradient measures, how it is rated, and how a raw reading is normalised."""

from datetime import datetime

from ..series import AIR, AIR_EXTRA

# name -> (fields to try, in order: corrected first), unit
METRICS = {
    "pm2_5": (("pm02_corrected", "pm02"), "µg/m³"),
    "pm10": (("pm10_corrected", "pm10"), "µg/m³"),
    "pm1": (("pm01_corrected", "pm01"), "µg/m³"),
    "co2": (("rco2_corrected", "rco2"), "ppm"),
    "voc_index": (("tvocIndex", "tvoc_index"), "relative index (100 = this sensor's recent average)"),
    "nox_index": (("noxIndex", "nox_index"), "relative index (1 = baseline)"),
}

# The sensor's own temperature and humidity (the corrected values first): kept with every reading, shown only when asked for
EXTRA = {
    "air_temperature": (("atmp_corrected", "atmp"), "°C"),
    "air_humidity": (("rhum_corrected", "rhum"), "%"),
}
STORED = {**METRICS, **EXTRA}   # every column of a stored reading
EXTRA_LABELS = {name: m.label for name, m in AIR_EXTRA.items()}
EXTRA_UNITS = {name: m.unit for name, m in AIR_EXTRA.items()}

# Traffic-light ratings (series.AIR): value <= first -> good, <= second -> poor, above -> very poor.
RATINGS = {name: m.limits for name, m in AIR.items()}
LABELS = {name: m.label for name, m in AIR.items()}
CHART_UNITS = {name: m.unit for name, m in AIR.items()}
ALL_METRICS = list(AIR)
MARK_LOW = tuple(name for name, m in AIR.items() if m.low)   # the metrics whose lowest is marked on a chart
# Metrics charted together, one panel each on a shared time axis, top to bottom: the particles on one axis, then CO2, VOC and
# NOx each on its own (their scales differ too much to share an axis honestly). A panel with none of the requested metrics
# is left out.
AIR_PANELS = (("pm1", "pm2_5", "pm10"), ("co2",), ("voc_index",), ("nox_index",))

PARTICLES = AIR_PANELS[0]
SPIKE_RATIO = 2.5    # a particle reading that is this many times the readings either side of it ...
SPIKE_RISE = 5.0     # ... and this far (µg/m³) above both, with nothing like it next to it, is a glitch of the sensor, not the air
SPIKE_NEIGHBOUR = 900   # the readings either side must be within this many seconds
REQUIRE_CONSECUTIVE = 2   # ANTISPIKE_REQUIRE_CONSECUTIVE: readings in a row a rise needs to be the air (set at startup); 1 turns the rule off


def without_spikes(rows: list[dict], require: int | None = None) -> list[dict]:
    """The readings with each spike of the particles taken out, for drawing and for the figures: a stretch of fewer than `require`
    readings in a row (REQUIRE_CONSECUTIVE) of PM1, PM2.5 or PM10 that are each at least SPIKE_RATIO times, and SPIKE_RISE above, the readings
    either side of the stretch. All the particle readings of those moments go (they spike together). A rise that lasts `require`
    readings or more is the air, and stays; so do the first and last readings (they have one side). require 1: nothing is taken out."""
    require = REQUIRE_CONSECUTIVE if require is None else require
    drop: set[int] = set()
    for name in PARTICLES:
        have = [(i, r["ts"], r[name]) for i, r in enumerate(rows) if name in r]
        for a in range(1, len(have) - 1):
            for length in range(1, require):
                b = a + length - 1                                   # the stretch is have[a..b]
                if b + 1 >= len(have):
                    break
                (_, t0, before), (_, t1, after) = have[a - 1], have[b + 1]
                near = max(before, after)
                if have[a][1] - t0 <= SPIKE_NEIGHBOUR and t1 - have[b][1] <= SPIKE_NEIGHBOUR and all(
                        v >= SPIKE_RATIO * near and v - near >= SPIKE_RISE for _, _, v in have[a:b + 1]):
                    drop.update(i for i, _, _ in have[a:b + 1])
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
    for field in STORED[name][0]:
        v = row.get(field)
        if isinstance(v, int | float):
            return float(v)
    return None


def epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def normalise(raw: dict) -> dict:
    """{"ts": epoch, "pm2_5": ..., ...} with only the metrics the reading has."""
    row = {"ts": epoch(raw["timestamp"])}
    for name in STORED:
        if (v := value_of(raw, name)) is not None:
            row[name] = v
    return row
