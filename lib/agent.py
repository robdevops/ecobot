"""Tool-calling loop against an OpenAI-compatible API (xAI Grok)."""

import asyncio
import json
import logging
import time

from openai import AsyncOpenAI

from .mcp_manager import MCPManager

log = logging.getLogger(__name__)


class Agent:
    def __init__(self, client: AsyncOpenAI, model: str, mcp: MCPManager,
                 max_steps: int = 8, reasoning_effort: str = "none"):
        self.client = client
        self.model = model
        self.mcp = mcp
        self.max_steps = max_steps
        self.reasoning_effort = reasoning_effort

    async def _call_tool(self, cache: dict, name: str, args: str) -> str:
        key = (name, args)
        if key not in cache:
            cache[key] = asyncio.ensure_future(self.mcp.call(name, args))
            return await cache[key]
        log.info("Tool call %s served from cache (repeat)", name)
        return ("[You already made this exact call - same result as before. Don't repeat it; "
                "change the request or answer now.]\n" + await cache[key])

    async def _complete(self, kwargs: dict, call_no: int) -> tuple[str, list[dict], float]:
        """One LLM call. Returns (content, tool_calls, seconds) and logs timing/usage."""
        started = time.monotonic()
        resp = await self.client.chat.completions.create(**kwargs)
        elapsed = time.monotonic() - started
        msg = resp.choices[0].message
        calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                 for tc in msg.tool_calls or []]
        content = msg.content or ""
        reasoning_chars = len(getattr(msg, "reasoning_content", None) or "")  # returned by some providers

        detail = "no usage reported"
        usage = resp.usage
        if usage:
            cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
            reasoning_tok = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
            detail = (f"input {usage.prompt_tokens} tok (cached {cached if cached is not None else 'n/a'}), "
                      f"output {usage.completion_tokens} tok "
                      f"(reasoning {reasoning_tok if reasoning_tok is not None else 'n/a'})")
        log.info("LLM call %d (%s): %.1fs, %s, reasoning text %d chars, %d tool call(s), %d chars answer",
                 call_no, kwargs["extra_body"]["reasoning_effort"], elapsed, detail, reasoning_chars, len(calls), len(content))
        return content, calls, elapsed

    async def run(self, messages: list[dict], system_prompt: str, reasoning_effort: str | None = None,
                  first_tool_call: tuple[str, dict] | None = None, require_tool: bool = True) -> str:
        """Runs the tool loop. Appends assistant/tool turns to `messages` in place.
        The prompt is passed per question (not stored) so concurrent chats can't clash;
        reasoning_effort overrides the default for this question only. first_tool_call
        (tool name, args) is a call the bot already worked out: it runs straight away
        and the model's first round trip is skipped. require_tool forces a fresh fetch on
        the first call (for weather questions); off for chat, so it can just reply."""
        effort = reasoning_effort or self.reasoning_effort
        cache: dict = {}  # identical tool calls within one question are only made once
        llm_time = tool_time = 0.0
        first_step = 0
        try:
            if first_tool_call:
                name, args = first_tool_call
                arguments = json.dumps(args)
                messages.append({"role": "assistant", "content": "", "tool_calls": [
                    {"id": "fast_1", "type": "function", "function": {"name": name, "arguments": arguments}}]})
                t0 = time.monotonic()
                result = await self._call_tool(cache, name, arguments)
                tool_time += time.monotonic() - t0
                messages.append({"role": "tool", "tool_call_id": "fast_1", "content": result})
                first_step = 1  # data is in; the model just answers (and may still call tools)
            for step in range(first_step, self.max_steps + 1):
                final = step == self.max_steps  # out of steps: force an answer from what we have
                kwargs = {
                    "model": self.model,
                    "messages": [{"role": "system", "content": system_prompt}, *messages],
                    # Grok 4.3: none / low / medium / high ("none" is fastest)
                    "extra_body": {"reasoning_effort": effort},
                }
                if self.mcp.openai_tools:
                    kwargs["tools"] = self.mcp.openai_tools
                    # Weather questions must fetch fresh data first, never reuse remembered numbers.
                    # Other messages (thanks, chat) may just be answered.
                    kwargs["tool_choice"] = "none" if final else (
                        "required" if step == 0 and require_tool else "auto")
                if final:
                    log.warning("Tool step limit (%d) reached - asking for a final answer", self.max_steps)
                    kwargs["messages"].append({"role": "user", "content":
                        "(System: tool limit reached. Answer now using only the data already fetched, "
                        "and briefly say if anything is missing.)"})

                content, tool_calls, secs = await self._complete(kwargs, step + 1)
                llm_time += secs

                entry = {"role": "assistant", "content": content}
                if tool_calls and not final:
                    entry["tool_calls"] = [
                        {"id": tc["id"], "type": "function",
                         "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for tc in tool_calls
                    ]
                messages.append(entry)

                if not tool_calls or final:
                    return content.strip() or "Sorry, I couldn't get an answer together - try a narrower question."

                t0 = time.monotonic()
                results = await asyncio.gather(
                    *(self._call_tool(cache, tc["name"], tc["arguments"]) for tc in tool_calls))
                tool_time += time.monotonic() - t0
                for tc, result in zip(tool_calls, results):
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        finally:
            log.info("Timing: LLM %.1fs, tools %.1fs", llm_time, tool_time)


def strip_tool_turns(messages: list[dict]) -> list[dict]:
    """Keep only the questions and final answers, dropping tool calls and raw tool
    results, so later questions can't reuse old data (and requests stay small)."""
    return [m for m in messages
            if m["role"] == "user" or (m["role"] == "assistant" and not m.get("tool_calls") and m.get("content"))]


def trim_history(messages: list[dict], max_messages: int) -> list[dict]:
    """Keep roughly the last `max_messages`, always starting on a user turn so
    tool results are never orphaned from their tool calls. If the latest turn
    alone is longer than the limit, keep that whole turn."""
    user_idx = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if not user_idx:
        return []
    start = len(messages) - max_messages
    later = [i for i in user_idx if i >= start]
    return messages[later[0] if later else user_idx[-1]:]
