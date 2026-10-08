"""Closing the valve after a run the bot started, even if the controller does not.

`water` asks the device to run for N minutes (its `countdown`), but nothing independent checks that it stops. So the end time is
written to the bot's state file; a minute-by-minute check (cheap: it only talks to the controller once the time has passed) switches
the valve off if it is still on. Because the time is saved, a restart in the middle of a run still closes the valve.
"""

import logging
import time

from .tuya import Tuya

log = logging.getLogger(__name__)

GRACE_SECONDS = 30      # the device's own countdown gets this long to finish first
CHECK_SECONDS = 60


class Watchdog:
    def __init__(self, device: Tuya, state, clock=time.time):
        self.device, self.state, self.clock = device, state, clock

    def _memory(self) -> dict:
        return self.state.monitor.setdefault("irrigation", {})

    def arm(self, minutes: int):
        """A run of this many minutes has started."""
        self._memory()["off_at"] = self.clock() + minutes * 60
        self.state.save()

    def disarm(self):
        if self._memory().pop("off_at", None) is not None:
            self.state.save()

    @property
    def off_at(self) -> float | None:
        return self._memory().get("off_at")

    async def check(self):
        """Switch the valve off if a run is over and it is still on. A failure is tried again at the next check."""
        due = self.off_at
        if due is None or self.clock() < due + GRACE_SECONDS:
            return
        if (await self.device.status()).get("switch"):
            await self.device.set_switch(False)
            log.warning("Irrigation: the run should have ended; the valve was still open, so it was switched off")
        self.disarm()
