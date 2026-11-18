# Math in ecobot: which formulas estimate reality

This file lists the formulas in the code: sun and sky geometry, the weather effects on the dashboard Sky, gauges and units, and the server-side derived readings. For each one it says whether it estimates something real or only shapes how the page looks.

Line numbers are approximate (the dashboard file changes often). Search for the named constant or function instead of trusting a number. Sky distances are in "art px", the low-resolution canvas pixel (3 to 4 CSS px, `ART_PX` 480). Art px have no physical length, so Sky speeds in px/s are only meaningful as ratios.

## Labels

| Label | Meaning | Example |
|---|---|---|
| REAL-MODEL | A published formula or exact identity, used as published. Its error is the model's own. | NOAA sun position, unit conversions, `r^3` bead volume |
| REAL-APPROX | The right physical idea in a simplified form. It tracks reality in some range and fails outside it; the limits are listed. | Rain slant `tan(angle) = wind / fall speed` with one fall speed |
| TUNED | A number chosen by look or feel, or a rule of thumb, on a real quantity. It says "more of X, more of the effect", not how much. | Rain strength ramp, pressure arrow 0.3 hPa |
| ARTISTIC | Made up for looks or behaviour. It estimates nothing. | Heat-wave brightness curve, eddy sizes, frog timing |

How to read the tables:

- "Where" is a file and an approximate line, or a function or constant name.
- Formulas are plain text. `x^n` is a power, `clamp(x, a, b)` limits x to [a, b], `smoothstep(u) = u*u*(3 - 2*u)`.
- A formula with two labels (for example REAL-MODEL structure, TUNED constants) is listed with both. The summary counts it under its weaker label.
- "Faithful?" says how close to reality the formula is and where it stops being right.
- A number appears only if it is in the code or a cited standard.

Status (as of 96e255f):

- The rain is three drop-size classes (far 0.6, mid 1.0, near 1.5 times the median drop D0 of the live rain rate): each layer's lean is `tan(angle) = wind / (v_t * 3.6 km/h)` (cap 70 degrees), its vertical fall speed follows its terminal speed v_t (path speed `V / cos(angle)`), and `TEST_WIND = 11` is in the code. The old one-for-all `tan(angle) = speed / 24 km/h` law (`FALL`) is gone. Section 3.2.
- The wind's eddies rise half as often: each slot rests 5 to 9 s after its eddy before the next (row A8 and section 3.3).
- The heat fills the top quarter of the window; the sun's lean (the UV beams' own value) sets only the direction its front travels. Section 4.2.
- Issues in section 9 carry a status: FIXED (with the commit), LEFT BY DECISION (the user chose to leave it), or N/A.

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
| S7 | Beam, glow and shaft drawing | 2.2 | ARTISTIC |
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
| H3 | Heat level, colour ramp, period, peak | 4.2 | TUNED |
| H4 | Heat wave phases, envelope and diagonal front (the lean itself is S3) | 4.2 | ARTISTIC |
| H5 | UV trigger (UV index 11 and above) | 4.3 | REAL-MODEL |
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
| B5 | Bead speed law and glass pace | 5.3 | TUNED |
| B6 | Condensation, swell and fog timing rates | 5.2 | TUNED |
| B7 | Snap, pop, gulp, stretch, lock-on, meander, pauses | 5.3 | ARTISTIC |
| B8 | Heavy-rain beads and splash | 5.4 | ARTISTIC |
| T1 | Gravity direction from the accelerometer | 6 | REAL-MODEL |
| T2 | Steepness `sin(theta)` | 6 | REAL-MODEL |
| T3 | Steepness response curve and dead zone | 6 | TUNED |
| T4 | Face-down treated as flat | 6 | REAL-APPROX |
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

Count per label (counted from this table, as of 96e255f):

| Label | Groups |
|---|---|
| REAL-MODEL | 19 |
| REAL-APPROX | 15 |
| TUNED | 31 |
| ARTISTIC | 12 |
| Total | 77 |

Not computed here at all (the station or a provider supplies them): dew point, feels like, apparent temperature, VPD, pollen and thunderstorm-asthma levels, and the dashboard's heat index or wind chill (none exists in `lib/`).

## 2. Sun and sky geometry

### 2.1 Sun position: `lib/solar.py` (NOAA solar position algorithm)

Inputs: latitude and longitude (degrees, east positive) and an aware datetime. The datetime is converted to UTC first (`solar.py:33`), so no time-zone term is needed. `web_dashboard.sun_of()` calls it with `refracted=False`.

| # | Formula | Where | Math | Label | Faithful? |
|---|---|---|---|---|---|
| 1 | Fractional year | solar.py:36 | `gamma = 2*pi/D * (doy - 1 + (hour_UTC - 12)/24)` rad; D = 366 in a leap year, else 365 | REAL-MODEL | Matches NOAA. |
| 2 | Equation of time | solar.py:39 | `EoT = 229.18*(0.000075 + 0.001868*cos(g) - 0.032077*sin(g) - 0.014615*cos(2g) - 0.040849*sin(2g))` minutes | REAL-MODEL | NOAA's Fourier fit; good to about 0.5 to 1 min (about 0.1 degree of sun position). |
| 3 | Declination | solar.py:41 | `d = 0.006918 - 0.399912*cos(g) + 0.070257*sin(g) - 0.006758*cos(2g) + 0.000907*sin(2g) - 0.002697*cos(3g) + 0.00148*sin(3g)` rad | REAL-MODEL | Spencer series, about 0.05 degree; ignores nutation. |
| 4 | True solar time | solar.py:43 | `TST = 60*hour_UTC + EoT + 4*lon` minutes | REAL-MODEL | Exact for the model. |
| 5 | Hour angle | solar.py:44 | `H = TST/4 - 180` degrees | REAL-MODEL | Exact. |
| 6 | Elevation | solar.py:45 | `sin(el) = sin(lat)*sin(d) + cos(lat)*cos(d)*cos(H)`; clamped to [-1, 1] before asin | REAL-MODEL | Geometric (not refracted). Survey comparison with an independent almanac algorithm: within 0.45 degree, typically 0.1 to 0.25. |
| 7 | Azimuth (clockwise from north) | solar.py:49 | `az = (atan2(sin(H), cos(H)*sin(lat) - tan(d)*cos(lat)) + 180) mod 360` degrees | REAL-MODEL | Correct standard form. Ill-conditioned when the sun is near the zenith (tropics at noon). |
| 8 | Refraction (only if `refracted=True`) | solar.py:14 | NOAA piecewise fit, arcsec: el above 85: 0; 5 to 85: `58.1/t - 0.07/t^3 + 0.000086/t^5` with `t = tan(el)`; -0.575 to 5: polynomial in el; below -0.575: `-20.772/t`. Divided by 3600 to give degrees. | REAL-MODEL | Standard atmosphere only; real refraction varies by up to about 10 % with pressure and temperature. NOAA's own fit has a small step (about 0.6 arcmin) at -0.575 degree. The dashboard does not use it. |

Spot value: (-37.81, 144.96) at 13:02 Melbourne time on 21 Dec 2026 gives elevation 75.2, azimuth 14.3 degrees.

### 2.2 Sun use on the dashboard (UV beams)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Beam lean | `leanAt` web_dashboard.html:915 | `lean = clamp(-atan(sin(az) / tan(max(el, 3 deg))), -75, 75)` degrees from vertical; `EL_LOW` 3, `BEAM_MAX` 75 | REAL-APPROX | The sun's direction projected on the east/up plane, east on the right of the screen (as on the compass dial). It drops the north/south component (toward or away from the viewer). The 3 degree floor and 75 degree cap are display limits. Example: az 85.7, el 45.6 gives -44.4 degrees; az 111, el 10.8 gives -78, capped to -75. For a northern-hemisphere station the left/right sense would depend on which way the window faces. |
| Fallback lean | `leanOf` :911 | `h = clamp((t - noon) / ((set - rise)/2), -1, 1)`, `lean = 70 * sin(pi/2 * h)`; `noon = (rise + set)/2`; `BEAM_CURVE` 70 | TUNED | Right sign (negative in the morning) but no latitude or season; 70 degrees at the extremes, not 90. Used only when no sun position exists. |
| Sunrise, sunset fallback | `SUN_FALLBACK` web_dashboard.py | 06:00 and 18:00 (360 and 1080 min) only with no station location | TUNED | Wrong by up to about 3 hours at Melbourne's solstices; no longer used once a location exists (issue 20 FIXED). |
| Sunrise and sunset | `sun_times` solar.py, `sun_of` web_dashboard.py | The sunrise equation. Solar noon (UTC min) = `720 - 4 lon - eqtime` (eqtime and declination `dec` from the NOAA fits at that noon, refined twice); `w0 = acos((sin(-0.833 deg) - sin(lat) sin(dec)) / (cos(lat) cos(dec)))`; rise/set = noon -/+ `4 w0` min; plus the zone's UTC offset that date (DST), wrapped into the day, 0.01 min. -0.833 deg = refraction 0.567 + the disc's radius 0.266. An acos argument above 1: polar night (None, "no rise"); below -1: polar day (None, "no set"); `sun_of` then sends rise 0, set 1440 (day) or both null (night: no beams) | REAL-MODEL | Checked against the sun's own elevation crossing -0.833 deg (within a minute or two): Melbourne 2026-12-21 05:54 / 20:41 (not the 05:52 / 20:32 first guessed: the longitude puts solar noon at 13:18 DST), 2026-06-21 07:35 / 17:08; London 04:43 / 21:21 BST. The forecast's sunrise and sunset are no longer requested. Ignores altitude and the horizon's real shape. |
| Run-on | `setSun` :931, `placeNow` :919 | `rate = (new - last) / dt` for samples 30 s to 3600 s apart (azimuth difference the short way: `((d + 540) mod 360) - 180`); position runs on at that rate for at most 1800 s (`RUN_ON`) | REAL-APPROX | Linear extrapolation; accurate to a small fraction of a degree between the 60 s fetches. If data stops, the error grows (near transit in a Melbourne summer the azimuth moves roughly 50 degrees an hour). |
| Beams | `uvShafts`, `beamOf` :950-995 | count `max(4, round(W/70))` plus off-screen ones; width 8 to 18 art px; length 0.4 to 0.9 of H; rotation about the top by the lean, width squeezed by `cos(lean)`; sway 0.85 to 1.65 px | ARTISTIC | Drawn only while the sun is up. |
| Top glow | :1179 | height 16 % of H; alpha `0.6 * (1 - y/(0.16 H))^2.4` | ARTISTIC | |

