"""The tools the model can call. Each data source contributes its own."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 60000
TOOL_TIMEOUT = 90


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], Awaitable[str]]

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


class Tools:
    def __init__(self, tools: list[Tool]):
        self.by_name = {t.name: t for t in tools}
        self.schemas = [t.schema() for t in tools]

    def __contains__(self, name: str) -> bool:
        return name in self.by_name

    async def call(self, name: str, raw_args: str) -> str:
        """Run a tool for the model; failures come back as text the model can explain."""
        tool = self.by_name.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError as e:
            return f"Error: tool arguments were not valid JSON ({e})"
        log.info("Tool call %s %s", name, raw_args[:300])
        try:
            out = await asyncio.wait_for(tool.handler(args), TOOL_TIMEOUT)
        except asyncio.TimeoutError:
            return f"Error: tool timed out after {TOOL_TIMEOUT}s"
        except Exception as e:
            log.exception("Tool %s failed", name)
            return f"Error calling tool: {e}"
        out = out or "(no output)"
        if len(out) > MAX_OUTPUT_CHARS:
            log.warning("Tool %s output truncated: %d chars", name, len(out))
            out = out[:MAX_OUTPUT_CHARS] + "\n...[truncated - request a shorter range]"
        log.info("Tool result %s: %s", name, out if len(out) < 300 else f"{len(out)} chars")
        return out
