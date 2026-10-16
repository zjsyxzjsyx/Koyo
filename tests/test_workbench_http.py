"""The workbench's HTTP side: chat endpoints, and the checks every write request must pass."""
import http.client
import json
import re
import sqlite3
import threading
import time
from datetime import date

import pytest
from typer.testing import CliRunner

from zhiqiu import dashboard
from zhiqiu.agent import ChatAgent
from zhiqiu.cli import app
from zhiqiu.clock import FixedClock
from zhiqiu.dashboard import MAX_BODY, TOKEN_HEADER, DashboardServer
from zhiqiu.runtime import LearningRuntime
from zhiqiu.webio import Workbench

from test_agent import NOTE, ChatModel, say, tool
from test_toolloop import QuerySearch


@pytest.fixture()
def served(tmp_path):
    """A workbench behind a server on a free port; yields (server, bench, model)."""
    root = tmp_path / "notes"
    root.mkdir()
    (root / "rbtree.md").write_text(NOTE, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    model = ChatModel([])
    search = QuerySearch([])

    def make_agent(store, io):
        return ChatAgent(store, LearningRuntime(store, model, search, search, write_roots=[out]), io,
                         read_roots=[root], write_roots=[out])
    bench = Workbench(tmp_path / "state.sqlite", make_agent, clock=FixedClock(date(2026, 10, 15)))
    bench.start()
    server = DashboardServer(tmp_path / "state.sqlite", port=0, workbench=bench)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield server, bench, model
    server.shutdown()
    server.server_close()
    assert bench.close()


def call(server, path, method="GET", body=None, *, token=True, host=None, origin="self",
         content_type="application/json", raw=None):
    headers = {"Host": host or f"127.0.0.1:{server.port}"}
    if token:
        headers[TOKEN_HEADER] = server.token if token is True else token
    if origin == "self":
        headers["Origin"] = f"http://127.0.0.1:{server.port}"
    elif origin:
        headers["Origin"] = origin
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    if data is not None:
        headers["Content-Type"] = content_type
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    connection.request(method, path, body=data, headers=headers)
    response = connection.getresponse()
    text = response.read().decode("utf-8")
    connection.close()
    return response.status, json.loads(text) if text.startswith("{") else text


def wait_event(server, kinds, after=0, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, data = call(server, f"/api/chat/events?after={after}")
        assert status == 200
        for event in data["events"]:
            if event["kind"] in kinds:
                return event, data
        time.sleep(0.02)
    raise AssertionError(f"no {kinds} after {after}")


def refusals(server):
    db = sqlite3.connect(server.db_path)
    try:
        return [json.loads(row[0])["reason"] for row in db.execute(
            "SELECT payload_json FROM events WHERE type='GuardrailTriggered' "
            "AND json_extract(payload_json,'$.rule')='web_request_refused' ORDER BY id")]
    finally:
        db.close()


def test_the_page_carries_the_token_and_chat_reads_need_it(served):
    server, bench, _ = served
    status, page = call(server, "/", token=False)
    assert status == 200 and 'name="zhiqiu-mode" content="workbench"' in page
    assert re.search(r'name="zhiqiu-token" content="([^"]+)"', page).group(1) == server.token
    assert call(server, "/api/chat/events", token=False)[0] == 403
    assert call(server, "/api/chat/events", token="wrong")[0] == 403
    status, data = call(server, "/api/chat/state")
    assert status == 200 and data["conversation_id"] == bench.conversation_id and data["busy"] is False
    # The observation endpoints are still there, without a token, as on the read-only dashboard.
    assert call(server, "/api/traces?days=1", token=False)[0] == 200


def test_a_chat_turn_runs_from_http_input_to_the_answer(served):
    server, bench, model = served
    model.rounds = [tool("zhiqiu_status"), say("今天没有到期复习。")]
    status, data = call(server, "/api/chat/input", "POST", {"text": "今天要复习什么？"})
    assert status == 202 and data["accepted"] is True
    event, _ = wait_event(server, ("idle",), after=data["next"])
    events = call(server, "/api/chat/events")[1]["events"]
    assert any(item["kind"] == "say" and item["text"] == "今天没有到期复习。" for item in events)
    assert call(server, "/api/chat/input", "POST", {"text": "  "})[0] == 400
    assert call(server, "/api/chat/input", "POST", {"words": "x"})[0] == 400
    assert call(server, "/api/chat/input", "POST", raw=b"[1, 2]")[0] == 400
    assert call(server, "/api/chat/nope", "POST", {})[0] == 404


def test_approvals_are_answered_over_http_one_turn_at_a_time(served, tmp_path):
    server, bench, model = served
    model.rounds = [tool("write_file", {"path": "a.md", "content": "笔记"}, "a"), say("写好了")]
    after = call(server, "/api/chat/input", "POST", {"text": "写下来"})[1]["next"]
    event, data = wait_event(server, ("approval",), after=after)
    assert data["busy"] is True and [item["request_id"] for item in data["pending"]] == [event["request_id"]]
    assert call(server, "/api/chat/input", "POST", {"text": "再来"})[0] == 409
    assert call(server, "/api/chat/respond", "POST", {"request_id": event["request_id"], "value": "yes"})[0] == 400
    assert call(server, "/api/chat/respond", "POST", {"request_id": event["request_id"], "value": True})[0] == 200
    assert call(server, "/api/chat/respond", "POST", {"request_id": event["request_id"], "value": True})[0] == 409
    wait_event(server, ("idle",), after=event["seq"])
    assert (tmp_path / "out" / "a.md").read_text(encoding="utf-8") == "笔记"


def test_stop_gives_up_on_the_waiting_prompt(served):
    server, bench, model = served
    model.rounds = [tool("propose_learn", {"point": "红黑树", "path": "rbtree.md"})]
    after = call(server, "/api/chat/input", "POST", {"text": "我想学红黑树"})[1]["next"]
    event, _ = wait_event(server, ("confirm",), after=after)
    assert call(server, "/api/chat/stop", "POST", {}) == (200, {"stopped": 1})
    resolved, _ = wait_event(server, ("resolved",), after=event["seq"])
    assert resolved["reason"] == "stopped"
    wait_event(server, ("idle",), after=event["seq"])


@pytest.mark.parametrize("kwargs, status, reason", [
    ({"token": False}, 403, "令牌"),
    ({"token": "not-the-token"}, 403, "令牌"),
    ({"origin": "https://evil.example"}, 403, "其他网站"),
    ({"content_type": "text/plain"}, 415, "application/json"),
    ({"raw": b"{" + b" " * (MAX_BODY + 1) + b"}"}, 413, "请求体超过"),
    ({"host": "evil.example:80"}, 403, "本机"),
])
def test_bad_write_requests_are_refused_and_recorded(served, kwargs, status, reason):
    server, bench, _ = served
    assert call(server, "/api/chat/input", "POST", {"text": "你好"}, **kwargs)[0] == status
    recorded = refusals(server)
    assert len(recorded) == 1 and reason in recorded[0] and "/api/chat/input" in recorded[0]
    assert not bench.busy  # nothing was submitted


def test_requests_without_an_origin_header_are_accepted(served):
    server, bench, model = served
    model.rounds = [say("好")]
    assert call(server, "/api/chat/input", "POST", {"text": "你好"}, origin=None)[0] == 202


def test_the_read_only_dashboard_has_no_token_and_no_chat(tmp_path):
    from zhiqiu.store import Store
    Store(tmp_path / "state.sqlite").close()
    server = DashboardServer(tmp_path / "state.sqlite", port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        status, page = call(server, "/", token=False)
        assert status == 200 and 'name="zhiqiu-token"' not in page and server.token is None
        assert call(server, "/api/chat/events", token=False)[0] == 404
        assert call(server, "/api/chat/input", "POST", {"text": "你好"}, token="x")[0] == 405
    finally:
        server.shutdown()
        server.server_close()


def test_stopping_the_server_closes_the_workbench(tmp_path, monkeypatch):
    bench = Workbench(tmp_path / "state.sqlite",
                      lambda store, io: ChatAgent(store, LearningRuntime(store, ChatModel([])), io))
    bench.start()

    def interrupted(self, poll_interval=0.5):
        raise KeyboardInterrupt
    monkeypatch.setattr(DashboardServer, "serve_forever", interrupted)
    began = time.monotonic()
    dashboard.serve(tmp_path / "state.sqlite", 0, workbench=bench)
    assert not bench._thread.is_alive() and time.monotonic() - began < 5


def test_web_refuses_to_start_with_a_bad_approval_policy(tmp_path, monkeypatch):
    config = tmp_path / "approval.json"
    config.write_text('{"permissions": {"write_file": "allow"}}', encoding="utf-8")
    monkeypatch.setenv("ZHIQIU_APPROVAL_CONFIG", str(config))
    result = CliRunner().invoke(app, ["--db", str(tmp_path / "state.sqlite"), "web", "--port", "0"])
    assert result.exit_code == 1 and "不能启动工作台" in result.output and "写文件必须由用户批准" in result.output


def test_history_returns_what_the_conversation_had_before_the_workbench_started(tmp_path):
    from zhiqiu.conversations import Conversations
    from zhiqiu.store import Store
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 15)))
    chats = Conversations(store)
    conversation = chats.open_today([], {})
    chats.add_message(conversation["id"], "user", "chat", "昨天的问题")
    chats.add_message(conversation["id"], "assistant", "chat", "昨天的回答")
    store.close()
    model = ChatModel([say("今天的回答")])
    bench = Workbench(tmp_path / "state.sqlite", lambda s, io: ChatAgent(s, LearningRuntime(s, model), io),
                      clock=FixedClock(date(2026, 10, 15)), resume=conversation["id"])
    bench.start()
    server = DashboardServer(tmp_path / "state.sqlite", port=0, workbench=bench)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        assert call(server, "/api/chat/history", token=False)[0] == 403
        status, data = call(server, "/api/chat/history")
        assert status == 200 and data["conversation_id"] == conversation["id"]
        assert [(m["role"], m["content"]) for m in data["messages"]] == [("user", "昨天的问题"), ("assistant", "昨天的回答")]
        # What happens in the workbench arrives as events, not as history.
        after = call(server, "/api/chat/input", "POST", {"text": "今天的问题"})[1]["next"]
        wait_event(server, ("idle",), after=after)
        assert len(call(server, "/api/chat/history")[1]["messages"]) == 2
    finally:
        server.shutdown()
        server.server_close()
        assert bench.close()


