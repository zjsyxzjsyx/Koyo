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
    usage = context["usage"]
    assert usage["messages"] == 0 and usage["can_compact"] is False and usage["limit"] is None
    assert sum(part["tokens"] for part in usage["parts"]) == usage["estimate"] > 0
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
    [pending] = risk["pending"]
    assert (pending["kind"], pending["tool"], pending["request_id"]) == ("approval", "write_file", event["request_id"])
    assert pending["approval_request_id"] == event["approval_request_id"] and pending["full_text"].startswith(pending["text"])
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

def test_the_page_renders_by_output_kind_not_by_text_patterns(served):
    server, bench, model = served
    status, page = call(server, "/", token=False)
    assert status == 200 and "TOOL_NOTE" not in page and "say_kind" in page
    greeting = call(server, "/api/chat/events")[1]["events"][0]
    assert greeting["say_kind"] == "note" and "/exit" not in greeting["text"]


def test_the_grading_card_carries_its_attempt_and_its_detail_reads_while_busy(served, tmp_path):
    server, bench, model = served
    note = tmp_path / "notes" / "rbtree.md"
    after = call(server, "/api/chat/input", "POST", {"text": f"/learn 红黑树 --from {note}"})[1]["next"]
    answers = iter(["/done", "根左右", "递归栈", "表达式树"])
    attempt_seen = False
    while True:
        event, data = wait_event(server, ("ask", "confirm", "idle"), after=after)
        after = event["seq"]
        if event["kind"] == "idle":
            break
        if event["kind"] == "ask" and event["prompt"].startswith("回答（"):
            # Mid-quiz the worker is busy; the read-only attempt endpoint still answers.
            attempt = sqlite3.connect(server.db_path).execute("SELECT id FROM attempts").fetchone()[0]
            assert data["busy"] is True and call(server, f"/api/attempt/{attempt}")[0] == 200
            attempt_seen = True
        value = next(answers) if event["kind"] == "ask" else True
        call(server, "/api/chat/respond", "POST", {"request_id": event["request_id"], "value": value})
    assert attempt_seen
    events = call(server, "/api/chat/events")[1]["events"]
    card = next(item for item in events if item["kind"] == "say" and item.get("say_kind") == "outcome")
    assert card["meta"]["attempt_id"] == attempt and card["text"].startswith("完整评阅 78 分")
    status, detail = call(server, f"/api/attempt/{attempt}")
    assert status == 200 and len(detail["dimension_scores"]) == 3



def seeded(tmp_path, *conversations):
    """Conversations written before the workbench starts: [(title or "", [messages...]), ...]."""
    from zhiqiu.conversations import Conversations
    from zhiqiu.store import Store
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 14)))
    chats = Conversations(store)
    ids = []
    for title, messages in conversations:
        conversation = chats.start_new([], {})
        for index, text in enumerate(messages):
            chats.add_message(conversation["id"], "user" if index % 2 == 0 else "assistant", "chat", text)
        if title:
            chats.set_title(conversation["id"], title, "model")
        ids.append(conversation["id"])
    store.close()
    return ids


@pytest.fixture()
def switching(tmp_path):
    """Two older conversations and an empty one, then a workbench on today's (new) conversation."""
    ids = seeded(tmp_path, ("明天杭州天气", ["明天杭州天气怎么样", "多云"]),
                 ("", ["二叉树的中序遍历为什么能输出有序序列", "因为左小右大", "那后序呢", "左右根"]),
                 ("", []))
    model = ChatModel([])
    bench = Workbench(tmp_path / "state.sqlite", lambda s, io: ChatAgent(s, LearningRuntime(s, model), io),
                      clock=FixedClock(date(2026, 10, 15)))
    bench.start()
    server = DashboardServer(tmp_path / "state.sqlite", port=0, workbench=bench)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield server, bench, model, ids
    server.shutdown()
    server.server_close()
    assert bench.close()


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def test_the_sidebar_lists_conversations_with_messages_and_the_current_one(switching):
    server, bench, model, (weather, tree, empty) = switching
    status, data = call(server, "/api/chat/conversations")
    assert status == 200 and data["current"] == bench.conversation_id
    rows = {row["id"]: row for row in data["conversations"]}
    assert set(rows) == {weather, tree, bench.conversation_id} and empty not in rows
    assert rows[weather]["title"] == "明天杭州天气" and rows[weather]["messages"] == 2
    assert rows[tree]["title"] == "二叉树的中序遍历为什么能输出有序序列" and rows[tree]["title_source"] == ""
    assert rows[tree]["short_id"] == tree[:8] and rows[tree]["chat_date"] == "2026-10-14"
    assert rows[bench.conversation_id]["current"] and rows[bench.conversation_id]["title"] == "（空对话）"
    assert all(row["resumable"] is False for row in rows.values())
    assert call(server, "/api/chat/conversations", token=False)[0] == 403


