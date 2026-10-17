import json
from datetime import date

import pytest
from typer.testing import CliRunner

from zhiqiu.cli import app
from zhiqiu.clock import FixedClock
from zhiqiu.conversations import Conversations
from zhiqiu.flow import IllegalTransition
from zhiqiu.store import Store
from zhiqiu.tools import ToolExecutor, ToolRegistry, ToolScope, ToolSpec

OBJECT = {"type": "object", "properties": {}, "additionalProperties": False}


def at(tmp_path, day=date(2026, 10, 13)):
    store = Store(tmp_path / "state.sqlite", FixedClock(day))
    return store, Conversations(store)


def test_same_day_continues_next_day_rolls_over_and_new_and_resume(tmp_path):
    store, chats = at(tmp_path)
    first = chats.open_today()
    assert chats.open_today()["id"] == first["id"]
    store.close()
    store, chats = at(tmp_path, date(2026, 10, 14))
    second = chats.open_today()
    assert second["id"] != first["id"]
    old = chats.get(first["id"])
    assert (old["status"], old["end_reason"]) == ("closed", "day_rollover")
    third = chats.start_new()
    assert chats.get(second["id"])["end_reason"] == "new" and third["status"] == "open"
    reopened = chats.reopen(first["id"])
    assert reopened["status"] == "open" and chats.get(third["id"])["end_reason"] == "switched"
    assert store.db.execute("SELECT COUNT(*) FROM conversations WHERE status='open'").fetchone()[0] == 1


def test_messages_get_consecutive_seq_and_read_back(tmp_path):
    store, chats = at(tmp_path)
    conversation = chats.open_today()
    chats.add_message(conversation["id"], "user", "chat", "今天复习什么？")
    reply = chats.add_message(conversation["id"], "assistant", "chat", "没有到期复习。", {"status": "done"})
    assert [m["seq"] for m in chats.messages(conversation["id"])] == [1, 2]
    assert reply["payload"] == {"status": "done"}
    assert chats.update_payload(reply["id"], rounds=1)["payload"] == {"status": "done", "rounds": 1}
    with pytest.raises(ValueError):
        chats.add_message(conversation["id"], "tool", "chat", "x")
    with pytest.raises(ValueError):
        chats.add_message(conversation["id"], "user", "chat", "  ")


def test_mode_switches_follow_the_table_and_write_events(tmp_path):
    store, chats = at(tmp_path)
    conversation_id = chats.open_today()["id"]
    chats.set_mode(conversation_id, "lesson", "proposal_accepted", active_kind="point", active_id="kp",
                   goal="学习 红黑树")
    chats.set_mode(conversation_id, "self_test", "done")
    with pytest.raises(IllegalTransition):
        chats.set_mode(conversation_id, "lesson", "back")
    chats.set_mode(conversation_id, "chat", "flow_finished")
    row = chats.get(conversation_id)
    assert (row["mode"], row["active_id"], row["goal"]) == ("chat", None, "")
    moves = [(e["from"], e["to"]) for e in
             (json.loads(r[0]) for r in store.db.execute("""SELECT payload_json FROM events
                 WHERE type='StateChanged' ORDER BY id"""))]
    assert moves == [(None, "chat"), ("chat", "lesson"), ("lesson", "self_test"), ("self_test", "chat")]


def test_reconcile_returns_a_leftover_flow_to_chat(tmp_path):
    store, chats = at(tmp_path)
    kp = store.ensure_knowledge_point("二叉树遍历")
    attempt = store.start_attempt(kp, "first")
    conversation_id = chats.open_today()["id"]
    chats.set_mode(conversation_id, "self_test", "resume", active_kind="attempt", active_id=attempt)
    store.finalize(attempt, [], "explicit")
    assert chats.reconcile(conversation_id) == attempt
    assert chats.get(conversation_id)["mode"] == "chat"
    cause = json.loads(store.db.execute("""SELECT payload_json FROM events WHERE type='StateChanged'
        ORDER BY id DESC LIMIT 1""").fetchone()[0])["cause"]
    assert cause == "reconcile" and chats.reconcile(conversation_id) is None


