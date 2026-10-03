"""Tool-calling loop against an OpenAI-compatible API (xAI Grok)."""

import asyncio
import json
import logging
import time

from openai import AsyncOpenAI

from .tools import Tools, Turn

log = logging.getLogger(__name__)

MAX_STEPS = 10
WARNING_SIGN = "\u26a0"   # a reply that starts with this, alongside tool calls, is a heads-up for the chat (see the prompt)
MAX_HISTORY = 16  # messages kept per chat (a follow-up needs the last few, every call resends them)

LIMIT_NOTICE = ("(System: tool limit reached. Answer now using only the data already fetched, "
                "and briefly say if anything is missing.)")


class Agent:
    def __init__(self, client: AsyncOpenAI, model: str, tools: Tools):
        self.client, self.model, self.tools = client, model, tools

    async def _complete(self, kwargs: dict, call_no: int, on_text=None) -> tuple[str, list[dict], float]:
        """One LLM call. Returns (content, tool_calls, seconds) and logs timing/usage. With on_text the answer is streamed
        and on_text(text so far) is called as it grows."""
        started = time.monotonic()
        if on_text:
            content, calls, usage = await self._stream(kwargs, on_text)
        else:
            resp = await self.client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                     for tc in msg.tool_calls or []]
            content, usage = msg.content or "", resp.usage
        elapsed = time.monotonic() - started
        detail = "no usage"
        if usage:
            cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
            thought = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
            detail = (f"in {usage.prompt_tokens} ({'?' if cached is None else cached} cached), "
                      f"out {usage.completion_tokens} ({'?' if thought is None else thought} thinking)")
        log.info("LLM %d (%s%s) %.1fs: %s, %d tools, %d chars", call_no, kwargs["extra_body"]["reasoning_effort"],
                 ", streamed" if on_text else "", elapsed, detail, len(calls), len(content))
        return content, calls, elapsed

    async def _stream(self, kwargs: dict, on_text) -> tuple[str, list[dict], object]:
        """The same answer as a plain call, assembled from the streamed pieces: (content, tool_calls, usage)."""
        content, calls, usage = "", {}, None
        stream = await self.client.chat.completions.create(**kwargs, stream=True, stream_options={"include_usage": True})
        async for chunk in stream:
            usage = getattr(chunk, "usage", None) or usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            for tc in getattr(delta, "tool_calls", None) or []:
                call = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                call["id"] = tc.id or call["id"]
                if fn := tc.function:
                    call["name"] += fn.name or ""
                    call["arguments"] += fn.arguments or ""
            if piece := getattr(delta, "content", None):
                content += piece
                on_text(content)
        return content, [calls[i] for i in sorted(calls)], usage

    async def run(self, messages: list[dict], system_prompt: str, effort: str,
                  first_call: tuple[str, dict] | list[tuple[str, dict]] | None = None, require_tool: bool = True, no_tools: bool = False,
                  turn: Turn | None = None, on_text=None, tool_names: list[str] | None = None, on_note=None) -> str:
        """Runs the tool loop, appending assistant/tool turns to `messages` in place. The prompt
        is passed per question (not stored) so concurrent chats can't clash.

        first_call (tool name, args), or a list of them, is what the bot already worked out (the fast path): the calls run
        straight away, together, and the model is only invoked once the data is in. require_tool forces a
        fresh fetch on the first model call (weather questions); off for chat, so it can just reply.
        on_text(text so far) is called as each answer streams in (private chats show it as a draft). on_note(text) is called with a
        warning the model wrote before a big job: its reply to a step that also calls tools and starts with a warning sign."""
        cache: dict = {}  # identical tool calls within one question are only made once

        async def call(name: str, args: str) -> str:
            if (name, args) not in cache:
                cache[(name, args)] = asyncio.ensure_future(self.tools.call(name, args, turn))
                return await cache[(name, args)]
            log.info("Tool call %s served from cache (repeat)", name)
            return ("[You already made this exact call - same result as before. Don't repeat it; "
                    "change the request or answer now.]\n" + await cache[(name, args)])

        schemas = self.tools.schemas_for(tool_names)   # only the tools the question can use: each definition is sent on every call
        llm_time = tool_time = 0.0
        first_step = 0
        try:
            if first_call:
                fast = [(n, json.dumps(a)) for n, a in (first_call if isinstance(first_call, list) else [first_call])]
                messages.append({"role": "assistant", "content": "", "tool_calls": [
                    {"id": f"fast_{i}", "type": "function", "function": {"name": n, "arguments": a}}
                    for i, (n, a) in enumerate(fast, 1)]})
                t0 = time.monotonic()
                results = await asyncio.gather(*(call(n, a) for n, a in fast))
                tool_time += time.monotonic() - t0
                messages.extend({"role": "tool", "tool_call_id": f"fast_{i}", "content": r} for i, r in enumerate(results, 1))
                first_step = 1  # data is in; the model just answers (and may still call tools)
            for step in range(first_step, MAX_STEPS + 1):
                final = step == MAX_STEPS  # out of steps: force an answer from what we have
                kwargs = {"model": self.model,
                          "messages": [{"role": "system", "content": system_prompt}, *messages],
                          "extra_body": {"reasoning_effort": effort}}  # Grok 4.3: none / low / medium / high
                if schemas:
                    kwargs["tools"] = schemas
                    kwargs["tool_choice"] = ("none" if final or no_tools else
                                             "required" if step == 0 and require_tool else "auto")
                if final:
                    log.warning("Tool step limit (%d) reached - asking for a final answer", MAX_STEPS)
                    kwargs["messages"].append({"role": "user", "content": LIMIT_NOTICE})

                content, tool_calls, secs = await self._complete(kwargs, step + 1, on_text)
                llm_time += secs
                if on_note and tool_calls and not final and content.strip().startswith(WARNING_SIGN):
                    await on_note(content.strip())
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
