import json
from datetime import date

import pytest

from zhiqiu import guardrails, tracing
from zhiqiu.clock import FixedClock
from zhiqiu.files import read_root_file_spec, write_file_spec
from zhiqiu.materials import Material
from zhiqiu.mcp_server import JsonlAudit
from zhiqiu.redact import redact
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store
from zhiqiu.toolloop import query_leak_check
from zhiqiu.tools import ToolError, ToolExecutor, ToolRegistry, ToolScope, ToolSpec

from test_agent import make_agent, say, tool
from test_runtime import FakeModel, prepare_service

SECRET = "sk-zhiqiu-test-0123456789abcdefghij"
OBJECT = {"type": "object", "additionalProperties": False, "properties": {"query": {"type": "string"}}}


def guards(store):
    return [json.loads(row[0]) for row in store.db.execute(
        "SELECT payload_json FROM events WHERE type='GuardrailTriggered' ORDER BY id")]


def executor(store, *specs):
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return ToolExecutor(registry, audit=store.record_tool_call)


def echo(name="echo", permission="read", confirm=False):
    return ToolSpec(name, "echo", OBJECT, permission, lambda args: args, source="test", requires_confirmation=confirm)


@pytest.mark.parametrize("spec, scope, rule, action", [
    (echo(), ToolScope(frozenset({"read"}), names=frozenset({"other"})), "name_not_allowed", "blocked"),
    (echo(permission="network"), ToolScope(frozenset({"read"})), "permission_denied", "blocked"),
    (echo(confirm=True), ToolScope(frozenset({"read"})), "confirmation_unavailable", "blocked"),
    (echo(confirm=True), ToolScope(frozenset({"read"}), confirm=lambda spec, args: False),
     "confirmation_denied", "denied_by_user"),
])
def test_the_tool_gate_records_which_rule_refused_the_call(tmp_path, spec, scope, rule, action):
    store = Store(tmp_path / "state.sqlite")
    with tracing.span(store, "turn", "chat") as span:
        with pytest.raises(ToolError):
            executor(store, spec).run(spec.name, {"query": "q"}, scope)
    assert guards(store) == [{"rule": rule, "action": action, "reason": guards(store)[0]["reason"], "tool": spec.name}]
    row = store.db.execute("SELECT trace_id,parent_span_id FROM events WHERE type='GuardrailTriggered'").fetchone()
    assert tuple(row) == (span.trace_id, span.span_id)


