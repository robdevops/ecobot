"""Pollen and thunderstorm asthma alerts, checked after every refresh of the Pollen source (every 30 minutes in the day, October to
December only).

A warning when grass pollen or the thunderstorm asthma risk is High 🔴 or Extreme 🟣 (the scale is Low 🟢, Moderate 🟠, High 🔴, Extreme 🟣; "No data" counts
as no reading). One alert per metric and day: another on a new day that is still High, and again when it rises (High to Extreme);
none while it stays the same. Once it drops below High the metric is ready to warn again.
"""

import logging
import time

from ..pollen.parse import LEVEL_EMOJI, LEVELS
from ..timeutil import day_text
from .menu import kinds_of

log = logging.getLogger(__name__)

WARN_FROM = LEVELS.index("High")
STALE_SECONDS = 2 * 3600       # a page not fetched for this long (it is only fetched in the day) is not news
ADVICE = {"grass": "", "asthma": " Check your asthma action plan."}
LABELS = {"grass": "Grass pollen forecast", "asthma": "Thunderstorm asthma forecast"}
LINK = ("melbournepollen.com.au", "https://www.melbournepollen.com.au/")


class PollenAndAsthmaMonitor:
    KINDS = kinds_of("Pollen")

    def __init__(self, pollen, state, notify):
        self.pollen, self.state, self.notify = pollen, state, notify

    async def check(self):
        if not self.pollen.in_season():   # October to December only
            return
        cur = self.pollen.current()
        if not cur["fetched_at"] or time.time() - cur["fetched_at"] > STALE_SECONDS:
            return
        m = self.state.monitor.setdefault("pollen_and_asthma", {})
        today = self.pollen.now().date()
        for key in ("grass", "asthma"):
            reading = cur[key]
            if reading is None:        # not on the page (the asthma forecast only runs 1 Oct - 31 Dec): keep what we know
                continue
            if LEVELS.index(reading["level"]) < WARN_FROM:
                m.pop(key, None)
                continue
            seen = m.get(key) or {}
            if seen.get("day") != str(today) or LEVELS.index(seen.get("level", "High")) < LEVELS.index(reading["level"]):   # once a day while it stays High or worse, and again when it rises
                m[key] = {"day": str(today), "level": reading["level"]}
                for_day = reading.get("date")
                when = f" (forecast for {day_text(for_day, year=False)})" if for_day and for_day != today else ""
                await self.notify(f"{LEVEL_EMOJI[reading['level']]} {LABELS[key]} is {reading['level']}{when}.{ADVICE[key]}", link=LINK, kind="pollen_and_asthma")
        self.state.save()
