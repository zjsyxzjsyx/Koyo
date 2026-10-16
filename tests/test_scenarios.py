"""Deterministic end-to-end scenarios (Eval layer A).

Each scenario runs a real flow with scripted models, no network and no real model.
Afterwards every run contract must hold (pass or insufficient, never fail) and the
trace must be one connected tree: this is the first stage of `zhiqiu gate`.
"""
import json
from datetime import date

import pytest

from zhiqiu import contracts, tracing, traces
from zhiqiu.agent import ChatAgent
from zhiqiu.approvals import ApprovalPolicy
from zhiqiu.clock import FixedClock
from zhiqiu.flows import run_lesson, run_quiz
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import Store

from test_agent import ChatModel, NOTE, say, tool
from test_flows import ScriptedIO
from test_side_questions import SIDE
from test_runtime import FakeModel
from test_toolloop import QuerySearch


class Audited:
    """Mixin: every scripted model answer leaves an llm_calls row, as the real adapter's audit does."""

    store = None

    def _audit(self, stage):
        if self.store is not None:
            self.store.record_llm_call({"point": "", "model": "scripted", "prompt_hash": stage, "latency_ms": 3,
                                        "tokens_in": 120, "tokens_out": 30, "status": "ok", "stage": stage,
                                        "started_at_ns": tracing.now_ns()})

    def assess(self, *args):
        self._audit("assess")
        return super().assess(*args)

    def teach(self, *args):
        self._audit("teach")
        return super().teach(*args)

    def next_question(self, *args):
        self._audit("next_question")
        return super().next_question(*args)

    def grade(self, *args):
        self._audit("grade")
        return super().grade(*args)

    def clarify(self, *args):
        self._audit("clarify")
        return super().clarify(*args)


class LearnModel(Audited, FakeModel):
    def __init__(self, labels=None):
        super().__init__()
        self.labels = labels or {}

    def classify_turn(self, question, text):
        self._audit("classify_turn")
        return {"kind": self.labels.get(text, "answer"), "reason": "脚本"}


class AuditedChatModel(Audited, ChatModel):
    def tool_round(self, request, messages, tools, tool_choice="auto", timeout_s=60):
        self._audit(request.stage)
        return super().tool_round(request, messages, tools, tool_choice, timeout_s)


def assert_run_rules_hold(store, roots):
    """No contract fails, and every audit row hangs on an existing span of a connected tree."""
    data = contracts.check(store.db, 1)
    failed = [item for item in data["results"] if item["status"] == "fail"]
    assert not failed, json.dumps(failed, ensure_ascii=False, indent=2)
    spans = {row["span_id"]: dict(row) for row in store.db.execute("SELECT * FROM spans")}
    assert sorted(row["kind"] + ":" + row["name"] for row in spans.values() if not row["parent_span_id"]) == sorted(roots)
    for row in spans.values():
        assert row["status"] != "running"
        if row["parent_span_id"]:
            parent = spans[row["parent_span_id"]]
            assert parent["trace_id"] == row["trace_id"]
    for table in ("llm_calls", "tool_calls", "context_builds"):
        for row in store.db.execute(f"SELECT trace_id,parent_span_id FROM {table}"):
            assert row["parent_span_id"] in spans and spans[row["parent_span_id"]]["trace_id"] == row["trace_id"]
    for trace_id in {row["trace_id"] for row in spans.values()}:
        traces.render(traces.tree(store.db, trace_id))  # every trace renders
    return data


def lesson_setup(tmp_path, model):
    path = tmp_path / "a.md"
    path.write_text("二叉树遍历的概念、机制和应用。", encoding="utf-8")
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    model.store = store
    return store, LearningRuntime(store, model), path


def chat_setup(tmp_path, rounds, *inputs, confirm=True, search=None, policy=None):
    root = tmp_path / "notes"
    root.mkdir(parents=True, exist_ok=True)
    (root / "rbtree.md").write_text(NOTE, encoding="utf-8")
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 13)))
    model = AuditedChatModel(rounds)
    model.store = store
    service = LearningRuntime(store, model, search, search)
    agent = ChatAgent(store, service, ScriptedIO(*inputs, confirm=confirm), read_roots=[root], policy=policy)
    agent.start()
    return store, agent, root


def guard_rules(store):
    return [json.loads(row[0])["rule"] for row in store.db.execute(
        "SELECT payload_json FROM events WHERE type='GuardrailTriggered' ORDER BY id")]


def test_scenario_learn_quiz_and_grade(tmp_path):
    store, service, path = lesson_setup(tmp_path, LearnModel())
    result = run_lesson(ScriptedIO("/done", "根左右", "递归栈", "表达式树"), store, service, "二叉树遍历", source=path)
    assert result.outcome["kind"] == "complete"
    data = assert_run_rules_hold(store, ["flow:learn"])
    assert {item["check"]: item["status"] for item in data["results"]}["state_replay"] == "pass"
    stages = [row[0] for row in store.db.execute("SELECT stage FROM llm_calls ORDER BY rowid")]
    assert stages[:2] == ["assess", "teach"] and stages[-1] == "grade"


