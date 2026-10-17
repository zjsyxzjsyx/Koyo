from datetime import date
import json

import httpx
import pytest

from zhiqiu.clock import FixedClock
from zhiqiu.materials import Material
from zhiqiu.model import OpenAICompatibleModel
from zhiqiu.runtime import LearningRuntime
from zhiqiu.search import web_search_spec
from zhiqiu.store import SCHEMA_VERSION, Store
from zhiqiu.tools import (ToolError, ToolExecutor, ToolRegistry, ToolScope, ToolSpec, post_json,
                          summarize_args)

from test_runtime import FakeModel, FakeSearch, prepare_service

PARAMS = {"type": "object", "additionalProperties": False, "required": ["query"],
          "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 20},
                         "limit": {"type": "integer", "minimum": 1, "maximum": 5}}}
NETWORK = ToolScope(frozenset({"network"}), point="二叉树遍历")


def make(handler, *, permission="network", retries=0, cap=1000, confirm=False, name="probe", source="tests"):
    registry = ToolRegistry()
    registry.register(ToolSpec(name, "测试工具", PARAMS, permission, handler, source=source,
                               max_retries=retries, max_output_chars=cap, requires_confirmation=confirm))
    audits, sleeps = [], []
    return ToolExecutor(registry, audits.append, sleep=sleeps.append), audits, sleeps


def test_invalid_args_never_reach_handler():
    calls = []
    executor, audits, _ = make(lambda args: calls.append(args) or "ok")
    for args in ({}, {"query": ""}, {"query": "x" * 21}, {"query": "ok", "extra": 1},
                 {"query": 3}, {"query": "ok", "limit": "2"}, {"query": "ok", "limit": 9}, "not-an-object"):
        with pytest.raises(ToolError) as error:
            executor.run("probe", args, NETWORK)
        assert error.value.kind == "invalid_args"
    assert calls == []
    assert {audit["status"] for audit in audits} == {"invalid_args"}
    assert all(audit["attempts"] == 0 for audit in audits)


def test_retryable_errors_back_off_and_upstream_errors_do_not_retry():
    failures = iter([ToolError("network", "断开"), ToolError("rate_limited", "429")])

    def flaky(args):
        error = next(failures, None)
        if error:
            raise error
        return "第三次成功"

    executor, audits, sleeps = make(flaky, retries=2)
    result = executor.run("probe", {"query": "遍历"}, NETWORK)
    assert result.value == "第三次成功" and result.attempts == 3
    assert sleeps == [0.5, 1.0]
    assert audits[-1]["status"] == "ok" and audits[-1]["attempts"] == 3

    def rejected(args):
        raise ToolError("upstream", "HTTP 400")

    executor, audits, sleeps = make(rejected, retries=3)
    with pytest.raises(ToolError, match="HTTP 400"):
        executor.run("probe", {"query": "遍历"}, NETWORK)
    assert sleeps == [] and audits[-1]["attempts"] == 1
    assert audits[-1]["status"] == "error" and audits[-1]["error_kind"] == "upstream"

    def always_timeout(args):
        raise ToolError("timeout", "超时")

    executor, audits, _ = make(always_timeout, retries=1)
    with pytest.raises(ToolError):
        executor.run("probe", {"query": "遍历"}, NETWORK)
    assert audits[-1]["status"] == "timeout" and audits[-1]["attempts"] == 2


def test_gate_denies_unless_scope_allows_and_user_confirms():
    calls = []
    handler = lambda args: calls.append(args) or "已执行"

    executor, audits, _ = make(handler)
    with pytest.raises(ToolError, match="未允许"):
        executor.run("probe", {"query": "遍历"}, ToolScope(frozenset({"read"})))

    def broken(spec, args):
        raise RuntimeError("界面崩溃")

    for confirm in (None, lambda spec, args: False, broken, lambda spec, args: "yes"):
        executor, audits, _ = make(handler, confirm=True)
        with pytest.raises(ToolError) as error:
            executor.run("probe", {"query": "遍历"}, ToolScope(frozenset({"network"}), confirm=confirm))
        assert error.value.kind == "denied" and audits[-1]["status"] == "denied"
    assert calls == []

    executor, audits, _ = make(handler, confirm=True)
    assert executor.run("probe", {"query": "遍历"},
                        ToolScope(frozenset({"network"}), confirm=lambda spec, args: True)).value == "已执行"
    assert audits[-1]["confirmed"] is True


