"""The service's ten tools (lib/service) as the bot offers them to its model (docs/mcp-service.md 4): each by its own name, description
and parameters, as they are. The bot draws the charts itself, so it asks for them as drawn (request `_meta` `chart_points: "drawn"`)
and keeps each whole result on the question's Turn (`Turn.results`: for drawing, the period buttons, the complete result that ends the
turn, the forecast days shown; a call that gave no JSON object leaves None); the model is sent the result less the charts' data (`for_model`): each chart as its title and period,
with `caption`, `reply` and `complete` as they are, and `chart_note` the bot's (BOT_CHART_NOTE: the service's asks the model to draw
the chart from data it is no longer sent; the bot sends the picture with the reply as its caption)."""

import json

from .service.common import AS_DRAWN
from .tools import Tool, Tools, Turn, json_result


BOT_CHART_NOTE = ("The bot sends this chart with your reply as its caption: keep your text to the figures. Don't describe the chart or "
                  "say it was sent.")


def for_model(out: dict) -> dict:
    """A result as the model is sent it: `charts` as [{title, period}] (the bot draws them) and `chart_note` saying so (BOT_CHART_NOTE),
    everything else as it is."""
    if not isinstance(out.get("charts"), list):
        return out
    told = {**out, "charts": [{"title": c.get("title"), "period": c.get("period")} for c in out["charts"] if isinstance(c, dict)]}
    if "chart_note" in out:
        told["chart_note"] = BOT_CHART_NOTE
    return told


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


def tools(service) -> Tools:
    """The service's tools (a lib.service.Service, or any list of ServiceTools), in its order, as the model is offered them."""
    return Tools([tool(t) for t in service])


__all__ = ["BOT_CHART_NOTE", "for_model", "tool", "tools"]
