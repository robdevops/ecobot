"""ecobot-mcp's startup (docs/ecobot-mcp.md step 5: "ecobot.py's warm_up() is split"), run by ecobot-mcp (python -m ecobot_mcp): the
bot is only ever its client (step 6b):

  1. `configure(cfg)`: the settings the engines read as module values (the anti-spike rule, what "all" draws, what `about` says);
  2. `start_sources(cfg)`: every configured source that starts, the weather station first (its time zone wins);
  3. `Core.open(cfg, sources, stack, tasks)`: the alert log, the tools' Context and the ten tools (lib/service);
  4. `core.log_startup()`: each source's startup line;
  5. `core.start_alerts()`: the alert rules (lib/service/alerts.py), writing the alert log; `core.start_warmers()`: the keep-warm;
  6. `core.warm_up(running)`: the first fetch, then the weather alerts, the live poll, the archive, Home Assistant's imports, the air
     sensor's history and backfill.

`Health` records each loop's last success and failure (ecobot://health)."""

import asyncio
import dataclasses
import functools
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from lib.airgradient import AirGradient
from lib.airgradient import metrics as air_metrics
from lib.alert_log import AlertLog
from lib.alerts.kinds import LABELS, available_kinds
from lib.alerts.store import AlertStore
from lib.compose import Composer
from lib.config import Config
from lib.ecowitt import Archive, Ecowitt
from lib.ecowitt.source import HA_IMPORT_SECONDS
from lib.forecast import Forecast
from lib.logs import BUGS
from lib.pollen import Pollen
from lib.service import Service
from lib.service import about as service_about
from lib.service import names as service_names
from lib.service.alerts import AlertRules
from lib.service.context import Context
from lib.sources import Source
from lib.storage import Storage
from lib.version import commit_in, version   # noqa: F401 (the running commit: ecobot-mcp's and the bot's)
from lib.warm import Tasks, every, safely

log = logging.getLogger("ecobot.mcp")   # (under the bot's own logger: LOG_LEVEL reaches it)


def configure(cfg: Config):
    """The settings the engines read as module values: the anti-spike rule, what "all" draws, what `about` says of the alerts."""
    air_metrics.REQUIRE_CONSECUTIVE = cfg.antispike_require_consecutive
    service_names.FEELS_LIKE_IN_ALL = cfg.chart_all_feels_like   # "all" (and "weather all week") draws these too
    service_names.VPD_IN_ALL = cfg.chart_all_vpd
    service_about.RAIN_QUIET_HOURS = cfg.rain_quiet_hours            # what `about` says of the rain alert's quiet hours ...
    service_about.ALERT_COOLDOWN_MINUTES = cfg.alert_cooldown_minutes   # ... and of the alerts' cooldown


async def start_sources(cfg: Config) -> tuple[list[Source], Config]:
    """Every configured data source that starts, and the settings in the time zone they run in. The weather station starts
    first: it names its own time zone, which wins over TZ, so the air sensor and the optional website sources (pollen,
    forecast, off unless switched on), and everything built after, use the station's; those three start side by side (each
    waits on its own server). The forecast uses the station's location unless one is configured."""
    sources: list[Source] = []
    took: dict[str, float] = {}

    async def start(kind: type[Source], *args, **kwargs) -> Source | None:
        """A source made and started; one that fails either way is logged and left out (None)."""
        source, started = None, time.monotonic()
        try:
            source = kind(*args, **kwargs)
            await source.start()
            return source
        except Exception as e:
            log.error("%s couldn't start (%s: %s) - continuing without it", kind.name, type(e).__name__, e, exc_info=isinstance(e, BUGS))
            if source is not None:
                await safely(source.close)
            return None
        finally:
            took[kind.name] = time.monotonic() - started
    storage = None
    if cfg.ecowitt or cfg.airgradient:
        try:   # (in a thread: opening makes or upgrades its tables)
            storage = await asyncio.to_thread(Storage, cfg.storage_path, cfg.tz)
        except Exception as e:
            log.error("The history file %s couldn't be opened (%s: %s) - continuing without the weather station and the air sensor",
                      cfg.storage_path, type(e).__name__, e, exc_info=isinstance(e, BUGS))
    if cfg.ecowitt and storage is not None and (eco := await start(Ecowitt, cfg, storage=storage)):
        sources.append(eco)
    eco = sources[0] if sources else None
    if eco and str(eco.tz) != str(cfg.tz):
        if os.getenv("TZ"):
            log.warning("TZ=%s is not the weather station's time zone; using the station's, %s", os.getenv("TZ"), eco.tz)
        cfg = dataclasses.replace(cfg, tz=eco.tz)
    if not getattr(cfg.tz, "key", None):
        log.warning("The time zone has no name, only an offset (%s), so daylight saving will put the times out: set TZ, "
                    "e.g. TZ=Australia/Melbourne", cfg.tz)
    starts = []
    if cfg.airgradient and storage is not None:   # (one file and one Home Assistant client for both; the file is the station's to close, else the sensor's)
        starts.append(start(AirGradient, cfg, storage=storage, owned=eco is None, **({"ha": eco.ha} if eco and eco.ha else {})))
    if cfg.pollen:
        starts.append(start(Pollen, cfg))
    if cfg.forecast:
        starts.append(start(Forecast, cfg, (eco.latitude, eco.longitude) if eco and None not in (eco.latitude, eco.longitude) else None))
    sources += [s for s in await asyncio.gather(*starts) if s is not None]   # (in this order, whichever answers first)
    if storage is not None and not sources:
        storage.close()
    if took:
        log.debug("Sources started in %.1fs: %s", sum(took.values()), ", ".join(f"{name} {seconds:.1f}" for name, seconds in took.items()))
    return sources, cfg


