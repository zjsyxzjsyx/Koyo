import http.client
import json
import threading
from datetime import date

import pytest

from zhiqiu import tracing
from zhiqiu.clock import FixedClock
from zhiqiu.dashboard import DashboardServer
from zhiqiu.flows import run_lesson
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store

from test_flows import ScriptedIO
from test_tracing import AuditingModel

SECRET = "sk-dashboard-test-0123456789abcdef"


@pytest.fixture()
def served(tmp_path):
    ref = []
    path = tmp_path / "a.md"
    path.write_text("二叉树遍历的概念、机制和应用。", encoding="utf-8")
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    ref.append(store)
    result = run_lesson(ScriptedIO("/done", "根左右", "递归栈", "表达式树"), store,
                        LearningRuntime(store, AuditingModel(ref)), "二叉树遍历", source=path)
    server = DashboardServer(tmp_path / "state.sqlite", port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield server, store, result
    server.shutdown()
    server.server_close()
    store.close()


def request(server, path, method="GET", host=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    connection.request(method, path, headers={"Host": host or f"127.0.0.1:{server.port}"})
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    connection.close()
    return response.status, response.getheader("Content-Type"), body


def get_json(server, path):
    status, kind, body = request(server, path)
    assert status == 200 and kind.startswith("application/json"), body
    return json.loads(body)


def test_the_page_and_every_endpoint_answer(served):
    server, store, result = served
    status, kind, body = request(server, "/")
    assert status == 200 and kind.startswith("text/html") and "知秋看板" in body and "cdn" not in body.lower()
    traces = get_json(server, "/api/traces?days=1")["traces"]
    assert traces[0]["name"] == "learn" and traces[0]["llm_calls"] > 0
    tree = get_json(server, f"/api/trace/{traces[0]['trace_id']}")["trees"][0]
    root = tree["roots"][0]
    assert root["kind"] == "flow" and root["children"] and root["totals"]["llm_calls"] == traces[0]["llm_calls"]
    by_attempt = get_json(server, f"/api/trace/{result.attempt_id}")
    assert by_attempt["trees"][0]["trace_id"] == traces[0]["trace_id"]
    metrics = get_json(server, "/api/metrics?days=1")
    assert metrics["model"]["calls"] == traces[0]["llm_calls"] and "grade" in metrics["model"]["by_stage"]
    assert get_json(server, "/api/check?days=1")["passed"] is True
    assert "categories" in get_json(server, "/api/failures?days=1")
    attempts = get_json(server, "/api/attempts")["attempts"]
    assert attempts[0]["id"] == result.attempt_id and attempts[0]["total"] == result.outcome["total"]
    detail = get_json(server, f"/api/attempt/{result.attempt_id}")
    assert len(detail["questions"]) == 3 and len(detail["dimension_scores"]) == 3
    assert detail["trace_ids"] == [traces[0]["trace_id"]]
    assert {event["payload"]["entity"] for event in detail["state_events"]} >= {"attempt", "review_task"}
    live = get_json(server, "/api/live")
    assert live["latest"]["trace_id"] == traces[0]["trace_id"] and live["db"] == "state.sqlite"


def test_running_spans_appear_live_with_their_progress_and_leave_when_done(served):
    server, store, _ = served
    assert get_json(server, "/api/live")["running"] == []
    with tracing.span(store, "flow", "quiz", point="二叉树遍历"):
        tracing.progress(step="等待回答", question=3)
        with tracing.span(store, "stage", "grade"):
            running = get_json(server, "/api/live")["running"]
            assert len(running) == 1
            chain = running[0]["chain"]
            assert [span["name"] for span in chain] == ["quiz", "grade"]
            assert chain[0]["progress"] == {"step": "等待回答", "question": 3}
            tree = get_json(server, f"/api/trace/{running[0]['trace_id']}")["trees"][0]
            assert tree["roots"][0]["status"] == "running"
    assert get_json(server, "/api/live")["running"] == []


def test_a_learning_run_shows_its_phases_while_it_runs(served):
    server, store, _ = served
    with tracing.span(store, "flow", "learn", point="红黑树"):
        with tracing.span(store, "stage", "assess"):
            pass
        with tracing.span(store, "stage", "teach"):
            pass
        with tracing.span(store, "flow", "quiz"):
            tracing.progress(step="等待回答", question=2)
            phases = get_json(server, "/api/live")["running"][0]["phases"]
    assert [(p["label"], p["status"]) for p in phases] == [("准备资料", "done"), ("讲解", "done"), ("自测", "running"),
                                                          ("评分", "pending")]
    assert phases[2]["progress"] == {"step": "等待回答", "question": 2}


def test_one_model_call_shows_its_build_failure_and_optional_text(served, monkeypatch):
    server, store, _ = served
    call = store.db.execute("SELECT id FROM llm_calls WHERE stage='grade'").fetchone()[0]
    detail = get_json(server, f"/api/llm/{call}")
    assert detail["stage"] == "grade" and "content" not in detail
    assert request(server, "/api/llm/unknown")[0] == 404
    monkeypatch.setenv("ZHIQIU_TRACE_CONTENT", "1")
    monkeypatch.setenv("ZHIQIU_LLM_API_KEY", SECRET)
    store.record_llm_call({"point": "p", "model": "m", "prompt_hash": "h", "latency_ms": 1, "status": "ok",
                           "request_messages": [{"role": "user", "content": f"问题 {SECRET}"}],
                           "response_text": "回答"})
    stored = store.db.execute("SELECT id FROM llm_calls ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    detail = get_json(server, f"/api/llm/{stored}")
    assert detail["content"]["response"] == "回答"
    assert detail["content"]["request"][0]["content"] == "问题 [redacted]"
    monkeypatch.delenv("ZHIQIU_TRACE_CONTENT")
    store.record_llm_call({"point": "p", "model": "m", "prompt_hash": "h", "latency_ms": 1, "status": "ok",
                           "request_messages": [{"role": "user", "content": "不保存"}], "response_text": "x"})
    assert store.db.execute("SELECT COUNT(*) FROM llm_contents").fetchone()[0] == 1


def test_summary_counts_feed_the_navigation(served):
    server, _, _ = served
    data = get_json(server, "/api/summary?days=1")
    assert data["traces"] == 1 and data["attempts"] == 1 and data["running"] == 0 and "failures" in data


def test_it_reads_while_the_terminal_holds_a_write_transaction(served):
    server, store, _ = served
    store.db.execute("BEGIN IMMEDIATE")
    try:
        store.db.execute("UPDATE spans SET name=name")
        assert get_json(server, "/api/traces?days=1")["traces"]
    finally:
        store.db.rollback()


def test_it_is_read_only_and_local_only(served):
    server, _, _ = served
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert request(server, "/api/traces", method)[0] == 405
    assert request(server, "/api/traces", host="evil.example:80")[0] == 403
    assert request(server, "/api/traces", host=f"localhost:{server.port}")[0] == 200
    assert request(server, "/api/nope")[0] == 404
    assert request(server, "/api/trace/unknown-id")[0] == 404
    assert server.server_address[0] == "127.0.0.1"


def test_responses_are_redacted(served, monkeypatch):
    server, store, _ = served
    monkeypatch.setenv("ZHIQIU_LLM_API_KEY", SECRET)
    with tracing.span(store, "turn", "chat", point=f"密钥 {SECRET}"):
        pass
    body = request(server, "/api/traces?days=1")[2]
    assert SECRET not in body and "[redacted]" in body
