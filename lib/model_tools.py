"""The service's ten tools (lib/service) as the bot offers them to its model (docs/mcp-service.md 4): each by its own name, description
and parameters, as they are. The bot draws the charts itself, so it asks for them as drawn (request `_meta` `chart_points: "drawn"`)
and keeps each whole result on the question's Turn (`Turn.results`: for drawing, the period buttons, the complete result that ends the
turn; a call that gave no JSON object leaves None); the model is sent the result less the charts' data (`for_model`): each chart as its title and period,
with `caption`, `reply` and `complete` as they are, and `chart_note` the bot's (BOT_CHART_NOTE: the service's asks the model to draw
the chart from data it is no longer sent; the bot sends the picture with the reply as its caption)."""

import json

from .service.common import AS_DRAWN, charts_as_titles
from .tools import Tool, Tools, Turn, json_result


BOT_CHART_NOTE = ("The bot sends this chart with your reply as its caption: keep your text to the figures. Don't describe the chart or "
                  "say it was sent.")


def for_model(out: dict) -> dict:
    """A result as the model is sent it: `charts` as [{title, period}] (the bot draws them) and `chart_note` saying so (BOT_CHART_NOTE),
    everything else as it is."""
    return charts_as_titles(out, BOT_CHART_NOTE)


def tool(service_tool) -> Tool:
    """One ServiceTool as the model is offered it: run with the question's metadata, charts as drawn; the whole result kept on the
    Turn, the model sent for_model of it."""
    async def handle(args: dict, turn: Turn) -> str:
        slot = len(turn.results)   # (taken before the call: the calls of a step run together, and keep the order they were made in)
        turn.results.append(None)
        text = await service_tool.call(args, {**turn.meta, "chart_points": AS_DRAWN})
        try:
            out = json.loads(text)
        except ValueError:
            return text
        if not isinstance(out, dict):
            return text
        turn.results[slot] = (service_tool.name, args, out)
        return json_result(for_model(out))
    return Tool(service_tool.name, service_tool.description, service_tool.parameters, handle)


class ServiceTools(Tools):
    """The service's tools as the model is offered them, following the service's list: read again whenever it is another list (a remote
    service's, read on each connection and when the service says its tools changed; none while it has not been reached)."""

    def __init__(self, service):
        self.service = service
        self._seen: list[int] | None = None
        self._by_name: dict[str, Tool] = {}
        self._schemas: list[dict] = []

    def _current(self):
        listed = list(self.service)
        if (seen := [id(t) for t in listed]) != self._seen:
            wrapped = [tool(t) for t in listed]
            self._by_name, self._schemas, self._seen = {t.name: t for t in wrapped}, [t.schema() for t in wrapped], seen

    @property
    def by_name(self) -> dict[str, Tool]:
        self._current()
        return self._by_name

    @property
    def schemas(self) -> list[dict]:
        self._current()
        return self._schemas


def tools(service) -> Tools:
    """The service's tools (a lib.service.Service, a remote service's list (lib/weather.py), or any list of ServiceTools), in its order,
    as the model is offered them."""
    return ServiceTools(service)


__all__ = ["BOT_CHART_NOTE", "ServiceTools", "for_model", "tool", "tools"]
