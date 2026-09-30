"""Does rain come with a change in another reading? `analyse` reads a driver (pressure, humidity, wind) beside the rain,
`analyse_air` an air-quality reading. Both work on {epoch: value} maps at 30-minute slots and return the numbers, the
findings in words and a plain verdict for the model to open with.

Per slot: the rain that fell and how the reading had changed over the previous 3 hours. From those: how much of the
rain fell while the reading was falling, steady or rising; its level in wet and dry slots; a correlation between the
change before and the rain after; and the biggest rain spells with the change that preceded each."""

from datetime import tzinfo

import numpy as np

from ..airgradient.metrics import ZONES, zone
from ..rain import SPELL_MM, rain_spells
from ..timeutil import MIN_DAY_SLOTS, SLOT, local_date, to_local
from .scan import rank

LOOKBACK = 6                   # slots: the change is measured over the previous 3 hours
AHEAD = 6                      # slots: and the rain counted over the next 3 hours

WASH_SLOTS = 12       # air quality in the 6 hours before a rain event starts is compared with the 6 hours after it ends
MIN_WASH_SLOTS = 6    # ... needing at least 3 hours of readings on each side
CLEANED = 0.9         # "cleaner afterwards": the mean after is at most this share of the mean before (worse: 1 / this)
MIN_EVENTS = 3


def change_before(driver: dict[int, float], t: int) -> float | None:
    then = driver.get(t - LOOKBACK * SLOT)
    now = driver.get(t)
    return None if then is None or now is None else now - then


def verdict(moving: float | None, steady: float | None) -> str | None:
    """A plain yes/no from how much more rain fell while the reading was moving than while it was steady (ratios
    against the fair share of time). Not from the correlation, which is weak by construction."""
    if moving is None or steady is None:
        return None
    if moving >= 1.5 and steady <= 0.65:
        return "Yes - a clear link"
    if moving >= 1.25 and steady <= 0.85:
        return "Yes - a moderate link"
    if 0.85 <= moving <= 1.15 and 0.85 <= steady <= 1.15:
        return "No - rain fell about evenly whatever the reading did"
    return "Weak or mixed - not a consistent link"


def _summary(table: dict, level: dict, events: list[dict], threshold: float, unit: str, clock) -> tuple[list[str], str | None]:
    """The findings in words, decided here so the answer can't lean on the correlation alone (it is weak by
    construction: most slots are dry, and rain after a fall and after a rise cancel in a signed correlation)."""
    out = []
    parts = [f"{n} {g['rain_vs_fair_share']} ({g['share_of_rain']} of the rain in {g['share_of_time']} of the time, "
             f"raining in {g['wet_share']} of its half-hours)" for n, g in table.items() if g["rain_vs_fair_share"]]
    if parts:
        out.append("Rain against its fair share of time, by the reading's change in the previous 3 hours (1.0x = no "
                   "difference): " + "; ".join(parts))
    slots = sum(g["slots"] for g in table.values())
    rain = sum(g["rain_mm"] for g in table.values())
    call = move_time = None
    if slots and rain:
        move_slots = table["falling"]["slots"] + table["rising"]["slots"]
        move_rain = table["falling"]["rain_mm"] + table["rising"]["rain_mm"]
        steady = table["steady"]
        move_time = move_slots / slots
        call = verdict((move_rain / rain) / (move_slots / slots) if move_slots else None,
                       (steady["rain_mm"] / rain) / (steady["slots"] / slots) if steady["slots"] else None)
        out.append(f"Moving either way (a change of {threshold:g} {unit} or more): {100 * move_rain / rain:.0f}% of the rain "
                   f"in {100 * move_slots / slots:.0f}% of the time"
                   + (f" = {(move_rain / rain) / (move_slots / slots):.1f}x" if move_slots else "")
                   + f"; steady: {steady['rain_vs_fair_share'] or 'n/a'}")
    if level["during_rain"] is not None and level["dry"] is not None:
        out.append(f"Average level {level['during_rain']:g} {unit} during rain vs {level['dry']:g} when dry "
                   f"({level['during_rain'] - level['dry']:+.1f})")
    if events:
        fell = sum(1 for e in events if e["change"] is not None and e["change"] <= -threshold)
        rose = sum(1 for e in events if e["change"] is not None and e["change"] >= threshold)
        moved = fell + rose
        big = max(events, key=lambda e: e["mm"])
        before = "" if big["change"] is None else f", after a change of {big['change']:+.1f} {unit}"
        out.append(f"{fell} of {len(events)} rain events (1 mm or more) began after a fall and {rose} after a rise ({100 * moved / len(events):.0f}% after a move"
                   + (f", against {100 * move_time:.0f}% of the time moving" if move_time is not None else "") + "); "
                   f"the biggest was {big['mm']:g} mm from {clock(big['start'])}{before}")
    return out, call


