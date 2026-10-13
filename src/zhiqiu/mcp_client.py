"""Connect to local stdio MCP servers and expose whitelisted tools through the tool layer.

Adapted from waku's client (daemon event-loop thread, sync bridge), with its gaps closed:
isError is checked, timeouts cancel the call, server and tool names are passed
explicitly, name clashes are refused and a server that fails to start is skipped
and audited. Every session is opened and closed inside its own task, as anyio requires.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Any, Callable

from .materials import Material
from .tools import ToolError, ToolSpec

MCP_PERMISSIONS = ("read", "network", "external_mcp")
CONNECT_TIMEOUT_S = 30.0
DEFAULT_CALL_TIMEOUT_S = 30.0
DEFAULT_OUTPUT_CHARS = 6000


def load_config(path: Path) -> dict[str, Any]:
    """Validate {"servers": {name: {"command", "args", "env", "tools": {tool: {"permission", "timeout_s"}}}}}."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 MCP 配置：{path}") from exc
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("MCP 配置必须包含 servers 对象")
    for name, server in servers.items():
        if not name.isidentifier() or "__" in name:
            raise ValueError(f"MCP 服务名只能是字母数字下划线且不含连续下划线：{name}")
        if not isinstance(server, dict) or not isinstance(server.get("command"), str):
            raise ValueError(f"MCP 服务 {name} 缺少 command")
        if not isinstance(server.get("args", []), list) or not isinstance(server.get("env", {}), dict):
            raise ValueError(f"MCP 服务 {name} 的 args 必须是数组、env 必须是对象")
        tools = server.get("tools")
        if not isinstance(tools, dict) or not tools:
            raise ValueError(f"MCP 服务 {name} 必须列出允许使用的工具（白名单）")
        for tool, rule in tools.items():
            rule = rule or {}
            if rule.get("permission", "external_mcp") not in MCP_PERMISSIONS:
                raise ValueError(f"MCP 工具 {name}.{tool} 的权限只能是 {', '.join(MCP_PERMISSIONS)}")
            if not isinstance(rule.get("timeout_s", DEFAULT_CALL_TIMEOUT_S), (int, float)):
                raise ValueError(f"MCP 工具 {name}.{tool} 的 timeout_s 必须是数字")
    return servers


def _strict(schema: Any) -> dict[str, Any]:
    """The tool layer requires closed object schemas; MCP servers often leave them open."""
    result = dict(schema) if isinstance(schema, dict) else {}
    result["type"] = "object"
    result.setdefault("properties", {})
    result["additionalProperties"] = False
    return result


