"""The irrigation controller: irrigation-mcp's code (the Tuya client, the watchdog, the daily check, its state, its client of ecobot-mcp,
the server), and `common` (what its client, the bot's menu, shares with it). The package itself loads nothing: `from lib.irrigation
import Tuya` loads the Tuya client when it is asked for (so the bot, importing lib.irrigation.common, never does)."""

_LAZY = {"Tuya": ".tuya", "Watchdog": ".watchdog", "IrrigationError": ".common"}


def __getattr__(name):
    if name in _LAZY:
        import importlib
        return getattr(importlib.import_module(_LAZY[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["IrrigationError", "Tuya", "Watchdog"]