def test_tool_calls_link_to_the_conversation_and_forget_unlinks_them(tmp_path):
    store, chats = at(tmp_path)
    conversation_id = chats.open_today()["id"]
    chats.add_message(conversation_id, "user", "chat", "你好")
    registry = ToolRegistry()
    registry.register(ToolSpec("ping", "ping", OBJECT, "read", lambda args: "pong", "test"))
    ToolExecutor(registry, store.record_tool_call).run(
        "ping", {}, ToolScope(frozenset({"read"}), conversation_id=conversation_id))
    assert chats.log(conversation_id)["tool_calls"][0]["tool"] == "ping"
    exported = store.export_json()
    assert len(exported["conversations"]) == 1 and len(exported["conversation_messages"]) == 1
    assert chats.forget(conversation_id) == 1
    assert store.db.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0
    assert store.db.execute("SELECT conversation_id FROM tool_calls").fetchone()[0] is None
    assert store.db.execute("""SELECT COUNT(*) FROM events WHERE
        json_extract(payload_json,'$.entity')='conversation'""").fetchone()[0] == 0
    with pytest.raises(ValueError):
        chats.forget(conversation_id)


def test_cli_lists_logs_and_forgets_conversations(tmp_path, monkeypatch):
    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: None)
    store, chats = at(tmp_path)
    conversation_id = chats.open_today()["id"]
    chats.add_message(conversation_id, "user", "chat", "你好")
    store.close()
    db = ["--db", str(tmp_path / "state.sqlite")]
    listed = CliRunner().invoke(app, db + ["chat", "list"])
    assert conversation_id in listed.output
    log = CliRunner().invoke(app, db + ["chat", "log", conversation_id])
    assert '"content": "你好"' in log.output
    refused = CliRunner().invoke(app, db + ["chat", "forget"])
    assert refused.exit_code != 0
    forgot = CliRunner().invoke(app, db + ["chat", "forget", "--all"], input="y\n")
    assert "已删除 1 段对话" in forgot.output


def test_titles_are_cleaned_and_fall_back_to_the_first_message(tmp_path):
    from zhiqiu.conversations import EMPTY_TITLE, clean_title
    assert clean_title("「BST 中序遍历有序性」。") == "BST 中序遍历有序性"
    assert clean_title("  学习：C#\n") == "学习：C#" and clean_title("C++？") == "C++"
    assert clean_title("a\u200bb\n\tc\x07") == "ab c"
    assert clean_title("“”。") == ""
    assert clean_title("继续：二叉树遍历（老讲解）") == "继续：二叉树遍历（老讲解）"
    assert clean_title("（空对话）") == "空对话" and clean_title("《算法导论》（第三版）") == "《算法导论》（第三版）"
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 16)))
    chats = Conversations(store)
    conversation = chats.open_today()
    assert chats.display_title(chats.get(conversation["id"])) == EMPTY_TITLE
    first = "请问二叉树的中序遍历为什么总能输出有序序列呢？"
    chats.add_message(conversation["id"], "user", "chat", first)
    assert chats.fallback_title(conversation["id"]) == first[:20] + "…"
    assert chats.get(conversation["id"])["title"] == ""  # computed when shown, not written back
    assert chats.set_title(conversation["id"], chats.fallback_title(conversation["id"]), "fallback").endswith("…")
    assert chats.set_title(conversation["id"], "  “红黑树”  ", "user") == "红黑树"
    row = chats.get(conversation["id"])
    assert (row["title"], row["title_source"]) == ("红黑树", "user")
    assert chats.recent()[0]["title"] == "红黑树"
    for source in ("", "auto"):
        with pytest.raises(ValueError):
            chats.set_title(conversation["id"], "x", source)
    with pytest.raises(ValueError):
        chats.set_title(conversation["id"], "。。", "user")
