import json
from datetime import date

import pytest
from typer.testing import CliRunner

from zhiqiu.agent import ChatAgent
from zhiqiu.approvals import ApprovalPolicy
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


ASK_NETWORK = ApprovalPolicy({"network": "ask"})


def make_agent(tmp_path, rounds, *inputs, confirm=True, search=None, offline=False, closing="（收尾回答）",
               day=date(2026, 10, 13), policy=None, write_roots=(), extra_tools=()):
    root = tmp_path / "notes"
    root.mkdir(parents=True, exist_ok=True)
    (root / "rbtree.md").write_text(NOTE, encoding="utf-8")
    store = Store(tmp_path / "state.sqlite", FixedClock(day))
    model = ChatModel(rounds, closing)
    service = LearningRuntime(store, model, search, search, write_roots=write_roots, extra_tools=extra_tools)
    io = ScriptedIO(*inputs, confirm=confirm)
    agent = ChatAgent(store, service, io, read_roots=[root], write_roots=write_roots, offline=offline,
                      policy=policy)
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
                                         confirm=False, search=search, policy=ASK_NETWORK)
    agent.handle("帮我搜一下红黑树插入")
    assert search.queries == [] and tool_rows(store)[0]["status"] == "denied"
    # The leak check runs whatever the policy: even auto-approved searches cannot copy read notes.
    store, model, io, agent = make_agent(tmp_path / "b", [
        tool("read_file", {"path": "rbtree.md"}, "a"),
        tool("web_search", {"query": NOTE[:20]}, "b"),
        tool("web_search", {"query": "红黑树 插入 变色规则"}, "c"), say("搜好了")], search=search)
    agent.handle("读笔记再搜")
    assert [r["status"] for r in tool_rows(store)] == ["ok", "invalid_args", "ok"]
    assert search.queries == ["红黑树 插入 变色规则"]
    assert tool_rows(store)[2]["confirmed"] == 1


def test_one_network_approval_covers_the_rest_of_the_turn(tmp_path):
    search = QuerySearch([])
    store, model, io, agent = make_agent(tmp_path, [
        tool("web_search", {"query": "北京 天气 明天"}, "a"),
        tool("web_search", {"query": "Beijing weather tomorrow"}, "b"), say("明天晴")], search=search,
        policy=ASK_NETWORK)
    asked = []
    io.confirm_tool = lambda spec, args: asked.append(args["query"]) or True
    agent.handle("查明天北京的天气")
    assert asked == ["北京 天气 明天"]
    assert search.queries == ["北京 天气 明天", "Beijing weather tomorrow"]
    assert [r["confirmed"] for r in tool_rows(store)] == [1, 1]
    # The second query is still shown, only not asked about.
    assert any("Beijing weather tomorrow" in line and "本轮已允许" in line for line in io.said)
    # The approval ends with the turn.
    model.rounds = [tool("web_search", {"query": "上海 天气"}, "c"), say("多云")]
    agent.handle("上海呢")
    assert asked == ["北京 天气 明天", "上海 天气"]


def test_a_refused_network_call_is_asked_again_in_the_same_turn(tmp_path):
    search = QuerySearch([])
    store, model, io, agent = make_agent(tmp_path, [
        tool("web_search", {"query": "第一次"}, "a"), tool("web_search", {"query": "第二次"}, "b"), say("好")],
        search=search, policy=ASK_NETWORK)
    answers = [False, True]
    io.confirm_tool = lambda spec, args: answers.pop(0)
    agent.handle("搜一下")
    assert answers == [] and search.queries == ["第二次"]
    assert [r["status"] for r in tool_rows(store)] == ["denied", "ok"]


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
    # The greeting itself does not mention /exit; only the terminal adds it.
    assert "输入 /exit 退出。" in result.output and "查看命令，/exit" not in result.output
    bare = CliRunner().invoke(app, db, input="你好\n")
    assert bare.exit_code == 0 and "你：" not in bare.output and "当前没有到期复习" in bare.output


def test_models_without_tool_rounds_report_instead_of_crashing(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 13)))
    agent = ChatAgent(store, LearningRuntime(store, FakeModel()), ScriptedIO())
    agent.start()
    agent.handle("你好")
    assert chat_messages(store, agent)[-1][1] == "error"


