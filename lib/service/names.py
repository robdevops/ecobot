"""The readings the service has, one name each, and the words people use for them (docs/mcp-service.md 3.2, "One naming scheme").

A reading's name is `sensor.reading`: the weather station's Ecowitt group and the reading's column (`outdoor.temperature`,
`indoor.humidity`, `pressure.relative`, `wind.wind_gust`), with rain as `rainfall.rain`, the mm that fell (the file keeps the running
daily total), and the air sensor's as `air.pm2_5` … `air.nox_index`, its own temperature and humidity `air.temperature` and
`air.humidity`. The catalogue holds the readings the file holds (lib/history.py's station_metrics and air_metrics, from the fields the
station has sent): `Catalogue.open(eco, air)`, or `CATALOGUE`, every reading the service knows, before any file is read (the evals).

`resolve` reads the names a tool is sent, the full names or the words people use:
  - a bare reading is the weather station's: `temperature` and `humidity` are outdoor and indoor, never the air sensor's (that is only
    ever `air.temperature`, `air.humidity`, or the AirGradient named: "airgradient humidity"); `dew point`, `feels like` and `vpd`
    are outdoor's; `outdoor humidity`, `indoor temp` name the side;
  - `pressure` is relative pressure, `rain` the main gauge, `uv`, `solar`, `gust`, `rain rate`, the air sensor's `pm2.5`, `co2` …;
  - `wind` is its speed shaded up to the gusts (the entry's `band`), and its direction, the compass rose on a chart;
  - a sensor's name (`outdoor`, `indoor`, `wind`, `rainfall`, `air` …) is its usual set (SENSOR_SETS); `all` is every weather-station
    reading, in the station's usual chart order (in_all); the devices' nicknames are their readings ("ecowitt": all; "ag": `air`).
A name or word it can't read raises UnknownReading, a Refusal that lists the names and the words with what each stands for.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace

from ..history import AIR_SENSOR, RAIN, Metric, air_metrics, station_fields, station_metrics
from ..series import AIR, AIR_EXTRA, INDOOR_READINGS, WEATHER
from ..storage.catalogue import label_of
from .common import Refusal

STATION, AIR_DEVICE = "station", "air"   # the two devices: the Ecowitt weather station, the AirGradient air sensor

# ---------- "all" ----------
# The settings "all" follows: the panels of "weather all week", top to bottom, as lib/series.py names the readings. intent.chart_fields
# reads them here too. ecobot.py sets the two flags from the config (CHART_ALL_FEELS_LIKE, CHART_ALL_VPD) at startup.
ALL_ORDER = ("temperature", "humidity", "solar", "pressure", "rain", "dew_point", "feels_like", "vpd", "uv", "wind")
LEFT_OUT_OF_ALL = ("uv",)   # the UV index has the same shape as solar radiation
FEELS_LIKE_IN_ALL = False   # the feels-like panel: off, as it nearly repeats temperature
VPD_IN_ALL = False          # the vapour pressure deficit panel: off, as it is temperature and humidity combined


def in_all() -> list[str]:
    """The readings "all" is, as lib/series.py names them, in the station's usual chart order, with the settings applied."""
    return [n for n in ALL_ORDER if n not in LEFT_OUT_OF_ALL and (FEELS_LIKE_IN_ALL or n != "feels_like") and (VPD_IN_ALL or n != "vpd")]


# ---------- the words ----------
# A reading named bare (lib/series.py's names): the weather station's. Temperature and humidity are both sides; the others outdoor's.
BARE = {"temperature": ("outdoor.temperature", "indoor.temperature"), "humidity": ("outdoor.humidity", "indoor.humidity"),
        "pressure": ("pressure.relative",), "wind": ("wind.wind_speed", "wind.wind_direction"), "rain": ("rainfall.rain",),
        "dew_point": ("outdoor.dew_point",), "feels_like": ("outdoor.feels_like",), "vpd": ("outdoor.vpd",),
        "solar": ("solar_and_uvi.solar",), "uv": ("solar_and_uvi.uvi",)}
