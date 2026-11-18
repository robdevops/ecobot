"""What a data source is to ecobot-mcp: a name, a local clock and a Warmer that keeps it fresh; started once, closed at the end. Its
startup (ecobot_mcp/core.py) uses nothing else of the four sources: the weather station (lib/ecowitt), the air sensor (lib/airgradient),
the pollen page (lib/pollen) and the forecast (lib/forecast). The bot has none (docs/ecobot-mcp.md step 6b)."""

from abc import ABC, abstractmethod
from datetime import datetime, tzinfo

from .timeutil import now_local
from .warm import Warmer


class Source(ABC):
    """Each source sets `name`, `tz` and `warmer` (a Warmer of its own `warm`). close() closes its `client` and its
    `store` on disk; the weather station, which has an API client and a history cache instead, closes its own."""
    name: str
    tz: tzinfo
    warmer: Warmer
    summary: str = ""   # set by start(): the line that tells what the source is reading and from where (logged by the startup, in order)

    def startup_lines(self) -> list[str]:
        """The lines the start-up logs for this source."""
        return [self.summary] if self.summary else []

    @abstractmethod
    async def start(self, wait: bool = True):
        """Get ready: find the device, read the first page. A failure that leaves the source of no use raises (the bot then runs
        without it); one that only means "not yet" is logged, and the next refresh tries again. wait False (ecobot-mcp's start, so the
        port opens at once): nothing is asked of the network, the source starts from what the data folder kept of it, and what it would
        have asked for is the warm-up's (ecobot_mcp/core.py Core.warm_up)."""

    @abstractmethod
    async def warm(self, fresh: bool = True) -> str:
        """Fetch what questions usually need, if it is due (`fresh`: a timed refresh, not a question's); says what it did."""

    def now(self) -> datetime:
        return now_local(self.tz)

    async def close(self):
        await self.client.aclose()
        self.store.close()