def test_after_a_graded_flow_the_total_is_not_repeated_and_detail_shows_the_report(tmp_path):
    import re
    store, model, io, agent = make_agent(tmp_path, [], "/done", "根左右", "递归栈", "表达式树")
    agent.handle(f"/learn 红黑树 --from {agent.read_roots[0] / 'rbtree.md'}")
    assert io.said[-1] == "已回到对话。"
    assert sum("评阅" in line and "分" in line for line in io.said) == 1
    result = chat_messages(store, agent)[-1]
    assert result[1] == "flow_result" and "完整评阅 78 分" in result[2]  # the log keeps the summary
    attempt = store.db.execute("SELECT id FROM attempts").fetchone()[0]
    start = len(io.said)
    agent.handle("/detail")
    report = io.said[start:]
    assert report[0].startswith("完整评阅已保存：78 分") and any(line.startswith("    原题：") for line in report)
    start = len(io.said)
    agent.handle(f"/detail {attempt[:8]}")
    assert io.said[start:] == report
    agent.handle("/detail abc")
    assert io.said[-1] == "短号至少 6 位：abc"
    agent.handle("/detail ffffffff")
    assert io.said[-1] == "没有以 ffffffff 开头的尝试"
    with pytest.raises(ValueError, match="有多个尝试以 abcdef 开头"):
        ChatAgent._resolve_prefix("abcdef", ["abcdef01-x", "abcdef02-y"], "尝试")
    assert not any(re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", line) for line in io.said[:start] if "评阅" in line
                   and "已保存" not in line)


def test_detail_without_a_graded_flow_explains_how_to_pick_one(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    agent.handle("/detail")
    assert "本段对话还没有评阅结果" in io.said[-1]
    agent.handle("/help")
    assert "/detail" in io.said[-1]



class TitledModel(ChatModel):
    """A chat model that also names the conversation; `titles` is what each call returns (or raises)."""

    def __init__(self, rounds, titles):
        super().__init__(rounds)
        self.titles = list(titles)
        self.title_inputs = []

    def conversation_title(self, text, reply):
        self.title_inputs.append((text, reply))
        value = self.titles.pop(0)
        if isinstance(value, Exception):
            raise value
        return {"title": value}


def titled_agent(tmp_path, rounds, titles):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 16)))
    model = TitledModel(rounds, titles)
    io = ScriptedIO()
    agent = ChatAgent(store, LearningRuntime(store, model), io)
    agent.start()
    return store, model, io, agent


def title_of(store, agent):
    row = Conversations(store).get(agent.conversation_id)
    return row["title"], row["title_source"]


def test_the_model_names_a_conversation_once_after_its_first_answer(tmp_path):
    store, model, io, agent = titled_agent(tmp_path, [say("中序遍历先左后根再右。"), say("是的。")],
                                           ["“BST 中序遍历有序性”"])
    agent.handle("二叉树的中序遍历为什么能输出有序序列")
    assert title_of(store, agent) == ("BST 中序遍历有序性", "model")
    assert model.title_inputs == [("二叉树的中序遍历为什么能输出有序序列", "中序遍历先左后根再右。")]
    agent.handle("那后序呢")
    assert len(model.title_inputs) == 1 and title_of(store, agent)[1] == "model"
    assert io.said[-1] == "是的。"  # nothing about the title is said


@pytest.mark.parametrize("value", ["", "一个远远超过十六个字的非常非常长的对话标题", ValueError("模型超时")])
def test_a_bad_or_failed_title_falls_back_to_the_first_message(tmp_path, value):
    store, model, io, agent = titled_agent(tmp_path, [say("好的。")], [value])
    agent.handle("明天杭州天气怎么样")
    assert title_of(store, agent) == ("明天杭州天气怎么样", "fallback")
    failures = store.db.execute("SELECT stage,conversation_id FROM llm_failures").fetchall()
    assert [tuple(row) for row in failures] == [("conversation_title", agent.conversation_id)]
    assert io.said[-1] == "好的。"


def test_a_conversation_opened_by_a_command_is_named_by_rule(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [], "/pause")
    agent.handle(f"/learn 红黑树 --from {agent.read_roots[0] / 'rbtree.md'}")
    assert title_of(store, agent) == ("学习：红黑树", "rule")
    assert store.db.execute("SELECT COUNT(*) FROM llm_failures WHERE stage='conversation_title'").fetchone()[0] == 0
    agent.handle("/new")
    agent.handle("/resume")
    assert title_of(store, agent) == ("继续：红黑树", "rule")
    # A command later in a conversation does not rename it.
    agent.handle("/status")
    agent.handle(f"/learn 红黑树 --from {agent.read_roots[0] / 'rbtree.md'}")
    assert title_of(store, agent) == ("继续：红黑树", "rule")


