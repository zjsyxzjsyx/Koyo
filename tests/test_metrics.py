import json
from datetime import date

import pytest
from typer.testing import CliRunner

from zhiqiu import metrics, pricing, tracing
from zhiqiu.cli import app
from zhiqiu.clock import FixedClock
from zhiqiu.model import OpenAICompatibleModel, cached_tokens
from zhiqiu.store import Store

from test_adapters import FakeClient, FakeResponse


def llm(store, stage, latency, tokens_in=100, tokens_out=10, cached=0, status="ok", error_kind=None):
    store.record_llm_call({"point": "p", "model": "m", "prompt_hash": "h", "latency_ms": latency,
                           "tokens_in": tokens_in, "tokens_out": tokens_out, "tokens_cached": cached,
                           "status": status, "error_kind": error_kind, "stage": stage,
                           "started_at_ns": tracing.now_ns()})


def tool(store, name, status, confirmed=None, attempts=1, source=None):
    store.record_tool_call({"id": f"{name}-{status}-{attempts}-{confirmed}-{source}", "tool": name, "status": status,
                            "confirmed": confirmed, "attempts": attempts, "latency_ms": 3, "output_chars": 0,
                            "started_at_ns": tracing.now_ns(), "approval_source": source})


def test_cached_tokens_reads_deepseek_and_openai_usage():
    assert cached_tokens({"prompt_cache_hit_tokens": 64}) == 64
    assert cached_tokens({"prompt_tokens_details": {"cached_tokens": 32}}) == 32
    assert cached_tokens({"prompt_tokens": 10}) is None


def test_the_adapter_records_cached_tokens_and_start_time(monkeypatch):
    class CachedClient(FakeClient):
        def post(self, url, **kwargs):
            return FakeResponse({"choices": [{"message": {"content": "{}"}}],
                                 "usage": {"prompt_tokens": 50, "completion_tokens": 5, "prompt_cache_hit_tokens": 30}})

    monkeypatch.setattr("zhiqiu.model.httpx.Client", CachedClient)
    calls = []
    OpenAICompatibleModel("https://model.example/v1", "m", "k", calls.append)._json("x", {})
    assert calls[0]["tokens_cached"] == 30 and calls[0]["started_at_ns"] > 0 and calls[0]["error_kind"] is None


def test_the_adapter_records_the_error_kind_of_a_failed_call(monkeypatch):
    class BrokenClient(FakeClient):
        def post(self, url, **kwargs):
            raise OSError("connection reset")

    monkeypatch.setattr("zhiqiu.model.httpx.Client", BrokenClient)
    calls = []
    with pytest.raises(OSError):
        OpenAICompatibleModel("https://model.example/v1", "m", "k", calls.append)._json("x", {})
    assert calls[0]["status"] == "error" and calls[0]["error_kind"] == "OSError"


def test_cost_needs_both_prices_and_bills_cache_hits_separately(monkeypatch):
    for name in ("ZHIQIU_PRICE_INPUT", "ZHIQIU_PRICE_OUTPUT", "ZHIQIU_PRICE_CACHED_INPUT"):
        monkeypatch.delenv(name, raising=False)
    assert pricing.cost(1_000_000, 1_000_000) is None
    monkeypatch.setenv("ZHIQIU_PRICE_INPUT", "2")
    monkeypatch.setenv("ZHIQIU_PRICE_OUTPUT", "8")
    assert pricing.cost(1_000_000, 500_000) == 6.0
    assert pricing.cost(1_000_000, 0, 400_000) == 2.0  # cache price defaults to the input price
    monkeypatch.setenv("ZHIQIU_PRICE_CACHED_INPUT", "0.5")
    assert pricing.cost(1_000_000, 0, 400_000) == pytest.approx(1.4)


