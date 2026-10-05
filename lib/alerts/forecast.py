"""A forecast changed after it was sent: told to the chat that was sent it.

When a chat has been shown a day's forecast (in the report or an answer) and a later refresh revises that day substantially,
that chat gets one message with the old and the new line for each changed day. Substantial is:
  - rain flips: it was rainy (chance of at least 1 mm of 50% or more) and is now dry (30% or less), or the other way round (the
    gap between 30 and 50 stops small wobbles flip-flopping); a day with only Open-Meteo's older "chance of rain" figure is
    compared on that;
  - the highest temperature differs by more than 2 degrees.
After the alert, the day as now forecast is the chat's new baseline, so the same revision never repeats, a further one does.
The forecast only refreshes from 6 am to 6 pm, so that is when this can fire.
"""

import logging

from ..forecast.source import day_label, describe_day

log = logging.getLogger(__name__)

RAIN_FROM_PCT = 50
DRY_BELOW_PCT = 30
TEMP_CHANGE_C = 2.0


def revised(old: dict, new: dict) -> bool:
    """Is `new` a substantial revision of what was sent (`old`)?"""
    key = "rain_1mm_pct" if old.get("rain_1mm_pct") is not None and new.get("rain_1mm_pct") is not None else "rain_chance_pct"
    before, after = old.get(key), new.get(key)
    if before is not None and after is not None and (
            (before >= RAIN_FROM_PCT and after <= DRY_BELOW_PCT) or (before <= DRY_BELOW_PCT and after >= RAIN_FROM_PCT)):
        return True
    hot_before, hot_after = old.get("max_c"), new.get("max_c")
    return hot_before is not None and hot_after is not None and abs(hot_after - hot_before) > TEMP_CHANGE_C


class ForecastMonitor:
    def __init__(self, forecast, state, notify):
        """notify: the Notifier (its to_chat sends to one chat)."""
        self.forecast, self.state, self.notify = forecast, state, notify

    async def check(self):
        """After each forecast refresh: tell each chat (with alerts on) about the days it was sent that have since changed."""
        today = self.forecast.now().date()
        current = {d["date"]: d for d in self.forecast.days or []}
        if not current:
            return
        for chat_id in self.state.forecast_chats():
            if not self.state.subscribed(chat_id, "forecast"):
                continue
            changes = [(day, old, current[day]) for day, old in sorted(self.state.forecast_sent(chat_id).items())
                       if day >= today and day in current and revised(old, current[day])]
            if not changes:
                continue
            lines = [f"• {day_label(day, today)}: was {describe_day(old)}\n  now {describe_day(new)}" for day, old, new in changes]
            if await self.notify.to_chat(chat_id, "\U0001f504 The forecast has changed since I sent it:\n" + "\n".join(lines), kind="forecast"):
                self.state.record_forecast(chat_id, [new for _, _, new in changes])
