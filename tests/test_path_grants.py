"""9.0: a path outside the read and write roots is asked for in the conversation (once / add its folder / no)."""
import json
import threading
import time

import pytest

from zhiqiu.approvals import ApprovalPolicy
from zhiqiu.files import FileRoots
from zhiqiu.webio import WebIO

from test_agent import make_agent, say, tool, tool_rows
from test_flows import ScriptedIO


class GrantIO(ScriptedIO):
    """ScriptedIO that answers path grants from a list and keeps every offer."""

    def __init__(self, *grants, confirm=True):
        super().__init__(confirm=confirm)
        self.grants = list(grants)
        self.offers = []

    def grant_path(self, offer, request_id=None):
        self.offers.append(offer)
        answer = self.grants.pop(0)
        return answer(offer) if callable(answer) else answer


def agent_with(tmp_path, rounds, *grants, policy=None):
    store, model, _, agent = make_agent(tmp_path, rounds, policy=policy)
    io = GrantIO(*grants)
    agent.io.inner = io
    # The test files live under %TEMP%, which is inside AppData, a folder never offered as a root.
    agent.blocked_dirs = []
    return store, model, io, agent


@pytest.fixture
def outside(tmp_path):
    folder = tmp_path / "elsewhere" / "redis"
    folder.mkdir(parents=True)
    (folder / "持久化.md").write_text("RDB 是快照，AOF 记录写命令。", encoding="utf-8")
    (folder / "复制.md").write_text("主从复制先全量再增量。", encoding="utf-8")
    return folder


def approvals(store):
    return [dict(row) for row in store.db.execute(
        'SELECT tool, permission, status, "grant", root_path FROM approval_requests ORDER BY created_at_ns')]


def read(path, call_id="r"):
    return tool("read_file", {"path": str(path)}, call_id)


def test_once_reads_the_file_and_adds_nothing(tmp_path, outside):
    target = outside / "持久化.md"
    store, model, io, agent = agent_with(tmp_path, [read(target), say("讲得对。")], "once")
    agent.handle(f"帮我看看 {target} 讲得对不对")
    assert io.offers[0]["access"] == "read" and io.offers[0]["path"] == str(target.resolve())
    assert io.offers[0]["folders"][0]["path"] == str(outside.resolve()) and io.offers[0]["folders"][0]["files"] == 2
    assert tool_rows(store)[0]["status"] == "ok"
    assert "RDB 是快照" in json.dumps(model.seen[-1][1], ensure_ascii=False)
    assert approvals(store) == [{"tool": "read_file", "permission": "read_path", "status": "approved",
                                 "grant": "once", "root_path": None}]
    assert store.roots("read") == []
    # Once is for this turn: the next turn asks again.
    model.rounds = [read(target, "r2"), say("同上。")]
    io.grants = ["denied"]
    agent.handle("再读一次")
    assert len(io.offers) == 2 and tool_rows(store)[-1]["status"] == "denied"


def test_adding_the_folder_makes_it_a_root_for_later_turns(tmp_path, outside):
    store, model, io, agent = agent_with(tmp_path, [read(outside / "持久化.md"), say("好")],
                                         lambda offer: "root:" + offer["folders"][0]["path"])
    agent.handle("读一下")
    assert [row["path"] for row in store.roots("read")] == [str(outside.resolve())]
    event = store.db.execute("SELECT payload_json FROM events WHERE type='RootsChanged'").fetchone()
    assert json.loads(event[0])["via"] == "chat_grant"
    assert approvals(store)[0]["grant"] == "root" and approvals(store)[0]["root_path"] == str(outside.resolve())
    assert any("加入可读目录" in line for line in io.said)
    model.rounds = [read(outside / "复制.md", "r2"), say("好")]
    agent.handle("再读另一篇")
    assert len(io.offers) == 1 and tool_rows(store)[-1]["status"] == "ok"
    assert FileRoots().current(store, "read") == [outside.resolve()]


def test_a_folder_not_offered_cannot_be_added_by_the_answer(tmp_path, outside):
    store, model, io, agent = agent_with(tmp_path, [read(outside / "持久化.md"), say("好")], "root:C:\\")
    agent.handle("读一下")
    assert store.roots("read") == [] and approvals(store)[0]["status"] == "denied"
    assert tool_rows(store)[0]["status"] == "denied"


def test_no_is_told_to_the_model(tmp_path, outside):
    store, model, io, agent = agent_with(tmp_path, [read(outside / "持久化.md"), say("好的，不读了。")], "denied")
    agent.handle("读一下")
    assert tool_rows(store)[0]["status"] == "denied"
    assert "用户没有允许读取" in json.dumps(model.seen[-1][1], ensure_ascii=False)
    assert approvals(store)[0]["status"] == "denied" and approvals(store)[0]["grant"] == "denied"