def test_switching_shows_that_conversation_and_writes_the_next_input_there(switching):
    server, bench, model, (weather, tree, empty) = switching
    status, data = call(server, "/api/chat/switch", "POST", {"conversation_id": tree})
    assert status == 202
    event, _ = wait_event(server, ("switched",), after=data["next"])
    assert event["conversation_id"] == tree and event["history_upto"] == 4
    wait_event(server, ("idle",), after=event["seq"])
    assert bench.conversation_id == tree
    history = call(server, "/api/chat/history")[1]
    assert history["conversation_id"] == tree and len(history["messages"]) == 4
    other = call(server, f"/api/chat/history?conversation_id={weather}")[1]
    assert [m["content"] for m in other["messages"]] == ["明天杭州天气怎么样", "多云"]
    assert call(server, "/api/chat/history?conversation_id=nope")[0] == 404
    model.rounds = [say("中序是左根右。")]
    after = call(server, "/api/chat/input", "POST", {"text": "再讲讲中序"})[1]["next"]
    wait_event(server, ("idle",), after=after)
    db = sqlite3.connect(server.db_path)
    try:
        messages = db.execute("SELECT content FROM conversation_messages WHERE conversation_id=? ORDER BY seq",
                              (tree,)).fetchall()
    finally:
        db.close()
    assert [row[0] for row in messages][-2:] == ["再讲讲中序", "中序是左根右。"]
    # After a reload the page sees the switch and what followed, nothing from the conversation before.
    events = call(server, "/api/chat/events")[1]["events"]
    assert events[0]["kind"] == "switched" and events[0]["conversation_id"] == tree
    assert any(item.get("text") == "已切换到「二叉树的中序遍历为什么能输出有序序列」（4 条消息）。" for item in events)
    assert not any(UUID.search(item.get("text") or "") for item in events)


def test_new_from_the_button_and_from_a_typed_command_both_switch(switching):
    server, bench, model, ids = switching
    first = bench.conversation_id
    data = call(server, "/api/chat/new", "POST", {})[1]
    event, _ = wait_event(server, ("switched",), after=data["next"])
    assert event["conversation_id"] != first and event["history_upto"] == 0
    wait_event(server, ("idle",), after=event["seq"])
    second = bench.conversation_id
    data = call(server, "/api/chat/input", "POST", {"text": "/new"})[1]
    event, _ = wait_event(server, ("switched",), after=data["next"])
    assert event["conversation_id"] not in (first, second)
    note, _ = wait_event(server, ("say",), after=event["seq"])
    assert note["text"] == "已开始新对话。" and note["say_kind"] == "note"


def test_renaming_from_the_sidebar_keeps_the_users_title(switching):
    server, bench, model, (weather, tree, empty) = switching
    data = call(server, "/api/chat/rename", "POST", {"conversation_id": tree, "title": "  「二叉树遍历」 "})[1]
    wait_event(server, ("idle",), after=data["next"])
    rows = {row["id"]: row for row in call(server, "/api/chat/conversations")[1]["conversations"]}
    assert (rows[tree]["title"], rows[tree]["title_source"]) == ("二叉树遍历", "user")
    assert bench.conversation_id != tree  # renaming does not switch
    assert call(server, "/api/chat/rename", "POST", {"conversation_id": tree, "title": "。。"})[0] == 400
    assert call(server, "/api/chat/rename", "POST", {"conversation_id": tree, "title": "长" * 31})[0] == 400
    assert call(server, "/api/chat/rename", "POST", {"conversation_id": tree})[0] == 400
    assert call(server, "/api/chat/rename", "POST", {"conversation_id": "nope", "title": "x"})[0] == 404
    assert call(server, "/api/chat/switch", "POST", {"conversation_id": "nope"})[0] == 404
    assert call(server, "/api/chat/switch", "POST", {})[0] == 400


