# Math in ecobot: which formulas estimate reality

This file lists the formulas in the code: sun and sky geometry, the weather effects on the dashboard Sky, gauges and units, and the server-side derived readings. For each one it says whether it estimates something real or only shapes how the page looks.

Line numbers are approximate. Search for the named constant or function instead of trusting a number. Sky distances are in "art px", the low-resolution canvas pixel (3 to 4 CSS px, `ART_PX` 480). Art px have no physical length, so Sky speeds in px/s are only meaningful as ratios.

## Labels

| Label | Meaning | Example |
|---|---|---|
| REAL-MODEL | A published formula or exact identity, used as published. Its error is the model's own. | NOAA sun position, unit conversions, `r^3` bead volume |
| REAL-APPROX | The right physical idea in a simplified form. It tracks reality in some range and fails outside it; the limits are listed. | Rain slant `tan(angle) = wind / fall speed` with one fall speed |
| TUNED | A number chosen by look or feel, or a rule of thumb, on a real quantity. It says "more of X, more of the effect", not how much. | Rain strength ramp, pressure arrow 0.3 hPa |
| ARTISTIC | Made up for looks or behaviour. It estimates nothing. | Ember glow wobble, eddy sizes, frog timing |

How to read the tables:

- "Where" is a file and an approximate line, or a function or constant name.
- Formulas are plain text. `x^n` is a power, `clamp(x, a, b)` limits x to [a, b], `smoothstep(u) = u*u*(3 - 2*u)`.
- A formula with two labels (for example REAL-MODEL structure, TUNED constants) is listed with both. The summary counts it under its weaker label.
- "Faithful?" says how close to reality the formula is and where it stops being right.
- A number appears only if it is in the code or a cited standard.

Key facts of the code as it is:

- The rain is three drop-size classes (far 0.6, mid 1.0, near 1.5 times the median drop D0 of the live rain rate): each layer's lean is `tan(angle) = wind / (v_t * 3.6 km/h)` (cap 70 degrees), its vertical fall speed follows its terminal speed v_t (path speed `V / cos(angle)`), and `TEST_WIND = 11` is a test link's wind. Section 3.2.
- Each wind eddy rests 5 to 9 s after its life before the next (row A8 and section 3.3).
- The heat is a blinding white ball of a sun (a few art px across, sized to the strip above the first row of tiles) on a clean arc along that strip: its place along it from the day fraction between the real sunrise and sunset, its height from the day's noon elevation; the UV is twelve alike beams all round it, the fan sweeping slowly back and forth as one, each running on to the window's edge or the flood's surface and fading with the distance (5 % of its strength by two thirds of the window's height at UV 11). Section 4.2.
- The haze's opacity follows the air sensor's PM2.5 (Beer-Lambert, `1 - exp(-0.01457*PM2.5)`, the viewing distance chosen). Section 4.1.
- The flash flood is a leaky bucket run on the server (`flood_of`, tau 30 min, runoff share 0.5, full at 15 mm). Section 4.5.
- The pressure arrow is the WMO 3-hour tendency in its classes (slow, moderate, quick, rapid). Section 7.3.
- Sunrise and sunset come from the sunrise equation (`solar.sun_times`). Sections 2.2 and 8.1.
- Section 9 lists the known limitations: where the code departs from reality.

## 1. Summary of formula groups

One row per group, one label per row (the weakest label inside the group; mixed groups say so in the section).

