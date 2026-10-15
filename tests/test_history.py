import json
from datetime import date

import pytest

from zhiqiu import history
from zhiqiu.clock import FixedClock
from zhiqiu.conversations import Conversations
from zhiqiu.store import Store
from zhiqiu.tools import ToolError

from test_agent import make_agent, say, tool, tool_rows

RBTREE = "红黑树插入后先把新节点染成红色，再通过旋转和变色恢复性质。"
BPLUS = "B+树的所有数据都在叶子节点，叶子之间用链表相连，适合磁盘上的范围查询。"


def lesson(store, point, text):
    kp_id = store.ensure_knowledge_point(point)
    store.record_message(kp_id, "assistant", "lesson", text)
    return kp_id


def indexed(store, source):
    return store.db.execute("SELECT COUNT(*) FROM search_docs WHERE source=?", (source,)).fetchone()[0]


def fts_rows(store):
    return store.db.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 15)))


def test_two_character_words_match_across_knowledge_points(store):
    lesson(store, "红黑树", RBTREE)
    lesson(store, "B+树", BPLUS)
    result = history.search(store, "旋转")
    assert [(item["source"], item["point"]) for item in result["results"]] == [("lesson", "红黑树")]
    hit = result["results"][0]
    assert hit["matched_terms"] == ["旋转"] and "旋转" in hit["snippet"] and hit["date"] == "2026-10-15"
    # Stop phrases carry no topic, so a question finds the same lesson as its keyword.
    assert [item["point"] for item in history.search(store, "什么是范围查询？")["results"]] == ["B+树"]


def test_bm25_ranks_the_lesson_sharing_more_rare_terms_first(store):
    lesson(store, "红黑树", RBTREE)
    lesson(store, "AVL 树", "AVL 树也靠旋转恢复平衡，但要求左右子树高度差不超过一。")
    ranked = [item["point"] for item in history.search(store, "红黑树旋转")["results"]]
    assert ranked == ["红黑树", "AVL 树"]


def test_index_follows_new_rows_and_forget_removes_them(store):
    kp_id = lesson(store, "红黑树", RBTREE)
    assert history.search(store, "磁盘")["results"] == []
    lesson(store, "B+树", BPLUS)
    assert [item["point"] for item in history.search(store, "磁盘")["results"]] == ["B+树"]
    assert indexed(store, "lesson") == 2
    store.forget(kp_id)
    # Forget drops the deleted lesson from the index in the same transaction, before any search.
    assert indexed(store, "lesson") == 1 and fts_rows(store) == 1
    assert history.search(store, "旋转")["results"] == []


def test_chat_messages_are_searchable_and_chat_forget_purges_them(store):
    chats = Conversations(store)
    conversation = chats.open_today([], {})
    chats.add_message(conversation["id"], "user", "chat", "我想把复习时间改到晚上九点")
    chats.add_message(conversation["id"], "assistant", "proposal", "建议：复习时间")
    hits = history.search(store, "复习时间")["results"]
    # Only chat turns are indexed; proposals and errors are not.
    assert [(item["source"], item["role"]) for item in hits] == [("chat", "user")]
    assert hits[0]["conversation_id"] == conversation["id"]
    # The asking conversation is left out: its turns are already in its own context.
    assert history.search(store, "复习时间", exclude_conversation=conversation["id"])["results"] == []
    chats.forget(conversation["id"])
    assert indexed(store, "chat") == 0 and fts_rows(store) == 0


def test_query_without_searchable_terms_is_invalid(store):
    with pytest.raises(ToolError) as error:
        history.search(store, "什么？a")
    assert error.value.kind == "invalid_args"


def test_long_lessons_return_a_capped_snippet_around_the_hit(store):
    lesson(store, "长文", "铺垫" * 200 + "关键结论在这里" + "收尾" * 200)
    snippet = history.search(store, "关键结论")["results"][0]["snippet"]
    assert snippet.startswith("…") and snippet.endswith("…") and "关键结论" in snippet
    assert len(snippet) <= history.SNIPPET_CHARS + 2


def test_chat_agent_can_search_history_as_a_read_tool(tmp_path):
    store, model, io, agent = make_agent(
        tmp_path, [tool("search_history", {"query": "旋转"}), say("你在红黑树那节学过旋转。")])
    lesson(store, "红黑树", RBTREE)
    agent.handle("我以前学过旋转吗？")
    assert "search_history" in agent.registry.names()
    assert [(row["tool"], row["status"], row["confirmed"]) for row in tool_rows(store)] == [
        ("search_history", "ok", None)]
    sent = model.seen[-1][1][-1]
    assert sent["role"] == "tool" and json.loads(sent["content"])["result"]["results"][0]["point"] == "红黑树"
    assert io.said[-1] == "你在红黑树那节学过旋转。"
