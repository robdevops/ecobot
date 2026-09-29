"""Connects to MCP servers (stdio, streamable HTTP, or SSE) and exposes their
tools in OpenAI function-calling format."""

import asyncio
import json
import logging
import os
import re
import shlex
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import get_default_environment, stdio_client

try:  # mcp SDK >= 2.0
    from mcp.client.streamable_http import streamable_http_client as _new_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
except ImportError:  # mcp SDK 1.x
    _new_http_client = None
    from mcp.client.streamable_http import streamablehttp_client as _old_http_client

log = logging.getLogger(__name__)
_BAD_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def _expand(value):
    """Expand ${VAR} / $VAR from the bot's environment in config strings."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


_EPOCH_RE = re.compile(r'(?<![\d.])(\d{13}|\d{10})(?![\d.])')
_ISO_RE = re.compile(
    r'\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)(Z|[+-]\d{2}:?\d{2})(?![\d:])')
_EPOCH_MIN, _EPOCH_MAX = 946684800, 4102444800  # years 2000-2100


def localise_times(text: str, tz: tzinfo) -> str:
    """Rewrite Unix timestamps (s or ms) and ISO times with a UTC offset as
    local time, e.g. 'Sat 2026-09-26 17:05', so the model doesn't have to do
    timezone maths. Kept short because history data repeats it hundreds of times."""
    def fmt(dt: datetime) -> str:
        return dt.astimezone(tz).strftime("%a %Y-%m-%d %H:%M")

    def epoch(m):
        raw = m.group(1)
        secs = int(raw) / 1000 if len(raw) == 13 else int(raw)
        if not _EPOCH_MIN <= secs <= _EPOCH_MAX:
            return raw
        local = fmt(datetime.fromtimestamp(secs, timezone.utc))
        quoted = m.start() > 0 and m.string[m.start() - 1] == '"'
        return local if quoted else f'"{local}"'

    def iso(m):
        offset = m.group(2).replace("Z", "+00:00")
        if len(offset) == 5:  # +1000 -> +10:00
            offset = offset[:3] + ":" + offset[3:]
        try:
            return fmt(datetime.fromisoformat(m.group(1).replace(" ", "T") + offset))
        except ValueError:
            return m.group(0)

    return _EPOCH_RE.sub(epoch, _ISO_RE.sub(iso, text))


def _breakdown(points: list, series: dict, tz: tzinfo) -> tuple[str, dict]:
    """Per-local-day (up to 31 days) or per-month min/max with times, for series
    keyed by Unix timestamps. Returns (key name, breakdown)."""
    stamped = []
    for ts, val in points:
        try:
            stamped.append((datetime.fromtimestamp(int(ts), timezone.utc).astimezone(tz), val, ts))
        except (ValueError, OverflowError, OSError):
            return "", {}
    n_days = len({dt.date() for dt, _, _ in stamped})
    if n_days < 2:
        return "", {}
    if n_days <= 31:
        name, group, when = "daily", "%a %d %b", "%H:%M"
    else:
        name, group, when = "monthly", "%b %Y", "%a %d %b %H:%M"
    buckets: dict = {}
    for dt, val, ts in stamped:
        buckets.setdefault(dt.strftime(group), []).append((dt.strftime(when), val, ts))
    out = {}
    for key, vals in buckets.items():
        lo = min(vals, key=lambda v: v[1])
        hi = max(vals, key=lambda v: v[1])
        out[key] = {"min": series[lo[2]], "min_time": lo[0], "max": series[hi[2]], "max_time": hi[0]}
    return name, out


def summarise_series(obj, max_points: int, tz: tzinfo = timezone.utc):
    """Walk parsed JSON. For every {"list": {timestamp: value, ...}} series, add
    min/max with their timestamps plus a daily/monthly breakdown, and replace lists longer
    than `max_points` with that summary. Saves tokens and spares the model
    scanning hundreds of values."""
    if isinstance(obj, list):
        return [summarise_series(v, max_points, tz) for v in obj]
    if not isinstance(obj, dict):
        return obj
    out = {k: summarise_series(v, max_points, tz) for k, v in obj.items()}
    series = obj.get("list")
    if isinstance(series, dict) and series:
        points = []
        for ts, val in series.items():
            try:
                points.append((ts, float(val)))
            except (TypeError, ValueError):
                continue
        if points:
            lo = min(points, key=lambda p: p[1])
            hi = max(points, key=lambda p: p[1])
            out.update(min=series[lo[0]], min_time=lo[0], max=series[hi[0]], max_time=hi[0],
                       points=len(series))
            name, breakdown = _breakdown(points, series, tz)
            if breakdown:
                out[name] = breakdown
            if len(series) > max_points:
                out["list"] = f"omitted ({len(series)} points); use the summary fields"
    return out


def _compact(text: str, max_points: int, tz: tzinfo = timezone.utc) -> str:
    """Summarise and minify JSON tool output; leave anything else unchanged."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    return json.dumps(summarise_series(data, max_points, tz), ensure_ascii=False, separators=(",", ":"))


