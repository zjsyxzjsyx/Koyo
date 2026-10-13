import json
import sys
from datetime import timedelta

import anyio
import pytest
from typer.testing import CliRunner

pytest.importorskip("mcp")
from mcp import StdioServerParameters, types  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from zhiqiu.cli import app  # noqa: E402
from zhiqiu.mcp_server import JsonlAudit, build_server, read_only_registry  # noqa: E402
from zhiqiu.tools import ToolExecutor, ToolRegistry, ToolSpec  # noqa: E402

from test_queries import fingerprint, finished_database  # noqa: E402

QUERY_TOOLS = {"zhiqiu_status", "zhiqiu_due", "zhiqiu_knowledge_point", "zhiqiu_trace"}


def served(db_path):
    registry = read_only_registry(db_path)
    audit = db_path.parent / "mcp-audit.jsonl"
    return build_server(registry, ToolExecutor(registry, JsonlAudit(audit))), audit


def payload(result):
    return json.loads(result.content[0].text)


def test_server_lists_only_read_tools_and_answers_with_json(tmp_path):
    db_path, kp, outcome = finished_database(tmp_path)
    server, audit = served(db_path)
    before = fingerprint(db_path)

    async def scenario():
        async with create_connected_server_and_client_session(server) as client:
            tools = (await client.list_tools()).tools
            status = await client.call_tool("zhiqiu_status", {})
            point = await client.call_tool("zhiqiu_knowledge_point", {"point": "二叉树遍历"})
            missing = await client.call_tool("zhiqiu_trace", {"attempt_id": "missing"})
            bad = await client.call_tool("zhiqiu_due", {"until": "明天"})
            return tools, status, point, missing, bad

    tools, status, point, missing, bad = anyio.run(scenario)
    assert {tool.name for tool in tools} == QUERY_TOOLS
    assert all(tool.annotations.readOnlyHint and not tool.annotations.destructiveHint for tool in tools)
    assert not status.isError and set(payload(status)["result"]) == {"due", "open_attempts", "needs_relearn"}
    assert payload(point)["result"]["knowledge_point"]["id"] == kp
    assert missing.isError and payload(missing)["error"] == "not_found"
    assert bad.isError and payload(bad)["error"] == "invalid_args"
    lines = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert [(line["tool"], line["status"]) for line in lines] == [
        ("zhiqiu_status", "ok"), ("zhiqiu_knowledge_point", "ok"), ("zhiqiu_trace", "error"),
        ("zhiqiu_due", "invalid_args")]
    assert fingerprint(db_path)[0] == before[0]


def test_server_refuses_to_start_with_a_writing_tool(tmp_path):
    db_path, _, _ = finished_database(tmp_path)
    registry = read_only_registry(db_path)
    registry.register(ToolSpec("write_file", "写文件", {"type": "object", "additionalProperties": False,
                                                       "properties": {}}, "write_file", str, source="tests"))
    with pytest.raises(ValueError, match="write_file"):
        build_server(registry, ToolExecutor(registry))


def test_cli_mcp_refuses_a_missing_database_without_creating_it(tmp_path, monkeypatch):
    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: None)
    missing = tmp_path / "none" / "state.sqlite"
    result = CliRunner().invoke(app, ["--db", str(missing), "mcp"])
    assert result.exit_code == 1 and "不存在" in result.output
    assert not missing.parent.exists()


def test_real_stdio_session_keeps_stdout_clean(tmp_path):
    db_path, _, _ = finished_database(tmp_path)
    params = StdioServerParameters(command=sys.executable,
                                   args=["-c", "from zhiqiu.cli import app; app()", "--db", str(db_path), "mcp"])

    async def scenario():
        with anyio.fail_after(60):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool("zhiqiu_status", {})
                    return tools, result

    tools, result = anyio.run(scenario)
    assert {tool.name for tool in tools.tools} == QUERY_TOOLS
    assert not result.isError and "due" in payload(result)["result"]
