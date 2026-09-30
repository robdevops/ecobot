"""Which readings go with which? A scan of every air-quality metric against every weather reading, built so that a chance
pattern or the daily cycle is not reported as a link.

Everything works on 30-minute slots. Each pair is looked at two ways: within the day (both readings minus their own
time-of-day average, so the shared daily cycle cancels, with the weather leading the air by 0 to 6 hours) and day to
day (daily means). How strong a result is judged by shuffling whole days of the weather against the air (readings in
one day are not independent) and asking how often chance does as well; then the number of pairs tested is allowed for
(Benjamini-Hochberg), so only pairs that survive are called links. Rank correlations throughout: rain and spikes are
skewed, and a rank correlation is not thrown by them."""

import warnings

import numpy as np

from ..timeutil import MIN_DAY_SLOTS, SLOT

PER_DAY = 48
LAGS = range(13)              # the weather leads the air by 0 to 6 hours
MIN_DAYS = 21
MIN_DAILY = 14                # days with enough readings for the day-to-day view
MIN_OVERLAP = 200             # slots two series must share for a within-day comparison
PERMUTATIONS = 300
DAILY_PERMUTATIONS = 2000
FDR = 0.1
CALM_KMH = 5                  # wind direction only counts with at least this much wind
MIN_SECTOR_SLOTS = 30
SECTORS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
MAX_FINDINGS = 6


def rank(x: np.ndarray) -> np.ndarray:
    """Ranks, ties averaged, 1 = smallest; NaN stays NaN."""
    out = np.full(len(x), np.nan)
    ok = ~np.isnan(x)
    values = x[ok]
    order = np.argsort(values, kind="mergesort")
    _, inverse, counts = np.unique(values[order], return_inverse=True, return_counts=True)
    ends = np.cumsum(counts)
    ranks = np.empty(len(values))
    ranks[order] = ((ends - counts + 1) + ends)[inverse] / 2
    out[ok] = ranks
    return out


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


def to_grid(series: dict[int, float], origin: int, days: int) -> np.ndarray:
    """A series as an array of 30-minute slots from `origin`, NaN where there is no reading."""
    out = np.full(days * PER_DAY, np.nan)
    for t, v in series.items():
        k = (t - origin) // SLOT
        if 0 <= k < len(out):
            out[k] = v
    return out


def anomalies(x: np.ndarray) -> np.ndarray:
    """The series minus its average for that time of day."""
    by_slot = x.reshape(-1, PER_DAY)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return (by_slot - np.nanmean(by_slot, axis=0)).reshape(-1)


