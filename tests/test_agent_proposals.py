import json
from datetime import date

from zhiqiu.agent import ChatAgent
from zhiqiu.conversations import Conversations

from test_agent import ChatModel, chat_messages, make_agent, say, tool, tool_rows
from test_flows import ScriptedIO
from test_runtime import complete_first


def conversation_moves(store, conversation_id):
    rows = [json.loads(r[0]) for r in store.db.execute(
        "SELECT payload_json FROM events WHERE type='StateChanged' ORDER BY id")]
    return [(r["from"], r["to"]) for r in rows if r["entity"] == "conversation" and r["id"] == conversation_id]


def proposals(store, agent):
    return [(m["kind"], m["payload"].get("status")) for m in Conversations(store).messages(agent.conversation_id)
            if m["kind"] in ("proposal", "memory_proposal")]


def test_rejected_proposal_never_enters_the_flow(tmp_path):
    store, model, io, agent = make_agent(
        tmp_path, [tool("propose_learn", {"point": "红黑树", "path": "rbtree.md", "reason": "你想学"})],
        confirm=False)
    agent.handle("我想学红黑树")
    assert proposals(store, agent) == [("proposal", "rejected")]
    assert store.get_knowledge_point("红黑树") is None
    assert Conversations(store).get(agent.conversation_id)["mode"] == "chat"
    assert "要开始吗？" in io.said and "好的，先不进入。" in io.said


def test_accepted_learn_proposal_runs_lesson_and_quiz_then_returns_to_chat(tmp_path):
    store, model, io, agent = make_agent(
        tmp_path, [tool("propose_learn", {"point": "红黑树", "path": "rbtree.md"})],
        "/done", "染红", "旋转", "插入后修复")
    agent.handle("我想学红黑树")
    assert proposals(store, agent) == [("proposal", "executed")]
    messages = chat_messages(store, agent)
    assert messages[-1][1] == "flow_result" and "完整评阅" in messages[-1][2]
    assert conversation_moves(store, agent.conversation_id) == [
        (None, "chat"), ("chat", "lesson"), ("lesson", "self_test"), ("self_test", "chat")]
    assert "允许本次学习发送这些内容吗？" in io.said  # the learn consent is still asked
    assert store.db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0] == 1


def test_a_proposal_runs_at_most_once(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    message = Conversations(store).add_message(agent.conversation_id, "assistant", "proposal", "示例", {})
    runs = []
    assert agent.execute_proposal(message["id"], "示例", lambda: runs.append(1)) is True
    assert agent.execute_proposal(message["id"], "示例", lambda: runs.append(1)) is False
    assert runs == [1] and "这个建议已经执行过了。" in io.said


def test_calls_after_a_proposal_in_the_same_round_are_not_run(tmp_path):
    rounds = [{"calls": tool("propose_review", {}, "p")["calls"] + tool("zhiqiu_status", {}, "s")["calls"]}]
    store, model, io, agent = make_agent(tmp_path, rounds)
    agent.handle("复习一下")
    assert [r["tool"] for r in tool_rows(store)] == ["propose_review"]
    # Nothing is due, so the program explains instead of switching modes.
    assert proposals(store, agent) == [("proposal", "refused")]
    assert any("这个建议现在无法执行：现在没有到期的复习" in line for line in io.said)
    assert conversation_moves(store, agent.conversation_id) == [(None, "chat")]


def test_invalid_learn_path_is_refused_before_asking(tmp_path):
    outside = tmp_path / "other.md"
    outside.write_text("其他", encoding="utf-8")
    store, model, io, agent = make_agent(tmp_path, [tool("propose_learn", {"point": "x", "path": str(outside)})])
    agent.handle("学这个")
    assert proposals(store, agent) == [("proposal", "refused")] and "要开始吗？" not in io.said


def test_paused_quiz_is_announced_at_start_and_resumed_by_proposal(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    kp, _, _ = agent.service.prepare("红黑树", agent.read_roots[0] / "rbtree.md", lambda *_: True)
    attempt = store.start_attempt(kp, "first")
    store.pause(attempt)
    model.rounds = [tool("propose_resume", {"target_id": attempt})]
    agent2 = ChatAgent(store, agent.service, ScriptedIO("/pause"), read_roots=agent.read_roots)
    agent2.start()
    assert any("有未完成的自测或讲解" in line for line in agent2.io.inner.said)
    agent2.handle("继续刚才的自测")
    assert store.db.execute("SELECT state FROM attempts WHERE id=?", (attempt,)).fetchone()[0] == "paused"
    assert conversation_moves(store, agent2.conversation_id)[-2:] == [("chat", "self_test"), ("self_test", "chat")]
    assert "已暂停" in chat_messages(store, agent2)[-1][2]


def test_slash_learn_and_review_run_without_a_proposal(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [], "/pause")
    agent.handle(f"/learn 红黑树 --from {agent.read_roots[0] / 'rbtree.md'}")
    assert store.get_knowledge_point("红黑树") is not None and len(store.pending_lessons()) == 1
    assert "要开始吗？" not in io.said and model.seen == []
    kp = store.get_knowledge_point("红黑树")["id"]
    first = complete_first(store, agent.service, kp)
    later = make_agent(tmp_path, [], "/pause", day=date.fromisoformat(first["due_date"]))
    store2, _, io2, agent2 = later
    agent2.handle("/review 红黑树")
    assert store2.db.execute("SELECT state FROM attempts WHERE kind='review'").fetchone()[0] == "paused"


def test_crash_left_mode_is_reconciled_on_start(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    chats = Conversations(store)
    chats.set_mode(agent.conversation_id, "lesson", "flow_stage")
    again = ChatAgent(store, agent.service, ScriptedIO(), read_roots=agent.read_roots)
    again.start()
    assert chats.get(agent.conversation_id)["mode"] == "chat"
    assert conversation_moves(store, agent.conversation_id)[-1] == ("lesson", "chat")


def test_a_flow_error_is_reported_in_the_flow_result(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])

    def broken():
        raise ValueError("模型返回了无效题目")
    agent._run_flow("继续自测", broken)
    last = chat_messages(store, agent)[-1]
    assert last[1] == "flow_result" and "出错停止（模型返回了无效题目）" in last[2]
    assert "模型返回了无效题目" in Conversations(store).get(agent.conversation_id)["last_error"]
