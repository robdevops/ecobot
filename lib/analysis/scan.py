"""Which readings go with which? A scan of every air-quality metric against every weather reading, built so that a chance
pattern or the daily cycle is not reported as a link.

Everything works on 30-minute slots. Each pair is looked at two ways: within the day (both readings minus their own
time-of-day average and that day's own level, so the shared daily cycle and a spell of days both cancel, with the weather leading
the air by 0 to 6 hours) and day to day (daily means). How strong a result is judged by Student's t with an effective sample size: readings half an hour (or a
day) apart are not independent, so the count of readings is shrunk by how long each series stays much the same (Bretherton's
estimate from the two series' autocorrelations, up to a day apart, or a fortnight for daily means), and the best of the 13 lags is
allowed for (Šidák). Then the number of
pairs tested is allowed for (Benjamini-Hochberg), so only pairs that survive are called links. Rank correlations throughout:
rain and spikes are skewed, and a rank correlation is not thrown by them. Pure Python (math.sumprod for the long sums)."""

import math
from datetime import tzinfo
from itertools import groupby
from statistics import fmean

from ..timeutil import MIN_DAY_SLOTS, SLOT, naive_local

PER_DAY = 48
LAGS = range(13)              # the weather leads the air by 0 to 6 hours
MIN_DAYS = 21
MIN_DAILY = 14                # days with enough readings for the day-to-day view
MIN_OVERLAP = 200             # slots two series must share for a within-day comparison
FDR = 0.1
CALM_KMH = 5                  # wind direction only counts with at least this much wind
MIN_SECTOR_SLOTS = 30
SECTORS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
MAX_FINDINGS = 6

Series = list   # a grid: one value (or None) per 30-minute slot of the local clock


def rank(values: list) -> list:
    """Ranks, ties averaged, 1 = smallest; a missing value (None or NaN) stays None."""
    order = sorted((i for i, v in enumerate(values) if v is not None and v == v), key=values.__getitem__)
    out: list = [None] * len(values)
    start = 0
    for _, run in groupby(order, key=values.__getitem__):
        run = list(run)
        middle = start + (len(run) + 1) / 2          # ties share the average of the places they take
        for i in run:
            out[i] = middle
        start += len(run)
    return out


def p_value(r: float, n_eff: float) -> float:
    """Two-sided p of a correlation `r` from `n_eff` independent pairs: Student's t with n_eff - 2 degrees of freedom (with few
    effective pairs its tails are much heavier than the normal curve's, as they should be)."""
    if n_eff <= 3 or r != r:
        return 1.0
    r = min(abs(r), 1 - 1e-12)
    df = n_eff - 2
    return incomplete_beta(df / 2, 0.5, df / (df + df * r * r / (1 - r * r)))


def incomplete_beta(a: float, b: float, x: float) -> float:
    """The regularised incomplete beta function I_x(a, b) (a continued fraction, Lentz's method)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    if x > (a + 1) / (a + b + 2):   # the fraction converges quickly only on this side
        return 1 - incomplete_beta(b, a, 1 - x)
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)) / a
    tiny = 1e-300
    c, d = 1.0, 1 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        for num in (m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)), -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))):
            d = 1 + num * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + num / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1) < 1e-14:
            break
    return front * h


def effective(n: float, acf_x: list[float], acf_y: list[float]) -> float:
    """The effective number of independent pairs for two series of `n` readings with these autocorrelations (at lags 1, 2, ...):
    n / (1 + 2 Σ ρx(k)·ρy(k)), the general form of Bretherton's estimate. Two series that each stay much the same for hours (or
    days) share far fewer independent turns than they have readings; where only one of them persists, nearly all count."""
    shared = sum(a * b for a, b in zip(acf_x, acf_y))
    return min(float(n), max(2.0, n / (1 + 2 * shared))) if 1 + 2 * shared > 0 else float(n)


def sidak(p: float, tries: int) -> float:
    """The chance of doing as well as `p` in the best of `tries` looks."""
    return 1 - (1 - p) ** tries


def chi2_sf(x: float, df: int) -> float:
    """P(X >= x) for a chi-square with `df` degrees of freedom: the regularised upper incomplete gamma Q(df/2, x/2)."""
    if x <= 0:
        return 1.0
    a, x = df / 2, x / 2
    if x < a + 1:   # the series for the lower part
        term = total = 1 / a
        n = a
        while abs(term) > abs(total) * 1e-15:
            n += 1
            term *= x / n
            total += term
        return max(0.0, 1 - total * math.exp(-x + a * math.log(x) - math.lgamma(a)))
    b, c, d = x + 1 - a, 1e300, 1 / (x + 1 - a)   # the continued fraction for the upper part (Lentz)
    h = d
    for i in range(1, 300):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = b + an / c
        c = c if abs(c) > 1e-300 else 1e-300
        h *= d * c
        if abs(d * c - 1) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def _pearson(x: list[float], y: list[float]) -> float | None:
    """Pearson's r of two lists of numbers; None when either does not vary."""
    if len(x) < 3:
        return None
    mx, my = fmean(x), fmean(y)
    dx, dy = [v - mx for v in x], [v - my for v in y]
    sx, sy = math.sumprod(dx, dx), math.sumprod(dy, dy)
    return math.sumprod(dx, dy) / math.sqrt(sx * sy) if sx > 0 and sy > 0 else None


