"""What the service's tools are built over: the sources that started, the file, the catalogue of readings, the station's zone and the
clock. Each tool module has `tools(ctx) -> list[ServiceTool]`, so the ten are built the same way, here today and behind MCP later."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from ..timeutil import to_local
from .names import Catalogue


@dataclass
class Context:
    eco: object | None = None          # the weather station (lib.ecowitt.Ecowitt), started; None when it isn't configured
    air: object | None = None          # the air sensor (lib.airgradient.AirGradient), started; None when it isn't configured
    composer: object | None = None     # the charts and comparisons across the two (lib.compose.Composer), when both are there
    forecast: object | None = None     # lib.forecast.Forecast, when switched on
    pollen: object | None = None       # lib.pollen.Pollen, when switched on
    catalogue: Catalogue | None = None   # the readings the file holds (names.Catalogue.open); None: every reading the service knows
    tz: tzinfo | None = None           # the station's zone (the station's own, once it has named it)
    clock: Callable[[], float] = field(default=lambda: time.time())   # the time now, as an epoch (a test's own)

    @property
    def storage(self):
        """The readings file the sources share."""
        return self.eco.storage if self.eco else self.air.store.storage if self.air else None

    @property
    def zone(self) -> tzinfo:
        return self.tz or (self.eco.tz if self.eco else self.air.tz)

    def now(self) -> datetime:
        """The time now, aware, in the station's zone."""
        return to_local(int(self.clock()), self.zone)

    def readings(self) -> Catalogue:
        from .names import CATALOGUE
        return self.catalogue or CATALOGUE

    @classmethod
    async def open(cls, *, eco=None, air=None, composer=None, forecast=None, pollen=None, **more) -> "Context":
        """The context with the catalogue read from the file."""
        return cls(eco=eco, air=air, composer=composer, forecast=forecast, pollen=pollen,
                   catalogue=await Catalogue.open(eco, air), **more)


__all__ = ["Context"]