def test_scenario_chat_answers_with_a_tool(tmp_path):
    store, agent, _ = chat_setup(tmp_path, [tool("zhiqiu_status"), say("今天没有到期复习。")])
    agent.handle("今天要复习什么？")
    data = assert_run_rules_hold(store, ["turn:chat"])
    statuses = {item["check"]: item["status"] for item in data["results"]}
    assert statuses["loop_rounds"] == "pass" and statuses["trace_linked"] == "pass"


def test_scenario_proposal_confirmed_runs_the_flow_and_returns_to_chat(tmp_path):
    note = str(tmp_path / "notes" / "rbtree.md")
    store, agent, _ = chat_setup(tmp_path, [tool("propose_learn", {"point": "红黑树", "path": note})],
                                 "/done", "先染红", "旋转变色", "插入修复")
    agent.handle("我想学红黑树")
    data = assert_run_rules_hold(store, ["turn:chat"])
    statuses = {item["check"]: item["status"] for item in data["results"]}
    assert statuses["proposal_stops_round"] == "pass" and statuses["state_replay"] == "pass"
    turn = store.db.execute("SELECT span_id FROM spans WHERE kind='turn'").fetchone()[0]
    learn = store.db.execute("SELECT parent_span_id FROM spans WHERE name='learn'").fetchone()[0]
    assert learn == turn  # the flow is part of the chat turn that accepted it
    modes = [json.loads(row[0])["to"] for row in store.db.execute("""SELECT payload_json FROM events
        WHERE type='StateChanged' AND json_extract(payload_json,'$.entity')='conversation' ORDER BY id""")]
    assert modes == ["chat", "lesson", "self_test", "chat"]


def test_scenario_side_question_in_a_quiz_is_not_graded(tmp_path):
    model = LearnModel({SIDE: "question"})
    store, service, path = lesson_setup(tmp_path, model)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    attempt = store.start_attempt(kp, "first")
    result = run_quiz(ScriptedIO(SIDE, "", "根左右", "递归", "表达式"), store, service, attempt)
    assert result.outcome["kind"] == "complete"
    assert_run_rules_hold(store, ["stage:assess", "stage:teach", "flow:quiz"])
    quiz = store.db.execute("SELECT span_id FROM spans WHERE name='quiz'").fetchone()[0]
    clarify = store.db.execute("SELECT parent_span_id FROM spans WHERE name='clarify'").fetchone()[0]
    assert clarify == quiz


def test_scenario_leak_is_blocked_and_a_refused_search_is_never_sent(tmp_path):
    search = QuerySearch([])
    store, agent, root = chat_setup(tmp_path, [
        tool("read_file", {"path": "rbtree.md"}),
        tool("web_search", {"query": NOTE[:20]}),
        tool("web_search", {"query": "红黑树 插入"}),
        say("按笔记回答：先染红再旋转。")], confirm=False, search=search,
        policy=ApprovalPolicy({"network": "ask"}))
    agent.handle("红黑树插入怎么做？")
    assert search.queries == []
    assert guard_rules(store) == ["leak_check", "confirmation_denied"]
    data = assert_run_rules_hold(store, ["turn:chat"])
    assert {item["check"]: item["status"] for item in data["results"]}["confirmed_side_effects"] == "insufficient"


class InterruptingModel(AuditedChatModel):
    def tool_round(self, request, messages, tools, tool_choice="auto", timeout_s=60):
        if not getattr(self, "interrupted", False):
            self.interrupted = True
            raise KeyboardInterrupt
        return super().tool_round(request, messages, tools, tool_choice, timeout_s)


def test_scenario_interruption_and_recovery(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 13)))
    model = InterruptingModel([say("现在可以回答了。")])
    model.store = store
    agent = ChatAgent(store, LearningRuntime(store, model), ScriptedIO(), read_roots=[root])
    agent.start()
    agent.handle("第一轮")  # Ctrl+C during the model call: this turn is cancelled
    agent.handle("第二轮")
    turns = [tuple(row) for row in store.db.execute("SELECT status,end_reason FROM spans WHERE kind='turn' ORDER BY rowid")]
    assert turns == [("ok", "cancelled"), ("ok", "done")]
    loops = [row[0] for row in store.db.execute("SELECT status FROM spans WHERE kind='loop' ORDER BY rowid")]
    assert loops == ["interrupted", "ok"]
    # A crashed process leaves a running span; the next start closes it and the rules hold again.
    store.db.execute("""INSERT INTO spans(span_id,trace_id,kind,name,started_at_ns,status,attrs_json)
        VALUES('crashed','C','command','learn',?,'running','{"pid": 2000000000}')""", (tracing.now_ns(),))
    assert not contracts.check(store.db, 1)["passed"]
    tracing.reconcile(store)
    assert_run_rules_hold(store, ["turn:chat", "turn:chat", "command:learn"])
    failures = {item["category"] for item in contracts.failures(store.db, 1)["categories"]}
    assert "flow_interrupted" in failures