def test_the_user_renames_a_conversation_and_keeps_that_title(tmp_path):
    store, model, io, agent = titled_agent(tmp_path, [say("好。"), say("嗯。")], ["模型标题"])
    agent.handle("/title  「我的红黑树笔记」 ")
    assert title_of(store, agent) == ("我的红黑树笔记", "user") and io.said[-1] == "已改名为「我的红黑树笔记」。"
    agent.handle("你好")
    assert title_of(store, agent) == ("我的红黑树笔记", "user") and model.title_inputs == []
    agent.handle("/title " + "长" * 31)
    assert io.said[-1] == "标题最多 30 字（现在 31 字）"
    agent.handle("/title 。。")
    assert io.said[-1] == "用法：/title 新标题"
    assert title_of(store, agent) == ("我的红黑树笔记", "user")



UUID_RE = __import__("re").compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def test_greetings_new_and_switch_show_titles_not_ids(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [say("你好"), say("在的")])
    assert io.said[-1] == "新对话。输入 /help 查看命令。"
    agent.handle("/title 红黑树复习")
    agent.handle("在吗")
    first = agent.conversation_id
    again = ChatAgent(store, agent.service, ScriptedIO(), read_roots=agent.read_roots)
    again.start()
    assert again.io.inner.said[-1] == "接着「红黑树复习」（3 条消息）。输入 /help 查看命令。"
    again.handle("/new")
    assert again.io.inner.said[-1] == "已开始新对话。"
    again.switch(first)
    assert again.conversation_id == first and again.io.inner.said[-1] == "已切换到「红黑树复习」（4 条消息）。"
    assert Conversations(store).get(first)["status"] == "open"
    again.switch(first)
    assert again.io.inner.said[-1] == "已经在「红黑树复习」里了。"
    assert not any(UUID_RE.search(line) for line in io.said + again.io.inner.said)


def test_chats_numbers_are_good_for_the_next_input_only(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [say("一"), say("二")])
    agent.handle("第一段")
    first = agent.conversation_id
    agent.handle("/new")
    agent.handle("/new")  # an empty conversation in between is not listed, and /new does not fill it
    agent.handle("第二段")
    second = agent.conversation_id
    agent.handle("/chats")
    listing = io.said[-1].splitlines()
    assert listing[0].startswith("1. 第二段 · 今天 · ") and listing[0].endswith("（当前）")
    assert listing[1].startswith("2. 第一段 · 今天 · 3 条") and len(listing) == 3
    agent.handle("/open 2")
    assert agent.conversation_id == first
    agent.handle("/open 1")
    assert io.said[-1] == "编号已失效，请先用 /chats 重新列出。" and agent.conversation_id == first
    agent.handle("/chats")
    agent.handle("/status")
    agent.handle("/open 1")
    assert io.said[-1] == "编号已失效，请先用 /chats 重新列出。"
    agent.handle("/chats")
    agent.handle("/open 9")
    assert io.said[-1].startswith("没有编号 9")
    agent.handle(f"/open {second[:8]}")
    assert agent.conversation_id == second
    assert not any(UUID_RE.search(line) for line in io.said)


def test_status_and_resume_use_short_ids(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [], "/pause", "/pause", "/pause")
    agent.handle(f"/learn 红黑树 --from {agent.read_roots[0] / 'rbtree.md'}")
    session = store.pending_lessons()[0]["session_id"]
    agent.handle("/status")
    assert f"待继续讲解：红黑树  短号 {session[:8]}" in io.said[-1] and not UUID_RE.search(io.said[-1])
    agent.handle(f"/resume {session[:8]}")
    assert any(line == "继续讲解：红黑树" for line in io.said)
    kp = store.get_knowledge_point("红黑树")["id"]
    attempt = store.start_attempt(kp, "first")
    store.pause(attempt)
    agent.handle("/status")
    assert f"待继续：红黑树  paused  短号 {attempt[:8]}" in io.said[-1] and not UUID_RE.search(io.said[-1])
    agent.handle(f"/resume {attempt[:6]}")
    assert store.db.execute("SELECT state FROM attempts WHERE id=?", (attempt,)).fetchone()[0] == "paused"
    assert any(line == f"已暂停（短号 {attempt[:8]}），可以用 resume 继续。" for line in io.said)
    agent.handle("/resume abc")
    assert io.said[-1] == "短号至少 6 位：abc"