def test_duplicate_registration_names_both_sources():
    registry = ToolRegistry()
    registry.register(ToolSpec("web_search", "a", PARAMS, "network", str, source="zhiqiu.search"))
    with pytest.raises(ValueError) as error:
        registry.register(ToolSpec("web_search", "b", PARAMS, "network", str, source="mcp:fetch"))
    assert "web_search" in str(error.value)
    assert "zhiqiu.search" in str(error.value) and "mcp:fetch" in str(error.value)


def test_spec_rejects_unknown_permission_and_open_params():
    with pytest.raises(ValueError, match="权限"):
        ToolSpec("x", "x", PARAMS, "admin", str, source="tests")
    with pytest.raises(ValueError, match="additionalProperties"):
        ToolSpec("x", "x", {"type": "object", "properties": {}}, "read", str, source="tests")


def test_output_is_capped_and_marked():
    executor, audits, _ = make(lambda args: "长" * 50, cap=10)
    result = executor.run("probe", {"query": "遍历"}, NETWORK)
    assert result.value == "长" * 10 and result.truncated and audits[-1]["truncated"]

    docs = [Material("web", f"https://example.org/{i}", f"h{i}", "字" * 6) for i in range(3)]
    executor, audits, _ = make(lambda args: docs, cap=10)
    result = executor.run("probe", {"query": "遍历"}, NETWORK)
    assert [len(item.content) for item in result.value] == [6, 4]
    assert result.truncated and result.result_count == 2 and result.output_chars == 10


def test_audit_summary_drops_secrets_and_cuts_long_strings():
    summary = summarize_args({"query": "q" * 300, "api_key": "sk-secret", "nested": {"Authorization": "Bearer x"},
                              "items": list(range(12))})
    assert len(summary["query"]) == 201 and summary["api_key"] == "[redacted]"
    assert summary["nested"]["Authorization"] == "[redacted]"
    assert summary["items"][-1] == "…" and len(summary["items"]) == 11
    assert "sk-secret" not in json.dumps(summary)


class Stream:
    def __init__(self, status_code=200, chunks=(b"{}",)):
        self.status_code = status_code
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_bytes(self):
        yield from self.chunks


def client_returning(stream=None, error=None):
    class Client:
        def __init__(self, **kwargs):
            self.timeout = kwargs["timeout"]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def stream(self, method, url, **kwargs):
            if error:
                raise error
            return stream
    return Client


def test_post_json_enforces_a_total_deadline_on_slow_streams(monkeypatch):
    ticks = iter(range(100))
    monkeypatch.setattr("zhiqiu.tools.httpx.Client", client_returning(Stream(chunks=[b"{"] + [b" "] * 50 + [b"}"])))
    with pytest.raises(ToolError) as error:
        post_json("https://api.example/x", {}, {}, timeout_s=5, clock=lambda: next(ticks))
    assert error.value.kind == "timeout"


@pytest.mark.parametrize("status, kind", [(429, "rate_limited"), (503, "network"), (404, "upstream")])
def test_post_json_classifies_http_status(monkeypatch, status, kind):
    monkeypatch.setattr("zhiqiu.tools.httpx.Client", client_returning(Stream(status)))
    with pytest.raises(ToolError) as error:
        post_json("https://api.example/x", {}, {}, timeout_s=5)
    assert error.value.kind == kind


@pytest.mark.parametrize("raised, kind", [(httpx.ConnectTimeout("slow"), "timeout"),
                                          (httpx.ConnectError("down"), "network")])