def test_leak_check_and_path_roots_are_recorded(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    root = tmp_path / "notes"
    root.mkdir()
    private = Material("local", "a.md", "h", "红黑树插入后先把新节点染成红色再旋转恢复性质")
    leaky = echo("web_search", "network")
    run = executor(store, leaky, read_root_file_spec([root]), write_file_spec([root]))
    scope = ToolScope(frozenset({"read", "network", "write_file"}), confirm=lambda *_: True,
                      check=query_leak_check([private]))
    for name, args in (("web_search", {"query": "红黑树插入后先把新节点染成红色再旋转"}),
                       ("read_file", {"path": str(tmp_path / "outside.md")}),
                       ("write_file", {"path": str(tmp_path / "outside.md"), "content": "x"})):
        with pytest.raises(ToolError):
            run.run(name, args, scope)
    assert [(item["rule"], item["tool"]) for item in guards(store)] == [
        ("leak_check", "web_search"), ("read_root", "read_file"), ("write_root", "write_file")]


def test_chat_records_rejected_and_refused_proposals_memory_limits_and_loop_limits(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [tool("propose_review", {"reason": "复习"})])
    agent.handle("复习一下")  # nothing is due: refused before asking
    assert [item["rule"] for item in guards(store)] == ["proposal_refused"]

    store, model, io, agent = make_agent(tmp_path / "b", [tool("propose_memory", {"key": "style.short", "content": "x"}),
                                                          say("好的")])
    agent.handle("记住这个")
    assert [item["rule"] for item in guards(store)] == ["memory_limit"]

    note = str(tmp_path / "c" / "notes" / "rbtree.md")
    store, model, io, agent = make_agent(tmp_path / "c", [tool("propose_learn", {"point": "红黑树", "path": note})],
                                         confirm=False)
    agent.handle("我想学红黑树")
    assert [(item["rule"], item["action"]) for item in guards(store)] == [("proposal_rejected", "denied_by_user")]

    store, model, io, agent = make_agent(tmp_path / "d", [tool("zhiqiu_status")] * 6)
    agent.handle("一直查")
    limit = guards(store)
    assert [(item["rule"], item["action"], item["end_reason"]) for item in limit] == [("loop_limit", "stopped",
                                                                                         "max_rounds")]


class BadQuestionModel(FakeModel):
    def next_question(self, point, context, sources, focus):
        return {"done": False, "dimension": "concept", "question": "解释概念", "difficulty": "hard",
                "difficulty_basis": "?"}


def test_an_invalid_question_is_kept_as_a_failure_with_a_guardrail_event(tmp_path):
    store, service, path = prepare_service(tmp_path, BadQuestionModel())
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    attempt = store.start_attempt(kp, "first")
    with pytest.raises(ValueError, match="无效题目难度"):
        service.next_question(attempt)
    # One retry with the validation error, then the question is refused: two rejected outputs.
    assert [(item["rule"], item["action"], item["stage"]) for item in guards(store)] == [
        ("output_invalid", "rejected", "next_question")] * 2
    failure = store.db.execute("SELECT stage,attempt_id,error,raw_output FROM llm_failures").fetchone()
    assert failure["stage"] == "next_question" and failure["attempt_id"] == attempt
    assert "无效题目难度" in failure["error"] and json.loads(failure["raw_output"])["difficulty"] == "hard"


def test_refused_sending_and_missing_consent_are_recorded(tmp_path):
    store, service, path = prepare_service(tmp_path)
    with pytest.raises(ValueError):
        service.prepare("二叉树遍历", path, lambda *_: False)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    other = LearningRuntime(store, type("Elsewhere", (FakeModel,), {"endpoint": "https://other.example/v1"})())
    with pytest.raises(ValueError, match="未获发送授权"):
        other.sources_for(kp)
    assert [(item["rule"], item["action"]) for item in guards(store)] == [
        ("confirmation_denied", "denied_by_user"), ("consent_missing", "blocked")]


def test_unknown_rules_and_actions_are_refused():
    with pytest.raises(ValueError):
        guardrails.payload("made_up", "blocked")
    with pytest.raises(ValueError):
        guardrails.payload("leak_check", "ignored")


def test_secrets_are_redacted_by_value_everywhere_they_could_be_written(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIQIU_LLM_API_KEY", SECRET)
    assert redact(f"key={SECRET}") == "key=[redacted]"
    assert redact("Authorization: Bearer abcdefghijklmnop123") == "Authorization: [redacted]"
    assert redact("ntn_" + "a" * 30) == "[redacted]"
    store, model, io, agent = make_agent(tmp_path, [tool("zhiqiu_status"), say(f"你的密钥是 {SECRET}")])
    agent.handle(f"我的密钥 {SECRET} 放在哪里？")
    agent._fail(f"失败：{SECRET}")
    store.record_tool_call({"id": "x", "tool": "web_search", "status": "ok", "attempts": 1, "latency_ms": 1,
                            "output_chars": 0, "args_summary": {"query": f"查 {SECRET}"}})
    from zhiqiu.tools import summarize_args
    assert SECRET not in json.dumps(summarize_args({"query": f"查 {SECRET}"}), ensure_ascii=False)
    dump = json.dumps(store.export_json(), ensure_ascii=False)
    assert "[redacted]" in dump and SECRET not in dump
    audit = JsonlAudit(tmp_path / "mcp-audit.jsonl")
    audit({"tool": "zhiqiu_status", "error": f"echo {SECRET}"})
    assert SECRET not in (tmp_path / "mcp-audit.jsonl").read_text(encoding="utf-8")
