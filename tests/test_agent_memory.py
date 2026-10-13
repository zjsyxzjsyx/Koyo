from zhiqiu.memory import MemoryService

from test_agent import make_agent, say, tool, tool_rows
from test_agent_proposals import proposals

REMEMBER = {"key": "pref.讲解顺序", "content": "先给结论再展开", "reason": "用户说以后都这样讲"}


def system_prompt(model, turn=-1):
    return model.seen[turn][1][0]["content"]


def test_confirmed_memory_is_saved_with_its_source_and_applies_from_the_next_conversation(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [tool("propose_memory", REMEMBER), say("好的")])
    agent.handle("以后讲解先给结论再展开")
    saved = MemoryService(store).list_semantic(None)
    message_id = [m for m in agent.chats.messages(agent.conversation_id) if m["kind"] == "memory_proposal"][0]["id"]
    assert [(m["key"], m["content"], m["source"]) for m in saved] == [
        ("pref.讲解顺序", "先给结论再展开", f"chat:{message_id}")]
    assert proposals(store, agent) == [("memory_proposal", "saved")]
    agent.handle("那讲讲红黑树")
    assert "先给结论再展开" not in system_prompt(model)  # frozen snapshot of this conversation
    agent.handle("/new")
    agent.handle("新的一段")
    assert "pref.讲解顺序: 先给结论再展开" in system_prompt(model)


def test_rejected_memory_is_not_saved(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [tool("propose_memory", REMEMBER)], confirm=False)
    agent.handle("以后讲解先给结论再展开")
    assert MemoryService(store).list_semantic(None) == []
    assert proposals(store, agent) == [("memory_proposal", "rejected")]


def test_memory_rules_are_checked_before_the_user_is_asked(tmp_path):
    long_text = "很长" * 200
    store, model, io, agent = make_agent(tmp_path, [
        tool("propose_memory", {"key": "语言偏好", "content": "用 Python"}, "a"),
        tool("propose_memory", {"key": "pref.长", "content": long_text}, "b"),
        say("我换个说法")])
    agent.handle("记住这些")
    assert [r["status"] for r in tool_rows(store)] == ["invalid_args", "invalid_args"]
    assert MemoryService(store).list_semantic(None) == [] and "要记住吗？" not in io.said
    full, full_model, _, full_agent = make_agent(tmp_path / "b", [tool("propose_memory", REMEMBER), say("满了")])
    for index in range(7):
        MemoryService(full).set_semantic(f"soul.条目{index}", "字" * 290, "测试")
    full_agent.handle("再记一条")
    replies = [m["content"] for m in full_model.seen[-1][1] if m.get("role") == "tool"]
    assert "合计不能超过 2000 字" in replies[0]
    # Rewriting an existing key counts only its new text, so it still fits.
    full_model.rounds = [tool("propose_memory", {"key": "soul.条目0", "content": "短"}), say("改好了")]
    full_agent.handle("把第一条改短")
    assert MemoryService(full).list_semantic(None)[0]["content"] == "短"