# ---------- health ----------
class Health:
    """Each loop's last success and failure (`track`), and the archive's last run (`archived`), for ecobot://health."""

    def __init__(self, tz=None):
        self.tz = tz
        self.loops: dict[str, dict] = {}
        self.archive: dict = {}
        self.extra: dict[str, Callable[[], dict]] = {}   # name -> figures read when asked (the live poll's last answer)

    def track(self, name: str, fn: Callable[..., Awaitable]) -> Callable[..., Awaitable]:
        """fn, its every run recorded under `name`: when it last succeeded, when it last failed and why. It raises as before, and keeps
        its name (safely() logs a failure by it)."""
        @functools.wraps(fn)
        async def run(*args, **kwargs):
            entry = self.loops.setdefault(name, {})
            try:
                out = await fn(*args, **kwargs)
            except Exception as e:
                entry.update(last_failure=time.time(), error=f"{type(e).__name__}: {e}")
                raise
            entry["last_ok"] = time.time()
            return out
        return run

    def _time(self, t: float | None) -> str | None:
        return None if not t else datetime.fromtimestamp(t, self.tz).isoformat(timespec="seconds")

    def report(self) -> dict:
        loops = {name: {k: self._time(v) if k.startswith("last_") else v for k, v in entry.items()} for name, entry in self.loops.items()}
        for name, read in self.extra.items():
            try:
                loops[name] = {k: self._time(v) if k.startswith("last_") else v for k, v in read().items()}
            except Exception as e:   # (only a figure)
                loops[name] = {"error": f"{type(e).__name__}: {e}"}
        archive = {k: self._time(v) if k.startswith("last_") else v for k, v in self.archive.items()}
        return {"loops": loops, **({"archive": archive} if archive else {})}


# ---------- ecobot-mcp ----------
def hold_data_folder(cfg: Config, stack):
    """One ecobot-mcp per data folder: two on the same folder would both fetch and both write each alert (sent twice). The lock is the
    kernel's, so it goes with the process, however that ends."""
    import fcntl
    path = cfg.state_path.parent / "mcp.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    held = open(path, "w")
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        held.close()
        raise SystemExit(f"Another ecobot-mcp (python -m ecobot_mcp) already runs on {path.parent}: stop it first, or give this one "
                         f"another DATA_DIR.")
    stack.callback(held.close)