def analyse(driver: dict[int, float], rain: dict[int, float], threshold: float, tz: tzinfo, unit: str = "") -> dict:
    """The numbers for the answer. `driver` and `rain` are {epoch: value} at 30-minute slots."""
    slots = sorted(t for t in rain if t in driver)
    if not slots:
        return {}
    wet = [t for t in slots if rain[t] > 0]
    total = sum(rain[t] for t in slots)
    groups: dict[str, list[int]] = {"falling": [], "steady": [], "rising": []}
    for t in slots:
        c = change_before(driver, t)
        if c is not None:
            groups["falling" if c <= -threshold else "rising" if c >= threshold else "steady"].append(t)
    table = {}
    for name, ts in groups.items():
        mm = sum(rain[t] for t in ts)
        time_share = len(ts) / max(sum(map(len, groups.values())), 1)
        rain_share = mm / total if total else 0.0
        table[name] = {"slots": len(ts), "share_of_time": f"{100 * time_share:.0f}%",
                       "rain_mm": round(mm, 1), "share_of_rain": f"{100 * rain_share:.0f}%",
                       "rain_vs_fair_share": f"{rain_share / time_share:.1f}x" if time_share else None,
                       "wet_share": f"{100 * sum(1 for t in ts if rain[t] > 0) / max(len(ts), 1):.0f}%"}
    dry = [t for t in slots if rain[t] <= 0]
    level = {"during_rain": round(float(np.mean([driver[t] for t in wet])), 1) if wet else None,
             "dry": round(float(np.mean([driver[t] for t in dry])), 1) if dry else None}
    pairs = [(c, sum(rain.get(t + k * SLOT, 0.0) for k in range(1, AHEAD + 1))) for t in slots
             if (c := change_before(driver, t)) is not None and t + AHEAD * SLOT in rain]
    r = None
    if len(pairs) >= 30:
        a, b = np.array(pairs).T
        if a.std() > 0 and b.std() > 0:
            r = round(float(np.corrcoef(a, b)[0, 1]), 2)
    events = [{"start": s[0], "end": s[-1], "mm": round(sum(rain[t] for t in s), 1), "change": change_before(driver, s[0]),
               "lowest": min(driver[t] for t in s if t in driver)} for s in rain_spells(rain, wet)]
    fell = sum(1 for e in events if e["change"] is not None and e["change"] <= -threshold)
    rose = sum(1 for e in events if e["change"] is not None and e["change"] >= threshold)
    clock = lambda ts: (lambda dt: f"{dt:%a} {dt.day} {dt:%b} {dt.strftime('%-I:%M%p').lower()}")(to_local(ts, tz))
    summary, call = _summary(table, level, events, threshold, unit, clock)
    top = sorted(events, key=lambda e: -e["mm"])[:5]
    return {"slots": len(slots), "rain_mm": round(total, 1), "wet_slots": len(wet), "verdict": call, "findings": summary, "by_change_before": table,
            "average_level": level, "correlation_change_vs_rain_next_3h": r,
            "rain_events": {"count": len(events), "started_after_a_fall": fell, "started_after_a_rise": rose,
                            "biggest": [{"start": clock(e["start"]), "mm": e["mm"], "hours": round((e["end"] - e["start"]) / 3600 + 0.5, 1),
                                         "change_in_3h_before": None if e["change"] is None else round(e["change"], 1),
                                         "lowest_during": round(e["lowest"], 1)} for e in top]}}


