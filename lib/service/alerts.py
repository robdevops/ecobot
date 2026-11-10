"""The service's alert rules (docs/mcp-service.md 3.6), each on the trigger it has: the weather alerts after every refresh of the station
(and the rain on each live reading), particulates every CHECK_SECONDS, pollen and the forecast after each refresh of their pages. Their
settings (quiet hours, cooldown) are the service's config, their state its own store (lib/alerts/store.py, alert_state.json), and what
they decide goes to the alert log (lib/alert_log.py), which clients read: they know nothing of chats or Telegram.

The irrigation controller's alerts are not here: they move to its own server (step 5)."""

import logging

from ..alerts.forecast import ForecastMonitor
from ..alerts.particulates import CHECK_SECONDS, ParticulatesMonitor
from ..alerts.pollen_and_asthma import PollenAndAsthmaMonitor
from ..alerts.weather import ALERT_COOLDOWN_SECONDS, WeatherMonitor
from ..warm import every

log = logging.getLogger(__name__)


class AlertRules:
    def __init__(self, sources: dict, store, alerts, cooldown_seconds: int = ALERT_COOLDOWN_SECONDS, quiet: tuple[int, int] | None = None,
                 service=None):
        """sources: by name (Ecowitt, AirGradient, Pollen, Forecast; those that started). store: the rules' state (AlertStore). alerts: the
        alert log. service: the service's tools, which draw the alerts' charts."""
        self.sources, self.store, self.alerts = sources, store, alerts
        eco, air = sources.get("Ecowitt"), sources.get("AirGradient")
        self.weather = WeatherMonitor(eco, store, alerts, cooldown_seconds, quiet, service) if eco else None
        self.particulates = ParticulatesMonitor(air, store, alerts, service) if air else None
        self.forecast = ForecastMonitor(sources["Forecast"], store, alerts) if "Forecast" in sources else None
        self.pollen = PollenAndAsthmaMonitor(sources["Pollen"], store, alerts) if "Pollen" in sources else None

    def start(self, tasks) -> list[str]:
        """Start every rule but the weather's (start_weather, once the station has been read): the alert types now running."""
        running = []
        if self.particulates:
            tasks.start(every(CHECK_SECONDS, self.particulates.check))
            running += ParticulatesMonitor.KINDS
        if self.forecast:
            self.sources["Forecast"].warmer.after.append(self.forecast.check)   # after each refresh (in the day)
            running += ForecastMonitor.KINDS
        if self.pollen:
            self.sources["Pollen"].warmer.after.append(self.pollen.check)       # after each refresh (in the day)
            running += PollenAndAsthmaMonitor.KINDS
        return running

    async def start_weather(self, running: list[str]):
        """The weather rules, once the station has been read: their state from the recent readings (first run), a first check, then one
        after every refresh, and the rain on each live reading. Their alert types go first in `running` once they are hooked up."""
        if not self.weather:
            return
        eco = self.sources["Ecowitt"]
        await self.weather.init()
        eco.warmer.after.append(self.weather.check)   # checked on every fresh refresh
        running[:0] = WeatherMonitor.KINDS
        await self.weather.check()
        if eco.fast is not None:   # Home Assistant: the rain alert looks at each new reading as it comes in, not only at the minute's refresh
            eco.on_live.append(self.weather.check_rain)