def _attr(obj, snake: str, camel: str, default=None):
    """mcp SDK 2.x uses snake_case fields, 1.x uses camelCase."""
    return getattr(obj, snake, None) or getattr(obj, camel, None) or default


@dataclass
class ToolRef:
    server: str
    name: str
    session: ClientSession


class MCPManager:
    def __init__(self, config_path: str, tz: tzinfo, tool_timeout: float = 60,
                 max_output_chars: int = 60000, max_series_points: int = 12):
        self.config_path = Path(config_path)
        self.tz = tz
        self.tool_timeout = tool_timeout
        self.max_output_chars = max_output_chars
        self.max_series_points = max_series_points
        self._stack = AsyncExitStack()
        self.tools: dict[str, ToolRef] = {}
        # oname -> async fn(args: dict) -> str; replaces the plain call for that tool
        self.interceptors: dict = {}
        self.openai_tools: list[dict] = []

    async def __aenter__(self):
        await self._stack.__aenter__()
        if not self.config_path.exists():
            log.warning("MCP config %s not found - running without tools", self.config_path)
            return self
        cfg = json.loads(self.config_path.read_text())
        for name, spec in cfg.get("mcpServers", {}).items():
            if spec.get("disabled"):
                continue
            try:
                await self._connect(name, spec)
            except Exception:
                log.exception("Failed to connect MCP server '%s' - skipping", name)
        log.debug("MCP ready: %d tools from %d servers", len(self.tools),
                 len({t.server for t in self.tools.values()}))
        return self

    async def __aexit__(self, *exc):
        return await self._stack.__aexit__(*exc)

    async def _connect(self, name: str, spec: dict):
        stack = AsyncExitStack()
        try:
            spec = _expand(spec)
            if "command" in spec:
                command, args = spec["command"], list(spec.get("args", []))
                if " " in command.strip() and not args:
                    # Accept "npx -y some-server" as a single string
                    command, *args = shlex.split(command)
                env = get_default_environment()
                if os.getenv("TZ"):
                    env["TZ"] = os.environ["TZ"]  # so the server's "today" matches ours
                for var, value in spec.get("env", {}).items():
                    # an unset ${VAR} stays literal, and an empty one is "": leave both out,
                    # so optional settings (e.g. an API token) are simply absent
                    if value and "${" not in str(value):
                        env[var] = value
                    elif var not in env:
                        log.info("MCP server '%s': %s not set, leaving it out", name, var)
                params = StdioServerParameters(
                    command=command, args=args,
                    env=env, cwd=spec.get("cwd"),
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            elif "url" in spec:
                headers = spec.get("headers")
                if spec.get("transport", "streamable_http") == "sse":
                    read, write = await stack.enter_async_context(sse_client(spec["url"], headers=headers))
                else:
                    if _new_http_client:
                        http = await stack.enter_async_context(create_mcp_http_client(headers=headers))
                        streams = await stack.enter_async_context(
                            _new_http_client(spec["url"], http_client=http))
                    else:
                        streams = await stack.enter_async_context(
                            _old_http_client(spec["url"], headers=headers))
                    read, write = streams[0], streams[1]
            else:
                raise ValueError("server needs either 'command' or 'url'")

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listed = await session.list_tools()
        except BaseException:
            await stack.aclose()
            raise

        self._stack.push_async_callback(stack.aclose)

        allowed = set(spec.get("allowedTools", []))
        for tool in listed.tools:
            if allowed and tool.name not in allowed:
                continue
            oname = _BAD_CHARS.sub("_", f"{name}__{tool.name}")[:64]
            self.tools[oname] = ToolRef(name, tool.name, session)
            self.openai_tools.append({
                "type": "function",
                "function": {
                    "name": oname,
                    "description": (tool.description or "")[:1024],
                    "parameters": _attr(tool, "input_schema", "inputSchema", {"type": "object", "properties": {}}),
                },
            })
        log.info("Connected MCP server '%s' (%d tools)", name, len(listed.tools))

    def add_local_tool(self, name: str, description: str, parameters: dict, handler) -> str:
        """A tool implemented in the bot itself (no MCP server): handler(args) -> str."""
        oname = _BAD_CHARS.sub("_", name)[:64]
        self.tools[oname] = ToolRef("local", name, None)
        self.interceptors[oname] = handler
        self.openai_tools.append({"type": "function",
                                  "function": {"name": oname, "description": description, "parameters": parameters}})
        return oname

    def remove_tool(self, oname: str):
        self.tools.pop(oname, None)
        self.openai_tools[:] = [t for t in self.openai_tools if t["function"]["name"] != oname]

    def describe(self) -> str:
        if not self.tools:
            return "No MCP tools loaded."
        lines = []
        for oname, ref in self.tools.items():
            lines.append(f"- {ref.server}: {ref.name}")
        return "\n".join(lines)

    async def call_raw(self, oname: str, args: dict) -> tuple[list[str], bool]:
        """Call a tool and return its raw text parts (unprocessed) and error flag.
        Raises on timeout or transport errors."""
        ref = self.tools[oname]
        result = await asyncio.wait_for(ref.session.call_tool(ref.name, args), self.tool_timeout)
        parts = []
        for c in result.content:
            if c.type == "text":
                parts.append(c.text)
            elif c.type == "image":
                parts.append(f"[image returned ({_attr(c, 'mime_type', 'mimeType')}) - not shown]")
            elif c.type == "resource" and hasattr(c.resource, "text"):
                parts.append(c.resource.text)
            else:
                parts.append(f"[{c.type} content]")
        structured = _attr(result, "structured_content", "structuredContent")
        if not parts and structured:
            parts.append(json.dumps(structured))
        return parts, bool(_attr(result, "is_error", "isError", False))

    async def call(self, oname: str, raw_args: str, quiet: bool = False) -> str:
        """Call a tool for the model. quiet=True logs at debug level (the bot's own calls)."""
        say = log.debug if quiet else log.info
        ref = self.tools.get(oname)
        if not ref:
            return f"Error: unknown tool '{oname}'"
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError as e:
            return f"Error: tool arguments were not valid JSON ({e})"

        say("Tool call %s.%s %s", ref.server, ref.name, raw_args[:300])
        try:
            if oname in self.interceptors:
                out = await self.interceptors[oname](args)
            else:
                parts, is_error = await self.call_raw(oname, args)
                out = "\n".join(_compact(p, self.max_series_points, self.tz) for p in parts)
                if is_error:
                    out = "Tool reported an error:\n" + out
        except asyncio.TimeoutError:
            return f"Error: tool timed out after {self.tool_timeout}s"
        except Exception as e:
            log.exception("Tool %s failed", oname)
            return f"Error calling tool: {e}"

        out = localise_times(out, self.tz) or "(no output)"
        if len(out) > self.max_output_chars:
            log.warning("Tool %s output truncated: %d -> %d chars", oname, len(out), self.max_output_chars)
            out = out[: self.max_output_chars] + "\n...[truncated - request a shorter range]"
        if len(out) < 300:  # errors and empty results: show them in full
            say("Tool result %s.%s: %s", ref.server, ref.name, out)
        else:
            say("Tool result %s.%s: %d chars", ref.server, ref.name, len(out))
        return out