def test_switching_is_refused_while_a_prompt_waits(served):
    server, bench, model = served
    model.rounds = [tool("write_file", {"path": "a.md", "content": "笔记"}, "a"), say("好")]
    after = call(server, "/api/chat/input", "POST", {"text": "写下来"})[1]["next"]
    event, data = wait_event(server, ("approval",), after=after)
    other = bench.conversation_id
    for path, body in (("/api/chat/switch", {"conversation_id": other}), ("/api/chat/new", {}),
                       ("/api/chat/rename", {"conversation_id": other, "title": "改名"}), ("/api/chat/compact", {})):
        assert call(server, path, "POST", body)[0] == 409
    assert "等这一轮结束再压缩" in call(server, "/api/chat/compact", "POST", {})[1]["error"]
    call(server, "/api/chat/respond", "POST", {"request_id": event["request_id"], "value": False})
    wait_event(server, ("idle",), after=event["seq"])
    assert call(server, "/api/chat/new", "POST", {})[0] == 202


@pytest.mark.parametrize("path, body", [("/api/chat/switch", {"conversation_id": "x"}), ("/api/chat/new", {}),
                                        ("/api/chat/rename", {"conversation_id": "x", "title": "x"}),
                                        ("/api/chat/compact", {}), ("/api/chat/model", {"model": "m2"}),
                                        ("/api/chat/model/default", {"model": "m2"}), ("/api/prices/update", {})])
@pytest.mark.parametrize("kwargs, status", [
    ({"token": False}, 403), ({"origin": "https://evil.example"}, 403), ({"host": "evil.example:80"}, 403),
    ({"content_type": "text/plain"}, 415), ({"raw": b"{" + b" " * (MAX_BODY + 1) + b"}"}, 413), ({"raw": b"[1]"}, 400)])
def test_conversation_writes_pass_the_same_checks_as_input(served, path, body, kwargs, status):
    server, bench, _ = served
    first = bench.conversation_id
    assert call(server, path, "POST", body, **kwargs)[0] == status
    assert not bench.busy and bench.conversation_id == first


def test_compact_from_the_panel_folds_older_messages_and_the_usage_follows(served):
    server, bench, model = served
    model.summarize_chat = lambda previous, items: {"summary": f"覆盖{len(items)}条"}
    status, data = call(server, "/api/chat/compact", "POST", {})
    assert status == 202  # too few messages: a note, no model call
    wait_event(server, ("idle",), after=data["next"])
    for index in range(6):
        data = call(server, "/api/chat/input", "POST", {"text": f"第{index}句"})[1]
        wait_event(server, ("idle",), after=data["next"])
    assert call(server, "/api/chat/state")[1]["context"]["usage"]["can_compact"] is True
    status, data = call(server, "/api/chat/compact", "POST", {})
    assert status == 202
    note, _ = wait_event(server, ("say",), after=data["next"])
    assert note["say_kind"] == "note" and note["text"].startswith("已压缩：2 条较早的消息并入摘要")
    wait_event(server, ("idle",), after=note["seq"])
    context = call(server, "/api/chat/state")[1]["context"]
    assert context["summarized"] is True and context["usage"]["summarized_upto"] == 2
    assert context["usage"]["messages"] == 10 and context["usage"]["can_compact"] is False


def rows(server, sql, *args):
    db = sqlite3.connect(server.db_path)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()


def test_the_model_button_switches_from_the_next_turn(served, monkeypatch):
    server, bench, model = served
    model.model = "m1"
    model.use = lambda name: setattr(model, "model", name)
    assert call(server, "/api/chat/model", "POST", {"model": "two words"})[0] == 400
    status, data = call(server, "/api/chat/model", "POST", {"model": "m2"})
    assert status == 202
    note, _ = wait_event(server, ("say",), after=data["next"])
    assert note["say_kind"] == "note" and note["text"].startswith("已切换到 m2，从下一轮起生效")
    wait_event(server, ("idle",), after=note["seq"])
    models = call(server, "/api/chat/models")[1]
    assert models["current"] == "m2" and models["choices"][0] == {"model": "m2", "current": True, "default": False, "price": None}
    assert call(server, "/api/chat/models", token=False)[0] == 403
    data = call(server, "/api/chat/input", "POST", {"text": "你好"})[1]
    wait_event(server, ("idle",), after=data["next"])
    attrs = json.loads(rows(server, "SELECT attrs_json FROM spans WHERE kind='turn' AND name='chat'")[-1][0])
    assert attrs["model"] == "m2"


def test_prices_update_needs_a_known_vendor(served, monkeypatch):
    server, bench, _ = served
    monkeypatch.setenv("ZHIQIU_LLM_BASE_URL", "https://model.example/v1")
    status, data = call(server, "/api/prices/update", "POST", {})
    assert status == 400 and "不认识" in data["error"]
