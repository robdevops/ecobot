"""The air sensor's gauges (AIR_GAUGES): what each shows and its scale, on US standards, drawn after the weather station's (lib/ecowitt/
dashboard.py GAUGES) by lib/gauge_sheet.py from the service's `current` with context.

Every air gauge is a dial whose coloured bands are its categories, each band an equal share of the dial (as AirNow's AQI dial draws
them), the needle placed in proportion within its band: so the bands that matter are as easy to read as the rest, and a scale that runs
to 500 (or 600 µg/m³) doesn't squeeze "good" and "moderate" into a sliver. The numbers round the dial are the categories' breakpoints.

  - US AQI (from PM2.5 now, by the service's own lib/airgradient/metrics.py pm25_aqi: the AirGradient's `aqi_us` is of the lower of the
    last two readings, so a dial of it could disagree with the PM2.5 dial beside it): 0-500, the EPA's six categories and colours;
  - PM2.5 (µg/m³): the EPA's 2024 breakpoints, 0-9.0, 9.1-35.4, 35.5-55.4, 55.5-125.4, 125.5-225.4, 225.5+ (to 325.4, AQI 500);
  - PM10 (µg/m³): the EPA's breakpoints, 0-54, 55-154, 155-254, 255-354, 355-424, 425+ (to 604, AQI 500);
  - CO2 (ppm): there is no EPA standard for it; the bands are the usual US indoor guidance, from ASHRAE's (ventilation keeps a room
    within about 700 ppm of outdoors): under 800 good, 800-1000 fair, 1000-1500 poor, 1500-2000 bad, 2000+ very bad, on a 400-2500 dial
    (400: outdoor air);
  - VOC index and NOx index: Sensirion's indices the AirGradient reports (1-500, learnt from this sensor's own air): VOC's 100 is its
    average, so under 100 low, 100-150 average, 150-250 elevated, 250-400 high, 400-500 very high (green, a paler green, then
    the EPA's yellow, orange and red); NOx's baseline is 1, so 1-20 low (as the service rates it good), 20-150 elevated, 150-300
    high, 300-500 very high (the EPA's first four colours). The edges at 150 and 250 (VOC) and 20 and 150 (NOx) are the service's own
    ratings (lib/series.py AIR).

Left out: PM1 (no US standard for it), and the sensor's own temperature and humidity (it is outdoors: the station's outdoor gauges show
the same air).
"""

from dataclasses import dataclass

from .metrics import PM25_AQI

# The EPA's colours for the AQI's categories, good to hazardous, and their names
EPA = ("#00E400", "#FFFF00", "#FF7E00", "#FF0000", "#8F3F97", "#7E0023")
EPA_NAMES = ("Good", "Moderate", "Unhealthy for Sensitive Groups", "Unhealthy", "Very Unhealthy", "Hazardous")
VOC_COLOURS = ("#00E400", "#9BE564", "#FFFF00", "#FF7E00", "#FF0000")   # (its average, 100-150, a paler green: the usual air)


@dataclass(frozen=True)
class AirGauge:
    title: str
    unit: str
    column: str                       # the air sensor's reading (air.<column>, lib/service/names.py)
    bounds: tuple[float, ...]         # the bands' edges, low to high: one more than the colours
    colours: tuple[str, ...]
    names: tuple[str, ...]            # each band's name, shown under the reading
    step: float                       # the breakpoints' resolution: a reading below an edge + step is still in the band below
    decimals: int = 0
    aqi: bool = False                 # shows PM2.5's US AQI (pm25_aqi of the reading now), not the reading itself

    @property
    def reading(self) -> str:
        return f"air.{self.column}"

    @property
    def low(self) -> float:
        return self.bounds[0]

    @property
    def high(self) -> float:
        return self.bounds[-1]

    def band(self, value: float | None) -> str | None:
        """The name of the band a reading is in (None for none)."""
        if value is None:
            return None
        return next((name for edge, name in zip(self.bounds[1:], self.names) if value < edge + self.step), self.names[-1])


AIR_GAUGES = (
    AirGauge("US AQI", "", "pm2_5", (0, *(a_hi for *_, a_hi, _ in PM25_AQI)), EPA, EPA_NAMES, 1, aqi=True),
    AirGauge("PM2.5", "µg/m³", "pm2_5", (0, *(hi for _, hi, *_ in PM25_AQI)), EPA, EPA_NAMES, 0.1, 1),
    AirGauge("PM10", "µg/m³", "pm10", (0, 54, 154, 254, 354, 424, 604), EPA, EPA_NAMES, 1),
    AirGauge("CO₂", "ppm", "co2", (400, 800, 1000, 1500, 2000, 2500), EPA[:5], ("Good", "Fair", "Poor", "Bad", "Very bad"), 0),
    AirGauge("VOC index", "", "voc_index", (1, 100, 150, 250, 400, 500), VOC_COLOURS,
             ("Low", "Average", "Elevated", "High", "Very high"), 0),
    AirGauge("NOx index", "", "nox_index", (1, 20, 150, 300, 500), EPA[:4], ("Low", "Elevated", "High", "Very high"), 1),
)


def air_gauges_of(titles=None) -> list[AirGauge]:
    """The air gauges with these titles, in the dashboard's order (all of them when None)."""
    return [g for g in AIR_GAUGES if titles is None or g.title in titles]


__all__ = ["AIR_GAUGES", "AirGauge", "EPA", "EPA_NAMES", "air_gauges_of"]
