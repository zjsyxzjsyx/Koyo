import json
from datetime import date

from zhiqiu.clock import FixedClock
from zhiqiu.context import system_message
from zhiqiu.conversations import Conversations
from zhiqiu.store import SCHEMA_VERSION, Store

from test_agent import ChatModel, make_agent


class SummaryModel(ChatModel):
    """ChatModel that can summarize chat history and can be told to fail."""

    def __init__(self, rounds=(), fail=False):
        super().__init__(list(rounds))
        self.summaries = []
        self.fail = fail

    def summarize_chat(self, previous, items):
        if self.fail:
            raise ValueError("摘要服务不可用")
        self.summaries.append((previous, len(items)))
        return {"summary": f"第{len(self.summaries)}次摘要，覆盖{len(items)}条较早消息"}


class CountingModel(ChatModel):
    """Reports a provider prompt token count for every round, like the real adapter."""

    def tool_round(self, request, messages, tools, tool_choice="auto", timeout_s=60):
        message = super().tool_round(request, messages, tools, tool_choice, timeout_s)
        self.last_usage = {"prompt_tokens": 900}
        self.last_usage_request_build_id = request.build_id
        return message


def system_prompts(model):
    return [seen[1][0]["content"] for seen in model.seen]


def fill(store, conversation_id, pairs, text="我们之前聊过一个比较长的话题，这里是第{}句，用来占用上下文预算"):
    chats = Conversations(store)
    for index in range(pairs):
        chats.add_message(conversation_id, "user", "chat", text.format(2 * index))
        chats.add_message(conversation_id, "assistant", "chat", text.format(2 * index + 1))


def budget(agent, window=6000, reserve=500, margin=100):
    context = agent.service.context
    context.window, context.output_reserve, context.margin = window, reserve, margin


def test_status_is_a_snapshot_refreshed_only_at_start_and_after_flows(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [], "/pause")
    agent.handle("你好")
    agent.handle("再聊一句")
    first, second = system_prompts(model)
    assert first == second  # the prefix stays the same between turns
    other = store.ensure_knowledge_point("堆排序")
    store.start_attempt(other, "first")
    agent.handle("第三句")
    assert "堆排序" not in system_prompts(model)[-1]  # changed outside the chat: not refreshed yet
    agent.handle(f"/learn 红黑树 --from {agent.read_roots[0] / 'rbtree.md'}")
    agent.handle("学完了吗")
    latest = system_prompts(model)[-1]
    assert "红黑树" in latest and "堆排序" in latest  # the flow refreshed the snapshot


def test_provider_counts_let_the_next_turn_reuse_the_measured_prefix(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    counting = CountingModel([])
    agent.service.model = counting
    agent.service.context.model = counting
    agent.handle("第一句")
    agent.handle("第二句")
    methods = [json.loads(row[0])["estimate_method"] for row in store.db.execute(
        "SELECT budget_json FROM context_builds WHERE stage='chat' ORDER BY rowid")]
    assert methods == ["utf8_bytes", "provider_usage_plus_new_messages"]


def test_older_turns_are_folded_into_a_summary_and_originals_stay(tmp_path):
    store, _, io, agent = make_agent(tmp_path, [])
    model = SummaryModel()
    agent.service.model = model
    budget(agent)
    fill(store, agent.conversation_id, 20)
    agent.handle("接着聊")
    chats = Conversations(store)
    summary = chats.latest_summary(agent.conversation_id)
    assert model.summaries == [("", 30)] and summary["upto_seq"] == 30
    sent = model.seen[-1][1]
    assert "第1次摘要，覆盖30条较早消息" in sent[0]["content"]
    assert len(sent) == 1 + 10 + 1  # system with summary, the 10 newest messages, the current input
    assert len(chats.messages(agent.conversation_id)) == 42  # nothing was deleted
    agent.handle("再聊一句")
    assert len(model.summaries) == 1  # under the threshold again: no new summary
    fill(store, agent.conversation_id, 15)
    agent.handle("又聊了很多")
    assert model.summaries[-1][0] == "第1次摘要，覆盖30条较早消息"  # updates the previous summary
    log = chats.log(agent.conversation_id)
    assert [item["upto_seq"] for item in log["summaries"]] == [30, chats.latest_summary(agent.conversation_id)["upto_seq"]]


def test_without_a_budget_long_history_is_summarized_by_count(tmp_path):
    store, _, io, agent = make_agent(tmp_path, [])
    model = SummaryModel()
    agent.service.model = model
    fill(store, agent.conversation_id, 21, text="第{}句")
    agent.handle("现在")
    assert model.summaries == [("", 32)]


def test_a_failed_summary_still_answers_and_records_why(tmp_path):
    store, _, io, agent = make_agent(tmp_path, [])
    model = SummaryModel(fail=True)
    agent.service.model = model
    budget(agent)
    fill(store, agent.conversation_id, 20)
    agent.handle("接着聊")
    chats = Conversations(store)
    assert chats.latest_summary(agent.conversation_id) is None
    assert "对话摘要生成失败" in chats.get(agent.conversation_id)["last_error"]
    assert chats.messages(agent.conversation_id)[-1]["content"] == "（默认回答）"


def test_over_budget_drops_old_history_first_and_the_summary_last(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    context = agent.service.context
    history = [{"id": f"h{i}", "role": "user", "content": "旧" * 600} for i in range(3)]
    summary = {"id": "s1", "content": "摘" * 600}
    required, _ = context.estimate([{"role": "system", "content": system_message("chat")["content"] + "\n\n上下文"},
                                    {"role": "user", "content": "现在"}])
    # Room for the required part plus a little: neither the history nor the summary fits.
    context.window, context.output_reserve, context.margin = required + 300 + 600, 500, 100
    request = context.build_chat("上下文", history, "现在", agent.conversation_id, summary)
    excluded = json.loads(store.db.execute("SELECT excluded_json FROM context_builds WHERE id=?",
                                           (request.build_id,)).fetchone()[0])
    assert [item["id"] for item in excluded] == ["h0", "h1", "h2", "s1"]
    assert "较早对话的摘要" not in request.messages[0]["content"]


def test_v14_database_gains_status_snapshot_and_summaries_and_forget_removes_them(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 13)))
    chats = Conversations(store)
    conversation = chats.open_today()
    store.db.execute("DROP TABLE chat_summaries")
    store.db.execute("ALTER TABLE conversations DROP COLUMN status_snapshot_json")
    store.db.execute("PRAGMA user_version=14")
    store.close()
    reopened = Store(path, FixedClock(date(2026, 10, 13)))
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 24
    assert path.with_suffix(".sqlite.pre-v15.bak").exists()
    chats = Conversations(reopened)
    assert chats.get(conversation["id"])["status_snapshot_json"] == "{}"
    chats.add_message(conversation["id"], "user", "chat", "你好")
    chats.add_summary(conversation["id"], 1, "hash", "一段摘要")
    assert len(reopened.export_json()["chat_summaries"]) == 1
    chats.forget(conversation["id"])
    assert reopened.db.execute("SELECT COUNT(*) FROM chat_summaries").fetchone()[0] == 0
    reopened.close()


