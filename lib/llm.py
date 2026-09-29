"""Tool-calling loop against an OpenAI-compatible API (xAI Grok)."""

import asyncio
import json
import logging
import time

from openai import AsyncOpenAI

from .tools import Tools

log = logging.getLogger(__name__)

MAX_STEPS = 10
MAX_HISTORY = 40  # messages kept per chat

LIMIT_NOTICE = ("(System: tool limit reached. Answer now using only the data already fetched, "
                "and briefly say if anything is missing.)")


class Agent:
    def __init__(self, client: AsyncOpenAI, model: str, tools: Tools):
        self.client, self.model, self.tools = client, model, tools

    async def _complete(self, kwargs: dict, call_no: int) -> tuple[str, list[dict], float]:
        """One LLM call. Returns (content, tool_calls, seconds) and logs timing/usage."""
        started = time.monotonic()
        resp = await self.client.chat.completions.create(**kwargs)
        elapsed = time.monotonic() - started
        msg = resp.choices[0].message
        calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                 for tc in msg.tool_calls or []]
        content = msg.content or ""
        detail = "no usage"
        if usage := resp.usage:
            cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
            thought = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
            detail = (f"in {usage.prompt_tokens} ({'?' if cached is None else cached} cached), "
                      f"out {usage.completion_tokens} ({'?' if thought is None else thought} thinking)")
        log.info("LLM %d (%s) %.1fs: %s, %d tools, %d chars", call_no, kwargs["extra_body"]["reasoning_effort"],
                 elapsed, detail, len(calls), len(content))
        return content, calls, elapsed

    async def run(self, messages: list[dict], system_prompt: str, effort: str,
                  first_call: tuple[str, dict] | None = None, require_tool: bool = True) -> str:
        """Runs the tool loop, appending assistant/tool turns to `messages` in place. The prompt
        is passed per question (not stored) so concurrent chats can't clash.

        first_call (tool name, args) is a call the bot already worked out (the fast path): it runs
        straight away and the model is only invoked once the data is in. require_tool forces a
        fresh fetch on the first model call (weather questions); off for chat, so it can just reply."""
        cache: dict = {}  # identical tool calls within one question are only made once

        async def call(name: str, args: str) -> str:
            if (name, args) not in cache:
                cache[(name, args)] = asyncio.ensure_future(self.tools.call(name, args))
                return await cache[(name, args)]
            log.info("Tool call %s served from cache (repeat)", name)
            return ("[You already made this exact call - same result as before. Don't repeat it; "
                    "change the request or answer now.]\n" + await cache[(name, args)])

        llm_time = tool_time = 0.0
        first_step = 0
        try:
            if first_call:
                name, args = first_call[0], json.dumps(first_call[1])
                messages.append({"role": "assistant", "content": "", "tool_calls": [
                    {"id": "fast_1", "type": "function", "function": {"name": name, "arguments": args}}]})
                t0 = time.monotonic()
                result = await call(name, args)
                tool_time += time.monotonic() - t0
                messages.append({"role": "tool", "tool_call_id": "fast_1", "content": result})
                first_step = 1  # data is in; the model just answers (and may still call tools)
            for step in range(first_step, MAX_STEPS + 1):
                final = step == MAX_STEPS  # out of steps: force an answer from what we have
                kwargs = {"model": self.model,
                          "messages": [{"role": "system", "content": system_prompt}, *messages],
                          "extra_body": {"reasoning_effort": effort}}  # Grok 4.3: none / low / medium / high
                if self.tools.schemas:
                    kwargs["tools"] = self.tools.schemas
                    kwargs["tool_choice"] = "none" if final else "required" if step == 0 and require_tool else "auto"
                if final:
                    log.warning("Tool step limit (%d) reached - asking for a final answer", MAX_STEPS)
                    kwargs["messages"].append({"role": "user", "content": LIMIT_NOTICE})

                content, tool_calls, secs = await self._complete(kwargs, step + 1)
                llm_time += secs
                entry = {"role": "assistant", "content": content}
                if tool_calls and not final:
                    entry["tool_calls"] = [{"id": tc["id"], "type": "function",
                                            "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                                           for tc in tool_calls]
                messages.append(entry)
                if not tool_calls or final:
                    return content.strip() or "Sorry, I couldn't get an answer together - try a narrower question."

                t0 = time.monotonic()
                results = await asyncio.gather(*(call(tc["name"], tc["arguments"]) for tc in tool_calls))
                tool_time += time.monotonic() - t0
                for tc, result in zip(tool_calls, results):
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        finally:
            log.info("Timing: LLM %.1fs, tools %.1fs", llm_time, tool_time)


def strip_tool_turns(messages: list[dict]) -> list[dict]:
    """Keep only the questions and final answers, dropping tool calls and raw tool results, so
    later questions can't reuse old data (and requests stay small)."""
    return [m for m in messages
            if m["role"] == "user" or (m["role"] == "assistant" and not m.get("tool_calls") and m.get("content"))]


def trim_history(messages: list[dict], max_messages: int = MAX_HISTORY) -> list[dict]:
    """Keep roughly the last `max_messages`, always starting on a user turn. If the latest turn
    alone is longer than the limit, keep that whole turn."""
    user_idx = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if not user_idx:
        return []
    later = [i for i in user_idx if i >= len(messages) - max_messages]
    return messages[later[0] if later else user_idx[-1]:]