def strength_word(r: float) -> str:
    r = abs(r)
    return "strong" if r >= 0.5 else "moderate" if r >= 0.3 else "weak"


def benjamini_hochberg(pvalues: list[float], q: float = FDR) -> list[bool]:
    """Which of these p-values still count once the number of tests is allowed for (false discovery rate q)."""
    n = len(pvalues)
    order = sorted(range(n), key=lambda i: pvalues[i])
    cutoff = -1
    for place, i in enumerate(order, start=1):
        if pvalues[i] <= q * place / n:
            cutoff = place
    keep = [False] * n
    for i in order[:cutoff] if cutoff > 0 else []:
        keep[i] = True
    return keep


def clock_slots(origin: int, days: int, tz: tzinfo) -> list[int]:
    """Each 30-minute slot from `origin` (a midnight in tz) as a slot of the local clock, 48 a day: across a daylight-saving change
    a time of day keeps its place (the skipped hour's slots stay empty, the repeated hour's are filled twice)."""
    start = naive_local(origin, tz)
    return [int((naive_local(origin + k * SLOT, tz) - start).total_seconds()) // SLOT for k in range(days * PER_DAY + 2)]


def to_grid(series: dict[int, float], origin: int, days: int, clock: list[int]) -> Series:
    """A series as a list of the local clock's 30-minute slots from `origin` (clock: clock_slots), None where there is no reading."""
    out: Series = [None] * (days * PER_DAY)
    for t, v in series.items():
        k = (t - origin) // SLOT
        if 0 <= k < len(clock) and clock[k] < len(out):
            out[clock[k]] = float(v)
    return out


def anomalies(x: Series) -> Series:
    """The series minus its average for that time of day."""
    means = []
    for s in range(PER_DAY):
        known = [v for v in x[s::PER_DAY] if v is not None]
        means.append(fmean(known) if known else None)
    return [None if v is None else v - means[i % PER_DAY] for i, v in enumerate(x)]


def within_days(x: Series) -> Series:
    """The series' movement within each day: its time-of-day anomaly minus that day's average anomaly. What a spell of days does
    (the level they sit at) is left to the day-to-day view, so a slow drift is not counted as thousands of half-hours agreeing."""
    a = anomalies(x)
    out: Series = []
    for d in range(len(a) // PER_DAY):
        day = a[d * PER_DAY:(d + 1) * PER_DAY]
        known = [v for v in day if v is not None]
        mean = fmean(known) if known else 0.0
        out += [None if v is None else v - mean for v in day]
    return out


SLOT_LAGS = PER_DAY - 1      # how far within-day persistence is looked for: up to a day
DAY_LAGS = 14                # and day-to-day persistence: up to a fortnight (at most a quarter of the days)


def autocorrelations(x: list[float], m: list[float], lags: int) -> list[float]:
    """The autocorrelation of a centred series at lags 1 to `lags` (the usual estimate: the sum of the products `k` apart over the sum
    of squares). A missing reading is 0 in `x`; `m` marks the readings, so each lag is scaled by the pairs it actually has."""
    total, n = math.sumprod(x, x), sum(m)
    if total <= 0 or n < 2:
        return [0.0] * lags
    share = n / len(x)                                # the pairs k apart that both have a reading: about (len - k) x share²
    return [math.sumprod(x[:-k], x[k:]) / total * n / max((len(x) - k) * share * share, 1.0) for k in range(1, min(lags, len(x) - 1) + 1)]


def _series_acf(values: list[float], lags: int) -> list[float]:
    """The autocorrelations at lags 1 to `lags` of a short series with no gaps (days in order)."""
    mean = fmean(values)
    x = [v - mean for v in values]
    return autocorrelations(x, [1.0] * len(x), min(lags, len(x) // 4))


def centred_ranks(x: Series) -> tuple[list[float], list[float]]:
    """(ranks minus their mean, missing readings as 0; a 1/0 mask of the readings there are)."""
    r = rank(x)
    known = [v for v in r if v is not None]
    mean = fmean(known) if known else 0.0
    return [0.0 if v is None else v - mean for v in r], [0.0 if v is None else 1.0 for v in r]


def _ranked(grid: Series) -> tuple[list[float], list[float], list[float], list[float]]:
    """(anomaly ranks centred, mask, their squares, autocorrelations up to a day): what a series contributes to every pair it is in."""
    x, m = centred_ranks(within_days(grid))
    return x, m, [v * v for v in x], autocorrelations(x, m, SLOT_LAGS)


def _leads(weather: tuple) -> list[tuple[list[float], list[float], list[float]]]:
    """The weather's (values, mask, squares) cut for each lag (weather[:n-lag]): made once, used against every air series."""
    x, mx, xx, _ = weather
    return [(x[:len(x) - lag], mx[:len(x) - lag], xx[:len(x) - lag]) for lag in LAGS]


def _lag_corr(lead: tuple, air: tuple, lag: int) -> float:
    """The correlation of the weather leading the air by `lag` slots: the weather cut for the lag (`lead`, from _leads) against
    air[lag:], over the slots where both have a reading."""
    (xs, mxs, xxs), (y, my, yy, _) = lead, air
    sx, sy = math.sumprod(xxs, my[lag:]), math.sumprod(mxs, yy[lag:])
    return math.sumprod(xs, y[lag:]) / (math.sqrt(sx * sy) + 1e-12)


def within_day(weather: tuple, air: tuple, leads: list | None = None) -> tuple[float, int, float] | None:
    """(correlation at the best lag, that lag in slots, p) of the weather's and air's anomalies, the best of the lags allowed for.
    None when they share too few readings. `weather` and `air` are _ranked results; `leads` the weather's _leads, if made."""
    if math.sumprod(weather[1], air[1]) < MIN_OVERLAP:
        return None
    leads = leads or _leads(weather)
    found = [_lag_corr(leads[lag], air, lag) for lag in LAGS]
    best = max(range(len(found)), key=lambda k: abs(found[k]))
    r, n = found[best], math.sumprod(leads[best][1], air[1][best:])   # and the slots they share at that lag
    p = sidak(p_value(r, effective(n, weather[3], air[3])), len(LAGS))
    return float(r), best, p


def daily_means(grid: Series) -> list[float | None]:
    """One mean per day (None for a day with under 12 hours of readings)."""
    out = []
    for d in range(len(grid) // PER_DAY):
        known = [v for v in grid[d * PER_DAY:(d + 1) * PER_DAY] if v is not None]
        out.append(fmean(known) if len(known) >= MIN_DAY_SLOTS else None)
    return out


def day_to_day(weather: list, air: list) -> tuple[float | None, int, float | None]:
    """(correlation of the daily means, days used, p), or (None, days, None) with too few days or no variation. `weather` and
    `air` are daily_means results; the days are not independent either (a dry spell lasts), so each series' day-to-day
    persistence shrinks the count."""
    both = [d for d in range(len(weather)) if weather[d] is not None and air[d] is not None]
    if len(both) < MIN_DAILY:
        return None, len(both), None
    x, y = rank([weather[d] for d in both]), rank([air[d] for d in both])
    r = _pearson(x, y)
    if r is None:
        return None, len(both), None
    return r, len(both), p_value(r, effective(len(both), _series_acf(x, DAY_LAGS), _series_acf(y, DAY_LAGS)))


def kruskal_wallis(groups: list[list[float]]) -> float:
    """The Kruskal-Wallis H of these groups of values (ranked together)."""
    pooled = [v for g in groups for v in g]
    ranks = rank(pooled)
    n = len(pooled)
    h, k = 0.0, 0
    for g in groups:
        total = sum(ranks[k:k + len(g)])
        h += total * total / len(g)
        k += len(g)
    return 12 / (n * (n + 1)) * h - 3 * (n + 1)


def direction_effect(air: Series, direction: Series, speed: Series) -> dict | None:
    """Does the air differ by where the wind comes from? Average anomaly per 8 compass sectors (with some wind), the spread
    between the highest and lowest, and p by a Kruskal-Wallis test across the sectors. Its units are a sector on a day (the air's
    average there): half-hours of one wind on one day are not independent. Their count is shrunk further by how much the air's
    daily level and the day's wind persist from one day to the next. None when there is nothing to compare."""
    a = anomalies(air)
    groups: dict[int, list[float]] = {}
    cells: dict[tuple[int, int], list[float]] = {}
    for i, (v, d, s) in enumerate(zip(a, direction, speed)):
        if v is None or d is None or s is None or s < CALM_KMH:
            continue
        k = int(((d % 360) + 22.5) // 45) % 8
        groups.setdefault(k, []).append(v)
        cells.setdefault((i // PER_DAY, k), []).append(v)
    kept = {k: g for k, g in groups.items() if len(g) >= MIN_SECTOR_SLOTS}
    if len(kept) < 2:
        return None
    means = {k: fmean(g) for k, g in kept.items()}
    high, low = max(means, key=means.get), min(means, key=means.get)
    by_sector: dict[int, list[float]] = {}
    days: dict[int, dict[int, list[float]]] = {}
    for (day, k), values in sorted(cells.items()):
        if k in kept:
            by_sector.setdefault(k, []).append(fmean(values))
            days.setdefault(day, {})[k] = values
    n = sum(len(g) for g in by_sector.values())
    order = sorted(days)
    level = {d: fmean([v for vs in days[d].values() for v in vs]) for d in order}
    wind = {d: max(days[d], key=lambda k: len(days[d][k])) for d in order}       # the day's commonest sector
    shares = [list(wind.values()).count(k) / len(wind) for k in set(wind.values())]
    chance = sum(s * s for s in shares)                                       # two days with the same wind by chance
    lags = min(DAY_LAGS, len(order) // 4)
    acf_air, acf_wind = [], []
    for k in range(1, lags + 1):                                              # how both persist from day to day, k days apart
        pairs = [(d, d + k) for d in order if d + k in days]
        acf_air.append(_pearson([level[d] for d, _ in pairs], [level[e] for _, e in pairs]) or 0.0)
        same = sum(wind[d] == wind[e] for d, e in pairs) / max(len(pairs), 1)
        acf_wind.append((same - chance) / (1 - chance) if chance < 1 else 1.0)   # how much more often than chance the wind holds
    groups_used = [g for g in by_sector.values() if g]
    if len(groups_used) < 2 or n < 3:
        return None
    h = kruskal_wallis(groups_used) * effective(n, acf_air, acf_wind) / n
    return {"spread": means[high] - means[low], "p": chi2_sf(h, len(groups_used) - 1), "highest": SECTORS[high], "lowest": SECTORS[low],
            "highest_by": float(means[high]), "lowest_by": float(means[low])}


def scan(air: dict[str, dict[int, float]], weather: dict[str, dict[int, float]], direction: dict[int, float] | None,
         speed: dict[int, float] | None, origin: int, days: int, tz: tzinfo) -> dict:
    """The whole scan. `air` and `weather` map a name to {epoch: value} at 30-minute slots; `origin` is the epoch of the
    first slot (a midnight in `tz`) and `days` the number of days. Returns {"tests": [...], "directions": [...], "note"?: ...}
    where each test is {air, weather, r, lag_hours, r_daily, days, p, survives}."""
    clock = clock_slots(origin, days, tz)   # series with no readings (a metric the sensor doesn't have) are left out
    grids = {k: to_grid(v, origin, days, clock) for k, v in air.items() if v}
    wgrids = {k: to_grid(v, origin, days, clock) for k, v in weather.items() if v}
    overlap = max((sum(1 for m in daily_means(g) if m is not None) for g in grids.values()), default=0)
    if overlap < MIN_DAYS:
        return {"tests": [], "directions": [], "note": f"Only {overlap} days have enough air-quality readings; at least {MIN_DAYS} are needed."}
    air_ranked = {k: _ranked(g) for k, g in grids.items()}   # what each series contributes to every pair, worked out once
    weather_ranked = {k: _ranked(g) for k, g in wgrids.items()}
    leads = {k: _leads(r) for k, r in weather_ranked.items()}
    air_daily, weather_daily = ({k: daily_means(g) for k, g in gs.items()} for gs in (grids, wgrids))
    tests = []
    for a_name in grids:
        for w_name in wgrids:
            r_daily, n_days, p_daily = day_to_day(weather_daily[w_name], air_daily[a_name])
            inside = within_day(weather_ranked[w_name], air_ranked[a_name], leads[w_name])
            if inside is None and p_daily is None:
                continue
            r, lag, p = inside or (None, 0, 1.0)
            best_p = min(p, p_daily if p_daily is not None else 1.0) * 2  # two views of one pair: allow for both
            tests.append({"air": a_name, "weather": w_name, "r": r, "lag_hours": lag * SLOT / 3600, "r_daily": r_daily,
                          "days": n_days, "p": min(1.0, best_p)})
    directions = []
    if direction and speed:
        d, s = to_grid(direction, origin, days, clock), to_grid(speed, origin, days, clock)
        for a_name, a in grids.items():
            if (found := direction_effect(a, d, s)) is not None:
                directions.append({"air": a_name, **found})
    if not tests and not directions:   # "nothing stands out" would claim a comparison that wasn't made
        return {"tests": [], "directions": [], "note": "No weather readings overlap the air-quality readings enough to compare."}
    keep = benjamini_hochberg([t["p"] for t in tests] + [d["p"] for d in directions])
    for t, k in zip([*tests, *directions], keep):
        t["survives"] = k
    return {"tests": tests, "directions": directions}


def _strength(t: dict) -> float:
    return max(abs(t["r"]) if t.get("r") is not None else 0.0, abs(t["r_daily"]) if t.get("r_daily") is not None else 0.0)


def describe_pair(t: dict, air: str, weather: str) -> str:
    """One pair in words: which way, how strong, and the evidence."""
    r = t["r_daily"] if t["r_daily"] is not None and (t["r"] is None or abs(t["r_daily"]) >= abs(t["r"])) else t["r"]
    bits = []
    if t["r_daily"] is not None:
        bits.append(f"day to day r {t['r_daily']:+.2f} over {t['days']} days")
    if t["r"] is not None:
        bits.append(f"within the day r {t['r']:+.2f}" + (f", the air about {t['lag_hours']:g} h later" if t["lag_hours"] else ""))
    return f"{air} is {'higher' if r > 0 else 'lower'} when {weather} is higher ({strength_word(r)}; {'; '.join(bits)})"


def summarise(result: dict, air_names: dict[str, tuple[str, str]], weather_names: dict[str, tuple[str, str]]) -> dict:
    """The scan for the model: a plain verdict, the relationships that survive (strongest first), and if none do, the two
    closest as probably chance. Names map a series to (label, unit)."""
    if "note" in result:
        return {"verdict": "Not enough data to say", "findings": [result["note"]]}
    tests, directions = result["tests"], result["directions"]
    label = lambda names, k: names[k][0]
    kept = sorted((t for t in tests if t["survives"]), key=_strength, reverse=True)
    findings = [describe_pair(t, label(air_names, t["air"]), label(weather_names, t["weather"])) for t in kept]
    for d in (d for d in directions if d["survives"]):
        unit = air_names[d["air"]][1]
        findings.append(f"{label(air_names, d['air'])} is highest with winds from the {d['highest']} ({d['highest_by']:+.1f} {unit} on its "
                        f"usual for the time of day) and lowest from the {d['lowest']} ({d['lowest_by']:+.1f})")
    n = len(kept) + sum(d["survives"] for d in directions)
    if not n:
        closest = sorted(tests, key=lambda t: t["p"])[:2]
        findings = [f"Closest, probably chance: {describe_pair(t, label(air_names, t['air']), label(weather_names, t['weather']))}"
                    for t in closest]
    return {"verdict": (f"Yes - {n} relationship{'s' if n != 1 else ''} stand{'' if n != 1 else 's'} out" if n else
                        "Nothing stands out - no reading went with air quality more than chance would"),
            "findings": findings[:MAX_FINDINGS], "pairs_tested": len(tests) + len(directions),
            "pairs_standing_out": n, "strongest": kept[0] if kept else None}