# A sensor named: its usual set, what the station's gauges show for it (lib/ecowitt/dashboard.py GAUGES), wind with its direction; the
# air sensor's six air-quality readings (its own temperature and humidity are named, never part of a set).
SENSOR_SETS = {"outdoor": ("outdoor.temperature", "outdoor.humidity", "outdoor.dew_point", "outdoor.feels_like"),
               "indoor": ("indoor.temperature", "indoor.humidity"),
               "pressure": ("pressure.relative",),
               "wind": BARE["wind"],
               "rainfall": ("rainfall.rain", "rainfall.rain_rate"),
               "rainfall_piezo": ("rainfall_piezo.rain", "rainfall_piezo.rain_rate"),
               "solar_and_uvi": ("solar_and_uvi.solar", "solar_and_uvi.uvi"),
               AIR_SENSOR: tuple(f"air.{n}" for n in AIR)}
ALL = ("all",)   # (stands for in_all(), read when resolved: the settings can change)
# The words people use, after _key (lower case, "_" and "-" as spaces) -> the names they stand for
ALIASES: dict[str, tuple[str, ...]] = {
    **{_name.replace("_", " "): _names for _name, _names in SENSOR_SETS.items()},
    "outside": SENSOR_SETS["outdoor"], "outdoors": SENSOR_SETS["outdoor"], "inside": SENSOR_SETS["indoor"], "indoors": SENSOR_SETS["indoor"],
    "piezo": ("rainfall_piezo.rain",), "piezo rain": ("rainfall_piezo.rain",), "piezo rain rate": ("rainfall_piezo.rain_rate",),
    "air quality": SENSOR_SETS[AIR_SENSOR], "aq": SENSOR_SETS[AIR_SENSOR],
    # the devices: the weather station is all its readings, the air sensor its usual set
    "all": ALL, "everything": ALL, "every reading": ALL, "weather": ALL, "ecowitt": ALL, "weather station": ALL, "station": ALL,
    "ag": SENSOR_SETS[AIR_SENSOR], "airgradient": SENSOR_SETS[AIR_SENSOR], "air gradient": SENSOR_SETS[AIR_SENSOR],
    "air sensor": SENSOR_SETS[AIR_SENSOR],
    # the weather station's readings, named bare
    **{_w: BARE["temperature"] for _w in ("temperature", "temp", "temperatures")},
    "humidity": BARE["humidity"],
    **{_w: BARE["pressure"] for _w in ("barometer", "barometric pressure", "relative pressure", "air pressure")},
    "absolute pressure": ("pressure.absolute",), "station pressure": ("pressure.absolute",),
    "wind": BARE["wind"], "wind speed": ("wind.wind_speed",), "windspeed": ("wind.wind_speed",),
    **{_w: ("wind.wind_gust",) for _w in ("gust", "gusts", "wind gust", "wind gusts")},
    "wind direction": ("wind.wind_direction",), "direction": ("wind.wind_direction",),
    "rain": BARE["rain"], "precipitation": BARE["rain"],
    "rain rate": ("rainfall.rain_rate",), "rainrate": ("rainfall.rain_rate",), "rain intensity": ("rainfall.rain_rate",),
    "dew point": BARE["dew_point"], "dewpoint": BARE["dew_point"],
    "feels like": BARE["feels_like"], "feelslike": BARE["feels_like"],
    "apparent temperature": ("outdoor.app_temp",), "app temp": ("outdoor.app_temp",),
    **{_w: BARE["vpd"] for _w in ("vpd", "vapour pressure deficit", "vapor pressure deficit")},
    **{_w: BARE["solar"] for _w in ("solar", "solar radiation", "radiation", "sunshine")},
    **{_w: BARE["uv"] for _w in ("uv", "uvi", "uv index", "ultraviolet")},
    # the air sensor's
    **{_w: ("air.pm2_5",) for _w in ("pm2.5", "pm25", "pm 2.5", "pm2 5", "pm 25")},
    **{_w: ("air.pm10",) for _w in ("pm10", "pm 10")},
    **{_w: ("air.pm1",) for _w in ("pm1", "pm 1", "pm1.0", "pm 1.0")},
    **{_w: ("air.co2",) for _w in ("co2", "co₂", "co 2", "carbon dioxide")},
    **{_w: ("air.voc_index",) for _w in ("voc", "vocs", "tvoc", "voc index")},
    **{_w: ("air.nox_index",) for _w in ("nox", "nox index")},
}
CLIMATE_WORDS = {"temperature": "temperature", "temp": "temperature", "humidity": "humidity", "dew point": "dew_point",
                 "dewpoint": "dew_point", "feels like": "feels_like", "apparent temperature": "app_temp", "app temp": "app_temp",
                 "vpd": "vpd", "vapour pressure deficit": "vpd", "vapor pressure deficit": "vpd"}   # after "outdoor", "indoor"
