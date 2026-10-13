import json
from datetime import date

import pytest
from typer.testing import CliRunner

from zhiqiu.agent import ChatAgent
from zhiqiu.cli import app
from zhiqiu.clock import FixedClock
from zhiqiu.conversations import Conversations
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store

from test_flows import ScriptedIO
from test_runtime import FakeModel
from test_toolloop import QuerySearch, ToolModel

NOTE = "红黑树插入后先把新节点染成红色，再通过旋转和变色恢复性质。"


def say(text):
    return {"role": "assistant", "content": text}


def tool(name, args=None, call_id=None):
    return {"calls": [{"id": call_id or f"call-{name}", "type": "function",
                       "function": {"name": name, "arguments": json.dumps(args or {}, ensure_ascii=False)}}]}


class ChatModel(ToolModel):
    """ToolModel whose default and closing answers are prose."""

    def __init__(self, rounds, closing="（收尾回答）"):
        # Materials count as sufficient, so preparing a lesson never consumes scripted chat rounds.
        super().__init__(rounds, enough=True)
        self.forced_final = say(closing) if closing else say("")

    def tool_round(self, request, messages, tools, tool_choice="auto", timeout_s=60):
        if not self.rounds and tool_choice != "none":
            self.seen.append((request.stage, [dict(m) for m in messages], tools))
            return say("（默认回答）")
        return super().tool_round(request, messages, tools, tool_choice, timeout_s)


def make_agent(tmp_path, rounds, *inputs, confirm=True, search=None, offline=False, closing="（收尾回答）",
               day=date(2026, 10, 13)):
    root = tmp_path / "notes"
    root.mkdir(parents=True, exist_ok=True)
    (root / "rbtree.md").write_text(NOTE, encoding="utf-8")
    store = Store(tmp_path / "state.sqlite", FixedClock(day))
    model = ChatModel(rounds, closing)
    service = LearningRuntime(store, model, search, search)
    io = ScriptedIO(*inputs, confirm=confirm)
    agent = ChatAgent(store, service, io, read_roots=[root], offline=offline)
    agent.start()
    return store, model, io, agent


def chat_messages(store, agent):
    return [(m["role"], m["kind"], m["content"]) for m in Conversations(store).messages(agent.conversation_id)]


def tool_rows(store):
    return [dict(r) for r in store.db.execute(
        "SELECT tool,status,error_kind,confirmed,conversation_id FROM tool_calls ORDER BY rowid")]


def test_a_turn_can_query_status_then_answer_and_both_messages_are_saved(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [tool("zhiqiu_status"), say("今天没有到期复习。")])
    agent.handle("今天要复习什么？")
    assert chat_messages(store, agent) == [("user", "chat", "今天要复习什么？"),
                                           ("assistant", "chat", "今天没有到期复习。")]
    assert "今天没有到期复习。" in io.said
    assert tool_rows(store) == [{"tool": "zhiqiu_status", "status": "ok", "error_kind": None, "confirmed": None,
                                 "conversation_id": agent.conversation_id}]
    stage, messages, tools = model.seen[0]
    assert stage == "chat" and "学习状态" in messages[0]["content"]
    names = {item["function"]["name"] for item in tools}
    assert {"read_file", "zhiqiu_status", "propose_learn", "propose_memory"} <= names
    assert "notion_page" not in names
    # The next turn sees the earlier exchange as history.
    agent.handle("好的")
    history = model.seen[-1][1]
    assert {"role": "assistant", "content": "今天没有到期复习。"} in history