def air_verdict(events: int, cleaned: int, worse: int, ratio: float | None) -> str:
    """A plain call on whether rain goes with cleaner air, from the events and the wet-vs-dry level (a ratio of the
    averages). Not from the daily correlation, which is a rough guide."""
    if events < MIN_EVENTS or ratio is None:
        return "Not enough rain events with air readings around them to say"
    share, bad = cleaned / events, worse / events
    if share >= 0.7 and ratio <= 0.8:
        return "Yes - rain clears the air"
    if share >= 0.55 and ratio <= 0.95:
        return "Yes - a moderate link: the air tends to be cleaner after rain"
    if bad >= 0.55 and ratio >= 1.05:
        return "Yes, the other way - the air was worse around rain"
    if 0.9 <= ratio <= 1.1 and share <= 0.4 and bad <= 0.4:
        return "No - air quality looked much the same wet and dry"
    return "Weak or mixed - not a consistent link"


def analyse_air(values: dict[int, float], rain: dict[int, float], limits: tuple[float, float], tz: tzinfo, label: str) -> dict:
    """Does rain go with cleaner air? `values` (an air reading) and `rain` are {epoch: value} at 30-minute slots. `limits`
    are the good and poor limits of the reading, to say what rating a level falls in."""
    slots = sorted(t for t in rain if t in values)
    if not slots:
        return {}
    wet, dry = [t for t in slots if rain[t] > 0], [t for t in slots if rain[t] <= 0]
    mean = lambda ts: float(np.mean([values[t] for t in ts])) if ts else None
    word = lambda v: ZONES[zone(v, limits)]
    wet_level, dry_level = mean(wet), mean(dry)
    events = []
    for s in rain_spells(rain, wet):
        before = [values[t] for t in range(s[0] - WASH_SLOTS * SLOT, s[0], SLOT) if t in values]
        after = [values[t] for t in range(s[-1] + SLOT, s[-1] + (WASH_SLOTS + 1) * SLOT, SLOT) if t in values]
        if len(before) >= MIN_WASH_SLOTS and len(after) >= MIN_WASH_SLOTS:
            events.append({"start": s[0], "mm": round(sum(rain[t] for t in s), 1), "before": float(np.mean(before)),
                           "after": float(np.mean(after))})
    cleaned = sum(1 for e in events if e["after"] <= CLEANED * e["before"])
    worse = sum(1 for e in events if e["after"] * CLEANED >= e["before"])
    ratio = wet_level / dry_level if wet_level is not None and dry_level else None
    days: dict = {}
    for t in slots:
        day = days.setdefault(local_date(t, tz), {"rain": 0.0, "air": []})
        day["rain"] += rain[t]
        day["air"].append(values[t])
    days = {d: v for d, v in days.items() if len(v["air"]) >= MIN_DAY_SLOTS}
    wet_days = [float(np.mean(v["air"])) for v in days.values() if v["rain"] >= SPELL_MM]
    dry_days = [float(np.mean(v["air"])) for v in days.values() if v["rain"] == 0]
    r = None
    if len(days) >= 10:
        rain_ranks, air_ranks = (rank(np.array(x)) for x in ([v["rain"] for v in days.values()], [float(np.mean(v["air"])) for v in days.values()]))
        if rain_ranks.std() > 0 and air_ranks.std() > 0:
            r = round(float(np.corrcoef(rain_ranks, air_ranks)[0, 1]), 2)
    findings = []
    if wet_level is not None and dry_level is not None:
        findings.append(f"Average {label} {wet_level:.1f} in wet half-hours ({word(wet_level)}) against {dry_level:.1f} when dry "
                        f"({word(dry_level)}): {ratio:.2f}x")
    if events:
        findings.append(f"Over {len(events)} rain events (1 mm or more) with readings around them: {cleaned} left the air cleaner "
                        f"in the 6 hours after than the 6 before, {worse} worse and {len(events) - cleaned - worse} about the same")
    if wet_days and dry_days:
        findings.append(f"Daily average {label} on wet days {np.mean(wet_days):.1f} ({len(wet_days)} days) against "
                        f"{np.mean(dry_days):.1f} on dry days ({len(dry_days)} days)")
    biggest = max(events, key=lambda e: e["mm"], default=None)
    if biggest:
        findings.append(f"The biggest event, {biggest['mm']:g} mm: {biggest['before']:.1f} in the 6 hours before, "
                        f"{biggest['after']:.1f} in the 6 hours after")
    return {"verdict": air_verdict(len(events), cleaned, worse, ratio), "findings": findings, "slots": len(slots),
            "wet_slots": len(wet), "events": {"count": len(events), "cleaner_after": cleaned, "worse_after": worse},
            "days_compared": len(days), "rank_correlation_daily_rain_vs_air": r}
