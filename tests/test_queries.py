from datetime import date
import hashlib
import sqlite3

import pytest

from zhiqiu import queries
from zhiqiu.clock import FixedClock
from zhiqiu.queries import open_readonly, query_tool_specs, readonly_runner
from zhiqiu.store import Store
from zhiqiu.tools import ToolError, ToolExecutor, ToolRegistry, ToolScope

from test_runtime import complete_first, prepare_service

READ = ToolScope(frozenset({"read"}))


def finished_database(tmp_path):
    store, service, path = prepare_service(tmp_path)
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    outcome = complete_first(store, service, kp)
    store.close()
    return tmp_path / "state.sqlite", kp, outcome


def readonly_executor(db_path, today="2026-10-02"):
    registry = ToolRegistry()
    for spec in query_tool_specs(readonly_runner(db_path), lambda: today):
        registry.register(spec)
    return ToolExecutor(registry)


def fingerprint(path):
    files = sorted(p.name for p in path.parent.iterdir())
    return hashlib.sha256(path.read_bytes()).hexdigest(), files


def test_query_tools_never_change_the_database(tmp_path):
    db_path, kp, outcome = finished_database(tmp_path)
    before = fingerprint(db_path)
    executor = readonly_executor(db_path, today=outcome["due_date"])
    status = executor.run("zhiqiu_status", {}, READ).value
    assert [task["id"] for task in status["due"]] == [outcome["task_id"]]
    assert executor.run("zhiqiu_due", {"until": "2027-01-01"}, READ).value[0]["title"] == "二叉树遍历"
    point = executor.run("zhiqiu_knowledge_point", {"point": "二叉树遍历"}, READ).value
    assert point["knowledge_point"]["id"] == kp
    assert all("content" not in item for item in point["materials"]) and point["materials"][0]["chars"] > 0
    assert point["attempts"][0]["total"] == 78 and len(point["attempts"][0]["dimension_scores"]) == 3
    trace = executor.run("zhiqiu_trace", {"attempt_id": outcome["attempt_id"]}, READ).value
    assert trace["attempt"]["id"] == outcome["attempt_id"]
    assert fingerprint(db_path) == before
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 13


def test_readonly_connection_cannot_write(tmp_path):
    db_path, _, _ = finished_database(tmp_path)
    db = open_readonly(db_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.execute("DELETE FROM knowledge_points")
    db.close()


def test_missing_or_old_database_is_refused_without_creating_or_migrating(tmp_path):
    missing = tmp_path / "nothing" / "state.sqlite"
    with pytest.raises(ValueError, match="不存在"):
        open_readonly(missing)
    assert not missing.parent.exists()
    db_path, _, _ = finished_database(tmp_path)
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version=12")
    before = fingerprint(db_path)
    with pytest.raises(ToolError) as error:
        readonly_executor(db_path).run("zhiqiu_status", {}, READ)
    assert error.value.kind == "not_found" and "版本为 12" in str(error.value)
    assert fingerprint(db_path) == before


def test_bad_arguments_and_unknown_records(tmp_path):
    db_path, _, _ = finished_database(tmp_path)
    executor = readonly_executor(db_path)
    for name, args, kind in [("zhiqiu_due", {"until": "明天"}, "invalid_args"),
                             ("zhiqiu_due", {"until": "2026-13-01"}, "invalid_args"),
                             ("zhiqiu_knowledge_point", {"point": "不存在的知识点"}, "not_found"),
                             ("zhiqiu_trace", {"attempt_id": "missing"}, "not_found")]:
        with pytest.raises(ToolError) as error:
            executor.run(name, args, READ)
        assert error.value.kind == kind, name


def test_large_trace_output_is_capped(tmp_path):
    db_path, _, outcome = finished_database(tmp_path)
    store = Store(db_path, FixedClock(date(2026, 10, 2)))
    with store.transaction():
        store.db.execute("UPDATE answers SET text=?", ("很长的回答" * 3000,))
    store.close()
    result = readonly_executor(db_path).run("zhiqiu_trace", {"attempt_id": outcome["attempt_id"]}, READ)
    assert result.truncated and isinstance(result.value, str) and len(result.value) == 20_000


def test_store_and_readonly_queries_share_one_sql(tmp_path):
    db_path, _, outcome = finished_database(tmp_path)
    store = Store(db_path, FixedClock(date.fromisoformat(outcome["due_date"])))
    db = open_readonly(db_path)
    assert store.status() == queries.status(db, outcome["due_date"])
    assert store.trace(outcome["attempt_id"]) == queries.trace(db, outcome["attempt_id"])
    db.close()
    store.close()
