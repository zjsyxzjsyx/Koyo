"""Conversations in groups, and the archive: nothing is deleted, the list only gets tidier."""
import pytest

from zhiqiu.conversations import Conversations

from test_agent import make_agent, say
from test_workbench_http import call, served, wait_event  # noqa: F401  (fixture)


def chats_with(store, *titles):
    chats = Conversations(store)
    ids = []
    for title in titles:
        conversation = chats.start_new()
        chats.add_message(conversation["id"], "user", "chat", title)
        chats.set_title(conversation["id"], title, "user")
        ids.append(conversation["id"])
    return chats, ids


def test_groups_hold_conversations_and_deleting_one_keeps_them(tmp_path):
    store, *_ = make_agent(tmp_path, [])
    chats, (a, b) = chats_with(store, "红黑树", "B 树")
    group = chats.create_group(" 「数据结构」 ")
    assert group["name"] == "数据结构"
    with pytest.raises(ValueError, match="已经有"):
        chats.create_group("数据结构")
    with pytest.raises(ValueError, match="1 到 20"):
        chats.create_group("长" * 21)
    chats.move(a, group["id"])
    assert [(g["name"], g["conversations"]) for g in chats.groups()] == [("数据结构", 1)]
    assert {row["id"]: row["group_name"] for row in chats.recent()}[a] == "数据结构"
    with pytest.raises(LookupError):
        chats.move(b, "no-such-group")
    assert chats.delete_group(group["id"]) == 1
    assert chats.get(a)["group_id"] is None and chats.groups() == []


def test_archived_conversations_leave_the_lists_but_stay(tmp_path):
    store, *_ = make_agent(tmp_path, [])
    chats, (a, b) = chats_with(store, "红黑树", "B 树")
    chats.archive(a)
    listed = [row["id"] for row in chats.recent()]
    assert a not in listed and b in listed
    assert chats.get(a)["archived_at"] and chats.messages(a)
    chats.archive(a, False)
    assert {a, b} <= {row["id"] for row in chats.recent()}


def test_new_conversation_inside_a_group(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [])
    agent.start()
    group = agent.chats.create_group("算法")
    agent.new(group["id"])
    assert agent.chats.get(agent.conversation_id)["group_id"] == group["id"]
    assert io.said[-1] == "已开始新对话（分组「算法」）。"


def test_chats_lists_by_group_and_archive_takes_a_number(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [say("好")])
    agent.start()
    chats, (a, b) = chats_with(store, "红黑树", "B 树")
    group = chats.create_group("数据结构")
    chats.move(a, group["id"])
    agent.handle("你好")
    agent.handle("/chats")
    listing = io.said[-1].split("\n")
    assert listing[0] == "【数据结构】" and listing[1].startswith("1. 红黑树")
    assert listing[2] == "【未分组】" and any(line.endswith("（当前）") for line in listing)
    agent.handle("/archive 1")
    assert io.said[-1] == "已归档「红黑树」。" and chats.get(a)["archived_at"]
    agent.handle("/chats")
    number = next(line.split(".")[0] for line in io.said[-1].split("\n") if line.endswith("（当前）"))
    agent.handle(f"/archive {number}")
    assert io.said[-1].startswith("不能归档当前对话")


def test_sidebar_groups_and_archive_over_http(served):
    server, bench, model = served
    status, data = call(server, "/api/chat/groups/create", "POST", {"name": "算法"})
    assert status == 200
    group = data["group"]
    assert call(server, "/api/chat/groups/create", "POST", {"name": ""})[0] == 400
    # A conversation with a message, then a new one in the group: the worker opens it.
    data = call(server, "/api/chat/input", "POST", {"text": "第一段"})[1]
    wait_event(server, ("idle",), after=data["next"])
    first = bench.conversation_id
    status, data = call(server, "/api/chat/new_in", "POST", {"group_id": group["id"]})
    assert status == 202
    wait_event(server, ("switched",), after=data["next"])
    assert bench.conversation_id != first
    listing = call(server, "/api/chat/conversations")[1]
    assert [g["name"] for g in listing["groups"]] == ["算法"]
    current = next(c for c in listing["conversations"] if c["current"])
    assert current["group_id"] == group["id"]
    # Archiving the current one is refused; another one goes, and comes back.
    assert call(server, "/api/chat/archive", "POST", {"conversation_id": bench.conversation_id})[0] == 409
    assert call(server, "/api/chat/archive", "POST", {"conversation_id": first})[0] == 200
    listing = call(server, "/api/chat/conversations")[1]
    assert next(c for c in listing["conversations"] if c["id"] == first)["archived"] is True
    assert call(server, "/api/chat/move", "POST", {"conversation_id": first, "group_id": group["id"]})[0] == 200
    assert call(server, "/api/chat/move", "POST", {"conversation_id": first, "group_id": "nope"})[0] == 404
    assert call(server, "/api/chat/archive", "POST", {"conversation_id": first, "archived": False})[0] == 200
    assert call(server, "/api/chat/groups/delete", "POST", {"group_id": group["id"]})[1]["ungrouped"] == 2
    assert call(server, "/api/chat/groups/create", "POST", {"name": "x"}, token=False)[0] == 403
