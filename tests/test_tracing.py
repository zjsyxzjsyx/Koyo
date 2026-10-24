import json
import sqlite3
from datetime import date

import pytest
from typer.testing import CliRunner

from zhiqiu import tracing, traces
from zhiqiu.cli import app
from zhiqiu.clock import FixedClock
from zhiqiu.context import ContextTask
from zhiqiu.flows import FlowInterrupted, run_lesson
from zhiqiu.runtime import LearningRuntime
from zhiqiu.store import SCHEMA_VERSION, Store

from test_agent import make_agent, say, tool
from test_flows import ScriptedIO
from test_runtime import FakeModel


def spans(store):
    return {row["span_id"]: dict(row) for row in store.db.execute("SELECT * FROM spans ORDER BY rowid")}


class AuditingModel(FakeModel):
    """FakeModel that leaves an llm_calls row per stage, as the real adapter's audit does."""

    def __init__(self, store_ref):
        super().__init__()
        self.store_ref = store_ref

    def _audit(self, stage):
        self.store_ref[0].record_llm_call({"point": "二叉树遍历", "model": "fake", "prompt_hash": "h",
                                           "latency_ms": 5, "tokens_in": 100, "tokens_out": 20,
                                           "tokens_cached": 40, "status": "ok", "stage": stage,
                                           "started_at_ns": tracing.now_ns()})

    def assess(self, point, sources):
        self._audit("assess")
        return super().assess(point, sources)

    def teach(self, point, sources):
        self._audit("teach")
        return super().teach(point, sources)

    def next_question(self, point, context, sources, focus):
        self._audit("next_question")
        return super().next_question(point, context, sources, focus)

    def grade(self, point, context, sources):
        self._audit("grade")
        return super().grade(point, context, sources)


def test_spans_nest_inherit_links_and_are_written_when_they_start(tmp_path):
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 14)))
    with tracing.span(store, "command", "learn", point="红黑树") as root:
        row = store.db.execute("SELECT status FROM spans WHERE span_id=?", (root.span_id,)).fetchone()
        assert row["status"] == "running"  # visible to a viewer while it runs
        with tracing.span(store, "stage", "teach", kp_id="kp-1") as child:
            child.progress(step="讲解", question=2)
            assert tracing.columns(store) == (root.trace_id, child.span_id)
        root.link(kp_id="kp-1")
        root.set(end_reason="done")
    rows = spans(store)
    assert rows[child.span_id]["trace_id"] == root.trace_id and rows[child.span_id]["parent_span_id"] == root.span_id
    assert rows[child.span_id]["point"] == "红黑树"  # inherited from the parent
    assert json.loads(rows[child.span_id]["progress_json"]) == {"step": "讲解", "question": 2}
    assert rows[root.span_id]["status"] == "ok" and rows[root.span_id]["end_reason"] == "done"
    assert rows[root.span_id]["kp_id"] == "kp-1" and rows[root.span_id]["duration_ms"] is not None
    assert tracing.columns(store) == (None, None)


@pytest.mark.parametrize("exc, status, kind", [(ValueError("x"), "error", "ValueError"),
                                                (KeyboardInterrupt(), "interrupted", None),
                                                (FlowInterrupted(), "interrupted", None)])
def test_a_span_records_how_it_ended_and_lets_the_exception_through(tmp_path, exc, status, kind):
    store = Store(tmp_path / "state.sqlite")
    with pytest.raises(type(exc)):
        with tracing.span(store, "turn", "chat"):
            raise exc
    row = store.db.execute("SELECT status,error_kind FROM spans").fetchone()
    assert (row["status"], row["error_kind"]) == (status, kind)


