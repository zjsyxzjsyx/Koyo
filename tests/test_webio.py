"""The web workbench's FlowIO and worker thread, driven the way the browser will drive them."""
import sqlite3
import threading
import time
from datetime import date

import pytest

from zhiqiu.agent import ChatAgent
from zhiqiu.approvals import ApprovalPolicy
from zhiqiu.clock import FixedClock
from zhiqiu.flows import FlowInterrupted
from zhiqiu.runtime import LearningRuntime
from zhiqiu.webio import WebIO, Workbench

from test_agent import NOTE, ChatModel, say, tool
from test_toolloop import QuerySearch

PROMPT_KINDS = ("ask", "confirm", "approval")
OPEN = []


def bench(tmp_path, rounds, *, policy=None, write=False, make=None):
    """A started workbench whose agent is built on the worker thread; returns (bench, model, built)."""
    root = tmp_path / "notes"
    root.mkdir(parents=True, exist_ok=True)
    (root / "rbtree.md").write_text(NOTE, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    model = ChatModel(rounds)
    search = QuerySearch([])
    built = {}

    def make_agent(store, io):
        built["thread"] = threading.current_thread()
        built["agent"] = ChatAgent(store, LearningRuntime(store, model, search, search,
                                                          write_roots=[out] if write else ()),
                                   io, read_roots=[root], write_roots=[out] if write else (), policy=policy)
        return built["agent"]
    work = Workbench(tmp_path / "state.sqlite", make or make_agent, clock=FixedClock(date(2026, 10, 15)))
    OPEN.append(work)
    work.start()
    return work, model, built


def next_event(io, after, kinds, timeout=10):
    """The first event after `after` whose kind is in `kinds`, waiting for it to appear."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in io.events(after):
            if event["kind"] in kinds:
                return event
        time.sleep(0.01)
    raise AssertionError(f"no {kinds} event after {after}: {io.events(after)[-5:]}")


def drive(work, text, answers=(), approve=True):
    """Submit one input and answer its prompts like the page would, until the turn is idle."""
    answers = list(answers)
    after = work.io.last_seq
    assert work.submit(text)
    while True:
        event = next_event(work.io, after, PROMPT_KINDS + ("idle",))
        after = event["seq"]
        if event["kind"] == "idle":
            return work.io.events(0)
        value = answers.pop(0) if event["kind"] == "ask" else approve
        work.io.respond(event["request_id"], value)


def said(events):
    return [event["text"] for event in events if event["kind"] == "say"]


@pytest.fixture(autouse=True)
def closed():
    """Every workbench a test started is closed, and its worker thread must end."""
    yield
    while OPEN:
        assert OPEN.pop().close(), "the worker thread did not end"


def test_a_question_is_answered_on_the_worker_thread(tmp_path):
    work, model, built = bench(tmp_path, [tool("zhiqiu_status"), say("今天没有到期复习。")])
    assert built["thread"] is not threading.current_thread() and work.conversation_id
    events = drive(work, "今天要复习什么？")
    kinds = [event["kind"] for event in events]
    assert kinds[0] == "say" and "idle" in kinds  # the greeting, then idle before any input
    assert "今天没有到期复习。" in said(events)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    user = next(event for event in events if event["kind"] == "user")
    assert user["text"] == "今天要复习什么？" and not work.busy
    # Only new events come back after a given sequence number.
    last = events[-1]["seq"]
    assert work.io.events(last) == [] and work.io.events(last - 1)[0]["seq"] == last


def test_write_approvals_are_answered_from_the_page_and_recorded_as_web(tmp_path):
    work, model, built = bench(tmp_path, [
        tool("write_file", {"path": "a.md", "content": "笔记"}, "a"),
        tool("write_file", {"path": "b.md", "content": "笔记"}, "b"), say("写好了一份")], write=True)
    answers = iter([True, False])
    after = work.io.last_seq
    assert work.submit("写下来")
    for _ in range(2):
        event = next_event(work.io, after, ("approval",))
        after = event["seq"]
        assert event["tool"] == "write_file" and "写入文件" in event["text"] and event["approval_request_id"]
        assert work.busy and not work.submit("再来一句")  # one turn at a time
        work.io.respond(event["request_id"], next(answers))
    next_event(work.io, after, ("idle",))
    assert (tmp_path / "out" / "a.md").exists() and not (tmp_path / "out" / "b.md").exists()
    # The database belongs to the worker thread; read a copy of the rows through a fresh connection.
    db = sqlite3.connect(tmp_path / "state.sqlite")
    rows = db.execute("SELECT tool,status,decided_via FROM approval_requests ORDER BY created_at_ns").fetchall()
    assert rows == [("write_file", "approved", "web"), ("write_file", "denied", "web")]
    db.close()


def test_a_learning_flow_runs_entirely_through_the_page(tmp_path):
    work, model, built = bench(tmp_path, [tool("propose_learn", {"point": "红黑树", "path": "rbtree.md"})])
    events = drive(work, "我想学红黑树", answers=["/done", "染红", "旋转", "插入后修复"])
    prompts = [event.get("prompt") or event.get("text") for event in events if event["kind"] in PROMPT_KINDS]
    # One consent: accepting the proposal also covers sending the listed materials.
    assert "要开始吗？" in prompts and "允许本次学习发送这些内容吗？" not in prompts
    assert "发送资料已在开始前确认。" in said(events)
    assert any("完整评阅" in text for text in said(events))
    db = sqlite3.connect(tmp_path / "state.sqlite")
    assert db.execute("SELECT state FROM attempts").fetchone()[0] == "completed"
    assert db.execute("SELECT COUNT(*) FROM review_tasks").fetchone()[0] == 1
    db.close()


def test_answers_are_checked_and_unknown_prompts_refused():
    io = WebIO(wait_s=5)
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("value", io.confirm("继续吗？")))
    worker.start()
    event = next_event(io, 0, ("confirm",))
    with pytest.raises(ValueError, match="是或否"):
        io.respond(event["request_id"], "yes")
    with pytest.raises(LookupError):
        io.respond("nope", True)
    assert [item["request_id"] for item in io.pending()] == [event["request_id"]]  # still waiting
    io.respond(event["request_id"], True)
    worker.join(5)
    assert result["value"] is True and io.pending() == []
    with pytest.raises(LookupError):
        io.respond(event["request_id"], False)  # already answered
    resolved = io.events(event["seq"])[0]
    assert resolved["kind"] == "resolved" and resolved["reason"] == "answered" and resolved["value"] is True


def test_stop_interrupts_questions_and_says_no_to_confirmations():
    io = WebIO(wait_s=5)
    outcomes = {}

    def ask():
        try:
            io.ask("回答：")
        except FlowInterrupted:
            outcomes["ask"] = "interrupted"
    workers = [threading.Thread(target=ask),
               threading.Thread(target=lambda: outcomes.setdefault("confirm", io.confirm("开始吗？")))]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + 5
    while len(io.pending()) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert io.stop() == 2
    for worker in workers:
        worker.join(5)
    assert outcomes == {"ask": "interrupted", "confirm": False}
    assert {event["reason"] for event in io.events() if event["kind"] == "resolved"} == {"stopped"}


def test_a_prompt_nobody_answers_times_out_as_stopped():
    io = WebIO(wait_s=0.05)
    with pytest.raises(FlowInterrupted):
        io.ask("回答：")
    assert io.events()[-1]["reason"] == "timeout" and io.pending() == []


def test_stopping_an_approval_cancels_its_request_instead_of_denying_it(tmp_path):
    work, model, built = bench(tmp_path, [tool("web_search", {"query": "x"}, "a"), say("没搜到")],
                               policy=ApprovalPolicy({"network": "ask"}))
    after = work.io.last_seq
    assert work.submit("搜一下")
    next_event(work.io, after, ("approval",))
    work.io.stop()
    next_event(work.io, after, ("idle",))
    db = sqlite3.connect(tmp_path / "state.sqlite")
    assert db.execute("SELECT status,decided_via FROM approval_requests").fetchall() == [("cancelled", None)]
    assert db.execute("SELECT status,confirmed FROM tool_calls").fetchall() == [("denied", None)]
    db.close()


def test_stop_pauses_a_running_lesson_and_the_workbench_keeps_going(tmp_path):
    work, model, built = bench(tmp_path, [tool("propose_learn", {"point": "红黑树", "path": "rbtree.md"})])
    after = work.io.last_seq
    assert work.submit("我想学红黑树")
    # Accepting the proposal is the only confirmation; it also covers sending the material.
    event = next_event(work.io, after, ("confirm",))
    after = event["seq"]
    work.io.respond(event["request_id"], True)
    next_event(work.io, after, ("ask",))  # the lesson waits for the learner
    work.io.stop()
    next_event(work.io, after, ("idle",))
    model.rounds = [say("讲解已暂停，可以 /resume 继续。")]
    assert "讲解已暂停，可以 /resume 继续。" in said(drive(work, "刚才怎么了"))


def test_the_agent_and_its_connection_stay_on_the_worker_thread(tmp_path):
    work, model, built = bench(tmp_path, [])
    with pytest.raises(sqlite3.ProgrammingError):
        built["agent"].store.db.execute("SELECT 1")


def test_a_broken_turn_is_reported_and_the_next_one_still_runs(tmp_path):
    work, model, built = bench(tmp_path, [])
    original = model.tool_round
    calls = []

    def broken(*args, **kwargs):
        if not calls:
            calls.append(1)
            raise RuntimeError("模型适配器出了错")
        return original(*args, **kwargs)
    model.tool_round = broken
    events = drive(work, "第一句")
    assert any(event["kind"] == "error" and "模型适配器出了错" in event["text"] for event in events)
    assert "（默认回答）" in said(drive(work, "第二句"))


def test_startup_errors_reach_the_caller(tmp_path):
    def broken(store, io):
        raise ValueError("审批策略有误")
    work = Workbench(tmp_path / "state.sqlite", broken)
    with pytest.raises(ValueError, match="审批策略有误"):
        work.start()
    assert not work.submit("你好")


def test_close_ends_the_worker_even_while_a_prompt_waits(tmp_path):
    work, model, built = bench(tmp_path, [tool("propose_learn", {"point": "红黑树", "path": "rbtree.md"})])
    assert work.submit("我想学红黑树")
    next_event(work.io, 0, ("confirm",))
    assert work.close(timeout=5)
    assert not work.submit("还在吗")
    # The proposal prompt was given up on: no flow ran, and the conversation is saved.
    db = sqlite3.connect(tmp_path / "state.sqlite")
    assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    db.close()
