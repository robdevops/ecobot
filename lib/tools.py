"""The tools the model can call. Each data source contributes its own."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .logs import QUOTES

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 60000


def json_result(out) -> str:
    """A tool's result as the model is sent it: compact JSON, with °, ² and emoji written as they are, not escaped."""
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Turn:
    """What one question carries between the bot and the tools it calls (nothing is shared between questions)."""
    charts: list = field(default_factory=list)   # chart specs the tools add; the bot renders them after the answer
    chart_asked: bool = False                    # the person's words asked for a chart ("plot"), whatever the model sets
    chart_field: str | None = None               # the one reading the question is about ("humidity"); None: temperature
    chart_fields: list[str] = field(default_factory=list)   # readings asked to be seen together ("temperature and rain")
    two_axes: bool = False                       # the words ask for two readings on one chart with a scale each ("on two axes")
    air_extra: bool = False                      # the words ask for the air sensor's own temperature or humidity: the tools then offer them
    average_asked: bool = False                  # an average was asked for: the caption leads with it
    text: str = ""                               # what the person said (a tool can check an argument against it)
    readings: list[str] = field(default_factory=list)   # the readings the words name ("temperature", "rain"): a result can leave out the others
    per_day: bool = False                        # day by day figures were asked for: a long period keeps every day, not only its extremes
    result_chars: int = 0                        # the size of every tool result so far this question (what the model will be sent)
    forecast_shown: list = field(default_factory=list)   # the forecast days the answer includes, remembered once it is sent
    times: dict = field(default_factory=dict)    # seconds spent by step ("data", "draw", "png"), for the reply's timing line in the log
    ahead: bool = False                          # a chart drawn ahead, nobody waiting: its requests wait their turn and its routine log lines are quiet

    @classmethod
    def of(cls, read, text: str, **more) -> "Turn":
        """The Turn for a question as intent.read read it (`read`): what its words ask of a chart and of the figures."""
        return cls(chart_asked=read.chart_asked, chart_field=read.chart_field, chart_fields=read.chart_fields, two_axes=read.two_axes, air_extra=read.air_extra,
                   average_asked=read.average_asked, readings=read.readings, per_day=read.per_day, text=text, **more)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, Turn], Awaitable[str]]   # (arguments, the question's Turn); a Turn is optional when called directly
    asked: Callable[[], tuple[str, dict]] | None = None   # (description, parameters) a question that asks for the air sensor's own
                                                          # temperature or humidity is offered instead (Turn.air_extra)

    def schema(self, turn: "Turn | None" = None) -> dict:
        description, parameters = self.asked() if turn and turn.air_extra and self.asked else (self.description, self.parameters)
        return {"type": "function", "function": {"name": self.name, "description": description, "parameters": parameters}}


class Tools:
    def __init__(self, tools: list[Tool]):
        self.by_name = {t.name: t for t in tools}
        self.schemas = [t.schema() for t in tools]

    def schemas_for(self, turn: "Turn | None") -> list[dict]:
        """The tool list for this question: every tool, the same list as every other question's (so the provider's cached prompt holds),
        except that a question asking for the air sensor's own temperature or humidity is offered them in the tools that can use them."""
        if turn and turn.air_extra and any(t.asked for t in self.by_name.values()):
            return [t.schema(turn) for t in self.by_name.values()]
        return self.schemas

    async def call(self, name: str, raw_args: str, turn: Turn | None = None) -> str:
        """Run a tool for the model; failures come back as text the model can explain."""
        tool = self.by_name.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError as e:
            return f"Error: tool arguments were not valid JSON ({e})"
        level = logging.DEBUG if turn and turn.ahead else logging.INFO
        log.log(level, "Tool call %s %s", name, raw_args, extra=QUOTES)
        try:   # no time limit of its own: the question's (bot.TURN_SECONDS) runs from before the call
            out = await tool.handler(args, turn or Turn())
        except Exception as e:
            log.exception("Tool %s failed", name)
            return f"Error calling tool: {e}"
        out = out or "(no output)"
        if len(out) > MAX_OUTPUT_CHARS:
            log.warning("Tool %s output truncated: %d chars", name, len(out))
            out = out[:MAX_OUTPUT_CHARS] + "\n...[truncated - request a shorter range]"
        log.log(level, "Tool result %s: %s", name, out if len(out) < 90 else f"{len(out)} chars", extra=QUOTES)
        if turn:
            turn.result_chars += len(out)
        return out

    async def call_all(self, calls: list[tuple[str, dict]], turn: Turn | None = None) -> list[str]:
        """The bot's own calls (name, arguments), made together; their results in the same order."""
        return await asyncio.gather(*(self.call(name, json.dumps(args), turn) for name, args in calls))