def test_audit_rows_carry_the_current_trace_and_nothing_outside_a_span(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    details = {"point": "p", "model": "m", "prompt_hash": "h", "latency_ms": 1, "status": "ok"}
    store.record_llm_call(details)
    with tracing.span(store, "turn", "chat") as span:
        store.record_llm_call(details)
        store.record_event(None, None, "Probe", {})
        store.record_tool_call({"id": "t1", "tool": "zhiqiu_status", "status": "ok", "attempts": 1,
                                "latency_ms": 1, "output_chars": 0})
    linked = [tuple(row) for row in store.db.execute("SELECT trace_id,parent_span_id FROM llm_calls ORDER BY rowid")]
    assert linked == [(None, None), (span.trace_id, span.span_id)]
    for table in ("events", "tool_calls"):
        row = store.db.execute(f"SELECT trace_id,parent_span_id FROM {table}").fetchone()
        assert tuple(row) == (span.trace_id, span.span_id)


def test_a_learning_flow_is_one_tree_with_every_model_call_attached(tmp_path):
    ref = []
    path = tmp_path / "a.md"
    path.write_text("二叉树遍历的概念、机制和应用。", encoding="utf-8")
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 2)))
    ref.append(store)
    service = LearningRuntime(store, AuditingModel(ref))
    result = run_lesson(ScriptedIO("/done", "根左右", "递归栈", "表达式树"), store, service, "二叉树遍历", source=path)
    assert result.kind == "outcome"
    rows = spans(store)
    roots = [row for row in rows.values() if row["parent_span_id"] is None]
    assert len(roots) == 1 and roots[0]["name"] == "learn" and roots[0]["end_reason"] == "outcome"
    assert roots[0]["kp_id"] == result.kp_id
    quiz = next(row for row in rows.values() if row["name"] == "quiz")
    assert quiz["parent_span_id"] == roots[0]["span_id"] and quiz["attempt_id"] == result.attempt_id
    stages = [row["name"] for row in rows.values() if row["kind"] == "stage"]
    assert {"assess", "teach", "next_question", "grade"} <= set(stages)
    assert all(row["status"] == "ok" for row in rows.values())
    for table in ("llm_calls", "context_builds"):
        for row in store.db.execute(f"SELECT trace_id,parent_span_id FROM {table}"):
            assert row["trace_id"] == roots[0]["trace_id"] and row["parent_span_id"] in rows
    grade_span = next(row for row in rows.values() if row["name"] == "grade")
    assert store.db.execute("SELECT parent_span_id FROM llm_calls WHERE stage='grade'").fetchone()[0] == grade_span["span_id"]
    data = traces.tree(store.db, roots[0]["trace_id"])
    assert data["roots"][0]["totals"]["llm_calls"] == store.db.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    text = traces.render(data)
    assert "[flow] learn" in text and "[flow] quiz" in text and "模型 grade" in text


def test_a_chat_turn_is_one_trace_with_its_tool_loop(tmp_path):
    store, model, io, agent = make_agent(tmp_path, [tool("zhiqiu_status"), say("今天没有到期复习。")])
    agent.handle("今天要复习什么？")
    rows = list(spans(store).values())
    turn = next(row for row in rows if row["kind"] == "turn")
    loop = next(row for row in rows if row["kind"] == "loop")
    assert turn["name"] == "chat" and turn["conversation_id"] == agent.conversation_id
    assert turn["end_reason"] == "done" and loop["parent_span_id"] == turn["span_id"]
    attrs = json.loads(loop["attrs_json"])
    assert attrs["rounds"] == 2 and loop["end_reason"] == "done"
    call = store.db.execute("SELECT loop_id,trace_id,parent_span_id FROM tool_calls").fetchone()
    assert call["loop_id"] == attrs["loop_id"] and call["parent_span_id"] == loop["span_id"]
    assert json.loads(loop["progress_json"])["round"] == 2


