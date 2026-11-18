"""The sun's place in the sky from a site's latitude and longitude and a moment (the NOAA solar position algorithm: the fractional year,
the equation of time, the solar declination, true solar time, the hour angle, zenith and azimuth; good to a fraction of a degree), in plain
Python with no library. The dashboard's UV beams lean with it (web_dashboard.sun_of()), and sun_times() gives the day's sunrise and sunset (the standard
sunrise equation, -0.833 degrees; None for a polar day or night: "no set" / "no rise").

    sun_position(51.5, -0.12, datetime(2026, 6, 21, 13, 2, tzinfo=ZoneInfo("Europe/London")))  ->  (elevation, azimuth)

The moment must be aware: its own UTC offset (so the time zone's DST) places it as an instant, which the equation of time and the
longitude are set against. Elevation in degrees above the horizon (negative at night); azimuth in degrees clockwise from north (0 north, 90 east, 180 south,
270 west)."""
import math
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import NamedTuple


def refraction(elevation: float) -> float:
    """The atmosphere's bending of the light, in degrees, to add to a geometric elevation (NOAA's piecewise fit; the standard
    atmosphere: its size at the horizon is about half a degree)."""
    if elevation > 85:
        return 0.0
    t = math.tan(math.radians(elevation))
    if elevation > 5:
        seconds = 58.1 / t - 0.07 / t ** 3 + 0.000086 / t ** 5
    elif elevation > -0.575:
        e = elevation
        seconds = 1735 + e * (-518.2 + e * (103.4 + e * (-12.79 + e * 0.711)))
    else:
        seconds = -20.772 / t
    return seconds / 3600


def equation_of_time(year: int, yday: int, hour: float) -> tuple[float, float]:
    """(the equation of time in minutes, the solar declination in radians) at this UTC day of the year (1 for January 1st) and hour
    (NOAA's fits of the fractional year gamma)."""
    days = 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365
    gamma = 2 * math.pi / days * (yday - 1 + (hour - 12) / 24)                                # the fractional year, radians
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)     # the equation of time, minutes
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma) - 0.006758 * math.cos(2 * gamma)   # the declination, radians
            + 0.000907 * math.sin(2 * gamma) - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    return eqtime, decl


def sun_position(lat: float, lon: float, when: datetime, refracted: bool = False) -> tuple[float, float]:
    """(elevation, azimuth) in degrees of the sun at latitude `lat` (north positive) and longitude `lon` (east positive) at the aware moment
    `when`; `refracted` adds the atmosphere's refraction to the elevation (as the sun looks, not where it geometrically is)."""
    if when.utcoffset() is None:
        raise ValueError("the moment must be aware (have a time zone)")
    when = when.astimezone(timezone.utc)   # (the instant, whatever zone it is given in: its local clock's offset, DST in it, is undone)
    hour = when.hour + when.minute / 60 + when.second / 3600 + when.microsecond / 3.6e9
    eqtime, decl = equation_of_time(when.year, when.timetuple().tm_yday, hour)
    solar_time = hour * 60 + eqtime + 4 * lon                                                # the true solar time, minutes (UTC's hour: no offset)
    hour_angle = math.radians(solar_time / 4 - 180)
    phi = math.radians(lat)
    sin_el = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(hour_angle)
    elevation = math.degrees(math.asin(max(-1.0, min(1.0, sin_el))))
    # from north, clockwise: due south at the hour angle 0 for a site north of the sun, due north for one south of it
    azimuth = (math.degrees(math.atan2(math.sin(hour_angle), math.cos(hour_angle) * math.sin(phi) - math.tan(decl) * math.cos(phi))) + 180) % 360
    if refracted:
        elevation += refraction(elevation)
    return elevation, azimuth


SUNRISE_ELEVATION = -0.833   # degrees: the sun's centre at sunrise and sunset (refraction 0.567 plus the disc's radius 0.266)


class SunTimes(NamedTuple):
    """Sunrise and sunset as minutes since local midnight in the station's zone (None: none that day) and a note: None, "no set" (the sun
    stays up all day: polar day) or "no rise" (it stays down: polar night; no set either)."""
    rise: float | None
    set: float | None
    note: str | None


def sun_times(lat: float, lon: float, day: date, zone: tzinfo) -> SunTimes:
    """The standard sunrise equation for the local date `day` at latitude `lat` and longitude `lon` (east positive), in the zone `zone`
    (its UTC offset that day: DST in it). Solar noon (UTC minutes) = 720 - 4 * lon - the equation of time, the declination and the equation
    taken at that noon (found by refinement); the hour angle w0 = acos((sin(-0.833 deg) - sin lat sin dec) / (cos lat cos dec)); sunrise
    and sunset = solar noon -/+ 4 * w0 (degrees) minutes. Minutes to a hundredth, wrapped into the local day. The acos's argument beyond
    1 (the sun never reaches the horizon: polar night) or below -1 (it never sets: polar day) gives None (note "no rise" / "no set")."""
    noon_local = datetime(day.year, day.month, day.day, 12, tzinfo=zone)
    offset = noon_local.utcoffset().total_seconds() / 60       # the zone's offset that day, minutes
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)                           # (the date's UTC midnight: solar noon is minutes on)
    noon, decl = 720 - 4 * lon, 0.0
    for _ in range(2):                                                                           # (the equation taken at the noon it gives, twice)
        at = start + timedelta(minutes=noon)
        eqtime, decl = equation_of_time(at.year, at.timetuple().tm_yday, at.hour + at.minute / 60 + at.second / 3600)
        noon = 720 - 4 * lon - eqtime
    phi = math.radians(lat)
    cosw = (math.sin(math.radians(SUNRISE_ELEVATION)) - math.sin(phi) * math.sin(decl)) / (math.cos(phi) * math.cos(decl)) \
        if abs(math.cos(phi) * math.cos(decl)) > 1e-12 else (2.0 if lat * decl < 0 else -2.0)
    if cosw > 1:
        return SunTimes(None, None, "no rise")
    if cosw < -1:
        return SunTimes(None, None, "no set")
    w0 = math.degrees(math.acos(cosw))
    local = lambda utc: round((utc + offset) % 1440, 2)
    return SunTimes(local(noon - 4 * w0), local(noon + 4 * w0), None)