@dataclass
class Core:
    """ecobot-mcp as it runs: its settings, sources, alert log, tools and their Context, the alert rules once started."""
    cfg: Config
    sources: list[Source]
    alert_log: AlertLog
    ctx: Context
    service: Service
    tasks: Tasks
    health: Health
    rules: AlertRules | None = None
    running: list[str] = field(default_factory=list)   # the alert types running (the weather's go first once they start)

    @property
    def by_name(self) -> dict[str, Source]:
        return {s.name: s for s in self.sources}

    @classmethod
    async def open(cls, cfg: Config, sources: list[Source], stack, tasks: Tasks) -> "Core":
        """The alert log (closed by `stack`) and the ten tools over the sources that started."""
        hold_data_folder(cfg, stack)
        by_name = {s.name: s for s in sources}
        eco, air = by_name.get("Ecowitt"), by_name.get("AirGradient")
        composer = Composer(eco, air) if eco and air else None  # charts and comparisons across the two sources
        alert_log = AlertLog(cfg.alert_log_path, cfg.tz)   # the alerts the rules decide, which clients deliver
        stack.callback(alert_log.close)
        ctx = await Context.open(eco=eco, air=air, composer=composer, forecast=by_name.get("Forecast"), pollen=by_name.get("Pollen"),
                                 tz=cfg.tz)
        service = Service.of(ctx)
        log.info("Tools: %s", ", ".join(t.name for t in service))
        health = Health(cfg.tz)
        for source in sources:   # (the keep-warm's refreshes, recorded)
            source.warmer.refresh = health.track(f"{source.name} refresh", source.warmer.refresh)
        if eco is not None and eco.fast is not None:
            health.extra["Ecowitt live poll"] = lambda: {"last_ok": eco._polled or None}
        return cls(cfg, sources, alert_log, ctx, service, tasks, health)

    def log_startup(self):
        """Each source's startup line, in the order they are listed."""
        for source in self.sources:
            for line in source.startup_lines():
                log.info("%s", line)

    def start_alerts(self) -> list[str]:
        """The alert rules over every source that started (lib/service/alerts.py), writing the alert log; the weather's start in
        warm_up, once the station has been read. Returns the alert types now running (a list warm_up adds the weather's to)."""
        store = AlertStore(self.cfg.alert_state_path)
        self.rules = AlertRules(self.by_name, store, self.alert_log, self.cfg.alert_cooldown_minutes * 60, self.cfg.rain_quiet_hours,
                                self.service)
        self.running = self.rules.start(self.tasks)
        return self.running

    def start_warmers(self):
        """Keeping warm: everything questions need, refreshed before they arrive."""
        for source in self.sources:
            self.tasks.start(source.warmer.run())

    async def warm_up(self, running: list[str] | None = None):
        """Fetch everything once, together; then start what waited for it: the weather alerts, the live poll, the archives (copying each
        source's history into its file), Home Assistant's imports."""
        running = self.running if running is None else running
        eco, air, tasks, health = self.by_name.get("Ecowitt"), self.by_name.get("AirGradient"), self.tasks, self.health

        async def ecowitt_part():
            # One request at a time (Ecowitt rate-limits): warm first, so the alerts' own look at the last 3 hours is answered
            # from what was just fetched. A failed first refresh must not stop the alerts or the archive: safely() logs it
            await safely(health.track("Ecowitt refresh", eco.warm), True)
            try:
                if self.rules is not None:
                    await self.rules.start_weather(running)
            except Exception as e:
                log.error("Weather alerts couldn't start (%s: %s)", type(e).__name__, e, exc_info=isinstance(e, BUGS))
            if eco.fast is not None:
                tasks.start(eco.watch_live())
            # The full history is copied into the cache in the background (a few minutes the first time), so questions rarely
            # need Ecowitt inline
            archive = Archive(eco, eco.archive_pace)
            archive.run_once = self._archived(archive, archive.run_once)
            tasks.start(archive.loop())
            if eco.ranges_from_ha:   # the cloud's readings get their 5-minute bands from what Home Assistant kept, the days the bot was not watching too
                tasks.start(every(HA_IMPORT_SECONDS, health.track("Ecowitt Home Assistant import", eco.import_ranges)))

        async def air_part():
            await safely(health.track("AirGradient refresh", air.warm), True)
            tasks.start(air_history())   # even if the sensor was unreachable just now

        async def air_history():
            """What Home Assistant kept of the monitor (the readings the service lacks, their bands, the minutes), then the days the cache
            lacks, then Home Assistant again every HA_IMPORT_SECONDS."""
            imported = health.track("AirGradient Home Assistant import", air.import_history)
            if air.ha:   # (first: it is quick, and holds the last days; the backfill can take minutes the first time)
                await safely(imported)
            if air.cloud:
                await safely(health.track("AirGradient backfill", air_backfill))
            if air.ha:
                await every(HA_IMPORT_SECONDS, imported)

        async def air_backfill():
            started = time.monotonic()
            fetched, failed, total = await air.backfill()
            log.info("Backfill AirGradient: %d days fetched, %d failed, %d days held, %.0fs", fetched, failed, total, time.monotonic() - started)

        try:
            await asyncio.gather(*([ecowitt_part()] if eco else []), *([air_part()] if air else []))
        except Exception as e:
            log.error("Startup fetch failed (%s: %s); the regular refreshes will catch up", type(e).__name__, e, exc_info=isinstance(e, BUGS))

    def _archived(self, archive: Archive, run_once):
        """The archive's run, its outcome recorded (Health.archive): when it last ran, what it fetched and failed, and what is held."""
        tracked = self.health.track("Ecowitt archive", run_once)

        @functools.wraps(run_once)
        async def run():
            fetched, failed = await tracked()
            self.health.archive = {"last_run": time.time(), "fetched": fetched, "failed": failed, "held": archive.held()}
            return fetched, failed
        return run

    def missing_alerts(self, running: list[str] | None = None) -> list[str]:
        """The alert types the sources have that are not running (their labels): a rule that could not start."""
        running = self.running if running is None else running
        return [LABELS[k] for k in available_kinds(self.by_name) if k not in running]


__all__ = ["Core", "Health", "commit_in", "configure", "start_sources", "version"]
