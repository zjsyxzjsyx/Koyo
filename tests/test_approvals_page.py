"""Answering a waiting approval from the approvals page: the same prompt as the chat card."""
import sqlite3

from test_agent import say, tool
from test_workbench_http import call, served, wait_event  # noqa: F401  (served is a fixture)


def approval_rows(server):
    db = sqlite3.connect(server.db_path)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute("SELECT id,status,decided_via FROM approval_requests ORDER BY created_at_ns")]
    finally:
        db.close()


def test_the_state_lists_the_waiting_approval_with_its_ids_and_full_text(served):
    server, bench, model = served
    model.rounds = [tool("write_file", {"path": "a.md", "content": "第一行\n第二行"}, "a"), say("好")]
    after = call(server, "/api/chat/input", "POST", {"text": "写下来"})[1]["next"]
    event, _ = wait_event(server, ("approval",), after=after)
    [pending] = call(server, "/api/chat/state")[1]["risk"]["pending"]
    [row] = approval_rows(server)
    assert pending["request_id"] == event["request_id"] and pending["approval_request_id"] == row["id"]
    assert row["status"] == "pending" and pending["text"].startswith("写入文件")
    assert "第一行\n第二行" in pending["full_text"] and "\n" not in pending["text"]
    call(server, "/api/chat/respond", "POST", {"request_id": event["request_id"], "value": False})
    wait_event(server, ("idle",), after=event["seq"])


def test_approving_on_the_approvals_page_runs_the_tool_and_marks_the_origin(served, tmp_path):
    server, bench, model = served
    model.rounds = [tool("write_file", {"path": "a.md", "content": "笔记"}, "a"), say("写好了")]
    after = call(server, "/api/chat/input", "POST", {"text": "写下来"})[1]["next"]
    event, _ = wait_event(server, ("approval",), after=after)
    body = {"request_id": event["request_id"], "value": True, "origin": "approvals"}
    assert call(server, "/api/chat/respond", "POST", body)[0] == 200
    # The chat card is the same prompt: answering it again is refused.
    assert call(server, "/api/chat/respond", "POST", {**body, "origin": "chat"})[0] == 409
    resolved, _ = wait_event(server, ("resolved",), after=event["seq"])
    assert resolved["origin"] == "approvals" and resolved["value"] is True
    wait_event(server, ("idle",), after=event["seq"])
    assert (tmp_path / "out" / "a.md").read_text(encoding="utf-8") == "笔记"
    assert [(row["status"], row["decided_via"]) for row in approval_rows(server)] == [("approved", "web")]


def test_the_chat_card_still_answers_with_its_own_origin(served):
    server, bench, model = served
    model.rounds = [tool("write_file", {"path": "a.md", "content": "笔记"}, "a"), say("好")]
    after = call(server, "/api/chat/input", "POST", {"text": "写下来"})[1]["next"]
    event, _ = wait_event(server, ("approval",), after=after)
    assert call(server, "/api/chat/respond", "POST", {"request_id": event["request_id"], "value": False})[0] == 200
    resolved, _ = wait_event(server, ("resolved",), after=event["seq"])
    assert resolved["origin"] == "chat"
    wait_event(server, ("idle",), after=event["seq"])
    assert [row["status"] for row in approval_rows(server)] == ["denied"]


def test_the_approvals_page_answers_only_approvals_and_known_origins(served):
    server, bench, model = served
    model.rounds = [tool("propose_learn", {"point": "红黑树", "path": "rbtree.md"})]
    after = call(server, "/api/chat/input", "POST", {"text": "我想学红黑树"})[1]["next"]
    event, _ = wait_event(server, ("confirm",), after=after)
    for origin in ("approvals", "elsewhere"):
        status, data = call(server, "/api/chat/respond", "POST",
                            {"request_id": event["request_id"], "value": False, "origin": origin})
        assert status == 400, (origin, data)
    assert call(server, "/api/chat/respond", "POST", {"request_id": event["request_id"], "value": False})[0] == 200
    wait_event(server, ("idle",), after=event["seq"])
