"""What the storage holds: each kind of sensor's readings (its columns), how each is written and told as one, and which of the sources'
sensors are which kind. The schema (schema.py) is generated from it; a reading added here later becomes a column at the next start.

A reading's column is named as its source names it: Ecowitt's field ("temperature", "1_hour"), the AirGradient code's metric ("pm2_5"). So
the code keeps naming a series "outdoor.temperature". A row holds each reading's value and its lowest and highest (`_lo`, `_hi`): Ecowitt's
own lows and highs of its 30-minute and coarser readings, and the bands the bot and Home Assistant add. A day row holds each reading's mean,
lowest and highest, and when those happened.
"""

from typing import NamedTuple


class Column(NamedTuple):
    name: str           # the reading, as its source names it
    decimals: int       # how it is written back out: "12.3", "94"
    mode: str           # how a stretch of readings is told as one: "mean" (over time), "max" (gusts), "last" (a counter, a compass point)
    ranged: bool = True     # it takes a band from finer readings (a compass point and the rain counters do not)
    whole: bool = False     # a whole number is written without its decimals: the cloud's UV is "3", Home Assistant's "1.4"


RAIN_COUNTERS = ("daily", "event", "1_hour", "weekly", "monthly", "yearly")
KINDS: dict[str, tuple[Column, ...]] = {
    "climate": (Column("temperature", 1, "mean"), Column("humidity", 0, "mean"), Column("dew_point", 1, "mean"),
                Column("feels_like", 1, "mean"), Column("app_temp", 1, "mean"), Column("vpd", 3, "mean")),
    "pressure": (Column("relative", 1, "mean"), Column("absolute", 1, "mean")),
    "wind": (Column("wind_speed", 1, "mean"), Column("wind_gust", 1, "max"), Column("wind_direction", 0, "last", False)),
    "rain": (Column("rain_rate", 1, "mean", False), *(Column(name, 1, "last", False) for name in RAIN_COUNTERS)),
    "light": (Column("solar", 1, "mean"), Column("uvi", 1, "mean", whole=True)),
    "air": (Column("pm1", 1, "mean"), Column("pm2_5", 1, "mean"), Column("pm10", 1, "mean"), Column("co2", 0, "mean"),
            Column("voc_index", 0, "mean"), Column("nox_index", 0, "mean"), Column("air_temperature", 1, "mean"),
            Column("air_humidity", 0, "mean")),
}
BY_NAME = {kind: {c.name: c for c in columns} for kind, columns in KINDS.items()}


class Group(NamedTuple):
    """One of Ecowitt's groups as a sensor: its kind, its label, and its own names for readings it shares a column with."""
    kind: str
    label: str
    aliases: tuple[tuple[str, str], ...] = ()   # (the group's name for a reading, the column)


ECOWITT = {
    "outdoor": Group("climate", "Outdoor"),
    "indoor": Group("climate", "Indoor", (("app_tempin", "app_temp"),)),
    "pressure": Group("pressure", "Pressure"),
    "wind": Group("wind", "Wind"),
    "rainfall": Group("rain", "Rain"),
    "rainfall_piezo": Group("rain", "Piezo rain"),
    "solar_and_uvi": Group("light", "Solar and UV"),
}
OTHER = "other"        # the kind of a group the catalogue does not know: all its readings are kept in `extra`
AIR = ("air", "air", "AirGradient")   # the AirGradient as a sensor: (kind, role, label); its device is its location (or "local")
AIR_EXTERNAL = "air"

# Resolutions: the seconds a reading covers. 1hour is the AirGradient's older history; 4hour and 1day are Ecowitt's, cut on UTC hours.
SLOT = 1800
# Where a row's values came from: a write replaces those of a row from a source ranked as high or lower (and fills what one ranked higher lacks)
CLOUD, HOME_ASSISTANT, BOT = 0, 1, 2


def kind_of(group: str) -> str:
    """The kind of sensor an Ecowitt group is (OTHER for one the catalogue does not know)."""
    return ECOWITT[group].kind if group in ECOWITT else OTHER


def label_of(group: str) -> str:
    return ECOWITT[group].label if group in ECOWITT else group.replace("_", " ").capitalize()


def column_of(group: str, field: str) -> tuple[str, str] | None:
    """(column, part) an Ecowitt field is kept in: part "value", "lo" or "hi" ("temperature_low" is the temperature's "lo"); None for a
    field the catalogue has no column for (it is kept in `extra`)."""
    kind = kind_of(group)
    if kind == OTHER:
        return None
    base, part = field, "value"
    for suffix, name in (("_low", "lo"), ("_high", "hi")):
        if field.endswith(suffix):
            base, part = field[: -len(suffix)], name
            break
    base = dict(ECOWITT[group].aliases).get(base, base)
    return (base, part) if base in BY_NAME[kind] else None


def field_of(group: str, column: str) -> str:
    """The group's own name for the reading in this column (indoor's apparent temperature is "app_tempin")."""
    return next((field for field, col in ECOWITT[group].aliases if col == column), column) if group in ECOWITT else column


def text(value: float, decimals: int, whole: bool = False) -> str:
    """A reading as the sources write one ("12.3", "94"): a zero is never written with a minus; whole, a whole number has no decimals."""
    written = f"{value:.{decimals}f}"
    if whole and decimals and written.endswith("." + "0" * decimals):
        written = written[: -decimals - 1]
    return written.lstrip("-") if float(written) == 0 else written


__all__ = ["AIR", "AIR_EXTERNAL", "BOT", "BY_NAME", "CLOUD", "Column", "ECOWITT", "Group", "HOME_ASSISTANT", "KINDS", "OTHER",
           "SLOT", "column_of", "field_of", "kind_of", "label_of", "text"]
