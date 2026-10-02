"""Pollen and thunderstorm asthma alerts, checked after every refresh of the Pollen source (every 30 minutes in the day).

A warning when grass pollen or the thunderstorm asthma risk is High 🟠 or Extreme 🔴. One alert per metric, level and day: another
when it rises from High to Extreme, or on a new day that is still High or worse; none while it stays the same. Once it drops below
High the metric is ready to warn again.
"""

import logging
import time

from ..pollen.parse import LEVEL_EMOJI, LEVELS

log = logging.getLogger(__name__)

WARN_FROM = LEVELS.index("High")
STALE_SECONDS = 2 * 3600       # a page not fetched for this long (it is only fetched in the day) is not news
ADVICE = {"grass": "", "asthma": " Check your asthma action plan."}
LABELS = {"grass": "Grass pollen", "asthma": "Thunderstorm asthma risk"}
LINK = ("melbournepollen.com.au", "https://www.melbournepollen.com.au/")


class PollenMonitor:
    def __init__(self, pollen, state, notify):
        self.pollen, self.state, self.notify = pollen, state, notify

    async def check(self):
        cur = self.pollen.current()
        if not cur["fetched_at"] or time.time() - cur["fetched_at"] > STALE_SECONDS:
            return
        m = self.state.monitor.setdefault("pollen", {})
        today = self.pollen.now().date()
        for key in ("grass", "asthma"):
            reading = cur[key]
            if reading is None:        # not on the page (the asthma forecast only runs 1 Oct - 31 Dec): keep what we know
                continue
            rank, day = LEVELS.index(reading["level"]), str(today)
            if rank < WARN_FROM:
                m.pop(key, None)
                continue
            last = m.get(key)
            if last is None or last["rank"] < rank or last["day"] != day:
                m[key] = {"rank": rank, "day": day}
                for_day = reading.get("date")
                when = f" (forecast for {for_day:%a %d %b})" if for_day and for_day != today else ""
                await self.notify(f"{LEVEL_EMOJI[reading['level']]} {LABELS[key]} is {reading['level']}{when}.{ADVICE[key]}", link=LINK, kind="pollen")
        self.state.save()