def test_post_json_classifies_transport_errors(monkeypatch, raised, kind):
    monkeypatch.setattr("zhiqiu.tools.httpx.Client", client_returning(error=raised))
    with pytest.raises(ToolError) as error:
        post_json("https://api.example/x", {}, {}, timeout_s=5)
    assert error.value.kind == kind


def test_model_sees_the_registered_web_search_declaration(monkeypatch):
    sent = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def post(self, url, **kwargs):
            sent.append(kwargs["json"])
            return httpx.Response(200, json={"choices": [{"message": {"tool_calls": []}}]},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr("zhiqiu.model.httpx.Client", Client)
    OpenAICompatibleModel("https://model.example/v1", "m", "k").search_query("遍历", "示例")
    registry = ToolRegistry()
    registry.register(web_search_spec(FakeSearch([])))
    assert sent[0]["tools"] == registry.openai_schema(["web_search"])


def test_prepare_audits_web_search_and_trace_links_it(tmp_path):
    web = Material("web", "https://example.org/trees", "web-hash", "可靠的遍历解释")
    store, service, path = prepare_service(tmp_path, FakeModel(enough=False), FakeSearch([web]))
    kp, _, _ = service.prepare("二叉树遍历", path, lambda *_: True)
    row = store.db.execute("SELECT * FROM tool_calls").fetchone()
    assert (row["tool"], row["permission"], row["status"], row["result_count"], row["point"]) == (
        "web_search", "network", "ok", 1, "二叉树遍历")
    assert json.loads(row["args_summary_json"]) == {"query": "知识点 解释"}
    assert store.db.execute("SELECT COUNT(*) FROM search_calls").fetchone()[0] == 0
    attempt = store.start_attempt(kp, "first")
    assert [call["id"] for call in store.trace(attempt)["tool_calls"]] == [row["id"]]
    assert len(store.export_json()["tool_calls"]) == 1
    store.forget(kp)
    assert store.db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0


def test_search_network_failure_retries_once_then_stops_learning(tmp_path):
    class DownSearch(FakeSearch):
        def search(self, query):
            self.queries.append(query)
            raise ToolError("network", "网络错误：ConnectError")

    search = DownSearch([])
    store, service, path = prepare_service(tmp_path, FakeModel(enough=False), search)
    service.executor.sleep = lambda seconds: None
    with pytest.raises(ToolError, match="网络错误"):
        service.prepare("二叉树遍历", path, lambda *_: True)
    assert search.queries == ["知识点 解释", "知识点 解释"]
    row = store.db.execute("SELECT status,error_kind,attempts FROM tool_calls").fetchone()
    assert tuple(row) == ("error", "network", 2)
    assert store.db.execute("SELECT COUNT(*) FROM knowledge_points").fetchone()[0] == 0


def test_v12_database_gains_tool_calls_with_backup(tmp_path):
    path = tmp_path / "state.sqlite"
    store = Store(path, FixedClock(date(2026, 10, 12)))
    kp = store.ensure_knowledge_point("二叉树遍历")
    store.db.execute("DROP TABLE tool_calls")
    store.db.execute("PRAGMA user_version=12")
    store.close()
    reopened = Store(path, FixedClock(date(2026, 10, 12)))
    assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 23
    assert reopened.db.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
    assert reopened.get_knowledge_point(kp)["title"] == "二叉树遍历"
    assert path.with_suffix(".sqlite.pre-v13.bak").exists()
    reopened.close()


def test_web_results_are_capped_to_save_tokens(monkeypatch):
    from zhiqiu.search import CALL_CHARS, RESULT_CHARS, ExaSearch
    long = {"results": [{"url": f"https://example.org/{i}", "highlights": ["很长" * 2000]} for i in range(5)]}
    monkeypatch.setattr("zhiqiu.search.post_json", lambda *args, **kwargs: long)
    registry = ToolRegistry()
    registry.register(web_search_spec(ExaSearch(api_key="test-key")))
    result = ToolExecutor(registry).run("web_search", {"query": "遍历"}, NETWORK)
    assert all(len(item.content) <= RESULT_CHARS for item in result.value)
    assert result.output_chars == CALL_CHARS and result.truncated