def test_a_new_conversation_has_no_history(served):
    server, bench, _ = served
    assert call(server, "/api/chat/history") == (200, {"conversation_id": bench.conversation_id, "messages": []})

def test_the_state_panel_starts_empty_without_errors(served, tmp_path):
    server, bench, _ = served
    status, data = call(server, "/api/chat/state")
    assert status == 200 and data["conversation_id"] == bench.conversation_id
    assert data["goal"] == {"mode": "chat", "goal": "", "step": "", "active_kind": None, "active_id": None}
    context = data["context"]
    assert context["read_roots"] == [str(tmp_path / "notes")] and context["write_roots"] == [str(tmp_path / "out")]
    assert context["memories"] == 0 and context["summarized"] is False and context["network"] is True
    assert data["running"] == [] and data["result"] is None
    assert data["verification"] == {"traces": 0, "passed": None, "failed": []}
    risk = data["risk"]
    assert risk["policy"]["permissions"]["network"] == "allow" and "联网：自动执行" in risk["policy_text"]
    assert risk["pending"] == [] and risk["recent"] == []


def test_the_state_panel_follows_approvals_and_learning_results(served):
    server, bench, model = served
    model.rounds = [tool("write_file", {"path": "a.md", "content": "笔记"}, "a"), say("好")]
    after = call(server, "/api/chat/input", "POST", {"text": "写下来"})[1]["next"]
    event, _ = wait_event(server, ("approval",), after=after)
    risk = call(server, "/api/chat/state")[1]["risk"]
    assert risk["pending"] == [{"kind": "approval", "tool": "write_file", "text": risk["pending"][0]["text"]}]
    assert risk["pending"][0]["text"].startswith("写入文件") and risk["recent"][0]["status"] == "pending"
    call(server, "/api/chat/respond", "POST", {"request_id": event["request_id"], "value": False})
    wait_event(server, ("idle",), after=event["seq"])
    risk = call(server, "/api/chat/state")[1]["risk"]
    assert risk["pending"] == [] and risk["recent"][0]["status"] == "denied"

    model.rounds = [tool("propose_learn", {"point": "红黑树", "path": "rbtree.md"})]
    after = call(server, "/api/chat/input", "POST", {"text": "我想学红黑树"})[1]["next"]
    modes = set()
    answers = ["/done", "染红", "旋转", "插入后修复"]
    while True:
        event, data = wait_event(server, ("ask", "confirm", "idle"), after=after)
        after = event["seq"]
        if event["kind"] == "idle":
            break
        modes.add(call(server, "/api/chat/state")[1]["goal"]["mode"])
        value = answers.pop(0) if event["kind"] == "ask" else True
        call(server, "/api/chat/respond", "POST", {"request_id": event["request_id"], "value": value})
    assert {"lesson", "self_test"} <= modes
    data = call(server, "/api/chat/state")[1]
    assert data["goal"]["mode"] == "chat"
    result = data["result"]
    assert result["total"] is not None and result["review_kind"] == "complete" and result["next_review"]
    assert "完整评阅" in result["summary"] and result["attempt_id"]
    assert data["verification"]["traces"] >= 2 and data["verification"]["passed"] is True