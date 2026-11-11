"""The tools the bot's model is offered (lib/service's ten, wrapped by lib/model_tools.py), and the Turn the engines take."""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .logs import BUGS, QUOTES

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 60000


def json_result(out) -> str:
    """A tool's result as the model is sent it: compact JSON, with °, ² and emoji written as they are, not escaped."""
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Turn:
    """What one call carries: the options an engine is driven with (set by the service from its own arguments: lib/service/summary.py,
    days.py, relate.py, scan.py) and what it gives back (its charts, its timings); for the bot's model, the request's metadata and the
    results it keeps. Nothing is shared between questions."""
    charts: list = field(default_factory=list)   # chart specs the engines add
    chart_asked: bool = False                    # draw a chart (the call's `chart`)
    chart_field: str | None = None               # the one reading drawn; None: temperature
    chart_fields: list[str] = field(default_factory=list)   # the readings drawn together, a panel each
    two_axes: bool = False                       # two readings on one panel with a scale each
    air_named: list[str] = field(default_factory=list)   # the air readings a chart of the air sensor draws
    average_asked: bool = False                  # the figures and the chart as averages
    readings: list[str] = field(default_factory=list)   # the readings asked for: a result leaves out the others
    per_day: bool = False                        # a long period keeps every day, not only its extremes
    ahead: bool = False                          # nobody is waiting (a background request): its fetches wait their turn
    times: dict = field(default_factory=dict)    # seconds spent by step ("data", "draw", "png"), for the reply's timing line in the log
    meta: dict = field(default_factory=dict)     # the request's metadata, all a service tool reads (lib/service/common.py: chart_points,
                                                 # fetch, priority); over MCP, the request's _meta
    results: list = field(default_factory=list)  # the bot's model: (tool, arguments, its whole result as data) of each call it made
    pictures: dict = field(default_factory=dict)  # ... and the charts the service drew for it, by its place in `results`
                                                  # (lib/weather.ChartImage each: the PNG, the chart's JSON, the theme)
    result_chars: int = 0                        # the size of every result the model was sent so far this question


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, Turn], Awaitable[str]]   # (arguments, the question's Turn)

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


class Tools:
    def __init__(self, tools: list[Tool]):
        self.by_name = {t.name: t for t in tools}
        self.schemas = [t.schema() for t in tools]

    async def call(self, name: str, raw_args: str, turn: Turn | None = None) -> str:
        """Run a tool for the model; failures come back as text the model can explain."""
        tool = self.by_name.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError as e:
            return f"Error: tool arguments were not valid JSON ({e})"
        log.debug("Tool call %s %s", name, raw_args, extra=QUOTES)
        turn = turn if turn is not None else Turn()
        try:   # no time limit of its own: the question's (bot.TURN_SECONDS) runs from before the call
            out = await tool.handler(args, turn)
        except Exception as e:
            log.error("Tool %s failed: %s: %s", name, type(e).__name__, e, exc_info=isinstance(e, BUGS))
            return f"Error calling tool: {e}"
        out = out or "(no output)"
        if len(out) > MAX_OUTPUT_CHARS:
            log.warning("Tool %s output truncated: %d chars", name, len(out))
            out = out[:MAX_OUTPUT_CHARS] + "\n...[truncated - request a shorter range]"
        log.debug("Tool result %s: %s", name, out if len(out) < 90 else f"{len(out)} chars", extra=QUOTES)
        turn.result_chars += len(out)
        return out