AIR_OWN = {"temperature": "air.temperature", "temp": "air.temperature", "humidity": "air.humidity"}      # after "air.", "airgradient"
SIDE = re.compile(r"(?P<side>outdoors?|outside|indoors?|inside)[. ](?P<rest>.+)")
DEVICE = re.compile(r"(?:air\.|(?:airgradient|air gradient|ag)[. ])(?P<rest>.+)")
LEGACY = {"air.air_temperature": "air.temperature", "air.air_humidity": "air.humidity", "air_temperature": "air.temperature",
          "air_humidity": "air.humidity", "rainfall.daily": "rainfall.rain", "rainfall_piezo.daily": "rainfall_piezo.rain"}   # today's names
AMBIGUOUS = {"air temperature": ("outdoor.temperature", "air.temperature"), "air temp": ("outdoor.temperature", "air.temperature"),
             "air humidity": ("outdoor.humidity", "air.humidity")}   # plain English for the weather, or the air sensor's own
# The words a description and a refusal list, one for each meaning, in this order
WORDS = ("temperature", "humidity", "dew point", "feels like", "vpd", "pressure", "wind", "gust", "wind direction", "rain", "rain rate",
         "solar", "uv", "pm2.5", "pm10", "pm1", "co2", "voc", "nox", "outdoor", "indoor", "rainfall", "air", "all", "ecowitt",
         "weather station", "ag", "airgradient", "air gradient")


def _key(word: str) -> str:
    return " ".join(str(word).lower().replace("_", " ").replace("-", " ").split())


# ---------- a reading ----------
CLIMATE_LABELS = {"temperature": "temperature", "humidity": "humidity", "dew_point": "dew point", "feels_like": "feels like",
                  "app_temp": "apparent temperature", "vpd": "vapour pressure deficit"}
LABELS = {"pressure.relative": "Pressure", "pressure.absolute": "Absolute pressure", "wind.wind_speed": "Wind speed",
          "wind.wind_gust": "Wind gust", "wind.wind_direction": "Wind direction", "rainfall.rain": "Rain", "rainfall.rain_rate": "Rain rate",
          "rainfall_piezo.rain": "Piezo rain", "rainfall_piezo.rain_rate": "Piezo rain rate", "solar_and_uvi.solar": "Solar radiation",
          "solar_and_uvi.uvi": "UV index"}
UNITS = {"wind_gust": "km/h"}   # where the file names no unit and lib/history.py has none either
AIR_NAMES = {"air_temperature": "temperature", "air_humidity": "humidity"}   # the air sensor's own: its column -> its reading's name
SERIES = {**{f"{r.group}.{RAIN if r.field == 'daily' else r.field}": name for name, r in WEATHER.items()},
          **{f"indoor.{WEATHER[name].field}": name for name in INDOOR_READINGS}}   # a name -> lib/series.py's (station readings)


@dataclass(frozen=True)
class Reading:
    """One reading of the catalogue."""
    name: str            # "outdoor.temperature": `sensor.reading`
    label: str           # "Outdoor temperature", "Rain", "PM2.5", "AirGradient temperature"
    unit: str            # "°C", "mm", "µg/m³"; "" for an index (UV, VOC, NOx)
    device: str          # STATION or AIR_DEVICE
    combine: str         # how a stretch of readings is told as one: "mean" (over time), "max" (gusts: the highest), "last" (wind
                         # direction: the reading at its end), "rain" (the mm that fell, summed)
    metric: Metric       # how lib/history.py reads it (read_intervals, read_days): metric.kind, metric.column, metric.sensor
    band: str | None = None     # the reading its line is shaded up to, whose highest goes with it (wind speed: "wind.wind_gust")
    series: str | None = None   # its name in lib/series.py and lib/panels.py ("temperature" for both sides, "wind", "rain", "pm2_5")

    @property
    def sensor(self) -> str:
        """"outdoor", "rainfall", "air": the Ecowitt group, or the air sensor."""
        return self.name.split(".", 1)[0]

    @property
    def reading(self) -> str:
        return self.name.split(".", 1)[1]

    @property
    def kind(self) -> str:
        """The storage table it is in ("climate", "rain", "air" …)."""
        return self.metric.kind

    @property
    def column(self) -> str:
        """The storage column read: rain's is "daily", the running daily total; the air sensor's own temperature "air_temperature"."""
        return self.metric.column.name

    @property
    def drawn(self) -> str:
        """How a chart shows it: "bars" (rain), "rose" (wind direction, the compass rose beside the wind) or "line"."""
        return "bars" if self.combine == RAIN else "rose" if self.reading == "wind_direction" else "line"


