"""The service's ten tools (lib/service) as the bot offers them to its model (docs/ecobot-mcp.md 4): each by its own name, description
and parameters, as they are. The bot draws nothing: over the weather service as the bot reaches it (lib/weather.py, its `call_images`)
each call asks for its charts as images (request `_meta` `chart_image: "png"`, as drawn: `chart_points: "drawn"`), and keeps each
whole result on the question's Turn (`Turn.results`: the charts' full JSON, for the period buttons, and the complete result that ends
the turn; a call that gave no JSON object leaves None) with its pictures (`Turn.pictures`, by the result's place: the bot sends them);
over the service's tools alone (a lib.service.Service, in tests) the result has no pictures. The model is never sent an image: it is
sent the result less the charts' data (`for_model`): each chart as its title and period, with `caption`, `reply` and `complete` as
they are, and `chart_note` the bot's (BOT_CHART_NOTE: the bot sends the picture with the reply as its caption)."""

import json

from .service.common import AS_DRAWN, charts_as_titles
from .tools import Tool, Tools, Turn, json_result


BOT_CHART_NOTE = ("The bot sends this chart with your reply as its caption: keep your text to the figures. Don't describe the chart or "
                  "say it was sent.")


def for_model(out: dict) -> dict:
    """A result as the model is sent it: `charts` as [{title, period}] (the bot draws them) and `chart_note` saying so (BOT_CHART_NOTE),
    everything else as it is."""
    return charts_as_titles(out, BOT_CHART_NOTE)


def tool(service_tool, images=None) -> Tool:
    """One ServiceTool as the model is offered it: run with the question's metadata, charts as drawn; the whole result kept on the
    Turn, the model sent for_model of it. images(name, args, meta) -> (the answer as data, its charts drawn): how the call is made when
    its charts are wanted as pictures (lib/weather.py call_images)."""
    async def handle(args: dict, turn: Turn) -> str:
        slot = len(turn.results)   # (taken before the call: the calls of a step run together, and keep the order they were made in)
        turn.results.append(None)
        meta = {**turn.meta, "chart_points": AS_DRAWN}
        if images is not None:
            out, pictures = await images(service_tool.name, args, meta)
            if not isinstance(out, dict):
                return out
        else:
            text, pictures = await service_tool.call(args, meta), []
            try:
                out = json.loads(text)
            except ValueError:
                return text
            if not isinstance(out, dict):
                return text
        turn.results[slot] = (service_tool.name, args, out)
        if pictures:
            turn.pictures[slot] = pictures
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
            images = getattr(self.service, "call_images", None)   # (the weather service: the charts come drawn)
            wrapped = [tool(t, images) for t in listed]
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
    """The service's tools (the weather service as the bot reaches it, lib/weather.py, whose calls bring their charts drawn; or a
    lib.service.Service, or any list of ServiceTools), in its order, as the model is offered them."""
    return ServiceTools(service)


__all__ = ["BOT_CHART_NOTE", "ServiceTools", "for_model", "tool", "tools"]