def test_collect_aggregates_models_tools_loops_humans_and_guardrails(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIQIU_PRICE_INPUT", "2")
    monkeypatch.setenv("ZHIQIU_PRICE_OUTPUT", "8")
    monkeypatch.delenv("ZHIQIU_PRICE_CACHED_INPUT", raising=False)
    store = Store(tmp_path / "state.sqlite", FixedClock(date(2026, 10, 14)))
    with tracing.span(store, "turn", "chat") as turn:
        for latency in (100, 200, 300, 400):
            llm(store, "grade", latency, cached=50)
        llm(store, "next_question", 50, error_kind="invalid_output")
        llm(store, "next_question", 70, status="error", error_kind="ReadTimeout", tokens_in=None, tokens_out=None)
        tool(store, "web_search", "ok", confirmed=True)
        tool(store, "web_search", "ok", confirmed=True, attempts=2)
        tool(store, "web_search", "denied")
        tool(store, "zhiqiu_status", "ok")
        # Approved by the policy, and by an earlier answer in the same turn: neither was asked.
        tool(store, "fetch_url", "ok", confirmed=True, source="policy")
        tool(store, "fetch_url", "ok", confirmed=True, source="turn")
        store.record_event(None, None, "GuardrailTriggered", {"rule": "confirmation_denied", "action": "denied_by_user"})
        store.record_event(None, None, "GuardrailTriggered", {"rule": "leak_check", "action": "blocked"})
        store.record_event(None, None, "ReviewRevised", {})
        with tracing.span(store, "loop", "chat") as loop:
            loop.set(end_reason="max_rounds")
        turn.set(end_reason="done")
    data = metrics.collect(store.db, 7)
    model = data["model"]
    assert model["calls"] == 6 and model["errors"] == 1 and model["invalid_output"] == 1
    grade = model["by_stage"]["grade"]
    assert grade["calls"] == 4 and grade["p50_ms"] in (200, 300) and grade["p95_ms"] == 400
    assert grade["tokens_in"] == 400 and grade["tokens_cached"] == 200
    assert grade["cost"] == pytest.approx((400 * 2 + 40 * 8) / 1e6)
    assert model["cache_hit_rate"] == pytest.approx(200 / 500)
    assert data["tools"]["by_tool"]["web_search"] == {"calls": 3, "ok": 2, "denied": 1}
    assert data["tools"]["retried"] == 1 and data["tools"]["confirmed"] == 4
    assert data["human"] == {"confirmations_asked": 3, "denied_by_user": 1, "denial_rate": 0.3333,
                             "proposals": {}, "review_revisions": 1, "policy_allowed": 1, "user_approved": 3,
                             "approvals_by_tool": {"fetch_url": {"policy_allowed": 1, "user_approved": 1},
                                                   "web_search": {"user_approved": 2}}}
    assert "策略放行 1 次，用户批准 3 次" in metrics.render(data)
    assert data["guardrails"] == {"confirmation_denied": 1, "leak_check": 1}
    assert data["loops"]["end_reasons"] == {"max_rounds": 1}
    assert data["flows"] == {"turn:chat": {"done": 1}}
    assert sum(day["calls"] for day in data["daily"].values()) == 6
    assert "模型调用 6 次" in metrics.render(data)


def test_without_prices_metrics_show_tokens_only(tmp_path, monkeypatch):
    monkeypatch.delenv("ZHIQIU_PRICE_INPUT", raising=False)
    monkeypatch.delenv("ZHIQIU_PRICE_OUTPUT", raising=False)
    # The project's own .env may set prices; this test is about running without them.
    monkeypatch.setattr("zhiqiu.cli.load_local_env", lambda: None)
    db = tmp_path / "state.sqlite"
    store = Store(db)
    llm(store, "chat", 10)
    store.close()
    result = CliRunner().invoke(app, ["--db", str(db), "metrics", "--json"])
    data = json.loads(result.output)
    assert result.exit_code == 0 and data["priced"] is False and data["model"]["cost"] is None
    text = CliRunner().invoke(app, ["--db", str(db), "metrics"]).output
    assert "只显示 token" in text