## 3. Wind, rain and the air (Sky)

### 3.1 Common machinery

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Effect levels | `conditionsOf` :5350 | rain: 0 if rate is 0, else `0.3 + 0.7*min(1, rate/8)` (mm/h). haze: the number of AQI edges [50, 100, 150, 200, 300] below the AQI (0 to 5; the tile's own bounds if given; with no AQI, the PM2.5 breakpoints 9, 35.4, 55.4, 125.4, 225.4 against `data.pm25`): the band only picks the baked pattern, the opacity and tint follow PM2.5 (the haze table). uv: 1 if UVI is 11 or more. humidity: 0 when not fogging, else `max(0.05, min(1, (RH - 90)/10))` (so 0.05 while held between 85 and 90 %). wind: 1 if wind is above 15 km/h (the reading, not the gust). heat: 0 below 35 C, else `0.3 + 0.7*min(1, (T - 35)/8)`. ice: `-T` if T is below 0 (degrees below zero). flood: the server's bucket depth `data.flood.depth` (0-1) when it is 0.05 or more (`FLOOD_SHOW`), else 0 (no hysteresis; see the flood trigger); humidity on from 90 % (`FOG_ON`), kept while it is 85 % or more (`FOG_OFF`) | TUNED | The thresholds come from real category names (UVI 11 is WHO "extreme"; 15 km/h is mid-Beaufort 3), but the ramps are linear and chosen by look. Rain saturates at 8 mm/h, so 8 and 30 mm/h look the same. Hysteresis on the flood (on over 30, off below 24 mm/h) and the humidity (on from 90 %, off below 85 %), held in the page (`flooding`, `fogging`, :5359-5360), so a reading near either does not toggle the effect; a page load starts with the plain mark. |
| Wind side | `windSideOf` :5372 | direction in (0, 180) gives leftward (-1); in (180, 360) gives rightward (+1); 0, 180, 360 or no reading keeps the last side | REAL-APPROX | Correct sign for a dial with east on the right: wind from the east blows toward the west. Only the side counts; 91 and 179 degrees behave the same. |
| Direction easing | `steer`, `DIR_S` 4 s | `p` slides to its target at rate `2/DIR_S`; `f = sin(p*pi/2)`; the side flips when f crosses 0 and sprites are mirrored | ARTISTIC | |
| Particle budget | `BUDGET` :285, `tick()` | budget = `W*H/400` particles (480 x 300 gives 360); `share = min(1, budget / sum of needs)`; each effect gets `round(need*share)`; humidity has its own limits; rain's count is the plain budget at every slant | TUNED | A performance and legibility cap. Max rain alone: need 360, exactly the budget. |
| Brightness cap | `CAP` :286, `draw()` | `sum = total of strength_k * amount_k` over all effects except rain; `cap = min(1, 1.5 / sum)`; each effect drawn at `strength*amount*cap`. Strengths: rain `0.55 + 0.35*L` (outside the cap), haze 0.44/0.52/0.60/0.70/0.82 by band, heat `0.4 + 0.4*L`, uv 0.8, wind 0.8, flood 0.6, gust 0.45, ice `0.36 - 0.12*min(1, extent/(0.6*big))`, humidity 0.8 | TUNED | Looks only. |
| Frame, fade | `FADE_S` 1.5 s, 30 fps cap | linear fade | ARTISTIC | |
| Smoothstep | various | `u*u*(3 - 2*u)` | REAL-MODEL | A standard easing function (the use of it is for looks). |

### 3.2 Rain (`web_dashboard.html` ~474-680; `GUST_SHARE` :335)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Drop size from rate (A5a) | `d0Of`, `rainRate`, `RAIN_NOMINAL` 5, `physics()` in the rain | `R` = the live rain rate (the gauge's, `Sky.setRainRate`; none, as with a test link's rain over a dry gauge: 5 mm/h) clamped 0.5 to 100 mm/h; median drop diameter `D0 = 0.9 * R^0.21` mm (Marshall-Palmer: 0.9 mm at 1 mm/h, 1.46 at 10, 2.05 at 50); the layers are size classes `D = 0.6 * D0` (far), `1.0 * D0` (mid), `1.5 * D0` (near) | REAL-APPROX | The Marshall-Palmer distribution has median volume diameter about `0.9 R^0.21` mm (the code's form: the true constant depends on the form of the median); it is an average over rain types (convective rain has larger drops than stratiform at the same rate). Three classes stand for the whole spectrum, with the 0.6 and 1.5 multipliers chosen (ARTISTIC). Needs the rate: with none it assumes 5 mm/h. |
| Terminal speed | `vtOf` | `v_t(D) = 9.65 - 10.3 * exp(-0.6 D)` m/s, D in mm (Gunn-Kinzer fit: 3.65 at 0.9 mm, 5.36 at 1.46, 6.73 at 2.1, 7.95 at 3) | REAL-APPROX | A good fit for 0.5 to 6 mm drops at sea level; ignores air density with height and drop shape, and above about 5.8 m/s large drops break up. |
| Slant law | `slantOf(kmh, k)`; `CAP` 70 deg | per layer `tan(angle) = min(tan 70 deg, v / (v_t * 3.6))`, v in km/h (across per down), v_t of that layer: small drops lean furthest, heavy rain (larger drops, faster) less than drizzle. At 25 km/h and 5 mm/h: far 65.9, mid 55.3, near 47.6 degrees (at 1 mm/h far 72 capped to 70, mid 62, near 54; at 50 mm/h 56, 46, 41). The 11 km/h test link at 5 mm/h: 44.5, 32.4, 25.8. The cap: far from 30.8 km/h, mid from 47.7, near from 62.7 (5 mm/h) | REAL-APPROX | The real law is `tan(angle) = u_wind / v_terminal`, per drop size, as here (it was one fall speed for all drops, 24 km/h). It ignores the wind profile with height (the anemometer is a few metres up) and turbulence, and uses three sizes only. There is no real cap: at 100 km/h the true angle is 76 degrees or more. The 70 degree cap also bounds the sprite set (21 tilts, 3.5 degrees apart, `STEPS` 20). |
| Effective speed | `effectiveSpeed` :336 (shared by the rain, the haze and the leaves' pace); `GUST_SHARE` 0.3 (:335) | `v_eff = speed + 0.3 * max(gust - speed, 0)` | TUNED | A mix of mean and gust with no standard. |
| Smoothing | `TAU_UP` 1 s, `TAU_DOWN` 2 s | first-order low-pass: `speed += (goal - speed) * (1 - exp(-dt/tau))`; snaps within 0.02 km/h | TUNED | Hides the 16 s reading jitter. The asymmetry is aesthetic. |
| Fall speed | `PATH` :491, `physics()`, `spawn`, `step` | per layer `v = 230 * clamp(v_t,mid(R) / v_t,mid(5 mm/h), 0.7, 1.3) * v_t,layer / v_t,mid` art px/s (mid at 230 and the others in the v_t ratios at 5 mm/h: about 148, 230, 303; drizzle 0.76x, a downpour 1.3x at most), `* U(0.85, 1.15) * pace`, `pace = 0.8 + 0.4*level`, the same at every slant; across = vertical * tan(angle); path speed = vertical / cos(angle), each layer at its own angle | REAL-APPROX | The kinematics are real: wind pushes a drop sideways without slowing its fall, so the path speed is vertical over cos, and the speeds follow the terminal speeds. The simplification: art px with no physical length (the scale is chosen so the mid layer at 5 mm/h keeps the old 230, ARTISTIC; the 0.7 to 1.3 clamp and the 0.8 to 1.2 level pace are chosen), so only the ratios are physical. A drop blown out of the window's downwind side starts again at the top or upwind side, so landings per area are the same at every slant; each layer spawns over its own lean's extent, so the most leaning layer's region covers the width. |
| Streak length | `streak` :507; `STREAK_MOST` 1.6 | length along the path `(n - 1) * PATH * min(1.6, 1/cos(angle))` with n 3 to 4 (far), 5 to 6 (mid), 8 to 10 (near); rows `round(len*cos)`, columns `round(len*tan*cos)` | ARTISTIC | Grows with the path speed, capped at 1.6x the calm length (reached at 51.3 degrees; the path speed itself keeps growing to 2.9x). The calm length is `(n - 1) * PATH` with `PATH = sqrt(1 + 0.45^2) = 1.097`. |
| Drop count | `need` :560 | `need = (W*H/1000) * (0.1 + 2.4 * level^1.5)`; count `round(need * share)` | TUNED | No density factor: the vertical speed is constant, so landings per area (measured over 60 s of weather time: 3870, 3822, 3823, 3695 at 0, 24, 46, 68 degrees) stay within 5 %. The comment now says about 71 drops for the light case on a laptop, which matches (`0.1 + 2.4*0.3^1.5` times 144, 71.2). |
| Sway | step | `slant * (1 + 0.12*sin(0.37 t))`, a 17 s period | ARTISTIC | |
| Wind push on rain | step :547 | extra across-speed `(calm*STEADY*(1 + gust)*k + BANDS*(0.7 + gust)*band) * slant/0.45`, `STEADY` 45, `BANDS` 190 px/s, `k` = the layer's nearness 150/330, 230/330, 1 (`LAYERS[].near`, fixed whatever the drop size) | ARTISTIC | Scales with the slant so there is none in calm. |
| Landing, splash | step | per drop, its layer's own lean for the splash drift; splash of 5 frames in 0.24 s, thrown downwind by `min(abs(vx)/vy, tan 70 deg) * 1.4` px | ARTISTIC | |
| Leaf knock | `hard` | `hard = 0.04 + 0.96 * level^2.5`; knock `60 px/s * hard * ratio * U(0.7, 1.3)` | ARTISTIC | |

### 3.3 Wind: the leaf air-velocity field (~1215-1560)

Everything here is drawn in art px. The measured wind speed scales the leaves' pace by `k = clamp(0.4 + 0.024*s, 0.5, 1.6)` (`PACE` :1227, `paceOf` :1228; applied at :1509; `s` the rain's effective speed, eased with `TAU_UP`/`TAU_DOWN`): 25 km/h gives the layer speeds below (k = 1), 15 km/h 0.76, 40 km/h 1.36, 50 and over 1.6, under about 4.2 km/h 0.5. The leaves are only on above 15 km/h, so in practice k is at least 0.76. `k` scales the leaves' speed, the gust bands' carrying speed, the turbulence's drift and swirl, and the eddies' drift; a test link's wind over a calmer real one uses k = 1. The reading also switches the effect on (above 15 km/h), chooses the side, and drives the rain slant and haze pan.

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Leaf speeds | LAYERS :1222 | 40/60/85 art px/s `* k * clamp(W/360, 0.6, 1) * (1 + gust + 0.3*(0.7 + gust)*band) * abs(dir.f)` | ARTISTIC | Their scale `k` follows the reading (TUNED: 0.4 + 0.024 s is chosen by look, with no physical length scale); the rest is not tied to it. |
| Gust pulses | step ~1500 | every `U(2.5, 6)` s add `U(0.5, 1.1)` to g; g decays as `exp(-0.9 t)` | TUNED | A stand-in for gustiness, not driven by the gust reading. |
| Gust bands | `bandAt` :1260 | `0.62*sin(0.024 s X) + 0.38*sin(0.041 s X + 1.7)`, `s = clamp(360/W, 1, 2.6)` (wavelengths 262 and 153 art px at s = 1) | ARTISTIC | No basis in the data. |
| Turbulence | `airAt` :1275 | stream function `psi = A*sin(0.05X + a)*sin(0.07y - 0.7a) + B*sin(0.11X + 1.3 - a)*sin(0.13y - 0.9a)`, `A = 420*(0.7 + 0.6g)*k`, `B = 110*k`, `k = layer speed/60`, `a = 0.6 t`. Velocity `(dpsi/dy, -dpsi/dx)`; curl `0.0074*A*s1*s2 + 0.029*B*s3*s4` | ARTISTIC | The structure is real maths: the field is divergence-free, and `0.0074 = 0.05^2 + 0.07^2`, `0.029 = 0.11^2 + 0.13^2` (checked). The amplitudes and wavelengths are chosen. |
| Eddies | `eddy` :1316, `EDDY_REST` | tangential speed `spin*rho*exp(-rho^2)`, `rho = distance/r`, maximum 0.43*spin at rho 0.71; `spin = +-U(4.5, 6)*r`, `r = U(26, 46)` art px; life `U(5, 9)` s, strength `sin(pi*k)^0.6`; count (slots) `max(4, round(W*H/30000))`; after its life each slot rests `U(5, 9)` s before the next eddy (`EDDY_REST`: a cycle of 14 s on average, so a new eddy rises half as often as the 7 s it did; half the slots start part way through a rest) | ARTISTIC | A Gaussian-type vortex, not a Lamb-Oseen or Rankine one (1/r far field). Real eddies do not pull leaves in and then fling them out. |
| Leaf relaxation | step | `v += (v_air - v)*(1 - exp(-dt/tau))`, `tau = U(0.12, 0.3)` s; sink `U(2, 10)` px/s; spin `U(4, 9) + min(2.5*abs(curl), 14) + whirl` frames/s | REAL-APPROX | First-order drag relaxation is the standard way a light particle follows air. The sink speed is not physical (a real leaf falls near 1 m/s). Spin rising with curl is qualitatively right. |
| Veer | `setWind` :3917, `leafAir` :1306 | on a side flip the heading turns by about 180 degrees: `angle = from + delta*smoothstep(u)`, duration `VEER_S*abs(delta)/pi`, at least 1 s, `VEER_S` 9 | ARTISTIC | A designed story so leaves do not stop and reverse; real air does not swirl 180 degrees on a wind shift. |

## 4. Haze, heat, UV, ice, flood

### 4.1 Haze (~680-900)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Level | `conditionsOf` | AQI above 50/100/150/200/300 gives levels 1 to 5 | REAL-MODEL | The US AQI band edges. The AQI itself is in section 8.3. |
| Opacity from PM2.5 | `pmOpacity`, `pmShown`, `PM_EXT` 0.01457, `PM_CAP` 0.97, `PM_EDGES` | Beer-Lambert with Koschmieder's view: extinction (1/m) = 4e-6*PM2.5 (4 m2/g mass extinction), L = 3643 m, `opacity = min(0.97, 1 - exp(-0.01457*PM2.5))`: 12 % at 9 ug/m3, 25 % at 20, 40 % at 35, 55 % at 55, 77 % at 100, 89 % at 150, 97 % at 250; none below 8, eased in to 9; each band's image is baked at the opacity of its top edge and drawn at `opacity/opacity(top)` (so at the band's midpoint it is its design look); PM2.5 low-passed with TAU 4 s; the tint is lerped by PM2.5's place between the bands' midpoints (quantised to 1/16, re-baked and cross-faded) | REAL-APPROX | Beer-Lambert is the real law, the 4 m2/g mass extinction is a typical fine-particle figure, and L = 3643 m is an effective distance chosen for the look (a few km of visibility scale). The look at each band's typical value is unchanged. |
| Pan speed | `panFor` :710; `PAN_MAX` 3.15, `PAN_K` 22.1 | `pan = 3.15 * (1 - exp(-w / 22.1))` art px/s, `w = speed + 0.3*max(gust - speed, 0)`. Checks: 3 km/h gives 0.40, 40 km/h gives 2.63 | TUNED | Real haze moves at the wind speed (40 km/h is 11 m/s). This is a slow, saturating stand-in that encodes only "faster wind, faster pan". |
| Low-pass | `TAU` 4 s | `filtered += (w - filtered)*min(1, dt/4)` | TUNED | |
| Texture | `bakeTexture` :730 | three layers of value noise (octaves 0.55/0.3/0.15), domain-warped, histogram-equalised per layer, summed with weights 0.6/0.85/1; cut by `comb`: `t = clamp((d/255 - lo)/span, 0, 1)`, shade `7*0.999*smoothstep(t)` | ARTISTIC | Each layer is uniform after equalisation but the sum is bell-shaped, so the cover per band is not the nominal share (see Issues). |
| Tint | `lookOf` | grey mix 14 % using `Y = 0.2126R + 0.7152G + 0.0722B` | ARTISTIC | The luma weights are the real ITU-R BT.709 ones (REAL-MODEL), applied to gamma-encoded values as a shortcut. The colours are chosen. |
| Palette | `bake` | 8 shades; colour mix `u^0.85`, alpha `255*open*u^0.8`, `open` 0.7/0.7/0.8/0.9/1 by band | TUNED | |
| Motes | ~752 | count `(W*H/1800)*(0.6 + 0.2*level)` moving at the pan speed | ARTISTIC | |

### 4.2 Heat (`web_dashboard.html` ~998-1170)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Heat 0 to 1 | `heatOf` :1026 | `h = clamp((level - 0.3)/0.7, 0, 1)`, which is `(T - 35)/8` for 35 to 43 C | TUNED | Linear and chosen. Nothing relates it to heat's effect on a person. |
| Colour ramp | `heatColours` :1033; `HEAT_HUE` [30, 4], `HEAT_SAT` [0.3, 1], `HEAT_LIGHT` :1023 | `hue = 30 - 26h` (30 to 4 degrees), `sat = 0.30 + 0.70h`, five lightnesses [0.3, 0.4, 0.48, 0.55, 0.68] plus `(1 - h)*0.14` | TUNED | Standard HSL conversion (REAL-MODEL), chosen ramp: pale amber to vivid scarlet. |
| Period | `cycle` :1053; `HEAT_PERIOD` [14, 10] | `period = 14 - 4h` s (14 s at 35 C, 10 s at 43 C) | TUNED | At most 0.1 flashes a second, far under the 3 Hz limit of WCAG 2.3.1. |
| Peak | `cycle`; `HEAT_PEAK` [0.7, 1] | `peak = 0.7 + 0.3h` | TUNED | |
| Wave phases | `heatWave` :1041; `HEAT_BUILD` 0.55, `HEAT_HOLD` 0.7, `HEAT_GONE` 0.9, `HEAT_RISE` 0.08 | phase u in [0, 1): build to 0.55 with extent `1 - (1 - s)^2`, `s = u/0.55`, brightness `(0.55 + 0.45s)*smoothstep(min(1, u/0.08))`; hold to 0.7; fade to 0.9 with brightness `1 - smoothstep`; gap to 1 | ARTISTIC | A visual "heat radiating" metaphor. |
| Lean | `wanted`, `draw` | the beams' lean (`beamState().lean`, section 2.2), 0 when the sun is down; the drawn lean catches up at `HEAT_SLEW` 20 degrees a second at most (`dt` capped at 0.1 s). It moves only the front, never the sprite | REAL-APPROX | The direction is the sun's own (S3, with its limits). The heat itself comes from the air temperature, not the sun: the lean says only which way the animation radiates. |
| Envelope | `bake`; `HEAT_FADE` 0.25, `HEAT_TOP` 0.62, `HEAT_LOBES` 0.85 | `rows = ceil(0.25*H)`; sprite W by `rows`, brightness `0.62*(0.5 + 0.5*cos(pi*y/rows))^1.25*lobe(x)`, `lobe = 0.85 + 0.15*clamp(0.5 + 0.32*sin(2*pi*x/W + 0.7) + 0.18*sin(4*pi*x/W + 2.1), 0, 1)`; alpha `0.5*(0.55 + 0.45*level)`; dithered | ARTISTIC | A plain full-width rectangle: strongest at the top edge, 0 at a quarter of the height, no side or top edges of its own, fixed subtle lobes (no dark gap). |
| Front | `draw`; `HEAT_SOFT` 0.4 | with `a = lean` in radians, the distance along the ray from the top corner on the sun's side (the right for negative lean) is `t = y*cos(a) + dx*|sin(a)|`; total way `long = rows*cos(a) + W*|sin(a)|`; the front is at `extent*(long + 0.4*rows)`, a linear mask over its last `0.4*rows` (a canvas linear gradient along the ray, so its iso-lines are perpendicular to it) | ARTISTIC | Driven past the far bottom corner, so at the hold the whole quarter is lit evenly. |


### 4.3 UV

| Name | Where | Formula | Label | Faithful? |
|---|---|---|---|---|
| Trigger | `conditionsOf` | UVI of 11 or more turns the beams on (strength 0.8) | REAL-MODEL | 11 and above is the WHO "extreme" category. |
| Beams, lean, glow | section 2.2 | | REAL-APPROX / ARTISTIC | See 2.2. |

### 4.4 Ice and frost (~3240-3600)

With `d` = degrees below 0 C.

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Reach | `reachFor` :3256 | `reach = big*(0.035 + 0.615*min(1, d/5)^1.2 + 0.15*clamp((d - 5)/5, 0, 1))`, `big` = the longer window side. 3.5 % just below 0, 65 % at -5 C, 80 % at -10 C and colder | TUNED | Real frost needs a surface at or below 0 C and below the frost point (dew point 0 C or lower). This uses air temperature only, ignores humidity and dew point, and the reach is a fraction of the window. |
| Growth | `growRate` :3259 | `big*(0.0005 + 0.0005*min(d, 10))` per second (about a minute to the fringe just below 0, 3.6 minutes at -5) | TUNED | "Colder, faster" is the real trend; the numbers are chosen. |
| Melt | `MELT` :3260 | `0.005*big` per second | TUNED | |
| Crystal forms | ~3180-3230 | branches mostly at 60 degrees to the stem; fern, plume, tuft, needles | ARTISTIC | The 60 degrees nods to ice's hexagonal habit; forms and sizes are decorative. |
| Haze depth | ~3188 | `min(0.4*extent, 0.11*min(W, H))` | ARTISTIC | |

### 4.5 Flood, frog, gust objects (~2810-3210, ~3600-3850)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Flood trigger | `flood_of` (lib/web_dashboard.py); `FLOOD_TAU` 30 min, `FLOOD_RUNOFF` 0.5, `FLOOD_FULL` 15 mm, `FLOOD_READ` 6 h, `FLOOD_ROW` 300 s; page `FLOOD_SHOW` 0.05 | depth' = 0.5*R - depth/tau (mm, R in mm/h, tau 30 min), stepped exactly over the past six hours' 5-minute rain-rate rows (`d = d*exp(-dt/tau) + 0.5*R*tau*(1 - exp(-dt/tau))`), the newest row run on to now with the live rate; sent as `depth/15` clamped to 0-1, read at most once a minute. The page shows the flood from 0.05 and sets the water's height to `depth * 0.25*H` (RISE/DRAIN as top speeds); the frog needs depth 0.9 or more | REAL-APPROX | A leaky-bucket (linear reservoir) runoff model: rain falls, half of it runs off into the bucket, which drains with a 30 minute time constant, so a sustained 60 mm/h fills it, 30 mm/h half, a 5 minute 100 mm/h burst about 0.2, and 60 mm/h for an hour then dry is 0.37 of its peak after 30 minutes and 0.05 after 90. The constants (runoff share, tau, full depth) are tuned, not catchment data; real flash floods depend on soil, slope and catchment. The old 30/24 mm/h trigger and its hysteresis are gone (the bucket is slow itself). |
| Level | step ~3095; `RISE` 4, `DRAIN` 6 (:2831) | full height `0.25*H`; rises at `full/4` per second, drains at `full/6` per second | ARTISTIC | Far faster than real water. |
| Wave equation | `waves` :2972; `c` 34 px/s | per column `a = c^2*(h[l] + h[r] - 2h[i]) + 2*(v[l] + v[r] - 2v[i]) - 0.1*v[i]`; substeps `ds <= 0.7/c`; surface clamped to +-6 px | REAL-APPROX | A damped 1D wave equation with viscous smoothing is a fair stand-in for surface waves. Real waves are dispersive (`c = sqrt(g*d)` only in shallow water); this is not. |
| Wind drift | `PUSH` 6 px/s (:2833) | first-order upwind advection of h and v at `6 px/s * wind amount` | REAL-APPROX | Real surface drift is a few percent of wind speed; this is a constant. Upwind adds numerical diffusion. |
| Drop hit | `hit` | Gaussian poke of `-power` px, width 1.3; power 0.3/0.6/1.1 by layer | ARTISTIC | |
| Chop | `CALMS` 25 s | chop eases to 1 with a 3 s time constant, back to 0 with 25 s; pokes per second `chop*30*W/300` | TUNED | |
| Frog timing | ~2767-3062 | first frog after 30 s, then exponential with mean 180 s (`-mean*ln(1 - U)`), minimum gap 40 s; speed `U(7, 10)` px/s | ARTISTIC | Whimsy. |
| Frog throat pulse | page `THROAT_PERIOD`, `throatAt` | period 0.67 s (about 1.5 a second, a resting frog's throat rate), a random phase per frog; amount = smoothstep(t/0.25) out, held to 0.4 s, `1 - smoothstep((t - 0.4)/0.27)` back; swollen (amount >= 0.5) adds a 5 x 1 patch row and a 3 x 1 row under it, 2 + 3 = 5 pixels | ARTISTIC | A small touch at a real rate; independent of the stroke clock. |
| Gust-object trigger | `GUST_MARK` :5430 | a new gust reading above 99 km/h flings one object | TUNED | 99 km/h is 27.5 m/s, in Beaufort 10 to 11 (89 to 117 km/h). The mark is "about a hundred". |
| Gust-object flight | `gust` step | speed `U(42, 56)` px/s `* (1 + 0.35*wind amount)`; shed bits every 0.15 to 0.45 s, up to 14 | ARTISTIC | |

## 5. Condensation beads (humidity effect, ~1568-2800)

Distances are art px, times are seconds. The dew point is not used anywhere in the humidity effect: the trigger is outdoor relative humidity only.

### 5.1 Trigger and fog

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Trigger and level | `conditionsOf` :5360; `FOG_ON` 90, `FOG_OFF` 85 | on from 90 % RH, kept while it is 85 % or more; level `clamp((RH - 90)/10, 0.05, 1)` (0.05 while held between 85 and 90 %) | TUNED | Real fog on glass needs the glass below the air's dew point. RH of 90 % is a stand-in. The 5 point hysteresis stops flicker. |
| Fog thickness | `thickness` :1816; `THICKEN` 4 s | `0.5 + 0.5*v`, approached at 1/4 per second | ARTISTIC | At v = 0.05 it is 0.525 (the comment says so now). |
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
| Speed law | `sized` :2016; `SPEED` 13 | `v(r, g) = 13 * (x <= 1 ? x^5 : x^3) * PACE[g] * run factor * tilt steepness`, `x = r/2.8` | TUNED | Real drops do not move below a pinning threshold, and above it speed grows with size (gravity pull scales with volume, drag with r). The `x^5` and `x^3` branches are a plausible shape, not fitted to data. They meet at x = 1 with a slope kink. The `x^3` branch is linear in water volume; below r = 2.8 the law is steeper than volume. |
| Pace | `PACE` :1632 | [dry 0.3, trail 1.6, fog 1] | TUNED | Drops run faster on a wetted track, so the order is right; the ratios are chosen. |
| Speed table | comment ~1592 | r 0.87 gives 0.04 px/s, r 1 gives 0.08, r 1.4 gives 0.4, r 2 gives 2.4, r 2.8 gives 13, r 4 gives 38 (on fog) | | Recomputed: 0.038, 0.076, 0.41, 2.4, 13, 37.9. All match. |
| Pinning | `RUN_R` :1649 | no motion below r = 1.4 (`1.4^3 = 2.74`, about sixteen tiny beads) | REAL-APPROX | Stands in for contact-angle pinning; real pinning depends on drop size, tilt and surface. |
| Track floor | `TRACK` :2017 | `sized(3, trail)`, about 25.6 px/s | TUNED | |
| Pooled pull | `gather()` | when pooled, a bead within 4 px edge gap of a larger or tiny bead drifts at 0.1 px/s until the gap is 0.25 px, then merges | TUNED | 0.1 is below the slowest running speed 0.122 (checked). |
| Snap | `snaps()`; `SNAP` 1 px | two beads within 1 px edge gap: the higher is pulled to the lower at 6 to 30 px/s (acceleration 120 px/s^2) and merged | ARTISTIC | Stands in for the capillary jump. |
| Pop, gulp, stretch | `POP` 0.12 s, `GULP_R` 3.9, `STRETCH` 0.14 | bulges and stretches of a bead | ARTISTIC | |
| Lock-on, runs, pauses | `LOCK_GAP` 5, `PAUSES`, `STEADY_R` 3.6, `MEANDER` 7 | stop-start, wandering, following a nearby trail | ARTISTIC | Real drops do stop, start and follow wet tracks; the rules are random behaviour. |

### 5.4 Heavy-rain beads

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Heavy gate | `HEAVY_RAIN` :1676 | 0.75 of the rain level, which is `8*(0.75 - 0.3)/0.7 = 5.14` mm/h | TUNED | The "about 5 mm/h" comment is right; the threshold itself has no physical meaning. |
| Landing | `land()` | `LANDING` 0.35 per second, at most `RAIN_MOST` 14 on the glass; bead radius `r = 2.6 + 0.37 * clamp((D0 - 0.9)/1.3, 0, 1)` (`rainR`, from the rain rate as above: 2.6 px in drizzle to 2.97 from D0 2.2 mm, about 70 mm/h; 2.70 at 5 mm/h, 2.82 at 20) | ARTISTIC | Not a real sticking rate; the growth of a bead with the drop is the real direction, the size range chosen. |
| Splash | ~2010 | `THROWN` [14, 20], `THROW` [24, 40] px/s, `FALL` 80 px/s^2 (:1687), `SPRAY` 0.25, `SPLASH_S` 0.25 s, `SQUASH` [0.5, 0.75] over 0.2 s | ARTISTIC | `FALL` 80 is not 9.8 m/s^2 at any scale. |
| Droplet drying | `DRYING` [15, 40] s | | TUNED | |

## 6. Device tilt (`screenGravity` :5477, `tilt` :4036)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Gravity on screen | `screenGravity` | non-iOS (`u = -ax`, `v = ay`): angle 0 gives `(u, v)`, 90 gives `(v, -u)`, 180 gives `(-u, -v)`, 270 gives `(-v, u)`; other angles by `x = u*cos(a) + v*sin(a)`, `y = -u*sin(a) + v*cos(a)`. iOS: `u = ax`, `v = -ay` before the same turn | REAL-MODEL | The direction of gravity from `accelerationIncludingGravity`. Checked against the code's own tables for all four angles on both platforms. |
| iOS sign | `screenGravity` | iOS reports the pull itself; Android reports the support force (minus gravity); the sign is flipped once | REAL-MODEL | Consistent with the flat-reading example (z = -9.56 on iOS, face up). |
| Steepness | `tilt()` | `s = sqrt(gx^2 + gy^2) / sqrt(gx^2 + gy^2 + gz^2) = sin(theta)`; theta is the screen normal's angle from vertical (0 lying face up, 90 upright, 180 face down) | REAL-MODEL | The in-plane share of gravity: the sliding force factor `g*sin(theta)`; independent of the reading's size. |
| Response curve | `POOL_IN`, `POOL_OUT`, `STEEP_FULL` :4033 | `POOL_IN = sin 5 deg = 0.0872`, `POOL_OUT = sin 7 deg = 0.1219`, `STEEP_FULL = sin 40 deg = 0.6428`; `t = clamp((s - 0.0872)/(0.6428 - 0.0872), 0, 1)`; `steep = smoothstep(t)`, low-passed with `k = 1 - exp(-dt/0.25)` | TUNED | The smoothstep is applied to `sin(theta)`, not theta: the ramp midpoint is s = 0.365 (theta 21.4 degrees). The real driving force keeps rising to 90 degrees; saturating at 40 is a choice. The 5/7 degree dead zone stands in for pinning and prevents flicker. |
| Smoothing | `SNAP_DEG` 4, `STEP_DEG` 1.5, `TAU` 0.25 s | low-pass of the direction; snaps to straight down within 4 degrees; changes only when it moves 1.5 degrees or more | TUNED | Noise suppression. |
| Face down | `tilt()` | steepness depends on `sin(theta)` only, so 180 degrees pools like 0 | REAL-APPROX | Real drops hang and fall from the underside. Rare, and documented in the code comment. |

Bead motion along the downhill unit vector (`b.x += grav.x*dy`, `b.y += grav.y*dy`) is REAL-MODEL for direction; wandering across the path is ARTISTIC.

## 7. Gauges, units and layout

### 7.1 Unit conversions (web_dashboard.html `UNITS` :4678)

The server always sends metric. The page converts the reading, scale, ticks, table figures, floor and the red hand. Sky thresholds (35 C, 15 km/h) read the metric figures and do not move with the unit switch.

| Name | Formula | Label | Faithful? |
|---|---|---|---|
| Celsius to Fahrenheit | `F = C*9/5 + 32` | REAL-MODEL | Exact. |
| km/h to mph | `mph = kmh / 1.609344` | REAL-MODEL | Exact. The wind tile shows whole mph. |
| hPa to inHg | `inHg = hPa / 33.8639` | REAL-MODEL | The exact figure is 33.86389; relative error below 3e-7. |
| mm to inches | `in = mm / 25.4` (rain, rain rate in/h) | REAL-MODEL | Exact; shown to 2 decimals (0.01 in is 0.254 mm). |
| Scale re-rounding | `inUnits()` :4685. Tick step: the first of `STEPS` [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000] with `floor((high - low)/s + 1e-9) + 1 <= most`. For dials and units flagged `round`: `low = floor(low/step)*step`, `high = max(low + step, round(high/step)*step)`. The thermometer is not rounded | TUNED | Wind 0 to 100 km/h becomes 0 to 60 mph (96.6 km/h), pressure 950 to 1050 hPa becomes 28 to 31 inHg (948.2 to 1049.8 hPa), rain 0 to 100 mm becomes 0 to 4 in (101.6 mm), rain rate 0 to 50 mm/h becomes 0 to 2 in/h (50.8 mm/h). The needle still matches the reading because `share()` uses the new ends. |

The server's own factors (`lib/ecowitt/hass_map.py` ~65-71) are listed in 8.2 and match these.

### 7.2 Needle, thermometer, beaker, scale ranges

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Linear share | `lib/gauge_look.py share()`, html `share` :4170 | `share = clamp((v - low)/(high - low), 0, 1)` | REAL-MODEL | A linear mapping. |
| Banded share (air gauges) | same | with bounds `b0..bn`, for `b_i < v <= b_(i+1)`: `share = (i + (v - b_i)/(b_(i+1) - b_i))/n` | TUNED | Equal-width bands are a layout choice (as AirNow does it). |
| Needle angle | `START` 215, `SWEEP` 250 | `theta = 215 - 250*share` degrees (0 = right, 90 = up); the dial spans 215 to -35 degrees, a 110 degree gap at the bottom | TUNED | Each colour band is `SWEEP/colours` wide minus 0.8 degree each side (ARTISTIC). |
| Thermometer level | ~4170-4225 | `y = 95 + (1 - share)*(238 - 95)` | REAL-MODEL | Same mapping as the scale bar, so ticks and mercury agree. At 0 C the mercury is at y 238, bulb and about 16 px of tube already filled. |
| Beaker level | ~4170-4225 | `y = 292 - max(share, 0.02)*(292 - 10 - 90)`, none at `v <= 0` | TUNED | Any rain above 0 shows at least 2 % (about 3.8 px), so a trace looks like about 2 mm. |
| Rest position | `floor()` | no reading rests at 0, or the scale's low (pressure 950), dimmed to `FADE` 0.4 | ARTISTIC | |
| Thermometer scale | `lib/ecowitt/dashboard.py scale()` | 0 to 40 C widened by 10 C: `low = min(0, floor(v/10)*10)`, `high = max(40, ceil(v/10)*10)`; band colours by `degree_colour()` | TUNED | A reading of 60 C makes a tall scale. Dew point, feels like and indoor share the 0 to 40 start. |
| Gauge ranges | `GAUGES` ecowitt/dashboard.py:50-85 | humidity 0 to 100; pressure 950 to 1050 hPa; wind 0 to 100 km/h; rain 0 to 100 mm; rain rate 0 to 50 mm/h; solar 0 to 1500 W/m^2; UV 0 to 12 with ticks 0, 3, 6, 8, 11 (twelve 1-UV colour slices on the WHO categories: 3 green, 3 yellow, 2 orange, 3 red, 1 violet) | TUNED | Display ranges, not physical limits (wind above 100 and UV above 12 pin at the end; full sun can reach about 1100 to 1400 W/m^2). |

### 7.3 Table statistics and pressure arrow

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Max, Min, Gust | `figures()`, `today_highs()` | maximum of the daily maxima over the row's days, plus the live reading; minimum likewise; Gust is the maximum of daily gust maxima plus the live gust | REAL-MODEL | Plain statistics; as good as the day rows. A brief spike between readings can be missed. |
| Rain total, Max | `figures()` | total `round(sum of daily rain, 1)`; Max is the wettest day (rows covering more than one day) | REAL-MODEL | |
| Rows | | Daily is today; Monthly is the current month to date; Yearly is 1 January to date | REAL-MODEL | |
| Limit mark | `dial()` | the red triangle sits at the angle of today's high (wind uses its gust), only on wind, rain rate, solar, UV | REAL-MODEL | Position is the same number as the table's Daily figure; shape is ARTISTIC. |
| Pressure tendency | `tendency_of()` web_dashboard.py; `TENDENCY_AGO` (150, 180, 210) min, `TENDENCY_READ` 240 min, `TENDENCY_STEADY` 0.1, `TENDENCY_CLASSES` | `change = round(newest - reading nearest 180 min before it, 1)` (among readings 150 to 210 min back; none otherwise); class by `|change|` to a tenth, the WMO 3-hour tendency scale: under 0.1 none (steady, no arrow), 0.1 to 1.5 slow, 1.6 to 3.5 moderate, 3.6 to 6.0 quick, over 6.0 rapid; JSON `{change, way: up/down/steady, class}` | REAL-MODEL | The meteorological 3-hour change, the daily pressure tide mostly gone. The arrow group (page): length by class 0.5 / 1 / 1.35 times the normal (0.8 of the digit height), rapid a double arrow (a second chevron 5.5 px behind the first at the normal length), 2 px after the last digit's ink, `lw(1.2)`, blue up (#3E86C0), red down (#FF3B30) (ARTISTIC). No arrow with no reading 150 to 210 minutes old. Change stays in hPa whatever unit is shown. |

### 7.4 Compass and wind rose

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Sector | `lib/ecowitt/direction.py sector()` | `int(((deg mod 360) + 11.25) // 22.5) mod 16`; N is sector 0 (348.75 to 11.25) | REAL-MODEL | 16 points of 22.5 degrees. |
| Needle | `compass()` :4369 | rotates to the reading (degrees clockwise from north, where the wind comes from), eased the short way: `from + (((to - from) mod 360) + 540) mod 360 - 180` | REAL-MODEL | |
| Rose counts | `directions_on()` ecowitt/reads.py:69, `rose()` | per sector, minutes of wind from it: each 5-minute reading counts 5; each 1-minute reading counts 1 only if no 5-minute reading is within 300 s; calm (speed 0 or less) is left out; a reading with no speed counts | REAL-APPROX | Time-weighted, not speed-weighted: a long light breeze equals a short gale. A single 5-minute sample counts 5 minutes. |
| Rose share | `rose()` web_dashboard.py:258, `ROSE_DECIMALS` 4 (web_dashboard.py:94) | `share_i = count_i / total`, rounded to 4 decimals; empty if total is 0 | REAL-MODEL | |
| Petal length | `ROSE_REACH` 0.8, `ROSE_HALF` 9 deg (web_dashboard.html:4362) | `out_i = 0.8*138*share_i/max(share)`; wedge half-width 9 degrees (18 degrees in a 22.5 degree sector) | TUNED | The longest petal is always 80 % of the radius: relative shape only, no absolute percentage scale. |

### 7.5 Layout, canvas, staleness, appearance

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Tile aspect | `CELL_W` 480, `CELL_H` 575 | `aspect = 480/575 = 0.8348` | TUNED | |
| Fit | `layout()` :4570; `PAD` 8, `STATUS` 18 | `W = clientWidth - 2*PAD`, `H = clientHeight - 2*PAD - STATUS`; for cols 1..n: `rows = ceil(n/cols)`, `w = min(W/cols, H/rows*aspect)`; the best w wins | TUNED | A simple optimisation. |
| Scroll fallback | `MIN_TILE` 160, `MIN_SCROLL_TILE` 150 | if best w is below 160: `cols = max(1, min(n, floor(W/150)))`, `w = W/cols`, page scrolls | TUNED | |
| Gallery | `galleryLayout()` :5158; `TABLE_TOP` 372 | zoomed aspect `480/372 = 1.29`; `w = min(W - 2*PAD, H*aspect)`; slot pitch W; flick if speed is above 0.4 px/ms and the drag is at least 24 px; settle to `round(position/pitch)`; duration distance*450 ms clamped to 150 to 320 ms; rubber band `(1 - 1/(over*0.55/pitch + 1))*pitch`; bounce 40 px over 300 ms; zoom 200 ms | TUNED | Feel constants. |
| Art pixel | `ART_PX` 480, ~3857 | `P = max(3, round(max(w, h)/480))`; canvas `ceil(w/P)` by `ceil(h/P)`. 1920 x 1080 gives P = 4, 480 x 270 art px | TUNED | |
| Overdraw margin | `OVER` 100 :290 | `M = ceil(100/P)` rows above and below, filled by mirroring | ARTISTIC | |
| Stale reading | `STALE_MS` 3600000 :4801 | stale if time is missing or `now - time` is not at most one hour | TUNED | Only the status line greys; gauges are not dimmed for old data. |
| Needle motion | `MOVE_MS` 900, `RISE_MS` 1000 | ease `1 - (1 - p)^3`; `REFETCH_MS` 60 s | TUNED | |
| Geometry | thermometer (top 95, bottom 238, cx 285, bulb radius 38), beaker (top 90, bottom 292), dial radii 130/138/102/84, hub 13, table chips, petal gradients | | ARTISTIC | Appearance only. |

## 8. Server data and derived readings

### 8.1 Dashboard server (`lib/web_dashboard.py`)

| Name | Where | Formula | Label | Faithful? |
|---|---|---|---|---|
| Sun for the beams | :128 | `sun_position(lat, lon, now)` (section 2.1), rounded to 3 decimals; location from the forecast, else the station's | REAL-MODEL | |
| Sunrise and sunset | `sun_of` | `solar.sun_times` for the station's location and zone (section 2.2); fallback 360 and 1080 with no location | REAL-MODEL, fallback TUNED | |

### 8.2 Derived readings and outlook

The station supplies dew point, feels like, `app_temp`, `app_tempin` and VPD (`lib/ecowitt/query.py:46`, `DERIVED`). The repo does not compute them, and has no heat index, wind chill or Steadman formula.

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Dew point (Magnus) | `lib/service/outlook.py:159`; `a` 17.62, `b` 243.12 C | `g = ln(RH/100) + a*T/(b + T)`, `Td = b*g/(a - g)`; RH clamped to at least 1 % | REAL-MODEL | Used only when the station sends no dew point. Good to about 0.4 C over a wide range; poor at very low RH ((35 C, 10 %) gives -1.17 C). Checks: (20 C, 50 %) gives 9.26 C; (30 C, 80 %) gives 26.17; (0 C, 100 %) gives 0.0. |
| Unit conversions | `lib/ecowitt/hass_map.py:65-71` | pressure to hPa: kPa x10, Pa x0.01, inHg x33.8639, mmHg x1.33322, psi x68.9476; speed to km/h: mph x1.609344, m/s x3.6, kn x1.852, ft/s x1.09728; length to mm: cm x10, in x25.4; rate in/h x25.4; temperature: F gives `(T - 32)*5/9`, K gives `T - 273.15`; VPD hPa to kPa x0.1 (api.py:82 corrects inHg VPD at x3.38639) | REAL-MODEL | Exact factors. |
| Outlook rain verdict | `lib/service/outlook.py:36-45, 136-309` | score: falling pressure 1 to 3 hPa adds 1, over 3 hPa adds 2; moist (RH 90 % or more, or `T - dew point <= 2 C`) adds 1; unsettled (under 1005 hPa) adds 1; wind picking up 5 km/h or more while falling adds 1; settled (over 1020 hPa) subtracts 1; clamp 0 to 2 maps to unlikely, possible, likely; any rain in the last hour forces likely. The pressure change is `last - first` over the 3 hours (`_pressure` :136, rounded to 0.1): falling if at most -1.0 hPa (`STEADY_HPA`), rising if at least +1.0, else steady | TUNED | Falling pressure with moist air does precede rain, but the cut-offs are not calibrated. A fall that recovers by the end now reads steady; a rise then fall still reads by its end points, so a swing inside the window is not seen. |
| Emoji triggers | `lib/ecowitt/glance.py:1-18` | hot 35/30/25 C, cold 8/0 C, wind 15/30/50 km/h, muggy 85 %, dry 30 %, pressure 1025 hPa, VPD 1.2 kPa, solar 600 and 200 W/m^2, UV 6 and 10 | TUNED | |

### 8.3 Air quality (`lib/airgradient/*`, `lib/series.py`)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| AQI interpolation | `_aqi` metrics.py:106, `truncate` :113 | `I = (I_hi - I_lo)/(C_hi - C_lo)*(C - C_lo) + I_lo`, rounded to the nearest integer (the EPA rounds the index); the concentration is first truncated (not rounded) to the table's resolution, 0.1 for PM2.5 and 1 for PM10 (`truncate`, with a 1e-9 guard against float error); above the top breakpoint returns 500 | REAL-MODEL | The EPA linear-interpolation method, with the EPA's truncation: 9.05 gives 9.0 (AQI 50), 35.45 gives 35.4 (AQI 100). |
| PM2.5 table | `pm25_aqi` :119 | 0.0 to 9.0 gives AQI 0 to 50; 9.1 to 35.4 gives 51 to 100; 35.5 to 55.4 gives 101 to 150; 55.5 to 125.4 gives 151 to 200; 125.5 to 225.4 gives 201 to 300; 225.5 to 325.4 gives 301 to 500 | REAL-MODEL | The 2024 EPA revision (checked row by row). |
| PM10 table | metrics.py:77-85 | 0 to 54, 55 to 154, 155 to 254, 255 to 354, 355 to 424, 425 to 604 give the same AQI bands; concentration truncated to a whole number | REAL-MODEL | Matches the EPA table (unchanged in 2024). |
| Combined AQI | `us_aqi` :134 | the higher of the PM2.5 and PM10 sub-indices; PM2.5 wins ties; the source is named | REAL-MODEL | The AirNow rule. |
| Applied to now | | the formulas run on the latest sensor reading, not a 24-hour average or NowCast | REAL-APPROX | It reads "the AQI if this level persisted". It differs from AirNow when PM2.5 is changing (the air-gauge header says so). |
| Gauge bounds | `lib/airgradient/dashboard.py:36-80, 85-89` | AQI 0, 50, 100, 150, 200, 300, 500; PM2.5 0, 9.0, 35.4, 55.4, 125.4, 225.4, 325.4; PM10 0, 54, 154, 254, 354, 424, 604 | REAL-MODEL | EPA edges. |
| PM1 bands | same | the PM2.5 bounds | TUNED | No standard exists for PM1; PM1 is part of PM2.5, so the scale is lenient. |
| CO2, VOC, NOx bands | same; `series.py:55-64` | CO2 400, 800, 1000, 1500, 2000, 2500 ppm (ratings 799/1499); VOC index 1, 100, 150, 250, 400, 500 (ratings 150/250); NOx index 1, 20, 150, 300, 500 (ratings 20/150) | TUNED | CO2 cut-offs are a common indoor rule of thumb (the code comment attributes them to ASHRAE, which sets no such bands). VOC and NOx are Sensirion relative indices (VOC 100 and NOx 1 are the baselines); the other edges are the service's own. |
| Ratings | `series.py:55-64`, `zone()` | good if at most the first limit, poor if at most the second, very poor above. PM2.5 9.0 and 55.4; PM10 54 and 254 | REAL-MODEL for PM2.5 and PM10 edges, TUNED for the rest | |
| Spike removal | `without_spikes` metrics.py:45; `SPIKE_RATIO` 2.5, `SPIKE_RISE` 5 ug/m^3, `REQUIRE_CONSECUTIVE` 2 | a stretch shorter than 2 readings of PM1, PM2.5 or PM10 is dropped if every reading is at least 2.5 times and at least 5 above the higher neighbour (neighbours within 900 s) | TUNED | Targets sensor glitches, but can drop a real short puff of smoke. |
| Gauge band names | `AirGauge.seen()` :65, `band()` :77 | the particles' concentration (PM2.5, PM10, PM1, not the AQI) is truncated to the step (0.1, 1, 0.1) by `seen()`; the needle and the band name both use that value; a value is in the band below an edge if `seen < edge + step`; step 1 for AQI and NOx, 0 for CO2 and VOC | REAL-APPROX | Mirrors the EPA's truncation to table resolution, and needle and band now agree. |

### 8.4 Stored readings and aggregation

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Interval mean | `lines._summarise` lines.py:94 | one value per 30-minute slot (finer readings win), then the mean of slot means | REAL-MODEL | Time-weighted, not weighted by reading count. |
| Interval low, high | history.py / storage | minimum of the readings' own lows (`COALESCE(_lo, value)`), maximum of their highs | REAL-MODEL | |
| Day mean, period mean | `storage/days.py:8`, `lines.time_mean`, `readings_mean` | mean of the day's half-hour means; period mean weights days by seconds covered | REAL-MODEL | |
| Gust "max", direction "last" | history.py:215 | maximum of the highs; the latest reading, with no averaging | REAL-MODEL | |
| Rain from the counter | `rain_slots` rain.py:34, `_is_reset` :25; `MAX_GAP` 3 slots (90 min), `RESET_MM` 2.0, `RESET_SHARE` 0.1, `RESET_HOURS` 2 | rain per slot is the rise of the running daily counter; a drop is the midnight reset (adds the whole new value) only if the new total is under 2 mm and either the reading is within 2 hours after local midnight (when the zone is given) or the new total is at most 10 % of the total before; any other drop is a glitch: it adds 0, the old total stays the baseline, and it is counted (`glitches`); a hole longer than 90 minutes counts 0 | REAL-APPROX | A mid-day re-sync no longer adds a spurious amount. A hole over 90 minutes loses real rain. A real reset the rule rejects (new total 2 mm or more, or not near midnight and above 10 %) is ignored, and because the baseline stays at the old total, the new day's rain is lost until the counter passes it (see Issues). |
| Rain events | `rain_spells` rain.py:59 | slots join one spell if the gap is 6 slots (3 dry hours) or less; a spell needs at least 1.0 mm | TUNED | |
| 5-minute band | storage/write.py:28 | exists only if `MIN_READINGS` 2 live readings fell in the slot | TUNED | |
| Line smoothing | `SMOOTH_POINTS` 3 | 3-point rolling mean (15 minutes of 5-minute readings), for temperature, humidity, dew point, feels like and pressure only | TUNED | Never for wind or rain. |
| Row caps | `MINUTE_HOURS` 8, `INTRADAY_DAYS` 8, `FINE_DAYS` 31, `MAX_ROWS` 300 | | TUNED | Limits, not maths. |

### 8.5 Forecast, alerts, day look

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Rain chance | `lib/forecast/source.py:107-125`; `MIN_HOURS` 20 | Open-Meteo ECMWF IFS 0.25 degree ensemble, 51 members. Per member the hourly precipitation is summed per local day (needs 20 hours). Chance `= 100*(members with at least 1 mm)/members`, rounded to the nearest 5 % | REAL-APPROX | A raw ensemble fraction, not calibrated: "40 %" means 20 of 51 members reach 1 mm. If the ensemble fails, the fallback is Open-Meteo's `precipitation_probability_max`, a different statistic (the label changes to "chance of rain"). |
| Temperatures | `whole()` source.py:46 | rounded half up (10.5 gives 11) | REAL-MODEL | |
| Pollen, asthma | `lib/pollen/*` | levels (Low, Moderate, High, Extreme) are the site's words, scraped; `SEASON_MONTHS` (10, 11, 12) | n/a | Not computed: official forecasts relayed. |
| Forecast alerts | `lib/alerts/forecast.py:20-54` | rain becomes likely if the chance rose from under 50 % to 50 % or more; unlikely if it fell from 50 % or more to 30 % or less; the high moves by more than 2 C; baseline is the first forecast after 06:00 | TUNED | The 30 to 50 % band stops flapping. |
| Day look | `lib/service/daylook.py:29-68` | rule order: stormy (rain 10 mm and gust 60 km/h or more), wet (rain 5 mm or more), showery (1 mm or more), windy (gust 50 or more), very hot (high 35 C or more), very cold (high under 10 C or low 0 C or less), then sunniness. Sky share = `solar_avg / clear_sky_mean`: sunny at 0.80 or more, mostly sunny 0.60, partly cloudy 0.35, else overcast | TUNED | |
| Clear-sky declination | daylook.py:54 | Cooper (1969): `d = 23.45 deg * sin(2*pi*(284 + doy)/365)` | REAL-APPROX | About 0.5 degree error, less accurate than the NOAA series in 2.1. |
| Clear-sky irradiance | daylook.py:44; `HAURWITZ_W` 1098, `HAURWITZ_K` 0.057 | Haurwitz (1945): `GHI = 1098*cos(z)*exp(-0.057/cos(z))` W/m^2; `cos(z) = sin(lat)*sin(d) + cos(lat)*cos(d)*cos(H)`; averaged over 288 five-minute steps | REAL-APPROX | Clean air at sea level; no Earth-Sun distance correction (about +-3.4 % over the year), no altitude, aerosols or humidity. Meaningful only for whole-day means. Values: Melbourne 21 Dec 367 W/m^2, 21 Jun 113 W/m^2. |

### 8.6 Analysis and alerts (`lib/analysis/*`, `lib/alerts/*`)

| Name | Where | Formula and constants | Label | Faithful? |
|---|---|---|---|---|
| Statistics scan | `analysis/scan.py` | rank (Spearman) and Pearson correlation; Student's t p-value via the incomplete beta function with effective n `n/(1 + 2*sum(rho_x*rho_y))` (Bretherton); Sidak correction over 13 lags (0 to 6 h) `p_adj = 1 - (1 - p)^13`; Newey-West (Bartlett) variance of the daily correlations and Fisher z; Benjamini-Hochberg false-discovery rate at q 0.1 | REAL-MODEL | Published methods. The incomplete-beta numerics were not re-derived (standard continued fractions). |
| Rain vs reading | `pairs.py:84-135`; `LOOKBACK` 6 slots (3 h) | each slot classed falling, steady or rising by the 3-hour change; fair-share ratio is rain share over time share; "clear link" at ratio 1.5 or more moving and 0.65 or less steady; "moderate" 1.25 and 0.85; "no link" 0.85 to 1.15 on both | TUNED | The Pearson correlation of the 3-hour change with the next 3 hours of rain is REAL-MODEL, shown only with at least 30 pairs. |
| Air vs rain | `pairs.py:143-197` | mean of the 6 hours before a rain event against the 6 hours after (3 hours of readings needed each side); "cleaner" if after is 0.9 times before or less; wet/dry ratio of means; rank correlation across at least 10 days; "rain clears the air" at share 0.7 or more and ratio 0.8 or less | TUNED | The rank correlation is REAL-MODEL. |
| Thresholds | scan.py | strength words: abs r 0.5 or more strong, 0.3 or more moderate, else weak; `MIN_DAYS` 21, `MIN_DAILY` 14, `CALM_KMH` 5, `MIN_SECTOR_SLOTS` 30 | TUNED | |
| Circular mean | `lib/ecowitt/direction.py:36` | mean direction `atan2(mean sin, mean cos)`; steadiness is the resultant length `R = hypot(mean cos, mean sin)`; steady at R 0.6 or more, fairly steady 0.35 or more, else variable | REAL-MODEL | Circular statistics; the word cut-offs are TUNED. Not weighted by speed. |
| Alert rules | `lib/alerts/*` | gust over 40 km/h (1-hour rearm); UV index 10 or more for 2 readings in a row (`CONFIRM_READINGS`, 1-hour rearm); outdoor-indoor gap over 1.0 C for the crossing alert; a rain alert on any tip; 60-minute default cooldown | TUNED | |

## 9. Issues found

Each item was checked against the code. The user reviewed the list and decided which to fix; the rest were left on purpose. Status: FIXED (with the commit), LEFT BY DECISION (the user chose to leave it, with the reason), N/A. New issues found while refreshing this file follow the list.

Wrong or misleading comments and numbers:

1. FIXED (03f2b5d): rain count comment now says 71 (`need()` at level 0.3 and 480 x 300 gives `(144000/1000)*(0.1 + 2.4*0.3^1.5) = 71`).
2. FIXED (03f2b5d): fog thickness comment says `0.5 + 0.5*v`, 0.525 at 90 %.
3. FIXED (03f2b5d): haze texture comment no longer says a cut "covers exactly that share" (the summed layers are bell-shaped, about 42/59/79/92/99 %).
4. FIXED (03f2b5d): swell comment: about 3.6 minutes at felt fog 0.4, 86 s only at 1.
5. FIXED (03f2b5d): bead speed comment names the `x^5` branch below r 2.8.
6. FIXED (03f2b5d): smoothstep comment says the 5 and 40 degrees apply to `sin(theta)`, midpoint theta 21.4.
7. FIXED (ccb7329): air-gauge header no longer says "Left out: PM1"; the CO2 note no longer attributes the bands to ASHRAE; the AQI line says "one reading, not NowCast or a 24-hour average".
8. FIXED (03f2b5d): `TABLE_TOP` 372 comment says the 6 px margin is by design.

Formulas that diverge from the standard they cite:

9. FIXED (ccb7329): PM2.5 (and PM10, PM1 gauges) truncated to 0.1 (1 for PM10) by `truncate`, the EPA's rule: 9.05 gives AQI 50, 35.45 gives 100.
10. LEFT BY DECISION: instantaneous AQI (one reading, not 24-hour or NowCast); the user chose to leave it, and the gauge header now says so.
11. FIXED (ccb7329): needle and band name both go by the truncated value (`AirGauge.seen`), so they agree.
12. FIXED (03f2b5d): UV dial is twelve 1-UV slices on the WHO categories (3 green, 3 yellow, 2 orange, 3 red, 1 violet).
13. LEFT BY DECISION: Fahrenheit thermometer tick and band edge mismatch; the user chose to leave it (cosmetic).
14. LEFT BY DECISION: inch dials span a slightly different range than metric; the user chose to leave it (rounded scale ends are intended).
15. LEFT BY DECISION: pressure arrow direction uses the unrounded change while the JSON shows 0.1; the user chose to leave it (no visible effect).
16. LEFT BY DECISION: pressure arrow (now the WMO 3-hour tendency classes) against the outlook (3 hours, 1.0 hPa for steady); the two scales differ, the user chose to leave them.
17. FIXED (ccb7329): outlook `_pressure` uses the change from the window's first reading to its last, 1.0 hPa for steady (a fall that recovers reads steady).
18. LEFT BY DECISION: Magnus fallback poor at very low RH; dormant while the station supplies a dew point.
19. FIXED (ccb7329): `rain_slots` takes a drop as the midnight reset only if the new total is under 2 mm and (within 2 h after local midnight, or at most 10 % of the total before); else a counted, ignored glitch (but see new issue N1).
20. FIXED (sunrise equation, `solar.sun_times`): sunrise and sunset come from the station's location; 06:00 / 18:00 remains only with no location.
21. LEFT BY DECISION: clear-sky model uses Cooper declination and no Earth-Sun distance term; the user chose to leave it (a few percent on a threshold).

Design simplifications that do not estimate reality:

22. LEFT BY DECISION: fog uses outdoor RH 90 % or more, not glass temperature against dew point; the user chose to leave it.
23. LEFT BY DECISION: ice uses air temperature below 0 C regardless of humidity or dew point; the user chose to leave it.
24. FIXED (03f2b5d) for the fog (on from 90 %, off below 85 %). The flood's 30/24 mm/h trigger was then replaced by the server's leaky bucket (a runoff model: see H8).
25. FIXED (03f2b5d): leaf pace scales with the wind speed, `k = clamp(0.4 + 0.024 s, 0.5, 1.6)`. Eddies and gust pulses stay random.
26. FIXED (96e255f): rain falls at a constant vertical speed, path speed `V0 / cos(angle)`, streaks grow to at most 1.6x, plain drop count (no density factor), drops blown out of the side start again.
27. LEFT BY DECISION: the 70 degree rain cap (bites above 66 km/h; the real law has no cap); the user chose to leave it.
28. FIXED (03f2b5d): one `GUST_SHARE` and `effectiveSpeed` (:335-336) for the rain, haze and leaves.
29. LEFT BY DECISION: `PAN_MAX`, `PAN_K` and the rain speeds are in art px/s with no physical length scale; the user chose to leave it.
30. LEFT BY DECISION: UV beam lean assumes east on the right of the screen; the user chose to leave it (right for the Melbourne station).
31. N/A: the heat fills the top quarter and its front follows the sun's lean (section 4.2).

New issues found while refreshing this file (recorded, not fixed):

N1. Rain counter, `rain.py:44-57`: a glitch keeps the old total as the baseline. If the real midnight reset is rejected (the first reading of the new day is 2 mm or more, or the reading is after 2 am local and the new total is above 10 % of yesterday's), the rest of the day's rain is lost until the new total passes yesterday's. Rare (needs 2 mm or more in the first slot after a gap of at most 90 minutes), but the loss is the whole day.
N2. Leaf pace is at least 0.76 whenever the wind effect is on (above 15 km/h), so the scaling only shows from 15 km/h up; below the threshold the leaves are not drawn at all, and the lower clamp 0.5 is reached only while the wind eases out.
N3. Rain path speed keeps growing to 2.9x at the cap while the streak length stops at 1.6x (from 51 degrees), so above 51 degrees a streak is shorter than the drop's path speed would suggest. Chosen by the user's rule, noted for honesty.
N4. `math.md` itself had two small errors, now corrected: the haze pan at 40 km/h is 2.63 (not 2.64), and the leaf pace at the 4 km/h floor is reached at 4.2 km/h.

Notes (not issues): `leafAir` measures the mean wind by differencing two `airAt` calls, so near a strong eddy it is reduced by the pull term and the eddy weight is slightly overstated. NOAA's refraction has a 0.6 arcmin step at -0.575 degree, irrelevant because `refracted=False`. The sun position was compared with an independent almanac algorithm: elevation within 0.45 degree, azimuth off only near the zenith.

## 10. Maintenance

When a formula in the code changes, update this file in the same commit: the formula, its constants, its label, and the line reference. Mark an issue FIXED (with the commit) in section 9 when it is fixed.

Constants to grep when checking that the numbers here still match:

| Area | Grep for |
|---|---|
| Rain slant, drop size | `d0Of`, `vtOf`, `beadROf`, `RAIN_NOMINAL`, `LAYERS` (`size`, `near`), `MID_SPEED`, `CAP = 70`, `OLD_SLANT`, `PATH`, `STREAK_MOST`, `GUST_SHARE`, `effectiveSpeed`, `TAU_UP`, `TEST_WIND`, `STEPS = 20`, `PACE` / `paceOf` |
| Effect levels | `conditionsOf`, `FLOOD_ON`, `FOG_OFF`, `windSideOf`, `GUST_MARK`, `BUDGET`, `ART_PX`, `FADE_S` |
| Haze | `PAN_MAX`, `PAN_K`, `bakeTexture`, `HOLLOW` |
| Heat | `heatOf`, `HEAT_STEPS`, `HEAT_PERIOD`, `HEAT_PEAK`, `heatWave`, `wanted`, `HEAT_SLEW`, `HEAT_SOFT` |
| UV and sun | `BEAM_MAX`, `BEAM_CURVE`, `EL_LOW`, `RUN_ON`, `SUN_FALLBACK`, `leanAt`, `leanOf` |
| Ice, flood | `reachFor`, `growRate`, `MELT`, `RISE = 4`, `DRAIN`, `CALMS`, `PUSH` |
| Beads | `RUN_R`, `GONE`, `TINY`, `SWELL`, `PACE`, `sized`, `HEAVY_RAIN`, `FOG_AT`, `FORM`, `CLEAR`, `THICKEN` |
| Tilt | `POOL_IN`, `POOL_OUT`, `STEEP_FULL`, `SNAP_DEG`, `screenGravity`, `tilt(` |
| Gauges | `STEPS = [0.1`, `START, SWEEP`, `MIN_TILE`, `TABLE_TOP`, `STALE_MS`, `TENDENCY_STEADY`, `TENDENCY_AGO`, `ROSE_REACH`, `33.8639`, `1.609344`, `25.4` |
| Server | `sun_position`, `refraction`, `17.62`, `us_aqi`, `pm25_aqi`, `PM25_AQI`, `SPIKE_RATIO`, `truncate`, `MAX_GAP`, `RESET_MM`, `MIN_READINGS`, `STEADY_HPA`, `HAURWITZ`, `MIN_HOURS`, `SEASON_MONTHS` |

Labels: if a TUNED or ARTISTIC number is later fitted to data or a standard, say which source and change the label. If a REAL-MODEL formula is simplified, downgrade it to REAL-APPROX and list the limits.