def _reading(m: Metric, held: set[str]) -> Reading:
    if m.air:
        reading = AIR_NAMES.get(m.reading, m.reading)
        about = AIR.get(m.reading) or AIR_EXTRA[m.reading]
        return Reading(f"air.{reading}", about.label, about.unit, AIR_DEVICE, m.how, m, series=m.reading)
    name = m.name
    label = f"{label_of(m.sensor)} {CLIMATE_LABELS[m.reading]}" if m.kind == "climate" else LABELS.get(name) or \
        f"{label_of(m.sensor)} {m.reading.replace('_', ' ')}"
    band = "wind.wind_gust" if name == "wind.wind_speed" and "wind.wind_gust" in held else None
    return Reading(name, label, m.unit or UNITS.get(m.reading, ""), STATION, m.how, m, band, SERIES.get(name))


class UnknownReading(Refusal):
    """A reading name or word the catalogue can't read: a Refusal listing the names it holds and the words with what each stands for."""


# ---------- the catalogue ----------
class Catalogue:
    """The readings the service has, by name, in the order the file's groups and the air sensor's metrics come."""

    def __init__(self, readings: Iterable[Reading]):
        self.readings = {r.name: r for r in readings}
        self._by_key = {_key(name): name for name in self.readings}

    @classmethod
    def of(cls, sent: dict[str, dict[str, str]] | None = None, *, station: bool = True, air: bool = True) -> "Catalogue":
        """The readings of the devices there are: the station's those its sensors have sent (`sent`: {group: {field: unit}}, as the
        file's fields table has them; a group with nothing sent yet has every reading of its kind), and the air sensor's."""
        metrics = [*(station_metrics(sent) if station else []), *(air_metrics(extra=True) if air else [])]
        held = {m.name for m in metrics}
        return cls(_reading(m, held) for m in metrics)

    @classmethod
    async def open(cls, eco=None, air=None) -> "Catalogue":
        """The readings the file holds for these sources (Ecowitt, AirGradient; either may be None), as the readings tool reads them."""
        return cls.of(await station_fields(eco) if eco else None, station=eco is not None, air=air is not None)

    @classmethod
    def listed(cls, entries: Iterable[dict]) -> "Catalogue":
        """The catalogue another process holds, from the readings it lists ({name, label, unit}, as ecobot://station's sensors give
        them: a client's, docs/mcp-service.md step 5): each reading as this catalogue makes it, with the label and unit listed."""
        wanted = {e["name"]: e for e in entries if isinstance(e, dict) and e.get("name")}
        made = (_reading(m, set(wanted)) for m in [*station_metrics(None), *air_metrics(extra=True)])
        return cls(replace(r, label=wanted[r.name].get("label") or r.label, unit=wanted[r.name].get("unit", r.unit))
                   for r in made if r.name in wanted)

    @property
    def names(self) -> list[str]:
        return list(self.readings)

    def get(self, name: str) -> Reading | None:
        return self.readings.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self.readings

    def __iter__(self):
        return iter(self.readings.values())

    def resolve(self, names, *, default) -> list[Reading]:
        """The readings these names stand for, in the order named, each once. `names`: a name or words, a list of them, or a string of
        them separated by commas or "and"; none (None, "", []) is `default`, read the same way. Raises UnknownReading."""
        words = _split(names) or _split(default)
        out: dict[str, Reading] = {}
        for word in words:
            for r in self._one(word):
                out.setdefault(r.name, r)
        return list(out.values())

    def words(self) -> dict[str, list[str]]:
        """{word: the names it stands for here}, one word for each meaning (WORDS), those standing for nothing held left out."""
        found = {word: self._lookup(word) for word in WORDS}
        return {word: [r.name for r in readings] for word, readings in found.items() if not isinstance(readings, str)}

    def description(self) -> str:
        """What a tool's readings property says: the full names, the words, and the rule for bare names."""
        air = any(r.device == AIR_DEVICE for r in self)
        station = any(r.device == STATION for r in self)
        parts = [f"Readings, as `sensor.reading`: {', '.join(self.names)}."]
        if station:
            parts.append("Or the words people use. A bare reading is the weather station's: temperature and humidity are outdoor and "
                         "indoor" + (" (the AirGradient's own are only ever air.temperature and air.humidity)" if air else "") + "; "
                         "dew point, feels like and vpd are outdoor's; pressure is relative pressure; rain is the main gauge, the mm "
                         "that fell; also rain rate, gust, uv, solar, wind (its speed shaded up to the gusts, with its direction: the "
                         "compass rose on a chart).")
        if air:
            parts.append("The air sensor's: pm2.5, pm10, pm1, co2, voc, nox.")
        sets = [s for s in ("outdoor", "indoor", "wind", "rainfall", AIR_SENSOR) if any(r.sensor == s for r in self)]
        parts.append(f"A sensor's name ({', '.join(sets)}) is its usual set"
                     + ("; all is every weather-station reading" if station else "") + ".")
        devices = [*(["ecowitt and weather station mean the weather station"] if station else []),
                   *(["ag, airgradient and air gradient the AirGradient air sensor"] if air else [])]
        if devices:
            parts.append(f"The devices: {', '.join(devices)}.")
        return " ".join(parts)

    # ---------- one name ----------
    def _one(self, word: str) -> list[Reading]:
        """The readings one name or word stands for; UnknownReading, with the names and the words, when it stands for none held."""
        found = self._lookup(word)
        if isinstance(found, str):
            raise UnknownReading(found, names=self.names, words=self.words())
        return found

    def _lookup(self, word: str) -> list[Reading] | str:
        """The readings a name or word stands for, or why there are none."""
        exact = " ".join(str(word).lower().split())
        if exact in self.readings:
            return [self.readings[exact]]
        key = _key(exact)
        if key in self._by_key:
            return [self.readings[self._by_key[key]]]
        if key in AMBIGUOUS and exact not in LEGACY:   # ("air_temperature", today's name, is the air sensor's)
            station, own = AMBIGUOUS[key]
            return f"{word!r} could be the weather station's ({station}) or the AirGradient's own ({own}): send the one meant."
        targets = (LEGACY[exact],) if exact in LEGACY else ALIASES.get(key) or _prefixed(key)
        if targets is None:
            return (f"{word!r} isn't a reading this service knows. Send names from `names`, or words from `words` (each with the names "
                    f"it stands for).")
        if targets == ALL:
            targets = tuple(n for name in in_all() for n in BARE[name])
        return [self.readings[t] for t in targets if t in self.readings] or \
            f"{word!r} means {', '.join(targets)}, which this service doesn't hold."


