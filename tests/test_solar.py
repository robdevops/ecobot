"""lib/solar.py: the sun's elevation and azimuth from a place and a moment (the NOAA algorithm), against known values and by reasoning."""
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from lib import solar

MEL, LON = ZoneInfo("Australia/Melbourne"), ZoneInfo("Europe/London")
MELBOURNE = (-37.81, 144.96)
LONDON = (51.5, -0.12)


def noon_of(place, day, zone):
    """The moment of solar noon on `day` (the elevation's peak, found by scanning the day by the minute)."""
    start = datetime(day.year, day.month, day.day, tzinfo=zone)
    best = max((start + timedelta(minutes=m) for m in range(0, 1440, 1)), key=lambda t: solar.sun_position(*place, t)[0])
    return best


def test_melbourne_at_its_solstices_and_equinox_at_solar_noon():
    summer = noon_of(MELBOURNE, datetime(2026, 12, 21), MEL)
    el, az = solar.sun_position(*MELBOURNE, summer)
    assert el == pytest.approx(90 - 37.81 + 23.44, abs=0.4) and (az < 3 or az > 357), (el, az)   # about 75.6, due north
    winter = noon_of(MELBOURNE, datetime(2026, 6, 21), MEL)
    el, az = solar.sun_position(*MELBOURNE, winter)
    assert el == pytest.approx(28.75, abs=0.4), el
    assert az < 3 or az > 357, az
    equinox = noon_of(MELBOURNE, datetime(2026, 9, 23), MEL)
    el, az = solar.sun_position(*MELBOURNE, equinox)
    assert el == pytest.approx(90 - 37.81, abs=0.5) and (az < 3 or az > 357), (el, az)


def test_london_in_june_at_noon_is_high_and_due_south():
    noon = noon_of(LONDON, datetime(2026, 6, 21), LON)
    el, az = solar.sun_position(*LONDON, noon)
    assert el == pytest.approx(90 - 51.5 + 23.44, abs=0.4) and az == pytest.approx(180, abs=3), (el, az)   # about 62, due south
    # and in December low (about 15 degrees), still south
    el, az = solar.sun_position(*LONDON, noon_of(LONDON, datetime(2026, 12, 21), LON))
    assert el == pytest.approx(90 - 51.5 - 23.44, abs=0.4) and az == pytest.approx(180, abs=3), (el, az)


def test_morning_is_east_afternoon_west_night_below_the_horizon():
    day = datetime(2026, 3, 20)
    el, az = solar.sun_position(*MELBOURNE, day.replace(hour=8, tzinfo=MEL))
    assert 0 < el < 40 and 60 < az < 100, (el, az)
    el, az = solar.sun_position(*MELBOURNE, day.replace(hour=16, tzinfo=MEL))
    assert 0 < el < 40 and 260 < az < 320, (el, az)
    el, _ = solar.sun_position(*MELBOURNE, day.replace(hour=1, tzinfo=MEL))
    assert el < -30
    # the northern hemisphere's too: the morning in the east, the afternoon in the west
    assert 60 < solar.sun_position(*LONDON, datetime(2026, 6, 21, 8, tzinfo=LON))[1] < 120
    assert 240 < solar.sun_position(*LONDON, datetime(2026, 6, 21, 17, tzinfo=LON))[1] < 300


def test_the_equinox_sun_rises_due_east_and_sets_due_west_and_the_equator_noon_is_overhead():
    # at an equinox the declination is nil: the sun is on the horizon due east at the hour angle -90 degrees (about six hours before solar noon)
    noon = noon_of(MELBOURNE, datetime(2026, 9, 23), MEL)
    el, az = solar.sun_position(*MELBOURNE, noon - timedelta(hours=6))
    assert abs(el) < 2 and az == pytest.approx(90, abs=4), (el, az)
    el, az = solar.sun_position(*MELBOURNE, noon + timedelta(hours=6))
    assert abs(el) < 2 and az == pytest.approx(270, abs=4), (el, az)
    el, _ = solar.sun_position(0, 0, datetime(2026, 3, 20, 12, 7, tzinfo=timezone.utc))
    assert el > 85


def test_the_declination_by_the_date_matches_the_axial_tilt():
    # at a place on the equator, the elevation at solar noon is 90 less the declination's size: 0 at an equinox, 23.4 degrees off at a solstice
    for day, off in ((datetime(2026, 3, 20), 0), (datetime(2026, 6, 21), 23.44), (datetime(2026, 12, 21), 23.44)):
        el, _ = solar.sun_position(0, 0, noon_of((0, 0), day, timezone.utc))
        assert el == pytest.approx(90 - off, abs=0.5), (day, el)


def test_symmetric_about_solar_noon_the_elevation_equal_and_the_azimuths_mirrored():
    noon = noon_of(MELBOURNE, datetime(2026, 11, 18), MEL)
    for minutes in (30, 90, 200, 300):
        before = solar.sun_position(*MELBOURNE, noon - timedelta(minutes=minutes))
        after = solar.sun_position(*MELBOURNE, noon + timedelta(minutes=minutes))
        assert before[0] == pytest.approx(after[0], abs=0.15), (minutes, before, after)
        assert (before[1] + after[1]) % 360 == pytest.approx(0, abs=1.0) or (before[1] + after[1]) == pytest.approx(360, abs=1.0), (minutes, before, after)


def test_daylight_saving_is_the_zones_the_same_instant_gives_the_same_sun_in_any_zone():
    instant = datetime(2026, 1, 15, 12, 30, tzinfo=MEL)   # (UTC+11 in summer)
    a = solar.sun_position(*MELBOURNE, instant)
    b = solar.sun_position(*MELBOURNE, instant.astimezone(timezone.utc))
    c = solar.sun_position(*MELBOURNE, instant.astimezone(ZoneInfo("America/New_York")))
    assert a == pytest.approx(b, abs=1e-6) and a == pytest.approx(c, abs=1e-6)
    # an hour of daylight saving: the same wall clock is an hour earlier in the sun's day in summer than a winter one's
    summer = solar.sun_position(*MELBOURNE, datetime(2026, 1, 15, 13, 0, tzinfo=MEL))
    assert summer[0] > 60   # (13:00 DST is noon by the sun, about: high)
    with pytest.raises(ValueError):
        solar.sun_position(*MELBOURNE, datetime(2026, 1, 15, 13, 0))


def test_refraction_raises_a_low_sun_by_about_half_a_degree_and_a_high_one_hardly_at_all():
    assert solar.refraction(0) == pytest.approx(0.48, abs=0.1) and solar.refraction(45) == pytest.approx(0.017, abs=0.005) and solar.refraction(89) == 0
    when = datetime(2026, 3, 20, 7, 0, tzinfo=MEL)
    plain, refracted = solar.sun_position(*MELBOURNE, when)[0], solar.sun_position(*MELBOURNE, when, refracted=True)[0]
    assert refracted > plain


def test_the_azimuth_moves_through_a_day_without_a_jump():
    start = datetime(2026, 12, 21, tzinfo=MEL)
    points = [solar.sun_position(*MELBOURNE, start + timedelta(minutes=m)) for m in range(0, 1440, 5)]
    day = [p for p in points if p[0] > 0]
    steps = [(b[1] - a[1] + 540) % 360 - 180 for a, b in zip(day, day[1:])]
    assert all(-30 < s < 0 for s in steps), steps   # (round the compass the one way, never back: SE through N to SW in the southern summer: falling)