def test_usage_splits_the_estimate_and_marks_where_summarizing_starts(tmp_path):
    store, _, io, agent = make_agent(tmp_path, [])
    fill(store, agent.conversation_id, 3)
    unbudgeted = agent.context_usage()
    assert unbudgeted["limit"] is None and unbudgeted["trigger"] is None
    assert (unbudgeted["messages"], unbudgeted["history_messages"], unbudgeted["can_compact"]) == (6, 40, False)
    budget(agent)
    usage = agent.context_usage()
    assert usage["limit"] == 5400 and usage["trigger"] == int(0.7 * 5400) and usage["method"] == "utf8_bytes"
    parts = {part["key"]: part["tokens"] for part in usage["parts"]}
    assert sum(parts.values()) == usage["estimate"] and parts["summary"] == 0
    assert parts["history"] > 0 and parts["rules"] > parts["context"] > 0
    # The total is what build_chat would check for the same messages.
    summary = Conversations(store).latest_summary(agent.conversation_id)
    request = agent.service.context.build_chat(agent._context(), agent._history(summary, drop_last=False), "",
                                               agent.conversation_id)
    assert request.budget["input_estimate"] == usage["estimate"]


def test_compact_folds_all_but_the_newest_messages_on_request(tmp_path):
    store, _, io, agent = make_agent(tmp_path, [])
    model = SummaryModel()
    agent.service.model = model
    fill(store, agent.conversation_id, 8)
    agent.handle("/compact")
    chats = Conversations(store)
    assert model.summaries == [("", 6)] and chats.latest_summary(agent.conversation_id)["upto_seq"] == 6
    assert any(line.startswith("已压缩：6 条较早的消息并入摘要，最近 10 条保留原文") for line in io.said)
    usage = agent.context_usage()
    assert usage["messages"] == 10 and usage["summarized_upto"] == 6 and usage["can_compact"] is False
    assert {part["key"]: part["tokens"] for part in usage["parts"]}["summary"] > 0
    traced = store.db.execute("SELECT name,end_reason FROM spans WHERE kind='turn'").fetchall()
    assert [tuple(row) for row in traced] == [("/compact", "compacted")]
    agent.handle("/compact")  # nothing older than the newest ten: no model call
    assert len(model.summaries) == 1 and io.said[-1].startswith("可压缩的消息不足")


def test_a_failed_compact_keeps_the_messages_and_says_why(tmp_path):
    store, _, io, agent = make_agent(tmp_path, [])
    agent.service.model = SummaryModel(fail=True)
    fill(store, agent.conversation_id, 8)
    agent.handle("/compact")
    chats = Conversations(store)
    assert chats.latest_summary(agent.conversation_id) is None
    assert "对话压缩失败" in chats.get(agent.conversation_id)["last_error"] and "对话压缩失败" in io.said[-1]
    assert agent.context_usage()["messages"] == 16
