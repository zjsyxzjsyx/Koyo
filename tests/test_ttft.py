"""V8.0: schema v23 keeps when a streamed reply began; traces, metrics and a contract read it."""
import shutil
import sqlite3

from zhiqiu import contracts, metrics, traces, tracing
from zhiqiu.store import SCHEMA_VERSION, Store


def call(store, **extra):
    store.record_llm_call({"point": "红黑树", "model": "m", "prompt_hash": "h", "latency_ms": 900, "tokens_in": 10,
                           "tokens_out": 5, "status": "ok", "stage": "chat", **extra})


def test_a_v22_database_gains_the_columns_and_old_calls_read_as_not_streamed(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path)
    call(store)
    store.db.execute("ALTER TABLE llm_calls DROP COLUMN ttft_ms")
    store.db.execute("ALTER TABLE llm_calls DROP COLUMN streamed")
    store.db.execute("PRAGMA user_version=22")
    store.close()
    reopened = Store(path)
    try:
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 24
        assert (path.parent / "state.sqlite.pre-v23.bak").exists()
        row = reopened.db.execute("SELECT ttft_ms, streamed FROM llm_calls").fetchone()
        assert (row["ttft_ms"], row["streamed"]) == (None, 0)
    finally:
        reopened.close()


def test_streamed_calls_are_recorded_and_shown(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    try:
        with tracing.span(store, "turn", "chat"):
            call(store, ttft_ms=320, streamed=True)
            call(store)
        row = store.db.execute("SELECT * FROM llm_calls WHERE streamed=1").fetchone()
        assert row["ttft_ms"] == 320
        detail = traces.llm_detail(store.db, row["id"])
        assert detail["ttft_ms"] == 320 and detail["streamed"] == 1
        text = traces.render(traces.tree(store.db, row["trace_id"]))
        assert text.count("首字") == 1 and "首字 320ms" in text
    finally:
        store.close()


def test_first_token_percentiles_need_five_streamed_calls(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    try:
        for ttft in (100, 200, 300, 400):
            call(store, ttft_ms=ttft, streamed=True)
        call(store)  # not streamed: never counted
        data = metrics.collect(store.db, 1)
        assert data["model"]["ttft_samples"] == 4 and data["model"]["ttft_p50_ms"] is None
        assert "样本不足（4 次）" in metrics.render(data)
        call(store, ttft_ms=500, streamed=True)
        data = metrics.collect(store.db, 1)
        stage = data["model"]["by_stage"]["chat"]
        assert stage["ttft_samples"] == 5 and stage["ttft_p50_ms"] is not None and stage["ttft_p95_ms"] >= 400
        assert data["model"]["streamed"] == 5
    finally:
        store.close()


def test_the_stream_contract_catches_a_missing_or_impossible_first_token(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    try:
        result = lambda: next(item for item in contracts.check(store.db, 1)["results"] if item["check"] == "stream_consistent")
        assert result()["status"] == contracts.INSUFFICIENT
        call(store, ttft_ms=300, streamed=True)
        assert result()["status"] == contracts.PASS
        call(store, ttft_ms=None, streamed=True)
        call(store, ttft_ms=5000, streamed=True)
        assert result()["status"] == contracts.FAIL and result()["failures"] == 2
    finally:
        store.close()