def _prefixed(key: str) -> tuple[str, ...] | None:
    """"outdoor humidity", "indoor.temp", "airgradient humidity", "air.temp", "ag pm2.5": a reading on the sensor named."""
    if m := SIDE.fullmatch(key):
        column = CLIMATE_WORDS.get(m["rest"])
        return (f"{'outdoor' if m['side'].startswith('out') else 'indoor'}.{column}",) if column else None
    if m := DEVICE.fullmatch(key):
        if m["rest"] in AIR_OWN:
            return (AIR_OWN[m["rest"]],)
        found = ALIASES.get(m["rest"])
        return found if found and found != ALL and all(n.startswith("air.") for n in found) else None
    return None


def _split(names) -> list[str]:
    """Names as sent: a string (commas, semicolons, "+", "&" or "and" between them) or a list of such strings."""
    if names is None:
        return []
    items = names if isinstance(names, (list, tuple)) else [names]
    return [w for item in items for w in (part.strip() for part in re.split(r"[,;+&]| and ", str(item))) if w]


CATALOGUE = Catalogue.of()   # every reading the service knows, before any file is read: for descriptions and the evals


def resolve(names, *, default, catalogue: Catalogue | None = None) -> list[Reading]:
    """Catalogue.resolve on `catalogue` (default CATALOGUE)."""
    return (catalogue or CATALOGUE).resolve(names, default=default)


def names_description(catalogue: Catalogue | None = None) -> str:
    """Catalogue.description of `catalogue` (default CATALOGUE)."""
    return (catalogue or CATALOGUE).description()


__all__ = ["ALIASES", "ALL_ORDER", "AIR_DEVICE", "BARE", "CATALOGUE", "Catalogue", "FEELS_LIKE_IN_ALL", "LEFT_OUT_OF_ALL", "Reading",
           "SENSOR_SETS", "STATION", "UnknownReading", "VPD_IN_ALL", "WORDS", "in_all", "names_description", "resolve"]