def test_read_file_is_limited_to_the_read_roots(tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("不在白名单", encoding="utf-8")
    store, model, io, agent = make_agent(tmp_path, [
        tool("read_file", {"path": "rbtree.md"}, "a"), tool("read_file", {"path": str(outside)}, "b"),
        tool("read_file", {"path": "../secret.md"}, "c"), say("读完了")])
    agent.handle("看看我的红黑树笔记")
    assert [(r["tool"], r["status"]) for r in tool_rows(store)] == [
        ("read_file", "ok"), ("read_file", "denied"), ("read_file", "denied")]
    replies = [m for m in model.seen[-1][1] if m.get("role") == "tool"]
    assert NOTE in replies[0]["content"] and "白名单" in replies[1]["content"]
    assert agent.private and agent.private[0].content == NOTE


def test_network_calls_need_confirmation_and_cannot_copy_read_notes(tmp_path):
    search = QuerySearch([])
    store, model, io, agent = make_agent(tmp_path, [tool("web_search", {"query": "红黑树 插入"}), say("好")],
                                         confirm=False, search=search)
    agent.handle("帮我搜一下红黑树插入")
    assert search.queries == [] and tool_rows(store)[0]["status"] == "denied"
    store, model, io, agent = make_agent(tmp_path / "b", [
        tool("read_file", {"path": "rbtree.md"}, "a"),
        tool("web_search", {"query": NOTE[:20]}, "b"),
        tool("web_search", {"query": "红黑树 插入 变色规则"}, "c"), say("搜好了")], search=search)
    agent.handle("读笔记再搜")
    assert [r["status"] for r in tool_rows(store)] == ["ok", "invalid_args", "ok"]
    assert search.queries == ["红黑树 插入 变色规则"]
    assert tool_rows(store)[2]["confirmed"] == 1


def test_offline_chat_offers_no_network_tools(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [say("离线")], search=QuerySearch([]), offline=True)
    agent.handle("你好")
    names = {item["function"]["name"] for item in model.seen[0][2]}
    assert "web_search" not in names and "fetch_url" not in names and "read_file" in names


def test_used_up_rounds_close_with_a_text_answer_or_record_the_failure(tmp_path):
    rounds = [tool("zhiqiu_status", call_id=f"s{i}") for i in range(5)]
    store, model, io, agent = make_agent(tmp_path, list(rounds), closing="最后的回答")
    agent.handle("一直查")
    assert model.choices[-1] == "none" and chat_messages(store, agent)[-1] == ("assistant", "chat", "最后的回答")
    store, model, io, agent = make_agent(tmp_path / "b", list(rounds), closing="")
    agent.handle("一直查")
    assert chat_messages(store, agent)[-1][1] == "error"
    assert "达到轮数上限" in Conversations(store).get(agent.conversation_id)["last_error"]
    agent.handle("还在吗")
    assert chat_messages(store, agent)[-1] == ("assistant", "chat", "（默认回答）")


def test_ctrl_c_cancels_only_the_current_turn(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    original = model.tool_round

    def interrupt(*args, **kwargs):
        model.tool_round = original
        raise KeyboardInterrupt
    model.tool_round = interrupt
    agent.handle("很慢的问题")
    agent.handle("再问一次")
    assert chat_messages(store, agent) == [("user", "chat", "很慢的问题"), ("assistant", "error", "已取消"),
                                           ("user", "chat", "再问一次"), ("assistant", "chat", "（默认回答）")]


def test_same_day_restart_continues_and_slash_commands_run_without_the_model(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [say("你好")])
    agent.handle("在吗")
    first = agent.conversation_id
    agent2 = ChatAgent(store, agent.service, ScriptedIO("/status", "/memory", "/new", "/bogus", "/exit"),
                       read_roots=agent.read_roots)
    agent2.start()
    assert agent2.conversation_id == first
    agent2.run()
    assert agent2.conversation_id != first
    said = agent2.io.inner.text
    assert "当前没有到期复习" in said and "还没有记忆" in said and "未知命令：/bogus" in said
    assert len(model.seen) == 1


def test_cli_chat_shows_status_then_talks_and_bare_command_stays_status_only(tmp_path, monkeypatch):
    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: None)
    model = ChatModel([say("你好，我是知秋。")])
    monkeypatch.setattr("zhiqiu.cli.runtime", lambda store, mcp=False: LearningRuntime(store, model))
    db = ["--db", str(tmp_path / "state.sqlite")]
    result = CliRunner().invoke(app, db + ["chat"], input="你好\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "当前没有到期复习" in result.output and "你好，我是知秋。" in result.output
    bare = CliRunner().invoke(app, db, input="你好\n")
    assert bare.exit_code == 0 and "你：" not in bare.output and "当前没有到期复习" in bare.output


def test_models_without_tool_rounds_report_instead_of_crashing(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 13)))
    agent = ChatAgent(store, LearningRuntime(store, FakeModel()), ScriptedIO())
    agent.start()
    agent.handle("你好")
    assert chat_messages(store, agent)[-1][1] == "error"
