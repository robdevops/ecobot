"""The service's tools (docs/mcp-service.md 3.2 and 3.3): the ten that will replace the bot's twelve, each in a module of its own, on
what every one of them shares:

  - common.py: how a tool is made, what it is sent (its arguments and the request's metadata, nothing else), and the shape of what it
    answers: the period it read, the time of its figures, its charts, and its refusals;
  - names.py: the readings the service has, one name each (`outdoor.temperature`, `air.pm2_5`), and the words people use for them;
  - daylook.py: a day's look ("wet", 🌧️) from its figures;
  - instructions.py: the server's instructions, a short summary for a client to pass to its model.

`build(ctx)` makes the ten, in 3.3's order, each module's `tools(ctx)` (a tool whose source isn't there is left out), and `Service`
holds them by name. The bot offers them to its model (lib/model_tools.py) and its own code paths call them; nothing here knows of
Telegram, of the bot's buttons or of who draws a chart.
"""

from .common import ServiceTool

ORDER = ("current", "summary", "readings", "days", "relate", "scan", "outlook", "forecast", "pollen", "about")   # 3.3's


def build(ctx) -> list["ServiceTool"]:
    """The service's tools over this Context, in 3.3's order: each module's tools(ctx)."""
    from importlib import import_module
    return [t for name in ORDER for t in import_module(f"{__name__}.{name}").tools(ctx)]


class Service:
    """The service's tools by name: `service["summary"]`, `service.get(name)`, `name in service`; iterated in 3.3's order."""

    def __init__(self, tools: list["ServiceTool"] = (), ctx=None):
        self.tools, self.ctx = list(tools), ctx   # ctx: the Context they were built over, when known
        self.by_name = {t.name: t for t in self.tools}

    @classmethod
    def of(cls, ctx) -> "Service":
        return cls(build(ctx), ctx)

    def held(self, names: list[str]) -> list[str]:
        """These readings' names, less those the service's catalogue doesn't hold (all of them when it isn't known)."""
        if self.ctx is None:
            return list(names)
        known = set(self.ctx.readings().names)
        return [n for n in names if n in known]

    def get(self, name: str) -> "ServiceTool | None":
        return self.by_name.get(name)

    def __getitem__(self, name: str) -> "ServiceTool":
        return self.by_name[name]

    def __contains__(self, name: str) -> bool:
        return name in self.by_name

    def __iter__(self):
        return iter(self.tools)


__all__ = ["ORDER", "Service", "build"]