class McpClientPool:
    def __init__(self, servers: dict[str, Any], audit: Callable[[dict[str, Any]], None] | None = None,
                 errlog_path: Path | None = None, connect_timeout_s: float = CONNECT_TIMEOUT_S):
        self.servers = servers
        self.audit = audit
        self.errlog_path = errlog_path
        self.connect_timeout_s = connect_timeout_s
        self.sessions: dict[str, Any] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="zhiqiu-mcp", daemon=True)
        self._thread.start()
        self._stop = self._submit(self._make_event()).result()
        self._owners: list[Any] = []
        self._errlog: Any = None

    @staticmethod
    async def _make_event() -> asyncio.Event:
        return asyncio.Event()

    def _submit(self, coroutine: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    async def _own(self, name: str, server: dict[str, Any], started: asyncio.Future) -> None:
        """Hold one session open in this task until close(); entering and exiting share the task."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=server["command"], args=[str(a) for a in server.get("args", [])],
                                       env={**os.environ, **{k: str(v) for k, v in server.get("env", {}).items()}})
        try:
            async with stdio_client(params, errlog=self._errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), self.connect_timeout_s)
                    listed = await asyncio.wait_for(session.list_tools(), self.connect_timeout_s)
                    started.set_result((session, listed.tools))
                    await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - report every start failure to the caller
            if not started.done():
                started.set_exception(exc if isinstance(exc, Exception) else RuntimeError(type(exc).__name__))
            elif not isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                raise

    async def _start_one(self, name: str, server: dict[str, Any]) -> tuple[Any, list[Any]]:
        started: asyncio.Future = asyncio.get_running_loop().create_future()
        self._owners.append(asyncio.ensure_future(self._own(name, server, started)))
        return await asyncio.wait_for(started, self.connect_timeout_s + 5)

    def start(self) -> list[ToolSpec]:
        """Connect every configured server; return specs for whitelisted tools the server offers."""
        if self.errlog_path is not None:
            self.errlog_path.parent.mkdir(parents=True, exist_ok=True)
            self._errlog = self.errlog_path.open("a", encoding="utf-8")
        else:
            self._errlog = open(os.devnull, "w", encoding="utf-8")
        specs: list[ToolSpec] = []
        for name, server in self.servers.items():
            began = time.perf_counter()
            try:
                session, offered = self._submit(self._start_one(name, server)).result()
            except Exception as exc:  # noqa: BLE001 - one broken server must not stop the others
                self._audit_failure(name, exc, began)
                continue
            self.sessions[name] = session
            rules = server["tools"]
            for tool in offered:
                if tool.name not in rules:
                    continue
                rule = rules[tool.name] or {}
                timeout = float(rule.get("timeout_s", DEFAULT_CALL_TIMEOUT_S))
                specs.append(ToolSpec(
                    f"{name}__{tool.name}", f"[MCP:{name}] {tool.description or tool.name}", _strict(tool.inputSchema),
                    rule.get("permission", "external_mcp"), partial(self.call, name, tool.name, timeout),
                    source=f"mcp:{name}", timeout_s=timeout,
                    max_output_chars=int(rule.get("max_output_chars", DEFAULT_OUTPUT_CHARS))))
        return specs

    def _audit_failure(self, name: str, exc: Exception, began: float) -> None:
        if not self.audit:
            return
        kind = "timeout" if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) else "network"
        self.audit({"id": str(uuid.uuid4()), "tool": f"{name}__*", "source": f"mcp:{name}",
                    "permission": "external_mcp", "args_summary": {"phase": "connect", "error": type(exc).__name__},
                    "status": "timeout" if kind == "timeout" else "error", "error_kind": kind, "attempts": 1,
                    "confirmed": None, "latency_ms": int((time.perf_counter() - began) * 1000),
                    "result_count": None, "output_chars": 0, "truncated": False})

    async def _call(self, server: str, tool: str, args: dict[str, Any], timeout: float) -> Any:
        # wait_for cancels the request when time runs out instead of leaving it running.
        return await asyncio.wait_for(self.sessions[server].call_tool(tool, args), timeout)

    def call(self, server: str, tool: str, timeout: float, args: dict[str, Any]) -> list[Material]:
        if server not in self.sessions:
            raise ToolError("not_found", f"MCP 服务未连接：{server}")
        try:
            result = self._submit(self._call(server, tool, args, timeout)).result()
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ToolError("timeout", f"MCP 工具 {server}.{tool} 超过 {timeout:g} 秒") from exc
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport failures become a classified error
            raise ToolError("network", f"MCP 工具 {server}.{tool} 调用失败：{type(exc).__name__}") from exc
        text = "\n".join(block.text if getattr(block, "type", "") == "text" else "[非文本内容]"
                         for block in result.content)
        if result.isError:
            raise ToolError("upstream", f"MCP 工具 {server}.{tool} 返回错误：{text[:500]}")
        # External tool output is a supplement for the program to judge, like a web result.
        return [Material("web", f"mcp:{server}/{tool}", hashlib.sha256(text.encode()).hexdigest(), text,
                         f"MCP {server}.{tool}")]

    def close(self) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._stop.set)
        if self._owners:
            async def drain() -> None:
                await asyncio.wait(self._owners, timeout=10)
            try:
                self._submit(drain()).result(15)
            except Exception:  # noqa: BLE001 - shutting down regardless
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()
        if self._errlog is not None:
            self._errlog.close()
