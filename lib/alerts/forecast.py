"""The forecast revised: station-wide, for today and tomorrow (docs/ecobot-mcp.md 3.6).

Each day, the first forecast fetched after 6 am is the baseline for today and tomorrow. Every later fetch is compared with it, day by
day, and a day that changed substantially is an alert, saying what changed:
  - the chance of rain becomes likely (it was under 50% and is now 50% or over) or unlikely (it was 50% or over and is now 30% or
    under); the chance of at least 1 mm, or, for a day with only Open-Meteo's older "chance of rain" figure on either side, that one.
    The band between keeps a chance that hovers around 50% (the ensemble's, rounded to 5%, moving 45 / 50 / 45 between model runs) from
    alerting likely, unlikely, likely on every refresh;
  - the highest temperature moves by more than 2 degrees.
The forecast alerted on becomes that day's new baseline, so the same revision never repeats and a further one does. The forecast only
refreshes from 6 am to 6 pm, so that is when this can fire. Each alert is an event in the alert log (lib/alert_log.py), news until the
end of the day it is about.
"""

import logging
from datetime import date, datetime, timedelta

from ..forecast.source import describe_day
from ..timeutil import at_hour
from .kinds import RAIN_FROM_PCT, kinds_of

log = logging.getLogger(__name__)

DRY_BELOW_PCT = 30   # rain unlikely again (from likely: RAIN_FROM_PCT, lib/alerts/kinds.py) only at or under this one (from likely)
TEMP_CHANGE_C = 2.0
BASELINE_HOUR = 6   # the first forecast fetched from this hour (local) is the day's baseline
DAYS = 2            # today and tomorrow


def _chance_key(old: dict, new: dict) -> str:
    return "rain_1mm_pct" if old.get("rain_1mm_pct") is not None and new.get("rain_1mm_pct") is not None else "rain_chance_pct"


def changes(old: dict, new: dict) -> list[str]:
    """What changed substantially from `old` (the baseline) to `new`, in words ("the high is now 19°C (was 24°C)"); [] for nothing."""
    out = []
    hot_before, hot_after = old.get("max_c"), new.get("max_c")
    if hot_before is not None and hot_after is not None and abs(hot_after - hot_before) > TEMP_CHANGE_C:
        out.append(f"the high is now {hot_after:.0f}°C (was {hot_before:.0f}°C)")
    key = _chance_key(old, new)
    before, after = old.get(key), new.get(key)
    if before is not None and after is not None:
        if before < RAIN_FROM_PCT <= after:
            out.append(f"rain is now likely ({after}%, was {before}%)")
        elif before >= RAIN_FROM_PCT and after <= DRY_BELOW_PCT:
            out.append(f"rain is now unlikely ({after}%, was {before}%)")
    return out


def revised(old: dict, new: dict) -> bool:
    """Is `new` a substantial revision of the baseline `old`?"""
    return bool(changes(old, new))


def _stored(d: dict) -> dict:
    return {**d, "date": d["date"].isoformat()}


class ForecastMonitor:
    KINDS = kinds_of("Forecast")

    def __init__(self, forecast, state, alerts):
        """state: the rules' store (AlertStore). alerts: the alert log."""
        self.forecast, self.state, self.alerts = forecast, state, alerts

    async def check(self):
        """After each forecast refresh: today's baseline once a forecast fetched from 6 am is there; then an alert for each of today and
        tomorrow that has changed substantially from its baseline, which it then replaces."""
        now = self.forecast.now()
        today = now.date()
        tz = getattr(self.forecast, "tz", None)
        if now.hour < BASELINE_HOUR or not self.forecast.days:
            return
        fetched = getattr(self.forecast, "fetched_at", None)
        if fetched is not None and tz is not None and fetched < at_hour(today, BASELINE_HOUR, tz):
            return   # (the forecast held is from before 6 am: not yet today's)
        current = {d["date"]: d for d in self.forecast.days if 0 <= (d["date"] - today).days < DAYS}
        m = self.state.monitor.setdefault("forecast", {})
        if m.get("day") != today.isoformat():   # the first forecast of the day: the baseline, nothing to compare
            m.clear()
            m.update(day=today.isoformat(), baseline={d.isoformat(): _stored(new) for d, new in current.items()})
            log.info("Forecast alert: today's baseline set (%s)", ", ".join(sorted(m["baseline"])))
            self.state.save()
            return
        baseline = m.setdefault("baseline", {})
        for day, new in sorted(current.items()):
            old = baseline.get(day.isoformat())
            if old is None:
                baseline[day.isoformat()] = _stored(new)
                continue
            if said := changes(old, new):
                self._emit(day, today, old, new, said, tz)
                baseline[day.isoformat()] = _stored(new)
        self.state.save()

    def _emit(self, day: date, today: date, old: dict, new: dict, said: list[str], tz):
        label = "today" if day == today else "tomorrow"
        text = f"\U0001f504 The forecast for {label} has changed: {' and '.join(said)}.\nNow: {describe_day(new)}"
        key = _chance_key(old, new)
        data = {"date": day.isoformat(), "was": {"max_c": old.get("max_c"), key: old.get(key), "summary": old.get("summary")},
                "now": {"max_c": new.get("max_c"), key: new.get(key), "summary": new.get("summary")}}
        ends = at_hour(day + timedelta(days=1), 0, tz) if tz else datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp()
        self.alerts.append("forecast", text, f"The forecast for {label} changed", data, expires=ends)
