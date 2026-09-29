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

# US EPA 2024 PM2.5 breakpoints: (conc low, conc high, AQI low, AQI high, band)
PM25_AQI = [
    (0.0, 9.0, 0, 50, "good"),
    (9.1, 35.4, 51, 100, "moderate"),
    (35.5, 55.4, 101, 150, "unhealthy for sensitive groups"),
    (55.5, 125.4, 151, 200, "unhealthy"),
    (125.5, 225.4, 201, 300, "very unhealthy"),
    (225.5, 325.4, 301, 500, "hazardous"),
]


def rating(name: str, value: float) -> str | None:
    limits = RATINGS.get(name)
    if limits is None:
        return None
    good, poor = limits
    return "\U0001f7e2 good" if value <= good else "\U0001f7e1 poor" if value <= poor else "\U0001f534 very poor"


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
        if isinstance(v, (int, float)):
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
