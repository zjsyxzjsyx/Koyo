"""zhiqiu as a read-only MCP server over stdio.

Only permission="read" tools are exposed, the database is opened read-only per call,
and every call is audited to a JSONL file next to the database instead of the database.
"""
from __future__ import annotations

import json
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .clock import Clock
from .queries import open_readonly, query_tool_specs, readonly_runner
from .tools import ToolError, ToolExecutor, ToolRegistry, ToolScope

READ_ONLY = ToolScope(frozenset({"read"}))
INSTALL_HINT = "MCP 服务需要可选依赖：uv sync --extra mcp"


class JsonlAudit:
    def __init__(self, path: Path):
        self.path = Path(path)

    def __call__(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}, ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def read_only_registry(db_path: Path, clock: Clock | None = None) -> ToolRegistry:
    clock = clock or Clock()
    registry = ToolRegistry()
    for spec in query_tool_specs(readonly_runner(db_path), lambda: clock.today().isoformat()):
        registry.register(spec)
    return registry


def build_server(registry: ToolRegistry, executor: ToolExecutor) -> Any:
    """The MCP server object; refuses to start when any registered tool is not read-only."""
    writable = [spec.name for spec in registry.specs() if spec.permission != "read"]
    if writable:
        raise ValueError(f"MCP 服务只开放只读工具，拒绝启动：{', '.join(writable)}")
    try:
        import anyio
        from mcp import types
        from mcp.server.lowlevel import Server
    except ImportError as exc:
        raise RuntimeError(INSTALL_HINT) from exc

    server = Server("zhiqiu")
    annotations = types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                                        openWorldHint=False)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(name=spec.name, description=spec.description, inputSchema=spec.params,
                           annotations=annotations) for spec in registry.specs()]

    # The executor validates arguments itself, so the schema has a single enforcement point.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> Any:
        try:
            result = await anyio.to_thread.run_sync(executor.run, name, arguments or {}, READ_ONLY)
        except ToolError as exc:
            return types.CallToolResult(content=[types.TextContent(
                type="text", text=json.dumps({"error": exc.kind, "message": str(exc)}, ensure_ascii=False))],
                isError=True)
        payload = {"result": result.value, "truncated": result.truncated}
        return [types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))]

    return server


def serve(db_path: Path) -> None:
    """Run over stdio until the client disconnects. Nothing else may write to stdout."""
    try:
        import anyio
        from mcp.server.stdio import stdio_server
    except ImportError as exc:
        raise RuntimeError(INSTALL_HINT) from exc
    with closing(open_readonly(db_path)):
        pass  # fail at startup, not on the first call, when the database is missing or outdated
    registry = read_only_registry(db_path)
    server = build_server(registry, ToolExecutor(registry, JsonlAudit(Path(db_path).parent / "mcp-audit.jsonl")))

    async def main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(main)
