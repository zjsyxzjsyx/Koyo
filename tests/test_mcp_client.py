import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from zhiqiu.mcp_client import McpClientPool, load_config  # noqa: E402
from zhiqiu.runtime import LearningRuntime  # noqa: E402
from zhiqiu.tools import ToolError, ToolExecutor, ToolRegistry, ToolScope  # noqa: E402

from test_runtime import FakeSearch, prepare_service  # noqa: E402
from test_toolloop import ToolModel, done  # noqa: E402

SERVER = str(Path(__file__).with_name("mcp_test_server.py"))
SCOPE = ToolScope(frozenset({"external_mcp", "read"}))


def config(tmp_path, servers):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": servers}, ensure_ascii=False), encoding="utf-8")
    return load_config(path)


def stdio_server(tools):
    return {"command": sys.executable, "args": [SERVER], "tools": tools}


@pytest.fixture
def pool(tmp_path):
    audits = []
    created = McpClientPool(config(tmp_path, {
        "test": stdio_server({"echo": {}, "fail": {}, "slow": {"timeout_s": 0.5}, "missing_tool": {}}),
        "broken": {"command": str(tmp_path / "no-such-server.exe"), "tools": {"x": {}}}}),
        audits.append, connect_timeout_s=20)
    specs = created.start()
    yield created, specs, audits
    created.close()


def executor(specs):
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    audits = []
    return ToolExecutor(registry, audits.append), audits


def test_only_whitelisted_tools_are_registered_and_broken_servers_are_skipped(pool):
    created, specs, audits = pool
    assert sorted(spec.name for spec in specs) == ["test__echo", "test__fail", "test__slow"]
    assert {spec.source for spec in specs} == {"mcp:test"} and {spec.permission for spec in specs} == {"external_mcp"}
    assert all(spec.params["additionalProperties"] is False for spec in specs)
    assert set(created.sessions) == {"test"}
    assert [(a["tool"], a["status"], a["error_kind"]) for a in audits] == [("broken__*", "error", "network")]


def test_calls_return_material_and_errors_are_classified(pool):
    created, specs, _ = pool
    run, audits = executor(specs)
    [material] = run.run("test__echo", {"text": "中序"}, SCOPE).value
    assert (material.kind, material.source, material.content) == ("web", "mcp:test/echo", "回声：中序")
    with pytest.raises(ToolError) as error:
        run.run("test__fail", {"text": "x"}, SCOPE)
    assert error.value.kind == "upstream" and "故意失败" in str(error.value)
    with pytest.raises(ToolError) as error:
        run.run("test__echo", {"text": "x", "extra": 1}, SCOPE)
    assert error.value.kind == "invalid_args"
    assert [a["status"] for a in audits] == ["ok", "error", "invalid_args"]


def test_timeout_cancels_the_call_and_the_session_stays_usable(pool):
    created, specs, _ = pool
    run, audits = executor(specs)

    async def pending():
        return {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    before = created._submit(pending()).result()
    began = time.perf_counter()
    with pytest.raises(ToolError) as error:
        run.run("test__slow", {"seconds": 5}, SCOPE)
    assert error.value.kind == "timeout" and time.perf_counter() - began < 3
    # The timed-out request was cancelled, not left running in the background.
    assert created._submit(pending()).result() == before
    assert run.run("test__echo", {"text": "仍可用"}, SCOPE).value[0].content == "回声：仍可用"


def test_name_clash_is_refused_with_both_sources(pool, tmp_path):
    _, specs, _ = pool
    store, _, _ = prepare_service(tmp_path)
    clash = specs[0].__class__(**{**specs[0].__dict__, "name": "read_file"})
    with pytest.raises(ValueError, match="zhiqiu.files.*mcp:test"):
        LearningRuntime(store, ToolModel([]), extra_tools=[clash])


@pytest.mark.parametrize("servers, message", [
    ({"a": {"command": "x", "tools": {}}}, "白名单"),
    ({"a": {"command": "x", "tools": {"t": {"permission": "write_file"}}}}, "权限"),
    ({"a__b": {"command": "x", "tools": {"t": {}}}}, "服务名"),
    ({"a": {"tools": {"t": {}}}}, "command"),
])
def test_config_is_validated(tmp_path, servers, message):
    with pytest.raises(ValueError, match=message):
        config(tmp_path, servers)


def test_material_preparation_can_use_an_mcp_tool(pool, tmp_path):
    _, specs, _ = pool
    call = {"id": "m1", "type": "function",
            "function": {"name": "test__echo", "arguments": json.dumps({"text": "旋转规则"}, ensure_ascii=False)}}
    model = ToolModel([{"calls": [call]}, done()])
    store, _, path = prepare_service(tmp_path)
    service = LearningRuntime(store, model, extra_tools=specs)
    remote = []
    kp, _, sources = service.prepare("二叉树遍历", path, lambda m, e, r: remote.extend(r) or True)
    assert remote == ["本地资料不足时，可调用外部 MCP 服务 test 的工具：echo、fail、slow"]
    assert [(s.kind, s.source, s.content) for s in sources if s.kind == "web"] == [("web", "mcp:test/echo", "回声：旋转规则")]
    assert "test__echo" in {tool["function"]["name"] for tool in model.seen[0][2]}
    assert tuple(store.db.execute("SELECT tool,source,status FROM tool_calls").fetchone()) == ("test__echo", "mcp:test", "ok")


def test_mcp_tools_are_not_offered_while_clarifying(pool, tmp_path):
    _, specs, _ = pool
    model = ToolModel([], enough=True)
    store, _, path = prepare_service(tmp_path)
    service = LearningRuntime(store, model, FakeSearch([]), extra_tools=specs)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    model.rounds = [{"role": "assistant", "content": json.dumps({"explanation": "答", "followup": ""})}]
    service.clarify(kp, "顺序？")
    assert not any(tool["function"]["name"].startswith("test__") for tool in model.seen[-1][2])