def centred_ranks(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(ranks minus their mean with missing readings as 0, a 1/0 mask of the readings there are)."""
    r = rank(x)
    return np.nan_to_num(r - np.nanmean(r)), (~np.isnan(r)).astype(float)


def _ranked(grid: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(anomaly ranks centred, mask, their squares): what a series contributes to every pair it is in."""
    x, m = centred_ranks(anomalies(grid))
    return x, m, x * x


def _shuffled(x: np.ndarray, permutations: np.ndarray) -> np.ndarray:
    """One row per permutation: whole days of the series moved around."""
    return x.reshape(len(x) // PER_DAY, PER_DAY)[permutations].reshape(len(permutations), -1)


def _lag_corr(x: np.ndarray, xx: np.ndarray, mx: np.ndarray, y: np.ndarray, yy: np.ndarray, my: np.ndarray) -> np.ndarray:
    """For each row of x (P shuffles of the weather; xx its squares, mx marks the readings), the correlation with y at
    every lag, over the slots where both have a reading: (P, len(LAGS)). The weather leads the air by L slots:
    x[:n-L] against y[L:]."""
    n = x.shape[1]
    out = np.zeros((x.shape[0], len(LAGS)))
    for j, lag in enumerate(LAGS):
        m = n - lag
        num = x[:, :m] @ y[lag:]
        sx, sy = xx[:, :m] @ my[lag:], mx[:, :m] @ yy[lag:]
        out[:, j] = num / (np.sqrt(sx * sy) + 1e-12)
    return out


def within_day(weather: tuple, air: tuple, shuffled: tuple) -> tuple[float, int, float] | None:
    """(correlation at the best lag, that lag in slots, p) of the weather's and air's anomalies; p by shuffling days. None
    when they share too few readings. `weather` and `air` are _ranked results; `shuffled` is the weather's (x, xx, mx)
    with its days shuffled."""
    (x, mx, xx), (y, my, yy) = weather, air
    if mx @ my < MIN_OVERLAP:
        return None
    observed = _lag_corr(x[None, :], xx[None, :], mx[None, :], y, yy, my)[0]
    best = int(np.abs(observed).argmax())
    strongest = np.abs(_lag_corr(*shuffled, y, yy, my)).max(axis=1)
    return float(observed[best]), best, (1 + int((strongest >= abs(observed[best])).sum())) / (len(strongest) + 1)


def daily_means(grid: np.ndarray) -> np.ndarray:
    """One mean per day (NaN for a day with under 12 hours of readings)."""
    counts = (~np.isnan(grid)).reshape(-1, PER_DAY).sum(axis=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        means = np.nanmean(grid.reshape(-1, PER_DAY), axis=1)
    return np.where(counts >= MIN_DAY_SLOTS, means, np.nan)


def day_to_day(weather: np.ndarray, air: np.ndarray, rng: np.random.Generator,
               shuffles: int = DAILY_PERMUTATIONS) -> tuple[float | None, int, float | None]:
    """(correlation of the daily means, days used, p by shuffling days), or (None, days, None) with too few days.
    `weather` and `air` are daily_means results."""
    both = ~np.isnan(weather) & ~np.isnan(air)
    if both.sum() < MIN_DAILY:
        return None, int(both.sum()), None
    x, y = rank(weather[both]), rank(air[both])
    x, y = x - x.mean(), y - y.mean()
    if not x.any() or not y.any():
        return None, int(both.sum()), None
    corr = lambda xs: xs @ y / (np.sqrt((xs * xs).sum(axis=-1)) * np.sqrt((y * y).sum()) + 1e-12)
    observed = float(corr(x))
    strongest = np.abs(corr(rng.permuted(np.tile(x, (shuffles, 1)), axis=1)))
    return observed, int(both.sum()), (1 + int((strongest >= abs(observed)).sum())) / (shuffles + 1)


def direction_effect(air: np.ndarray, direction: np.ndarray, speed: np.ndarray, permutations: np.ndarray) -> dict | None:
    """Does the air differ by where the wind comes from? Average anomaly per 8 compass sectors (with some wind), the
    spread between the highest and lowest, and p by shuffling days. None when there is nothing to compare."""
    a = anomalies(air)
    usable = ~np.isnan(direction) & ~np.isnan(speed) & (speed >= CALM_KMH)
    sectors = np.where(usable, ((np.nan_to_num(direction) % 360 + 22.5) // 45).astype(int) % 8, -1)   # N covers 337.5 to 22.5
    known = ~np.isnan(a)

    def means(secs: np.ndarray) -> np.ndarray:
        take = known & (secs >= 0)
        counts = np.bincount(secs[take], minlength=8)
        sums = np.bincount(secs[take], weights=a[take], minlength=8)
        return np.where(counts >= MIN_SECTOR_SLOTS, sums / np.maximum(counts, 1), np.nan)

    def spread(secs: np.ndarray) -> float:
        m = means(secs)
        return float(np.nanmax(m) - np.nanmin(m)) if np.isfinite(m).sum() >= 2 else 0.0
    by_sector = means(sectors)
    if np.isfinite(by_sector).sum() < 2:
        return None
    observed = spread(sectors)
    exceed = sum(spread(shuffled) >= observed for shuffled in _shuffled(sectors, permutations))
    high, low = int(np.nanargmax(by_sector)), int(np.nanargmin(by_sector))
    return {"spread": observed, "p": (1 + exceed) / (len(permutations) + 1), "highest": SECTORS[high], "lowest": SECTORS[low],
            "highest_by": float(by_sector[high]), "lowest_by": float(by_sector[low])}


def scan(air: dict[str, dict[int, float]], weather: dict[str, dict[int, float]], direction: dict[int, float] | None,
         speed: dict[int, float] | None, origin: int, days: int, seed: int = 1, shuffles: int = PERMUTATIONS) -> dict:
    """The whole scan. `air` and `weather` map a name to {epoch: value} at 30-minute slots; `origin` is the epoch of the
    first slot and `days` the number of days. Returns {"tests": [...], "directions": [...], "note"?: ...} where each test
    is {air, weather, r, lag_hours, r_daily, days, p, survives}."""
    grids = {k: to_grid(v, origin, days) for k, v in air.items()}
    wgrids = {k: to_grid(v, origin, days) for k, v in weather.items()}
    overlap = max((int(((~np.isnan(g)).reshape(-1, PER_DAY).sum(axis=1) >= MIN_DAY_SLOTS).sum()) for g in grids.values()), default=0)
    if overlap < MIN_DAYS:
        return {"tests": [], "directions": [], "note": f"Only {overlap} days have enough air-quality readings; at least {MIN_DAYS} are needed."}
    rng = np.random.default_rng(seed)
    permutations = np.array([rng.permutation(days) for _ in range(shuffles)])
    # what each series contributes to every pair, worked out once
    air_ranked = {k: _ranked(g) for k, g in grids.items()}
    air_daily = {k: daily_means(g) for k, g in grids.items()}
    weather_prepared = {}
    for k, g in wgrids.items():
        ranked = _ranked(g)
        xs, ms = _shuffled(ranked[0], permutations), _shuffled(ranked[1], permutations)
        weather_prepared[k] = (ranked, (xs, xs * xs, ms), daily_means(g))
    tests = []
    for a_name in grids:
        for w_name, (ranked, shuffled, w_daily) in weather_prepared.items():
            inside = within_day(ranked, air_ranked[a_name], shuffled)
            r_daily, n_days, p_daily = day_to_day(w_daily, air_daily[a_name], rng, DAILY_PERMUTATIONS * shuffles // PERMUTATIONS)
            if inside is None and p_daily is None:
                continue
            r, lag, p = inside or (None, 0, 1.0)
            best_p = min(p, p_daily if p_daily is not None else 1.0) * 2  # two views of one pair: allow for both
            tests.append({"air": a_name, "weather": w_name, "r": r, "lag_hours": lag * SLOT / 3600, "r_daily": r_daily,
                          "days": n_days, "p": min(1.0, best_p)})
    directions = []
    if direction and speed:
        d, s = to_grid(direction, origin, days), to_grid(speed, origin, days)
        for a_name, a in grids.items():
            if (found := direction_effect(a, d, s, permutations)) is not None:
                directions.append({"air": a_name, **found})
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
