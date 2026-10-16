"""Run contracts against hand-made records: each checker must catch its violation (Hermes: test the checker)."""
import json
import os

import pytest
from typer.testing import CliRunner

from zhiqiu import contracts, tracing
from zhiqiu.cli import app
from zhiqiu.store import Store


def status(store, name):
    data = contracts.check(store.db, 1)
    return next(item for item in data["results"] if item["check"] == name)


def loop_span(store, span_id, rounds, end_reason="done", loop_id="L1", max_rounds=5):
    store.db.execute("""INSERT INTO spans(span_id,trace_id,kind,name,started_at_ns,duration_ms,status,end_reason,attrs_json)
        VALUES(?,?,?,?,?,1,'ok',?,?)""", (span_id, "T" * 32, "loop", "chat", tracing.now_ns(), end_reason,
                                          json.dumps({"loop_id": loop_id, "rounds": rounds, "max_rounds": max_rounds,
                                                      "pid": os.getpid()})))


def tool_call(store, call_id, tool="zhiqiu_status", status_="ok", loop_id="L1", round_=1, permission="read",
              confirmed=None, conversation_id=None, trace_id="T" * 32, parent="S1", source=None):
    store.db.execute("""INSERT INTO tool_calls(id,tool,permission,status,attempts,latency_ms,output_chars,truncated,
        created_at,loop_id,round,confirmed,conversation_id,trace_id,parent_span_id,started_at_ns,approval_source)
        VALUES(?,?,?,?,1,1,0,0,'2026-10-14',?,?,?,?,?,?,?,?)""",
        (call_id, tool, permission, status_, loop_id, round_, confirmed, conversation_id, trace_id, parent,
         tracing.now_ns() + len(call_id), source))


def state_event(store, entity, entity_id, source, target):
    store.db.execute("""INSERT INTO events(type,payload_json,created_at,at_ns) VALUES('StateChanged',?,?,?)""",
                     (json.dumps({"entity": entity, "id": entity_id, "from": source, "to": target, "cause": "t"}),
                      "2026-10-14", tracing.now_ns()))


def test_an_empty_record_is_insufficient_evidence_not_a_pass(tmp_path):
    data = contracts.check(Store(tmp_path / "s.sqlite").db, 1)
    assert data["passed"] is True
    assert {item["status"] for item in data["results"]} == {"insufficient"}