| ID | Formula group | Section | Label |
|---|---|---|---|
| S1 | Sun position (NOAA solar position) | 2.1 | REAL-MODEL |
| S2 | Atmospheric refraction (unused by the dashboard) | 2.1 | REAL-MODEL |
| S3 | UV beam lean from sun azimuth and elevation | 2.2 | REAL-APPROX |
| S4 | Beam lean fallback curve (no sun position) | 2.2 | TUNED |
| S5 | Sun position run-on between fetches | 2.2 | REAL-APPROX |
| S6 | Sunrise and sunset fallback 06:00 / 18:00 (no station location only) | 2.2 | TUNED |
| S7 | Beams: twelve, alike, round the sun, running on to the window's edge or the water, the root faded in from the ball | 2.2 | ARTISTIC |
| S7b | Beam strength 0.17 at the root (the design mock's 0.23 at its beam intensity 0.7, 0.161, over the ripple's 0.95 against the page's 0.9) and the violet's soft cap (knee 0.45, ceiling 0.62 of the ramp) | 2.2 | TUNED |
| S7d | Beam fade with the distance from the sun: a smooth falloff to 5 % of the strength near the sun at two thirds of the window's height (UV 11), then a tail, cut where it falls under 1 %; the beams run to the window's edge or the flood's surface | 2.2 | TUNED |
| S7a | Beam sweep: the fan's angle `12 sin(2 pi t / 80 s)` degrees, each beam's own sway 2.4 degrees at 0.05 to 0.1 Hz | 2.2 | TUNED |
| S8 | The sun's arc across the window (day fraction from the real sunrise and sunset, noon elevation) | 4.2 | REAL-APPROX |
| S7c | Beam look held to the design mock's: the cross-section's reference reach 0.95 of the way to 0.34 H, the sides' fade 14 art px, the violet's steps dithered at 0.6 of the blue noise's swing, the beams' field at every art pixel | 2.2 | TUNED |
| S9 | The sun's strip above the first row of tiles, the ball's size in it, the arc's rise by the noon elevation and the glow's limit (0.34 of the height) | 4.2 | TUNED |
| A1 | Effect levels from the readings (`conditionsOf`) | 3.1 | TUNED |
| A2 | Wind side from wind direction | 3.1 | REAL-APPROX |
| A3 | Rain slant law (per layer, by drop size) | 3.2 | REAL-APPROX |
| A4 | Rain effective speed and smoothing | 3.2 | TUNED |
| A5 | Rain fall speed (per layer by terminal speed, path speed `V / cos`); A5a drop size from rain rate (Marshall-Palmer D0) and Gunn-Kinzer terminal speed | 3.2 | REAL-APPROX |
| A5b | Rain layer speeds, streak length, landing, splash | 3.2 | ARTISTIC |
| A6 | Rain drop count and density | 3.2 | TUNED |
| A7 | Leaf wind: stream function turbulence | 3.3 | ARTISTIC |
| A8 | Leaf wind: eddies, gust pulses and bands, veer | 3.3 | ARTISTIC |
| A9 | Leaf relaxation toward the air velocity | 3.3 | REAL-APPROX |
| A10 | Particle budget and brightness cap | 3.1 | TUNED |
| A11 | Frame rate, fades, direction easing | 3.1 | ARTISTIC |
| A12 | Smoothstep easing | 3.1 | REAL-MODEL |
| A13 | Wind speed to haze pan | 4.1 | TUNED |
| A14 | Leaf pace scale `k` from the wind speed | 3.3 | TUNED |
| H1 | Haze band from AQI band edges; its opacity from PM2.5 (Beer-Lambert) | 4.1 | REAL-MODEL |
| H2 | Haze texture, tint, palette, motes | 4.1 | ARTISTIC |
| H3 | Heat level (`0.3 + 0.7 (T - 30)/13`), the ball's size `Rh = 1.4 R (3.4 + 2.6h)` (the halo 1.4 times as wide), strength `pk` and colour by heat, the ember bloom's strength `bI` (0.62) and its colour read `0.6 + 0.4h` | 4.2 | TUNED |
| H4 | Heat's white ball and its glow profile, the dark-orange ember and the ember bloom's outline (6.5 R by 5.2 R) and wobble | 4.2 | ARTISTIC |
| H5 | The furnace's breathing amplitude (radius +-1.5 % at 30 C to +-22 % at 43 C, brightness +-3 % to +-30 %) and the glare's baseline growth with the heat (+25 %) | 4.2 | TUNED |
| H6 | The breathing's three periods (7, 11, 17 s), weights and phases, none above 0.15 Hz | 4.2 | ARTISTIC |
| H5 | UV trigger (fades in from UV index 6, once the violet shows, to full at 11) | 4.3 | TUNED |
| H6 | Ice reach, growth and melt rates | 4.4 | TUNED |
| H7 | Ice crystal forms | 4.4 | ARTISTIC |
| H8 | Flood trigger: a leaky-bucket runoff model on the server (tau 30 min, runoff share 0.5, full 15 mm) | 4.5 | REAL-APPROX |
| H9 | Flood level, chop, frog timing, gust objects | 4.5 | ARTISTIC |
| H10 | Flood wave equation and wind drift | 4.5 | REAL-APPROX |
| H11 | Gust-object trigger (gust above 99 km/h) | 4.5 | TUNED |
| B1 | Humidity trigger (90 % on, 85 % off) and fog level | 5.1 | TUNED |
| B2 | Bead volume `r^3` and merging | 5.2 | REAL-MODEL |
| B3 | Bead gathering, leaving, evaporation | 5.2 | REAL-APPROX |
| B4 | Bead pinning threshold `RUN_R` | 5.3 | REAL-APPROX |
| B9 | Size-dependent pinning under tilt: `sin(alpha_crit) ~ 1/r^2`, `r_crit = RUN_R / sqrt(s)` | 5.3 | REAL-APPROX |
| B10 | Lean before giving way and braking to a stop under `r_crit` | 5.3 | ARTISTIC |
| B5 | Bead speed law and glass pace | 5.3 | TUNED |
| B6 | Condensation, swell and fog timing rates | 5.2 | TUNED |
| B7 | Snap, pop, gulp, stretch, lock-on, meander, pauses | 5.3 | ARTISTIC |
| B8 | Heavy-rain beads and splash | 5.4 | ARTISTIC |
| T1 | Gravity direction from the accelerometer | 6 | REAL-MODEL |
| T2 | Steepness `sin(theta)` | 6 | REAL-MODEL |
| T3 | Steepness response curve and dead zone | 6 | TUNED |
| T4 | Face-down treated as flat | 6 | REAL-APPROX |
| T5 | Wind push on the beads' run direction | 6 | TUNED |
| G1 | Display unit conversions | 7.1 | REAL-MODEL |
| G2 | Scale tick and end re-rounding | 7.1 | TUNED |
| G3 | Linear needle share and thermometer level | 7.2 | REAL-MODEL |
| G4 | Banded share, needle angle, beaker floor, band colouring | 7.2 | TUNED |
| G5 | Gauge scale ranges and thermometer widening | 7.2 | TUNED |
| G6 | Table Max / Min / Gust / Total | 7.3 | REAL-MODEL |
| G7 | Pressure 3-hour tendency arrow (WMO classes) | 7.3 | REAL-MODEL |
| G8 | Compass sector and needle | 7.4 | REAL-MODEL |
| G9 | Wind rose | 7.4 | REAL-APPROX |
| G10 | Tile layout fit and gallery gestures | 7.5 | TUNED |
| G11 | Art pixel size and canvas margin | 7.5 | TUNED |
| G12 | Stale-reading rule (1 hour) | 7.5 | TUNED |
| G13 | Dial, needle, thermometer, beaker geometry | 7.5 | ARTISTIC |
| G14 | Most common wind direction (16-point binning, calm left out, nearest-to-latest tie rule) | 7.4 | REAL-APPROX |
| D1 | Server sun position call and sunrise/sunset times (sunrise equation, `solar.sun_times`) | 8.1 | REAL-MODEL |
| D2 | Magnus dew point (outlook fallback only) | 8.2 | REAL-MODEL |
| D3 | Server unit conversions | 8.2 | REAL-MODEL |
| D4 | US AQI from PM2.5 and PM10 (truncated, instantaneous) | 8.3 | REAL-APPROX |
| D5 | AQI and PM gauge bands (EPA edges) | 8.3 | REAL-MODEL |
| D6 | PM1, CO2, VOC, NOx bands and ratings | 8.3 | TUNED |
| D7 | Spike removal | 8.3 | TUNED |
| D8 | Means, minima, maxima, time-weighting | 8.4 | REAL-MODEL |
| D9 | Rain from the daily counter (plausible-reset rule) | 8.4 | REAL-APPROX |
| D10 | Display smoothing, row caps, MIN_READINGS | 8.4 | TUNED |
| D11 | Outlook rain verdict (pressure by start-to-end change) | 8.2 | TUNED |
| D12 | Forecast rain chance from the ensemble | 8.5 | REAL-APPROX |
| D13 | Forecast-revision alert thresholds | 8.5 | TUNED |
| D14 | Day look rules | 8.5 | TUNED |
| D15 | Clear-sky irradiance (Haurwitz, Cooper) | 8.5 | REAL-APPROX |
| D16 | Statistics scan (rank correlation, p-values, FDR) | 8.6 | REAL-MODEL |
| D17 | Scan and analysis verdict thresholds, minimum-data constants | 8.6 | TUNED |
| D18 | Circular mean of wind direction | 8.6 | REAL-MODEL |
| D19 | Alert rule thresholds | 8.6 | TUNED |

Count per label (counted from the table above):

| Label | Groups |
|---|---|
| REAL-MODEL | 19 |
| REAL-APPROX | 19 |
| TUNED | 33 |
| ARTISTIC | 13 |
| Total | 84 |

Not computed here at all (the station or a provider supplies them): dew point, feels like, apparent temperature, VPD, pollen and thunderstorm-asthma levels, and the dashboard's heat index or wind chill (none exists in `lib/`).

## 2. Sun and sky geometry

### 2.1 Sun position: `lib/solar.py` (NOAA solar position algorithm)

Inputs: latitude and longitude (degrees, east positive) and an aware datetime. The datetime is converted to UTC first (`solar.py:49`), so no time-zone term is needed. `web_dashboard.sun_of()` calls it with `refracted=False`.

| # | Formula | Where | Math | Label | Faithful? |
|---|---|---|---|---|---|
| 1 | Fractional year | solar.py:36 | `gamma = 2*pi/D * (doy - 1 + (hour_UTC - 12)/24)` rad; D = 366 in a leap year, else 365 | REAL-MODEL | Matches NOAA. |
| 2 | Equation of time | solar.py:37 | `EoT = 229.18*(0.000075 + 0.001868*cos(g) - 0.032077*sin(g) - 0.014615*cos(2g) - 0.040849*sin(2g))` minutes | REAL-MODEL | NOAA's Fourier fit; good to about 0.5 to 1 min (about 0.1 degree of sun position). |
| 3 | Declination | solar.py:39 | `d = 0.006918 - 0.399912*cos(g) + 0.070257*sin(g) - 0.006758*cos(2g) + 0.000907*sin(2g) - 0.002697*cos(3g) + 0.00148*sin(3g)` rad | REAL-MODEL | Spencer series, about 0.05 degree; ignores nutation. |
| 4 | True solar time | solar.py:52 | `TST = 60*hour_UTC + EoT + 4*lon` minutes | REAL-MODEL | Exact for the model. |
| 5 | Hour angle | solar.py:53 | `H = TST/4 - 180` degrees | REAL-MODEL | Exact. |
| 6 | Elevation | solar.py:55 | `sin(el) = sin(lat)*sin(d) + cos(lat)*cos(d)*cos(H)`; clamped to [-1, 1] before asin | REAL-MODEL | Geometric (not refracted). Survey comparison with an independent almanac algorithm: within 0.45 degree, typically 0.1 to 0.25. |
| 7 | Azimuth (clockwise from north) | solar.py:58 | `az = (atan2(sin(H), cos(H)*sin(lat) - tan(d)*cos(lat)) + 180) mod 360` degrees | REAL-MODEL | Correct standard form. Ill-conditioned when the sun is near the zenith (tropics at noon). |
| 8 | Refraction (only if `refracted=True`) | solar.py:16 | NOAA piecewise fit, arcsec: el above 85: 0; 5 to 85: `58.1/t - 0.07/t^3 + 0.000086/t^5` with `t = tan(el)`; -0.575 to 5: polynomial in el; below -0.575: `-20.772/t`. Divided by 3600 to give degrees. | REAL-MODEL | Standard atmosphere only; real refraction varies by up to about 10 % with pressure and temperature. NOAA's own fit has a small step (about 0.6 arcmin) at -0.575 degree. The dashboard does not use it. |

Spot value: (-37.81, 144.96) at 13:02 Melbourne time on 21 Dec 2026 gives elevation 75.2, azimuth 14.3 degrees.

### 2.2 Sun use on the dashboard (UV beams)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Beam lean | `leanAt` web_dashboard.html:996 | `lean = clamp(-atan(sin(az) / tan(max(el, 3 deg))), -75, 75)` degrees from vertical; `EL_LOW` 3, `BEAM_MAX` 75 | REAL-APPROX | The sun's direction projected on the east/up plane, east on the right of the screen (as on the compass dial). It drops the north/south component (toward or away from the viewer). The 3 degree floor and 75 degree cap are display limits. Example: az 85.7, el 45.6 gives -44.4 degrees; az 111, el 10.8 gives -78, capped to -75. For a northern-hemisphere station the left/right sense would depend on which way the window faces. |
| Fallback lean | `leanOf` :992 | `h = clamp((t - noon) / ((set - rise)/2), -1, 1)`, `lean = 70 * sin(pi/2 * h)`; `noon = (rise + set)/2`; `BEAM_CURVE` 70 | TUNED | Right sign (negative in the morning) but no latitude or season; 70 degrees at the extremes, not 90. Used only when no sun position exists. |
| Sunrise, sunset fallback | `SUN_FALLBACK` web_dashboard.py | 06:00 and 18:00 (360 and 1080 min) only with no station location | TUNED | Wrong by up to about 3 hours at Melbourne's solstices; used only while no location exists. |
| Sunrise and sunset | `sun_times` solar.py, `sun_of` web_dashboard.py | The sunrise equation. Solar noon (UTC min) = `720 - 4 lon - eqtime` (eqtime and declination `dec` from the NOAA fits at that noon, refined twice); `w0 = acos((sin(-0.833 deg) - sin(lat) sin(dec)) / (cos(lat) cos(dec)))`; rise/set = noon -/+ `4 w0` min; plus the zone's UTC offset that date (DST), wrapped into the day, 0.01 min. -0.833 deg = refraction 0.567 + the disc's radius 0.266. An acos argument above 1: polar night (None, "no rise"); below -1: polar day (None, "no set"); `sun_of` then sends rise 0, set 1440 (day) or both null (night: no beams) | REAL-MODEL | Checked against the sun's own elevation crossing -0.833 deg (within a minute or two): Melbourne 2026-12-21 05:54 / 20:41 (the longitude puts solar noon at 13:18 DST), 2026-06-21 07:35 / 17:08; London 04:43 / 21:21 BST. The forecast's sunrise and sunset are not used. Ignores altitude and the horizon's real shape. |
| Run-on | `setSun` :998, `placeNow` :986 | `rate = (new - last) / dt` for samples 30 s to 3600 s apart (azimuth difference the short way: `((d + 540) mod 360) - 180`); position runs on at that rate for at most 1800 s (`RUN_ON`) | REAL-APPROX | Linear extrapolation; accurate to a small fraction of a degree between the 60 s fetches. If data stops, the error grows (near transit in a Melbourne summer the azimuth moves roughly 50 degrees an hour). |
| Beams | `BEAMS`, `art` `render` | twelve beams spaced 30 degrees all round the sun (each placed within 0.06 rad of its place, seeded), each `2 * 0.13 * Lmax * (0.93 to 1.07)` wide at its base (`Lmax` the longest possible reference reach, `0.95 * ((0.34 H - 3 - y - R) / 1.1) * 1.1 * (0.6 + 0.4 uvn)` (`BEAM_REACH` 0.95; `0.34 H` is `BEAM_REF`): the width follows the reference reach, 0.26 of it, not the sun's size), widening along the distance `d` as `1 + 0.9 (d / L)^0.8` times up to the beam's reference reach `L = 0.95 * ((0.34 H - 3 - y - R) / 1.1) * (0.6 + 0.4 uvn) * (0.9 to 1.1) + R` (`y` the sun's height; 1.9 times there) and on as `1.9 d / L` (its angular width stays the one it has at `L`, so the neighbours at 30 degrees merge as softly as they do there); each beam runs on along its direction to the window's farthest corner at most (its pixels outside the window are not made) and is drawn only above the flood's surface (the sun's layer is clipped by `flood.above`, column by column: none at or below the surface, and the beam ends at it); brightness `0.17 * (0.9 to 1.1) * (0.6 + 0.4 h) * amp` (`BEAM_I` 0.17) at the root, times the fade below; their length, strength and width are the same at every time, and the fan sweeps as one: every beam's angle is its place plus `12 sin(2 pi t / 80)` degrees (`BEAM_SWEEP` 12, `BEAM_SWEEP_T` 80 s, `t` the weather's clock, so it stops with the weather; the even 30 degree spacing is kept, no gap opens) plus its own sway `2.4 sin(2 pi f t + phi)` degrees (`BEAM_SWAY`, `f` 0.05 to 0.1 Hz spread over the beams, phases spread: a neighbour pair's gap changes by at most 4.8 degrees); crossing beams add as `1 - exp(-1.6 v)` and the sum is held to the violet's cap: above `0.45` (`BEAM_KNEE`) it eases as `0.45 + 0.17 (1 - exp(-1.5 (v - 0.45) / 0.17))`, at most `0.62` (`BEAM_CAP`) of the ramp, about rgb(118, 74, 229), so overlaps stay violet; faded over 14 art px (`BEAM_EDGE`; `0.15` of the narrower side if less) at the window's sides (not the bottom: the beams run to it), at the top over the sun's own height (a ramp from 0 at the window's top to 1 at the sun's height, at most 14 px; with the sun within 3 px of the top edge the ramp starts above the window, 14 px wide, and by 3 px down it is the plain one, so the sun on the edge at summer noon leaves no bare top row); held back by up to 55 % where the orange flare is lit; each faded in from 0.6 R to 1.7 R from the sun while the heat's flare is on (with the heat off, the UV alone, they start at the sun's centre, so the glow falls smoothly outward with no dark ring between the ball and the beams), and within 30 px of it a hot root of `0.9 exp(-max(0, r - R) / 4)` of the beams' level read on the ball's pink-white ramp; the whole made again every third frame | ARTISTIC | The widths, the reference reach and the fade are chosen; the sun's real direction does not show in the beams. The sweep's amplitude (under half the spacing) and period are chosen: under 1 degree a second (0.94), so the picture made every third frame moves well under a degree between makes. |

## 3. Wind, rain and the air (Sky)

### 3.1 Common machinery

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Effect levels | `conditionsOf` :5676 | rain: 0 if rate is 0, else `0.3 + 0.7*min(1, rate/8)` (mm/h). haze: the number of AQI edges [50, 100, 150, 200, 300] below the AQI (0 to 5; the tile's own bounds if given; with no AQI, the PM2.5 breakpoints 9, 35.4, 55.4, 125.4, 225.4 against `data.pm25`): the band only picks the baked pattern, the opacity and tint follow PM2.5 (the haze table). uv: `clamp((UVI - 6)/5, 0, 1)`, 0 until the violet shows (about UVI 6.24; full at 11). humidity: 0 when not fogging, else `max(0.05, min(1, (RH - 90)/10))` (so 0.05 while held between 85 and 90 %). wind: 1 if wind is above 15 km/h (the reading, not the gust). heat: 0 below 30 C (`HEAT_FROM`), else `0.3 + 0.7*min(1, (T - 30)/13)` (13 = `HEAT_TO` 43 less `HEAT_FROM`). ice: `-T` if T is below 0 (degrees below zero). flood: the server's bucket depth `data.flood.depth` (0-1) when it is 0.05 or more (`FLOOD_SHOW`), else 0 (no hysteresis; see the flood trigger) | TUNED | The thresholds come from real category names (UVI 11 is WHO "extreme"; 15 km/h is mid-Beaufort 3), but the ramps are linear and chosen by look. Rain saturates at 8 mm/h, so 8 and 30 mm/h look the same. Hysteresis on the humidity (on from 90 %, off below 85 %) is held in the page (`fogging`, :5431), so a reading near the edge does not toggle the effect; the flood has none (the bucket is slow itself). |
| Wind side | `windSideOf` :5456 | direction in (0, 180) gives leftward (-1); in (180, 360) gives rightward (+1); 0, 180, 360 or no reading keeps the last side | REAL-APPROX | Correct sign for a dial with east on the right: wind from the east blows toward the west. Only the side counts; 91 and 179 degrees behave the same. |
| Direction easing | `steer`, `DIR_S` 4 s | `p` slides to its target at rate `2/DIR_S`; `f = sin(p*pi/2)`; the side flips when f crosses 0 and sprites are mirrored | ARTISTIC | |
| Particle budget | `BUDGET` :289, `tick()` | budget = `W*H/400` particles (480 x 300 gives 360); `share = min(1, budget / sum of needs)`; each effect gets `round(need*share)`; humidity has its own limits; rain's count is the plain budget at every slant | TUNED | A performance and legibility cap. Max rain alone: need 360, exactly the budget. |
| Brightness cap | `CAP` :290, `draw()` | `sum = total of strength_k * amount_k` over all effects except rain and the gust; `cap = min(1, 1.5 / sum)`; each effect drawn at `strength*amount*cap`, except the sun's two layers (heat and uv), which count in the sum but are drawn at `strength*amount` (a gust or wind never dims the sun), and rain and the gust, drawn at their own (the gust's cow, barn and chicken are solid sprites, drawn at their own alphas over the sun's layers, which are the bottom of the back-to-front order: heat, uv, gust, haze, rain, wind, humidity, flood, ice, so the beams never show through them). Strengths: rain `0.55 + 0.35*L` (outside the cap), gust 1 (outside the cap), haze 0.44/0.52/0.60/0.70/0.82 by band, heat 0.8, uv 0.8, wind 0.8, flood 0.6, ice `0.36 - 0.12*min(1, extent/(0.6*big))`, humidity 0.8 | TUNED | Looks only. |
| Frame, fade | `FADE_S` 1.5 s, 30 fps cap | linear fade | ARTISTIC | |
| Smoothstep | various | `u*u*(3 - 2*u)` | REAL-MODEL | A standard easing function (the use of it is for looks). |

### 3.2 Rain (`web_dashboard.html` ~474-680; `GUST_SHARE` :339)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Drop size from rate (A5a) | `d0Of`, `rainRate`, `RAIN_NOMINAL` 5, `physics()` in the rain | `R` = the live rain rate (the gauge's, `Sky.setRainRate`; none, as with a test link's rain over a dry gauge: 5 mm/h) clamped 0.5 to 100 mm/h; median drop diameter `D0 = 0.9 * R^0.21` mm (Marshall-Palmer: 0.9 mm at 1 mm/h, 1.46 at 10, 2.05 at 50); the layers are size classes `D = 0.6 * D0` (far), `1.0 * D0` (mid), `1.5 * D0` (near) | REAL-APPROX | The Marshall-Palmer distribution has median volume diameter about `0.9 R^0.21` mm (the code's form: the true constant depends on the form of the median); it is an average over rain types (convective rain has larger drops than stratiform at the same rate). Three classes stand for the whole spectrum, with the 0.6 and 1.5 multipliers chosen (ARTISTIC). Needs the rate: with none it assumes 5 mm/h. |
| Terminal speed | `vtOf` | `v_t(D) = 9.65 - 10.3 * exp(-0.6 D)` m/s, D in mm (the Atlas-Ulbrich fit to the Gunn-Kinzer measurements: 3.65 at 0.9 mm, 5.36 at 1.46, 6.73 at 2.1, 7.95 at 3) | REAL-APPROX | A good fit for 0.5 to 6 mm drops at sea level; ignores air density with height and drop shape, and above about 5.8 m/s large drops break up. |
| Slant law | `slantOf(kmh, k)`; `CAP` 70 deg | per layer `tan(angle) = min(tan 70 deg, v / (v_t * 3.6))`, v in km/h (across per down), v_t of that layer: small drops lean furthest, heavy rain (larger drops, faster) less than drizzle. At 25 km/h and 5 mm/h: far 65.9, mid 55.2, near 47.6 degrees (at 1 mm/h far 72 capped to 70, mid 62, near 54; at 50 mm/h 56, 46, 41). The 11 km/h test link at 5 mm/h: 44.5, 32.4, 25.7. The cap: far from 30.8 km/h, mid from 47.7, near from 62.7 (5 mm/h) | REAL-APPROX | The real law is `tan(angle) = u_wind / v_terminal`, per drop size, as here. It ignores the wind profile with height (the anemometer is a few metres up) and turbulence, and uses three sizes only. There is no real cap: at 100 km/h the true angle is 76 degrees or more. The 70 degree cap also bounds the sprite set (21 tilts, 3.5 degrees apart, `STEPS` 20). |
| Effective speed | `effectiveSpeed` :340 (shared by the rain, the haze and the leaves' pace); `GUST_SHARE` 0.3 (:339) | `v_eff = speed + 0.3 * max(gust - speed, 0)` | TUNED | A mix of mean and gust with no standard. |
| Smoothing | `TAU_UP` 1 s, `TAU_DOWN` 2 s | first-order low-pass: `speed += (goal - speed) * (1 - exp(-dt/tau))`; snaps within 0.02 km/h | TUNED | Hides the 16 s reading jitter. The asymmetry is aesthetic. |
| Fall speed | `PATH` :507, `physics()`, `spawn`, `step` | per layer `v = 230 * clamp(v_t,mid(R) / v_t,mid(5 mm/h), 0.7, 1.3) * v_t,layer / v_t,mid` art px/s (mid at 230 and the others in the v_t ratios at 5 mm/h: about 148, 230, 303; drizzle 0.76x, a downpour 1.3x at most), `* U(0.85, 1.15) * pace`, `pace = 0.8 + 0.4*level`, the same at every slant; across = vertical * tan(angle); path speed = vertical / cos(angle), each layer at its own angle | REAL-APPROX | The kinematics are real: wind pushes a drop sideways without slowing its fall, so the path speed is vertical over cos, and the speeds follow the terminal speeds. The simplification: art px with no physical length (the scale is chosen so the mid layer at 5 mm/h is 230, ARTISTIC; the 0.7 to 1.3 clamp and the 0.8 to 1.2 level pace are chosen), so only the ratios are physical. A drop blown out of the window's downwind side starts again at the top or upwind side, so landings per area are the same at every slant; each layer spawns over its own lean's extent, so the most leaning layer's region covers the width. |
| Streak length | `streak` :535; `STREAK_MOST` 1.6 | length along the path `(n - 1) * PATH * min(1.6, 1/cos(angle))` with n 3 to 4 (far), 5 to 6 (mid), 8 to 10 (near); rows `round(len*cos)`, columns `round(len*tan*cos)` | ARTISTIC | Grows with the path speed, capped at 1.6x the calm length (reached at 51.3 degrees; the path speed itself keeps growing to 2.9x). The calm length is `(n - 1) * PATH` with `PATH = sqrt(1 + 0.45^2) = 1.097`. |
| Drop count | `need` (rain effect) | `need = (W*H/1000) * (0.1 + 2.4 * level^1.5)`; count `round(need * share)` | TUNED | No density factor: the vertical speed is constant, so landings per area (measured over 60 s of weather time: 3870, 3822, 3823, 3695 at 0, 24, 46, 68 degrees) stay within 5 %. The code comment says about 71 drops for the light case on a laptop, which matches (`0.1 + 2.4*0.3^1.5` times 144, 71.2). |
| Sway | step | `slant * (1 + 0.12*sin(0.37 t))`, a 17 s period | ARTISTIC | |
| Wind push on rain | `across` :1416 | extra across-speed `(calm*STEADY*(1 + gust)*k + BANDS*(0.7 + gust)*band) * slant/0.45`, `STEADY` 45, `BANDS` 190 px/s, `k` = the layer's nearness 150/330, 230/330, 1 (`LAYERS[].near`, the same whatever the drop size) | ARTISTIC | Scales with the slant so there is none in calm. |
| Landing, splash | step | per drop, its layer's own lean for the splash drift; splash of 5 frames in 0.24 s, thrown downwind by `min(abs(vx)/vy, tan 70 deg) * 1.4` px | ARTISTIC | |
| Leaf knock | `hard` | `hard = 0.04 + 0.96 * level^2.5`; knock `60 px/s * hard * ratio * U(0.7, 1.3)` | ARTISTIC | |

### 3.3 Wind: the leaf air-velocity field (~1215-1560)

Everything here is drawn in art px. The measured wind speed scales the leaves' pace by `k = clamp(0.4 + 0.024*s, 0.5, 1.6)` (`PACE` :1268, `paceOf` :1269; applied at :1555; `s` the rain's effective speed, eased with `TAU_UP`/`TAU_DOWN`): 25 km/h gives the layer speeds below (k = 1), 15 km/h 0.76, 40 km/h 1.36, 50 and over 1.6, under about 4.2 km/h 0.5. The leaves are only on above 15 km/h, so in practice k is at least 0.76. `k` scales the leaves' speed, the gust bands' carrying speed, the turbulence's drift and swirl, and the eddies' drift; a test link's wind over a calmer real one uses k = 1. The reading also switches the effect on (above 15 km/h), chooses the side, and drives the rain slant and haze pan.

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Leaf speeds | LAYERS :1263 | 40/60/85 art px/s `* k * clamp(W/360, 0.6, 1) * (1 + gust + 0.3*(0.7 + gust)*band) * abs(dir.f)` | ARTISTIC | Their scale `k` follows the reading (TUNED: 0.4 + 0.024 s is chosen by look, with no physical length scale); the rest is not tied to it. |
| Gust pulses | step ~1500 | every `U(2.5, 6)` s add `U(0.5, 1.1)` to g; g decays as `exp(-0.9 t)` | TUNED | A stand-in for gustiness, not driven by the gust reading. |
| Gust bands | `bandAt` :1301 | `0.62*sin(0.024 s X) + 0.38*sin(0.041 s X + 1.7)`, `s = clamp(360/W, 1, 2.6)` (wavelengths 262 and 153 art px at s = 1) | ARTISTIC | No basis in the data. |
| Turbulence | `airAt` :1316 | stream function `psi = A*sin(0.05X + a)*sin(0.07y - 0.7a) + B*sin(0.11X + 1.3 - a)*sin(0.13y - 0.9a)`, `A = 420*(0.7 + 0.6g)*k`, `B = 110*k`, `k = layer speed/60`, `a = 0.6 t`. Velocity `(dpsi/dy, -dpsi/dx)`; curl `0.0074*A*s1*s2 + 0.029*B*s3*s4` | ARTISTIC | The structure is real maths: the field is divergence-free, and `0.0074 = 0.05^2 + 0.07^2`, `0.029 = 0.11^2 + 0.13^2` (checked). The amplitudes and wavelengths are chosen. |
| Eddies | `eddy` :1357, `EDDY_REST` | tangential speed `spin*rho*exp(-rho^2)`, `rho = distance/r`, maximum 0.43*spin at rho 0.71; `spin = +-U(4.5, 6)*r`, `r = U(26, 46)` art px; life `U(5, 9)` s, strength `sin(pi*k)^0.6`; count (slots) `max(4, round(W*H/30000))`; after its life each slot rests `U(5, 9)` s before the next eddy (`EDDY_REST`: a cycle of 14 s on average, life and rest each 7 s on average; half the slots start part way through a rest) | ARTISTIC | A Gaussian-type vortex, not a Lamb-Oseen or Rankine one (1/r far field). Real eddies do not pull leaves in and then fling them out. |
| Leaf relaxation | step | `v += (v_air - v)*(1 - exp(-dt/tau))`, `tau = U(0.12, 0.3)` s; sink `U(2, 10)` px/s; spin `U(4, 9) + min(2.5*abs(curl), 14) + whirl` frames/s | REAL-APPROX | First-order drag relaxation is the standard way a light particle follows air. The sink speed is not physical (a real leaf falls near 1 m/s). Spin rising with curl is qualitatively right. |
| Veer | `setWind` :3984, `leafAir` :1347 | on a side flip the heading turns by about 180 degrees: `angle = from + delta*smoothstep(u)`, duration `VEER_S*abs(delta)/pi`, at least 1 s, `VEER_S` 9 | ARTISTIC | A designed story so leaves do not stop and reverse; real air does not swirl 180 degrees on a wind shift. |

## 4. Haze, heat, UV, ice, flood

### 4.1 Haze (~680-900)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Level | `conditionsOf` | AQI above 50/100/150/200/300 gives levels 1 to 5 | REAL-MODEL | The US AQI band edges. The AQI itself is in section 8.3. |
| Opacity from PM2.5 | `pmOpacity`, `pmShown`, `PM_EXT` 0.01457, `PM_CAP` 0.97, `PM_EDGES` | Beer-Lambert with Koschmieder's view: extinction (1/m) = 4e-6*PM2.5 (4 m2/g mass extinction), L = 3643 m, `opacity = min(0.97, 1 - exp(-0.01457*PM2.5))`: 12 % at 9 ug/m3, 25 % at 20, 40 % at 35, 55 % at 55, 77 % at 100, 89 % at 150, 97 % at 250; none below 8, eased in to 9; each band's image is baked at the opacity of its top edge and drawn at `opacity/opacity(top)` (so at the band's midpoint it is its design look); PM2.5 low-passed with TAU 4 s; the tint is lerped by PM2.5's place between the bands' midpoints (quantised to 1/16, re-baked and cross-faded) | REAL-APPROX | Beer-Lambert is the real law, the 4 m2/g mass extinction is a typical fine-particle figure, and L = 3643 m is an effective distance chosen for the look (a few km of visibility scale). The look at each band's typical value is its design look. |
| Pan speed | `panFor` :766; `PAN_MAX` 3.15, `PAN_K` 22.1 | `pan = 3.15 * (1 - exp(-w / 22.1))` art px/s, `w = speed + 0.3*max(gust - speed, 0)`. Checks: 3 km/h gives 0.40, 40 km/h gives 2.63 | TUNED | Real haze moves at the wind speed (40 km/h is 11 m/s). This is a slow, saturating stand-in that encodes only "faster wind, faster pan". |
| Low-pass | `TAU` 4 s | `filtered += (w - filtered)*min(1, dt/4)` | TUNED | |
| Texture | `bakeTexture` :786 | three layers of value noise (octaves 0.55/0.3/0.15), domain-warped, histogram-equalised per layer, summed with weights 0.6/0.85/1; cut by `comb`: `t = clamp((d/255 - lo)/span, 0, 1)`, shade `7*0.999*smoothstep(t)` | ARTISTIC | Each layer is uniform after equalisation but the sum is bell-shaped, so the cover per band is not the nominal share (section 9). |
| Tint | `lookOf` | grey mix 14 % using `Y = 0.2126R + 0.7152G + 0.0722B` | ARTISTIC | The luma weights are the real ITU-R BT.709 ones (REAL-MODEL), applied to gamma-encoded values as a shortcut. The colours are chosen. |
| Palette | `bake` | 8 shades; colour mix `u^0.85`, alpha `255*open*u^0.8`, `open` 0.7/0.7/0.8/0.9/1 by band | TUNED | |
| Motes | ~878 | count `(W*H/1800)*(0.6 + 0.2*level)` moving at the pan speed | ARTISTIC | |

### 4.2 Heat (`web_dashboard.html`: the sun, `art`, `heat`)

The heat is a blinding white ball of a sun on a clean arc across the window, drawn with the UV's beams in one picture (`art`). `H` and `W` are the window's height and width in art px, `R` the sun's radius, set by the strip above the first row of tiles (below).

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Day fraction | `beamState` | `t = (minutes - rise) / (set - rise)`, from the station's real sunrise and sunset (section 2.2); nothing is drawn for `t` under 0 or over 1, or with the sun's elevation under -1 degree | REAL-MODEL | The sun is drawn only in daylight, from sunrise (`t = 0`) to sunset (`t = 1`). |
| Strip | `bandOf`; `SUN_STRIP0` 0.06, `SUN_STRIP1` 0.16, `SUN_RS` 0.8/6, `SUN_RMIN` 2.6, `SUN_RW0` 0.04, `SUN_RW1` 0.023 | `S = clamp(top - 1, 0.06 H, 0.16 H)`, `top` the first tile's top read from the page (art px); `R = min(max(S 0.8/6, 2.6), 0.04 W, 0.023 W)`, the glow's reach at its fullest breath `Rmax = 1.4 * 6 R * 1.25 * 1.22 = 12.8 R`; `yHi = R + 2`, `yLo = max(yHi, S - 3 R - 1)` (the ball and its pink halo, 3 R out, in the strip at sunrise and sunset) | TUNED | Where the tiles start at the top edge the strip is its minimum and the sun overlaps the first row's top. |
| Arc | `arcOf` ; `SUN_X` 0.42 | `x = W/2 + 0.42 W cos(pi t)`, `y = yLo - yLo min(1, sin(noonEl) / sin(75.6 deg)) sin(pi t)`: from the low point at sunrise and sunset to the high point at noon, the rise scaled by the sine of the day's noon elevation over that of the highest noon of the year (75.6 degrees): at the summer solstice's noon the ball's centre is on the window's top edge (half of it shows), a winter noon stays well inside; the ball is whole at sunrise and sunset | REAL-APPROX | Sunrise at the right (east) and sunset at the left (west), the compass dial's convention: east on the screen's right; higher when the day's noon elevation is higher. A real sun's path is not an ellipse and its disc is half a degree wide: the picture is not to scale. |
| Fade | `BEAM_FADE_H` 2/3, `BEAM_AT` 0.05, `BEAM_CUT` 0.01, `BEAM_P` 2.5, `BEAM_XEND` | the beam's strength along it at the distance `d` from the sun is `f(x) = (g(x) - 0.01) / 0.99` with `g(x) = 1 / (1 + (x / xs)^2.5)`, `x = d / fadeLen`, `fadeLen = (2/3) H (0.6 + 0.4 uvn)` (two thirds of the window's height at UV 11, 0.68 of it at UV 7: the same for every direction, so the fan has no lobe), `xs` chosen so that `f(1) = 0.05` (0.3303); `f` is 0 from `x = xs * 99^(1/2.5)` (2.08: `g` has fallen to 0.01), so a beam's strength under 1 % is not made; 25 % at a third of the window's height and 1.3 % at its height (UV 11, a beam straight down); the root's first 5 % of the reference reach rises from a half to 1 | TUNED | A smooth, monotone, flat-topped curve with a long tail, chosen for the look the user asked for: the beams go to the edge or the water and are barely visible by two thirds of the way across, with no end and no band. Window height, not width, is the length (a window over 1.5 times as wide as high has its sideways beams gone before its far side). |
| Faint end | `TAIL_V`, `TAIL_K`, `TAIL_C`; `toneAt`, `violet` | where the toned strength `v` is under `2 / (10 * 1.1)` (0.182: the violet ramp's first knot, 0.2 of the ramp, 2 of its 10 shades) a pixel is not one of the shades but the colour on the straight line from the page's own up to that knot, `c = (34, 14, 100) v / 0.182` added to `#14191D`, in whole bytes with the blue noise's swing of one byte, `floor(c + th)` (the strong part, `v` of 0.182 and over, keeps its ten shades and `BEAM_GRAIN` 0.6, pixel for pixel as before); every pixel over a thousandth of strength is lit, no pixel is skipped | TUNED | The tail fades out as a smooth tint: at most a byte between neighbours (under 6 % of the tail's pixels differ from their 8 neighbours' mean by over 2 bytes at UV 11; the dithered tail it replaces had over 80 %), no contour (the steps are single bytes, dithered). The cut-off 0.182 is where the shade steps and the line agree. |
| Reach | `BEAM_REF` 0.34, `BEAM_REF_GAP` 3, `SUN_JIT` 1.1 | the reference reach above (it sizes the beams' cross-section only); the glow and ember bloom end well above `0.34 H` (their reach `Rmax` is under it at every sun place) | TUNED | The flares reach at most a third of the way down the window (twice the length they had), at every season, sun place, window size and phone layout. The beams are not held to it. |
| Noon elevation | `noon_el` of `sun_of`; `SUN_NOON_EL` 50 | the sun's elevation at solar noon (the middle of rise and set) at the station's latitude and longitude, degrees; 50 with no location | REAL-MODEL | From S1. |
| Heat 0 to 1 | `heat.level` | `h = 0.3` at 30 C (`HEAT_FROM`) to `1` at 43 C (`HEAT_TO`) (the level is `0.3 + 0.7 (T - 30)/13`); 0.3 where only the UV is on | TUNED | Linear and chosen. Nothing relates it to heat's effect on a person. |
| Ball | `render`; `BALL_RAMP`, 40 shades; `SUN_BALL0` 3.4, `SUN_BALL1` 2.6, `SUN_HALO` 1.4, `SUN_HALO_GAIN` 1.35 | one radial profile `v(r) = pk (1.15 exp(-(r/(0.6 R kS))^2) + 0.62 g exp(-r/(1.4 R kS)) + 0.24 g exp(-r/(2.1 1.4 R kS))) (1 - smoothstep(0.3 Rh, Rh, r))`, `kS = 0.72 + 0.28h`, `g = 1.35` (`SUN_HALO_GAIN`: the pink halo's amplitude), `pk = 0.5 + 1.5 h^2`, `Rh = 1.4 R (3.4 + 2.6h) kH` (`kH` below) (`SUN_HALO` 1.4: the halo 1.4 times as wide, the white core as it was), `v = 0` from `Rh` (no straight edge: the box, `Rh + 3` round the sun, is empty at its own edge); `r` is the distance over `1 + sh (0.025 sin(2 th - 2a) + 0.02 sin(3 th + 3a + 1.3) + 0.02 sin(th + a + 0.7))`, `th` the angle round the sun, `sh = 0.6 + 0.4h`, `a = 2 pi t / 20`; read on the ramp white at the centre through pale yellow-white to a pink-magenta halo | ARTISTIC | A real sun looks like a white-hot ball with a glare round it, and no edge you can see through the glare: that is the real cue. The sizes, the falloff and the colours (larger and whiter at 43 C, smaller and dimmer with a cream centre at 35 C) are chosen by eye. |
| Ember | `render`; `FLARE_RAMP`, 12 shades; `SUN_EMBER` 4 | a dark-orange glow in the halo: `4 (0.3 (1 + 0.35 sh (0.5 sin(2 th - 2a) + 0.5 sin(th + a + 0.7)))) (0.35 + 0.65h) exp(-r0/(2.2 R)) (1 - r0/Rh)^2 smoothstep(1.2 R, 3.2 R, r0)`, `r0` the distance from the centre; read through the ramp at `0.45` of it, to `0.6 + 0.4h` of the ramp and desaturated to the same | ARTISTIC | Heat shimmer: a modest orange cast to the glare, strongest at 43 C, never a ring, never on the ball. |
| Breathing amplitude and radius growth | `render`; `SUN_PR` 0.015 to 0.22, `SUN_PB` 0.03 to 0.30, `SUN_PE` 1.25, `SUN_GROW` 0.25, `SUN_GROWB` 0.12 | with `hq = (h - 0.3)/0.7`: the glare's radius is scaled by `kH = (1 + 0.25 hq)(1 + ar p)` and its brightness by `1 + ab p`, `ar = 0.015 + 0.205 hq`, `ab = 0.03 + 0.27 hq`; the ember's brightness by `1 + 1.25 ab p`, the bloom's radius by `(1 + 0.12 hq)(1 + 1.25 ar p)`; both weighed in by `w = smoothstep(rc + 0.5 R, rc + 2 R, r)` (the profile radius `r / (1 + (kH - 1) w)`, times `1 + (gB - 1) w`), `rc` the radius where the unbreathed profile falls below 1 (the white ball's blown-out edge), so the ball's own radius stays put; the glare and the bloom fade out over `BEAM_EDGE` (14 art px) before the window's sides | TUNED | A hotter day has a brighter, bigger corona that shimmers more: the amplitude and growth rise with the heat, chosen by eye, not measured; at 30 C the breath is barely visible. The beams do not pulse. |
| Breathing periods | `sunPulse`; `SUN_PULSE` [7, 0.5, 0.6], [11, 0.3, 2.9], [17, 0.2, 4.1] | `p = 0.5 sin(2 pi t/7 + 0.6) + 0.3 sin(2 pi t/11 + 2.9) + 0.2 sin(2 pi t/17 + 4.1)`, `t` the weather's clock in s (it stops with the weather), `|p| <= 1`; incommensurate periods, so it surges irregularly | ARTISTIC | A furnace's draught is uneven, not a metronome; the fastest component is 0.14 Hz, under the page's 0.15 Hz bound for the sun (and 0.5 Hz for anything). |
| Ember bloom | `render`; `SUN_BLOOM` 1 | an ellipse `6.5 R` by `5.2 R`: `f^3 (0.7 + 0.3 f) iA bI smoothstep(1.2 R, 3.2 R, r)`, `f = 1 - r/rA`, `bI = 0.62 (0.3 + 0.7h)(0.8 + 0.2 cos(2 pi (t - 4)/20))`, its outline `rA` and brightness `iA` wobbling by slow sines | ARTISTIC | Radiated glow; scaled by the heat, faded to nothing at its outline and in towards the ball. Nothing in the sun moves faster than 0.15 Hz and all of it comes round again every 20 s. |
| Dither and composition | `thr`, `shadesOf` | every shade `floor(v n + threshold)` with the page's blue-noise `dither(x, y)` (no grid); the shades are added to the page's own colour (#14191D, light on dark) and the picture drawn once at the strength; the violet's steps `floor(v n + 0.5 + (threshold - 0.5) * 0.6)` (`BEAM_GRAIN` 0.6: cleaner shafts and valleys); made every third frame (`SUN_EVERY` 0.09 s), the beams' field at every art pixel, the sun's place held to a quarter art px (`SUN_STEP`) so its tables are made again only when it has moved that | TUNED | Cost only: the picture is the same to the eye. |
| Brightness | `SUN_STRENGTH` 1, `CAP` | the heat and the UV count once in the page's cap sum (the UV's strength is 0 while the heat is on) but are never scaled by it; every pixel drawn at most that | TUNED | Looks only. |

### 4.3 UV

| Name | Where | Formula | Label | Faithful? |
|---|---|---|---|---|
| Trigger | `Sky.uvLevel` (called by `conditionsOf`); `UV_FROM` 6, `UV_TO` 11, `UV_SEEN` 0.02 | the effect's level is `(UVI - 6)/5` clamped to 0..1, but 0 until the violet's amount reaches `UV_SEEN` (about UVI 6.24), so the red frame and "stop" come only when there is something to see; the violet's amount `(0.3 + 0.7 uvn) smoothstep(6, 7.5, UVI)`, `uvn = clamp((UVI - 6)/5, 0, 1)`, full at 11; the beams' reach `0.6 + 0.4 uvn` of their longest | TUNED | 11 and above is the WHO "extreme" category; the rise from 6 ("high") is chosen. |
| Beams, lean, glow | section 2.2 | | REAL-APPROX / ARTISTIC | See 2.2. |

### 4.4 Ice and frost (~3240-3600)

With `d` = degrees below 0 C.

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Reach | `reachFor` :3323 | `reach = big*(0.035 + 0.615*min(1, d/5)^1.2 + 0.15*clamp((d - 5)/5, 0, 1))`, `big` = the longer window side. 3.5 % just below 0, 65 % at -5 C, 80 % at -10 C and colder | TUNED | Real frost needs a surface at or below 0 C and below the frost point (dew point 0 C or lower). This uses air temperature only, ignores humidity and dew point, and the reach is a fraction of the window. |
| Growth | `growRate` :3326 | `big*(0.0005 + 0.0005*min(d, 10))` per second (about a minute to the fringe just below 0, 3.6 minutes at -5) | TUNED | "Colder, faster" is the real trend; the numbers are chosen. |
| Melt | `MELT` :3327 | `0.005*big` per second | TUNED | |
| Crystal forms | ~3180-3230 | branches mostly at 60 degrees to the stem; fern, plume, tuft, needles | ARTISTIC | The 60 degrees nods to ice's hexagonal habit; forms and sizes are decorative. |
| Haze depth | ~3188 | `min(0.4*extent, 0.11*min(W, H))` | ARTISTIC | |

### 4.5 Flood, frog, gust objects (~2810-3210, ~3600-3850)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Flood trigger | `flood_of` (lib/web_dashboard.py); `FLOOD_TAU` 30 min, `FLOOD_RUNOFF` 0.5, `FLOOD_FULL` 15 mm, `FLOOD_READ` 6 h, `FLOOD_ROW` 300 s; page `FLOOD_SHOW` 0.05 | depth' = 0.5*R - depth/tau (mm, R in mm/h, tau 30 min), stepped exactly over the past six hours' 5-minute rain-rate rows (`d = d*exp(-dt/tau) + 0.5*R*tau*(1 - exp(-dt/tau))`), the newest row run on to now with the live rate; sent as `depth/15` clamped to 0-1, read at most once a minute. The page shows the flood from 0.05 and sets the water's height to `depth * 0.25*H` (RISE/DRAIN as top speeds); the frog needs depth 0.9 or more | REAL-APPROX | A leaky-bucket (linear reservoir) runoff model: rain falls, half of it runs off into the bucket, which drains with a 30 minute time constant, so a sustained 60 mm/h fills it, 30 mm/h half, a 5 minute 100 mm/h burst about 0.2, and 60 mm/h for an hour then dry is 0.37 of its peak after 30 minutes and 0.05 after 90. The constants (runoff share, tau, full depth) are tuned, not catchment data; real flash floods depend on soil, slope and catchment. There is no rate threshold or hysteresis: the bucket is slow itself. |
| Level | step ~3095; `RISE` 4, `DRAIN` 6 (:2880) | full height `0.25*H`; rises at `full/4` per second, drains at `full/6` per second | ARTISTIC | Far faster than real water. |
| Wave equation | `waves` :3032; `c` 34 px/s | per column `a = c^2*(h[l] + h[r] - 2h[i]) + 2*(v[l] + v[r] - 2v[i]) - 0.1*v[i]`; substeps `ds <= 0.7/c`; surface clamped to +-6 px | REAL-APPROX | A damped 1D wave equation with viscous smoothing is a fair stand-in for surface waves. Real waves are dispersive (`c = sqrt(g*d)` only in shallow water); this is not. |
| Wind drift | `PUSH` 6 px/s (:2882) | first-order upwind advection of h and v at `6 px/s * wind amount` | REAL-APPROX | Real surface drift is a few percent of wind speed; this is a constant. Upwind adds numerical diffusion. |
| Drop hit | `hit` | Gaussian poke of `-power` px, width 1.3; power 0.3/0.6/1.1 by layer | ARTISTIC | |
| Chop | `CALMS` 25 s | chop eases to 1 with a 3 s time constant, back to 0 with 25 s; pokes per second `chop*30*W/300` | TUNED | |
| Frog timing | ~2767-3062 | first frog after 30 s, then exponential with mean 180 s (`-mean*ln(1 - U)`), minimum gap 40 s; speed `U(7, 10)` px/s | ARTISTIC | Whimsy. |
| Gust-object trigger | `GUST_MARK` :5516 | a new gust reading above 99 km/h flings one object | TUNED | 99 km/h is 27.5 m/s, in Beaufort 10 to 11 (89 to 117 km/h). The mark is "about a hundred". |
| Gust-object flight | `gust` step | speed `U(42, 56)` px/s `* (1 + 0.35*wind amount)`; shed bits every 0.15 to 0.45 s, up to 14 | ARTISTIC | |

## 5. Condensation beads (humidity effect, ~1568-2800)

Distances are art px, times are seconds. The dew point is not used anywhere in the humidity effect: the trigger is outdoor relative humidity only.

### 5.1 Trigger and fog

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Trigger and level | `conditionsOf` :5676; `FOG_ON` 90, `FOG_OFF` 85 | on from 90 % RH, kept while it is 85 % or more; level `clamp((RH - 90)/10, 0.05, 1)` (0.05 while held between 85 and 90 %) | TUNED | Real fog on glass needs the glass below the air's dew point. RH of 90 % is a stand-in. The 5 point hysteresis stops flicker. |
| Fog thickness | `thickness` :1863; `THICKEN` 4 s | `0.5 + 0.5*v`, approached at 1/4 per second | ARTISTIC | At v = 0.05 it is 0.525 (the code comment says so). |
| Foggy threshold | `FOG_AT` 0.06 | glass is FOGGY at 0.06 felt fog or more, else DRY; a TRAIL is wet glass less than half re-fogged | TUNED | |
| Form, clear | `FORM` 6 s, `CLEAR` 10 s | cover moves at `dt/FORM` up, `dt/CLEAR` down | TUNED | Real fog on glass takes minutes. |

### 5.2 Water bookkeeping

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Volume, radius | `change()` | `vol = r^3`, `r = cbrt(vol)` | REAL-MODEL | Geometric similarity for drops of one contact angle (a spherical cap's volume scales as `r^3`). |
| Merge | `merge()` | `into.vol += from.vol`; position moves toward the water's centre of mass with `k = from.vol/(into.vol + from.vol)` | REAL-MODEL | Water adds exactly (no volume lost, checked). |
| Gather on fog | ~1946 | `dvol = GATHER*f*2r*px + CONDENSE*k*f*s`; `GATHER` 0.1, `CONDENSE` 0.012, per-bead k 0.3 to 1.5, `f = max(felt fog, 0.25)` | REAL-APPROX | A drop sweeping a path of width 2r collects a film in proportion to its width and distance. The constants are tuned. |
| Leave and evaporate | ~1950 | `dvol = -LEAVE*(rain ? 0.5 : 1)*2r*px - EVAPORATE*r*s`; `LEAVE` 0.1, `EVAPORATE` 0.15 | REAL-APPROX | A running drop leaves water in proportion to its width. Evaporation proportional to r is the diffusion-limited scaling for a drop. It ignores humidity, temperature and wind; the rates are tuned. |
| Gone | `GONE` 0.03 | a bead with less water than this vanishes (r about 0.31) | TUNED | |
| Swell | `SWELL` 0.0007, `SWELLS` [0.6, 1.6], `TINY` 0.61 | `dvol = SWELL*swell*f*s` for tiny beads | TUNED | From r 0.55 to `TINY^3 = 0.227` takes about 86 s at f = 1 and about 215 s at f = 0.4. Not a real condensation rate. |
| Condense | `condense()` | new tiny beads per second `0.02*W*(0.95 + 0.4*v)`; r from [0.5, 0.6]; `y = 6 + (0.82H - 6)*u^1.3` | TUNED | Real condensation depends on glass temperature and dew point. |
| Budget | `need()` | beads at most `0.45*(1 + 0.38*v)*W` plus 14 in heavy rain; a tiny bead counts `TINY_COST` 0.25 | ARTISTIC | A performance cap. |
| Trail life | `LIFE` [180, 600] s | wiped clear for the first `CRISP` 0.85 of it, then re-fogs | TUNED | |

### 5.3 Bead motion

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Speed law | `sized` :2235; `SPEED` 13 | `v(r, g) = 13 * (x <= 1 ? x^5 : x^3) * PACE[g] * run factor * tilt steepness`, `x = r/2.8` | TUNED | Real drops do not move below a pinning threshold, and above it speed grows with size (gravity pull scales with volume, drag with r). The `x^5` and `x^3` branches are a plausible shape, not fitted to data. They meet at x = 1 with a slope kink. The `x^3` branch is linear in water volume; below r = 2.8 the law is steeper than volume. |
| Pace | `PACE` :1841 | [dry 0.3, trail 1.6, fog 1] | TUNED | Drops run faster on a wetted track, so the order is right; the ratios are chosen. |
| Speed table | comment ~1843 | r 0.87 gives 0.04 px/s, r 1 gives 0.08, r 1.4 gives 0.4, r 2 gives 2.4, r 2.8 gives 13, r 4 gives 38 (on fog) | TUNED | Computed from the law: 0.038, 0.076, 0.41, 2.4, 13, 37.9, matching the comment. |
| Pinning | `RUN_R` :1858 | with no tilt sensor, no motion below r = 1.4 (`1.4^3 = 2.74`, about sixteen tiny beads); with one, the threshold is `r_crit` (the next row), which is 1.4 on an upright device | REAL-APPROX | Stands in for contact-angle pinning; real pinning depends on drop size, tilt and surface. |
| Pinning threshold by size and tilt | `critOf` :1867, `critR`, `grav.sin` | gravity along the glass pulls with the drop's volume (`r^3`) and surface tension pins it with its width (`r`), so the critical steepness is `sin(alpha_crit) ~ 1/r^2`; with the constant set by `RUN_R` 1.4 at 90 degrees (`s = 1`) a bead runs only when `r >= r_crit(s) = RUN_R / sqrt(s)`, `s = sin(theta)` the smoothed in-plane share of gravity (`max(s, 0.05)`): `r_crit` is 1.40 at 90 degrees, 1.98 at 30 (`s = 0.5`), 3.36 at 10 (`s = 0.174`); under the flat dead zone (pooled) nothing runs. A bead under `r_crit` neither runs nor leaves a trail, and still merges, swells and snaps; a pooled bead's pull is `TENSION`, whatever `r_crit` is | REAL-APPROX | The `r^3` over `r` balance is the standard drop-on-incline estimate (critical Bond number); real hysteresis also depends on the contact angles, the glass and the dirt on it, which one constant stands in for. Tilt with no direction change (the wind's push) does not move the threshold: it uses the steepness only. |
| Lean and braking | `LEAN_FROM` 0.6, `LEAN_PX` 1, `LEAN_STRETCH` 0.15, `LEAN_R` 2, `LEAN_S` 0.15 s, `BRAKE_S` 0.3 s | a held bead (`r < r_crit`, a sensor reading present, not pooled) with `u = r / r_crit` from 0.6 leans toward downhill by `lambda = smoothstep((u - 0.6)/0.4)`: its sprite shifts `lambda` px (rounded to whole px when drawn) and, from r 2, is `0.15 * lambda` longer along its way (whole rows, at least one from 5 %), `lambda` rising over 0.15 s and dropping to 0 at once; at `u >= 1` it runs and, from r 2.5, sets off with the restart stretch from where the lean was. A running bead that falls under `r_crit` brakes linearly to a stop over 0.3 s | ARTISTIC | A drop does bulge toward its downhill edge before it lets go; the amounts are chosen for the look. |
| Track floor | `TRACK` :2236 | `sized(3, trail)`, about 25.6 px/s | TUNED | |
| Pooled pull | `gather()` | when pooled, a bead within 4 px edge gap of a larger or tiny bead drifts at 0.1 px/s until the gap is 0.25 px, then merges | TUNED | 0.1 is below the slowest running speed 0.122 (checked). |
| Snap | `snaps()`; `SNAP` 1 px | two beads within 1 px edge gap: the higher is pulled to the lower at 6 to 30 px/s (acceleration 120 px/s^2) and merged | ARTISTIC | Stands in for the capillary jump. |
| Pop, gulp, stretch | `POP` 0.12 s, `GULP_R` 3.9, `STRETCH` 0.14 | bulges and stretches of a bead | ARTISTIC | |
| Lock-on, runs, pauses | `LOCK_GAP` 5, `PAUSES`, `STEADY_R` 3.6, `MEANDER` 7 | stop-start, wandering, following a nearby trail | ARTISTIC | Real drops do stop, start and follow wet tracks; the rules are random behaviour. |

### 5.4 Heavy-rain beads

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Heavy gate | `HEAVY_RAIN` :1895 | 0.75 of the rain level, which is `8*(0.75 - 0.3)/0.7 = 5.14` mm/h | TUNED | The code comment says about 5 mm/h, which matches; the threshold itself has no physical meaning. |
| Landing | `land()` | `LANDING` 0.35 per second, at most `RAIN_MOST` 14 on the glass; bead radius `r = 2.6 + 0.37 * clamp((D0 - 0.9)/1.3, 0, 1)` (`rainR`, from the rain rate as above: 2.6 px in drizzle to 2.97 from D0 2.2 mm, about 70 mm/h; 2.70 at 5 mm/h, 2.82 at 20) | ARTISTIC | Not a real sticking rate; the growth of a bead with the drop is the real direction, the size range chosen. |
| Splash | ~2010 | `THROWN` [14, 20], `THROW` [24, 40] px/s, `FALL` 80 px/s^2 (:1734), `SPRAY` 0.25, `SPLASH_S` 0.25 s, `SQUASH` [0.5, 0.75] over 0.2 s | ARTISTIC | `FALL` 80 is not 9.8 m/s^2 at any scale. |
| Droplet drying | `DRYING` [15, 40] s | | TUNED | |

## 6. Device tilt and wind push (`screenGravity`, `tilt`, `aim`)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Gravity on screen | `screenGravity` | non-iOS (`u = -ax`, `v = ay`): angle 0 gives `(u, v)`, 90 gives `(v, -u)`, 180 gives `(-u, -v)`, 270 gives `(-v, u)`; other angles by `x = u*cos(a) + v*sin(a)`, `y = -u*sin(a) + v*cos(a)`. iOS: `u = ax`, `v = -ay` before the same turn | REAL-MODEL | The direction of gravity from `accelerationIncludingGravity`. Checked against the code's own tables for all four angles on both platforms. |
| iOS sign | `screenGravity` | iOS reports the pull itself; Android reports the support force (minus gravity); the sign is flipped once | REAL-MODEL | Consistent with the flat-reading example (z = -9.56 on iOS, face up). |
| Steepness | `tilt()` | `s = sqrt(gx^2 + gy^2) / sqrt(gx^2 + gy^2 + gz^2) = sin(theta)`; theta is the screen normal's angle from vertical (0 lying face up, 90 upright, 180 face down) | REAL-MODEL | The in-plane share of gravity: the sliding force factor `g*sin(theta)`; independent of the reading's size. The same `s`, low-passed with `TAU` (the first reading at once), is `grav.sin`, the steepness of the pinning threshold. |
| Response curve | `POOL_IN`, `POOL_OUT`, `STEEP_FULL` :4100 | `POOL_IN = sin 5 deg = 0.0872`, `POOL_OUT = sin 7 deg = 0.1219`, `STEEP_FULL = sin 40 deg = 0.6428`; `t = clamp((s - 0.0872)/(0.6428 - 0.0872), 0, 1)`; `steep = smoothstep(t)`, low-passed with `k = 1 - exp(-dt/0.25)` | TUNED | The smoothstep is applied to `sin(theta)`, not theta: the ramp midpoint is s = 0.365 (theta 21.4 degrees). The real driving force keeps rising to 90 degrees; saturating at 40 is a choice. The 5/7 degree dead zone stands in for pinning and prevents flicker. |
| Smoothing | `SNAP_DEG` 4, `STEP_DEG` 1.5, `TAU` 0.25 s | low-pass of the direction; snaps to straight down within 4 degrees; changes only when it moves 1.5 degrees or more | TUNED | Noise suppression. |
| Wind push | `pushOf`, `aim()`, `blow()`; `PUSH_ON` 15, `PUSH_FULL` 45, `PUSH_MAX` 0.6 | with the eased effective wind speed s (km/h: the speed plus 0.3 of the gust's excess, low-passed as the rain's is) the push ratio is `p = 0.6 * smoothstep((s - 15) / 30)`: 0 below 15 km/h, 0.3 at 30, 0.6 from 45. The beads' run direction is `normalize(g + p * f * steep * (1, 0))`, with `g` the unit gravity (straight down, or the tilt's), `f = dir.f` (-1 leftward to +1 rightward, eased through 0 as the wind turns) and `steep` the steepness factor: at most `atan(0.6)` = 31.0 degrees off vertical, downwind. A direction within 4 degrees of straight down is straight down; it changes only by 1.5 degrees or more at a time. Speeds are the speed law's; a pooled device has no push | TUNED | Wind on a window drags drops downwind; how much, and from which speed, is chosen by look. The ratio is a force ratio (wind drag against gravity) in spirit only: no air density or drop size enters. |
| Face down | `tilt()` | steepness depends on `sin(theta)` only, so 180 degrees pools like 0 | REAL-APPROX | Real drops hang and fall from the underside. Rare, and documented in the code comment. |

Bead motion along the run unit vector (`b.x += grav.x*dy`, `b.y += grav.y*dy`: gravity, with the wind's push added) is REAL-MODEL for direction; wandering across the path is ARTISTIC.

## 7. Gauges, units and layout

### 7.1 Unit conversions (web_dashboard.html `UNITS` :4756)

The server always sends metric. The page converts the reading, scale, ticks, table figures, floor and the red hand. Sky thresholds (30 C, 15 km/h) read the metric figures and do not move with the unit switch.

| Name | Formula | Label | Faithful? |
|---|---|---|---|
| Celsius to Fahrenheit | `F = C*9/5 + 32` | REAL-MODEL | Exact. |
| km/h to mph | `mph = kmh / 1.609344` | REAL-MODEL | Exact. The wind tile shows whole mph. |
| hPa to inHg | `inHg = hPa / 33.8639` | REAL-MODEL | The exact figure is 33.86389; relative error below 3e-7. |
| mm to inches | `in = mm / 25.4` (rain, rain rate in/h) | REAL-MODEL | Exact; shown to 2 decimals (0.01 in is 0.254 mm). |
| Scale re-rounding | `inUnits()` :4763. Tick step: the first of `STEPS` [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000] with `floor((high - low)/s + 1e-9) + 1 <= most`. For dials and units flagged `round`: `low = floor(low/step)*step`, `high = max(low + step, round(high/step)*step)`. The thermometer is not rounded | TUNED | Wind 0 to 100 km/h becomes 0 to 60 mph (96.6 km/h), pressure 950 to 1050 hPa becomes 28 to 31 inHg (948.2 to 1049.8 hPa), rain 0 to 100 mm becomes 0 to 4 in (101.6 mm), rain rate 0 to 50 mm/h becomes 0 to 2 in/h (50.8 mm/h). The needle still matches the reading because `share()` uses the new ends. |

The server's own factors (`lib/ecowitt/hass_map.py` ~66-71) are listed in 8.2 and match these.

### 7.2 Needle, thermometer, beaker, scale ranges

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Linear share | `lib/gauge_look.py share()`, html `share` :4241 | `share = clamp((v - low)/(high - low), 0, 1)` | REAL-MODEL | A linear mapping. |
| Banded share (air gauges) | same | with bounds `b0..bn`, for `b_i < v <= b_(i+1)`: `share = (i + (v - b_i)/(b_(i+1) - b_i))/n` | TUNED | Equal-width bands are a layout choice (as AirNow does it). |
| Needle angle | `START` 215, `SWEEP` 250 | `theta = 215 - 250*share` degrees (0 = right, 90 = up); the dial spans 215 to -35 degrees, a 110 degree gap at the bottom | TUNED | Each colour band is `SWEEP/colours` wide minus 0.8 degree each side (ARTISTIC). |
| Thermometer level | ~4170-4225 | `y = 95 + (1 - share)*(238 - 95)` | REAL-MODEL | Same mapping as the scale bar, so ticks and mercury agree. At 0 C the mercury is at y 238, bulb and about 16 px of tube already filled. |
| Beaker level | ~4170-4225 | `y = 292 - max(share, 0.02)*(292 - 10 - 90)`, none at `v <= 0` | TUNED | Any rain above 0 shows at least 2 % (about 3.8 px), so a trace looks like about 2 mm. |
| Rest position | `floor()` | no reading rests at 0, or the scale's low (pressure 950), dimmed to `FADE` 0.4 | ARTISTIC | |
| Thermometer scale | `lib/ecowitt/dashboard.py scale()` | 0 to 40 C widened by 10 C: `low = min(0, floor(v/10)*10)`, `high = max(40, ceil(v/10)*10)`; band colours by `degree_colour()` | TUNED | A reading of 60 C makes a tall scale. Dew point, feels like and indoor share the 0 to 40 start. |
| Gauge ranges | `GAUGES` ecowitt/dashboard.py:67-91 | humidity 0 to 100; pressure 950 to 1050 hPa; wind 0 to 100 km/h; rain 0 to 100 mm; rain rate 0 to 50 mm/h; solar 0 to 1500 W/m^2; UV 0 to 12 with ticks 0, 3, 6, 8, 11 (the colours: the next row) | TUNED | Display ranges, not physical limits (wind above 100 and UV above 12 pin at the end; full sun can reach about 1100 to 1400 W/m^2). |
| UV bands | `UV_SCALE`, `UV` ecowitt/dashboard.py | twelve 1-UV slices of the 0 to 12 dial, each band one solid swatch of the official UV index scale (WHO categories; US EPA / AirNow colours): Low 0-2 `#299501`, Moderate 3-5 `#F7E401`, High 6-7 `#F95901`, Very high 8-10 `#D90011`, Extreme 11+ `#6C49C9`; the edges 3, 6, 8, 11 are the ticks; the page and the Status picture's gauge sheet read the same tuple | REAL-MODEL | The official bands are on the rounded index, so on a continuous dial the edges sit at the whole numbers. The Extreme violet is near the beams' violet (about #764AE5 at their cap) but independent of it. |

### 7.3 Table statistics and pressure arrow

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Max, Min, Gust | `figures()`, `today_highs()` | maximum of the daily maxima over the row's days, plus the live reading; minimum likewise; Gust is the maximum of daily gust maxima plus the live gust | REAL-MODEL | Plain statistics; as good as the day rows. A brief spike between readings can be missed. |
| Rain total, Max | `figures()` | total `round(sum of daily rain, 1)`; Max is the wettest day (rows covering more than one day) | REAL-MODEL | |
| Rows | | Daily is today; Monthly is the current month to date; Yearly is 1 January to date | REAL-MODEL | |
| Limit mark | `dial()` | the red triangle sits at the angle of today's high (wind uses its gust), only on wind, rain rate, solar, UV | REAL-MODEL | Position is the same number as the table's Daily figure; shape is ARTISTIC. |
| Pressure tendency | `tendency_of()` web_dashboard.py; `TENDENCY_AGO` (150, 180, 210) min, `TENDENCY_READ` 240 min, `TENDENCY_STEADY` 0.1, `TENDENCY_CLASSES` | `change = round(newest - reading nearest 180 min before it, 1)` (among readings 150 to 210 min back; none otherwise); class by `abs(change)` to a tenth, the WMO 3-hour tendency scale: under 0.1 none (steady, no arrow), 0.1 to 1.5 slow, 1.6 to 3.5 moderate, 3.6 to 6.0 quick, over 6.0 rapid; JSON `{change, way: up/down/steady, class}` | REAL-MODEL | The meteorological 3-hour change, the daily pressure tide mostly gone. The arrow group (page): length by class 0.5 / 1 / 1.35 times the normal (0.8 of the digit height), rapid a double arrow (a second chevron 5.5 px behind the first at the normal length), 2 px after the last digit's ink, `lw(1.2)`, blue up (#3E86C0), red down (#FF3B30) (ARTISTIC). No arrow with no reading 150 to 210 minutes old. Change stays in hPa whatever unit is shown. |

### 7.4 Compass and wind rose

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Sector | `lib/ecowitt/direction.py sector()` | `int(((deg mod 360) + 11.25) // 22.5) mod 16`; N is sector 0 (348.75 to 11.25) | REAL-MODEL | 16 points of 22.5 degrees. |
| Needle | `compass()` :4447 | rotates to the reading (degrees clockwise from north, where the wind comes from), eased the short way: `from + (((to - from) mod 360) + 540) mod 360 - 180` | REAL-MODEL | |
| Rose counts | `directions_on()` ecowitt/reads.py:69, `rose()` | per sector, minutes of wind from it: each 5-minute reading counts 5; each 1-minute reading counts 1 only if no 5-minute reading is within 300 s; calm (speed 0 or less) is left out; a reading with no speed counts | REAL-APPROX | Time-weighted, not speed-weighted: a long light breeze equals a short gale. A single 5-minute sample counts 5 minutes. |
| Rose share | `rose()` web_dashboard.py:319, `ROSE_DECIMALS` 4 (web_dashboard.py:97) | `share_i = count_i / total`, rounded to 4 decimals; empty if total is 0 | REAL-MODEL | |
| Most common direction (the Wind direction tile's table) | `commonest()` direction.py, `common()` web_dashboard.py, `wind_readings_on()` ecowitt/reads.py; `CALM_BELOW` 1 km/h, `COMMON_SECONDS` 120 | per period (since the station's local midnight, the first of its month, the first of its year), each stored reading (5-minute ones, and minutes no 5-minute reading is within 300 s of) counts 1 in `sector(deg)`; a reading under 1 km/h is left out (no speed counts); the most counted point wins; a tie goes to the tied point with the smallest circular distance `min((i - j) mod 16, (j - i) mod 16)` to the newest reading's point `j`, then to the lowest index (first clockwise from N); none left gives a dash | REAL-APPROX | A count of readings, not of time or speed: a 5-minute reading and a 1-minute one count alike, and a light wind counts as much as a gale. The 1 km/h calm limit is a convention, not a station property. |
| Petal length | `ROSE_REACH` 0.8, `ROSE_HALF` 9 deg (web_dashboard.html:4440) | `out_i = 0.8*138*share_i/max(share)`; wedge half-width 9 degrees (18 degrees in a 22.5 degree sector) | TUNED | The longest petal is always 80 % of the radius: relative shape only, no absolute percentage scale. |

### 7.5 Layout, canvas, staleness, appearance

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Tile aspect | `CELL_W` 480, `CELL_H` 575 | `aspect = 480/575 = 0.8348` | TUNED | |
| Fit | `layout()` :4648; `PAD` 8, `STATUS` 18 | `W = clientWidth - 2*PAD`, `H = clientHeight - 2*PAD - STATUS`; for cols 1..n: `rows = ceil(n/cols)`, `w = min(W/cols, H/rows*aspect)`; the best w wins | TUNED | A simple optimisation. |
| Scroll fallback | `MIN_TILE` 160, `MIN_SCROLL_TILE` 150 | if best w is below 160: `cols = max(1, min(n, floor(W/150)))`, `w = W/cols`, page scrolls | TUNED | |
| Gallery | `galleryLayout()` :5238; `TABLE_TOP` 372 | zoomed aspect `480/372 = 1.29`; `w = min(W - 2*PAD, H*aspect)`; slot pitch W; flick if speed is above 0.4 px/ms and the drag is at least 24 px; settle to `round(position/pitch)`; duration distance*450 ms clamped to 150 to 320 ms; rubber band `(1 - 1/(over*0.55/pitch + 1))*pitch`; bounce 40 px over 300 ms; zoom 200 ms | TUNED | Feel constants. |
| Art pixel | `ART_PX` 480, :3951 | `P = max(3, round(max(w, h)/480))`; canvas `ceil(w/P)` by `ceil(h/P)`. 1920 x 1080 gives P = 4, 480 x 270 art px | TUNED | |
| Overdraw margin | `OVER` 100 :295 | `M = ceil(100/P)` rows above and below, filled by mirroring | ARTISTIC | |
| Stale reading | `STALE_MS` 3600000 :4879 | stale if time is missing or `now - time` is not at most one hour | TUNED | Only the status line greys; gauges are not dimmed for old data. |
| Needle motion | `MOVE_MS` 900, `RISE_MS` 1000 | ease `1 - (1 - p)^3`; `REFETCH_MS` 60 s | TUNED | |
| Geometry | thermometer (top 95, bottom 238, cx 285, bulb radius 38), beaker (top 90, bottom 292), dial radii 130/138/102/84, hub 13, table chips, petal gradients | | ARTISTIC | Appearance only. |

## 8. Server data and derived readings

### 8.1 Dashboard server (`lib/web_dashboard.py`)

| Name | Where | Formula | Label | Faithful? |
|---|---|---|---|---|
| Sun for the beams | `sun_of` :123 | `sun_position(lat, lon, now)` (section 2.1), rounded to 3 decimals; location from the forecast, else the station's; `noon_el` is the elevation at solar noon (the middle of sunrise and sunset), rounded to 2 decimals, absent in polar night and with no location | REAL-MODEL | |
| Sunrise and sunset | `sun_of` | `solar.sun_times` for the station's location and zone (section 2.2); fallback 360 and 1080 with no location | REAL-MODEL, fallback TUNED | |

### 8.2 Derived readings and outlook

The station supplies dew point, feels like, `app_temp`, `app_tempin` and VPD (`lib/ecowitt/query.py:46`, `DERIVED`). The repo does not compute them, and has no heat index, wind chill or Steadman formula.

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Dew point (Magnus) | `lib/service/outlook.py:159`; `a` 17.62, `b` 243.12 C | `g = ln(RH/100) + a*T/(b + T)`, `Td = b*g/(a - g)`; RH clamped to at least 1 % | REAL-MODEL | Used only when the station sends no dew point. Good to about 0.4 C over a wide range; poor at very low RH ((35 C, 10 %) gives -1.17 C). Checks: (20 C, 50 %) gives 9.26 C; (30 C, 80 %) gives 26.17; (0 C, 100 %) gives 0.0. |
| Unit conversions | `lib/ecowitt/hass_map.py:66-71` | pressure to hPa: kPa x10, Pa x0.01, inHg x33.8639, mmHg x1.33322, psi x68.9476; speed to km/h: mph x1.609344, m/s x3.6, kn x1.852, ft/s x1.09728; length to mm: cm x10, in x25.4; rate in/h x25.4; temperature: F gives `(T - 32)*5/9`, K gives `T - 273.15`; VPD hPa to kPa x0.1 (`UNIT_FIXES` api.py:82 converts inHg VPD at x3.38639) | REAL-MODEL | Exact factors. |
| Outlook rain verdict | `lib/service/outlook.py:36-45, 136-309` | score: falling pressure 1 to 3 hPa adds 1, over 3 hPa adds 2; moist (RH 90 % or more, or `T - dew point <= 2 C`) adds 1; unsettled (under 1005 hPa) adds 1; wind picking up 5 km/h or more while falling adds 1; settled (over 1020 hPa) subtracts 1; clamp 0 to 2 maps to unlikely, possible, likely; any rain in the last hour forces likely. The pressure change is `last - first` over the 3 hours (`_pressure` :136, rounded to 0.1): falling if at most -1.0 hPa (`STEADY_HPA`), rising if at least +1.0, else steady | TUNED | Falling pressure with moist air does precede rain, but the cut-offs are not calibrated. A fall that recovers by the end reads steady; a rise then fall still reads by its end points, so a swing inside the window is not seen. |
| Emoji triggers | `lib/ecowitt/glance.py:1-18` | hot 35/30/25 C, cold 8/0 C, wind 15/30/50 km/h, muggy 85 %, dry 30 %, pressure 1025 hPa, VPD 1.2 kPa, solar 600 and 200 W/m^2, UV 6 and 10 | TUNED | |

### 8.3 Air quality (`lib/airgradient/*`, `lib/series.py`)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| AQI interpolation | `_aqi` metrics.py:106, `truncate` :113 | `I = (I_hi - I_lo)/(C_hi - C_lo)*(C - C_lo) + I_lo`, rounded to the nearest integer (the EPA rounds the index); the concentration is first truncated (not rounded) to the table's resolution, 0.1 for PM2.5 and 1 for PM10 (`truncate`, with a 1e-9 guard against float error); above the top breakpoint returns 500 | REAL-MODEL | The EPA linear-interpolation method, with the EPA's truncation: 9.05 gives 9.0 (AQI 50), 35.45 gives 35.4 (AQI 100). |
| PM2.5 table | `pm25_aqi` :119 | 0.0 to 9.0 gives AQI 0 to 50; 9.1 to 35.4 gives 51 to 100; 35.5 to 55.4 gives 101 to 150; 55.5 to 125.4 gives 151 to 200; 125.5 to 225.4 gives 201 to 300; 225.5 to 325.4 gives 301 to 500 | REAL-MODEL | The EPA's 2024 breakpoints (checked row by row). |
| PM10 table | metrics.py:77-85 | 0 to 54, 55 to 154, 155 to 254, 255 to 354, 355 to 424, 425 to 604 give the same AQI bands; concentration truncated to a whole number | REAL-MODEL | Matches the EPA table. |
| Combined AQI | `us_aqi` :134 | the higher of the PM2.5 and PM10 sub-indices; PM2.5 wins ties; the source is named | REAL-MODEL | The AirNow rule. |
| Applied to the latest reading | | the formulas run on the latest sensor reading, not a 24-hour average or NowCast | REAL-APPROX | It reads "the AQI if this level persisted". It differs from AirNow when PM2.5 is varying (the air-gauge header says so). |
| Gauge bounds | `lib/airgradient/dashboard.py:36-80, 85-89` | AQI 0, 50, 100, 150, 200, 300, 500; PM2.5 0, 9.0, 35.4, 55.4, 125.4, 225.4, 325.4; PM10 0, 54, 154, 254, 354, 424, 604 | REAL-MODEL | EPA edges. |
| PM1 bands | same | the PM2.5 bounds | TUNED | No standard exists for PM1; PM1 is part of PM2.5, so the scale is lenient. |
| CO2, VOC, NOx bands | same; `series.py:55-64` | CO2 400, 800, 1000, 1500, 2000, 2500 ppm (ratings 799/1499); VOC index 1, 100, 150, 250, 400, 500 (ratings 150/250); NOx index 1, 20, 150, 300, 500 (ratings 20/150) | TUNED | CO2 cut-offs are a common indoor rule of thumb (the code comment attributes them to ASHRAE, which sets no such bands). VOC and NOx are Sensirion relative indices (VOC 100 and NOx 1 are the baselines); the other edges are the service's own. |
| Ratings | `series.py:55-64`, `zone()` | good if at most the first limit, poor if at most the second, very poor above. PM2.5 9.0 and 55.4; PM10 54 and 254 | REAL-MODEL for PM2.5 and PM10 edges, TUNED for the rest | |
| Spike removal | `without_spikes` metrics.py:45; `SPIKE_RATIO` 2.5, `SPIKE_RISE` 5 ug/m^3, `REQUIRE_CONSECUTIVE` 2 | a stretch shorter than 2 readings of PM1, PM2.5 or PM10 is dropped if every reading is at least 2.5 times and at least 5 above the higher neighbour (neighbours within 900 s) | TUNED | Targets sensor glitches, but can drop a real short puff of smoke. |
| Gauge band names | `AirGauge.seen()` :65, `band()` :77 | the particles' concentration (PM2.5, PM10, PM1, not the AQI) is truncated to the step (0.1, 1, 0.1) by `seen()`; the needle and the band name both use that value; a value is in the band below an edge if `seen < edge + step`; step 1 for AQI and NOx, 0 for CO2 and VOC | REAL-APPROX | Mirrors the EPA's truncation to table resolution, so needle and band agree. |

### 8.4 Stored readings and aggregation

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Interval mean | `lines._summarise` lines.py:94 | one value per 30-minute slot (finer readings win), then the mean of slot means | REAL-MODEL | Time-weighted, not weighted by reading count. |
| Interval low, high | history.py / storage | minimum of the readings' own lows (`COALESCE(_lo, value)`), maximum of their highs | REAL-MODEL | |
| Day mean, period mean | `storage/days.py:8`, `lines.time_mean`, `readings_mean` | mean of the day's half-hour means; period mean weights days by seconds covered | REAL-MODEL | |
| Gust "max", direction "last" | history.py:215 | maximum of the highs; the latest reading, with no averaging | REAL-MODEL | |
| Rain from the counter | `rain_slots` lib/rain.py:34, `_is_reset` :25; `MAX_GAP` 3 slots (90 min), `RESET_MM` 2.0, `RESET_SHARE` 0.1, `RESET_HOURS` 2 | rain per slot is the rise of the running daily counter; a drop is the midnight reset (adds the whole new value) only if the new total is under 2 mm and either the reading is within 2 hours after local midnight (when the zone is given) or the new total is at most 10 % of the total before; any other drop is a glitch: it adds 0, the total before the drop stays the baseline, and it is counted (`glitches`); a hole longer than 90 minutes counts 0 | REAL-APPROX | A mid-day re-sync adds nothing. A hole over 90 minutes loses real rain. A real reset the rule rejects (new total 2 mm or more, or not near midnight and above 10 %) is ignored, and because the baseline stays at the total before the reset, the new day's rain is lost until the counter passes it (section 9). |
| Rain events | `rain_spells` lib/rain.py:59 | slots join one spell if the gap is 6 slots (3 dry hours) or less; a spell needs at least 1.0 mm | TUNED | |
| 5-minute band | storage/write.py:28 | exists only if `MIN_READINGS` 2 live readings fell in the slot | TUNED | |
| Line smoothing | `SMOOTH_POINTS` 3 | 3-point rolling mean (15 minutes of 5-minute readings), for temperature, humidity, dew point, feels like and pressure only | TUNED | Never for wind or rain. |
| Row caps | `MINUTE_HOURS` 8, `INTRADAY_DAYS` 8, `FINE_DAYS` 31, `MAX_ROWS` 300 | | TUNED | Limits, not maths. |

### 8.5 Forecast, alerts, day look

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Rain chance | `lib/forecast/source.py` `_ensemble` :131; `MIN_HOURS` 20 | Open-Meteo ECMWF IFS 0.25 degree ensemble, 51 members. Per member the hourly precipitation is summed per local day (needs 20 hours). Chance `= 100*(members with at least 1 mm)/members`, rounded to the nearest 5 % | REAL-APPROX | A raw ensemble fraction, not calibrated: "40 %" means 20 of 51 members reach 1 mm. If the ensemble fails, the fallback is Open-Meteo's `precipitation_probability_max`, a different statistic (the label changes to "chance of rain"). |
| Temperatures | `whole()` source.py:37 | rounded half up (10.5 gives 11) | REAL-MODEL | |
| Pollen, asthma | `lib/pollen/*` | levels (Low, Moderate, High, Extreme) are the site's words, scraped; `SEASON_MONTHS` (10, 11, 12) | none | Not computed: the site's official forecasts, relayed. |
| Forecast alerts | `lib/alerts/forecast.py` `changes()` :34 | rain becomes likely if the chance rose from under 50 % to 50 % or more; unlikely if it fell from 50 % or more to 30 % or less; the high moves by more than 2 C; baseline is the first forecast after 06:00 | TUNED | The 30 to 50 % band stops flapping. |
| Day look | `lib/service/daylook.py:27-90` | rule order: stormy (rain 10 mm and gust 60 km/h or more), wet (rain 5 mm or more), showery (1 mm or more), windy (gust 50 or more), very hot (high 35 C or more), very cold (high under 10 C or low 0 C or less), then sunniness. Sky share = `solar_avg / clear_sky_mean`: sunny at 0.80 or more, mostly sunny 0.60, partly cloudy 0.35, else overcast | TUNED | |
| Clear-sky declination | daylook.py:54 | Cooper (1969): `d = 23.45 deg * sin(2*pi*(284 + doy)/365)` | REAL-APPROX | About 0.5 degree error, less accurate than the NOAA series in 2.1. |
| Clear-sky irradiance | daylook.py:47-61; `HAURWITZ_W` 1098, `HAURWITZ_K` 0.057 | Haurwitz (1945): `GHI = 1098*cos(z)*exp(-0.057/cos(z))` W/m^2; `cos(z) = sin(lat)*sin(d) + cos(lat)*cos(d)*cos(H)`; averaged over 288 five-minute steps | REAL-APPROX | Clean air at sea level; no Earth-Sun distance correction (about +-3.4 % over the year), no altitude, aerosols or humidity. Meaningful only for whole-day means. Values: Melbourne 21 Dec 367 W/m^2, 21 Jun 113 W/m^2. |

### 8.6 Analysis and alerts (`lib/analysis/*`, `lib/alerts/*`)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Statistics scan | `analysis/scan.py` | rank (Spearman) and Pearson correlation; Student's t p-value via the incomplete beta function with effective n `n/(1 + 2*sum(rho_x*rho_y))` (Bretherton); Sidak correction over 13 lags (0 to 6 h) `p_adj = 1 - (1 - p)^13`; Newey-West (Bartlett) variance of the daily correlations and Fisher z; Benjamini-Hochberg false-discovery rate at q 0.1 | REAL-MODEL | Published methods. The incomplete-beta numerics were not re-derived (standard continued fractions). |
| Rain vs reading | `pairs.py` `analyse` :84; `LOOKBACK` 6 slots (3 h) | each slot classed falling, steady or rising by the 3-hour change; fair-share ratio is rain share over time share; "clear link" at ratio 1.5 or more moving and 0.65 or less steady; "moderate" 1.25 and 0.85; "no link" 0.85 to 1.15 on both | TUNED | The Pearson correlation of the 3-hour change with the next 3 hours of rain is REAL-MODEL, shown only with at least 30 pairs. |
| Air vs rain | `pairs.py` `analyse_air` :149 | mean of the 6 hours before a rain event against the 6 hours after (3 hours of readings needed each side); "cleaner" if after is 0.9 times before or less; wet/dry ratio of means; rank correlation across at least 10 days; "rain clears the air" at share 0.7 or more and ratio 0.8 or less | TUNED | The rank correlation is REAL-MODEL. |
| Thresholds | scan.py | strength words: abs r 0.5 or more strong, 0.3 or more moderate, else weak; `MIN_DAYS` 21, `MIN_DAILY` 14, `CALM_KMH` 5, `MIN_SECTOR_SLOTS` 30 | TUNED | |
| Circular mean | `lib/ecowitt/direction.py:33` | mean direction `atan2(mean sin, mean cos)`; steadiness is the resultant length `R = hypot(mean cos, mean sin)`; steady at R 0.6 or more, fairly steady 0.35 or more, else variable | REAL-MODEL | Circular statistics; the word cut-offs are TUNED. Not weighted by speed. |
| Alert rules | `lib/alerts/*` | gust over 40 km/h (1-hour rearm); UV index 10 or more for 2 readings in a row (`CONFIRM_READINGS`, 1-hour rearm); outdoor-indoor gap over 1.0 C for the crossing alert; a rain alert on any tip; 60-minute default cooldown | TUNED | |

## 9. Known limitations

Each item states how the code behaves and where that departs from reality.

Readings and standards:

1. AQI is computed from one instantaneous reading, not a 24-hour average or NowCast (section 8.3).
2. In Fahrenheit the thermometer's ticks and band edges do not coincide; the bands are drawn in Celsius steps.
3. The inch dials span a slightly different range than the metric ones, because the scale ends are rounded to whole steps of the new unit (section 7.1).
4. The pressure arrow (3-hour change, WMO classes, 0.1 hPa for steady) and the outlook's pressure verdict (change over the 3-hour window, 1.0 hPa for steady) use different scales.
5. The Magnus dew point is poor at very low humidity; it runs only when the station sends no dew point.
6. The clear-sky model uses Cooper's declination and no Earth-Sun distance term, so it is off by a few percent on the day-look threshold (section 8.5).
7. Sunrise and sunset ignore altitude and the horizon's real shape (section 2.2).
8. The terminal speed `9.65 - 10.3 exp(-0.6 D)` is the Atlas-Ulbrich fit to the Gunn-Kinzer data, not Gunn-Kinzer's own table; it is good for 0.5 to 6 mm drops (section 3.2).

Rain counter:

9. The rain counter keeps the total before a rejected drop as its baseline (`lib/rain.py:44-51`). If the real midnight reset is rejected (the first reading of the new day is 2 mm or more, or the reading is after 2 am local and the new total is above 10 % of yesterday's), the rest of the day's rain is lost until the new total passes yesterday's. This needs 2 mm or more in the first slot after a gap of at most 90 minutes, but the loss is the whole day.

Design simplifications that do not estimate reality:

10. Fog uses outdoor relative humidity of 90 % or more, not the glass temperature against the dew point.
11. Ice uses air temperature below 0 C regardless of humidity or dew point.
12. The rain slant is capped at 70 degrees (reached from about 31 km/h by the far layer at 5 mm/h, from 63 km/h by the near one); the real law has no cap.
13. `PAN_MAX`, `PAN_K` and the rain, leaf and haze speeds are in art px/s with no physical length scale, so only their ratios mean anything.
14. The UV beam lean assumes east on the right of the screen, which is right for the Melbourne station and mirrored for a window facing the other way.
15. The leaf pace is at least 0.76 whenever the wind effect is on (above 15 km/h), so its scaling shows only from 15 km/h up; the leaves are not drawn below the threshold, and the lower clamp 0.5 is reached only while the wind eases out.
16. The rain's path speed keeps growing to 2.9x its calm value at the cap while the streak length stops at 1.6x (from 51 degrees), so above 51 degrees a streak is shorter than the path speed suggests.
17. The flood bucket reads only the file's past six hours (`FLOOD_READ`): water from rain more than six hours old is not counted. With tau 30 min six hours is 12 time constants, so the share lost is under 0.01 %.
18. The haze's viewing distance `L = 3643 m` is chosen so the look at each AQI band's typical PM2.5 is that band's design look; it is not a measured visibility, and Koschmieder's visibility for the same extinction would be far longer at low PM2.5.
19. The haze texture's cover per band is not the nominal share: each layer is uniform after equalisation but the sum is bell-shaped (about 42, 59, 79, 92 and 99 % by band).

Notes: `leafAir` measures the mean wind by differencing two `airAt` calls, so near a strong eddy it is reduced by the pull term and the eddy weight is slightly overstated. NOAA's refraction has a 0.6 arcmin step at -0.575 degree, irrelevant because `refracted=False`. The sun position agrees with an independent almanac algorithm to 0.45 degree in elevation; the azimuth differs only near the zenith.

## 10. Maintenance

This file is kept in step with the code: the formula, its constants, its label and its line reference are edited in the same commit as the code. A limitation is deleted from section 9 when the code stops having it.

Constants to grep when checking that the numbers here match the code:

| Area | Grep for |
|---|---|
| Rain slant, drop size | `d0Of`, `vtOf`, `beadROf`, `RAIN_NOMINAL`, `LAYERS` (`size`, `near`), `MID_SPEED`, `CAP = 70`, `OLD_SLANT`, `PATH`, `STREAK_MOST`, `GUST_SHARE`, `effectiveSpeed`, `TAU_UP`, `TEST_WIND`, `STEPS = 20`, `PACE` / `paceOf` |
| Effect levels | `conditionsOf`, `FLOOD_SHOW`, `FOG_ON`, `FOG_OFF`, `windSideOf`, `GUST_MARK`, `BUDGET`, `ART_PX`, `FADE_S` |
| Haze | `PM_EXT`, `PM_CAP`, `PM_EDGES`, `pmOpacity`, `PAN_MAX`, `PAN_K`, `bakeTexture`, `HOLLOW` |
| Heat | `arcOf`, `sunNow`, `art`, `BALL_RAMP`, `FLARE_RAMP`, `SUN_R`, `SUN_X`, `SUN_Y`, `SUN_RY0`, `SUN_RY1`, `SUN_EVERY`, `SUN_STEP`, `SUN_STRENGTH`, `shadesOf` |
| UV and sun | `BEAMS`, `BEAM_BL`, `BEAM_WK`, `BEAM_EDGE`, `BEAM_FADE`, `UV_FROM`, `UV_TO`, `UV_SOFT`, `uvAmount`, `BEAM_MAX`, `BEAM_CURVE`, `EL_LOW`, `RUN_ON`, `SUN_FALLBACK`, `leanAt`, `leanOf` |
| Ice, flood | `reachFor`, `growRate`, `MELT`, `RISE = 4`, `DRAIN`, `CALMS`, `PUSH`, `EDDY_REST`, `flood_of` |
| Beads | `RUN_R`, `critOf`, `LEAN_FROM`, `BRAKE_S`, `GONE`, `TINY`, `SWELL`, `PACE`, `sized`, `HEAVY_RAIN`, `FOG_AT`, `FORM`, `CLEAR`, `THICKEN` |
| Tilt, wind push | `POOL_IN`, `POOL_OUT`, `STEEP_FULL`, `SNAP_DEG`, `screenGravity`, `tilt(`, `PUSH_ON`, `PUSH_FULL`, `PUSH_MAX`, `pushOf`, `aim(` |
| Gauges | `STEPS = [0.1`, `START, SWEEP`, `MIN_TILE`, `TABLE_TOP`, `STALE_MS`, `TENDENCY_STEADY`, `TENDENCY_AGO`, `TENDENCY_CLASSES`, `FLOOD_TAU`, `FLOOD_RUNOFF`, `FLOOD_FULL`, `FLOOD_ROW`, `FLOOD_SHOW`, `ROSE_REACH`, `33.8639`, `1.609344`, `25.4` |
| Server | `sun_position`, `sun_times`, `SUNRISE_ELEVATION`, `refraction`, `17.62`, `us_aqi`, `pm25_aqi`, `PM25_AQI`, `SPIKE_RATIO`, `truncate`, `MAX_GAP`, `RESET_MM`, `MIN_READINGS`, `STEADY_HPA`, `HAURWITZ`, `MIN_HOURS`, `SEASON_MONTHS` |

A TUNED or ARTISTIC number fitted to data or a standard takes the label of its source. A REAL-MODEL formula that is simplified is REAL-APPROX, with its limits listed.