# ----- V6: approvals and the web workbench --------------------------------------------------


def statuses(data):
    return {item["check"]: item["status"] for item in data["results"]}


def approval_rows(store):
    return [(row[0], row[1], row[2]) for row in store.db.execute(
        "SELECT tool,status,decided_via FROM approval_requests ORDER BY created_at_ns")]


def test_scenario_default_policy_searches_freely_and_asks_before_writing(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 16)))
    model = AuditedChatModel([tool("web_search", {"query": "杭州 天气"}, "a"),
                              tool("write_file", {"path": "天气.md", "content": "小雨"}, "b"), say("记好了")])
    model.store = store
    search = QuerySearch([])
    agent = ChatAgent(store, LearningRuntime(store, model, search, search, write_roots=[out]), ScriptedIO(),
                      read_roots=[root], write_roots=[out])
    agent.start()
    agent.handle("查天气并记下来")
    data = assert_run_rules_hold(store, ["turn:chat"])
    assert statuses(data)["confirmed_side_effects"] == "pass" and statuses(data)["approvals_consistent"] == "pass"
    sources = [tuple(row) for row in store.db.execute("SELECT tool,approval_source FROM tool_calls ORDER BY rowid")]
    assert sources == [("web_search", "policy"), ("write_file", "user")]
    assert approval_rows(store) == [("write_file", "approved", "terminal")]
    assert (out / "天气.md").exists()


def test_scenario_a_denied_tool_is_withheld_and_refused(tmp_path):
    store, agent, _ = chat_setup(tmp_path, [tool("fetch_url", {"url": "https://example.org"}), say("没打开")],
                                 search=QuerySearch([]), policy=ApprovalPolicy(tools={"fetch_url": "deny"}))
    agent.handle("打开这个网页")
    data = assert_run_rules_hold(store, ["turn:chat"])
    assert guard_rules(store) == ["policy_denied"]
    assert statuses(data)["confirmed_side_effects"] == "insufficient"  # nothing outgoing ran


def workbench_setup(tmp_path, rounds, write=False):
    from zhiqiu.webio import Workbench
    root = tmp_path / "notes"
    root.mkdir()
    (root / "rbtree.md").write_text(NOTE, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    model = AuditedChatModel(rounds)
    search = QuerySearch([])

    def make_agent(store, io):
        model.store = store  # audits are written on the worker thread, like the real adapter's
        return ChatAgent(store, LearningRuntime(store, model, search, search, write_roots=[out] if write else ()),
                         io, read_roots=[root], write_roots=[out] if write else ())
    bench = Workbench(tmp_path / "state.sqlite", make_agent, clock=FixedClock(date(2026, 10, 16)))
    bench.start()
    return bench, out


def test_scenario_a_write_refused_on_the_web_page(tmp_path):
    from test_webio import drive
    bench, out = workbench_setup(tmp_path, [tool("write_file", {"path": "a.md", "content": "笔记"}, "a"),
                                            say("那就不写了")], write=True)
    try:
        drive(bench, "写下来", approve=False)
    finally:
        assert bench.close()
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 16)))
    data = assert_run_rules_hold(store, ["turn:chat"])
    assert statuses(data)["approvals_consistent"] == "pass"
    assert approval_rows(store) == [("write_file", "denied", "web")]
    assert guard_rules(store) == ["confirmation_denied"] and not (out / "a.md").exists()


def test_scenario_a_lesson_and_quiz_taken_on_the_web_page(tmp_path):
    from test_webio import drive
    bench, _ = workbench_setup(tmp_path, [tool("propose_learn", {"point": "红黑树", "path": "rbtree.md"})])
    try:
        drive(bench, "我想学红黑树", answers=["/done", "先染红", "旋转变色", "插入修复"])
    finally:
        assert bench.close()
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 16)))
    data = assert_run_rules_hold(store, ["turn:chat"])
    assert statuses(data)["state_replay"] == "pass" and statuses(data)["proposal_stops_round"] == "pass"
    assert store.db.execute("SELECT state FROM attempts").fetchone()[0] == "completed"
    modes = [json.loads(row[0])["to"] for row in store.db.execute("""SELECT payload_json FROM events
        WHERE type='StateChanged' AND json_extract(payload_json,'$.entity')='conversation' ORDER BY id""")]
    assert modes == ["chat", "lesson", "self_test", "chat"]