def test_hidden_system_and_own_folders_are_refused_without_asking(tmp_path, monkeypatch):
    secret = tmp_path / ".ssh" / "id_rsa.txt"
    secret.parent.mkdir()
    secret.write_text("PRIVATE", encoding="utf-8")
    system = tmp_path / "Windows"
    system.mkdir()
    (system / "notes.md").write_text("系统", encoding="utf-8")
    own = tmp_path / "localappdata" / "zhiqiu"
    own.mkdir(parents=True)
    (own / "dump.md").write_text("数据", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    store, model, io, agent = agent_with(tmp_path, [read(secret, "a"), read(system / "notes.md", "b"),
                                                    read(own / "dump.md", "c"), say("读不了。")])
    agent.blocked_dirs = [system]
    agent.handle("读这些")
    assert io.offers == []
    reasons = json.dumps(model.seen[-1][1], ensure_ascii=False)
    assert "隐藏目录或文件" in reasons and "系统目录" in reasons and "知秋自己的数据或配置目录" in reasons
    assert "PRIVATE" not in reasons and approvals(store) == []


def test_one_turn_asks_at_most_five_times(tmp_path):
    folder = tmp_path / "many"
    folder.mkdir()
    rounds = []
    for number in range(6):
        (folder / f"{number}.md").write_text(f"第 {number} 篇", encoding="utf-8")
        rounds.append(read(folder / f"{number}.md", f"r{number}"))
    store, model, io, agent = agent_with(tmp_path, rounds + [say("好")], *["denied"] * 6)
    agent.max_rounds = 10
    agent.handle("全读")
    assert len(io.offers) == 5
    assert "本轮已经申请了 5 次" in json.dumps(model.seen[-1][1], ensure_ascii=False)


def test_policy_can_turn_asking_off(tmp_path, outside):
    store, model, io, agent = agent_with(tmp_path, [read(outside / "持久化.md"), say("好")],
                                         policy=ApprovalPolicy({"read_path": "deny"}))
    agent.handle("读一下")
    assert io.offers == [] and "不在可读目录白名单内" in json.dumps(model.seen[-1][1], ensure_ascii=False)
    with pytest.raises(ValueError, match="不能配置为 allow"):
        ApprovalPolicy({"read_path": "allow"})
    assert "白名单外路径：已禁止" in ApprovalPolicy({"read_path": "deny"}).text()


def test_writing_outside_the_roots_is_one_card_with_the_preview(tmp_path, outside):
    target = outside / "总结.md"
    store, model, io, agent = agent_with(
        tmp_path, [tool("write_file", {"path": str(target), "content": "# 持久化总结"}, "w"), say("写好了")], "once")
    agent.handle("把总结写到 redis 目录")
    assert target.read_text(encoding="utf-8") == "# 持久化总结"
    offer = io.offers[0]
    assert offer["access"] == "write" and "内容预览" in offer["preview"] and "# 持久化总结" in offer["preview"]
    assert approvals(store) == [{"tool": "write_file", "permission": "write_path", "status": "approved",
                                 "grant": "once", "root_path": None}]
    row = tool_rows(store)[0]
    assert row["status"] == "ok" and row["confirmed"] == 1


def test_a_denied_write_writes_nothing(tmp_path, outside):
    target = outside / "总结.md"
    store, model, io, agent = agent_with(
        tmp_path, [tool("write_file", {"path": str(target), "content": "x"}, "w"), say("好")], "denied")
    agent.handle("写")
    assert not target.exists() and tool_rows(store)[0]["status"] == "denied"


def test_an_io_without_cards_is_asked_yes_or_no_for_this_one_time(tmp_path, outside):
    store, model, io, agent = make_agent(tmp_path, [read(outside / "持久化.md"), say("好")])
    agent.blocked_dirs = []
    agent.handle("读一下")
    assert any("白名单外的文件" in line and "只允许这一次吗" in line for line in io.said)
    assert approvals(store)[0]["grant"] == "once"


def test_the_web_card_accepts_only_its_three_answers():
    io = WebIO()
    result = {}
    offer = {"access": "read", "path": "D:/x.md", "folders": [{"path": "D:/", "refused": "根目录"}], "endpoint": "e"}
    worker = threading.Thread(target=lambda: result.update(answer=io.grant_path(offer, "req")))
    worker.start()
    deadline = time.monotonic() + 5
    while not io.pending() and time.monotonic() < deadline:
        time.sleep(0.01)
    event = io.pending()[0]
    assert event["kind"] == "grant" and event["access"] == "read" and event["approval_request_id"] == "req"
    with pytest.raises(ValueError, match="once、denied 或 root"):
        io.respond(event["request_id"], "yes")
    with pytest.raises(ValueError):
        io.respond(event["request_id"], True)
    io.respond(event["request_id"], "once")
    worker.join(5)
    assert result["answer"] == "once"


def test_the_terminal_offers_a_numbered_folder(monkeypatch):
    from zhiqiu.cli import TerminalIO
    offer = {"access": "read", "path": "D:/a/b/x.md", "endpoint": "e", "folders": [
        {"path": "D:/a/b", "files": 3, "truncated": False}, {"path": "D:/a", "files": 9, "truncated": True}]}
    answers = iter(["a", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert TerminalIO().grant_path(offer) == "root:D:/a"
    answers = iter(["y"])
    assert TerminalIO().grant_path(offer) == "once"
    answers = iter(["n"])
    assert TerminalIO().grant_path(offer) == "denied"


def test_grants_keep_the_approvals_contract(tmp_path, outside):
    from zhiqiu.contracts import approvals_consistent
    store, model, io, agent = agent_with(
        tmp_path, [read(outside / "持久化.md", "a"), read(outside / "复制.md", "b"),
                   tool("write_file", {"path": str(outside / "n.md"), "content": "x"}, "w"), say("好")],
        "once", "denied", "once")
    agent.handle("读、读、写")
    result = approvals_consistent(store.db, 0, "")
    # Three answered requests, plus the approved write call that must have one.
    assert result["checked"] == 4 and result["failures"] == 0, result["evidence"]