def test_spans_left_running_by_a_dead_process_become_interrupted(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    now = tracing.now_ns()
    for span_id, pid in (("dead", 2_000_000_000), ("alive", 0)):
        store.db.execute("""INSERT INTO spans(span_id,trace_id,kind,name,started_at_ns,status,attrs_json)
            VALUES(?,?,?,?,?,'running',?)""", (span_id, span_id * 4, "command", "learn", now,
                                               json.dumps({"pid": pid or __import__("os").getpid()})))
    assert [item["trace_id"] for item in traces.live(store.db)] == ["alive" * 4]
    assert tracing.reconcile(store) == 1
    status = dict(store.db.execute("SELECT span_id,status FROM spans").fetchall())
    assert status == {"dead": "interrupted", "alive": "running"}


def test_an_unparsable_answer_marks_the_model_call_invalid_output(tmp_path):
    store = Store(tmp_path / "state.sqlite")

    class BrokenModel(FakeModel):
        def request(self, request):
            store.record_llm_call({"point": "", "model": "m", "prompt_hash": "h", "latency_ms": 1, "status": "ok",
                                   "context_build_id": request.build_id, "stage": request.stage})
            raise ValueError("模型返回了无效 JSON，当前业务状态未结案")

    service = LearningRuntime(store, BrokenModel())
    with pytest.raises(ValueError):
        service._invoke(ContextTask("assess", "二叉树"), lambda: {})
    row = store.db.execute("SELECT status,error_kind,trace_id FROM llm_calls").fetchone()
    assert (row["status"], row["error_kind"]) == ("ok", "invalid_output")
    stage = store.db.execute("SELECT name,status,error_kind FROM spans").fetchone()
    assert tuple(stage) == ("assess", "error", "ValueError") and row["trace_id"]


def test_v15_database_gains_spans_and_trace_columns_with_backup(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 14)))
    kp = store.ensure_knowledge_point("红黑树")
    for index in ("spans_trace", "spans_started", "spans_status", "llm_calls_trace", "tool_calls_trace",
                  "events_trace", "llm_failures_trace"):
        store.db.execute(f"DROP INDEX {index}")
    store.db.execute("DROP TABLE spans")
    store.db.execute("DROP TABLE llm_failures")
    for table, columns in (("llm_calls", ("trace_id", "parent_span_id", "started_at_ns", "tokens_cached", "error_kind")),
                           ("events", ("trace_id", "parent_span_id", "at_ns"))):
        for column in columns:
            store.db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    store.db.execute("PRAGMA user_version=15")
    store.close()
    reopened = Store(path, FixedClock(date(2026, 10, 14)))
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 26
    assert (tmp_path / "state.sqlite.pre-v16.bak").exists()
    assert reopened.get_knowledge_point("红黑树")["id"] == kp
    columns = {row["name"] for row in reopened.db.execute("PRAGMA table_info(llm_calls)")}
    assert {"trace_id", "parent_span_id", "started_at_ns", "tokens_cached", "error_kind"} <= columns
    assert reopened.db.execute("SELECT COUNT(*) FROM spans").fetchone()[0] == 0


def test_forget_and_export_cover_spans(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    kp = store.ensure_knowledge_point("红黑树")
    with tracing.span(store, "flow", "learn", kp_id=kp, point="红黑树"):
        pass
    with tracing.span(store, "turn", "chat"):
        pass
    assert len(store.export_json()["spans"]) == 2
    store.forget(kp)
    assert [row["name"] for row in store.db.execute("SELECT name FROM spans")] == ["chat"]


def test_traces_and_timeline_commands(tmp_path):
    db = tmp_path / "state.sqlite"
    ref = []
    path = tmp_path / "a.md"
    path.write_text("二叉树遍历的概念、机制和应用。", encoding="utf-8")
    store = Store(db, FixedClock(date(2026, 10, 2)))
    ref.append(store)
    result = run_lesson(ScriptedIO("/done", "根左右", "递归栈", "表达式树"), store,
                        LearningRuntime(store, AuditingModel(ref)), "二叉树遍历", source=path)
    store.close()
    runner = CliRunner()
    listed = runner.invoke(app, ["--db", str(db), "traces"])
    assert listed.exit_code == 0 and "flow:learn" in listed.output
    items = json.loads(runner.invoke(app, ["--db", str(db), "traces", "--json"]).output)
    assert items[0]["llm_calls"] > 0 and items[0]["tokens_cached"] > 0
    timeline = runner.invoke(app, ["--db", str(db), "trace", result.attempt_id, "--timeline"])
    assert timeline.exit_code == 0 and "[flow] quiz" in timeline.output
    plain = runner.invoke(app, ["--db", str(db), "trace", result.attempt_id])
    assert json.loads(plain.output[plain.output.index("{"):])["attempt"]["id"] == result.attempt_id