def test_loop_rounds_and_tool_round_bound(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    loop_span(store, "S1", 2)
    tool_call(store, "a", round_=2)
    assert status(store, "loop_rounds")["status"] == "pass"
    assert status(store, "tool_round_bound")["status"] == "pass"
    tool_call(store, "b", round_=3)
    loop_span(store, "S2", 7, loop_id="L2")
    assert status(store, "tool_round_bound")["status"] == "fail"
    bad = status(store, "loop_rounds")
    assert bad["status"] == "fail" and bad["evidence"][0]["rounds"] == 7


def test_proposal_must_stop_the_round(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    loop_span(store, "S1", 1, end_reason="proposal")
    tool_call(store, "a", tool="propose_learn", permission="propose")
    tool_call(store, "b", tool="zhiqiu_status", status_="denied")
    assert status(store, "proposal_stops_round")["status"] == "pass"
    tool_call(store, "c", tool="zhiqiu_due")
    bad = status(store, "proposal_stops_round")
    assert bad["status"] == "fail" and bad["evidence"][0]["ran_after_proposal"] == ["zhiqiu_due"]


def test_side_effects_need_confirmation(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    tool_call(store, "a", tool="write_file", permission="write_file", confirmed=1)
    tool_call(store, "b", tool="web_search", permission="network", confirmed=None)  # preparation: consent covers it
    assert status(store, "confirmed_side_effects")["status"] == "pass"
    tool_call(store, "c", tool="web_search", permission="network", conversation_id="C1")
    assert status(store, "confirmed_side_effects")["status"] == "fail"


def test_writes_need_the_user_and_chat_network_calls_need_policy_or_user(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    tool_call(store, "a", tool="web_search", permission="network", conversation_id="C1", confirmed=1, source="policy")
    tool_call(store, "b", tool="write_file", permission="write_file", confirmed=1, source="user")
    tool_call(store, "c", tool="web_search", permission="network", conversation_id="C1", confirmed=1, source="turn")
    tool_call(store, "d", tool="write_file", permission="write_file", confirmed=1)  # written before v19
    tool_call(store, "e", tool="web_search", permission="network", conversation_id="C1", confirmed=0,
              status_="denied", source="user")  # refused calls never ran, so they are not checked
    assert status(store, "confirmed_side_effects")["status"] == "pass"
    tool_call(store, "f", tool="write_file", permission="write_file", confirmed=1, source="policy")
    bad = status(store, "confirmed_side_effects")
    assert bad["status"] == "fail" and bad["evidence"] == [
        {"trace_id": "T" * 32, "tool_call": "f", "tool": "write_file", "confirmed": 1, "approval_source": "policy"}]
    other = Store(tmp_path / "t.sqlite")
    tool_call(other, "g", tool="notion__search", permission="external_mcp", conversation_id="C1", confirmed=0,
              source="user")
    assert status(other, "confirmed_side_effects")["status"] == "fail"


def test_state_replay_catches_moves_outside_the_table_and_gaps(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    state_event(store, "attempt", "A1", None, "in_progress")
    state_event(store, "attempt", "A1", "in_progress", "completed")
    assert status(store, "state_replay")["status"] == "pass"
    state_event(store, "attempt", "A1", "completed", "in_progress")
    assert "表外转换" in status(store, "state_replay")["evidence"][0]["problem"]
    other = Store(tmp_path / "t.sqlite")
    state_event(other, "attempt", "A2", None, "in_progress")
    state_event(other, "attempt", "A2", "paused", "in_progress")
    assert "不衔接" in status(other, "state_replay")["evidence"][0]["problem"]


def test_trace_links_and_abandoned_spans(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    with tracing.span(store, "turn", "chat"):
        store.record_llm_call({"point": "", "model": "m", "prompt_hash": "h", "latency_ms": 1, "status": "ok",
                               "started_at_ns": tracing.now_ns()})
    assert status(store, "trace_linked")["status"] == "pass"
    store.record_llm_call({"point": "", "model": "m", "prompt_hash": "h", "latency_ms": 1, "status": "ok",
                           "started_at_ns": tracing.now_ns()})
    tool_call(store, "orphan", parent="missing")
    problems = {item["problem"] for item in status(store, "trace_linked")["evidence"]}
    assert problems == {"没有 trace", "父 span 不存在"}
    assert status(store, "spans_closed")["status"] == "pass"
    store.db.execute("""INSERT INTO spans(span_id,trace_id,kind,name,started_at_ns,status,attrs_json)
        VALUES('dead','D','command','learn',?,'running','{"pid": 2000000000}')""", (tracing.now_ns(),))
    assert status(store, "spans_closed")["status"] == "fail"
    tracing.reconcile(store)
    assert status(store, "spans_closed")["status"] == "pass"


def test_check_command_exits_nonzero_on_a_failure(tmp_path):
    db = tmp_path / "s.sqlite"
    store = Store(db)
    state_event(store, "attempt", "A1", "completed", "in_progress")
    store.close()
    result = CliRunner().invoke(app, ["--db", str(db), "check"])
    assert result.exit_code == 1 and "FAIL" in result.output and "state_replay" in result.output
    data = json.loads(CliRunner().invoke(app, ["--db", str(db), "check", "--json"]).output)
    assert data["passed"] is False


def test_failures_are_grouped_into_categories_with_trace_samples(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    with tracing.span(store, "turn", "chat") as turn:
        store.record_llm_call({"point": "", "model": "m", "prompt_hash": "h", "latency_ms": 1, "status": "error",
                               "error_kind": "ReadTimeout", "stage": "chat", "started_at_ns": tracing.now_ns()})
        store.record_llm_failure("next_question", "模型返回了不允许的追问；问答保持可恢复", "{}")
        store.record_event(None, None, "GuardrailTriggered", {"rule": "leak_check", "action": "blocked",
                                                              "tool": "web_search", "reason": "夹带原文"})
        with tracing.span(store, "loop", "chat") as loop:
            loop.set(end_reason="deadline")
    with pytest.raises(KeyboardInterrupt):
        with tracing.span(store, "flow", "quiz"):
            raise KeyboardInterrupt
    data = contracts.failures(store.db, 1)
    counts = {item["category"]: item["count"] for item in data["categories"]}
    assert counts == {"model_error:ReadTimeout": 1, "invalid_followup": 1, "guard:leak_check": 1,
                      "loop_deadline": 1, "flow_interrupted": 1}
    sample = next(item for item in data["categories"] if item["category"] == "guard:leak_check")["samples"][0]
    assert sample["trace_id"] == turn.trace_id and "夹带原文" in sample["detail"]
    assert "invalid_followup" in contracts.render_failures(data)


def approval(store, request_id, status, call=None, age_ns=0, tool="write_file"):
    store.db.execute("""INSERT INTO approval_requests(id,conversation_id,trace_id,tool,permission,status,tool_call_id,
        created_at_ns,created_at) VALUES(?,?,?,?,?,?,?,?,'2026-10-16')""",
        (request_id, "C1", "T" * 32, tool, "write_file", status, call, tracing.now_ns() - age_ns))


def test_approval_records_must_agree_with_the_calls_they_decided(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    approval(store, "fresh", "pending")  # may still be waiting on a screen: not checked
    assert status(store, "approvals_consistent")["status"] == "insufficient"
    tool_call(store, "a", tool="write_file", permission="write_file", confirmed=1, conversation_id="C1", source="user")
    approval(store, "r1", "approved", call="a")
    tool_call(store, "b", tool="write_file", permission="write_file", status_="denied", confirmed=0,
              conversation_id="C1", source="user")
    approval(store, "r2", "denied", call="b")
    tool_call(store, "c", tool="web_search", permission="network", status_="error", conversation_id="C1")
    approval(store, "r3", "cancelled", call="c", tool="web_search")
    tool_call(store, "d", tool="web_search", permission="network", confirmed=1, conversation_id="C1", source="policy")
    assert status(store, "approvals_consistent")["status"] == "pass"


@pytest.mark.parametrize("setup, problem", [
    (lambda s: approval(s, "old", "pending", age_ns=31 * 60 * 10**9), "仍未取消"),
    (lambda s: approval(s, "r", "approved"), "没有关联"),
    (lambda s: (tool_call(s, "x", tool="write_file", permission="write_file", status_="denied", confirmed=0,
                          conversation_id="C1", source="user"), approval(s, "r", "approved", call="x")), "批准了"),
    (lambda s: (tool_call(s, "x", tool="write_file", permission="write_file", confirmed=1,
                          conversation_id="C1", source="user"), approval(s, "r", "denied", call="x")), "拒绝了"),
    (lambda s: (tool_call(s, "x", tool="web_search", permission="network", confirmed=1, conversation_id="C1",
                          source="user"), approval(s, "r", "cancelled", call="x")), "已取消"),
    (lambda s: tool_call(s, "x", tool="write_file", permission="write_file", confirmed=1, conversation_id="C1",
                         source="user"), "没有审批记录"),
])
def test_each_approval_inconsistency_is_caught(tmp_path, setup, problem):
    store = Store(tmp_path / "s.sqlite")
    setup(store)
    result = status(store, "approvals_consistent")
    assert result["status"] == "fail" and problem in result["evidence"][0]["problem"]